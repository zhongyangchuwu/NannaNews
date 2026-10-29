"""Small deterministic private fixtures; no production paths or network access."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from nannanews.data_foundation import (
    SourceDB, build_foundation, compact_foundation, connect_foundation, export_public,
    normalize_date_claim, verify_foundation,
)


ACTIVITY = 'item-' + 'a' * 24
ALIAS = 'item-' + 'b' * 24
MANUAL = 'f' * 64
OFFICIAL = 'https://www.nju.edu.cn/info/123.htm'
PRIVATE = 'PRIVATE_CAPTURE_MODEL_REPLY_DO_NOT_PUBLISH'
HTML = '''<!doctype html><html><body>
<h1>南哪消息 2026年08月02日 第439期</h1>
<h2 id="known">原始刊载标题</h2><p>活动时间：2026年08月04日 地点：原文报告厅</p>
<h2 id="new">尚未核对的新活动</h2><p>时间：2026年08月05日 14:00</p>
</body></html>'''


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return path


def occurrence_id(digest, key):
    return 'occ-' + hashlib.sha256((digest + ':' + key).encode()).hexdigest()[:24]


@pytest.fixture
def public_data(tmp_path):
    root = tmp_path / 'public'
    digest = hashlib.sha256(HTML.encode()).hexdigest()
    identity = occurrence_id(digest, 'section-2')
    activity = {'title': '人工确认标题', 'summary': '人工精简摘要', 'content': PRIVATE,
                'category': '讲座', 'time': '2026-08-04 14:00', 'venue': '人工确认会场',
                'organizer': '南京大学', 'audience': '全校学生', 'participation': '免费参加',
                'source_urls': [OFFICIAL], 'dates': [{'kind': 'event', 'date': '2026-08-04'}]}
    manual = {'revision': MANUAL, 'status': 'verified', 'reason': '人工检查原始通知',
              'reviewed_at': '2026-08-03T08:00:00+08:00', 'activity': activity}
    summary = {'id': identity, 'item_id': ACTIVITY, 'document_id': digest, 'issue_id': 'issue-439',
               'issue_date': '2026-08-02', 'title': activity['title'], 'preview': activity['summary'],
               'category': '讲座', 'kind': 'detail', 'content_status': 'human_verified', 'source_urls': [OFFICIAL]}
    detail = dict(summary, title='原始刊载标题', body_text=PRIVATE, manual_review=manual,
                  locator={'anchor': 'known'}, images=[{'raw_response_base64': PRIVATE}],
                  dates=[{'kind': 'event', 'normalized_date': '2026-08-04', 'raw_text': None, 'evidence': '人工确认'}],
                  date_review={'status': 'resolved', 'reason': '历史来源检查', 'rules_version': 'fixture',
                               'source_review': {'activity': {'title': {'value': '过时机器标题'}}, 'raw_response': PRIVATE}})
    shard = 'versions/test/items/aa.json'
    write_json(root / shard, {ACTIVITY: {'id': ACTIVITY, 'latest_occurrence_id': identity, 'occurrences': [detail]}})
    write_json(root / 'versions/test/catalog.json', {'items': [
        {'id': ACTIVITY, 'aliases': [ALIAS], 'latest_occurrence_id': identity, 'detail_path': shard}],
        'occurrences': [summary]})
    write_json(root / 'versions/test/issues.json', [
        {'id': 'issue-439', 'title': '南哪消息 第439期', 'issue_date': '2026-08-02', 'issue_number': 439,
         'documents': [{'id': digest, 'format': 'html', 'filename': '2026-08-02.html',
                        'claimed_issue_date': '2026-08-02', 'issue_date': '2026-08-02'}]}])
    write_json(root / 'manifest.json', {'schema_version': 2, 'data_version': 'fixture-version',
        'counts': {'items': 1, 'occurrences': 1, 'documents': 1, 'issues': 1},
        'files': {'catalog': 'versions/test/catalog.json', 'issues': 'versions/test/issues.json'}})
    return root


def test_idempotent_identity_alias_manual_fields_and_nullable_dates(tmp_path, public_data):
    root = tmp_path / 'foundation'
    first = build_foundation(root, public_data=public_data, parse_originals=False)
    second = build_foundation(root, public_data=public_data, parse_originals=False)
    assert first['counts'] == second['counts']
    assert second['errors'] == []
    with closing(connect_foundation(root)) as db:
        row = db.execute('SELECT * FROM activities').fetchone()
        assert (row['activity_id'], row['title'], row['summary'], row['venue'], row['review_status']) == (
            ACTIVITY, '人工确认标题', '人工精简摘要', '人工确认会场', 'human_verified')
        assert tuple(db.execute('SELECT * FROM activity_aliases').fetchone()) == (ALIAS, ACTIVITY)
        assert db.execute('SELECT count(*) FROM activity_dates').fetchone()[0] == 1
        revision = db.execute('SELECT * FROM revisions WHERE revision_id=?', (MANUAL,)).fetchone()
        assert revision['reason'] == '人工检查原始通知'
        assert json.loads(revision['fields_json'])['content'] == PRIVATE
        assert row['record_version'] == 'fixture-version'
        assert len(row['input_sha256']) == 64
        assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        with db:
            for _ in range(2):
                db.execute('INSERT OR IGNORE INTO activity_snapshots VALUES(?,?,?)', (ACTIVITY, None, None))
        assert db.execute('SELECT count(*) FROM activity_snapshots').fetchone()[0] == 1
    assert verify_foundation(root)['ok']
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / 'foundation.sqlite3').stat().st_mode & 0o777 == 0o600


def test_html_all_paths_one_document_and_unlinked_sources(tmp_path, public_data):
    paths = [tmp_path / 'originals/first.html', tmp_path / 'originals/alias.html']
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(HTML, encoding='utf-8')
    plan = write_json(tmp_path / 'plan.json', {'historical_files': [
        {'source': str(path), 'filename_date': '2026-08-02', 'content_date': '2026-08-02'} for path in paths]})
    root = tmp_path / 'foundation'
    first = build_foundation(root, public_data=public_data, originals_plan=plan)
    second = build_foundation(root, public_data=public_data, originals_plan=plan)
    assert first['errors'] == second['errors'] == []
    assert first['counts'] == second['counts']
    assert first['counts']['originals'] == 2
    assert first['counts']['documents'] == 1
    assert first['counts']['activities'] == 1
    assert first['pending_review'] == 1
    with closing(connect_foundation(root)) as db:
        rows = list(db.execute('SELECT * FROM source_records ORDER BY title'))
        known = next(row for row in rows if row['activity_id'] is not None)
        pending = next(row for row in rows if row['activity_id'] is None)
        assert known['activity_id'] == ACTIVITY
        assert pending['title'] == '尚未核对的新活动'
        assert pending['parse_status'] == 'pending_review'
        assert json.loads(pending['locator_json'])['id'] == 'new'
        assert db.execute("SELECT count(*) FROM blobs WHERE kind='original' AND owned_local=0").fetchone()[0] == 1
        evidence = db.execute("SELECT local_path FROM blobs WHERE kind='document_evidence'").fetchone()[0]
        stored = json.loads(Path(evidence).read_text())
        assert 'raw_html' not in json.dumps(stored)
        assert 'body_text' not in json.dumps(stored)
        assert all('locator' not in claim for row in stored['entries'] for claim in row['dates'])
    assert all(path.read_text() == HTML for path in paths)
    assert not list(root.glob('.parse-*'))
    assert verify_foundation(root)['ok']


def test_canonical_immutable_revisions_and_upload_relative_originals(tmp_path, public_data):
    canonical = tmp_path / 'canonical'
    canonical.mkdir()
    original = canonical / 'uploads/bulletins/fixture/original.html'
    original.parent.mkdir(parents=True)
    original.write_text(HTML, encoding='utf-8')
    path = canonical / 'database.db'
    with closing(sqlite3.connect(path)) as source, source:
        source.executescript('''
            CREATE TABLE bulletin_documents(id INTEGER PRIMARY KEY,source_sha256 TEXT,format TEXT,
              source_filename TEXT,original_path TEXT,parsed_json TEXT);
            CREATE TABLE reader_review_revisions(id INTEGER PRIMARY KEY,item_id TEXT,revision TEXT,
              created_at TEXT,base_version TEXT,previous_head TEXT,payload_json TEXT);
            CREATE TABLE bulletin_occurrences(id INTEGER PRIMARY KEY,item_id INTEGER,local_key TEXT,
              locator_json TEXT,document_id INTEGER);
        ''')
        digest = hashlib.sha256(HTML.encode()).hexdigest()
        source.execute('INSERT INTO bulletin_documents VALUES(1,?,?,?,?,?)',
                       (digest, 'html', original.name, 'bulletins/fixture/original.html',
                        json.dumps({'issue_date': '2026-08-02', 'issue_number': 439})))
        payload = {'status': 'needs_review', 'reason': '尚未发布的人工草稿',
                   'activity': {'title': '草稿不是当前公开标题', 'content': PRIVATE}}
        source.execute('INSERT INTO reader_review_revisions VALUES(1,?,?,?,?,?,?)',
                       (ALIAS, 'd' * 64, '2026-08-03T00:00:00Z', 'old-version', MANUAL, json.dumps(payload)))
        source.execute('INSERT INTO bulletin_occurrences VALUES(41,7,?,?,1)',
                       ('section-2', json.dumps({'id': 'known', 'raw_html': PRIVATE})))
    before = path.read_bytes()
    with SourceDB(path) as source:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            source.db.execute('DELETE FROM bulletin_documents')
    root = tmp_path / 'foundation'
    result = build_foundation(root, public_data=public_data, canonical_data=canonical, parse_originals=False)
    assert result['errors'] == []
    assert path.read_bytes() == before
    assert not Path(str(path) + '-wal').exists()
    with closing(connect_foundation(root)) as db:
        assert db.execute('SELECT path FROM originals').fetchone()[0] == str(original)
        revision = db.execute('SELECT * FROM revisions WHERE revision_id=?', ('d' * 64,)).fetchone()
        assert revision['activity_id'] == ACTIVITY
        assert revision['reason'] == '尚未发布的人工草稿'
        assert json.loads(revision['fields_json'])['legacy_item_id'] == ALIAS
        assert db.execute('SELECT title FROM activities').fetchone()[0] == '人工确认标题'
        locator = json.loads(db.execute('SELECT locator_json FROM source_records').fetchone()[0])
        assert locator['local_key'] == 'section-2'
        assert locator['legacy_occurrence_id'] == 41
        assert locator['legacy_item_id'] == 7
        assert locator['legacy_source_db'] == str(path)
        assert db.execute('SELECT count(*) FROM activity_aliases').fetchone()[0] == 1
        assert 'raw_html' not in locator


def test_pdf_date_conflict_real_normalization_and_no_duplicate_parser_tree(tmp_path, public_data, monkeypatch):
    # Substitute only the external command boundary. The installed adapter still
    # validates/normalizes a 2.14.4-shaped tagged page and detects the issue date.
    from nannanews import pdf_adapter
    source = tmp_path / '0803.pdf'
    source.write_bytes(b'%PDF-2.0\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n')

    def node(kind, mcid=None, children=()):
        return {'type': kind, 'marked_content_ids': [] if mcid is None else [mcid],
                'children': list(children), 'annotations': []}

    fragments = [{'mcid': i, 'text': text, 'x': 20, 'y': i * 30, 'width': 400, 'height': 12, 'font_size': 12}
                 for i, text in enumerate(['南哪消息', '2026-08-02 No. 439', '未刊载的新通知', '时间：2026年08月08日 14:00'])]
    payload = {'total_pages': 1, 'images': [], 'pages': [
        {'page': 1, 'width': 800, 'height': 1000, 'text': PRIVATE,
         'text_items': fragments, 'annotations': [], 'structure_tree': {'roots': [
             node('Document', children=[node('P', 0), node('P', 1), node('H1', 2), node('P', 3)])]}}]}
    monkeypatch.setattr(pdf_adapter.shutil, 'which', lambda name: '/private/lit')

    def command(argv, root, **kwargs):
        if '--version' in argv:
            return 'lit 2.14.4\n'
        (root / 'rawparser.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        return ''

    monkeypatch.setattr(pdf_adapter, '_run', command)
    plan = write_json(tmp_path / 'plan.json', {'historical_files': [
        {'source': str(source), 'filename_date': '2026-08-03', 'content_date': '2026-08-02', 'issue_number': 439}]})
    root = tmp_path / 'foundation'
    result = build_foundation(root, public_data=public_data, originals_plan=plan)
    assert result['errors'] == []
    assert result['counts']['activities'] == 1
    assert result['pending_review'] == 1
    with closing(connect_foundation(root)) as db:
        original = db.execute('SELECT * FROM originals').fetchone()
        assert (original['filename_date'], original['content_date'], original['date_status']) == (
            '2026-08-03', '2026-08-02', 'conflict')
        document = db.execute("SELECT * FROM documents WHERE format='pdf'").fetchone()
        assert (document['claimed_date'], document['issue_date'], document['issue_number']) == ('2026-08-03', '2026-08-02', 439)
        entry = db.execute('SELECT * FROM source_records WHERE activity_id IS NULL').fetchone()
        assert entry['title'] == '未刊载的新通知'
        assert entry['parse_status'] == 'pending_review'
        locator = json.loads(entry['locator_json'])
        assert locator['page'] == 1
        assert locator['tagpath']
        evidence = db.execute('SELECT local_path FROM blobs WHERE sha256=?', (locator['evidence_blob_sha256'],)).fetchone()[0]
        text = Path(evidence).read_text()
        assert PRIVATE not in text
        assert 'structure_tree' not in text
        assert json.loads(text)['pages'] == [{'page': 1, 'width': 800, 'height': 1000}]
    assert source.exists()
    assert not list(root.rglob('rawparser.json'))
    assert not list(root.rglob('source.pdf'))
    assert not list(root.glob('.parse-*'))


def test_public_whitelist_excludes_private_graph_and_credential_urls(tmp_path, public_data):
    root = tmp_path / 'foundation'
    build_foundation(root, public_data=public_data, parse_originals=False)
    with closing(connect_foundation(root)) as db, db:
        urls = [OFFICIAL, 'https://www.nju.edu.cn/?api_key=secret',
                'https://authserver.nju.edu.cn/authserver/login', 'http://127.0.0.1/private',
                'https://user:password@www.nju.edu.cn/private']
        db.execute('UPDATE source_records SET source_urls_json=?,summary=?,locator_json=?',
                   (json.dumps(urls), PRIVATE, json.dumps({'raw_html': PRIVATE})))
    output = tmp_path / 'projection.json'
    result = export_public(root, output)
    assert result['activities'] == 1
    text = output.read_text()
    assert PRIVATE not in text
    assert 'secret' not in text and 'password' not in text and 'authserver' not in text
    item = json.loads(text)['activities'][0]
    assert item['source_urls'] == [OFFICIAL]
    assert item['title'] == '人工确认标题'
    assert set(item) == {'activity_id', 'title', 'summary', 'category', 'time_text', 'venue', 'organizer',
                         'audience', 'participation', 'review_status', 'source_urls', 'dates'}
    assert item['dates'] == [{'kind': 'event', 'date': '2026-08-04'}]
    before = (root / 'foundation.sqlite3').read_bytes()
    with pytest.raises(ValueError, match='outside'):
        export_public(root, root / 'foundation.sqlite3')
    assert (root / 'foundation.sqlite3').read_bytes() == before


def test_failed_original_is_reported_and_source_remains(tmp_path, public_data):
    invalid = tmp_path / 'bad.pdf'
    invalid.write_bytes(b'not a PDF')
    plan = write_json(tmp_path / 'plan.json', {'historical_files': [{'source': str(invalid)}]})
    root = tmp_path / 'foundation'
    result = build_foundation(root, public_data=public_data, originals_plan=plan)
    assert result['counts']['originals'] == 1
    assert result['errors'][0]['code'] == 'invalid_pdf'
    assert invalid.read_bytes() == b'not a PDF'
    verified = verify_foundation(root)
    assert not verified['ok']
    assert verified['statuses']['documents']['failed'] == 1
    assert verified['preparation_errors'][0]['stage'] == 'parse_original'
    assert not list(root.glob('.parse-*'))


def test_verify_detects_source_change_and_missing_semantic_reference(tmp_path, public_data):
    original = tmp_path / 'original.html'
    original.write_text(HTML, encoding='utf-8')
    plan = write_json(tmp_path / 'plan.json', {'historical_files': [{'source': str(original)}]})
    root = tmp_path / 'foundation'
    build_foundation(root, public_data=public_data, originals_plan=plan, parse_originals=False)
    original.write_text('changed', encoding='utf-8')
    with closing(connect_foundation(root)) as db, db:
        db.execute('INSERT INTO activity_dates(activity_id,kind,date,raw_text,evidence,source_id) VALUES(?,?,?,?,?,?)',
                   (ACTIVITY, 'event', None, 'unknown', None, 'missing-source'))
    verified = verify_foundation(root)
    assert not verified['ok']
    assert {'original_changed', 'blob_changed', 'missing_reference'} <= {row['code'] for row in verified['errors']}


def test_manifest_counts_and_traversal_fail_without_partial_domain_import(tmp_path, public_data):
    manifest_path = public_data / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['counts']['occurrences'] = 2
    write_json(manifest_path, manifest)
    root = tmp_path / 'foundation'
    with pytest.raises(ValueError, match='count mismatch'):
        build_foundation(root, public_data=public_data)
    with closing(connect_foundation(root)) as db:
        assert db.execute('SELECT count(*) FROM activities').fetchone()[0] == 0
    manifest['files']['catalog'] = '../outside.json'
    write_json(manifest_path, manifest)
    with pytest.raises(ValueError, match='escapes'):
        build_foundation(root, public_data=public_data)


def test_read_commands_do_not_create_unprepared_roots(tmp_path):
    root = tmp_path / 'not-prepared'
    with pytest.raises(ValueError, match='not been explicitly prepared'):
        verify_foundation(root)
    with pytest.raises(ValueError, match='not been explicitly prepared'):
        export_public(root, tmp_path / 'output.json')
    assert not root.exists()


@pytest.mark.parametrize('name', ['database.db', 'settings.json', 'site.json'])
def test_public_export_refuses_existing_targets(tmp_path, public_data, name):
    root = tmp_path / 'foundation'
    build_foundation(root, public_data=public_data, parse_originals=False)
    output = tmp_path / name
    sentinel = b'PROTECTED EXISTING DATA'
    output.write_bytes(sentinel)
    with pytest.raises(FileExistsError, match='overwrite'):
        export_public(root, output)
    assert output.read_bytes() == sentinel
    assert not list(tmp_path.glob('.foundation-public-*'))


def test_public_export_preserves_target_appearing_at_publish(tmp_path, public_data, monkeypatch):
    root = tmp_path / 'foundation'
    build_foundation(root, public_data=public_data, parse_originals=False)
    output = tmp_path / 'racing-target.json'
    link = os.link

    def racing_link(source, destination):
        Path(destination).write_bytes(b'CONCURRENT PROTECTED DATA')
        return link(source, destination)

    monkeypatch.setattr(os, 'link', racing_link)
    with pytest.raises(FileExistsError):
        export_public(root, output)
    assert output.read_bytes() == b'CONCURRENT PROTECTED DATA'
    assert not list(tmp_path.glob('.foundation-public-*'))


def test_rebuild_refreshes_effective_head_without_reviving_withdrawn_facts(tmp_path, public_data):
    manifest_path = public_data / 'manifest.json'
    catalog_path = public_data / 'versions/test/catalog.json'
    shard_path = public_data / 'versions/test/items/aa.json'
    manifest = json.loads(manifest_path.read_text())
    catalog = json.loads(catalog_path.read_text())
    shard = json.loads(shard_path.read_text())
    current = shard[ACTIVITY]['occurrences'][0]
    current_id = current['id']
    old_id = 'occ-' + 'c' * 24
    old_url = 'https://www.nju.edu.cn/older.htm'
    old_date = {'kind': 'event', 'normalized_date': '2026-07-01', 'raw_text': '2026-07-01'}
    older = dict(current, id=old_id, title='旧期活动标题', source_urls=[old_url],
                 dates=[old_date], manual_review=None, date_review=None)
    # The declared head, not list ordering or an aggregate of all occurrences,
    # defines current public facts. Historical source records remain available.
    shard[ACTIVITY]['occurrences'].insert(0, older)
    catalog['occurrences'].append(dict(catalog['occurrences'][0], id=old_id,
                                       source_urls=[old_url], dates=[old_date]))
    manifest['counts']['occurrences'] = 2
    write_json(manifest_path, manifest)
    write_json(catalog_path, catalog)
    write_json(shard_path, shard)
    original = tmp_path / 'original.html'
    original.write_text(HTML, encoding='utf-8')
    plan = write_json(tmp_path / 'plan.json', {'historical_files': [{'source': str(original)}]})
    root = tmp_path / 'foundation'
    build_foundation(root, public_data=public_data, originals_plan=plan, parse_originals=False)
    with closing(connect_foundation(root)) as db, db:
        locator = json.loads(db.execute('SELECT locator_json FROM source_records WHERE source_id=?', (current_id,)).fetchone()[0])
        locator.update(legacy_source_db='/private/canonical/database.db', legacy_occurrence_id=41, page=3)
        db.execute('UPDATE source_records SET original_ref=?,locator_json=? WHERE source_id=?',
                   (str(original), json.dumps(locator), current_id))
        before_ids = [row[0] for row in db.execute('SELECT source_id FROM source_records ORDER BY source_id')]
        first_sha = db.execute('SELECT input_sha256 FROM activities').fetchone()[0]
    initial_output = tmp_path / 'initial-projection.json'
    export_public(root, initial_output)
    initial = json.loads(initial_output.read_text())['activities'][0]
    assert initial['source_urls'] == [OFFICIAL]
    assert initial['dates'] == [{'kind': 'event', 'date': '2026-08-04'}]

    updated_url = 'https://www.nju.edu.cn/updated.htm'
    updated_date = {'kind': 'event', 'normalized_date': '2026-08-09', 'raw_text': '2026-08-09'}
    current.update(dates=[updated_date], source_urls=[updated_url])
    current['manual_review'].update(revision='e' * 64, reason='人工更正日期和来源')
    current['manual_review']['activity'].update(title='人工更正标题', summary='人工更正摘要',
        dates=[{'kind': 'event', 'date': '2026-08-09'}], source_urls=[updated_url])
    catalog['occurrences'][0].update(title='人工更正标题', preview='人工更正摘要',
                                      source_urls=[updated_url], dates=[updated_date])
    manifest['data_version'] = 'revision-two'
    write_json(manifest_path, manifest)
    write_json(catalog_path, catalog)
    write_json(shard_path, shard)
    result = build_foundation(root, public_data=public_data, originals_plan=plan, parse_originals=False)
    assert result['errors'] == []
    updated_output = tmp_path / 'updated-projection.json'
    export_public(root, updated_output)
    updated = json.loads(updated_output.read_text())['activities'][0]
    assert updated['title'] == '人工更正标题'
    assert updated['source_urls'] == [updated_url]
    assert updated['dates'] == [{'kind': 'event', 'date': '2026-08-09'}]
    with closing(connect_foundation(root)) as db:
        source = db.execute('SELECT * FROM source_records WHERE source_id=?', (current_id,)).fetchone()
        assert source['title'] == '人工更正标题'
        assert source['summary'] == '人工更正摘要'
        assert source['original_ref'] == str(original)
        assert json.loads(source['locator_json']) == locator
        assert [row[0] for row in db.execute('SELECT source_id FROM source_records ORDER BY source_id')] == before_ids
        assert tuple(db.execute('SELECT * FROM activity_aliases').fetchone()) == (ALIAS, ACTIVITY)
        assert {row[0] for row in db.execute('SELECT revision_id FROM revisions')} >= {MANUAL, 'e' * 64}
        assert db.execute('SELECT input_sha256 FROM activities').fetchone()[0] != first_sha
        assert [row[0] for row in db.execute('SELECT date FROM activity_dates WHERE source_id=?', (current_id,))] == ['2026-08-09']

    current.update(dates=[], source_urls=[], content_status='needs_review')
    current['manual_review'].update(revision='d' * 64, status='needs_review', reason='撤回未确定日期和来源')
    current['manual_review']['activity'].update(dates=[], source_urls=[])
    catalog['occurrences'][0].update(dates=[], source_urls=[], content_status='needs_review')
    manifest['data_version'] = 'revision-three'
    write_json(manifest_path, manifest)
    write_json(catalog_path, catalog)
    write_json(shard_path, shard)
    build_foundation(root, public_data=public_data, originals_plan=plan, parse_originals=False)
    cleared_output = tmp_path / 'cleared-projection.json'
    export_public(root, cleared_output)
    cleared = json.loads(cleared_output.read_text())['activities'][0]
    assert cleared['source_urls'] == []
    assert cleared['dates'] == []
    assert cleared['review_status'] == 'needs_review'
    with closing(connect_foundation(root)) as db:
        assert db.execute('SELECT count(*) FROM activity_dates WHERE source_id=?', (current_id,)).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM activity_dates WHERE source_id=?', (old_id,)).fetchone()[0] == 1
        assert {row[0] for row in db.execute('SELECT revision_id FROM revisions')} >= {MANUAL, 'e' * 64, 'd' * 64}

    # An invalid replacement snapshot cannot partially restore the withdrawn facts.
    current['dates'] = [updated_date]
    current['source_urls'] = [updated_url]
    catalog['occurrences'][0].update(dates=[updated_date], source_urls=[updated_url])
    manifest['counts']['occurrences'] = 3
    write_json(manifest_path, manifest)
    write_json(catalog_path, catalog)
    write_json(shard_path, shard)
    with pytest.raises(ValueError, match='count mismatch'):
        build_foundation(root, public_data=public_data, parse_originals=False)
    after_failure = tmp_path / 'after-failure.json'
    export_public(root, after_failure)
    assert json.loads(after_failure.read_text())['activities'][0] == cleared


@pytest.mark.parametrize('raw,reason', [
    ('2026年8月4日 14:00', None),
    ('日' * 512, None),
    ('日' * 513, 'oversized'),
    ('{"id":123,"company":{"intro":"招聘API响应"}}', 'non_date_payload'),
    ('[{"date":"2026-08-04"}]', 'non_date_payload'),
    ('<html><p>2026-08-04</p></html>', 'non_date_payload'),
])
def test_normalize_date_claim_preserves_semantics_and_original_digest(raw, reason):
    claim = {'kind': 'event', 'normalized_date': '2026-08-04', 'raw_text': raw,
             'evidence': '通知正文明确列出活动日期', 'resolution': 'explicit', 'source_id': 'source-fixture'}
    before = dict(claim)
    normalized = normalize_date_claim(claim)
    assert claim == before
    assert normalized['raw_text_sha256'] == hashlib.sha256(raw.encode()).hexdigest()
    for key in ('kind', 'normalized_date', 'evidence', 'resolution', 'source_id'):
        assert normalized[key] == claim[key]
    if reason:
        assert normalized['raw_text'] is None
        assert normalized['raw_text_omitted'] == reason
    else:
        assert normalized['raw_text'] == raw
        assert 'raw_text_omitted' not in normalized
    assert normalize_date_claim(normalized) == normalized
    missing = {'kind': 'event', 'normalized_date': '2026-08-04', 'raw_text': None}
    assert normalize_date_claim(missing) == missing


def test_schema_one_migration_compacts_dates_without_changing_ids_or_facts(tmp_path, public_data):
    raw_payloads = [json.dumps({'id': index, 'company': {'intro': '招聘API完整响应' * 3000}}, ensure_ascii=False)
                    for index in (1, 2)]
    claims = [{'kind': 'event', 'normalized_date': '2026-08-04', 'raw_text': raw,
               'evidence': '官方通知日期证据', 'resolution': 'explicit'}
              for raw in [*raw_payloads, '2026年8月4日']]
    shard_path = public_data / 'versions/test/items/aa.json'
    shard = json.loads(shard_path.read_text())
    occurrence = shard[ACTIVITY]['occurrences'][0]
    source_id = occurrence['id']
    occurrence['dates'] = claims
    occurrence['date_review']['claims'] = claims
    write_json(shard_path, shard)
    root = tmp_path / 'foundation'
    build_foundation(root, public_data=public_data, parse_originals=False)
    before_output = tmp_path / 'before-migration.json'
    export_public(root, before_output)
    with closing(connect_foundation(root)) as db, db:
        revision_ids = {row[0] for row in db.execute('SELECT revision_id FROM revisions')}
        activity_ids = [row[0] for row in db.execute('SELECT activity_id FROM activities')]
        source_ids = [row[0] for row in db.execute('SELECT source_id FROM source_records')]
        aliases = [tuple(row) for row in db.execute('SELECT * FROM activity_aliases')]
        historical = db.execute("SELECT revision_id FROM revisions WHERE reason='历史来源检查'").fetchone()[0]
        # Reconstruct the actual schema-1 representation in this temporary DB.
        # Both long-TEXT identity indexes existed before the migration.
        db.execute('DROP TABLE activity_dates')
        db.execute('''CREATE TABLE activity_dates(activity_id TEXT REFERENCES activities,kind TEXT,
            date TEXT,raw_text TEXT,evidence TEXT,source_id TEXT,
            PRIMARY KEY(activity_id,kind,date,raw_text,source_id))''')
        db.execute('''CREATE UNIQUE INDEX dates_identity ON activity_dates(
            coalesce(activity_id,''),coalesce(kind,''),coalesce(date,''),coalesce(raw_text,''),coalesce(source_id,''))''')
        for claim in claims:
            db.execute('INSERT INTO activity_dates VALUES(?,?,?,?,?,?)',
                       (ACTIVITY, claim['kind'], claim['normalized_date'], claim['raw_text'], claim['evidence'], source_id))
        db.execute('UPDATE revisions SET fields_json=? WHERE revision_id=?',
                   (json.dumps({'claims': claims, 'rules_version': 'fixture'}, ensure_ascii=False), historical))
        db.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
    path = root / 'foundation.sqlite3'
    with closing(connect_foundation(root)) as db:
        assert db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == '2'
        assert [row['name'] for row in db.execute('PRAGMA table_info(activity_dates)')] == [
            'activity_id', 'kind', 'date', 'raw_text', 'evidence', 'source_id', 'raw_text_sha256']
        rows = list(db.execute('SELECT * FROM activity_dates ORDER BY raw_text_sha256'))
        assert len(rows) == 3
        assert {row['raw_text_sha256'] for row in rows} == {
            hashlib.sha256(claim['raw_text'].encode()).hexdigest() for claim in claims}
        assert sum(row['raw_text'] is None for row in rows) == 2
        assert {row['raw_text'] for row in rows if row['raw_text'] is not None} == {'2026年8月4日'}
        assert all((row['kind'], row['date'], row['evidence'], row['source_id']) ==
                   ('event', '2026-08-04', '官方通知日期证据', source_id) for row in rows)
        index_bytes = db.execute("SELECT sum(pgsize) FROM dbstat WHERE name='dates_identity'").fetchone()[0]
        assert index_bytes < len(raw_payloads[0].encode())
        fields = json.loads(db.execute('SELECT fields_json FROM revisions WHERE revision_id=?', (historical,)).fetchone()[0])
        assert fields['claims'] == [normalize_date_claim(claim) for claim in claims]
        assert db.execute("SELECT count(*) FROM metadata WHERE key LIKE 'date_raw_omitted:%'").fetchone()[0] == 2
        assert {row[0] for row in db.execute('SELECT revision_id FROM revisions')} == revision_ids
        assert [row[0] for row in db.execute('SELECT activity_id FROM activities')] == activity_ids
        assert [row[0] for row in db.execute('SELECT source_id FROM source_records')] == source_ids
        assert [tuple(row) for row in db.execute('SELECT * FROM activity_aliases')] == aliases
    after_output = tmp_path / 'after-migration.json'
    export_public(root, after_output)
    assert after_output.read_bytes() == before_output.read_bytes()
    compacted = compact_foundation(root)
    assert compacted['schema_version'] == '2'
    assert compacted['bytes_before'] > compacted['bytes_after'] == path.stat().st_size
    assert compacted['bytes_reclaimed'] > 0
    # Re-reading/rebuilding the original raw input must reuse original-derived
    # revision IDs, not manufacture new normalized-input history entries.
    build_foundation(root, public_data=public_data, parse_originals=False)
    with closing(connect_foundation(root)) as db:
        assert {row[0] for row in db.execute('SELECT revision_id FROM revisions')} == revision_ids
        assert db.execute('SELECT count(*) FROM activity_dates').fetchone()[0] == 3
        assert all(row[0] is None or len(row[0]) < 512 for row in db.execute('SELECT raw_text FROM activity_dates'))
    rebuilt_output = tmp_path / 'after-rebuild.json'
    export_public(root, rebuilt_output)
    assert rebuilt_output.read_bytes() == before_output.read_bytes()


def test_compaction_refuses_unprepared_and_unrelated_databases(tmp_path):
    root = tmp_path / 'not-prepared'
    with pytest.raises(ValueError, match='not been explicitly prepared'):
        compact_foundation(root)
    assert not root.exists()
    root.mkdir()
    path = root / 'foundation.sqlite3'
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE official_data(value TEXT)')
        db.execute('INSERT INTO official_data VALUES(?)', ('protected',))
    before = path.read_bytes()
    with pytest.raises(ValueError, match='not a prepared foundation'):
        compact_foundation(root)
    assert path.read_bytes() == before


def test_metadata_check_and_explicit_full_hash_have_distinct_guarantees(tmp_path):
    from nannanews.data_foundation_blobs import BlobStore

    root = tmp_path / 'foundation'
    with closing(connect_foundation(root)) as db, db:
        digest = BlobStore(root, db).put_bytes(b'first', mime='text/plain', kind='answer')
    (root / 'blobs' / digest).write_bytes(b'other')

    metadata = verify_foundation(root)
    assert metadata['ok'] and metadata['level'] == 'metadata'
    full = verify_foundation(root, full=True)
    assert not full['ok'] and full['level'] == 'full'
    assert {'code': 'blob_changed', 'sha256': digest} in full['errors']


def test_missing_local_content_is_not_covered_by_archive_configuration(tmp_path):
    from nannanews.data_foundation_blobs import BlobStore

    root = tmp_path / 'foundation'
    with closing(connect_foundation(root)) as db, db:
        digest = BlobStore(root, db).put_bytes(b'local bytes', mime='text/plain', kind='answer')
        db.execute('UPDATE blobs SET local_path=NULL WHERE sha256=?', (digest,))
        db.execute('INSERT INTO metadata VALUES(?,?)',
                   ('restic_archive:v1', 'Cloud credentials are not part of local verification.'))

    result = verify_foundation(root)
    assert not result['ok']
    assert result['errors'] == [{'code': 'blob_has_no_local_copy', 'sha256': digest}]


def test_verification_uses_registered_original_after_source_relocation(tmp_path, public_data):
    original = tmp_path / 'loose-original.html'
    original.write_text(HTML, encoding='utf-8')
    plan = write_json(tmp_path / 'originals.json', {'historical_files': [{'source': str(original)}]})
    root = tmp_path / 'foundation'
    build_foundation(root, public_data=public_data, originals_plan=plan, parse_originals=False)
    with closing(connect_foundation(root)) as db, db:
        digest = db.execute('SELECT sha256 FROM originals WHERE path=?', (str(original),)).fetchone()[0]
        managed = root / 'blobs' / digest
        managed.parent.mkdir(exist_ok=True)
        original.rename(managed)
        db.execute('UPDATE blobs SET local_path=?,owned_local=1 WHERE sha256=?', (str(managed), digest))
    assert verify_foundation(root)['ok']
    assert verify_foundation(root, full=True)['ok']
    with closing(connect_foundation(root)) as db:
        assert db.execute('SELECT path FROM originals').fetchone()[0] == str(original)
