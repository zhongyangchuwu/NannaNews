"""Integrated route regressions; no network or compiler calls."""
from contextlib import closing
from datetime import date
import json
from dataclasses import replace
import os
import subprocess
import sys

import pytest


@pytest.fixture
def editorial(tmp_path, monkeypatch):
    from nannanews import runtime
    monkeypatch.setenv('NANNA_DATA_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('NANNA_MODEL_BASE_URL', '')
    monkeypatch.setenv('NANNA_MODEL_API_KEY', '')
    monkeypatch.setattr(runtime, 'settings', runtime.load_settings())
    from nannanews import schema
    schema.migrate()
    from nannanews import cli as manage
    manage.create_user('owner', 'owner-regression-password', 'admin', initial_admin=True)
    manage.create_user('reviewer', 'reviewer-regression-password', 'editor')
    manage.create_user('member', 'member-regression-password', 'member')
    from nannanews.web import create_app
    app = create_app()
    app.config['TESTING'] = True

    def client(username='owner'):
        result = app.test_client()
        with result.session_transaction() as session:
            session['username'] = username
            session['csrf_token'] = 'regression-csrf'
        return result

    def entry(entry_id, *, status='described', reviewer=None, published=None, lecture=None):
        with closing(runtime.connect_db()) as db, db:
            db.execute('''INSERT INTO entries(id,uploader,upload_time,title,description,describer,
                reviewer,status,type,publish_date,tag,short_title,lecture_data,priority)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (entry_id, 'owner', str(date.today()), '公开活动', '公开正文', 'model:gpt-6-luna',
                 reviewer, status, '活动预告', published, '讲座' if lecture else '校级活动',
                 '公开活动', json.dumps(lecture, ensure_ascii=False) if lecture else None, 0))

    def row(entry_id):
        with closing(runtime.connect_db()) as db:
            return db.execute('SELECT * FROM entries WHERE id=?', (entry_id,)).fetchone()

    return app, runtime, client, entry, row


def test_mixed_publication_selection_is_rejected_atomically(editorial):
    _, _, client, add, row = editorial
    add(1, status='approved', reviewer='reviewer')
    add(2, status='pending')
    response = client().post('/admin/publish', data={'csrf_token': 'regression-csrf',
        'entry_ids': ['1', '2'], 'publish_time': date.today().isoformat()})
    assert response.status_code == 409
    assert row(1)[14] is None and row(2)[14] is None
    accepted = client().post('/admin/publish', data={'csrf_token': 'regression-csrf',
        'entry_ids': ['1'], 'publish_time': date.today().isoformat()})
    assert accepted.status_code == 302
    assert client().get('/api/entries/1').status_code == 200
    assert client().get('/api/entries/2').status_code == 404


def test_changed_lecture_requires_modification_and_independent_review(editorial):
    _, _, client, add, row = editorial
    original = {'title': {'primary': '公开活动'}, 'venue': "O'Brien 报告厅"}
    changed = {'title': {'primary': '公开活动'}, 'venue': '另一报告厅'}
    add(3, lecture=original)
    form = {'csrf_token': 'regression-csrf', 'title': '公开活动', 'short_title': '公开活动',
            'description': '公开正文', 'due_time': '', 'entry_type': '活动预告', 'tag': '讲座',
            'lecture_data': json.dumps(changed, ensure_ascii=False), 'priority': '0', 'action': 'approve'}
    response = client().post('/review/3', data=form)
    assert response.status_code == 409
    assert row(3)[9] == 'described' and json.loads(row(3)[17]) == original
    form['action'] = 'modify'
    assert client().post('/review/3', data=form).status_code == 302
    assert row(3)[9] == 'modified' and row(3)[14] is None
    form['action'] = 'approve'
    assert client().post('/review/3', data=form).status_code == 409
    assert client('reviewer').post('/review/3', data=form).status_code == 302
    assert row(3)[9] == 'approved' and row(3)[7] == 'reviewer'
    assert row(3)[14] is None


def test_content_edit_withdraws_published_entry_and_reviewer(editorial):
    _, _, client, add, row = editorial
    add(4, status='approved', reviewer='reviewer', published=date.today().isoformat())
    authenticated = client()
    assert authenticated.get('/api/entries/4').status_code == 200
    response = authenticated.post('/describe/4', data={'csrf_token': 'regression-csrf',
        'title': '修订标题', 'description': '修订正文', 'entry_type': '活动预告', 'tag': '校级活动'})
    assert response.status_code == 302
    assert row(4)[9] == 'described' and row(4)[7] is None and row(4)[14] is None
    assert authenticated.get('/api/entries/4').status_code == 404


def test_deleting_last_candidate_removes_evidence_without_foreign_key_failure(editorial):
    _, runtime, client, add, row = editorial
    add(5)
    with closing(runtime.connect_db()) as db, db:
        db.execute('''INSERT INTO acquisitions(id,source_url,digest,document_json,model_name,acquired_by,created_at)
            VALUES(1,'https://example.org/public','digest','{}','gpt-6-luna','owner','2026-10-01')''')
        db.execute("INSERT INTO acquisition_candidates VALUES(1,'event',5,'{}')")
    assert client().post('/admin/delete/5', data={'csrf_token': 'regression-csrf'}).status_code == 302
    assert row(5) is None
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT COUNT(*) FROM acquisition_candidates').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM acquisitions').fetchone()[0] == 0


def test_mutations_require_csrf_and_legacy_reads_are_removed(editorial):
    _, _, client, add, row = editorial
    add(6)
    authenticated = client()
    assert authenticated.post('/admin/delete/6').status_code == 400
    assert authenticated.post('/admin/delete/6', data={'csrf_token': '无效令牌'}).status_code == 400
    assert row(6) is not None
    assert authenticated.get('/logout').status_code == 405
    assert authenticated.get('/cancel/6').status_code == 405
    for path in ('/api/query?id=6', '/api/search?q=公开', '/typst/2026-10-01', '/preview/2026-10-01'):
        assert authenticated.get(path).status_code == 404


def test_importing_web_does_not_construct_app_or_initialize_state(tmp_path, monkeypatch):
    state = tmp_path / 'uninitialized'
    monkeypatch.setenv('NANNA_DATA_DIR', str(state))
    monkeypatch.setenv('NANNA_MODEL_BASE_URL', '')
    monkeypatch.setenv('NANNA_MODEL_API_KEY', '')
    result = subprocess.run(
        [sys.executable, '-c', 'import nannanews.web; assert not hasattr(nannanews.web, "app")'],
        env=os.environ.copy(), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not state.exists()


def test_factory_requires_explicit_migration_without_creating_state(tmp_path, monkeypatch):
    from nannanews import runtime
    from nannanews.web import create_app

    state = tmp_path / 'uninitialized'
    monkeypatch.setenv('NANNA_DATA_DIR', str(state))
    monkeypatch.setattr(runtime, 'settings', runtime.load_settings())
    with pytest.raises(runtime.ConfigurationError, match='migrate'):
        create_app()
    assert not state.exists()


def test_factory_registers_fresh_apps_with_existing_editorial_state(editorial):
    from nannanews.web import create_app

    first, _, _, add, row = editorial
    add(7)
    second = create_app()
    assert second is not first
    assert row(7)[3] == '公开活动'
    assert {(rule.rule, rule.endpoint) for rule in first.url_map.iter_rules()} == {
        (rule.rule, rule.endpoint) for rule in second.url_map.iter_rules()
    }


def test_controller_endpoints_keep_surviving_urls(editorial):
    from flask import url_for

    app, _, _, _, _ = editorial
    endpoints = {
        'login': '/login', 'register': '/register', 'logout': '/logout',
        'change_password': '/change_password', 'main': '/workbench', 'stats': '/stats',
        'upload': '/upload', 'paste': '/paste', 'batch_upload_links': '/batch_upload_links',
        'batch_upload_invalid_links': '/batch_upload_invalid_links', 'upload_image': '/upload_image',
        'admin': '/admin', 'admin_publish': '/admin/publish',
        'admin_publish_today': '/admin/publish_today', 'admin_unpublish': '/admin/unpublish',
        'admin_make_invalid': '/admin/make_invalid', 'user_admin': '/admin/user_admin',
        'search': '/search', 'preview_edit': '/preview_edit', 'add_deadline': '/add_deadline',
        'publish': '/publish', 'publish_png': '/publish/png',
        'publish_lecture': '/publish_lecture', 'publish_lecture_png': '/publish_lecture/png',
    }
    with app.test_request_context():
        for endpoint, expected in endpoints.items():
            assert url_for(endpoint) == expected
        for endpoint, expected in {
            'describe': '/describe/1', 'review': '/review/1', 'cancel': '/cancel/1',
            'admin_edit': '/admin/edit/1', 'admin_delete': '/admin/delete/1', 'delete_entry': '/delete/1',
        }.items():
            assert url_for(endpoint, entry_id=1) == expected
        assert url_for('user_edit', user_id=1) == '/admin/user_edit/1'
        assert url_for('latex_entries', date='2026-10-01') == '/latex/2026-10-01'
        assert url_for('download', kind='news_pdf', date='2026-10-01') == '/download/news_pdf/2026-10-01'


def test_packaged_templates_assets_and_current_settings(editorial, monkeypatch):
    _, runtime, client, _, _ = editorial
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, site_name='独立测试小报'))
    authenticated = client()
    login = authenticated.get('/login')
    assert login.status_code == 200
    assert '独立测试小报'.encode() in login.data
    admin = authenticated.get('/admin')
    assert admin.status_code == 200
    assert b'/send_email' not in admin.data
    assert authenticated.get('/static/app.css').status_code == 200


def test_retired_email_and_sync_routes_are_absent(editorial):
    app, _, client, _, _ = editorial
    assert 'send_email' not in app.view_functions
    assert not any('sync' in rule.rule for rule in app.url_map.iter_rules())
    authenticated = client()
    assert authenticated.get('/send_email').status_code == 404
    assert authenticated.post('/send_email', data={'csrf_token': 'regression-csrf'}).status_code == 404


def test_member_cannot_review_or_publish(editorial):
    _, _, client, add, row = editorial
    add(8)
    member = client('member')
    assert member.get('/review/8').status_code == 401
    assert member.post('/admin/publish', data={'csrf_token': 'regression-csrf',
        'entry_ids': ['8'], 'publish_time': date.today().isoformat()}).status_code == 401
    assert row(8)[9] == 'described' and row(8)[14] is None
