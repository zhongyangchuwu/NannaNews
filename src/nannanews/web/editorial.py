"""Draft editing, locks and independent review of private entries."""

from datetime import datetime, timedelta
import json

from flask import abort, flash, redirect, render_template, request, session, url_for

from nannanews import acquisition
from nannanews.runtime import connect_db
from .inputs import valid_date
from .security import editor_required, login_required

def entry_context(entry):
    lecture = None
    if entry and len(entry) > 17 and entry[17]:
        try:
            decoded = json.loads(entry[17])
            if isinstance(decoded, dict):
                lecture = decoded
        except (TypeError, ValueError):
            pass
    return {'lecture_data_obj': lecture, 'entry_evidence': acquisition.get_entry_evidence(entry[0]) if entry else None}

@login_required
def main():
    conn = connect_db()
    c = conn.cursor()

    cutoff = datetime.now() - timedelta(minutes=15)
    c.execute("UPDATE entries SET locked_by=NULL, lock_time=NULL WHERE lock_time IS NOT NULL AND lock_time < ?", (cutoff,))
    conn.commit()

    status_filter = request.args.get('status', 'all').strip()
    order = request.args.get('order', 'desc').strip()
    if order not in ('asc', 'desc'):
        order = 'desc'
    order_sql = 'ASC' if order == 'asc' else 'DESC'
    allowed_statuses = ('pending', 'described', 'modified', 'approved')
    if status_filter in allowed_statuses:
        c.execute(f"SELECT * FROM entries WHERE publish_date IS NULL AND status=? ORDER BY upload_time {order_sql}", (status_filter,))
        entries = c.fetchall()
        total_count = len(entries)
    elif status_filter == 'none':
        status_filter = 'none'
        c.execute(f"SELECT * FROM entries WHERE publish_date IS NULL ORDER BY upload_time {order_sql}")
        entries = c.fetchall()
        total_count = len(entries)
    else:
        status_filter = 'all'
        c.execute(f"SELECT * FROM entries WHERE publish_date IS NULL ORDER BY upload_time {order_sql}")
        entries = c.fetchall()
        total_count = len(entries)
    evidence_ids = {row[0] for row in c.execute('SELECT entry_id FROM acquisition_candidates')}
    conn.close()
    return render_template('main.html', entries=entries, status_filter=status_filter, total_count=total_count, order=order, evidence_ids=evidence_ids)

@login_required
def stats():
    conn = connect_db()
    c = conn.cursor()
    five_days_ago = datetime.now() - timedelta(days=5)
    c.execute("SELECT uploader, COUNT(*) AS count FROM entries WHERE upload_time >= ? GROUP BY uploader ORDER BY count DESC LIMIT 20", (five_days_ago,))
    uploader_stats = c.fetchall()
    c.execute("SELECT describer, COUNT(*) AS count FROM entries WHERE upload_time >= ? AND describer IS NOT NULL GROUP BY describer ORDER BY count DESC LIMIT 20", (five_days_ago,))
    describer_stats = c.fetchall()
    c.execute("SELECT reviewer, COUNT(*) AS count FROM entries WHERE upload_time >= ? AND reviewer IS NOT NULL GROUP BY reviewer ORDER BY count DESC LIMIT 20", (five_days_ago,))
    reviewer_stats = c.fetchall()
    conn.close()
    return render_template('stats.html', uploader_stats=uploader_stats, describer_stats=describer_stats, reviewer_stats=reviewer_stats)

@editor_required
def describe(entry_id):
    conn = connect_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        entry = _editable_entry(conn, entry_id)
        if request.method == 'POST':
            values = _editor_values(entry)
            conn.execute('''UPDATE entries SET title=?, short_title=?, due_time=?, description=?, type=?,
                use_image=?, tag=?, lecture_data=?, priority=?, describer=?, reviewer=NULL,
                status='described', publish_date=NULL, locked_by=NULL, lock_time=NULL WHERE id=?''',
                (*values, session['username'], entry_id))
            conn.commit()
            return redirect(url_for('main'))
        conn.execute('UPDATE entries SET locked_by=?, lock_time=? WHERE id=?', (session['username'], datetime.now(), entry_id))
        conn.commit()
    finally:
        conn.close()
    return render_template('describe.html', entry=entry, **entry_context(entry))

