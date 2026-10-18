"""Authenticated self-view of captured information; never a publication path."""
from __future__ import annotations

import json
import secrets
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path

from flask import Blueprint, Response, abort, g, jsonify, render_template, request, send_file, session, url_for

from nannanews import acquisition
from nannanews import daily_acquisition
from nannanews import information
from nannanews import public_reader
from nannanews import publication
from nannanews import runtime

bp = Blueprint("information_reader", __name__)
PAGE_SIZE = 20
MAX_PAGE_SIZE = 50
EXPORT_LIMIT = 500
LEDGER_STATES = (
    ("pending", "等待处理"),
    ("fetching", "正在读取原文"),
    ("ready", "原文已保存，等待提取"),
    ("modelling", "正在提取"),
    ("done", "已完成"),
    ("history_deferred", "原文留存成功，未提取"),
    ("fetch_failed", "读取失败"),
    ("model_failed", "提取失败"),
    ("model_uncertain", "结果未确认"),
    ("disallowed", "禁止访问"),
)
MAX_LEDGER_PAGE = 1_000_000
LEDGER_VISIBLE_FIELDS = (
    "source_id", "source_name", "title", "url", "record_id", "state",
    "processing_status", "processing_label", "processed_at", "checked_at", "message",
)


@bp.before_request
def require_reader():
    username = session.get("username")
    role = runtime.get_user_role(username) if username else None
    if role not in {"admin", "editor", "member"}:
        abort(401 if not role else 403)
    g.library_role = role
    if request.method == "POST":
        expected = session.get("csrf_token", "")
        supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
        if not isinstance(expected, str) or not isinstance(supplied, str) or not expected or not secrets.compare_digest(expected.encode(), supplied.encode()):
            abort(400, description="CSRF token missing or invalid")


