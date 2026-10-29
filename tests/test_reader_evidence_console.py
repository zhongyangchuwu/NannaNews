"""Synthetic local foundation fixtures; no production, cloud or model access."""
from contextlib import closing
from dataclasses import replace
import base64
import hashlib
import json
import shutil
import sqlite3

import pytest

from nannanews import runtime, schema
from nannanews.data_foundation import connect_foundation
from nannanews.reader_evidence import EvidenceStore
from nannanews.reader_reviews import ReviewConflict, ReviewNotFound, ReviewStore, ReviewUnavailable
from test_reader_review_console import _client, _form
from test_reader_review_console import console, password_hash  # noqa: F401 -- shared synthetic fixture
from test_reader_reviews import store as review_store  # noqa: F401 -- registers synthetic fixture
from test_reader_reviews import BASE, ITEM, OTHER, payload

UNPUBLISHED = 'item-' + 'c' * 24
REVIEW = 'review-exact-request'
FAILED = 'review-failed-original'
OTHER_REVIEW = 'review-other-activity'
UNKNOWN_REVIEW = 'review-unknown-recorded-activity'
PNG = bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489'
                    '0000000b49444154789c636000020000050001a5f645400000000049454e44ae426082')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')


@pytest.fixture
def evidence_console(console, tmp_path, monkeypatch):
    root = tmp_path / 'private-foundation'
    blobs = {}
    with closing(connect_foundation(root)) as db, db:
        def blob(raw, mime, kind):
            digest = hashlib.sha256(raw).hexdigest()
            path = root / ('blob-' + digest)
            path.write_bytes(raw)
            path.chmod(0o600)
            db.execute('INSERT INTO blobs(sha256,size,mime,kind,local_path,owned_local) VALUES(?,?,?,?,?,1)',
                       (digest, len(raw), mime, kind, str(path)))
            blobs[digest] = path
            return digest
        request = {'model': 'synthetic-only', 'messages': [
            {'role': 'system', 'content': 'EXACT_SYSTEM not source capture'},
            {'role': 'user', 'content': [
                {'type': 'text', 'text': 'EXACT_SENT_TEXT <script>not executable</script>'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(PNG).decode()}},
                {'type': 'image_url', 'image_url': {'url': 'https://unvisited.example.org/image.png'}}]}]}
        request_sha = blob(_json(request), 'application/json', 'model_request')
        response_bytes = b'RAW_ORIGINAL_RESPONSE\x00\xff<script>response()</script>'
        response_sha = blob(response_bytes, 'application/octet-stream', 'model_response')
        receipt_sha = blob(_json({'completeness': 'bounded_complete', 'stage': 'response_saved'}),
                           'application/json', 'model_receipt')
        source_sha = blob(b'CAPTURED_WEB_SOURCE_DIFFERENT_FROM_SENT_TEXT', 'text/plain', 'source_text')
        other_sha = blob(b'OTHER_ACTIVITY_ATTACHMENT_MUST_NOT_FETCH', 'image/png', 'source_image')
        for identity, title in ((ITEM, 'Published facts'), (OTHER, 'Other facts'), (UNPUBLISHED, 'Private catalog activity')):
            db.execute('INSERT INTO activities VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (identity, title, 'Small canonical summary', None, None, None, None, None, None,
                        'needs_review', 'synthetic-local', '0' * 64))
        legacy_bytes = {'legacy-request': b'{\n  "descriptor" : "LEGACY_REQUEST_METADATA_NOT_HTTP", "text":"<script>legacy()</script>"\n}\n',
                        'legacy-result': _json({'parsed': 'LEGACY_PARSED_RESULT_NOT_PROVIDER_RESPONSE'}),
                        'legacy-decision': _json({'decision': 'LEGACY_STORED_DECISION'})}
        legacy_shas = {role: blob(raw, 'application/json', 'legacy_attempt_metadata')
                       for role, raw in legacy_bytes.items()}
        legacy_paid = {'request_key': 'original-logical-request-key', 'state': 'historical_failed',
                       'charge': 'unknown', 'source_ledger': 'synthetic-original-ledger.sqlite3',
                       'artifacts': {'request_blob_sha': legacy_shas['legacy-request'],
                                     'result_blob_sha': legacy_shas['legacy-result'],
                                     'decision_blob_sha': legacy_shas['legacy-decision']}}
        fields = {'status': 'resolved', 'decision': 'PARSED_JUDGMENT', '_model_wire': {
            'request_blob_sha': request_sha, 'response_blob_sha': response_sha, 'receipt_blob_sha': receipt_sha}}
        for review_id, identity, status, data, errors in (
                (REVIEW, ITEM, 'resolved', fields, []),
                (FAILED, ITEM, 'failed', {'decision': 'NOT_ADMITTED', 'paid_facts': legacy_paid}, [{'stage': 'parse', 'message': 'SYNTHETIC_PARSE_ERROR'}]),
                (UNKNOWN_REVIEW, None, 'failed', {'decision': 'UNKNOWN_IDENTITY_ORIGINAL'}, []),
                (OTHER_REVIEW, OTHER, 'resolved', {'_model_wire': {'request_blob_sha': other_sha}}, [])):
            db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (review_id, identity, None, None, None, 'synthetic-rule', 'synthetic-model', status,
                        'Recorded decision', None, json.dumps(data), json.dumps(errors)))
        db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?)',
                   ('snapshot-selected', 'https://unvisited.example.org/source', 'Captured only', source_sha,
                    '2026-10-27', 'captured', '{}', None, None))
        db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?)',
                   ('snapshot-other', 'https://unvisited.example.org/other', 'Other', None,
                    '2026-10-27', 'captured', '{}', None, None))
        db.execute('INSERT INTO review_snapshots VALUES(?,?)', (REVIEW, 'snapshot-selected'))
        db.execute('INSERT INTO review_snapshots VALUES(?,?)', (OTHER_REVIEW, 'snapshot-other'))
        db.execute('INSERT INTO snapshot_blobs VALUES(?,?,?,?)', ('snapshot-other', other_sha, 'image', 0))
    settings = replace(runtime.settings, reader_evidence_dir=root)
    monkeypatch.setattr(runtime, 'settings', settings)
    store = ReviewStore(settings)
    fetched = []
    original_blob = EvidenceStore.blob
    def selected_blob(evidence, digest, **kwargs):
        result = original_blob(evidence, digest, **kwargs)
        fetched.append(digest)
        return result
    monkeypatch.setattr(EvidenceStore, 'blob', selected_blob)
    return {'console': (console[0], store), 'store': store, 'evidence': EvidenceStore(settings),
            'root': root, 'directory': root, 'blobs': blobs, 'fetched': fetched,
            'request_sha': request_sha, 'response_sha': response_sha, 'receipt_sha': receipt_sha,
            'legacy_bytes': legacy_bytes, 'legacy_shas': legacy_shas,
            'source_sha': source_sha, 'other_sha': other_sha, 'response_bytes': response_bytes}


