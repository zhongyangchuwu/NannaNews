"""Historical bulletin maintenance, rendering and private artifact downloads."""

import json
from pathlib import Path
import subprocess
import tempfile

from flask import abort, flash, redirect, render_template, request, send_file, url_for

from nannanews import public_reader, runtime
from nannanews.publication import atomic_output, compile_pdf, render_workspace
from nannanews.runtime import connect_db
from .bulletin import (DEFAULT_LOC, DEFAULT_PPI, DEFAULT_SAYING, _parse_notices,
                      _sanitize_columns, get_beijing_date_str, get_columns,
                      get_issue_no, get_saying_loc, typst)
from .inputs import valid_date
from .rendering import compile_png_adaptive
from .security import editor_required, login_required

@editor_required
def preview_edit():
    return render_template("preview_edit.html")

@login_required
def latex_entries(date):
    conn = connect_db()
    c = conn.cursor()
    c.execute("SELECT title, description, link, tag, describer FROM entries WHERE DATE(publish_date)=? ORDER BY tag ASC", (date,))
    entries = c.fetchall()
    conn.close()

    def escape_latex(text):
        if not text:
            return ""
        text = text.replace('\\', r'\textbackslash{}')
        special_chars = {
            '&': r'\&',
            '%': r'\%',
            '$': r'\$',
            '#': r'\#',
            '_': r'\_',
            '{': r'\{',
            '}': r'\}',
            '~': r'\textasciitilde{}',
            '^': r'\^{}',
        }
        for char, replacement in special_chars.items():
            text = text.replace(char, replacement)
        return text

    latex_output = ""
    for title, description, link, tag, describer in entries:
        title = escape_latex(title)
        title = title.rstrip('\r\n')
        description = escape_latex(description).replace('\n', r'\\')
        if tag in ["讲座", "院级活动", "社团活动", "招聘升学宣讲"]:
            latex_output += r"\subsection{" + title + "} % " + tag + " describer: " + describer + "\n"
        else:
            latex_output += r"\section{" + title + "} % " + tag + " describer: " + describer + "\n"
        latex_output += description + "\n"
        if link and len(link) > 10:
            latex_output += "\\\\详见：" + r"\url{" + link + "}" + "\n\n"
    return latex_output, 200, {'Content-Type': 'text/plain; charset=utf-8'}

@editor_required
def publish():
    date_str = valid_date(request.values.get('date') or get_beijing_date_str(), past_only=True)
    if request.method == 'POST':
        if request.form.get('action', 'preview_pdf') != 'preview_pdf':
            abort(400, description='仅支持由已审阅条目生成刊物。')
        try:
            _render_issue(date_str)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            flash(f'PDF 未更新：{error}', 'warning')
        else:
            flash(f'{date_str} PDF 已生成；公开内容与已审阅条目一致。')
        return redirect(url_for('publish', date=date_str))
    return render_template('publish.html', **_publish_context(date_str))

def _render_issue(date_str, *, lecture=False, png=False):
    _save_priorities_from_form(request.form)
    _auto_tidy_date(date_str)
    signature = public_reader.publication_signature(date_str)
    metadata = {'columns': _sanitize_columns(request.form.get('columns'), fallback=get_columns(date_str))}
    if not lecture:
        try:
            no = int(request.form.get('no') or get_issue_no(date_str)[0])
        except (TypeError, ValueError):
            abort(400, description='期号须为整数。')
        metadata.update(no=max(1, no), saying=request.form.get('saying') or DEFAULT_SAYING,
                        loc=request.form.get('loc') or DEFAULT_LOC, notices=_parse_notices(request.form))
    issue = typst(date_str, **metadata)
    if not any(issue['data'][key] for key in ('other', 'college', 'club', 'career', 'lecture', 'reference')) and not any(issue['due'].values()):
        raise ValueError('该日期没有已审阅并发布的内容。')
    if signature != public_reader.publication_signature(date_str):
        raise RuntimeError('内容在生成期间发生变化，请重新生成。')
    issue['public_signature'] = signature
    template = 'lecture_template.typ' if lecture else 'news_template.typ'
    archive = runtime.settings.lecture_archive_dir if lecture else runtime.settings.archive_dir
    prefix = 'lecture' if lecture else 'news'
    try:
        ppi = int(request.form.get('ppi', DEFAULT_PPI))
    except (TypeError, ValueError):
        abort(400, description='PPI 须为整数。')
    with tempfile.TemporaryDirectory(prefix='.issue-', dir=runtime.settings.publication_dir) as temporary:
        staging = Path(temporary)
        source = staging / 'issue.json'
        source.write_text(json.dumps(issue, ensure_ascii=False, indent=2), encoding='utf-8')
        if png:
            with render_workspace(source, template) as (workspace, staged_template):
                actual_ppi, output, size, tried = compile_png_adaptive(staged_template, str(workspace / 'output'), workspace, requested_ppi=ppi)
                if not output or not size:
                    raise RuntimeError('PNG 编译未产生有效文件。')
                if signature != public_reader.publication_signature(date_str):
                    raise RuntimeError('内容在编译期间发生变化，请重新生成。')
                atomic_output(output, runtime.settings.publication_dir / f'{prefix}_{date_str}.png')
            return actual_ppi, size
        output = staging / 'issue.pdf'
        result = compile_pdf(source, template, output)
        if result.returncode:
            raise RuntimeError((result.stderr or 'Typst 编译失败')[-1000:])
        if signature != public_reader.publication_signature(date_str):
            raise RuntimeError('内容在编译期间发生变化，请重新生成。')
        atomic_output(output, archive / f'{date_str}.pdf')
        atomic_output(source, archive / f'{date_str}.json')
        atomic_output(source, runtime.settings.publication_dir / ('lecture_latest.json' if lecture else 'latest.json'))
    return None

