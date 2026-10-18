"""Public-only reader routes; editor and acquisition state never crosses this boundary."""

from __future__ import annotations

import json
import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, abort, jsonify, render_template, request, send_file, session, url_for

from nannanews import runtime
from nannanews import information

bp = Blueprint("public_reader", __name__)


@bp.errorhandler(400)
def invalid_filters(error):
    if request.path.startswith("/api/"):
        return jsonify(error=error.description), 400
    return error.get_response()

PAGE_SIZE = 12
MAX_PAGE_SIZE = 50
MAX_SEARCH_LENGTH = 200

@bp.app_template_filter("display_date")
def _display_date(value):
    """Format presentation only; preserve date-only and unknown timezone semantics."""
    if value is None:
        return None
    raw = value.isoformat() if isinstance(value, (date, datetime)) else str(value).strip()
    if not raw or len(raw) > 64 or len(raw) < 10 or raw[4] != "-" or raw[7] != "-":
        return None
    try:
        if len(raw) == 10:
            parsed = date.fromisoformat(raw)
            return {"datetime": parsed.isoformat(), "date": parsed.isoformat(), "time": "", "zone": ""}
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        zone = ""
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone(timedelta(hours=8)))
            zone = "北京时间"
        return {"datetime": parsed.isoformat(), "date": parsed.date().isoformat(),
                "time": parsed.strftime("%H:%M:%S"), "zone": zone}
    except (ValueError, OverflowError):
        return None


LECTURE_TEXT_FIELDS = (
    "venue", "organizer", "abstract", "target_audience", "registration", "online_access"
)
PERSON_FIELDS = ("name", "title", "biography")

# Keep date checking in SQL for bounded pagination, then validate again in Python
# so SQLite's permissive date handling cannot admit impossible calendar dates.
_PUBLIC_WHERE = """status = 'approved'
    AND publish_date IS NOT NULL
    AND publish_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
    AND date(publish_date) IS NOT NULL
    AND date(substr(publish_date,1,10), '+0 days') = substr(publish_date,1,10)
    AND date(publish_date) <> '1970-01-01'
    AND date(publish_date) <= date('now', '+8 hours')"""


def _iso_date(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if len(raw) > 64:
        return None
    match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:$|[ T])", raw)
    if not match:
        return None
    date_text = match.group(1)
    try:
        parsed_date = date.fromisoformat(date_text)
        if len(raw) > 10:
            datetime.fromisoformat(raw.replace(" ", "T", 1))
    except ValueError:
        return None
    if parsed_date.isoformat() != date_text or parsed_date == date(1970, 1, 1) or parsed_date > (datetime.now(timezone.utc) + timedelta(hours=8)).date():
        return None
    return parsed_date.isoformat()


def _safe_http_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or any(ord(ch) < 32 for ch in candidate):
        return None
    try:
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        parsed.port  # reject malformed ports
    except ValueError:
        return None
    return candidate


def _public_lecture_data(raw: object) -> dict | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None

    result: dict = {}
    for key in ("series", "title", "schedule"):
        section = value.get(key)
        if isinstance(section, dict):
            result[key] = {str(k): str(v) for k, v in section.items() if k in {"name", "number", "primary", "secondary", "date", "start_time", "end_time"} and isinstance(v, (str, int, float))}
    for key in LECTURE_TEXT_FIELDS:
        field = value.get(key)
        if isinstance(field, str):
            result[key] = field
    for key in ("speakers", "discussants"):
        people = value.get(key)
        if isinstance(people, list):
            result[key] = [
                {field: person[field] for field in PERSON_FIELDS if isinstance(person.get(field), str)}
                for person in people if isinstance(person, dict)
            ]
    for key in ("inviter", "moderator"):
        person = value.get(key)
        if isinstance(person, dict):
            result[key] = {field: person[field] for field in PERSON_FIELDS if isinstance(person.get(field), str)}
        elif isinstance(person, list):
            result[key] = [
                {field: item[field] for field in PERSON_FIELDS if isinstance(item.get(field), str)}
                for item in person if isinstance(item, dict)
            ]
    return result or None


def _dto(row: tuple) -> dict | None:
    entry_id, title, description, link, tag, due_time, publish_date, lecture_data, use_image, entry_type = row
    public_date = _iso_date(publish_date)
    if not public_date:
        return None
    image_url = None
    source_url = _safe_http_url(link)
    if use_image and isinstance(link, str) and Path(link).name == link and Path(link).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        image_url = f"/news/{entry_id}/image"
        source_url = None
    normalized_lecture = _public_lecture_data(lecture_data) if tag == "讲座" or entry_type == "讲座" else None
    return {
        "id": int(entry_id),
        "title": str(title or ""),
        "description": str(description or ""),
        "source_url": source_url,
        "tag": str(tag or "") or None,
        "due_time": str(due_time) if due_time else None,
        "publish_date": public_date,
        "lecture_data": normalized_lecture,
        "image_url": image_url,
    }