def test_queue_and_review_headers_do_not_fetch_heavy_records(evidence_console):
    case = evidence_console
    rows = case['store'].list_items(status='all')['items']
    assert any(row['item_id'] == UNPUBLISHED and row['title'] == 'Private catalog activity' for row in rows)
    reviews = case['store'].list_reviews(ITEM)
    assert {row['review_id'] for row in reviews['items']} == {REVIEW, FAILED}
    assert case['fetched'] == []
    assert not (case['directory'] / 'cache').exists()


def test_selected_trace_uses_exact_request_and_not_captured_source(evidence_console):
    case = evidence_console
    trace = case['evidence'].review(ITEM, REVIEW)
    assert [entry['text'] for entry in trace['texts']] == [
        'EXACT_SYSTEM not source capture', 'EXACT_SENT_TEXT <script>not executable</script>']
    assert trace['images'][0]['available'] and not trace['images'][1]['available']
    assert trace['response'].startswith('RAW_ORIGINAL_RESPONSE') and 'PARSED_JUDGMENT' in trace['judgment']
    assert trace['missing'] == []
    assert {case['request_sha'], case['response_sha'], case['receipt_sha']} <= set(case['fetched'])
    assert case['source_sha'] not in case['fetched'] and case['other_sha'] not in case['fetched']
    image, mime = case['evidence'].prepared_image(ITEM, REVIEW, 0)
    assert (image, mime) == (PNG, 'image/png')
    assert case['evidence'].artifact(ITEM, REVIEW, 'response') == case['response_bytes']
    source = case['evidence'].source(ITEM, REVIEW, 'snapshot-selected')
    assert source['text'] == 'CAPTURED_WEB_SOURCE_DIFFERENT_FROM_SENT_TEXT'
    assert case['other_sha'] not in case['fetched']


