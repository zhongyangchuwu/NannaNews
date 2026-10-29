"""Private web boundary against a real tiny public pack and migrated audit DB."""

from contextlib import closing
from dataclasses import replace
import json

import pytest
from werkzeug.security import generate_password_hash

from nannanews import runtime
from nannanews.reader_reviews import ReviewConflict
from nannanews.web import create_app
from test_reader_reviews import BASE, ITEM, NEW, OTHER, payload, write_pack
from test_reader_reviews import store as review_store  # noqa: F401 -- registers the native pytest fixture


URL = f'/console/activities/{ITEM}'


@pytest.fixture(scope='module')
def password_hash():
    return generate_password_hash('test-only-console-password')


@pytest.fixture
def console(review_store, monkeypatch, password_hash):
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings,
        reader_url='https://reader.example.org/nested/'))
    with closing(runtime.connect_db()) as db, db:
        db.executemany('INSERT INTO users(username,password,role) VALUES(?,?,?)',
                       [(role + '-account', password_hash, role) for role in ('admin', 'editor', 'member')])
    app = create_app()
    app.testing = True
    return app, review_store


def _client(console, role='editor'):
    client = console[0].test_client()
    with client.session_transaction() as session:
        session['csrf_token'] = 'login-csrf'
    response = client.post('/login', data={'username': role + '-account',
        'password': 'test-only-console-password', 'csrf_token': 'login-csrf'})
    assert response.status_code == 302
    with client.session_transaction() as session:
        session['csrf_token'] = 'test-csrf'
    return client


def _form(**overrides):
    data = {
        'csrf_token': 'test-csrf', 'expected_head': '', 'base_version': BASE,
        'title': 'Human title', 'summary': 'Human summary', 'content': 'Human complete body',
        'category': '', 'time': '', 'venue': 'Confirmed venue', 'organizer': '',
        'audience': '', 'participation': '', 'source_urls': 'https://events.example.org/new',
        'date_kind': ['event', 'deadline'], 'date_value': ['2026-11-01', '2026-10-31'],
        'status': 'verified', 'reason': 'Public explanation', 'action': 'save',
    }
    data.update(overrides)
    return data


def _edit_original(console, edit):
    root = console[1].settings.reader_data_dir
    path = root / 'versions' / BASE / 'items' / (ITEM[5:7] + '.json')
    shard = json.loads(path.read_bytes())
    edit(shard[ITEM]['occurrences'][0])
    path.write_text(json.dumps(shard), encoding='utf-8')


def test_console_requires_real_login_editor_role_and_global_csrf(console):
    app, store = console
    assert app.test_client().get('/console/activities').status_code == 302
    assert _client(console, 'member').get('/console/activities').status_code == 401
    client = _client(console)
    assert client.post(URL, data=_form(csrf_token='wrong')).status_code == 400
    assert client.post('/console/publish', data={'csrf_token': 'test-csrf', 'confirm_publish': 'yes'}).status_code == 401
    assert store.get_item(ITEM)['history'] == []


def test_queue_filters_real_problem_items_and_saved_unpublished_state(console):
    client = _client(console)
    html = client.get('/console/activities').get_data(as_text=True)
    assert 'Original activity a' in html and 'Original activity b' not in html
    assert '日期待核对' in html and '发布已保存修改' not in html
    assert '当前筛选没有活动' in client.get('/console/activities?q=not-present').get_data(as_text=True)
    assert client.post(URL, data=_form()).status_code == 302
    response = client.get('/console/activities?q=Human&status=unpublished')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'Human title' in html and '已保存 · 未发布' in html
    assert 'value="Human"' in html and 'value="unpublished" selected' in html


def test_item_normal_fields_original_evidence_escaping_and_nested_hash_links(console):
    def poison(row):
        row['title'] = 'Original <script>notice</script>'
        row['body_text'] = 'Source <img src=x onerror=alert(1)> evidence'
        row['source_urls'] += ['javascript:alert(1)', 'http://127.0.0.1/private']
        row['date_review'] = {'status': 'ambiguous', 'reason': 'Original AI reason',
            'source_review': {'status': 'source_conflict', 'reason': 'Source disagrees', 'sources': []}}
    _edit_original(console, poison)
    response = _client(console).get(URL)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'name="title"' in html and 'name="date_kind"' in html and 'name="date_value"' in html
    assert 'name="reason"' in html and '读者可见' in html
    assert '全部期次' in html and 'Original AI reason' in html and 'Source disagrees' in html
    assert '&lt;script&gt;notice&lt;/script&gt;' in html
    assert '<script>notice</script>' not in html and '<img src=x onerror=' not in html
    assert 'href="javascript:' not in html and 'href="http://127.0.0.1/private' not in html
    assert f'https://reader.example.org/nested/#/items/{ITEM}' in html
    assert '?occ=occ-' in html