@bp.after_request
def private_response(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.vary.add("Cookie")
    return response


@bp.errorhandler(400)
@bp.errorhandler(401)
@bp.errorhandler(403)
@bp.errorhandler(404)
@bp.errorhandler(503)
def reader_error(error):
    if request.path.startswith("/api/"):
        return jsonify(error=error.description), error.code
    return render_template("library_error.html", error=error), error.code


@bp.app_context_processor
def reader_context():
    role = getattr(g, "library_role", None)
    if role is None and session.get("username"):
        role = runtime.get_user_role(session["username"])
    return {"library_available": True, "user_role": role}


def visible_record(record):
    """Original source text is readable; detailed extraction evidence is editorial."""
    result = dict(record)
    result["source_url"] = public_reader._safe_http_url(record.get("source_url"))
    if g.library_role not in {"admin", "editor"}:
        result.pop("evidence_quotes", None)
        result["colleges"] = [
            {"name": college["name"], "relation": college["relation"]}
            for college in record.get("colleges", [])
        ]
    return result


def page_args():
    page = max(1, request.args.get("page", 1, type=int) or 1)
    per_page = min(MAX_PAGE_SIZE, max(1, request.args.get("per_page", PAGE_SIZE, type=int) or PAGE_SIZE))
    return page, per_page


def filter_args(filters):
    return {
        "q": filters["q"], "date_type": filters["date_type"],
        "date_from": filters["date_from"] or "", "date_to": filters["date_to"] or "",
        "college": filters["colleges"], "college_relation": filters["college_relation"],
        "source": filters["sources"], "tag": filters["tags"],
    }


def listing(scope="private"):
    try:
        filters = information.parse_filters(request.args)
    except ValueError as error:
        abort(400, description=str(error))
    page, per_page = page_args()
    return filters, information.query_records(filters, scope=scope, page=page, per_page=per_page)


@bp.get("/library")
def index():
    filters, result = listing()
    args = filter_args(filters)
    return render_template(
        "library_home.html", filters=filters, options=information.filter_options(scope="private"),
        records=[visible_record(record) for record in result["records"]],
        total=result["total"], page=result["page"], pages=result["pages"], per_page=result["per_page"],
        previous_url=url_for("information_reader.index", **args, page=result["page"] - 1, per_page=result["per_page"]),
        next_url=url_for("information_reader.index", **args, page=result["page"] + 1, per_page=result["per_page"]),
        api_url=url_for("information_reader.api_records", **args),
    )


@bp.get("/api/library")
def api_records():
    filters, result = listing()
    return jsonify(**{**result, "records": [visible_record(record) for record in result["records"]]}, filters=filters)


@bp.get("/library/<int:record_id>")
def detail(record_id):
    record = information.get_record(record_id, scope="private")
    if record is None:
        abort(404)
    provenance = None
    image_count = 0
    if record.get("document_id") is not None and g.library_role in {"admin", "editor"}:
        document = information.get_document(record["document_id"])
        if document:
            provenance = {key: document.get(key) for key in ("fetched_at", "digest", "canonical_url", "title")}
            image_count = len(document.get("media", []))
    return render_template("library_detail.html", record=visible_record(record), provenance=provenance,
                           image_count=image_count)


@bp.get("/api/library/<int:record_id>")
def api_record(record_id):
    record = information.get_record(record_id, scope="private")
    if record is None:
        abort(404)
    return jsonify(visible_record(record))


@bp.get("/library/<int:record_id>/images/<int:index>")
def evidence_image(record_id, index):
    if g.library_role not in {"admin", "editor"}:
        abort(403)
    record = information.get_record(record_id, scope="private")
    if record is None or record.get("document_id") is None:
        abort(404)
    document = information.get_document(record["document_id"])
    media = document.get("media", []) if document else []
    if not 0 <= index < len(media):
        abort(404)
    image = media[index]
    return acquisition._image_response(image, index)


@bp.get("/library/sources")
def sources():
    mode_values = request.args.getlist("mode")
    if len(mode_values) > 1 or (mode_values and mode_values[0] not in {"new", "history"}):
        abort(400, description="mode 必须为 new 或 history。")
    mode = mode_values[0] if mode_values else "new"

    source_values = request.args.getlist("source_id")
    if len(source_values) > 1:
        abort(400, description="source_id 只能指定一次。")
    source_id = source_values[0] if source_values else None
    if source_id == "":
        source_id = None
    if source_id and (not source_id.strip() or len(source_id) > 200 or any(ord(char) < 32 for char in source_id)):
        abort(400, description="source_id 无效。")

    state_values = request.args.getlist("state")
    valid_states = {value for value, _ in LEDGER_STATES}
    if len(state_values) > 1 or (state_values and state_values[0] not in valid_states | {""}):
        abort(400, description="state 无效。")
    state = state_values[0] if state_values else ""

    def positive_int(name, default, maximum=None):
        values = request.args.getlist(name)
        if len(values) > 1:
            abort(400, description=f"{name} 只能指定一次。")
        raw = values[0] if values else str(default)
        if not raw.isascii() or not raw.isdigit() or (maximum is not None and len(raw) > len(str(maximum))):
            abort(400, description=f"{name} 必须是范围内的正整数。")
        value = int(raw)
        if value < 1 or (maximum is not None and value > maximum):
            abort(400, description=f"{name} 超出允许范围。")
        return value

    page = positive_int("page", 1, MAX_LEDGER_PAGE)
    per_page = positive_int("per_page", PAGE_SIZE, MAX_PAGE_SIZE)

    try:
        new_status = daily_acquisition.get_status(mode="new")
        history_status = daily_acquisition.get_status(mode="history")
        mode_status = new_status if mode == "new" else history_status
        item_result = daily_acquisition.list_items(
            mode=mode, source_id=source_id, state=state or None, page=page, per_page=per_page,
        )
    except (OSError, ValueError, sqlite3.Error):
        abort(503, description="处理台账暂时无法读取，当前处理结果未确认。")
    items = []
    for item in item_result["items"]:
        visible = {key: item.get(key) for key in LEDGER_VISIBLE_FIELDS}
        visible["url"] = public_reader._safe_http_url(visible["url"])
        items.append(visible)

    last_runs = new_status.get("last_runs", {})
    history_runs = history_status.get("last_runs", {})
    new_run = last_runs.get("new") or new_status.get("last_run")
    history_run = history_runs.get("history") or history_status.get("last_run")
    legacy_run = last_runs.get("legacy") or history_runs.get("legacy")
    runs = {row["source_id"]: row for row in mode_status.get("sources", [])}
    sources = [{**source, "collection": runs.get(source["id"])} for source in information.list_sources()]
    by_id = {source['id']: source for source in sources}
    linked = {}
    with closing(runtime.connect_db()) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_source_state'").fetchone():
            for directory_source_id, raw in connection.execute('SELECT source_id,definition_json FROM daily_source_state'):
                definition = json.loads(raw)
                directory_ids = definition.get('directory_ids', [])
                if isinstance(directory_ids, list) and directory_source_id in by_id:
                    for directory_id in directory_ids:
                        if isinstance(directory_id, str):
                            linked.setdefault(directory_id, []).append(by_id[directory_source_id])
    directory = []
    directory_counts = {'website_enabled': 0, 'website_pending': 0, 'reference_checked': 0, 'no_entry': 0}
    for item in acquisition.load_catalog():
        channels = linked.get(item['id'], [])
        references = [by_id[identity] for index, _ in enumerate(item.get('source_urls') or [])
                      if (identity := f"historical-{item['id']}-{index}") in by_id]
        if any(channel['enabled'] and channel['status'] == 'verified' for channel in channels):
            coverage = 'website_enabled'
        elif channels:
            coverage = 'website_pending'
        elif any(reference.get('last_checked_at') for reference in references):
            coverage = 'reference_checked'
        else:
            coverage = 'no_entry'
        directory_counts[coverage] += 1
        directory.append({'name': item['name'], 'unit': item.get('unit'), 'coverage': coverage,
                          'channels': channels, 'references': references,
                          'workbook_count': len({p['workbook'] for p in item.get('provenance', [])})})

    current_page = item_result["page"]
    pagination_args = {"mode": mode, "source_id": source_id, "state": state or None,
                       "per_page": item_result["per_page"]}
    return render_template(
        "library_sources.html", sources=sources, status=mode_status, new_status=new_status,
        history_status=history_status, new_run=new_run, history_run=history_run, legacy_run=legacy_run,
        directory=directory, directory_counts=directory_counts, mode=mode,
        source_id=source_id, state=state, state_options=LEDGER_STATES,
        ledger_items=items, ledger_total=item_result["total"], ledger_page=current_page,
        ledger_pages=item_result["pages"], per_page=item_result["per_page"],
        previous_url=url_for("information_reader.sources", **pagination_args, page=current_page - 1),
        next_url=url_for("information_reader.sources", **pagination_args, page=current_page + 1),
    )

@bp.post("/library/export")
def export():
    try:
        filters = information.parse_filters(request.form)
        records = information.export_records(filters, scope="private", max_records=EXPORT_LIMIT)
    except ValueError as error:
        abort(400, description=str(error))
    if not records:
        abort(400, description="没有符合条件的记录，请调整筛选后再导出。")
    records = [visible_record(record) for record in records]
    exports = Path(runtime.settings.publication_dir) / "exports"
    try:
        exports.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="library-", dir=exports) as workspace:
            pdf_path = Path(workspace) / "information.pdf"
            result = publication.compile_information_pdf(records, filters, pdf_path)
            if result.returncode != 0 or not pdf_path.is_file() or pdf_path.stat().st_size == 0:
                abort(503, description="PDF 编译失败，记录仍可在信息库中浏览。")
            # send_file wraps the open descriptor. Unlinking the private temporary
            # workspace on Linux keeps streaming safe without retaining exports.
            handle = pdf_path.open("rb")
            try:
                response = send_file(handle, mimetype="application/pdf", as_attachment=True,
                                     download_name="information-unverified.pdf", conditional=False, max_age=0)
            except Exception:
                handle.close()
                raise
            response.call_on_close(handle.close)
            return response
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
        abort(503, description="PDF 导出暂时不可用，记录仍可在信息库中浏览。")