def test_failed_history_is_visible_with_actual_missing_fields(evidence_console):
    trace = evidence_console['evidence'].review(ITEM, FAILED)
    assert trace['status'] == 'failed' and 'SYNTHETIC_PARSE_ERROR' in trace['errors']
    assert {entry['field'] for entry in trace['missing']} == {'request', 'response', 'receipt'}
    assert trace['response'] is None
    assert '不能证明完整' in trace['missing'][0]['message']


@pytest.mark.parametrize('failure', ['missing', 'corrupt'])
def test_selected_artifact_failure_is_explicit_not_empty_success(evidence_console, monkeypatch, failure):
    case = evidence_console
    path = case['blobs'][case['request_sha']]
    if failure == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'corrupt')
    trace = EvidenceStore(case['store'].settings).review(ITEM, REVIEW)
    assert trace['texts'] == [] and trace['response'].startswith('RAW_ORIGINAL_RESPONSE')
    assert any(row['field'] == 'request' and ('缺失' in row['message'] or '校验失败' in row['message'])
               for row in trace['missing'])

def test_remote_only_blob_is_never_fetched(evidence_console):
    case = evidence_console
    with closing(sqlite3.connect(case['root'] / 'foundation.sqlite3')) as db, db:
        db.execute('UPDATE blobs SET local_path=NULL, remote_key=? WHERE sha256=?',
                   ('synthetic-remote-object', case['request_sha']))
    trace = case['evidence'].review(ITEM, REVIEW)
    assert trace['texts'] == []
    assert any(row['field'] == 'request' and '不会访问云端' in row['message'] for row in trace['missing'])



def test_cross_activity_and_source_boundaries_require_explicit_private_link(evidence_console):
    case = evidence_console
    with pytest.raises(ReviewNotFound):
        case['evidence'].review(ITEM, OTHER_REVIEW)
    with pytest.raises(ReviewNotFound):
        case['evidence'].source(ITEM, REVIEW, 'snapshot-other')
    with pytest.raises(ReviewNotFound):
        case['store'].associate_review(ITEM, OTHER_REVIEW, actor='editor')
    with pytest.raises(ReviewNotFound):
        case['evidence'].review(ITEM, UNKNOWN_REVIEW)
    link = case['store'].associate_review(ITEM, UNKNOWN_REVIEW, actor='editor')
    assert case['store'].review_associated(ITEM, UNKNOWN_REVIEW, dataset=link['dataset_id'])
    assert any(row['review_id'] == UNKNOWN_REVIEW for row in case['store'].list_reviews(ITEM)['associated'])
    assert not case['store'].review_associated(UNPUBLISHED, UNKNOWN_REVIEW)
    trace = case['evidence'].review(ITEM, UNKNOWN_REVIEW, associated=True, dataset=link['dataset_id'])
    assert trace['activity_id'] is None and 'UNKNOWN_IDENTITY_ORIGINAL' in trace['judgment']



