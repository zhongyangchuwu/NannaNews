"""Build approved bulletin content and reuse archived publication metadata."""

from datetime import datetime, timedelta, timezone
import json
import os
import re

from nannanews import runtime
from nannanews.runtime import connect_db
from .inputs import LINK_REGEX, allowed_file, is_valid_url, valid_date

DEFAULT_SAYING = "謹言慎行"
DEFAULT_LOC = "南京市栖霞区"
DEFAULT_COLUMNS = 4
MIN_COLUMNS = 1
MAX_COLUMNS = 8
MAX_PNG_BYTES = int(30.5 * 1024 * 1024)
DEFAULT_PPI = 300
NOTICE_SIZES = ("huge", "large", "medium")
MAX_NOTICES = 20

def get_beijing_date_str(now=None):
    if now is None:
        now = datetime.now(timezone.utc) + timedelta(hours=8)
    else:
        pass
    if now.hour < 12:
        d = (now - timedelta(days=1)).date()
    else:
        d = now.date()
    return d.strftime("%Y-%m-%d")

def _read_archived_no_map():
    no_map = {}
    max_no = 0
    arc_dir = runtime.settings.archive_dir
    if not os.path.isdir(arc_dir):
        return no_map, max_no
    for fn in os.listdir(arc_dir):
        if not fn.endswith('.json'):
            continue
        fp = os.path.join(arc_dir, fn)
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                obj = json.load(f)
            no = obj.get('data', {}).get('no')
            date = obj.get('data', {}).get('date')
            if isinstance(no, bool):
                continue
            if isinstance(no, str):
                try:
                    no = int(no)
                except (TypeError, ValueError):
                    continue
            if isinstance(no, int) and no > 0:
                if no > max_no:
                    max_no = no
                if isinstance(date, str) and len(date) >= 8:
                    if date not in no_map or no > no_map[date]:
                        no_map[date] = no
        except Exception:
            continue
    return no_map, max_no

def get_issue_no(date_str):
    no_map, max_no = _read_archived_no_map()
    if date_str in no_map:
        return no_map[date_str], True
    try:
        if runtime.path('publications/latest.json').exists():
            with runtime.path('publications/latest.json').open('r', encoding='utf-8') as f:
                obj = json.load(f)
            if obj.get('data', {}).get('date') == date_str:
                no = obj['data'].get('no')
                if isinstance(no, int) and no > 0:
                    return no, True
                if isinstance(no, str):
                    try:
                        return int(no), True
                    except (TypeError, ValueError):
                        pass
    except Exception:
        pass
    return (max_no + 1 if max_no else 1), False

def _sanitize_columns(raw, fallback=DEFAULT_COLUMNS):
    try:
        if isinstance(raw, bool):
            raise ValueError("bool is not columns")
        v = int(raw)
    except (TypeError, ValueError):
        try:
            return int(fallback)
        except (TypeError, ValueError):
            return DEFAULT_COLUMNS
    return max(MIN_COLUMNS, min(MAX_COLUMNS, v))

def get_columns(date_str):
    arc_fp = runtime.settings.archive_dir / (date_str + '.json')
    if os.path.exists(arc_fp):
        try:
            with open(arc_fp, 'r', encoding='utf-8') as f:
                obj = json.load(f)
            return _sanitize_columns(obj.get('data', {}).get('columns'), fallback=DEFAULT_COLUMNS)
        except Exception:
            pass
    latest_fp = runtime.path('publications/latest.json')
    if os.path.exists(latest_fp):
        try:
            with open(latest_fp, 'r', encoding='utf-8') as f:
                obj = json.load(f)
            if obj.get('data', {}).get('date') == date_str:
                return _sanitize_columns(obj.get('data', {}).get('columns'), fallback=DEFAULT_COLUMNS)
        except Exception:
            pass
    return DEFAULT_COLUMNS

def get_saying_loc(date_str):
    arc_fp = runtime.settings.archive_dir / (date_str + '.json')
    if os.path.exists(arc_fp):
        try:
            with open(arc_fp, 'r', encoding='utf-8') as f:
                obj = json.load(f)
            saying = obj.get('data', {}).get('saying') or DEFAULT_SAYING
            loc = obj.get('data', {}).get('loc') or DEFAULT_LOC
            return saying, loc
        except Exception:
            pass
    latest_fp = runtime.path('publications/latest.json')
    if os.path.exists(latest_fp):
        try:
            with open(latest_fp, 'r', encoding='utf-8') as f:
                obj = json.load(f)
            saying = obj.get('data', {}).get('saying') or DEFAULT_SAYING
            loc = obj.get('data', {}).get('loc') or DEFAULT_LOC
            return saying, loc
        except Exception:
            pass
    return DEFAULT_SAYING, DEFAULT_LOC

def _sanitize_notices(raw):
    out = []
    if not isinstance(raw, list):
        return out
    for n in raw:
        if not isinstance(n, dict):
            continue
        text = str(n.get("text", "") or "").strip()
        if not text:
            continue
        size = n.get("size", "medium")
        if size not in NOTICE_SIZES:
            size = "medium"
        out.append({"text": text, "size": size})
        if len(out) >= MAX_NOTICES:
            break
    return out