PUBLIC_METADATA = (
    "source_id", "source_name", "source_published_date", "event_date",
    "deadline_date", "publication_date", "verification_status",
)


def _public_records(records: list[dict]) -> list[dict]:
    if not records:
        return []
    ids = [record["entry_id"] for record in records]
    conn = runtime.connect_db()
    try:
        rows = conn.execute(
            f"""SELECT id, title, description, link, tag, due_time, publish_date,
                       lecture_data, use_image, type FROM entries
                WHERE id IN ({','.join('?' for _ in ids)}) AND {_PUBLIC_WHERE}""", ids,
        ).fetchall()
    finally:
        conn.close()
    legacy = {row[0]: dto for row in rows if (dto := _dto(row)) is not None}
    result = []
    for record in records:
        dto = legacy.get(record["entry_id"])
        if dto is None:
            continue
        dto.update({key: record.get(key) for key in PUBLIC_METADATA})
        dto["information_id"] = record["id"]
        dto["colleges"] = [{"name": college["name"], "relation": college["relation"]}
                           for college in record.get("colleges", [])]
        if record.get("source_url"):
            dto["source_url"] = _safe_http_url(record["source_url"])
        result.append(dto)
    return result


def _entry_by_id(entry_id: int) -> dict | None:
    record = information.get_entry_record(entry_id, scope="public")
    entries = _public_records([record]) if record else []
    return entries[0] if entries else None


def _filtered_entries():
    try:
        filters = information.parse_filters(request.args)
    except ValueError as error:
        abort(400, description=str(error))
    page, per_page = _page_args()
    result = information.query_records(filters, scope="public", page=page, per_page=per_page)
    return filters, {**result, "entries": _public_records(result["records"])}


def _filter_args(filters):
    return {"q": filters["q"], "date_type": filters["date_type"],
            "date_from": filters["date_from"] or "", "date_to": filters["date_to"] or "",
            "college": filters["colleges"], "college_relation": filters["college_relation"],
            "source": filters["sources"], "tag": filters["tags"]}


def _validated_date(value: str) -> str:
    if len(value) != 10 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        abort(404)
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        abort(404)
    if parsed.isoformat() != value or parsed == date(1970, 1, 1) or parsed > (datetime.now(timezone.utc) + timedelta(hours=8)).date():
        abort(404)
    return value


def _page_args() -> tuple[int, int]:
    page = max(1, request.args.get("page", default=1, type=int) or 1)
    per_page = request.args.get("per_page", default=PAGE_SIZE, type=int) or PAGE_SIZE
    return page, min(max(1, per_page), MAX_PAGE_SIZE)


def _load_sources() -> list[dict]:
    path = runtime.path("source_catalog.json")
    try:
        with Path(path).open("r", encoding="utf-8") as source_file:
            catalog = json.load(source_file)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        abort(503, description="来源目录暂时不可用")

    if not isinstance(catalog, list):
        return []
    result = []
    for source in catalog:
        if not isinstance(source, dict):
            continue
        name = source.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        raw_urls = source.get("source_urls")
        if not isinstance(raw_urls, list):
            continue
        urls = [url for item in raw_urls if (url := _safe_http_url(item))]
        if not urls:
            continue
        category = source.get("category")
        subcategory = source.get("subcategory")
        description = source.get("description")
        result.append({
            "name": name.strip(),
            "source_urls": urls,
            "category": category.strip() if isinstance(category, str) and category.strip() else None,
            "subcategory": subcategory.strip() if isinstance(subcategory, str) and subcategory.strip() else None,
            "description": description.strip() if isinstance(description, str) and description.strip() else None,
        })
    return sorted(result, key=lambda item: (item["category"] or "", item["name"].casefold()))



def publication_signature(published_on: str) -> str:
    """Bind a PDF to all published content it may include, including older deadlines."""
    conn = runtime.connect_db()
    try:
        rows = conn.execute(f'''SELECT id, title, description, link, tag, type, due_time,
            publish_date, short_title, lecture_data, use_image, priority FROM entries
            WHERE {_PUBLIC_WHERE} AND date(publish_date) <= ? ORDER BY id''', (published_on,)).fetchall()
    finally:
        conn.close()
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()



@bp.get("/")
def index():
    filters, result = _filtered_entries()
    args = _filter_args(filters)
    return render_template(
        "public_home.html", filters=filters, options=information.filter_options(scope="public"),
        entries=result["entries"], page=result["page"], per_page=result["per_page"],
        total=result["total"], pages=result["pages"],
        previous_url=url_for("public_reader.index", **args, page=result["page"] - 1, per_page=result["per_page"]),
        next_url=url_for("public_reader.index", **args, page=result["page"] + 1, per_page=result["per_page"]),
        api_url=url_for("public_reader.api_entries", **args),
    )