def test_registration_save_conflict_and_publish_snapshot_remain_private(console):
    store = console[1]
    root = store.settings.reader_data_dir
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    registration = store.register_activity('opaque-synthetic-registration', payload('New independent activity'), actor='editor')
    identity = registration['item_id']
    item = store.get_item(identity)
    assert item['initial_source'] == 'private_registration' and not item['removed'] and item['head'] is None
    assert item['registration'] == registration
    assert store.publication_snapshot()['new_activities'] == []
    with pytest.raises(ReviewConflict):
        store.register_activity('opaque-synthetic-registration', payload('Different title'), actor='editor')
    revision = store.save(identity, payload('New saved facts'), actor='editor', expected_head=None, base_version=BASE)
    with pytest.raises(ReviewConflict):
        store.save(identity, payload('Stale overwrite'), actor='other', expected_head=None, base_version=BASE)
    snapshot = store.publication_snapshot()
    exported = snapshot['new_activities'][0]
    assert exported['revision'] == revision['revision'] and exported['occurrence_id'] == registration['occurrence_id']
    assert exported['source_id'] == registration['source_id'] and exported['kind'] == 'standalone'
    assert exported['issue_id'] is exported['document_id'] is None
    assert exported['source_urls'] == revision['payload']['activity']['source_urls']
    assert {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()} == before
    with closing(sqlite3.connect(store.settings.database_path)) as db, db:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute('DELETE FROM reader_private_activities WHERE item_id=?', (identity,))


def test_archive_only_activity_is_editable_and_publishable(evidence_console):
    case = evidence_console
    item = case['store'].get_item(UNPUBLISHED)
    assert item['title'] == 'Private catalog activity' and item['initial_payload']['activity']['content'] == ''
    revision = case['store'].save(UNPUBLISHED, payload('Explicitly reviewed independent facts'), actor='editor',
                                  expected_head=None, base_version=item['base_version'])
    assert case['store'].publication_snapshot()['new_activities'][0]['item_id'] == UNPUBLISHED
    assert case['store'].get_item(UNPUBLISHED)['head_revision'] == revision['revision']


def test_human_and_agent_auth_csrf_and_private_trace_escaping(evidence_console):
    case = evidence_console
    console = case['console']
    path = f'/console/activities/{ITEM}/reviews/{REVIEW}'
    assert console[0].test_client().get(path).status_code == 302
    assert _client(console, 'member').get(path).status_code == 401
    client = _client(console)
    assert client.get(path).headers['Cache-Control'] == 'private, no-store'
    html = client.get(path).get_data(as_text=True)
    assert 'EXACT_SENT_TEXT &lt;script&gt;' in html and '<script>not executable</script>' not in html
    assert 'response()&lt;/script&gt;' in html
    api = client.get(f'/console/api/activities/{ITEM}/reviews/{REVIEW}')
    assert api.status_code == 200 and api.get_json()['texts'][1]['text'].startswith('EXACT_SENT_TEXT')
    source_api = client.get(f'/console/api/activities/{ITEM}/reviews/{REVIEW}/sources/snapshot-selected')
    assert source_api.status_code == 200 and source_api.get_json()['text'].startswith('CAPTURED_WEB_SOURCE')
    raw = client.get(path + '/artifacts/response')
    assert raw.data == case['response_bytes'] and raw.headers['X-Content-Type-Options'] == 'nosniff'
    assert 'attachment' in raw.headers['Content-Disposition']
    assert client.get(path + '/images/0').data == PNG
    assert client.get(path + '/images/1').status_code == 503
    assert client.get(f'/console/activities/{ITEM}/reviews/{OTHER_REVIEW}').status_code == 404
    assert client.put(f'/console/api/activities/{ITEM}', json={'payload': payload()}).status_code == 400
    headers = {'X-CSRF-Token': 'test-csrf'}
    assert client.post('/console/api/activities', json=[], headers=headers).status_code == 400
    created = client.post('/console/api/activities', json={'registration_key': 'api-new', 'payload': payload()}, headers=headers)
    assert created.status_code == 201
    identity = created.get_json()['item_id']
    saved = client.put(f'/console/api/activities/{identity}', json={'payload': payload(), 'expected_head': None,
                                                                 'base_version': BASE}, headers=headers)
    assert saved.status_code == 200
    assert client.get('/console/activities/new').status_code == 200
    human = client.post('/console/activities/new', data=_form(registration_key='human-new'))
    assert human.status_code == 302
    human_page = client.get(human.headers['Location']).get_data(as_text=True)
    assert '私有登记' in human_page and '尚无人工修订' in human_page
    assert client.post('/console/api/publish', json={'confirm_publish': True}, headers=headers).status_code == 401
    assert _client(console, 'admin').post('/console/api/publish', json={'confirm_publish': False}, headers=headers).status_code == 400