def test_save_appends_private_revision_without_changing_public_files(console):
    client = _client(console)
    root = console[1].settings.reader_data_dir
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    response = client.post(URL, data=_form())
    assert response.status_code == 302
    item = console[1].get_item(ITEM)
    assert item['unpublished'] and len(item['history']) == 1
    assert item['head']['actor'] == 'editor-account'
    assert item['head']['payload']['activity']['dates'] == [
        {'kind': 'event', 'date': '2026-11-01'}, {'kind': 'deadline', 'date': '2026-10-31'}]
    assert item['head']['payload']['activity']['category'] is None
    assert {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()} == before
    html = client.get(response.headers['Location']).get_data(as_text=True)
    assert '尚未发布' in html and '公开阅读页没有因此更新' in html


def test_actual_validation_error_preserves_invalid_dates_and_submitted_text(console):
    response = _client(console).post(URL, data=_form(
        title='Submitted <script>private input</script>', date_value=['2026-02-30', 'bad-date'],
        reason='Keep this explanation'))
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert 'Submitted &lt;script&gt;private input&lt;/script&gt;' in html
    assert 'value="2026-02-30"' in html and 'value="bad-date"' in html
    assert 'Keep this explanation' in html
    assert f'name="base_version" value="{BASE}"' in html
    assert console[1].get_item(ITEM)['history'] == []


def test_actual_concurrent_conflict_preserves_tokens_and_latest_diff(console):
    client = _client(console)
    first = console[1].save(ITEM, payload('Another editors title'), actor='other-private-editor',
                           expected_head=None, base_version=BASE)
    response = client.post(URL, data=_form())
    assert response.status_code == 409
    html = response.get_data(as_text=True)
    assert 'Another editors title' in html and 'Human title' in html
    assert 'name="expected_head" value=""' in html
    assert '以最新版本为基准继续核对' in html
    assert console[1].get_item(ITEM)['head_revision'] == first['revision']
    assert len(console[1].get_item(ITEM)['history']) == 1


def test_stale_source_requires_explicit_recheck_and_separate_save(console):
    store = console[1]
    first = store.save(ITEM, payload(), actor='editor-account', expected_head=None, base_version=BASE)
    write_pack(store.settings.reader_data_dir, NEW, changed_source=True)
    client = _client(console)
    html = client.get(URL).get_data(as_text=True)
    assert f'name="base_version" value="{BASE}"' in html and 'Changed source body' in html
    assert client.post(URL, data=_form(expected_head=first['revision'])).status_code == 409
    form = _form(action='rebase', expected_head=first['revision'], rebase_head=first['revision'],
                 rebase_base=NEW, acknowledge_latest='yes')
    response = client.post(URL, data=form)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f'name="base_version" value="{NEW}"' in html and '你的输入仍未保存' in html
    assert len(store.get_item(ITEM)['history']) == 1
    form.update(action='save', base_version=NEW)
    assert client.post(URL, data=form).status_code == 302
    assert len(store.get_item(ITEM)['history']) == 2 and not store.get_item(ITEM)['stale']


def test_rebase_does_not_silently_accept_another_intervening_save(console):
    store = console[1]
    first = store.save(ITEM, payload('First'), actor='editor-account', expected_head=None, base_version=BASE)
    second = store.save(ITEM, payload('Second'), actor='another-editor', expected_head=first['revision'], base_version=BASE)
    response = _client(console).post(URL, data=_form(action='rebase', expected_head=first['revision'],
        rebase_head=first['revision'], rebase_base=BASE, acknowledge_latest='yes'))
    assert response.status_code == 409
    assert '又出现了新版本' in response.get_data(as_text=True)
    assert store.get_item(ITEM)['head_revision'] == second['revision']


