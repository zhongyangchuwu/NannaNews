"""Small synthetic evidence exercises persistence, identity and data-loss guards."""
from contextlib import closing
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from nannanews.data_foundation import connect_foundation
from nannanews.data_foundation_extract import extract_paths


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
URL = 'https://example.edu/activity/42'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return path


def write_reviews(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
    return path


def image():
    return {'data': base64.b64encode(PNG).decode(), 'sha256': hashlib.sha256(PNG).hexdigest(),
            'mime': 'image/png', 'url': 'https://example.edu/poster.png', 'image_index': 0}


def source(*, fetched_at='2026-10-01T00:00:00+00:00'):
    transport = b'<html>Discard transport wrapper and scripts, retain readable evidence.</html>'
    return {'url': URL, 'resolved_url': URL, 'title': '公开讲座',
            'text': '公开讲座于2026年10月26日在报告厅举行。', 'published_date': '2026-10-01',
            'fetched_at': fetched_at, 'source_type': 'public_html', 'content_type': 'text/html',
            'images': [image()], 'quotes': ['2026年10月26日'],
            'quote_evidence': [{'text': '2026年10月26日', 'image_index': None}],
            'raw_response_base64': base64.b64encode(transport).decode(),
            'raw_response_digest': hashlib.sha256(transport).hexdigest()}


def review(occurrence='occ-1', **values):
    record = {'occurrence_id': occurrence, 'item_id': 'old-alias', 'local_key': 'legacy-local',
              'input_sha256': 'input-hash', 'related_context_sha256': 'context-hash',
              'rules_version': 'historical-rule', 'reviewer': 'luna:retained-model',
              'status': 'confirmed', 'reason': '日期与原文一致。',
              'claims': [{'kind': 'event', 'raw_text': '2026年10月26日',
                          'normalized_date': '2026-10-26', 'evidence': '原文日期。'}],
              'source_review': {'status': 'source_verified', 'reason': '已核实公开来源。',
                                'activity': {'title': {'value': '公开讲座', 'evidence': [
                                    {'url': URL, 'text': '公开讲座', 'image_index': None}]},
                                    'venue': {'value': '报告厅', 'evidence': []}},
                                'attempts': [], 'sources': [source()]}}
    record.update(values)
    return record


def cache(document=None):
    document = deepcopy(document or source())
    media = []
    for captured in document['images']:
        media.append({'data': captured['data'], 'digest': captured['sha256'],
                      'mime': captured['mime'], 'url': captured['url']})
    document['images'] = [item['url'] for item in media]
    document['canonical_url'] = document.pop('url')
    document['source_published_date'] = document.pop('published_date')
    document.pop('quotes')
    document.pop('quote_evidence')
    document.update(capture_complete=True, capture_errors=[], pdf_documents=[])
    return {'url': URL, 'sha256': 'legacy-capture-digest',
            'capture': {'document': document, 'media': media, 'media_error': None, '_timing': {'elapsed': 1.0}}}


def seed(root):
    db = connect_foundation(root)
    with db:
        db.execute('''INSERT INTO activities(activity_id,title,summary,review_status,record_version,input_sha256)
                      VALUES('canonical-1','Current manually reviewed title','Current summary','approved','1','input')''')
        db.execute("INSERT INTO activity_aliases VALUES('old-alias','canonical-1')")
        for occurrence in ('occ-1', 'occ-2'):
            db.execute('''INSERT INTO source_records
                (source_id,activity_id,occurrence_id,source_urls_json,locator_json,parse_status)
                VALUES(?,'canonical-1',?,'[]','{}','parsed')''', ('source-' + occurrence, occurrence))
    db.close()


def blob_text(db, digest):
    row = db.execute('SELECT local_path FROM blobs WHERE sha256=?', (digest,)).fetchone()
    return Path(row[0]).read_text(encoding='utf-8')


def test_shared_source_and_image_deduplicate_and_timestamp_does_not_change_snapshot(tmp_path):
    root = tmp_path / 'foundation'
    seed(root)
    first = review()
    second = review('occ-2')
    second['source_review']['sources'][0]['fetched_at'] = '2026-10-02T00:00:00+00:00'
    second['source_review']['sources'][0]['images'][0]['image_index'] = 7
    # Distinct reviews and a cache share semantic source content despite different
    # timestamps, receipt-local image indexes and envelope transport metadata.
    reviews_path = write_reviews(tmp_path / 'historical/reviews.jsonl', [first, second, first])
    cache_path = write_json(tmp_path / 'historical/source-cache/example.json', cache())
    before = {path: path.read_bytes() for path in (reviews_path, cache_path)}
    result = extract_paths(root, [reviews_path, cache_path])
    assert result['held'] == 0
    assert result['records'] == 2
    db = connect_foundation(root)
    try:
        assert db.execute('SELECT count(*) FROM reviews').fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM snapshots').fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM blobs WHERE kind='source_image'").fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM review_snapshots').fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM activity_snapshots').fetchone()[0] == 2
        snapshot = db.execute('SELECT * FROM snapshots').fetchone()
        assert snapshot['fetched_at'] == '2026-10-01T00:00:00+00:00'
        assert blob_text(db, snapshot['text_blob_sha']) == source()['text']
        for row in db.execute('SELECT activity_id,fields_json,answer_blob_sha FROM reviews'):
            assert row['activity_id'] == 'canonical-1'
            fields = json.loads(row['fields_json'])
            assert fields['activity']['venue']['value'] == '报告厅'
            assert fields['legacy_refs']['local_key'] == 'legacy-local'
            assert fields['answer_kind'] == 'parsed_result'
            explanation = blob_text(db, row['answer_blob_sha'])
            assert explanation.startswith('Parsed review result (not a verbatim model answer)')
            assert '日期与原文一致' in explanation and '2026-10-26' in explanation
        for row in db.execute('SELECT * FROM ingestions'):
            assert row['status'] == 'completed'
            assert row['snapshot_count'] == 1
            outputs = list(db.execute('SELECT kind,row_id FROM ingestion_outputs WHERE source_path=?', (row['source_path'],)))
            assert any(item['kind'] == 'snapshot' for item in outputs)
            assert sum(item['kind'] == 'blob' for item in outputs) >= 2
        assert db.execute('SELECT title FROM activities').fetchone()[0] == 'Current manually reviewed title'
        contents = '\n'.join(blob_text(db, row[0]) for row in db.execute("SELECT sha256 FROM blobs WHERE mime LIKE 'text/%'"))
        assert 'raw_response_base64' not in contents
        assert 'Discard transport wrapper' not in contents
        assert 'data:image' not in contents
        assert 'raw_response_base64' not in snapshot['manifest_json']
    finally:
        db.close()
    again = extract_paths(root, [reviews_path, cache_path])
    assert again['skipped'] == 2 and again['records'] == 0
    assert {path: path.read_bytes() for path in before} == before


