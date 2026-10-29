"""Consumer-visible registered reviews with synthetic source/provider boundaries."""
from contextlib import closing
from dataclasses import replace
import base64
import hashlib
import json

import pytest

from nannanews import acquisition, runtime
from nannanews.bulletin_historical_attempts import AttemptStore
from nannanews.data_foundation import connect_foundation
from nannanews.reader_activity_review import review_registered_activity
from nannanews.reader_evidence import EvidenceStore
from nannanews.reader_reviews import ReviewStore, ReviewValidationError
from test_reader_reviews import ITEM, payload, store  # noqa: F401 -- synthetic fixture
from test_bulletin_date_repair import (SOURCE_URL, SOURCE_TEXT, _fetched_document,
    _model_response, reference_response, source_data_from_payload)
from test_model_audit import Connection, Response, envelope


@pytest.fixture
def activity_review(store, tmp_path, monkeypatch):
    root = tmp_path / 'foundation'
    with closing(connect_foundation(root)):
        pass
    roots = {'current': root}
    settings = replace(store.settings, reader_evidence_dir=root,
        model_base_url='https://synthetic-provider.invalid/v1', model_api_key='synthetic-secret',
        model_name='synthetic-model', model_api_style='chat_completions', model_timeout=5)
    monkeypatch.setattr(runtime, 'settings', settings)
    store = ReviewStore(settings)
    registered_payload = payload('Explicit private registration')
    registered_payload['status'] = 'needs_review'
    registered_payload['activity']['source_urls'] = [SOURCE_URL]
    registration = store.register_activity('synthetic-registration', registered_payload, actor='registrar')
    connection = Connection(Response(b''))
    monkeypatch.setattr(acquisition, '_resolve_addresses', lambda *args: ['127.0.0.1'])
    monkeypatch.setattr(acquisition, '_PinnedHTTPS', lambda *args: connection)
    fetched = []

    def fetch(url):
        assert url == SOURCE_URL
        fetched.append(url)
        return _fetched_document()

    def response():
        sent = json.loads(connection.sent[-1][2])
        data = source_data_from_payload(sent)
        answer = reference_response(_model_response(), data)
        return Response((' \n' + json.dumps(envelope(answer), ensure_ascii=False, indent=2) + '\n').encode())

    real_fetch = acquisition.fetch_document
    monkeypatch.setattr(acquisition, 'fetch_document', fetch)
    monkeypatch.setattr(connection, 'getresponse', response)
    monkeypatch.setattr(ReviewStore, 'save',
        lambda *args, **kwargs: pytest.fail('evidence collection changed editable activity facts'))
    args = dict(store=store, item_id=registration['item_id'], actor='reviewer',
        allow_fetch=True, allow_paid=True, foundation_root=root)
    return {'store': store, 'registration': registration, 'args': args, 'fetched': fetched,
        'connection': connection, 'roots': roots,
        'initial': registered_payload, 'real_fetch': real_fetch}


def _traces(case, result):
    evidence = EvidenceStore(case['store'].settings)
    return [evidence.review(result['item_id'], identity, associated=True)
            for identity in result['review_ids']]


@pytest.mark.parametrize('fetch,paid', [(False, True), (True, False), (False, False), (1, True)])
def test_authorization_precedes_all_source_model_and_local_writes(activity_review, fetch, paid):
    case = activity_review
    with pytest.raises(ReviewValidationError):
        review_registered_activity(**dict(case['args'], allow_fetch=fetch, allow_paid=paid))
    assert case['fetched'] == [] and case['connection'].sent == []
    assert not (case['roots']['current'] / 'activity-review').exists()
    assert not AttemptStore(case['store'].settings.database_path).path.exists()


def test_exact_wire_and_stable_registration_visible_in_same_dataset(activity_review):
    case = activity_review
    result = review_registered_activity(**case['args'])
    assert result['state'] == 'reviewed' and result['new_request'] is True
    assert result['decision_status'] == 'resolved'
    assert case['fetched'] == [SOURCE_URL] and len(case['connection'].sent) == 1
    assert result['receipt_review_ids'] and result['result_review_ids']
    assert set(result['receipt_review_ids']).isdisjoint(result['result_review_ids'])
    evidence = EvidenceStore(case['store'].settings)
    receipt_id, = result['receipt_review_ids']
    request = evidence.artifact(result['item_id'], receipt_id, 'request', associated=True)
    assert request == case['connection'].sent[0][2]
    receipt = json.loads(evidence.artifact(result['item_id'], receipt_id, 'receipt',
                                         associated=True))
    for key in ('activity_id', 'item_id', 'occurrence_id', 'source_id'):
        assert receipt['context'][key] == case['registration'][key]
    assert receipt['context']['actor'] == 'reviewer'
    assert receipt['request']['sha256'] == hashlib.sha256(request).hexdigest()
    sent_data = source_data_from_payload(json.loads(request))
    assert sent_data['bulletin']['original_date_claims'][0]['normalized_date'] == '2026-10-26'
    trace = evidence.review(result['item_id'], receipt_id, associated=True)
    assert SOURCE_TEXT in trace['response'] and trace['texts'] and trace['missing'] == []
    assert 'synthetic-secret' not in request.decode()
    assert all(case['store'].review_associated(result['item_id'], identity)
               for identity in result['review_ids'])
    assert {row['review_id'] for row in case['store'].list_reviews(result['item_id'])['associated']} == set(result['review_ids'])
    assert case['store'].get_item(result['item_id'])['head'] is None
    assert case['store'].get_item(result['item_id'])['initial_payload'] == case['initial']