def test_unconfigured_and_absent_directory_are_explicit_and_noncreating(console, tmp_path):
    settings = replace(console[1].settings, reader_evidence_dir=None)
    with pytest.raises(ReviewUnavailable, match='NANNA_READER_EVIDENCE_DIR'):
        EvidenceStore(settings).list_reviews(ITEM)
    absent = tmp_path / 'does-not-exist'
    settings = replace(settings, reader_evidence_dir=absent)
    with pytest.raises(ReviewUnavailable, match='不存在'):
        EvidenceStore(settings).list_reviews(ITEM)
    assert not absent.exists()


@pytest.mark.parametrize('mime,body', [('image/svg+xml', b'<svg onload="alert(1)"/>'),
                                     ('image/png', b'<html><script>alert(1)</script></html>')])
def test_prepared_images_never_execute_svg_or_mislabeled_html(evidence_console, monkeypatch, mime, body):
    case = evidence_console
    request = _json({'input': [{'content': [{'type': 'input_image',
        'image_url': 'data:' + mime + ';base64,' + base64.b64encode(body).decode()}]}]})
    monkeypatch.setattr(case['evidence'], 'artifact', lambda *args, **kwargs: request)
    with pytest.raises(ReviewUnavailable):
        case['evidence'].prepared_image(ITEM, REVIEW, 0)


def test_private_v2_schema_requires_explicit_migration(console):
    settings = console[1].settings
    with closing(sqlite3.connect(settings.database_path)) as db, db:
        db.execute('UPDATE nannanews_schema SET version=2')
    before = settings.database_path.read_bytes()
    with pytest.raises(ReviewUnavailable, match='migrate'):
        ReviewStore(settings).list_items()
    assert settings.database_path.read_bytes() == before
    assert schema.migrate() == schema.CURRENT_VERSION



def test_oversized_selected_wire_is_rejected_before_fetch(evidence_console):
    case = evidence_console
    with closing(sqlite3.connect(case['root'] / 'foundation.sqlite3')) as db, db:
        db.execute('UPDATE blobs SET size=? WHERE sha256=?', (65 * 1024 * 1024, case['request_sha']))
    trace = EvidenceStore(case['store'].settings).review(ITEM, REVIEW)
    assert any(row['field'] == 'request' and '上限' in row['message'] for row in trace['missing'])
    assert case['request_sha'] not in case['fetched']


@pytest.mark.parametrize('field,value', [('confirm_fetch', False), ('confirm_paid', False),
                                       ('confirm_fetch', 1), ('confirm_paid', 'true')])
def test_explicit_review_rejects_missing_or_nonboolean_authorization_before_work(console, monkeypatch, field, value):
    client = _client(console)
    def unexpected(*args, **kwargs):
        raise AssertionError('registration or evidence opened before both explicit authorizations')
    monkeypatch.setattr(ReviewStore, 'get_item', unexpected)
    body = {'confirm_fetch': True, 'confirm_paid': True, field: value}
    response = client.post(f'/console/api/activities/{ITEM}/review', json=body,
                           headers={'X-CSRF-Token': 'test-csrf'})
    assert response.status_code == 400
    body = {'confirm_fetch': True, 'confirm_paid': True, 'foundation_root': '/untrusted'}
    assert client.post(f'/console/api/activities/{ITEM}/review', json=body,
                       headers={'X-CSRF-Token': 'test-csrf'}).status_code == 400