@bp.get("/news/<int:entry_id>")
def detail(entry_id: int):
    entry = _entry_by_id(entry_id)
    if not entry:
        abort(404)
    return render_template("public_detail.html", entry=entry)


@bp.get("/news/<int:entry_id>/image")
def image(entry_id: int):
    entry = _entry_by_id(entry_id)
    if not entry or not entry["image_url"]:
        abort(404)
    conn = runtime.connect_db()
    try:
        row = conn.execute(
            f"SELECT link FROM entries WHERE id = ? AND {_PUBLIC_WHERE} AND use_image = 1",
            (entry_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not isinstance(row[0], str) or Path(row[0]).name != row[0]:
        abort(404)
    root = Path(runtime.settings.upload_dir).resolve()
    image_path = (root / row[0]).resolve()
    if image_path.parent != root or not image_path.is_file():
        abort(404)
    return send_file(image_path, conditional=True, max_age=3600)


@bp.get("/editor/entries/<int:entry_id>/image")
def editor_image(entry_id: int):
    username = session.get("username")
    if not username:
        abort(401)
    if runtime.get_user_role(username) not in {"admin", "editor"}:
        abort(403)
    conn = runtime.connect_db()
    try:
        row = conn.execute("SELECT link FROM entries WHERE id = ?", (entry_id,)).fetchone()
    finally:
        conn.close()
    if not row or not isinstance(row[0], str):
        abort(404)
    filename = row[0]
    suffix = Path(filename).suffix.lower()
    if Path(filename).name != filename or suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        abort(404)
    root = Path(runtime.settings.upload_dir).resolve()
    image_path = (root / filename).resolve()
    if image_path.parent != root or not image_path.is_file():
        abort(404)
    mime_type = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[suffix]
    response = send_file(image_path, mimetype=mime_type, conditional=False, max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.get("/issues/<date_text>")
def issue(date_text: str):
    issue_date = _validated_date(date_text)
    conn = runtime.connect_db()
    try:
        rows = conn.execute(f'''SELECT id, title, description, link, tag, due_time, publish_date,
            lecture_data, use_image, type FROM entries WHERE {_PUBLIC_WHERE}
            AND date(publish_date)=? ORDER BY priority DESC, id''', (issue_date,)).fetchall()
    finally:
        conn.close()
    entries = [dto for row in rows if (dto := _dto(row)) is not None]
    if not entries:
        abort(404)
    deadlines = [entry for entry in entries if entry["due_time"] and entry["due_time"][:10] >= issue_date]
    deadlines.sort(key=lambda entry: (entry["due_time"] or "", entry["title"]))
    return render_template("public_issue.html", issue_date=issue_date, entries=entries, deadlines=deadlines)


@bp.get("/sources")
def sources():
    catalog = _load_sources()
    all_categories = sorted({source["category"] for source in catalog if source["category"]})
    query_text = request.args.get("q", "").strip()[:MAX_SEARCH_LENGTH]
    query = query_text.casefold()
    category = request.args.get("category", "").strip()[:80]
    filtered = catalog
    if query:
        filtered = [source for source in filtered if query in source["name"].casefold() or query in (source["description"] or "").casefold()]
    if category:
        filtered = [source for source in filtered if source["category"] == category]
    return render_template("public_sources.html", sources=filtered, query=query_text, selected_category=category, categories=all_categories)


@bp.get("/api/entries")
def api_entries():
    filters, result = _filtered_entries()
    return jsonify(entries=result["entries"], page=result["page"], per_page=result["per_page"],
                   total=result["total"], pages=result["pages"], filters=filters)


@bp.get("/api/entries/<int:entry_id>")
def api_entry(entry_id: int):
    entry = _entry_by_id(entry_id)
    if not entry:
        abort(404)
    return jsonify(entry)


@bp.get("/issues/<date_text>/pdf")
def issue_pdf(date_text: str):
    issue_date = _validated_date(date_text)
    filters = information.parse_filters({"date_type": "publication", "date": issue_date})
    entries = information.query_records(filters, scope="public", page=1, per_page=1)["records"]
    if not entries:
        abort(404)
    pdf_path = (Path(runtime.settings.archive_dir) / f"{issue_date}.pdf").resolve()
    archive_root = Path(runtime.settings.archive_dir).resolve()
    if pdf_path.parent != archive_root or not pdf_path.is_file():
        abort(404)
    try:
        snapshot = json.loads((archive_root / f'{issue_date}.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        abort(404)
    if snapshot.get('public_signature') != publication_signature(issue_date):
        abort(404)
    response = send_file(pdf_path, mimetype='application/pdf', as_attachment=True, download_name=f'{issue_date}.pdf', conditional=False, max_age=0)
    response.headers['Cache-Control'] = 'no-store'
    return response