def test_duplicate_paid_key_refused_without_replaying_or_reusing_verdict(activity_review):
    case = activity_review
    first = review_registered_activity(**case['args'])
    ledger = AttemptStore(case['store'].settings.database_path)
    with closing(connect_foundation(case['roots']['current'])) as db:
        attempt = db.execute('SELECT request_key FROM attempts').fetchone()[0]
    retained = ledger.lookup(attempt)
    second = review_registered_activity(**case['args'])
    assert second['state'] == 'admission_refused' and second['new_request'] is False
    assert second['receipt_review_ids'] == []
    assert len(case['connection'].sent) == 1 and ledger.lookup(attempt) == retained
    workflow = [json.loads(trace['judgment'])['workflow'] for trace in _traces(case, second)
                if trace['review_id'] in second['result_review_ids']]
    assert workflow == [{'state': 'admission_refused', 'actor': 'reviewer',
        'request_state': retained['state'], 'request_key': attempt, 'admission_code': None}]
    assert first['receipt_review_ids'][0] in {
        row['review_id'] for row in case['store'].list_reviews(first['item_id'])['associated']}


@pytest.mark.parametrize('failure', ['provider', 'invalid_evidence', 'partial_response'])
def test_model_failures_retain_actual_original_wire_and_explicit_state(activity_review, monkeypatch, failure):
    case = activity_review
    if failure == 'provider':
        raw = b'{"error":{"message":"synthetic provider refusal"}}'
        response = lambda: Response(raw, status=403)
    elif failure == 'partial_response':
        raw = b'original incomplete provider prefix'
        response = lambda: Response(raw, interrupt=True)
    else:
        raw = json.dumps(envelope({'not_a_valid_review': True})).encode()
        response = lambda: Response(raw)
    monkeypatch.setattr(case['connection'], 'getresponse', response)
    result = review_registered_activity(**case['args'])
    assert result['state'] == 'model_failed' and result['new_request'] is True
    assert result['receipt_review_ids'] and len(case['connection'].sent) == 1
    evidence = EvidenceStore(case['store'].settings)
    receipt_id, = result['receipt_review_ids']
    assert evidence.artifact(result['item_id'], receipt_id, 'response', associated=True) == raw
    traces = _traces(case, result)
    workflow = [json.loads(trace['judgment'])['workflow'] for trace in traces
                if trace['review_id'] in result['result_review_ids']]
    assert workflow == [{'state': 'model_failed', 'actor': 'reviewer',
        'request_state': None, 'request_key': None, 'admission_code': None}]
    repeated = review_registered_activity(**case['args'])
    assert repeated['state'] == 'admission_refused' and len(case['connection'].sent) == 1


@pytest.mark.parametrize('failure', ['media', 'source_response'])
def test_partial_source_capture_retained_and_visible_without_paid_send(activity_review, monkeypatch, failure):
    case = activity_review
    if failure == 'media':
        document = dict(_fetched_document(), capture_complete=False,
                        capture_errors=['synthetic embedded image unavailable'])
        monkeypatch.setattr(acquisition, 'fetch_document', lambda url: document)
    else:
        def fail(url):
            error = acquisition.AcquisitionError('synthetic parse failure', 'source_empty')
            error.source_response = _fetched_document()
            raise error
        monkeypatch.setattr(acquisition, 'fetch_document', fail)
    result = review_registered_activity(**case['args'])
    assert result['state'] == 'capture_failed' and result['new_request'] is False
    assert case['connection'].sent == []
    assert any(trace['errors'] != '[]' for trace in _traces(case, result))
    originals = list((case['roots']['current'] / 'activity-review' / 'captures').glob('*.json'))
    captures = [json.loads(path.read_text()) for path in originals]
    capture = next(value['capture'] for value in captures if 'capture' in value)
    document = capture['document'] if failure == 'media' else capture['error']['source_response']
    assert document['raw_response_base64'] == _fetched_document()['raw_response_base64']
    assert not AttemptStore(case['store'].settings.database_path).path.exists()