def _publish_context(date_str, raw_content=None, ppi=None, columns=None):
    no, existed = get_issue_no(date_str)
    saying, loc = get_saying_loc(date_str)
    if columns is None:
        columns = get_columns(date_str)
    else:
        columns = _sanitize_columns(columns, fallback=get_columns(date_str))
    try:
        if runtime.path('publications/latest.json').exists():
            with runtime.path('publications/latest.json').open('r', encoding='utf-8') as f:
                _lj = json.load(f)
            if _lj.get("data", {}).get("date") == date_str:
                saying = _lj["data"].get("saying", saying)
                loc = _lj["data"].get("loc", loc)
                columns = _sanitize_columns(_lj["data"].get("columns", columns), fallback=columns)
                try:
                    no = int(_lj["data"].get("no", no))
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    issue = typst(date_str, saying=saying, loc=loc, no=no, columns=columns)
    if raw_content is None:
        raw_content = json.dumps(issue, ensure_ascii=False, indent=2)
    if ppi is None:
        ppi = DEFAULT_PPI
    else:
        try:
            ppi = int(ppi)
        except (TypeError, ValueError):
            ppi = DEFAULT_PPI
    default_date = get_beijing_date_str()
    return {
        "date": date_str,
        "no": no,
        "no_existed": existed,
        "saying": saying,
        "loc": loc,
        "columns": columns,
        "ppi": ppi,
        "issue": issue,
        "content": raw_content,
        "default_date": default_date,
        "max_png_mb": 30.5,
    }

def _save_priorities_from_form(form):
    conn = connect_db()
    c = conn.cursor()
    try:
        c.execute("SELECT id FROM entries")
        valid_ids = {str(r[0]) for r in c.fetchall()}
    except Exception:
        valid_ids = set()
    updates = 0
    for key, val in form.items():
        if key.startswith("priority_"):
            eid = key[len("priority_"):]
            if eid not in valid_ids:
                continue
            try:
                p = int(val or 0)
            except (TypeError, ValueError):
                p = 0
            c.execute("UPDATE entries SET priority=? WHERE id=?", (p, int(eid)))
            updates += 1
    conn.commit()
    conn.close()
    return updates

def _auto_tidy_date(date_str):
    issue = typst(date_str)
    conn = connect_db()
    c = conn.cursor()
    changed = 0
    for key in ("other", "lecture", "college", "club", "career", "reference"):
        items = issue["data"].get(key) or []
        pris = []
        for m in items:
            try:
                pris.append(int(m.get("priority", 0) or 0))
            except (TypeError, ValueError):
                pris.append(0)
        levels = sorted(set(pris), reverse=True)
        if len(levels) <= 1:
            continue
        remap = {lv: (len(levels) - 1 - i) for i, lv in enumerate(levels)}
        for m, pr in zip(items, pris):
            new_p = remap[pr]
            if new_p != pr:
                c.execute("UPDATE entries SET priority=? WHERE id=?", (new_p, m.get("id")))
                changed += 1
    conn.commit()
    conn.close()
    return changed