def get_notices(date_str):
    arc_fp = runtime.settings.archive_dir / (date_str + '.json')
    if os.path.exists(arc_fp):
        try:
            with open(arc_fp, 'r', encoding='utf-8') as f:
                obj = json.load(f)
            ns = _sanitize_notices(obj.get('data', {}).get('notices'))
            return ns
        except Exception:
            pass
    latest_fp = runtime.path('publications/latest.json')
    if os.path.exists(latest_fp):
        try:
            with open(latest_fp, 'r', encoding='utf-8') as f:
                obj = json.load(f)
            if obj.get('data', {}).get('date') == date_str:
                return _sanitize_notices(obj.get('data', {}).get('notices'))
        except Exception:
            pass
    return []

def _parse_notices(form):
    texts = form.getlist("notice_text")
    sizes = form.getlist("notice_size")
    raw = [{"text": t, "size": s} for t, s in zip(texts, sizes)]
    return _sanitize_notices(raw)

def typst(date, saying=None, loc=None, no=None, columns=None, notices=None):
    conn = connect_db()
    c = conn.cursor()
    date = valid_date(date)
    c.execute('''SELECT title, description, link, tag, type, id, priority, upload_time, use_image
        FROM entries WHERE status='approved' AND DATE(publish_date)=?
        AND publish_date <> '1970-01-01' ORDER BY upload_time ASC''', (date,))
    rows = c.fetchall()
    conn.close()
    other, college, club, career, lecture  = [], [], [], [], []
    _ref_pending = []
    for row in rows:
        if len(row) == 9:
            title, description, link, tag, sector, num, pri, upload_time, ui = row
        elif len(row) == 8:
            title, description, link, tag, sector, num, upload_time, ui = row
            pri = 0
        else:
            title, description, link, tag, sector, num = row
            pri, upload_time, ui = 0, "", 0
        try:
            pri = int(pri) if pri is not None else 0
        except (TypeError, ValueError):
            pri = 0
        try:
            ui = int(ui) if ui is not None else 0
        except (TypeError, ValueError):
            ui = 0
        if sector == "DDLOnly":
            continue
        try:
            description = re.split(LINK_REGEX, description)
        except:
            continue
        splitted = []
        for e in description:
            if is_valid_url(e):
                splitted.append({"type": "link", "content": e})
            else:
                splitted.append({"type": "text", "content": e})
        description = splitted
        if allowed_file(link):
            image = link if ui == 1 else None
            link = None
        else:
            image = None
        item = {"title": title, "description": description, "link": link, "id": num, "priority": pri, "image": image}
        if sector == "参考消息":
            _ref_pending.append((pri, str(upload_time or ""), item))
        elif(tag == "讲座" or sector == "讲座"):
            lecture.append(item)
        elif(tag == "院级活动"):
            college.append(item)
        elif(tag == "社团活动"):
            club.append(item)
        elif(tag == "招聘升学宣讲"):
            career.append(item)
        else:
            other.append(item)
    _ref_pending.sort(key=lambda t: (-t[0], t[1]))
    reference = [t[2] for t in _ref_pending]
    if saying is None or loc is None:
        _d_saying, _d_loc = get_saying_loc(date)
        if saying is None:
            saying = _d_saying
        if loc is None:
            loc = _d_loc
    if no is None:
        no, _ = get_issue_no(date)
    else:
        try:
            no = int(no)
        except (TypeError, ValueError):
            no, _ = get_issue_no(date)
    if columns is None:
        columns = get_columns(date)
    else:
        columns = _sanitize_columns(columns, fallback=get_columns(date))
    if notices is None:
        notices = get_notices(date)
    else:
        notices = _sanitize_notices(notices)
    data = {
        "date": date,
        "no": no,
        "saying": saying,
        "loc": loc,
        "columns": columns,
        "college": college,
        "club": club,
        "career": career,
        "lecture": lecture,
        "other": other,
        "reference": reference,
        "notices": notices
    }
    conn = connect_db()
    c = conn.cursor()
    c.execute("SELECT title, link, tag, due_time, publish_date, short_title, type, id FROM entries WHERE status='approved' AND due_time IS NOT NULL AND due_time > ? AND DATE(publish_date) <= ? AND publish_date >= '2023-01-01' AND type != '参考消息' ORDER BY due_time ASC", (date, date))
    due_entries = c.fetchall()
    conn.close()
    other_due, college_due, club_due, career_due, lecture_due  = [], [], [], [], []
    for title, link, tag, due_time, publish_date, short_title, sector, num in due_entries:
        if short_title:
            title = short_title
        if allowed_file(link):
            link = None
        if(tag == "讲座" or sector == "讲座"):
            lecture_due.append({"title": title, "link": link, "due_time": due_time, "publish_date": publish_date[:10], "id": num})
        elif(tag == "院级活动"):
            college_due.append({"title": title, "link": link, "due_time": due_time, "publish_date": publish_date[:10], "id": num})
        elif(tag == "社团活动"):
            club_due.append({"title": title, "link": link, "due_time": due_time, "publish_date": publish_date[:10], "id": num})
        elif(tag == "招聘升学宣讲"):
            career_due.append({"title": title, "link": link, "due_time": due_time, "publish_date": publish_date[:10], "id": num})
        else:
            other_due.append({"title": title, "link": link, "due_time": due_time, "publish_date": publish_date[:10], "id": num})
    due = {
        "college": college_due,
        "club": club_due,
        "career": career_due,
        "lecture": lecture_due,
        "other": other_due
    }

    return {"data": data, "due": due}