def test_admin_publish_requires_confirmation_and_reports_real_missing_config(console):
    client = _client(console, 'admin')
    assert client.post('/console/publish', data={'csrf_token': 'test-csrf'}).status_code == 400
    assert client.post(URL, data=_form()).status_code == 302
    response = client.post('/console/publish', data={'csrf_token': 'test-csrf', 'confirm_publish': 'yes'})
    assert response.status_code == 503
    html = response.get_data(as_text=True)
    assert '发布尚未完整完成' in html and 'NANNA_READER_SITE_DATA_DIR' in html
    assert console[1].get_item(ITEM)['unpublished']


def test_missing_pack_config_is_actionable_not_fake_empty(console, monkeypatch):
    client = _client(console)
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, reader_data_dir=None))
    response = client.get('/console/activities')
    assert response.status_code == 503
    html = response.get_data(as_text=True)
    assert 'NANNA_READER_DATA_DIR' in html and '显式数据库迁移' in html
    assert '当前筛选没有活动' not in html


def test_only_referenced_public_images_can_be_read(console):
    root = console[1].settings.reader_data_dir
    path = root / 'versions' / BASE / 'assets' / 'public.png'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'public poster')
    _edit_original(console, lambda row: row.update(images=[{
        'id': 'public-image', 'path': path.relative_to(root).as_posix(), 'mime': 'image/png'}]))
    client = _client(console)
    response = client.get(URL + '/images/public-image')
    assert response.status_code == 200 and response.data == b'public poster'
    assert response.headers['Cache-Control'].startswith('private')
    assert client.get(URL + '/images/not-admitted').status_code == 404
    _edit_original(console, lambda row: row['images'][0].update(path='../secret'))
    assert client.get(URL + '/images/public-image').status_code == 404


def test_incomplete_date_row_is_not_silently_dropped(console):
    response = _client(console).post(URL, data=_form(date_kind=['event'], date_value=['']))
    assert response.status_code == 400
    assert 'name="date_value"' in response.get_data(as_text=True)
    assert console[1].get_item(ITEM)['history'] == []


@pytest.mark.parametrize('unsafe', [
    'http://%31%32%37.0.0.1/private', 'https://%65xample.org/path',
    'http://2130706433/private', 'http://0177.0.0.1/private',
    'http://0x7f000001/private', 'http://127.1/private',
    'http://[::ffff:127.0.0.1]/private',
])
def test_evidence_and_submitted_links_share_public_url_validation(console, unsafe):
    _edit_original(console, lambda row: row.update(source_urls=[unsafe, 'https://events.example.org/safe']))
    client = _client(console)
    html = client.get(URL).get_data(as_text=True)
    assert unsafe in html and f'href="{unsafe}"' not in html
    assert '非安全公开 HTTP(S) 链接，仅显示文字' in html
    assert 'href="https://events.example.org/safe"' in html
    response = client.post(URL, data=_form(source_urls=unsafe))
    assert response.status_code == 400
    assert unsafe in response.get_data(as_text=True)
    assert console[1].get_item(ITEM)['history'] == []


def test_primary_images_without_archive_images_are_visible_and_authenticated(console):
    root = console[1].settings.reader_data_dir
    path = root / 'versions' / BASE / 'assets' / 'source.png'
    path.parent.mkdir(parents=True)
    png = bytes.fromhex(
        '89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c02'
        '0000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082')
    path.write_bytes(png)
    _edit_original(console, lambda row: row.update(images=[], primary_images=[{
        'id': 'source-image', 'path': path.relative_to(root).as_posix(),
        'mime': 'image/png', 'caption': 'Source-only poster'}]))
    image_url = URL + '/images/source-image'
    assert console[0].test_client().get(image_url).status_code == 302
    assert _client(console, 'member').get(image_url).status_code == 401
    client = _client(console)
    html = client.get(URL).get_data(as_text=True)
    assert '来源核查图片（只读参考）' in html and 'Source-only poster' in html
    assert f'src="{image_url}"' in html
    response = client.get(image_url)
    assert response.status_code == 200 and response.data == png
    assert response.mimetype == 'image/png'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['Cache-Control'].startswith('private')
    assert client.get(URL + '/images/not-admitted').status_code == 404