def test_registered_review_uses_configured_foundation(evidence_console, monkeypatch):
    from nannanews import reader_activity_review
    case = evidence_console
    identity = case['store'].register_activity('direct-run', payload(), actor='editor')['item_id']
    calls = []
    def workflow(**kwargs):
        calls.append(kwargs)
        return {'state': 'reviewed', 'item_id': identity, 'review_ids': [],
                'receipt_review_ids': [], 'result_review_ids': [], 'new_request': True,
                'decision_status': 'needs_review', 'reason': 'recorded'}
    monkeypatch.setattr(reader_activity_review, 'review_registered_activity', workflow)
    client = _client(case['console'])
    body = {'confirm_fetch': True, 'confirm_paid': True}
    response = client.post(f'/console/api/activities/{identity}/review', json=body,
                           headers={'X-CSRF-Token': 'test-csrf'})
    assert response.status_code == 200
    assert len(calls) == 1 and calls[0]['foundation_root'] == case['root']
    assert calls[0]['allow_fetch'] is calls[0]['allow_paid'] is True
    assert client.post(f'/console/activities/{identity}/review', data={
        'csrf_token': 'test-csrf', 'confirm_fetch': 'yes', 'confirm_paid': 'yes'}).status_code == 200






def test_legacy_metadata_downloads_are_lazy_and_never_substitute_for_wire(evidence_console):
    case = evidence_console
    evidence = case['evidence']
    trace = evidence.review(ITEM, FAILED)
    assert trace['legacy']['request_key'] == 'original-logical-request-key'
    assert trace['legacy']['source_state'] == 'historical_failed'
    assert {entry['role'] for entry in trace['legacy']['artifacts']} == {
        'legacy-request', 'legacy-result', 'legacy-decision'}
    assert not set(case['legacy_shas'].values()) & set(case['fetched'])
    assert trace['texts'] == [] and trace['images'] == [] and trace['response'] is None
    assert {entry['field'] for entry in trace['missing']} == {'request', 'response', 'receipt'}
    with pytest.raises(ReviewUnavailable):
        evidence.artifact(ITEM, FAILED, 'request')
    with pytest.raises(ReviewUnavailable):
        evidence.prepared_image(ITEM, FAILED, 0)
    for role, expected in case['legacy_bytes'].items():
        assert evidence.artifact(ITEM, FAILED, role) == expected
    with pytest.raises(ReviewNotFound):
        evidence.artifact(OTHER, FAILED, 'legacy-request')
    with pytest.raises(ReviewUnavailable):
        evidence.artifact(ITEM, REVIEW, 'legacy-request')
    assert case['other_sha'] not in case['fetched']


def test_legacy_download_labels_auth_and_attachment_semantics(evidence_console):
    case = evidence_console
    path = f'/console/activities/{ITEM}/reviews/{FAILED}'
    assert case['console'][0].test_client().get(path + '/artifacts/legacy-request').status_code == 302
    assert _client(case['console'], 'member').get(path + '/artifacts/legacy-request').status_code == 401
    client = _client(case['console'])
    html = client.get(path).get_data(as_text=True)
    for label in ('历史请求元数据（非HTTP实体）', '历史解析结果（非提供方原始响应）', '历史判定记录',
                  'original-logical-request-key', 'historical_failed', '不能证明完整'):
        assert label in html
    assert 'LEGACY_REQUEST_METADATA_NOT_HTTP' not in html and '<script>legacy()</script>' not in html
    api = client.get(f'/console/api/activities/{ITEM}/reviews/{FAILED}').get_json()
    assert api['legacy']['source_state'] == 'historical_failed' and api['response'] is None
    for role, expected in case['legacy_bytes'].items():
        response = client.get(f'/console/api/activities/{ITEM}/reviews/{FAILED}/artifacts/{role}')
        assert response.status_code == 200 and response.data == expected
        assert response.headers['Content-Type'].startswith('application/octet-stream')
        assert 'attachment' in response.headers['Content-Disposition'] and role + '.json' in response.headers['Content-Disposition']
        assert response.headers['X-Content-Type-Options'] == 'nosniff'
        assert response.headers['Cache-Control'] == 'private, no-store'
    assert client.get(f'/console/activities/{OTHER}/reviews/{FAILED}/artifacts/legacy-result').status_code == 404