def test_nonregistered_item_and_wrong_root_rejected_before_fetch(activity_review, tmp_path):
    case = activity_review
    with pytest.raises(ReviewValidationError):
        review_registered_activity(**dict(case['args'], item_id=ITEM))
    with pytest.raises(ReviewValidationError):
        review_registered_activity(**dict(case['args'], foundation_root=tmp_path / 'other'))
    assert case['fetched'] == [] and case['connection'].sent == []


def test_public_fetch_guard_blocks_private_dns_before_paid_transport(activity_review, monkeypatch):
    case = activity_review
    monkeypatch.setattr(acquisition, 'fetch_document', case['real_fetch'])
    result = review_registered_activity(**case['args'])
    assert result['state'] == 'capture_failed' and result['new_request'] is False
    assert case['connection'].sent == []
    assert any(trace['errors'] != '[]' for trace in _traces(case, result))


def test_captured_pdf_original_is_linked_to_exact_review(activity_review, monkeypatch):
    case = activity_review
    original = b'%PDF-1.4\nsynthetic-original-pdf-not-reconstructed'
    document = dict(_fetched_document(), pdf_documents=[{
        'canonical_url': SOURCE_URL + '/attachment.pdf',
        'raw_response_base64': base64.b64encode(original).decode(),
        'raw_response_digest': hashlib.sha256(original).hexdigest()}])
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: document)
    result = review_registered_activity(**case['args'])
    assert result['state'] == 'reviewed'
    with closing(connect_foundation(case['roots']['current'])) as db:
        for review_id in result['review_ids']:
            row = db.execute('SELECT b.blob_sha FROM review_snapshots r '
                'JOIN snapshot_blobs b USING(snapshot_id) WHERE r.review_id=? AND b.role=\'pdf\'',
                (review_id,)).fetchone()
            assert row[0] == hashlib.sha256(original).hexdigest()
            snapshot = db.execute('SELECT r.snapshot_id FROM review_snapshots r '
                'JOIN snapshot_blobs b USING(snapshot_id) WHERE r.review_id=? AND b.role=\'pdf\'',
                (review_id,)).fetchone()[0]
            view = EvidenceStore(case['store'].settings).source(result['item_id'], review_id,
                snapshot, associated=True)
            assert 'attachment.pdf' in view['metadata']


def test_historical_unknown_reference_fields_remain_null_not_numeric_ids(activity_review):
    from pathlib import Path
    from nannanews import bulletin_date_repair as repair

    case = activity_review
    decision = repair.model_decision({'item_id': 7, 'id': 9,
        'title': 'Historical entry with unknown portable refs', 'body_text': SOURCE_TEXT,
        'issue_date': None, 'source_urls': [SOURCE_URL]}, [],
        attempt_store=AttemptStore(case['store'].settings.database_path))
    assert decision['status'] == 'resolved'
    metadata = json.loads(Path(decision['_model_audit']['receipt']).read_text())
    assert {key: metadata['context'][key] for key in
        ('activity_id', 'item_id', 'occurrence_id', 'source_id')} == {
        'activity_id': None, 'item_id': None, 'occurrence_id': None, 'source_id': None}



def test_html_poster_failure_remains_navigable_without_paid_transport(activity_review, monkeypatch):
    case = activity_review
    poster_url = SOURCE_URL + '/poster.png'
    raw = b'<html><body>Captured poster server error, not an image</body></html>'
    document = dict(_fetched_document(), images=[poster_url])
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: document)
    def resource(url, *args, **kwargs):
        assert url == poster_url
        return raw, 'text/html', poster_url
    monkeypatch.setattr(acquisition, 'fetch_public_resource', resource)
    result = review_registered_activity(**case['args'])
    assert result['state'] == 'preparation_failed' and result['new_request'] is False
    assert case['connection'].sent == [] and result['receipt_review_ids'] == []
    traces = _traces(case, result)
    result_trace, = [trace for trace in traces if trace['review_id'] in result['result_review_ids']]
    assert json.loads(result_trace['judgment'])['workflow']['state'] == 'preparation_failed'
    associated = case['store'].list_reviews(result['item_id'])['associated']
    assert result_trace['review_id'] in {row['review_id'] for row in associated}
    with closing(connect_foundation(case['roots']['current'])) as db:
        rows = db.execute('SELECT DISTINCT b.sha256,b.local_path,b.kind,s.role FROM blobs b '
            'JOIN snapshot_blobs s ON s.blob_sha=b.sha256 WHERE b.sha256=?',
            (hashlib.sha256(raw).hexdigest(),)).fetchall()
        assert [(row['kind'], row['role']) for row in rows] == [('source_failed_media', 'failed_media')]
        from pathlib import Path
        assert Path(rows[0]['local_path']).read_bytes() == raw
    assert not AttemptStore(case['store'].settings.database_path).path.exists()
