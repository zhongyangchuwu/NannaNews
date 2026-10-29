"""Offline synthetic SQLite originals, body fidelity, holds and stable identities."""
import base64
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from nannanews import data_foundation_capture as capture
from nannanews.data_foundation import connect_foundation


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
URL = 'https://example.edu/activity/42'
HTML = b'<!doctype html><html>Verification failed, not an image.</html>'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def media(data=PNG, mime='image/png'):
    return {'data': base64.b64encode(data).decode('ascii'), 'digest': sha(data),
            'mime': mime, 'url': 'https://example.edu/poster.png'}


def document(**changes):
    value = {'canonical_url': URL, 'resolved_url': URL, 'title': '合成讲座',
             'text': '公开讲座的合成证据。', 'source_type': 'public_html',
             'content_type': 'text/html', 'images': [], 'media': [],
             'capture_complete': True, 'capture_errors': [], 'pdf_documents': []}
    value.update(changes)
    return value


def database(path, documents):
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE source_documents(id INTEGER PRIMARY KEY,source_url TEXT,digest TEXT,document_json TEXT)')
        originals = []
        for ordinal, value in enumerate(documents, 1):
            raw = value if isinstance(value, bytes) else (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
            # Deliberately NOT the SHA of document_json: the native capture digest
            # is an independent declared identity, not a raw-byte checksum.
            db.execute('INSERT INTO source_documents VALUES(?,?,?,?)',
                       (ordinal, URL, 'native-capture-digest-' + str(ordinal), raw.decode('utf-8')))
            originals.append(raw)
        db.commit()
    return originals


def seed(root, urls):
    with closing(connect_foundation(root)) as db, db:
        db.execute('''INSERT INTO activities(activity_id,title,summary,review_status,record_version,input_sha256)
                      VALUES('existing','Manually reviewed title','Current summary','approved','1','input')''')
        db.execute('''INSERT INTO source_records(source_id,activity_id,source_urls_json,locator_json,parse_status)
                      VALUES('declared','existing',?,'{}','parsed')''', (json.dumps(urls),))


def contents(db, digest):
    return Path(db.execute('SELECT local_path FROM blobs WHERE sha256=?', (digest,)).fetchone()[0]).read_bytes()


def manifests(db):
    return [json.loads(contents(db, row[0])) for row in db.execute("SELECT sha256 FROM blobs WHERE kind='capture_manifest'")]


def evidence_rows(db):
    tables = ('blobs', 'snapshots', 'snapshot_blobs', 'activity_snapshots', 'ingestions', 'ingestion_outputs')
    return {table: sorted(tuple(row) for row in db.execute('SELECT * FROM ' + table)) for table in tables}


def source_guard(path):
    info = path.stat()
    return (path.read_bytes(), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def test_exact_original_http_failed_html_and_image_ordinals_are_preserved(tmp_path):
    root, path = tmp_path / 'foundation', tmp_path / 'native.sqlite3'
    seed(root, [URL])
    raw_http = b'<html>Original raw HTTP\r\n\x00 bytes.</html>'
    malformed = {'data': 'not-base64!', 'digest': 'bad', 'mime': 'image/png'}
    originals = database(path, [document(
        raw_response_base64=base64.b64encode(raw_http).decode('ascii'), raw_response_digest=sha(raw_http),
        media=[media(HTML, 'image/png'), malformed, media()],
        images=['https://example.edu/failure.png', 'https://example.edu/bad.png', 'https://example.edu/poster.png'])])
    before = source_guard(path)
    result = capture.preserve_capture_databases(root, [path])
    assert result['captures'] == 1 and result['snapshots'] == 1
    assert result['held'] == 1 and result['errors'] == 2
    assert result['source_guards_unchanged']
    assert source_guard(path) == before
    with closing(connect_foundation(root)) as db:
        manifest, = manifests(db)
        assert contents(db, manifest['original_document_blob_sha']) == originals[0]
        assert manifest['original_document_blob_sha'] == sha(originals[0])
        assert manifest['source_row']['declared_digest'] == 'native-capture-digest-1'
        assert manifest['source_row']['rowid'] == 1 and manifest['source_row']['id'] == 1
        assert manifest['source_database']['sha256'] == sha(before[0])
        assert contents(db, manifest['raw_http']['blob_sha']) == raw_http
        assert manifest['raw_http']['digest_valid'] is True
        assert contents(db, manifest['media'][0]['blob_sha']) == HTML
        assert manifest['media'][0]['image'] is False
        assert manifest['media'][1]['blob_sha'] is None
        assert manifest['media'][2]['image'] is True
        snapshot = db.execute('SELECT * FROM snapshots').fetchone()
        semantic = json.loads(snapshot['manifest_json'])
        assert [image['ordinal'] for image in semantic['images']] == [2]
        assert contents(db, semantic['images'][0]['sha256']) == PNG
        links = {(row['role'], row['ordinal'], row['blob_sha']) for row in db.execute('SELECT * FROM snapshot_blobs')}
        assert ('original_document', 0, sha(originals[0])) in links
        assert ('raw_http', 0, sha(raw_http)) in links
        assert ('failed_media', 0, sha(HTML)) in links
        assert ('image', 2, sha(PNG)) in links
        assert not any(role == 'image' and digest == sha(HTML) for role, _, digest in links)
        assert db.execute('SELECT kind FROM blobs WHERE sha256=?', (sha(HTML),)).fetchone()[0] != 'source_image'
        assert db.execute('SELECT count(*) FROM activity_snapshots').fetchone()[0] == 1
        assert db.execute('SELECT title FROM activities').fetchone()[0] == 'Manually reviewed title'
        ingestion = db.execute('SELECT * FROM ingestions').fetchone()
        assert ingestion['source_path'] == str(path)
        assert ingestion['status'] == 'held' and ingestion['completed_at'] is None
        assert ingestion['cleaned_at'] is None
        assert db.execute('SELECT count(*) FROM cleanup_log').fetchone()[0] == 0
        first_rows = evidence_rows(db)
    repeated = capture.preserve_capture_databases(root, [path, path])
    assert repeated['files'] == 1 and repeated['captures'] == 1
    with closing(connect_foundation(root)) as db:
        assert evidence_rows(db) == first_rows
    assert source_guard(path) == before


def test_image_only_unknown_url_does_not_invent_activity_or_title_match(tmp_path):
    root, path = tmp_path / 'foundation', tmp_path / 'image-only.sqlite3'
    seed(root, [URL + '/different'])
    database(path, [document(title='Manually reviewed title', text='', media=[media()])])
    before = source_guard(path)
    result = capture.preserve_capture_databases(root, [path])
    assert result['ok'] and result['captures'] == 1 and result['snapshots'] == 1
    with closing(connect_foundation(root)) as db:
        snapshot = db.execute('SELECT * FROM snapshots').fetchone()
        assert snapshot['text_blob_sha'] is None
        assert db.execute('SELECT count(*) FROM activity_snapshots').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM activities').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM reviews').fetchone()[0] == 0
    assert source_guard(path) == before


def test_rejected_html_only_capture_remains_reachable_without_snapshot(tmp_path):
    root, path = tmp_path / 'foundation', tmp_path / 'rejected.sqlite3'
    original, = database(path, [document(title='安全验证', text='', media=[media(HTML, 'text/html')])])
    result = capture.preserve_capture_databases(root, [path])
    assert result['captures'] == 1 and result['snapshots'] == 0 and result['errors'] >= 1
    with closing(connect_foundation(root)) as db:
        manifest, = manifests(db)
        assert manifest['snapshot_id'] is None
        assert contents(db, manifest['original_document_blob_sha']) == original
        assert contents(db, manifest['media'][0]['blob_sha']) == HTML
        output_ids = {row[0] for row in db.execute("SELECT row_id FROM ingestion_outputs WHERE kind='blob'")}
        assert sha(original) in output_ids and sha(HTML) in output_ids
        assert db.execute("SELECT count(*) FROM blobs WHERE kind='source_image'").fetchone()[0] == 0


def test_tampered_body_bytes_preserved_but_never_promoted_to_image(tmp_path):
    root, path = tmp_path / 'foundation', tmp_path / 'tampered.sqlite3'
    bad_image = dict(media(), digest='0' * 64)
    original, = database(path, [document(media=[bad_image], raw_response_base64=base64.b64encode(HTML).decode(),
                                       raw_response_digest='f' * 64)])
    before = source_guard(path)
    result = capture.preserve_capture_databases(root, [path])
    assert not result['ok'] and result['errors'] == 2
    with closing(connect_foundation(root)) as db:
        manifest, = manifests(db)
        assert contents(db, manifest['original_document_blob_sha']) == original
        assert manifest['raw_http']['blob_sha'] == sha(HTML)
        assert manifest['raw_http']['digest_valid'] is False
        assert manifest['media'][0]['blob_sha'] == sha(PNG)
        assert manifest['media'][0]['digest_valid'] is False
        assert db.execute("SELECT count(*) FROM snapshot_blobs WHERE role='image'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM snapshot_blobs WHERE role='failed_media'").fetchone()[0] == 1
    assert source_guard(path) == before


def test_malformed_and_oversize_records_keep_originals_and_continue_rowwise(tmp_path, monkeypatch):
    root, path = tmp_path / 'foundation', tmp_path / 'bounded.sqlite3'
    monkeypatch.setattr(capture, 'MAX_RECORD_BYTES', 600)
    originals = database(path, [b'{ broken JSON\n', document(text='x' * 2000), document()])
    before = source_guard(path)
    result = capture.preserve_capture_databases(root, [path])
    assert result['captures'] == 3 and result['snapshots'] == 1 and result['errors'] == 2
    with closing(connect_foundation(root)) as db:
        records = {value['source_row']['id']: value for value in manifests(db)}
        assert len(records) == 3
        for index, raw in enumerate(originals, 1):
            assert contents(db, records[index]['original_document_blob_sha']) == raw
        assert records[1]['errors'] == [{'code': 'document_json_malformed'}]
        assert records[2]['errors'] == [{'code': 'document_oversize'}]
        assert records[1]['snapshot_id'] is None and records[2]['snapshot_id'] is None
        assert db.execute('SELECT record_count FROM ingestions').fetchone()[0] == 3
    assert source_guard(path) == before


def test_change_after_capture_holds_evidence_and_reports_guard_failure(tmp_path, monkeypatch):
    root, path = tmp_path / 'foundation', tmp_path / 'changing.sqlite3'
    original, = database(path, [document()])
    original_capture = capture._capture

    def change_after_read(source, row, extractor, origin):
        result = original_capture(source, row, extractor, origin)
        with path.open('ab') as stream:
            stream.write(b'changed-after-capture')
        return result

    monkeypatch.setattr(capture, '_capture', change_after_read)
    result = capture.preserve_capture_databases(root, [path])
    assert not result['ok'] and not result['source_guards_unchanged']
    assert result['sources'][0]['errors'][-1]['code'] == 'source_guard_changed'
    with closing(connect_foundation(root)) as db:
        manifest, = manifests(db)
        assert contents(db, manifest['original_document_blob_sha']) == original
        row = db.execute('SELECT * FROM ingestions').fetchone()
        assert row['status'] == 'held' and row['completed_at'] is None
    repeated = capture.preserve_capture_databases(root, [path])
    assert repeated['captures'] == 0 and not repeated['source_guards_unchanged']
    assert repeated['sources'][0]['errors'] == [{'code': 'source_changed'}]


def test_previously_ingested_source_replacement_is_not_reinterpreted(tmp_path):
    root, path = tmp_path / 'foundation', tmp_path / 'replace.sqlite3'
    database(path, [document()])
    capture.preserve_capture_databases(root, [path])
    previous = path.stat()
    replacement = tmp_path / 'replacement.sqlite3'
    replacement.write_bytes(path.read_bytes())
    # Same bytes and mtime still cannot authorize a different source inode.
    import os
    os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    replacement.replace(path)
    result = capture.preserve_capture_databases(root, [path])
    assert not result['ok'] and result['captures'] == 0
    assert result['sources'][0]['errors'] == [{'code': 'source_changed'}]


def test_source_connection_is_readonly_without_transaction_sidecars(tmp_path, monkeypatch):
    root, path = tmp_path / 'foundation', tmp_path / 'readonly.sqlite3'
    database(path, [document()])
    before = source_guard(path)
    original_capture = capture._capture

    def assert_readonly(source, row, extractor, origin):
        assert source.execute('PRAGMA query_only').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            source.execute("UPDATE source_documents SET digest='not-allowed'")
        return original_capture(source, row, extractor, origin)

    monkeypatch.setattr(capture, '_capture', assert_readonly)
    result = capture.preserve_capture_databases(root, [path])
    assert result['ok'] and source_guard(path) == before
    assert not any(path.with_name(path.name + suffix).exists() for suffix in ('-wal', '-journal', '-shm'))


def test_pending_wal_is_held_without_ignoring_uncommitted_evidence(tmp_path):
    root, path = tmp_path / 'foundation', tmp_path / 'pending.sqlite3'
    database(path, [document()])
    sidecar = path.with_name(path.name + '-wal')
    sidecar.write_bytes(b'synthetic pending WAL evidence')
    before, wal = source_guard(path), sidecar.read_bytes()
    result = capture.preserve_capture_databases(root, [path])
    assert not result['ok'] and result['captures'] == 0
    assert result['sources'][0]['errors'] == [{'code': 'source_read_failed'}]
    assert source_guard(path) == before and sidecar.read_bytes() == wal


def test_refuses_foundation_as_input_before_mutating_it(tmp_path):
    root = tmp_path / 'foundation'
    with closing(connect_foundation(root)):
        pass
    path = root / 'foundation.sqlite3'
    before = source_guard(path)
    with pytest.raises(ValueError, match='cannot also be a capture input'):
        capture.preserve_capture_databases(root, [path])
    assert source_guard(path) == before