def test_legacy_blob_failure_is_explicit_and_not_an_alternate_wire_fallback(evidence_console, monkeypatch):
    case = evidence_console
    case['blobs'][case['legacy_shas']['legacy-request']].write_bytes(b'corrupt')
    with pytest.raises(ReviewUnavailable, match='校验失败'):
        case['evidence'].artifact(ITEM, FAILED, 'legacy-request')
    trace = case['evidence'].review(ITEM, FAILED)
    assert trace['response'] is None and {entry['field'] for entry in trace['missing']} == {'request', 'response', 'receipt'}


def test_legacy_artifact_uses_same_size_limit_before_fetch(evidence_console):
    case = evidence_console
    digest = case['legacy_shas']['legacy-request']
    with closing(sqlite3.connect(case['root'] / 'foundation.sqlite3')) as db, db:
        db.execute('UPDATE blobs SET size=? WHERE sha256=?', (65 * 1024 * 1024, digest))
    with pytest.raises(ReviewUnavailable, match='上限'):
        EvidenceStore(case['store'].settings).artifact(ITEM, FAILED, 'legacy-request')
    assert digest not in case['fetched']


def test_modern_paid_facts_without_legacy_artifacts_do_not_claim_legacy_capture(evidence_console):
    case = evidence_console
    with closing(sqlite3.connect(case['root'] / 'foundation.sqlite3')) as db, db:
        row = db.execute('SELECT fields_json FROM reviews WHERE review_id=?', (REVIEW,)).fetchone()
        fields = json.loads(row[0])
        fields['paid_facts'] = {'charge': 'unknown'}
        db.execute('UPDATE reviews SET fields_json=? WHERE review_id=?', (json.dumps(fields), REVIEW))
    trace = EvidenceStore(case['store'].settings).review(ITEM, REVIEW)
    assert trace['legacy'] is None and trace['texts'][1]['text'].startswith('EXACT_SENT_TEXT')



@pytest.fixture
def pdf_console(evidence_console):
    case = evidence_console
    originals = {}
    with closing(connect_foundation(case['root'])) as db, db:
        for index, filename in enumerate(('0802.pdf', '<script>private</script>0803.pdf')):
            raw = b'%PDF-1.4\nsynthetic historical original ' + str(index).encode() + b'\n%%EOF\n'
            digest = hashlib.sha256(raw).hexdigest()
            path = case['root'] / ('original-' + digest)
            path.write_bytes(raw)
            path.chmod(0o600)
            db.execute('INSERT INTO blobs(sha256,size,mime,kind,local_path,owned_local) VALUES(?,?,?,?,?,1)',
                       (digest, len(raw), 'application/pdf', 'original', str(path)))
            db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)',
                       (digest, digest, 'pdf', filename, '2026-08-03' if index else '2026-08-02',
                        '2026-08-02', 439, 'parsed'))
            originals[digest] = (raw, path)
    return case, originals


