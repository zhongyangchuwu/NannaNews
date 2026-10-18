"""Manual source, image and draft intake; no publication side effects."""

from datetime import datetime
import hashlib

from flask import flash, redirect, render_template, request, session, url_for

from nannanews import acquisition, runtime
from nannanews.runtime import connect_db
from .inputs import allowed_file, is_valid_url
from .security import editor_required, login_required

def fetch_title(url):
    try:
        document = acquisition.fetch_document(url)
        return document.get('title') or '无标题公开来源'
    except acquisition.AcquisitionError:
        return '待核对公开来源（正文暂不可读取）'

def hash_file(file_obj):
    file_obj.seek(0)
    file_content = file_obj.read()
    file_hash = hashlib.md5(file_content).hexdigest()
    file_obj.seek(0)
    return file_hash

@login_required
def upload():
    if request.method == 'POST':
        title = request.form['title']
        description = request.form['description']
        due_time = request.form['due_time']
        entry_type = request.form['entry_type']
        tag = request.form.get('tag')
        short_title = request.form.get('short_title') or title
        try:
            priority = int(request.form.get('priority', 0) or 0)
        except (TypeError, ValueError):
            priority = 0

        conn = connect_db()
        c = conn.cursor()
        c.execute("""INSERT INTO entries
                    (uploader, describer, upload_time, title, short_title, description, due_time, status, type, tag, priority)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (session['username'], session['username'], datetime.now(), title, short_title, description, due_time, 'described', entry_type, tag, priority))
        conn.commit()
        conn.close()
        return redirect(url_for('main'))

    return render_template('upload.html')

@login_required
def paste():
    link = request.form['link'].strip()
    if not link or not is_valid_url(link):
        flash('请输入有效的地址')
        return redirect(url_for('main'))
    canonical_url = acquisition.canonical_url(link)
    conn = connect_db()
    c = conn.cursor()
    c.execute("SELECT id FROM entries WHERE link=?", (canonical_url,))
    if c.fetchone():
        conn.close()
        flash("该链接已经上传")
        return redirect(url_for('main'))
    title = fetch_title(link)
    c.execute("""INSERT INTO entries
                (uploader, upload_time, title, link, due_time, status, type)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
              (session['username'], datetime.now(), title, canonical_url, None, 'pending', '活动预告'))
    conn.commit()
    conn.close()
    flash('地址添加成功')
    return redirect(url_for('main'))

@login_required
def batch_upload_links():
    if request.method == 'POST':
        links_text = request.form.get('links', '')
        links = [line.strip() for line in links_text.split('\n') if line.strip()]
        conn = connect_db()
        c = conn.cursor()
        success_count = 0
        skip_count = 0
        for link in links:
            if not is_valid_url(link):
                continue
            canonical_url = acquisition.canonical_url(link)
            c.execute("SELECT id FROM entries WHERE link=?", (canonical_url,))
            if c.fetchone():
                skip_count += 1
                continue
            title = fetch_title(link)
            c.execute("""INSERT INTO entries
                        (uploader, upload_time, title, link, due_time, status, type)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                      (session['username'], datetime.now(), title, canonical_url, None, 'pending', '活动预告'))
            success_count += 1
        conn.commit()
        conn.close()
        msg = f'批量处理完成：成功添加了 {success_count} 个地址'
        if skip_count > 0:
            msg += f'，跳过了 {skip_count} 个已存在的地址'
        flash(msg)
        return redirect(url_for('main'))
    return render_template('batch_upload_links.html')

@editor_required
def batch_upload_invalid_links():
    if request.method == 'POST':
        links_text = request.form.get('links', '')
        links = [line.strip() for line in links_text.split('\n') if line.strip()]
        conn = connect_db()
        c = conn.cursor()
        success_count = 0
        skip_count = 0
        invalid_date = datetime(1970, 1, 1)
        for link in links:
            if not is_valid_url(link):
                continue
            canonical_url = acquisition.canonical_url(link)

            c.execute("SELECT id FROM entries WHERE link=?", (canonical_url,))
            if c.fetchone():
                skip_count += 1
                continue
            title = fetch_title(link)
            c.execute("""INSERT INTO entries
                        (uploader, upload_time, title, link, due_time, status, type, describer)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                      (session['username'], datetime.now(), "【无效】"+title, canonical_url, None, 'described', '活动预告', session['username']))
            entry_id = c.lastrowid
            success_count += 1
            c.execute("UPDATE entries SET publish_date=? WHERE id=?", (invalid_date, entry_id))

        conn.commit()
        conn.close()
        msg = f'批量无效化完成：新上传了 {success_count} 个地址并标为无效'
        if skip_count > 0:
            msg += f'，跳过了 {skip_count} 个已存在的地址'
        flash(msg)
        return redirect(url_for('main'))
    return render_template('batch_upload_invalid_links.html')

@login_required
def upload_image():
    if 'image' not in request.files:
        flash("没有文件上传")
        return redirect(url_for('main'))
    file = request.files['image']
    if file.filename == '':
        flash("未选择文件")
        return redirect(url_for('main'))
    if file and allowed_file(file.filename):
        extension = file.filename.rsplit('.', 1)[1].lower()
        file_hash = hash_file(file)
        filename = f"{file_hash}.{extension}"
        conn = connect_db()
        c = conn.cursor()
        c.execute("SELECT id FROM entries WHERE link=?", (filename,))
        if c.fetchone():
            conn.close()
            flash("该图片已经上传")
            return redirect(url_for('main'))
        conn.close()
        file_path = runtime.settings.upload_dir / filename
        file.save(file_path)
        file_path.chmod(0o600)
        conn = connect_db()
        c = conn.cursor()
        c.execute("""INSERT INTO entries
                     (uploader, upload_time, title, link, due_time, status, type)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
                  (session['username'], datetime.now(), file.filename, filename, None, 'pending', '活动预告'))
        conn.commit()
        conn.close()
        return redirect(url_for('main'))
    else:
        flash("不支持的文件格式")
        return redirect(url_for('main'))


def register(app):
    """Register controller routes without changing public endpoint names."""
    app.add_url_rule('/upload', view_func=upload, methods=['GET', 'POST'])
    app.add_url_rule('/paste', view_func=paste, methods=['POST'])
    app.add_url_rule('/batch_upload_links', view_func=batch_upload_links, methods=['GET', 'POST'])
    app.add_url_rule('/batch_upload_invalid_links', view_func=batch_upload_invalid_links, methods=['GET', 'POST'])
    app.add_url_rule('/upload_image', view_func=upload_image, methods=['POST'])