def test_actual_visible_answer_provider_errors_and_unknown_charge_are_retained(tmp_path):
    root = tmp_path / 'foundation'
    answer = '{"status":"confirmed","reason":"Actual visible answer."}'
    wire = {'id': 'response-1', 'model': 'retained-model', 'status': 'completed',
            'output': [{'type': 'reasoning', 'summary': [{'text': 'Never preserve hidden reasoning.'}]},
                       {'type': 'message', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': answer}]}],
            'usage': {'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30}}
    good = write_json(tmp_path / 'historical/response.json', wire)
    bad = write_json(tmp_path / 'historical/error.json', {
        'error': {'code': 'rate_limit', 'message': 'Provider declined the request.'}})
    result = extract_paths(root, [good, bad])
    assert result['records'] == 2 and result['errors'] >= 1
    db = connect_foundation(root)
    try:
        rows = list(db.execute('SELECT * FROM reviews'))
        visible = next(row for row in rows if json.loads(row['fields_json'])['answer_kind'] == 'visible_answer')
        assert blob_text(db, visible['answer_blob_sha']) == answer
        facts = json.loads(visible['fields_json'])['paid_facts']
        assert facts['charge'] == 'unknown'
        assert facts['usage'] == {'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30}
        failure = next(row for row in rows if row['review_id'] != visible['review_id'])
        errors = json.loads(failure['error_json'])
        assert errors[0]['code'] == 'rate_limit'
        assert errors[0]['stage'] == 'model'
        assert 'Provider declined' in errors[0]['reason']
        assert 'hidden reasoning' not in blob_text(db, visible['answer_blob_sha'])
    finally:
        db.close()


@pytest.mark.parametrize('title,url,text', [
    ('统一身份认证平台', 'https://authserver.nju.edu.cn/authserver/login?service=https%3A%2F%2Fexample.edu', '请输入账号密码'),
    ('安全验证', 'https://example.edu/captcha', '请完成验证'),
    ('登录后查看', 'https://example.edu/article', '请先登录后访问'),
])
def test_auth_captcha_pages_keep_short_error_not_background_media(tmp_path, title, url, text):
    root = tmp_path / 'foundation'
    envelope = cache()
    envelope['capture']['document'].update(title=title, resolved_url=url, text=text, capture_complete=True)
    envelope['capture']['media'] *= 6
    path = write_json(tmp_path / 'historical/source-cache/f432.json', envelope)
    original = path.read_bytes()
    result = extract_paths(root, [path])
    assert result['records'] == 1 and result['held'] == 1
    db = connect_foundation(root)
    try:
        snapshot = db.execute('SELECT * FROM snapshots').fetchone()
        assert snapshot['status'] == 'failed' and snapshot['text_blob_sha'] is None
        raw = db.execute("SELECT b.local_path FROM snapshot_blobs s JOIN blobs b ON b.sha256=s.blob_sha WHERE s.role='raw_http'").fetchone()
        assert Path(raw[0]).read_bytes() == base64.b64decode(envelope['capture']['document']['raw_response_base64'])
        assert db.execute("SELECT count(*) FROM blobs WHERE kind='source_image'").fetchone()[0] == 0
        row = db.execute('SELECT * FROM reviews').fetchone()
        errors = json.loads(row['error_json'])
        assert errors[0]['code'] == 'source_unavailable'
        assert any(word in errors[0]['reason'] for word in ('login', 'authentication'))
        assert row['activity_id'] is None
        assert db.execute('SELECT status FROM ingestions').fetchone()[0] == 'held'
        assert len(blob_text(db, row['answer_blob_sha'])) < 3000
    finally:
        db.close()
    assert path.read_bytes() == original


def test_unknown_legacy_identity_remains_unlinked_and_failures_meaningful(tmp_path):
    root = tmp_path / 'foundation'
    item = review('retired-occurrence', item_id='retired-id', local_key='retired-local')
    item['source_review']['sources'] = []
    item['source_review']['attempts'] = [{'url': URL, 'status': 'failed', 'code': 'source_timeout', 'reason': 'Public source timed out.'}]
    path = write_reviews(tmp_path / 'historical/reviews.jsonl', [item])
    extract_paths(root, [path])
    db = connect_foundation(root)
    try:
        row = db.execute('SELECT * FROM reviews').fetchone()
        assert row['activity_id'] is None
        assert row['occurrence_id'] == 'retired-occurrence'
        assert json.loads(row['fields_json'])['legacy_refs']['item_id'] == 'retired-id'
        assert json.loads(row['error_json'])[0] == {'stage': 'source', 'code': 'source_timeout', 'reason': 'Public source timed out.', 'url': URL}
        assert db.execute('SELECT count(*) FROM activities').fetchone()[0] == 0
    finally:
        db.close()


def test_unsupported_malformed_and_changed_sources_are_held(tmp_path):
    root = tmp_path / 'foundation'
    historical = tmp_path / 'historical'
    unknown = write_json(historical / 'unknown.json', {'payload': 'Do not delete this unknown source.'})
    malformed = historical / 'malformed.jsonl'
    write_reviews(malformed, [review()])
    with malformed.open('ab') as stream:
        stream.write(b'{not-json}\n')
    changed = write_reviews(historical / 'changed.jsonl', [review('changed')])
    extract_paths(root, [changed])
    original_digest = hashlib.sha256(changed.read_bytes()).hexdigest()
    with changed.open('ab') as stream:
        stream.write(b'\n')
    before = {path: path.read_bytes() for path in (unknown, malformed, changed)}
    result = extract_paths(root, [unknown, malformed, changed])
    assert result['held'] == 3
    assert {path: path.read_bytes() for path in before} == before
    db = connect_foundation(root)
    try:
        rows = {Path(row['source_path']).name: row for row in db.execute('SELECT * FROM ingestions')}
        assert all(row['status'] == 'held' for row in rows.values())
        assert rows['changed.jsonl']['sha256'] == original_digest
        assert rows['malformed.jsonl']['record_count'] == 1
        assert 'line 2' in rows['malformed.jsonl']['error_json']
        assert 'Unsupported' in rows['unknown.json']['error_json']
        assert db.execute('SELECT count(*) FROM cleanup_log').fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize('mime', ['image/svg+xml', 'image/tiff'])
def test_vector_and_tiff_originals_are_preserved_without_rendering(tmp_path, mime):
    if mime == 'image/svg+xml':
        raw = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"><rect width="1" height="1"/></svg>'
    else:
        import io
        from PIL import Image
        buffer = io.BytesIO()
        Image.new('RGB', (1, 1), 'red').save(buffer, format='TIFF')
        raw = buffer.getvalue()
    envelope = cache()
    digest = hashlib.sha256(raw).hexdigest()
    envelope['capture']['media'] = [{'mime': mime, 'data': base64.b64encode(raw).decode(),
                                     'digest': digest, 'url': 'https://example.edu/original'}]
    path = write_json(tmp_path / 'historical/cache.json', envelope)
    root = tmp_path / 'foundation'
    result = extract_paths(root, [path])
    assert result['held'] == 0 and result['snapshots'] == 1
    db = connect_foundation(root)
    try:
        blob = db.execute('SELECT * FROM blobs WHERE sha256=?', (digest,)).fetchone()
        assert blob['mime'] == mime and Path(blob['local_path']).read_bytes() == raw
        assert db.execute('SELECT role FROM snapshot_blobs WHERE blob_sha=?', (digest,)).fetchone()[0] == 'image'
    finally:
        db.close()


@pytest.mark.parametrize('raw', [
    b'<html><body><svg/></body></html>',
    b'<!DOCTYPE svg [<!ENTITY expanded "payload">]><svg>&expanded;</svg>',
    b'<svg>incomplete',
])
def test_mislabeled_or_unsafe_xml_keeps_its_original_input(tmp_path, raw):
    envelope = cache()
    envelope['capture']['media'] = [{'mime': 'image/svg+xml', 'data': base64.b64encode(raw).decode(),
                                     'digest': hashlib.sha256(raw).hexdigest()}]
    path = write_json(tmp_path / 'historical/cache.json', envelope)
    root = tmp_path / 'foundation'
    result = extract_paths(root, [path])
    assert result['held'] == 1 and path.exists()
    db = connect_foundation(root)
    try:
        digest = hashlib.sha256(raw).hexdigest()
        blob = db.execute('SELECT * FROM blobs WHERE sha256=?', (digest,)).fetchone()
        assert blob['kind'] == 'source_failed_media' and blob['mime'] == 'application/octet-stream'
        assert Path(blob['local_path']).read_bytes() == raw
        assert db.execute("SELECT role FROM snapshot_blobs WHERE blob_sha=?", (digest,)).fetchone()[0] == 'failed_media'
        assert db.execute("SELECT blob_sha FROM snapshot_blobs WHERE role='image'").fetchall() == []
        manifest = json.loads(db.execute('SELECT manifest_json FROM snapshots').fetchone()[0])
        assert manifest['capture_complete'] is False
        assert manifest['failed_media'][0]['digest_verified'] is True
        assert db.execute('SELECT count(*) FROM cleanup_log').fetchone()[0] == 0
    finally:
        db.close()


def test_image_digest_mismatch_holds_source_instead_of_accepting_loss(tmp_path):
    root = tmp_path / 'foundation'
    envelope = cache()
    envelope['capture']['media'][0]['digest'] = '0' * 64
    path = write_json(tmp_path / 'historical/cache.json', envelope)
    result = extract_paths(root, [path])
    assert result['held'] == 1 and path.exists()
    db = connect_foundation(root)
    try:
        manifest = json.loads(db.execute('SELECT manifest_json FROM snapshots').fetchone()[0])
        failure = manifest['failed_media'][0]
        assert failure['declared_sha256'] == '0' * 64 and failure['digest_verified'] is False
        assert failure['sha256'] == hashlib.sha256(base64.b64decode(envelope['capture']['media'][0]['data'])).hexdigest()
        assert db.execute('SELECT kind FROM blobs WHERE sha256=?', (failure['sha256'],)).fetchone()[0] == 'source_failed_media'
        assert db.execute("SELECT blob_sha FROM snapshot_blobs WHERE role='image'").fetchall() == []
        errors = json.loads(db.execute('SELECT error_json FROM ingestions').fetchone()[0])
        assert 'failed_media' in {error['code'] for error in errors}
    finally:
        db.close()


def test_generic_extraction_refuses_sqlite_without_hashing_or_touching_wal(tmp_path, monkeypatch):
    from nannanews import data_foundation_extract as extraction

    root, path = tmp_path / 'foundation', tmp_path / 'legacy.sqlite3'
    with closing(sqlite3.connect(path)) as source_db:
        assert source_db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        source_db.execute('CREATE TABLE historical_attempts(request_sha256 TEXT,state TEXT)')
        source_db.execute('INSERT INTO historical_attempts VALUES(?,?)', ('paid-wal-key', 'reserved'))
        source_db.commit()
        paths = [path, path.with_name(path.name + '-wal'), path.with_name(path.name + '-shm')]
        def guards():
            return [(item.stat().st_ino, item.stat().st_size, item.stat().st_mtime_ns,
                     item.stat().st_ctime_ns, item.read_bytes()) for item in paths]
        before = guards()
        def forbidden_hash(*args):
            pytest.fail('Generic extraction must not hash a SQLite ledger')
        monkeypatch.setattr(extraction, '_fingerprint', forbidden_hash)
        result = extract_paths(root, [path])
        assert result['held'] == 1 and result['records'] == 0
        assert guards() == before
        with sqlite3.connect(root / 'foundation.sqlite3') as db:
            assert db.execute('SELECT request_key FROM attempts').fetchall() == []
            assert db.execute('SELECT review_id FROM reviews').fetchall() == []


def test_wire_receipt_checks_response_digest_and_preserves_visible_text(tmp_path):
    root = tmp_path / 'foundation'
    directory = tmp_path / 'wire-history'
    directory.mkdir()
    response = {'id': 'wire-response', 'model': 'old-model', 'choices': [
        {'message': {'role': 'assistant', 'content': 'The actual retained assistant answer.'}, 'finish_reason': 'stop'}]}
    response_path = write_json(directory / 'response.bin', response)
    receipt = {'version': 'model-wire-audit-v1', 'phase': 'completed', 'created_at': '2026-10-01',
               'configured_model': 'old-model', 'request': {'sha256': 'wire-request-key'},
               'response': {'bytes': response_path.stat().st_size, 'sha256': hashlib.sha256(response_path.read_bytes()).hexdigest(), 'complete': True},
               'context': {'logical_request_sha256': 'logical-paid-key', 'rules_version': 'rules'}}
    receipt_path = write_json(directory / 'receipt.json', receipt)
    result = extract_paths(root, [receipt_path])
    assert result['records'] == 1 and result['held'] == 1
    db = connect_foundation(root)
    try:
        row = db.execute('SELECT * FROM reviews').fetchone()
        assert blob_text(db, row['answer_blob_sha']) == 'The actual retained assistant answer.'
        assert db.execute('SELECT request_key FROM attempts').fetchone()[0] == 'logical-paid-key'
    finally:
        db.close()
    # A separately corrupt receipt cannot turn an unbound response into accepted
    # evidence or a deletable input.
    receipt['response']['sha256'] = '0' * 64
    bad_directory = tmp_path / 'bad-wire-history'
    bad_directory.mkdir()
    shutil.copy2(response_path, bad_directory / 'response.bin')
    bad = write_json(bad_directory / 'receipt.json', receipt)
    result = extract_paths(root, [bad])
    assert result['held'] == 1 and result['records'] == 0
    assert receipt_path.exists() and response_path.exists()


@pytest.mark.parametrize(('raw_response', 'complete', 'error_code'), [
    (b'{"choices":[{"message":{"content":"Actual retained answer"}}]}', True, None),
    (b'{"choices":[', False, 'incomplete_response'),
    (b'provider failure, not JSON', True, 'malformed_response'),
])
def test_exact_wire_is_available_locally_before_archive(tmp_path, raw_response, complete, error_code):
    from nannanews.model_audit import ModelAudit

    payload = {'model': 'synthetic-model', 'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'The exact prepared request, not a reconstructed source.'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(PNG).decode()}}]}]}
    audit = ModelAudit(tmp_path / 'wire', payload, request_limit=4096, response_limit=4096,
                       context={'item_id': 'new-item', 'activity_id': 'new-activity',
                                'occurrence_id': 'new-occurrence', 'source_id': 'new-source',
                                'logical_request_sha256': 'exact-paid-key'})
    audit.append(raw_response)
    audit.preserve_response(complete=complete)
    audit.save('completed' if complete else 'failed')
    audit.close()
    originals = {name: (audit.directory / name).read_bytes() for name in ('request.json', 'response.bin', 'receipt.json')}
    root = tmp_path / 'foundation'
    result = extract_paths(root, [audit.directory / 'receipt.json'])
    assert result['records'] == 1 and result['held'] == 1
    with sqlite3.connect(root / 'foundation.sqlite3') as db:
        fields_json, error_json, activity_id = db.execute(
            'SELECT fields_json,error_json,activity_id FROM reviews').fetchone()
        blob_paths = dict(db.execute('SELECT sha256,local_path FROM blobs'))
    assert activity_id is None  # Registry identity is retained, never invented in the catalog.
    fields, errors = json.loads(fields_json), json.loads(error_json)
    assert fields['legacy_refs']['activity_id'] == 'new-activity'
    assert fields['legacy_refs']['source_id'] == 'new-source'
    for artifact in ('request', 'response', 'receipt'):
        name = artifact + ('.bin' if artifact == 'response' else '.json')
        digest = fields['_model_wire'][artifact + '_blob_sha']
        assert Path(blob_paths[digest]).read_bytes() == originals[name]
    if error_code:
        assert error_code in {error['code'] for error in errors}
    else:
        assert not errors
    assert all((audit.directory / name).read_bytes() == raw for name, raw in originals.items())


def test_wire_request_tampering_never_creates_a_bound_review(tmp_path):
    from nannanews.model_audit import ModelAudit

    audit = ModelAudit(tmp_path / 'wire', {'model': 'synthetic'}, request_limit=4096, response_limit=4096)
    audit.append(b'{}')
    audit.preserve_response(complete=True)
    audit.close()
    (audit.directory / 'request.json').write_bytes(b'{"different":"request"}')
    root = tmp_path / 'foundation'
    result = extract_paths(root, [audit.directory / 'receipt.json'])
    assert result['held'] == 1 and result['records'] == 0
    with sqlite3.connect(root / 'foundation.sqlite3') as db:
        assert db.execute('SELECT review_id FROM reviews').fetchall() == []
    assert (audit.directory / 'request.json').read_bytes() == b'{"different":"request"}'


def test_prepared_wire_does_not_mistake_reserved_disk_space_for_response(tmp_path):
    from nannanews.model_audit import ModelAudit

    audit = ModelAudit(tmp_path / 'wire', {'model': 'synthetic'}, request_limit=4096, response_limit=4096)
    audit.close()
    root = tmp_path / 'foundation'
    result = extract_paths(root, [audit.directory / 'receipt.json'])
    assert result['held'] == 1 and result['records'] == 1
    with sqlite3.connect(root / 'foundation.sqlite3') as db:
        fields, errors = db.execute('SELECT fields_json,error_json FROM reviews').fetchone()
    assert 'request_blob_sha' in json.loads(fields)['_model_wire']
    assert 'response_blob_sha' not in json.loads(fields)['_model_wire']
    assert 'response_unavailable' in {error['code'] for error in json.loads(errors)}


def test_retained_pdf_is_in_snapshot_identity_and_dependency_graph(tmp_path):
    root = tmp_path / 'foundation'
    paths = []
    for index in (1, 2):
        envelope = cache()
        pdf = ('%PDF-1.4\n% distinct original ' + str(index) + '\n%%EOF').encode()
        envelope['capture']['document']['pdf_documents'] = [{
            'canonical_url': 'https://example.edu/original.pdf',
            'raw_response_base64': base64.b64encode(pdf).decode(),
            'raw_response_digest': hashlib.sha256(pdf).hexdigest()}]
        paths.append(write_json(tmp_path / 'historical' / ('cache-' + str(index) + '.json'), envelope))
    result = extract_paths(root, paths)
    assert result['held'] == 0
    db = connect_foundation(root)
    try:
        assert db.execute('SELECT count(*) FROM snapshots').fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM snapshot_blobs WHERE role='pdf'").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM blobs WHERE kind='source_pdf'").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM blobs WHERE kind='source_image'").fetchone()[0] == 1
    finally:
        db.close()


def test_numeric_legacy_refs_are_not_globally_linked_but_document_identity_is(tmp_path):
    root = tmp_path / 'foundation'
    seed(root)
    document_sha, local_key = 'ab' * 32, 'table-1-row-2'
    stable_occurrence = 'occ-' + hashlib.sha256((document_sha + ':' + local_key).encode()).hexdigest()[:24]
    db = connect_foundation(root)
    with db:
        # Even a stale unsafe alias and reused integer locator must not bind an
        # unrelated campaign's bare numeric IDs to this canonical activity.
        db.execute("INSERT INTO activity_aliases VALUES('legacy-item:41','canonical-1')")
        db.execute("UPDATE source_records SET locator_json=? WHERE occurrence_id='occ-1'",
                   (json.dumps({'legacy_occurrence_id': 73, 'legacy_item_id': 41}),))
        db.execute('''INSERT INTO source_records
            (source_id,activity_id,occurrence_id,source_urls_json,locator_json,parse_status)
            VALUES('document-bound','canonical-1',?,'[]','{}','parsed')''', (stable_occurrence,))
    db.close()
    bare = review(73, item_id=41)
    bound = review(73, item_id=41, document_sha256=document_sha, local_key=local_key)
    path = write_reviews(tmp_path / 'historical/reviews.jsonl', [bare, bound])
    extract_paths(root, [path])
    db = connect_foundation(root)
    try:
        rows = list(db.execute('SELECT * FROM reviews'))
        assert len(rows) == 2
        for row in rows:
            refs = json.loads(row['fields_json'])['legacy_refs']
            assert refs['occurrence_id'] == 73 and refs['item_id'] == 41
            assert row['activity_id'] == ('canonical-1' if refs.get('document_sha256') else None)
        assert db.execute('SELECT source_id FROM activity_snapshots').fetchone()[0] == 'document-bound'
    finally:
        db.close()



def test_failed_source_cache_preserves_raw_failure_separately_from_readable_error(tmp_path):
    root = tmp_path / 'foundation'
    raw = b'<html>Private original access-denied response.</html>'
    failure = {'url': URL, 'sha256': 'legacy-failure', 'capture': {
        '_timing': {'elapsed': 10}, 'error': {'code': 'source_blocked',
            'reason': 'Source requires campus access.',
            'source_response': {'raw_response_base64': base64.b64encode(raw).decode(),
                'raw_response_digest': hashlib.sha256(raw).hexdigest(),
                'content_type': 'text/html', 'http_status': 403, 'raw_response_complete': False}}}}
    path = write_json(tmp_path / 'historical/cache.json', failure)
    result = extract_paths(root, [path])
    assert result['records'] == 1 and result['held'] == 1
    db = connect_foundation(root)
    try:
        row = db.execute('SELECT * FROM reviews').fetchone()
        errors = json.loads(row['error_json'])
        assert errors[0]['code'] == 'source_blocked'
        assert errors[0]['reason'] == 'Source requires campus access.'
        original = db.execute('''SELECT b.local_path,b.mime,s.status FROM review_snapshots r
            JOIN snapshots s USING(snapshot_id) JOIN snapshot_blobs sb USING(snapshot_id)
            JOIN blobs b ON b.sha256=sb.blob_sha WHERE r.review_id=? AND sb.role='raw_http' ''',
            (row['review_id'],)).fetchone()
        assert Path(original['local_path']).read_bytes() == raw
        assert original['mime'] == 'application/octet-stream' and original['status'] == 'failed'
        assert db.execute("SELECT sha256 FROM blobs WHERE kind='source_image'").fetchall() == []
        answer = blob_text(db, row['answer_blob_sha'])
        assert 'raw_response_base64' not in answer
        assert raw.decode() not in answer
        assert path.read_text(encoding='utf-8') == json.dumps(failure, ensure_ascii=False)
    finally:
        db.close()


def test_source_changed_during_normalization_is_held(tmp_path, monkeypatch):
    from nannanews.data_foundation_blobs import BlobStore

    root = tmp_path / 'foundation'
    path = write_reviews(tmp_path / 'historical/reviews.jsonl', [review()])
    original = BlobStore.put_bytes
    changed = False

    def change_source_after_retaining_blob(store, data, *, mime, kind):
        nonlocal changed
        digest = original(store, data, mime=mime, kind=kind)
        if not changed:
            changed = True
            with path.open('ab') as stream:
                stream.write(b'\n')
        return digest

    monkeypatch.setattr(BlobStore, 'put_bytes', change_source_after_retaining_blob)
    result = extract_paths(root, [path])
    assert result['held'] == 1 and path.exists()
    db = connect_foundation(root)
    try:
        row = db.execute('SELECT * FROM ingestions').fetchone()
        assert row['status'] == 'held' and 'changed' in row['error_json']
        assert db.execute('SELECT count(*) FROM reviews').fetchone()[0] == 1
    finally:
        db.close()


def test_jsonl_bound_holds_oversized_record_without_losing_prior_review(tmp_path, monkeypatch):
    from nannanews import data_foundation_extract as extraction

    root = tmp_path / 'foundation'
    small = {'status': 'unresolved', 'reason': 'No source available.', 'claims': [], 'source_review': {}}
    path = write_reviews(tmp_path / 'historical/reviews.jsonl', [small, dict(small, reason='x' * 2000)])
    monkeypatch.setattr(extraction, 'MAX_RECORD_BYTES', 1024)
    result = extract_paths(root, [path])
    assert result['records'] == 1 and result['held'] == 1
    db = connect_foundation(root)
    try:
        row = db.execute('SELECT * FROM ingestions').fetchone()
        assert row['record_count'] == 1 and row['status'] == 'held'
        assert 'line 2' in row['error_json'] and 'bounded record limit' in row['error_json']
    finally:
        db.close()


def test_cleaned_missing_source_requires_matching_durable_deletion_proof(tmp_path):
    root = tmp_path / 'foundation'
    path = write_reviews(tmp_path / 'historical/reviews.jsonl', [review()])
    extract_paths(root, [path])
    info = path.stat()
    db = connect_foundation(root)
    with db:
        ingestion = db.execute('SELECT * FROM ingestions').fetchone()
        outputs = [dict(row) for row in db.execute('SELECT kind,row_id FROM ingestion_outputs')]
        proof = {'phase': 'deleted', 'mtime_ns': info.st_mtime_ns, 'device': info.st_dev,
                 'inode': info.st_ino, 'outputs': outputs}
        db.execute("UPDATE ingestions SET status='cleaned',cleaned_at='2026-10-25'")
        db.execute('INSERT INTO cleanup_log VALUES(?,?,?,?,?)',
                   (str(path), ingestion['sha256'], ingestion['size'], '2026-10-25', json.dumps(proof)))
    db.close()
    path.unlink()  # Only this synthetic temporary source is removed.
    result = extract_paths(root, [path])
    assert result['skipped'] == 1 and result['held'] == 0 and result['errors'] == 0
    write_reviews(path, [review()])
    reappeared = extract_paths(root, [path])
    assert reappeared['held'] == 1 and path.exists()
    path.unlink()  # Restore the missing-file condition for the corrupt-proof case.
    db = connect_foundation(root)
    with db:
        db.execute("UPDATE cleanup_log SET proof_json='{}'")
    db.close()
    result = extract_paths(root, [path])
    assert result['held'] == 1 and result['skipped'] == 0


def test_progress_capture_reference_preserves_missing_evidence(tmp_path):
    root = tmp_path / 'foundation'
    item = review(capture_artifact='/private/history/captures/capture-abc.json')
    item['source_review']['sources'] = []
    path = write_reviews(tmp_path / 'historical/progress.jsonl', [item])
    result = extract_paths(root, [path])
    assert result['held'] == 1 and path.exists()
    db = connect_foundation(root)
    try:
        fields = json.loads(db.execute('SELECT fields_json FROM reviews').fetchone()[0])
        assert fields['retained_evidence']['capture_artifact'] == item['capture_artifact']
    finally:
        db.close()


def test_unidentified_output_wrapper_and_empty_capture_remain_unsupported(tmp_path):
    root = tmp_path / 'foundation'
    paths = [write_json(tmp_path / 'historical/unknown-output.json', {'output': [], 'payload': 'Unique unknown data.'}),
             write_json(tmp_path / 'historical/empty-cache.json', {'url': URL, 'capture': {}})]
    result = extract_paths(root, paths)
    assert result['held'] == 2 and result['records'] == 0
    assert all(path.exists() for path in paths)


@pytest.mark.parametrize('record', [
    {'source_review': {}},
    {'source_review': None},
    {'source_review': []},
    {'source_review': False},
    {'source_review': ''},
    {'source_review': {'status': 'unresolved'}},
    {'source_review': {'reason': None}},
    {'source_review': {}, 'status': 'unresolved', 'reason': '   '},
    {'source_review': {}, 'error': '   '},
    {'source_review': {'activity': {}}},
    {'source_review': {'attempts': [{}]}},
    {'source_review': {'payload': 'Unique nested evidence.'}},
    {'source_review': {}, 'reason': {'payload': 'Not a parsed explanation.'}},
    {'source_review': {}, 'claims': [None]},
])
@pytest.mark.parametrize('unknown_payload', [False, True])
def test_empty_null_and_malformed_review_wrappers_are_held(tmp_path, record, unknown_payload):
    root = tmp_path / 'foundation'
    historical = tmp_path / 'historical'
    record = deepcopy(record)
    if unknown_payload:
        record['payload'] = 'Unique unrecognized evidence must not be discarded.'
    paths = [write_json(historical / 'wrapper.json', record),
             write_reviews(historical / 'wrapper.jsonl', [record])]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    result = extract_paths(root, paths)
    assert result['held'] == 2
    assert result['records'] == result['snapshots'] == result['blobs'] == 0
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths} == before
    db = connect_foundation(root)
    try:
        for path in paths:
            row = db.execute('SELECT * FROM ingestions WHERE source_path=?', (str(path),)).fetchone()
            assert row['status'] == 'held' and row['completed_at'] is None
            assert row['record_count'] == row['snapshot_count'] == 0
            assert row['sha256'] == hashlib.sha256(before[path][0]).hexdigest()
            assert json.loads(row['error_json'])
        for table in ('reviews', 'snapshots', 'blobs', 'ingestion_outputs'):
            assert db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
    finally:
        db.close()


def test_unknown_review_payload_is_held_even_alongside_a_known_reason(tmp_path):
    root = tmp_path / 'foundation'
    item = {'source_review': {}, 'status': 'unresolved', 'reason': 'A retained parsed reason.',
            'claims': [], 'payload': 'Unique evidence outside the recognized review contract.'}
    path = write_json(tmp_path / 'historical/review.json', item)
    original = path.read_bytes()
    result = extract_paths(root, [path])
    assert result['held'] == 1 and result['records'] == 0
    assert path.read_bytes() == original
    db = connect_foundation(root)
    try:
        assert db.execute('SELECT status FROM ingestions').fetchone()[0] == 'held'
        assert db.execute('SELECT count(*) FROM ingestion_outputs').fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize('suffix', ['json', 'jsonl'])
def test_parent_directory_rotation_cannot_bind_b_outputs_to_a_fingerprint(tmp_path, monkeypatch, suffix):
    from nannanews import data_foundation_extract as extraction

    root = tmp_path / 'foundation'
    active, alternate, parked = (tmp_path / name for name in ('active', 'alternate', 'parked-a'))
    record_a = {'status': 'unresolved', 'reason': 'Actual source A result.', 'claims': [], 'source_review': {}}
    record_b = dict(record_a, reason='Different source B result.')
    filename = 'reviews.' + suffix
    writer = write_reviews if suffix == 'jsonl' else write_json
    path = writer(active / filename, [record_a] if suffix == 'jsonl' else record_a)
    other_path = writer(alternate / filename, [record_b] if suffix == 'jsonl' else record_b)
    original_a, original_b = path.read_bytes(), other_path.read_bytes()
    fingerprint = extraction._fingerprint
    state = {'started': False, 'rotated': False}

    def restore_a():
        if state['rotated']:
            active.rename(alternate)
            parked.rename(active)
            state['rotated'] = False

    def fingerprint_with_real_parent_rotation(candidate):
        if Path(candidate) == path:
            if not state['started']:
                expected = fingerprint(candidate)
                active.rename(parked)
                alternate.rename(active)
                state.update(started=True, rotated=True)
                return expected
            # The vulnerable implementation parsed B, then saw A again in its
            # independent final fingerprint. No sleeps or timing luck required.
            restore_a()
        return fingerprint(candidate)

    monkeypatch.setattr(extraction, '_fingerprint', fingerprint_with_real_parent_rotation)
    try:
        result = extract_paths(root, [path])
    finally:
        restore_a()
    assert state['started']
    assert result['held'] == 1
    assert path.read_bytes() == original_a and other_path.read_bytes() == original_b
    db = connect_foundation(root)
    try:
        ingestion = db.execute('SELECT * FROM ingestions WHERE source_path=?', (str(path),)).fetchone()
        assert ingestion['sha256'] == hashlib.sha256(original_a).hexdigest()
        assert ingestion['status'] == 'held' and ingestion['completed_at'] is None
        assert 'identity changed before parsing' in ingestion['error_json']
        assert db.execute('SELECT count(*) FROM reviews').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM ingestion_outputs').fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize('suffix', ['json', 'jsonl'])
def test_parser_fingerprint_includes_blank_lines_and_trailing_raw_bytes(tmp_path, suffix):
    root = tmp_path / 'foundation'
    historical = tmp_path / 'historical'
    historical.mkdir()
    path = historical / ('review.' + suffix)
    record = {'status': 'unresolved', 'reason': 'Actual retained parsed explanation.', 'claims': [], 'source_review': {}}
    raw = b'\n \r\n' + json.dumps(record).encode() + b'\r\n\t \n \t'
    path.write_bytes(raw)
    result = extract_paths(root, [path])
    assert result['held'] == 0 and result['records'] == 1
    assert path.read_bytes() == raw
    db = connect_foundation(root)
    try:
        ingestion = db.execute('SELECT * FROM ingestions').fetchone()
        assert ingestion['status'] == 'completed'
        assert ingestion['sha256'] == hashlib.sha256(raw).hexdigest()
        assert db.execute('SELECT reason FROM reviews').fetchone()[0] == record['reason']
    finally:
        db.close()


@pytest.mark.parametrize('raw_text', [
    json.dumps({'data': [{'description': 'Recruiting API transport marker. ' * 180}], 'total': 1}),
    json.dumps({'date': '2026-10-26', 'payload': 'Structured transport marker.'}),
    'Oversized raw-date transport marker. ' * 40,
])
@pytest.mark.parametrize('visible_answer', [False, True])
def test_transport_raw_dates_are_normalized_without_rewriting_visible_answers(tmp_path, raw_text, visible_answer):
    root = tmp_path / 'foundation'
    claim = {'kind': 'registration', 'normalized_date': '2026-10-26', 'raw_text': raw_text,
             'resolution': 'resolved', 'evidence': 'Source explicitly gives the date.',
             'source_id': 'legacy-source-42'}
    record = {'status': 'confirmed', 'reason': 'The date was resolved from retained evidence.',
              'claims': [deepcopy(claim)], 'source_review': {},
              'fields': {'claims': [deepcopy(claim)]},
              'parsed_result': {'reason': 'Readable parsed date result.',
                                'claims': [deepcopy(claim)], 'dates': [deepcopy(claim)]}}
    answer = '  Actual visible model answer:\n' + raw_text + '\n日期：2026-10-26\n'
    if visible_answer:
        record['answer'] = answer
    path = write_json(tmp_path / 'historical/review.json', record)
    original = path.read_bytes()
    result = extract_paths(root, [path])
    assert result['held'] == 0 and result['records'] == 1
    assert path.read_bytes() == original
    db = connect_foundation(root)
    try:
        row = db.execute('SELECT * FROM reviews').fetchone()
        fields = json.loads(row['fields_json'])
        normalized_claims = [fields['claims'][0], fields['field_checks']['claims'][0],
                             fields['parsed_result']['claims'][0], fields['parsed_result']['dates'][0]]
        expected_sha = hashlib.sha256(raw_text.encode('utf-8')).hexdigest()
        for normalized in normalized_claims:
            assert normalized['kind'] == 'registration'
            assert normalized['normalized_date'] == '2026-10-26'
            assert normalized['resolution'] == 'resolved'
            assert normalized['source_id'] == 'legacy-source-42'
            assert normalized['raw_text'] is None
            assert normalized['raw_text_sha256'] == expected_sha
            assert normalized['raw_text_omitted'] in {'non_date_payload', 'oversized'}
            assert normalized['evidence'] == claim['evidence']
        assert raw_text not in row['fields_json']
        retained_answer = blob_text(db, row['answer_blob_sha'])
        if visible_answer:
            assert fields['answer_kind'] == 'visible_answer'
            assert retained_answer == answer
        else:
            assert fields['answer_kind'] == 'parsed_result'
            assert retained_answer.startswith('Parsed review result (not a verbatim model answer)')
            assert raw_text not in retained_answer
            assert expected_sha in retained_answer and '2026-10-26' in retained_answer
    finally:
        db.close()


def test_genuine_date_text_survives_and_normalized_claims_reimport_idempotently(tmp_path):
    root = tmp_path / 'foundation'
    genuine = {'kind': 'event', 'normalized_date': '2026-10-26',
               'raw_text': '2026年10月26日（星期一）', 'evidence': 'Literal event date.'}
    transport = dict(genuine, raw_text=json.dumps({'items': [{'body': 'API body fragment. ' * 100}]}))
    record = {'status': 'confirmed', 'reason': 'Retained parsed date checks.',
              'claims': [genuine, transport], 'source_review': {}}
    first = write_json(tmp_path / 'historical/first.json', record)
    assert extract_paths(root, [first])['held'] == 0
    db = connect_foundation(root)
    try:
        row = db.execute('SELECT * FROM reviews').fetchone()
        first_review_id, first_answer_sha = row['review_id'], row['answer_blob_sha']
        normalized = json.loads(row['fields_json'])['claims']
        assert normalized[0]['raw_text'] == genuine['raw_text']
        assert genuine['raw_text'] in blob_text(db, row['answer_blob_sha'])
        assert normalized[1]['raw_text'] is None
        assert normalized[1]['raw_text_sha256'] == hashlib.sha256(transport['raw_text'].encode()).hexdigest()
    finally:
        db.close()
    record['claims'] = normalized
    second = write_json(tmp_path / 'historical/second.json', record)
    assert extract_paths(root, [second])['held'] == 0
    db = connect_foundation(root)
    try:
        assert db.execute('SELECT count(*) FROM reviews').fetchone()[0] == 1
        row = db.execute('SELECT * FROM reviews').fetchone()
        assert row['review_id'] == first_review_id and row['answer_blob_sha'] == first_answer_sha
        assert json.loads(row['fields_json'])['claims'] == normalized
        assert db.execute("SELECT count(*) FROM ingestion_outputs WHERE kind='review'").fetchone()[0] == 2
    finally:
        db.close()


@pytest.mark.parametrize('resolved_url', [URL, 'https://cdn.example.net/content/42'])
def test_cache_snapshots_link_all_exact_declared_urls_without_merging_activities(tmp_path, resolved_url):
    root = tmp_path / 'foundation'
    db = connect_foundation(root)
    with db:
        db.executemany('''INSERT INTO activities
            (activity_id,title,summary,review_status,record_version,input_sha256)
            VALUES(?,?,?,?,?,?)''', [
                ('activity-a', 'Manually approved A', 'Distinct A', 'approved', 'a-1', 'input-a'),
                ('activity-b', 'Pending activity B', 'Distinct B', 'pending_review', 'b-1', 'input-b'),
            ])
        db.executemany('''INSERT INTO source_records
            (source_id,activity_id,occurrence_id,source_urls_json,locator_json,parse_status)
            VALUES(?,?,?,?,?,?)''', [
                ('source-a', 'activity-a', 'occ-a', json.dumps([URL, URL]), '{}', 'parsed'),
                ('source-b', 'activity-b', 'occ-b', json.dumps([URL]), '{}', 'parsed'),
                ('source-other', 'activity-a', 'occ-other', json.dumps([URL + '.html']), '{}', 'parsed'),
                ('source-pending', None, 'occ-pending', json.dumps([URL]), '{}', 'pending_review'),
            ])
    original_activities = [dict(row) for row in db.execute('SELECT * FROM activities ORDER BY activity_id')]
    original_sources = [dict(row) for row in db.execute('SELECT * FROM source_records ORDER BY source_id')]
    db.close()
    historical = tmp_path / 'historical'
    captured = cache()
    captured['capture']['document']['resolved_url'] = resolved_url
    first = write_json(historical / 'cache-a.json', captured)
    repeated = cache()
    repeated['capture']['document']['resolved_url'] = resolved_url
    repeated['capture']['document']['fetched_at'] = '2026-10-02T00:00:00+00:00'
    second = write_json(historical / 'cache-a-again.json', repeated)
    unmatched = cache()
    unmatched['url'] = URL + '/'
    unmatched['capture']['document'].update(canonical_url=URL + '/', resolved_url=URL + '/')
    third = write_json(historical / 'cache-unmatched.json', unmatched)
    inputs = [first, second, third]
    result = extract_paths(root, inputs)
    assert result['held'] == 0 and result['records'] == 0
    db = connect_foundation(root)
    try:
        assert db.execute('SELECT count(*) FROM snapshots WHERE url=?', (resolved_url,)).fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM snapshots').fetchone()[0] == 2
        links = [tuple(row) for row in db.execute('''SELECT link.activity_id,snapshot.url,link.source_id
            FROM activity_snapshots link JOIN snapshots snapshot USING(snapshot_id)
            ORDER BY link.activity_id,link.source_id''')]
        assert links == [('activity-a', resolved_url, 'source-a'), ('activity-b', resolved_url, 'source-b')]
        assert db.execute('''SELECT count(*) FROM activity_snapshots link
            JOIN snapshots snapshot USING(snapshot_id) WHERE snapshot.url=?''', (URL + '/',)).fetchone()[0] == 0
        assert [dict(row) for row in db.execute('SELECT * FROM activities ORDER BY activity_id')] == original_activities
        assert [dict(row) for row in db.execute('SELECT * FROM source_records ORDER BY source_id')] == original_sources
        assert db.execute('SELECT count(*) FROM reviews').fetchone()[0] == 0
    finally:
        db.close()
    again = extract_paths(root, inputs)
    assert again['skipped'] == 3 and again['held'] == 0
    db = connect_foundation(root)
    try:
        assert db.execute('SELECT count(*) FROM snapshots').fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM activity_snapshots').fetchone()[0] == 2
        assert [dict(row) for row in db.execute('SELECT * FROM activities ORDER BY activity_id')] == original_activities
    finally:
        db.close()




@pytest.mark.parametrize('existing_reference', [None, 'review-existing'])
def test_wire_evidence_preserves_existing_paid_state_and_result_reference(tmp_path, existing_reference):
    from nannanews.model_audit import ModelAudit

    root = tmp_path / 'foundation'
    with closing(connect_foundation(root)) as db, db:
        db.execute('INSERT INTO reviews(review_id,status,reason,fields_json,error_json) VALUES(?,?,?,?,?)',
                   ('review-existing', 'failed', 'Previously retained result', '{}', '[]'))
        db.execute('INSERT INTO attempts(request_key,state,result_review_id) VALUES(?,?,?)',
                   ('exact-paid-key', 'reserved', existing_reference))
    audit = ModelAudit(tmp_path / 'wire', {'model': 'synthetic-model'}, request_limit=4096,
        response_limit=4096, context={'logical_request_sha256': 'exact-paid-key'})
    audit.append(b'{"id":"synthetic-response","choices":[{"message":{"role":"assistant","content":"Original answer"}}]}')
    audit.preserve_response(complete=True)
    audit.save('completed')
    audit.close()
    extract_paths(root, [audit.directory / 'receipt.json'])
    with closing(connect_foundation(root)) as db:
        attempt = db.execute('SELECT state,result_review_id FROM attempts WHERE request_key=?', ('exact-paid-key',)).fetchone()
        wire_review = db.execute('SELECT review_id,fields_json FROM reviews WHERE review_id<>?', ('review-existing',)).fetchone()
        assert attempt['state'] == 'reserved'
        assert attempt['result_review_id'] == (existing_reference or wire_review['review_id'])
        assert json.loads(wire_review['fields_json'])['paid_facts']['state'] == 'completed'
        assert db.execute('SELECT state FROM attempt_origins WHERE request_key=?', ('exact-paid-key',)).fetchone()[0] == 'completed'
