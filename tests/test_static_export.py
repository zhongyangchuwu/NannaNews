"""Consumer-boundary coverage using real SQLite and filesystem evidence.

No runtime/bootstrap, model, network, or production database is needed. The
fixture deliberately poisons every private metadata surface with a sentinel.
"""
import base64
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from types import SimpleNamespace

import pytest


from nannanews import static_export
from nannanews.bulletin_date_repair import create_date_schema, record_revision
from nannanews.bulletin_activities import create_activity_schema

PRIVATE = 'PRIVATE_SENTINEL_users_keys_editorial_parser_provenance'
PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jh9sAAAAASUVORK5CYII=')
PDF = b'%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n'


def _json(value):
    return json.dumps(value, ensure_ascii=False)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _sql(settings, sql, parameters=()):
    with closing(sqlite3.connect(settings.database_path)) as db, db:
        return db.execute(sql, parameters).fetchall()


@pytest.fixture
def evidence(tmp_path):
    private = tmp_path / 'private'
    uploads = private / 'uploads'
    uploads.mkdir(parents=True)
    settings = SimpleNamespace(data_dir=private, upload_dir=uploads,
                               database_path=private / 'database.db')
    # Actual store column names, with irrelevant private tables also present.
    with closing(sqlite3.connect(settings.database_path)) as db, db:
        db.executescript('''
            CREATE TABLE bulletin_issues(id INTEGER PRIMARY KEY,identity_key TEXT,title TEXT,
                issue_date TEXT,issue_number INTEGER);
            CREATE TABLE bulletin_documents(id INTEGER PRIMARY KEY,issue_id INTEGER,
                source_sha256 TEXT,format TEXT,source_filename TEXT,original_path TEXT,
                parsed_json TEXT,parser_json TEXT,raw_artifact_path TEXT,normalized_path TEXT,
                imported_at TEXT,actor TEXT,text TEXT,issue_date_evidence TEXT);
            CREATE TABLE bulletin_items(id INTEGER PRIMARY KEY,identity_key TEXT,identity_basis TEXT);
            CREATE TABLE bulletin_occurrences(id INTEGER PRIMARY KEY,item_id INTEGER,document_id INTEGER,
                local_key TEXT,kind TEXT,title TEXT,category TEXT,body_text TEXT,
                locator_json TEXT,image_refs_json TEXT,raw_item_json TEXT);
            CREATE TABLE bulletin_dates(id INTEGER PRIMARY KEY,occurrence_id INTEGER,kind TEXT,
                raw_text TEXT,normalized_date TEXT,resolution TEXT,evidence TEXT,locator_json TEXT);
            CREATE TABLE bulletin_colleges(id INTEGER PRIMARY KEY,occurrence_id INTEGER,
                name TEXT,relation TEXT,evidence TEXT,locator_json TEXT);
            CREATE TABLE bulletin_sources(occurrence_id INTEGER,source_url TEXT);
            CREATE TABLE bulletin_document_sources(document_id INTEGER,source_url TEXT,evidence_json TEXT);
            CREATE TABLE bulletin_assets(document_id INTEGER,asset_id TEXT,private_path TEXT,
                sha256 TEXT,evidence_json TEXT);
            CREATE TABLE bulletin_document_provenance(document_id INTEGER,evidence_json TEXT);
            CREATE TABLE source_documents(content TEXT);
            CREATE TABLE entries(title TEXT);
            CREATE TABLE users(password TEXT);
            CREATE TABLE api_keys(secret TEXT);
        ''')
        for table, column in [('source_documents', 'content'), ('entries', 'title'),
                              ('users', 'password'), ('api_keys', 'secret')]:
            db.execute(f'INSERT INTO {table}({column}) VALUES(?)', (PRIVATE,))
        db.executemany('INSERT INTO bulletin_issues VALUES(?,?,?,?,?)', [
            (1, 'issue:2026-10-13', '南哪第1期', '2026-10-13', 1),
            (2, 'issue:2026-10-14', '南哪第2期', '2026-10-14', 2),
        ])
        db.executemany('INSERT INTO bulletin_items VALUES(?,?,?)', [
            (7, 'public-source:讲座', PRIVATE), (8, 'document:通知', PRIVATE),
        ])
        original_html = ('<html><body><h1>南哪消息 2026-10-13</h1><h2>讲座</h2>'
                         '<table><tr><th>活动</th></tr><tr><td>科学讲座</td></tr></table>'
                         '<h2>具体通知标题</h2><p>通知自己的正文，不是栏目。</p>'
                         '<h3>另一条消息</h3><p>后续消息正文。</p></body></html>').encode()
        docs = [(11, 1, 'html', original_html, 'one.html', '2026-10-13'),
                (12, 1, 'pdf', PDF, 'original.pdf', '2026-10-13'),
                (13, 2, 'html', original_html + b'\n', 'two.html', '2026-10-14')]
        for document_id, issue_id, kind, original, filename, issue_date in docs:
            digest = _sha(original)
            relative = f'bulletins/{digest}/original.{kind}'
            original_path = uploads / relative
            original_path.parent.mkdir(parents=True)
            original_path.write_bytes(original)
            parsed = {'issue_date': issue_date, 'claimed_issue_date': '2026-10-12',
                      'warnings': [{'code': 'claimed_issue_date_mismatch', 'message': PRIVATE},
                                   {'code': PRIVATE, 'private_path': PRIVATE}],
                      'parser': PRIVATE, 'pages': PRIVATE, 'items': PRIVATE, 'text': PRIVATE,
                      'provenance': PRIVATE, 'raw_artifact_path': PRIVATE}
            db.execute('INSERT INTO bulletin_documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (document_id, issue_id, digest, kind, filename, relative, _json(parsed),
                        PRIVATE, PRIVATE, PRIVATE, PRIVATE, PRIVATE, PRIVATE, PRIVATE))
            db.execute('INSERT INTO bulletin_document_sources VALUES(?,?,?)',
                       (document_id, f'https://bulletin.example.edu/issue/{document_id}', _json({'actor': PRIVATE})))
            db.execute('INSERT INTO bulletin_document_provenance VALUES(?,?)', (document_id, PRIVATE))
        locator = {'id': 'lecture-anchor', 'raw_html': PRIVATE,
                   'raw_section_html': PRIVATE, 'private_path': PRIVATE}
        html_table = dict(locator, headers=['活动', '截止时间'], cells=['科学讲座', '2026-10-30'])
        pdf_table = {'page': 3, 'cells': [{'header': '内容', 'text': '原表文字', 'private_path': PRIVATE}],
                     'raw_html': PRIVATE, 'tagpath': PRIVATE}
        rows = [
            (100, 7, 11, 'table-1-row-1', 'summary', '科学讲座', '讲座', '科学讲座摘要', html_table, []),
            (101, 7, 11, 'section-2', 'detail', '科学讲座', '讲座', '科学讲座全文：所有历史正文', locator,
             ['poster', 'unacquired-remote-image']),
            (102, 8, 12, 'p3-tag-1', 'notice', '原始PDF通知', None, 'PDF中原始通知内容', pdf_table, []),
            (103, 7, 13, 'section-1', 'detail', '科学讲座新版', '讲座', '日期不确定：10月20日', locator, []),
            (104, 7, 13, 'table-1-row-1', 'summary', '科学讲座新版', '讲座', '原样保留日期范围和无效日期', html_table, []),
        ]
        for row in rows:
            db.execute('INSERT INTO bulletin_occurrences VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (*row[:8], _json(row[8]), _json(row[9]), _json({'raw_html': PRIVATE, 'parser': PRIVATE})))
        db.executemany('INSERT INTO bulletin_dates VALUES(?,?,?,?,?,?,?,?)', [
            (1, 100, 'deadline', '2026-10-30', '2026-10-30', 'explicit', '截止：2026-10-30', _json({'private_path': PRIVATE})),
            (2, 101, 'event', '2026年10月20日', '2026-10-20', 'explicit', '活动：2026年10月20日', _json({'private_path': PRIVATE})),
            (3, 102, 'event', '10月21日', None, 'year_unknown', '活动：10月21日', _json({'private_path': PRIVATE})),
            (4, 103, 'event', '10月20日', None, 'year_unknown', '活动：10月20日', _json({'private_path': PRIVATE})),
            (5, 104, 'deadline', '2026-13-99', None, 'invalid', '截止：2026-13-99', _json({'private_path': PRIVATE})),
            (6, 104, 'event', '2026-10-20至2026-10-22', None, 'ambiguous', '完整日期范围', _json({'private_path': PRIVATE})),
            (7, 103, 'source', '2025-01-01', '2025-01-01', 'explicit', '来源日期', _json({'private_path': PRIVATE})),
        ])
        db.execute('INSERT INTO bulletin_colleges VALUES(?,?,?,?,?,?)',
                   (1, 100, '化学学院', 'audience', '面向化学学院', PRIVATE))
        db.execute('INSERT INTO bulletin_colleges VALUES(?,?,?,?,?,?)',
                   (2, 103, '物理学院', 'mentioned', '物理学院讲座', PRIVATE))
        db.executemany('INSERT INTO bulletin_sources VALUES(?,?)', [
            (100, 'https://public.example.edu/lecture'), (101, 'https://another.example.edu/detail'),
            (101, 'file:///private/secret'), (101, 'http://127.0.0.1/private'),
            (101, 'https://user:password@example.edu/private'),
        ])
        html_digest = _sha(docs[0][3])
        poster_path = f'bulletins/{html_digest}/poster.png'
        (uploads / poster_path).write_bytes(PNG)
        asset_evidence = {'width': '1', 'height': 1, 'page': None, 'caption_text': '历史活动海报',
                          'path': PRIVATE, 'bbox': PRIVATE, 'caption_locator': PRIVATE, 'mime': 'image/png'}
        db.execute('INSERT INTO bulletin_assets VALUES(?,?,?,?,?)',
                   (11, 'poster', poster_path, _sha(PNG), _json(asset_evidence)))
        create_date_schema(db)
    return settings


def _load(output):
    manifest = json.loads((output / 'manifest.json').read_bytes())
    catalog = json.loads((output / manifest['files']['catalog']).read_bytes())
    issues = json.loads((output / manifest['files']['issues']).read_bytes())
    search = json.loads((output / manifest['files']['search_documents']).read_bytes())
    details = {}
    for path in {item['detail_path'] for item in catalog['items']}:
        details.update(json.loads((output / path).read_bytes()))
    return manifest, catalog, issues, search, details


def _tree(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


def test_public_contract_privacy_readonly_and_all_occurrences(evidence, tmp_path):
    output = tmp_path / 'public'
    before = evidence.database_path.read_bytes()
    summary = static_export.export_static(output, settings=evidence)
    manifest, catalog, issues, search, details = _load(output)
    assert evidence.database_path.read_bytes() == before
    assert summary['counts'] == manifest['counts'] == {
        'items': 2, 'occurrences': 5, 'issues': 2, 'documents': 3, 'images': 1, 'pdfs': 1}
    assert summary['bytes'] == sum(len(value) for value in _tree(output).values())
    assert summary['output'] == str(output)
    assert set(manifest) == {'schema_version', 'generator', 'data_version', 'generated_at', 'counts', 'files', 'content_rules_version'}
    assert manifest['content_rules_version'] == static_export.RULES_VERSION
    assert manifest['schema_version'] == 2 and manifest['generator'] == 'webnews-static-v2'
    assert not (output / manifest['files']['search_index']).exists()  # Not a fake built index.
    assert len(search) == 5
    public_occurrences = [row for detail in details.values() for row in detail['occurrences']]
    assert {row['id']: row['body_text'] for row in public_occurrences} == {
        row['id']: row['text'] for row in search}
    assert {row['id'] for row in catalog['occurrences']} == {row['id'] for row in search}
    assert sum(issue['occurrence_count'] for issue in issues) == 5
    assert sum(len(detail['occurrences']) for detail in details.values()) == 5
    assert all(len(detail['occurrences']) == item['occurrence_count']
               for item in catalog['items'] for detail in [details[item['id']]])
    for content in _tree(output).values():
        assert PRIVATE.encode() not in content
        assert str(evidence.data_dir).encode() not in content
        assert b'raw_html' not in content and b'raw_section_html' not in content
        assert b'file:///private' not in content and b'user:password' not in content
    documents = [document for issue in issues for document in issue['documents']]
    assert all(set(document) == {'id', 'format', 'filename', 'claimed_issue_date', 'issue_date',
                                  'pdf_path', 'source_urls', 'warnings'} for document in documents)
    pdf = next(document for document in documents if document['format'] == 'pdf')
    assert (output / pdf['pdf_path']).read_bytes() == PDF
    assert all(document['pdf_path'] is None for document in documents if document['format'] == 'html')
    images = [image for row in public_occurrences for image in row['images']]
    assert len(images) == 1 and (output / images[0]['path']).read_bytes() == PNG
    assert images[0]['mime'] == 'image/png' and images[0]['width'] == 1
    assert images[0]['caption'] == '历史活动海报'
    assert any('image_not_acquired' in document['warnings'] for document in documents)
    pdf_occurrence = next(row for row in public_occurrences if row['kind'] == 'notice')
    assert pdf_occurrence['locator'] == {'page': 3}
    assert pdf_occurrence['table_cells'] == [{'header': '内容', 'text': '原表文字'}]
    catalog_keys = {'id', 'item_id', 'issue_id', 'document_id', 'kind', 'title', 'category', 'preview',
                    'issue_date', 'source_urls', 'source_hosts', 'colleges', 'dates', 'expiry_date', 'image_count'}
    assert all(set(row) == catalog_keys for row in catalog['occurrences'])
    assert all(set(row) == catalog_keys | {'body_text', 'college_mentions', 'images', 'locator', 'table_cells', 'date_review', 'original_dates', 'primary_images'}
               for row in public_occurrences)


def test_linked_evidence_dates_identity_and_selection(evidence, tmp_path):
    output = tmp_path / 'public'
    static_export.export_static(output, settings=evidence)
    _, catalog, issues, _, details = _load(output)
    lecture_id = 'item-' + _sha('public-source:讲座'.encode())[:24]
    detail = details[lecture_id]
    assert detail['latest_occurrence_id'] == detail['occurrences'][0]['id']
    rows = detail['occurrences']
    assert [row['kind'] for row in rows] == ['detail', 'summary', 'detail', 'summary']
    assert [row['issue_date'] for row in rows] == ['2026-10-14', '2026-10-14', '2026-10-13', '2026-10-13']
    older = [row for row in rows if row['issue_date'] == '2026-10-13']
    newer = [row for row in rows if row['issue_date'] == '2026-10-14']
    assert all(row['expiry_date'] == '2026-10-30' for row in older)
    assert all(row['colleges'] == ['化学学院'] for row in older)
    assert all(row['expiry_date'] is None for row in newer)  # Never borrow prior issue's deadline.
    assert all(row['colleges'] == ['物理学院'] for row in newer)
    assert all({d['resolution'] for d in row['dates']} == {'year_unknown', 'invalid', 'ambiguous', 'explicit'}
               for row in newer)
    assert all(d['normalized_date'] is None for row in newer for d in row['dates']
               if d['resolution'] != 'explicit')
    assert any(d['raw_text'] == '2026-10-20至2026-10-22' for d in newer[0]['dates'])
    assert any(d['raw_text'] == '2026-13-99' for d in newer[0]['dates'])
    assert older[0]['source_urls'] == older[1]['source_urls'] == [
        'https://another.example.edu/detail', 'https://bulletin.example.edu/issue/11',
        'https://public.example.edu/lecture']
    assert all('https://public.example.edu/lecture' not in row['source_urls'] for row in newer)
    assert older[1]['table_cells'] == [{'header': '活动', 'text': '科学讲座'},
                                      {'header': '截止时间', 'text': '2026-10-30'}]
    digest = _sql(evidence, 'SELECT source_sha256 FROM bulletin_documents WHERE id=13')[0][0]
    assert newer[0]['id'] == 'occ-' + _sha((digest + ':section-1').encode())[:24]
    assert newer[0]['document_id'] == digest
    assert issues[0]['id'] == 'issue-' + _sha(b'issue:2026-10-14')[:24]
    assert catalog['occurrences'][0]['id'] == newer[0]['id']


def test_reader_rejects_article_title_categories_without_rewriting_evidence(evidence, tmp_path):
    _sql(evidence, 'UPDATE bulletin_occurrences SET category=? WHERE id=101', ('具体通知标题',))
    before = evidence.database_path.read_bytes()
    output = tmp_path / 'public'
    static_export.export_static(output, settings=evidence)
    _, catalog, _, _, details = _load(output)
    assert evidence.database_path.read_bytes() == before
    affected = [row for row in catalog['occurrences'] if row['title'] == '科学讲座' and row['kind'] == 'detail']
    assert affected[0]['category'] is None
    assert next(row for item in details.values() for row in item['occurrences']
                if row['id'] == affected[0]['id'])['category'] is None
    assert next(row for row in catalog['occurrences'] if row['kind'] == 'summary')['category'] == '讲座'
    assert _sql(evidence, 'SELECT category FROM bulletin_occurrences WHERE id=101') == [('具体通知标题',)]


@pytest.mark.parametrize('damage', ['missing', 'hash', 'symlink'])
def test_unverifiable_html_never_replaces_category_dataset(evidence, tmp_path, damage):
    output = tmp_path / 'public'
    static_export.export_static(output, settings=evidence)
    before = _tree(output)
    original = evidence.upload_dir / _sql(evidence, 'SELECT original_path FROM bulletin_documents WHERE id=11')[0][0]
    if damage == 'missing':
        original.unlink()
    elif damage == 'hash':
        original.write_bytes(b'<html>changed category evidence</html>')
    else:
        target = tmp_path / 'outside.html'
        target.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(target)
    with pytest.raises(static_export.StaticExportError):
        static_export.export_static(output, settings=evidence, replace=True)
    assert _tree(output) == before


def test_deterministic_version_ignores_private_metadata_and_timestamp(evidence, tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    one = static_export.export_static(first, settings=evidence)
    _sql(evidence, 'UPDATE bulletin_documents SET actor=?,parser_json=?,text=?', ('different private actor',) * 3)
    _sql(evidence, 'UPDATE bulletin_items SET identity_basis=?', ('different private identity basis',))
    two = static_export.export_static(second, settings=evidence)
    assert one['data_version'] == two['data_version']
    left, right = _tree(first), _tree(second)
    left.pop('manifest.json')
    right.pop('manifest.json')
    assert left == right
    assert _load(first)[0]['generated_at'] != _load(second)[0]['generated_at']
    _sql(evidence, 'UPDATE bulletin_occurrences SET body_text=? WHERE id=101', ('公开正文实际变更',))
    changed = static_export.export_static(tmp_path / 'changed', settings=evidence)
    assert changed['data_version'] != one['data_version']
    assert set(_load(first)[4]) == set(_load(tmp_path / 'changed')[4])


@pytest.mark.parametrize('damage', ['escape', 'absolute', 'symlink', 'parent_symlink', 'hash',
                                    'missing', 'non_image', 'pdf_hash', 'non_pdf'])
def test_invalid_stored_assets_never_replace_previous_dataset(evidence, tmp_path, damage):
    output = tmp_path / 'public'
    static_export.export_static(output, settings=evidence)
    before = _tree(output)
    relative, digest = _sql(evidence, 'SELECT private_path,sha256 FROM bulletin_assets')[0]
    asset = evidence.upload_dir / relative
    if damage == 'escape':
        _sql(evidence, 'UPDATE bulletin_assets SET private_path=?', ('../outside.png',))
    elif damage == 'absolute':
        _sql(evidence, 'UPDATE bulletin_assets SET private_path=?', (str(asset),))
    elif damage == 'symlink':
        outside = tmp_path / 'outside.png'
        outside.write_bytes(PNG)
        asset.unlink()
        asset.symlink_to(outside)
    elif damage == 'parent_symlink':
        nested = asset.parent / 'nested'
        nested.symlink_to(tmp_path, target_is_directory=True)
        (tmp_path / 'outside.png').write_bytes(PNG)
        _sql(evidence, 'UPDATE bulletin_assets SET private_path=?',
             (str(Path(relative).parent / 'nested' / 'outside.png'),))
    elif damage == 'hash':
        _sql(evidence, 'UPDATE bulletin_assets SET sha256=?', ('0' * 64,))
    elif damage == 'missing':
        asset.unlink()
    elif damage == 'non_image':
        asset.write_bytes(b'<svg onload="steal()"></svg>')
        _sql(evidence, 'UPDATE bulletin_assets SET sha256=?', (_sha(asset.read_bytes()),))
    elif damage == 'pdf_hash':
        original = _sql(evidence, "SELECT original_path FROM bulletin_documents WHERE format='pdf'")[0][0]
        (evidence.upload_dir / original).write_bytes(PDF + b'changed')
    elif damage == 'non_pdf':
        original = _sql(evidence, "SELECT original_path FROM bulletin_documents WHERE format='pdf'")[0][0]
        (evidence.upload_dir / original).write_bytes(b'<html>not a PDF</html>')
    with pytest.raises(static_export.StaticExportError) as error:
        static_export.export_static(output, settings=evidence, replace=True)
    assert str(evidence.data_dir) not in str(error.value)
    assert _tree(output) == before
    assert list(tmp_path.glob('.public-static-*')) == []
    fresh = tmp_path / 'fresh'
    with pytest.raises(static_export.StaticExportError):
        static_export.export_static(fresh, settings=evidence)
    assert not fresh.exists()


def test_replacement_is_guarded_preserves_unrelated_files_and_built_index(evidence, tmp_path):
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    keep = unrelated / 'keep.txt'
    keep.write_text('must never be deleted')
    with pytest.raises(static_export.StaticExportError):
        static_export.export_static(unrelated, settings=evidence, replace=True)
    assert keep.read_text() == 'must never be deleted'
    output = tmp_path / 'public'
    first = static_export.export_static(output, settings=evidence)
    with pytest.raises(static_export.StaticExportError):
        static_export.export_static(output, settings=evidence)
    manifest = _load(output)[0]
    index = output / manifest['files']['search_index']
    index.write_text('already built search index')
    (output / 'unrelated.txt').write_text('keep this too')
    same = static_export.export_static(output, settings=evidence, replace=True)
    assert same['data_version'] == first['data_version']
    assert index.read_text() == 'already built search index'
    _sql(evidence, 'UPDATE bulletin_occurrences SET body_text=? WHERE id=101', ('真正修改的正文',))
    changed = static_export.export_static(output, settings=evidence, replace=True)
    assert changed['data_version'] != first['data_version']
    assert index.read_text() == 'already built search index'
    assert (output / 'unrelated.txt').read_text() == 'keep this too'
    assert (output / manifest['files']['catalog']).is_file()  # Old shareable URLs remain valid.
    assert not (output / _load(output)[0]['files']['search_index']).exists()


def test_manifest_commit_failure_leaves_old_dataset_intact(evidence, tmp_path, monkeypatch):
    output = tmp_path / 'public'
    static_export.export_static(output, settings=evidence)
    before = _tree(output)
    _sql(evidence, 'UPDATE bulletin_occurrences SET body_text=? WHERE id=101', ('将被拒绝发布的正文',))
    original_replace = static_export.os.replace

    def fail_manifest(source, target):
        if Path(target) == output / 'manifest.json':
            raise OSError('simulated publish failure')
        return original_replace(source, target)

    monkeypatch.setattr(static_export.os, 'replace', fail_manifest)
    with pytest.raises(static_export.StaticExportError):
        static_export.export_static(output, settings=evidence, replace=True)
    assert _tree(output) == before
    assert list(tmp_path.glob('.public-static-*')) == []


@pytest.mark.parametrize('target', ['private_child', 'private_root', 'private_ancestor', 'uploads_child'])
def test_private_output_roots_are_rejected_before_any_write(evidence, tmp_path, target):
    outputs = {'private_child': evidence.data_dir / 'public', 'private_root': evidence.data_dir,
               'private_ancestor': tmp_path, 'uploads_child': evidence.upload_dir / 'public'}
    before = _tree(evidence.data_dir)
    with pytest.raises(static_export.StaticExportError):
        static_export.export_static(outputs[target], settings=evidence, replace=True)
    assert _tree(evidence.data_dir) == before


def test_symlinked_output_and_forged_manifest_are_rejected(evidence, tmp_path):
    output = tmp_path / 'public'
    static_export.export_static(output, settings=evidence)
    alias = tmp_path / 'alias'
    alias.symlink_to(output, target_is_directory=True)
    with pytest.raises(static_export.StaticExportError):
        static_export.export_static(alias, settings=evidence, replace=True)
    forged = tmp_path / 'forged'
    forged.mkdir()
    manifest = _load(output)[0]
    manifest['files']['catalog'] = '../private/database.db'
    (forged / 'manifest.json').write_text(_json(manifest))
    (forged / 'keep.txt').write_text('not owned by generator')
    before = _tree(forged)
    with pytest.raises(static_export.StaticExportError):
        static_export.export_static(forged, settings=evidence, replace=True)
    assert _tree(forged) == before


def test_existing_version_symlink_is_never_followed(evidence, tmp_path):
    output = tmp_path / 'public'
    static_export.export_static(output, settings=evidence)
    manifest = _load(output)[0]
    catalog = output / manifest['files']['catalog']
    untouched = tmp_path / 'untouched.json'
    untouched.write_bytes(catalog.read_bytes())
    catalog.unlink()
    catalog.symlink_to(untouched)
    before = untouched.read_bytes()
    with pytest.raises(static_export.StaticExportError):
        static_export.export_static(output, settings=evidence, replace=True)
    assert untouched.read_bytes() == before


def test_unresolved_normalized_year_is_not_silently_published(evidence, tmp_path):
    _sql(evidence, "UPDATE bulletin_dates SET normalized_date='2026-10-21' WHERE id=3")
    with pytest.raises(static_export.StaticExportError):
        static_export.export_static(tmp_path / 'public', settings=evidence)
    assert not (tmp_path / 'public').exists()


def test_database_access_is_readonly_and_bulletin_only(evidence, tmp_path, monkeypatch):
    original_connect = sqlite3.connect
    reads = set()
    writes = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
              sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE,
              sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_CREATE_INDEX}
    forbidden_columns = {'raw_item_json', 'parser_json', 'raw_artifact_path',
                         'normalized_path', 'actor', 'imported_at', 'identity_basis', 'text'}

    def authorize(action, first, second, database, trigger):
        if action in writes and first != 'sqlite_master':
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ:
            reads.add((first, second))
            projection_internal = first in {'json_each', 'sqlite_master'}
            if not projection_internal and (not first.startswith('bulletin_') or second in forbidden_columns):
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def connect(*args, **kwargs):
        assert kwargs['uri'] is True and '?mode=ro' in args[0]
        db = original_connect(*args, **kwargs)
        db.set_authorizer(authorize)
        return db

    monkeypatch.setattr(static_export.sqlite3, 'connect', connect)
    static_export.export_static(tmp_path / 'public', settings=evidence)
    assert ('bulletin_occurrences', 'body_text') in reads
    assert not any(table in {'users', 'entries', 'source_documents', 'api_keys',
                             'bulletin_document_provenance'} for table, column in reads)


def test_asset_bytes_participate_in_version_and_determine_safe_mime(evidence, tmp_path):
    first = static_export.export_static(tmp_path / 'first', settings=evidence)
    gif = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
    relative = _sql(evidence, 'SELECT private_path FROM bulletin_assets')[0][0]
    (evidence.upload_dir / relative).write_bytes(gif)
    _sql(evidence, 'UPDATE bulletin_assets SET sha256=?', (_sha(gif),))
    second = static_export.export_static(tmp_path / 'second', settings=evidence)
    assert first['data_version'] != second['data_version']
    images = [image for detail in _load(tmp_path / 'second')[4].values()
              for occurrence in detail['occurrences'] for image in occurrence['images']]
    assert images[0]['mime'] == 'image/gif' and images[0]['path'].endswith('.gif')
    assert (tmp_path / 'second' / images[0]['path']).read_bytes() == gif


@pytest.mark.parametrize('status,expected_expiry', [('resolved', '2026-10-21'), ('recurring_no_end', None), ('uncertain', None)])
def test_reviewed_contextual_dates_keep_basis_without_private_review_metadata(evidence, tmp_path, status, expected_expiry):
    with closing(sqlite3.connect(evidence.database_path)) as db, db:
        db.row_factory = sqlite3.Row
        occurrence = dict(db.execute('''SELECT o.id,o.local_key,o.title,o.body_text,d.source_sha256,i.issue_date
            FROM bulletin_occurrences o JOIN bulletin_documents d ON d.id=o.document_id
            JOIN bulletin_issues i ON i.id=d.issue_id WHERE o.id=102''').fetchone())
        claim = {'kind': 'event', 'raw_text': '10月21日', 'normalized_date': '2026-10-21',
                 'resolution': 'contextual', 'evidence': '原文10月21日；依据原刊2026-10-13补年。', 'locator': None}
        record_revision(db, occurrence, [], [claim], rules_version=PRIVATE, origin='luna_review',
                        reviewer=PRIVATE, status=status, reason='依据原刊与原文日期复核；长期服务起点不作结束日。')
    output = tmp_path / 'reviewed'
    static_export.export_static(output, settings=evidence)
    manifest, catalog, _, _, details = _load(output)
    public = next(row for row in catalog['occurrences'] if row['title'] == '原始PDF通知')
    assert public['expiry_date'] == expected_expiry
    assert public['dates'] == [{'kind': 'event', 'normalized_date': '2026-10-21', 'resolution': 'contextual'}]
    detail = next(row for item in details.values() for row in item['occurrences'] if row['id'] == public['id'])
    assert detail['dates'][0]['evidence'] == claim['evidence']
    assert detail['date_review']['status'] == status
    assert detail['date_review']['reason'] == '依据原刊与原文日期复核；长期服务起点不作结束日。'
    assert detail['date_review']['source_review']['status'] == 'secondary_only'
    assert detail['date_review']['source_review']['sources'] == []
    assert 'date_review' not in public
    assert all(PRIVATE.encode() not in content for name, content in _tree(output).items() if name.endswith('.json'))


def test_version_one_dataset_is_a_guarded_migration_target_not_a_reader_contract(evidence, tmp_path):
    output = tmp_path / 'public'
    static_export.export_static(output, settings=evidence)
    manifest, *_ = _load(output)
    manifest.update(schema_version=1, generator='webnews-static-v1')
    (output / 'manifest.json').write_text(_json(manifest), encoding='utf-8')
    static_export.export_static(output, settings=evidence, replace=True)
    assert _load(output)[0]['schema_version'] == 2

def test_reviewed_activity_groups_share_detail_but_retain_original_occurrence_ids(evidence, tmp_path):
    before = static_export.export_static(tmp_path / 'before', settings=evidence)
    original_catalog = _load(tmp_path / 'before')[1]
    originals = {row['title']: row['item_id'] for row in original_catalog['occurrences']}
    canonical = originals['科学讲座']
    alias = originals['原始PDF通知']
    with closing(sqlite3.connect(evidence.database_path)) as db, db:
        create_activity_schema(db)
        db.execute('''INSERT INTO bulletin_activity_groups
            (id,canonical_item_id,decision_sha256,input_sha256,reviewer,reason,evidence_json,created_at)
            VALUES(1,7,?,?,?,?,?,?)''', ('a' * 64, 'b' * 64, PRIVATE, PRIVATE, PRIVATE, PRIVATE))
        db.executemany('INSERT INTO bulletin_activity_members VALUES(1,?)', [(7,), (8,)])
    after = static_export.export_static(tmp_path / 'after', settings=evidence)
    _, catalog, _, _, details = _load(tmp_path / 'after')
    assert before['counts']['items'] == 2 and after['counts']['items'] == 1
    assert {row['id'] for row in catalog['occurrences']} == {row['id'] for row in original_catalog['occurrences']}
    assert {row['item_id'] for row in catalog['occurrences']} == {canonical}
    assert catalog['items'][0]['aliases'] == [alias]
    assert {row['title'] for row in details[canonical]['occurrences']} == set(originals)
    assert all(PRIVATE.encode() not in content for name, content in _tree(tmp_path / 'after').items() if name.endswith('.json'))


def test_later_automatic_revision_cannot_replace_ai_review_metadata(evidence, tmp_path):
    with closing(sqlite3.connect(evidence.database_path)) as db, db:
        db.row_factory = sqlite3.Row
        occurrence = dict(db.execute('''SELECT o.id,o.local_key,o.title,o.body_text,d.source_sha256,i.issue_date
            FROM bulletin_occurrences o JOIN bulletin_documents d ON d.id=o.document_id
            JOIN bulletin_issues i ON i.id=d.issue_id WHERE o.id=102''').fetchone())
        claim = {'kind': 'event', 'raw_text': '10月21日', 'normalized_date': '2026-10-21',
                 'resolution': 'contextual', 'evidence': '原文10月21日；依据原刊2026-10-13补年。', 'locator': None}
        ai_reason = '模型判断尚有来源不确定性。'
        record_revision(db, occurrence, [], [claim], rules_version='ai-review', origin='luna_review',
                        reviewer='luna:test', status='uncertain', reason=ai_reason)
        db.execute('''INSERT INTO bulletin_date_revisions
            (occurrence_id,input_sha256,revision_sha256,rules_version,origin,reviewer,status,reason,source_review_json,created_at)
            SELECT occurrence_id,input_sha256,?,rules_version,'automatic','automatic','resolved',?,source_review_json,created_at
            FROM bulletin_date_revisions WHERE occurrence_id=?''',
            ('a' * 64, '旧自动规则宣称已经确定。', occurrence['id']))
    output = tmp_path / 'reviewed'
    static_export.export_static(output, settings=evidence)
    _, _, _, _, details = _load(output)
    occurrence = next(row for detail in details.values() for row in detail['occurrences']
                      if row['date_review'] is not None)
    assert occurrence['date_review']['status'] == 'uncertain'
    assert occurrence['date_review']['reason'] == ai_reason
    assert occurrence['dates'][0]['normalized_date'] == '2026-10-21'
    assert occurrence['expiry_date'] is None


def _install_content_review(settings, *, rules_version=None, bulletin_deadline=False):
    import io
    from PIL import Image, PngImagePlugin
    from nannanews.bulletin_date_repair import date_snapshot

    text = '科学讲座将于2026年10月20日19:00在报告厅举行。活动面向化学学院。'
    url = 'https://another.example.edu/detail'
    citation = {'url': url, 'text': text, 'image_index': None}
    unknown = {'value': None, 'evidence': []}
    field = lambda value: {'value': value, 'evidence': [dict(citation)]}
    activity = {
        'title': field('科学讲座'), 'summary': field('面向化学学院的科学讲座。'),
        'content': field('在报告厅举行科学讲座，活动面向化学学院。'),
        'venue': field('报告厅'), 'organizer': dict(unknown), 'audience': field('化学学院'),
        'participation': dict(unknown), 'time': field('19:00'),
        'colleges': [{'name': '化学学院', 'relation': 'audience', 'evidence': dict(citation)}],
        'image_indices': [0],
    }
    buffer = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text('private-capture-metadata', PRIVATE)
    with Image.new('RGB', (24, 16), 'navy') as image:
        image.save(buffer, format='PNG', pnginfo=metadata)
    image_bytes = buffer.getvalue()
    raw = (text + '\n' + PRIVATE).encode()
    source = {'url': url, 'resolved_url': url, 'title': '科学讲座', 'text': text,
        'raw_response_base64': base64.b64encode(raw).decode(), 'raw_response_digest': _sha(raw),
        'fetched_at': '2026-10-14T10:00:00+00:00', 'published_date': '2026-10-10',
        'raw_date_text': '2026年10月10日', 'source_type': 'html', 'content_type': 'text/plain',
        'quotes': [text], 'quote_evidence': [{'text': text, 'image_index': None}],
        'images': [{'image_index': 0, 'url': 'https://another.example.edu/poster.png',
                    'sha256': _sha(image_bytes), 'mime': 'image/png',
                    'data': base64.b64encode(image_bytes).decode()}]}
    claim = {'kind': 'event', 'raw_text': '2026年10月20日', 'normalized_date': '2026-10-20',
        'resolution': 'explicit', 'evidence': '同一场讲座的官网原文明示完整日期。', 'locator': None}
    claims = [claim]
    if bulletin_deadline:
        claims.append({'kind': 'deadline', 'raw_text': '2026-10-30', 'normalized_date': '2026-10-30',
            'resolution': 'explicit', 'evidence': '刊物清单所列日期，原文未确认其截止语义。', 'locator': None})
    with closing(sqlite3.connect(settings.database_path)) as db, db:
        db.row_factory = sqlite3.Row
        if bulletin_deadline:
            db.execute("UPDATE bulletin_occurrences SET body_text=body_text || '\n刊物清单截止时间：2026-10-30' WHERE id=101")
        rows, originals, _ = date_snapshot(db)
        occurrence = next(row for row in rows if row['id'] == 101)
        record_revision(db, occurrence, originals[101], claims,
            rules_version=rules_version or static_export.RULES_VERSION,
            origin='luna_review', reviewer='luna:test-model', status='resolved',
            reason='已对照同一活动的官网内容、地点及日期。', source_review={
                'status': 'source_verified', 'reason': '官网中的主体与场次匹配。',
                'sources': [source], 'attempts': [], 'activity': activity})
    return source


def test_verified_activity_exports_own_facts_dates_and_inert_selected_images(evidence, tmp_path):
    import io
    from PIL import Image

    source = _install_content_review(evidence)
    before = evidence.database_path.read_bytes()
    output = tmp_path / 'confirmed-public'
    static_export.export_static(output, settings=evidence)
    assert evidence.database_path.read_bytes() == before
    manifest, _, _, _, details = _load(output)
    occurrence = next(row for item in details.values() for row in item['occurrences']
                      if row['body_text'] == '科学讲座全文：所有历史正文')
    assert [(claim['kind'], claim['normalized_date']) for claim in occurrence['date_review']['claims']] == [
        ('event', '2026-10-20')]
    assert occurrence['date_review']['claims'][0]['literal_source'] == 'primary'
    assert any(claim['kind'] == 'deadline' for claim in occurrence['dates'])
    assert occurrence['original_dates'] == [{'kind': 'event', 'raw_text': '2026年10月20日',
        'normalized_date': '2026-10-20', 'resolution': 'explicit', 'evidence': '活动：2026年10月20日'}]
    assert len(occurrence['primary_images']) == 1 and manifest['counts']['images'] == 2
    image = occurrence['primary_images'][0]
    assert image['mime'] == 'image/jpeg' and image['source_url'] == 'https://another.example.edu/poster.png'
    preview = (output / image['path']).read_bytes()
    with Image.open(io.BytesIO(preview)) as raster:
        assert raster.size == (24, 16) and raster.format == 'JPEG'
    assert PRIVATE.encode() not in preview
    public_bytes = b'\n'.join(_tree(output).values())
    assert PRIVATE.encode() not in public_bytes
    assert source['raw_response_base64'].encode() not in public_bytes
    assert source['images'][0]['data'].encode() not in public_bytes


def test_legacy_activity_cannot_publish_confirmed_facts_or_source_images(evidence, tmp_path):
    _install_content_review(evidence, rules_version='previous-content-contract')
    output = tmp_path / 'legacy-public'
    static_export.export_static(output, settings=evidence)
    manifest, _, _, _, details = _load(output)
    occurrence = next(row for item in details.values() for row in item['occurrences']
                      if row['body_text'] == '科学讲座全文：所有历史正文')
    assert occurrence['date_review']['source_review']['activity'] is None
    assert occurrence['primary_images'] == [] and manifest['counts']['images'] == 1
    assert 'claims' not in occurrence['date_review']
    assert occurrence['body_text'] == '科学讲座全文：所有历史正文'


def test_empty_ai_date_verdict_keeps_original_dates_as_explicit_reference(evidence, tmp_path):
    from nannanews.bulletin_date_repair import date_snapshot

    with closing(sqlite3.connect(evidence.database_path)) as db, db:
        db.row_factory = sqlite3.Row
        rows, originals, _ = date_snapshot(db)
        row = next(row for row in rows if row['id'] == 102)
        record_revision(db, row, originals[102], [], rules_version='old-date-contract',
            origin='luna_review', reviewer='luna:old', status='unresolved', reason='原文未能读取。')
    output = tmp_path / 'unconfirmed-public'
    static_export.export_static(output, settings=evidence)
    _, _, _, _, details = _load(output)
    occurrence = next(row for item in details.values() for row in item['occurrences']
                      if row['title'] == '原始PDF通知')
    assert occurrence['dates'] == []
    assert occurrence['original_dates'] == [{'kind': 'event', 'raw_text': '10月21日',
        'normalized_date': None, 'resolution': 'year_unknown', 'evidence': '活动：10月21日'}]
    assert occurrence['date_review']['source_review']['activity'] is None



def test_primary_source_verdict_does_not_upgrade_bulletin_only_deadline(evidence, tmp_path):
    _install_content_review(evidence, bulletin_deadline=True)
    output = tmp_path / 'mixed-date-evidence'
    static_export.export_static(output, settings=evidence)
    _, _, _, _, details = _load(output)
    occurrence = next(row for item in details.values() for row in item['occurrences']
                      if row['body_text'].startswith('科学讲座全文：所有历史正文'))
    assert occurrence['date_review']['source_review']['status'] == 'source_verified'
    assert [(claim['kind'], claim['literal_source']) for claim in occurrence['date_review']['claims']] == [
        ('event', 'primary'), ('deadline', 'bulletin')]


def test_mismatched_source_quote_cannot_upgrade_bulletin_date_provenance(evidence, tmp_path):
    from nannanews.bulletin_date_repair import date_snapshot

    source = _install_content_review(evidence)
    with closing(sqlite3.connect(evidence.database_path)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("UPDATE bulletin_occurrences SET body_text=body_text || '\n2026年10月20日' WHERE id=101")
        rows, originals, _ = date_snapshot(db)
        row = next(value for value in rows if value['id'] == 101)
        record_revision(db, row, originals[101], [{'kind': 'event', 'raw_text': '2026年10月20日',
            'normalized_date': '2026-10-20', 'resolution': 'explicit',
            'evidence': '日期文字在刊物中，但链接指向不同活动。', 'locator': None}],
            rules_version=static_export.RULES_VERSION, origin='luna_review', reviewer='luna:test-model',
            status='resolved', reason='仅保留刊物日期，不确认错链原文。', source_review={
                'status': 'source_mismatch', 'reason': '链接中的主体或场次不匹配。',
                'sources': [source], 'attempts': [], 'activity': None})
    output = tmp_path / 'mismatch-date-evidence'
    static_export.export_static(output, settings=evidence)
    _, _, _, _, details = _load(output)
    occurrence = next(row for item in details.values() for row in item['occurrences']
                      if row['body_text'].startswith('科学讲座全文：所有历史正文'))
    assert occurrence['date_review']['source_review']['status'] == 'source_mismatch'
    assert occurrence['date_review']['source_review']['activity'] is None
    assert occurrence['date_review']['claims'][0]['literal_source'] == 'bulletin'



def test_verified_college_filters_use_primary_facts_but_keep_original_mentions(evidence, tmp_path):
    with closing(sqlite3.connect(evidence.database_path)) as db, db:
        db.execute('INSERT INTO bulletin_colleges VALUES(?,?,?,?,?,?)',
                   (99, 101, '未获原文支持的学院', 'mentioned', '原刊提及未获原文支持的学院', '{}'))
    _install_content_review(evidence)
    output = tmp_path / 'verified-college-filter'
    static_export.export_static(output, settings=evidence)
    _, catalog, _, _, details = _load(output)
    detail = next(row for item in details.values() for row in item['occurrences']
                  if row['body_text'] == '科学讲座全文：所有历史正文')
    indexed = next(row for row in catalog['occurrences'] if row['id'] == detail['id'])
    assert indexed['colleges'] == ['化学学院']
    assert {row['name'] for row in detail['college_mentions']} == {'化学学院', '未获原文支持的学院'}