@editor_required
def publish_png():
    date_str = valid_date(request.form.get('date') or get_beijing_date_str(), past_only=True)
    try:
        ppi, size = _render_issue(date_str, png=True)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        flash(f'PNG 未更新：{error}', 'warning')
    else:
        flash(f'PNG 已生成：{ppi} PPI，{size / 1024 / 1024:.1f} MB。')
    return redirect(url_for('publish', date=date_str))

def _lecture_context(date_str, raw_content=None, ppi=None, columns=None):
    if columns is None:
        columns = get_columns(date_str)
    else:
        columns = _sanitize_columns(columns, fallback=get_columns(date_str))
    issue = typst(date_str, columns=columns)
    if raw_content is None:
        raw_content = json.dumps(issue, ensure_ascii=False, indent=2)
    if ppi is None:
        ppi = DEFAULT_PPI
    else:
        try:
            ppi = int(ppi)
        except (TypeError, ValueError):
            ppi = DEFAULT_PPI
    default_date = get_beijing_date_str()
    return {
        "date": date_str,
        "columns": columns,
        "ppi": ppi,
        "issue": issue,
        "content": raw_content,
        "default_date": default_date,
        "max_png_mb": 30.5,
    }

@editor_required
def publish_lecture():
    date_str = valid_date(request.values.get('date') or get_beijing_date_str(), past_only=True)
    if request.method == 'POST':
        if request.form.get('action', 'preview_pdf') != 'preview_pdf':
            abort(400)
        try:
            _render_issue(date_str, lecture=True)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            flash(f'讲座 PDF 未更新：{error}', 'warning')
        else:
            flash(f'{date_str} 讲座 PDF 已生成。')
        return redirect(url_for('publish_lecture', date=date_str))
    return render_template('publish_lecture.html', **_lecture_context(date_str))

@editor_required
def publish_lecture_png():
    date_str = valid_date(request.form.get('date') or get_beijing_date_str(), past_only=True)
    try:
        ppi, size = _render_issue(date_str, lecture=True, png=True)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        flash(f'讲座 PNG 未更新：{error}', 'warning')
    else:
        flash(f'讲座 PNG 已生成：{ppi} PPI，{size / 1024 / 1024:.1f} MB。')
    return redirect(url_for('publish_lecture', date=date_str))

def _dl_filename(kind, date_str):
    y, m, d = date_str.split("-")
    dotted = f"{y}.{m}.{d}"
    short = f"{y[2:]}.{m}.{d}"
    suffix = ".png" if kind.endswith("_png") else ".pdf"
    if kind.startswith("lec_"):
        return f"lec{short}{suffix}"
    return f'{runtime.settings.site_name}{dotted}{suffix}'

@editor_required
def download(kind, date):
    date = valid_date(date, past_only=True)
    candidates = {
        'news_pdf': runtime.settings.archive_dir / f'{date}.pdf',
        'news_png': runtime.settings.publication_dir / f'news_{date}.png',
        'lec_pdf': runtime.settings.lecture_archive_dir / f'{date}.pdf',
        'lec_png': runtime.settings.publication_dir / f'lecture_{date}.png',
    }
    artifact = candidates.get(kind)
    if artifact is None:
        abort(404)
    if not artifact.is_file():
        flash('该日期尚无文件，先生成再下载。')
        return redirect(url_for('publish_lecture' if kind.startswith('lec_') else 'publish', date=date))
    response = send_file(artifact, as_attachment=True, download_name=_dl_filename(kind, date), conditional=False, max_age=0)
    response.headers['Cache-Control'] = 'private, no-store'
    return response


def register(app):
    """Register controller routes without changing public endpoint names."""
    app.add_url_rule("/preview_edit", view_func=preview_edit)
    app.add_url_rule('/latex/<date>', view_func=latex_entries)
    app.add_url_rule('/publish', view_func=publish, methods=['GET', 'POST'])
    app.add_url_rule('/publish/png', view_func=publish_png, methods=['POST'])
    app.add_url_rule('/publish_lecture', view_func=publish_lecture, methods=['GET', 'POST'])
    app.add_url_rule('/publish_lecture/png', view_func=publish_lecture_png, methods=['POST'])
    app.add_url_rule('/download/<kind>/<date>', view_func=download)