def test_original_pdf_library_preserves_variants_and_downloads_exact_bytes(pdf_console):
    case, originals = pdf_console
    client = _client(case['console'])
    listing = client.get('/console/documents')
    assert listing.status_code == 200
    html = listing.get_data(as_text=True)
    assert '&lt;script&gt;private&lt;/script&gt;0803.pdf' in html
    assert '2026-08-03' in html and '2026-08-02' in html
    assert not set(originals).intersection(case['fetched'])
    for digest, (raw, _) in originals.items():
        assert f'/console/documents/{digest}/pdf' in html
        selected = client.get('/console/documents?q=' + digest).get_data(as_text=True)
        assert digest in selected
        assert all(other not in selected for other in originals if other != digest)
        response = client.get(f'/console/documents/{digest}/pdf')
        assert response.status_code == 200 and response.data == raw
        assert response.mimetype == 'application/pdf'
        assert response.headers['Cache-Control'] == 'private, no-store'
        assert response.headers['X-Content-Type-Options'] == 'nosniff'
        assert response.headers['Content-Disposition'].startswith('attachment;')


def test_original_pdfs_require_editor_and_catalogued_document_identity(pdf_console):
    case, originals = pdf_console
    digest = next(iter(originals))
    path = f'/console/documents/{digest}/pdf'
    assert case['console'][0].test_client().get(path).status_code == 302
    member = _client(case['console'], 'member')
    assert member.get('/console/documents').status_code == 401
    assert member.get(path).status_code == 401
    client = _client(case['console'])
    for identity in (case['request_sha'], case['source_sha'], '0' * 64, 'not-a-sha'):
        assert client.get(f'/console/documents/{identity}/pdf').status_code == 404
    assert case['request_sha'] not in case['fetched']


@pytest.mark.parametrize('failure', ['missing', 'corrupt'])
def test_original_pdf_failure_is_not_an_empty_or_replacement_download(pdf_console, failure):
    case, originals = pdf_console
    digest, (raw, path) = next(iter(originals.items()))
    if failure == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'X' * len(raw))
    response = _client(case['console']).get(f'/console/documents/{digest}/pdf')
    assert response.status_code == 503
    assert response.mimetype == 'text/html'




@pytest.mark.parametrize('field', ['reader_data_dir', 'reader_site_data_dir'])
def test_public_symlink_cannot_expose_private_foundation(evidence_console, tmp_path, field):
    case = evidence_console
    public = tmp_path / 'public-alias'
    public.symlink_to(case['root'], target_is_directory=True)
    settings = replace(case['store'].settings, **{field: public})
    with pytest.raises(ReviewUnavailable):
        EvidenceStore(settings).documents()


@pytest.mark.skipif(shutil.which('restic') is None, reason='native restic unavailable')
def test_archive_initialization_preserves_existing_private_review_links(evidence_console, tmp_path):
    from nannanews.data_foundation_archive import configure_archive

    case = evidence_console
    link = case['store'].associate_review(ITEM, UNKNOWN_REVIEW, actor='editor')
    password = tmp_path / 'archive-password'
    password.write_text('synthetic test password', encoding='utf-8')
    password.chmod(0o600)
    configure_archive(case['root'], repository=tmp_path / 'native-repository',
                      password_file=password, restic_binary=shutil.which('restic'))

    store = ReviewStore(case['store'].settings)
    assert store.review_associated(ITEM, UNKNOWN_REVIEW)
    assert any(row['review_id'] == UNKNOWN_REVIEW for row in store.list_reviews(ITEM)['associated'])
    trace = store.evidence().review(ITEM, UNKNOWN_REVIEW, associated=True, dataset=link['dataset_id'])
    assert trace['activity_id'] is None and 'UNKNOWN_IDENTITY_ORIGINAL' in trace['judgment']


def test_history_reader_refuses_old_schema_without_migrating(evidence_console):
    case = evidence_console
    with closing(sqlite3.connect(case['root'] / 'foundation.sqlite3')) as db, db:
        db.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
    before = (case['root'] / 'foundation.sqlite3').read_bytes()
    with pytest.raises(ReviewUnavailable):
        case['evidence'].documents()
    assert (case['root'] / 'foundation.sqlite3').read_bytes() == before