@editor_required
def review(entry_id):
    conn = connect_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        entry = _editable_entry(conn, entry_id)
        if request.method == 'POST':
            action = request.form.get('action')
            if action not in ('approve', 'modify'):
                abort(400)
            values = _editor_values(entry)
            original = (entry[3], entry[16] or entry[3], entry[8] or None, entry[5] or '', entry[10],
                        int(entry[13] or 0), entry[15] or None, _lecture_json(entry[17]), int(entry[18] or 0))
            modified = list(entry)
            for index, value in zip((3, 16, 8, 5, 10, 13, 15, 17, 18), values):
                modified[index] = value
            if action == 'approve':
                if entry[9] not in ('described', 'modified', 'approved') or not entry[6]:
                    abort(409, description='请先完成撰写，再审阅。')
                if session['username'] in (entry[6], entry[7]):
                    flash('不可通过自己撰写或修改的内容。')
                    return render_template('review.html', entry=modified, **entry_context(modified)), 409
                if values != original:
                    flash('内容已修改（包括讲座结构）；请先提交修改，由另一位编辑审阅。')
                    return render_template('review.html', entry=modified, **entry_context(modified)), 409
                conn.execute("UPDATE entries SET reviewer=?, status='approved', locked_by=NULL, lock_time=NULL WHERE id=?", (session['username'], entry_id))
            else:
                conn.execute('''UPDATE entries SET title=?, short_title=?, due_time=?, description=?, type=?,
                    use_image=?, tag=?, lecture_data=?, priority=?, reviewer=?, status='modified',
                    publish_date=NULL, locked_by=NULL, lock_time=NULL WHERE id=?''', (*values, session['username'], entry_id))
            conn.commit()
            return redirect(url_for('main'))
        conn.execute('UPDATE entries SET locked_by=?, lock_time=? WHERE id=?', (session['username'], datetime.now(), entry_id))
        conn.commit()
    finally:
        conn.close()
    return render_template('review.html', entry=entry, **entry_context(entry))

def _editable_entry(conn, entry_id):
    entry = conn.execute('SELECT * FROM entries WHERE id=?', (entry_id,)).fetchone()
    if not entry:
        abort(404)
    if entry[11] and entry[11] != session['username'] and entry[12]:
        if datetime.now() - datetime.fromisoformat(entry[12]) < timedelta(minutes=15):
            abort(409, description='该条目正被其他编辑锁定。')
    return entry

def _lecture_json(raw):
    if not raw or not str(raw).strip():
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        abort(400, description='讲座数据须为合法 JSON 对象。')
    if not isinstance(value, dict):
        abort(400, description='讲座数据须为 JSON 对象。')
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

def _editor_values(entry):
    title = request.form.get('title', entry[3]).strip()
    if not title:
        abort(400, description='标题不能为空。')
    try:
        priority = int(request.form.get('priority', entry[18] or 0))
    except (TypeError, ValueError):
        abort(400, description='优先级须为整数。')
    due_time = request.form.get('due_time', entry[8]) or None
    if due_time:
        valid_date(due_time)
    return (title, request.form.get('short_title') or title, due_time,
            request.form.get('description', entry[5]) or '', request.form.get('entry_type', entry[10]),
            int(request.form.get('use_image') == 'on'), request.form.get('tag', entry[15]) or None,
            _lecture_json(request.form.get('lecture_data', entry[17])), priority)

@login_required
def cancel(entry_id):
    conn = connect_db()
    c = conn.cursor()
    c.execute("SELECT locked_by FROM entries WHERE id=?", (entry_id,))
    entry_lock_info = c.fetchone()
    if entry_lock_info and entry_lock_info[0] == session['username']:
        c.execute("UPDATE entries SET locked_by=NULL, lock_time=NULL WHERE id=?", (entry_id,))
        conn.commit()
    conn.close()
    return redirect(url_for('main'))