@pytest.mark.parametrize('collection', ['images', 'primary_images'])
@pytest.mark.parametrize('unsafe', ['traversal', 'absolute', 'symlink', 'mime'])
def test_both_image_collections_keep_path_mime_and_symlink_boundaries(console, collection, unsafe):
    root = console[1].settings.reader_data_dir
    path = root / 'versions' / BASE / 'assets' / 'poster.png'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'public fixture image')
    entry = {'id': 'admitted-image', 'path': path.relative_to(root).as_posix(), 'mime': 'image/png'}
    if unsafe == 'traversal':
        entry['path'] = '../secret.png'
    elif unsafe == 'absolute':
        entry['path'] = str(path)
    elif unsafe == 'symlink':
        link = path.with_name('linked.png')
        link.symlink_to(path)
        entry['path'] = link.relative_to(root).as_posix()
    else:
        entry['mime'] = 'text/html'
    _edit_original(console, lambda row: row.update({collection: [entry]}))
    assert _client(console).get(URL + '/images/admitted-image').status_code == 404


def _removed_item(console):
    store = console[1]
    revision = store.save(ITEM, payload('Retained human history'), actor='editor-account',
                          expected_head=None, base_version=BASE)
    manifest = write_pack(store.settings.reader_data_dir, NEW, changed_source=True)
    path = store.settings.reader_data_dir / manifest['files']['catalog']
    catalog = json.loads(path.read_bytes())
    catalog['items'] = [row for row in catalog['items'] if row['id'] != ITEM]
    catalog['occurrences'] = [row for row in catalog['occurrences'] if row['item_id'] != ITEM]
    path.write_text(json.dumps(catalog), encoding='utf-8')
    return revision


def _retirement_form(revision, **overrides):
    form = {'csrf_token': 'test-csrf', 'action': 'retire_removed',
            'expected_head': revision['revision'], 'base_version': NEW,
            'retirement_reason': 'Private confirmation: merged into another activity',
            'acknowledge_retirement': 'yes'}
    form.update(overrides)
    return form


def test_removed_queue_retains_original_evidence_and_head_without_editable_save(console):
    revision = _removed_item(console)
    client = _client(console)
    queue = client.get('/console/activities?status=removed').get_data(as_text=True)
    assert 'Retained human history' in queue and '来源已移除 / 合并' in queue
    assert 'Retained human history' in client.get('/console/activities').get_data(as_text=True)
    html = client.get(URL).get_data(as_text=True)
    assert '当前来源已移除 / 合并此活动' in html and '最后保存的历史修订（只读）' in html
    assert 'Original complete body' in html and 'Complete human-reviewed body' in html
    assert revision['revision'] in html and 'editor-account' in html
    assert 'id="reader-review-form"' not in html and 'value="save"' not in html
    assert 'value="retire_removed"' not in html and '仅管理员可明确确认停用' in html
    assert 'value="retire_removed"' in _client(console, 'admin').get(URL).get_data(as_text=True)
    response = client.post(URL, data=_form(expected_head=revision['revision'], base_version=NEW))
    assert response.status_code == 409
    assert 'Human title' in response.get_data(as_text=True)
    assert console[1].get_item(ITEM)['head_revision'] == revision['revision']
    assert not console[1].get_item(ITEM)['retired']


def test_retirement_keeps_login_admin_and_csrf_guards(console):
    revision = _removed_item(console)
    form = _retirement_form(revision)
    assert console[0].test_client().post(URL, data=form).status_code == 400
    assert _client(console, 'member').post(URL, data=form).status_code == 401
    assert _client(console).post(URL, data=form).status_code == 401
    assert _client(console, 'admin').post(URL, data=_retirement_form(revision, csrf_token='wrong')).status_code == 400
    assert not console[1].get_item(ITEM)['retired']
    assert len(console[1].get_item(ITEM)['history']) == 1


@pytest.mark.parametrize('reason,acknowledgement', [
    ('Keep this private reason', ''), ('', 'yes'), ('x' * 2001, 'yes'),
])
def test_retirement_requires_private_reason_and_explicit_acknowledgement_preserving_form(console, reason, acknowledgement):
    revision = _removed_item(console)
    response = _client(console, 'admin').post(URL, data=_retirement_form(
        revision, retirement_reason=reason, acknowledge_retirement=acknowledgement))
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert reason in html
    assert f'name="expected_head" value="{revision["revision"]}"' in html
    assert f'name="base_version" value="{NEW}"' in html
    assert ('value="yes" required checked' in html) == (acknowledgement == 'yes')
    assert not console[1].get_item(ITEM)['retired']


