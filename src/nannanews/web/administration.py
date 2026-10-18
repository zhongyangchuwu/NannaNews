"""Entry administration and atomic approval-to-publication transitions."""

from datetime import datetime

from flask import abort, flash, redirect, render_template, request, session, url_for

from nannanews.runtime import connect_db
from .bulletin import get_beijing_date_str
from .editorial import delete_entry_data
from .inputs import valid_date
from .security import admin_required, editor_required

@editor_required
def admin():
    page = request.args.get('page', default=1, type=int)
    page_size = 25
    offset = (page - 1) * page_size
    conn = connect_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM entries")
    total_count = c.fetchone()[0]
    total_pages = (total_count + page_size - 1) // page_size
    c.execute("SELECT * FROM entries ORDER BY upload_time DESC LIMIT ? OFFSET ?", (page_size, offset))
    entries = c.fetchall()
    conn.close()
    default_publish_date = get_beijing_date_str()
    return render_template('admin.html', entries=entries, page=page, total_pages=total_pages, default_publish_date=default_publish_date)

@admin_required
def admin_edit(entry_id):
    conn = connect_db()
    c = conn.cursor()
    if request.method == 'POST':
        title = request.form['title']
        link = request.form['link']
        description = request.form['description']
        due_time = request.form['due_time']
        entry_type = request.form['entry_type']
        c.execute('''UPDATE entries SET title=?, link=?, description=?, describer=?, reviewer=NULL,
            due_time=?, status='described', type=?, publish_date=NULL, locked_by=NULL, lock_time=NULL WHERE id=?''',
            (title, link, description, session['username'], due_time or None, entry_type, entry_id))
        conn.commit()
        conn.close()
        flash('条目已更新并撤回公开状态；须由另一位编辑重新审阅。')
        return redirect(url_for('admin'))
    else:
        c.execute("SELECT * FROM entries WHERE id=?", (entry_id,))
        entry = c.fetchone()
        conn.close()
        return render_template('admin_edit.html', entry=entry)

@admin_required
def admin_delete(entry_id):
    conn = connect_db()
    c = conn.cursor()
    delete_entry_data(conn, entry_id)
    conn.commit()
    conn.close()
    flash("条目已删除")
    return redirect(url_for('admin'))

@editor_required
def admin_publish():
    entry_ids = request.form.getlist('entry_ids')
    if not entry_ids:
        abort(400, description='请至少选择一个条目。')
    date_str = valid_date(request.form.get('publish_time'))
    conn = connect_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        selected = []
        for eid in entry_ids:
            row = conn.execute('SELECT id, status, reviewer, lecture_data FROM entries WHERE id=?', (eid,)).fetchone()
            if not row or row[1] != 'approved' or not row[2]:
                abort(409, description='只能发布已通过人工审阅的条目；本次选择未作任何更新。')
            selected.append(row)
        conn.executemany('UPDATE entries SET publish_date=? WHERE id=?', ((date_str, row[0]) for row in selected))
        conn.commit()
    finally:
        conn.close()
    flash(f'已发布 {len(selected)} 条已审阅消息，日期 {date_str}。')
    return redirect(url_for('admin'))

@editor_required
def admin_publish_today():
    publish_time = datetime.now().date()
    today = datetime.now().date()
    conn = connect_db()
    c = conn.cursor()
    c.execute("UPDATE entries SET publish_date=? WHERE date(upload_time)=? AND status='approved' AND reviewer IS NOT NULL AND publish_date IS NULL", (publish_time, today))
    conn.commit()
    conn.close()
    flash('今天上传且已通过人工审阅的未发布条目已发布。')
    return redirect(url_for('admin'))

@editor_required
def admin_unpublish():
    entry_ids = request.form.getlist("entry_ids")
    if not entry_ids:
        flash("请至少选择一个条目")
        return redirect(url_for('admin'))
    conn = connect_db()
    c = conn.cursor()
    for eid in entry_ids:
        c.execute("UPDATE entries SET publish_date=NULL WHERE id=?", (eid,))
    conn.commit()
    conn.close()
    flash("选定条目已取消发布")
    return redirect(url_for('admin'))

@editor_required
def admin_make_invalid():
    entry_ids = request.form.getlist("entry_ids")
    if not entry_ids:
        flash("请至少选择一个条目")
        return redirect(url_for('admin'))
    invalid_date = datetime(1970, 1, 1)
    conn = connect_db()
    c = conn.cursor()
    for eid in entry_ids:
        c.execute("UPDATE entries SET publish_date=? WHERE id=?", (invalid_date, eid))
    conn.commit()
    conn.close()
    flash("选定条目已标记为无效")
    return redirect(url_for('admin'))


def register(app):
    """Register controller routes without changing public endpoint names."""
    app.add_url_rule('/admin', view_func=admin)
    app.add_url_rule('/admin/edit/<int:entry_id>', view_func=admin_edit, methods=['GET', 'POST'])
    app.add_url_rule('/admin/delete/<int:entry_id>', view_func=admin_delete, methods=['POST'])
    app.add_url_rule('/admin/publish', view_func=admin_publish, methods=['POST'])
    app.add_url_rule('/admin/publish_today', view_func=admin_publish_today, methods=['POST'])
    app.add_url_rule('/admin/unpublish', view_func=admin_unpublish, methods=['POST'])
    app.add_url_rule('/admin/make_invalid', view_func=admin_make_invalid, methods=['POST'])