def delete_entry_data(conn, entry_id):
    evidence = conn.execute('SELECT acquisition_id FROM acquisition_candidates WHERE entry_id=?', (entry_id,)).fetchone()
    conn.execute('DELETE FROM acquisition_candidates WHERE entry_id=?', (entry_id,))
    conn.execute('DELETE FROM entries WHERE id=?', (entry_id,))
    if evidence:
        conn.execute('''DELETE FROM acquisitions WHERE id=? AND NOT EXISTS
            (SELECT 1 FROM acquisition_candidates WHERE acquisition_id=?)''', (evidence[0], evidence[0]))

@login_required
def delete_entry(entry_id):
    conn = connect_db()
    c = conn.cursor()
    c.execute("SELECT uploader FROM entries WHERE id=?", (entry_id,))
    entry = c.fetchone()
    if not entry:
        conn.close()
        flash("条目不存在")
        return redirect(url_for('main'))
    if entry[0] != session['username']:
        conn.close()
        flash("你没有权限删除此条目，仅可删除自己上传的条目")
        return redirect(url_for('main'))
    delete_entry_data(conn, entry_id)
    conn.commit()
    conn.close()
    flash("条目已删除")
    return redirect(url_for('main'))

@login_required
def search():
    page = request.args.get('page', default=1, type=int)
    page_size = 5
    offset = (page - 1) * page_size
    query = request.args.get('q', '').strip()
    results = []
    total_pages = 0

    if query:
        conn = connect_db()
        c = conn.cursor()
        like_query = f'%{query}%'
        c.execute("SELECT COUNT(*) FROM entries WHERE title LIKE ? OR description LIKE ?", (like_query, like_query))
        total_count = c.fetchone()[0]
        total_pages = (total_count + page_size - 1) // page_size

        c.execute("""SELECT * FROM entries
                     WHERE title LIKE ? OR description LIKE ?
                     ORDER BY upload_time DESC
                     LIMIT ? OFFSET ?""",
                  (like_query, like_query, page_size, offset))
        results = c.fetchall()
        conn.close()

    return render_template('search.html', query=query, results=results, page=page, total_pages=total_pages)

@editor_required
def add_deadline():
    if request.method == 'POST':
        link = request.form.get('link', '').strip()
        link_value = link if link else None
        short_title = request.form.get('short_title', '').strip()
        tag = request.form.get('tag', '').strip()
        today = datetime.now().strftime("%Y-%m-%d")
        publish_time = request.form.get('publish_time', today)
        due_time = request.form.get('due_time', today)

        conn = connect_db()
        c = conn.cursor()
        c.execute("""INSERT INTO entries
                     (uploader, describer, upload_time, title, link, short_title, description, due_time, publish_date, status, tag, type)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (session['username'], session['username'], datetime.now(), short_title, link_value, short_title, '', due_time, None, 'described', tag, 'DDLOnly'))
        conn.commit()
        conn.close()
        flash('截止日期草稿已添加，须经独立审阅后发布。')
        return redirect(url_for('main'))
    today = datetime.now().strftime("%Y-%m-%d")
    return render_template('add_deadline.html', today=today)


def register(app):
    """Register controller routes without changing public endpoint names."""
    app.add_url_rule('/workbench', view_func=main)
    app.add_url_rule('/stats', view_func=stats)
    app.add_url_rule('/describe/<int:entry_id>', view_func=describe, methods=['GET', 'POST'])
    app.add_url_rule('/review/<int:entry_id>', view_func=review, methods=['GET', 'POST'])
    app.add_url_rule('/cancel/<int:entry_id>', view_func=cancel, methods=['POST'])
    app.add_url_rule('/delete/<int:entry_id>', view_func=delete_entry, methods=['POST'])
    app.add_url_rule('/search', view_func=search, methods=['GET'])
    app.add_url_rule('/add_deadline', view_func=add_deadline, methods=['GET', 'POST'])