@pytest.mark.parametrize('changed', ['head', 'base'])
def test_retirement_rejects_optimistic_conflicts_without_rebinding_submitted_form(console, changed):
    revision = _removed_item(console)
    overrides = {'expected_head': ''} if changed == 'head' else {'base_version': BASE}
    response = _client(console, 'admin').post(URL, data=_retirement_form(revision, **overrides))
    assert response.status_code == 409
    html = response.get_data(as_text=True)
    assert 'Private confirmation: merged into another activity' in html
    assert f'name="base_version" value="{overrides.get("base_version", NEW)}"' in html
    assert f'name="expected_head" value="{overrides.get("expected_head", revision["revision"])}"' in html
    assert not console[1].get_item(ITEM)['retired']


def test_explicit_retirement_unblocks_other_reviews_without_publishing_or_erasing_history(console):
    revision = _removed_item(console)
    store = console[1]
    other = store.save(OTHER, payload('Unrelated edit'), actor='editor-account',
                       expected_head=None, base_version=NEW)
    with pytest.raises(ReviewConflict):
        store.publication_snapshot()
    root = store.settings.reader_data_dir
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    client = _client(console, 'admin')
    response = client.post(URL, data=_retirement_form(revision))
    assert response.status_code == 302
    item = store.get_item(ITEM)
    assert item['retired'] and item['head_revision'] == revision['revision']
    assert item['history'] == [revision]
    assert item['retirement']['actor'] == 'admin-account'
    assert item['retirement']['reason'] == 'Private confirmation: merged into another activity'
    assert store.publication_snapshot()['revisions'] == [other]
    assert store.get_item(OTHER)['unpublished']
    assert {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()} == before
    html = client.get(response.headers['Location']).get_data(as_text=True)
    assert '已确认停用当前历史修订' in html and '私有原因（不公开）' in html
    assert item['retirement']['reason'] in html and 'Original complete body' in html
    assert 'value="retire_removed"' not in html and 'value="save"' not in html
    assert ITEM not in client.get('/console/activities?status=removed').get_data(as_text=True)
    assert 'Retained human history' in client.get('/console/activities?status=retired').get_data(as_text=True)


def test_review_text_limits_use_server_codepoints_not_html_utf16(console):
    client = _client(console)
    assert 'maxlength=' not in client.get(URL).get_data(as_text=True)
    response = client.post(URL, data=_form(title='😀' * 250, reason='😀' * 2000))
    assert response.status_code == 302
    head = console[1].get_item(ITEM)['head']
    assert head['payload']['activity']['title'] == '😀' * 250
    response = client.post(URL, data=_form(expected_head=head['revision'], title='😀' * 251))
    assert response.status_code == 400 and '😀' * 251 in response.get_data(as_text=True)
    assert console[1].get_item(ITEM)['head_revision'] == head['revision']


def test_both_image_collections_render_with_distinct_evidence_labels(console):
    _edit_original(console, lambda row: row.update(
        images=[{'id': 'archive-image', 'path': 'archive.png', 'mime': 'image/png', 'caption': 'Archive poster'}],
        primary_images=[{'id': 'source-image', 'path': 'source.png', 'mime': 'image/png', 'caption': 'Source poster'}]))
    html = _client(console).get(URL).get_data(as_text=True)
    assert '原刊海报 / 档案配图（参考）' in html and '来源核查图片（只读参考）' in html
    assert 'Archive poster' in html and 'Source poster' in html
    assert f'src="{URL}/images/archive-image"' in html and f'src="{URL}/images/source-image"' in html


def test_surviving_activity_cannot_be_retired_even_by_admin(console):
    store = console[1]
    revision = store.save(ITEM, payload(), actor='editor-account', expected_head=None, base_version=BASE)
    response = _client(console, 'admin').post(URL, data=_retirement_form(revision, base_version=BASE))
    assert response.status_code == 409
    item = store.get_item(ITEM)
    assert not item['retired'] and item['retirements'] == [] and item['history'] == [revision]


def test_retirement_reason_accepts_server_codepoint_boundary_without_html_limit(console):
    revision = _removed_item(console)
    client = _client(console, 'admin')
    assert 'maxlength=' not in client.get(URL).get_data(as_text=True)
    response = client.post(URL, data=_retirement_form(revision, retirement_reason='😀' * 2000))
    assert response.status_code == 302
    assert console[1].get_item(ITEM)['retirement']['reason'] == '😀' * 2000
