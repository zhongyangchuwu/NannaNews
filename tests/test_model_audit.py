"""Actual wire receipts with socket/provider and OS disk failure boundaries faked."""
import base64
from copy import deepcopy
import hashlib
import http.client
import io
import json
from pathlib import Path
import stat

from types import SimpleNamespace

import pytest



from nannanews import acquisition
from nannanews import model_audit
from test_acquisition_media import image_bytes
from nannanews import bulletin_date_repair as repair
from nannanews.bulletin_historical_attempts import AttemptStore
from test_bulletin_date_repair import (SOURCE_URL, _fetched_document, _model_response,
    database, reference_response, source_data_from_payload)


def envelope(output):
    return {'id': 'response-123', 'model': 'actual-model', 'usage': {'input_tokens': 12,
        'output_tokens': 5, 'output_tokens_details': {'reasoning_tokens': 2}},
        'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}]}


class Response:
    def __init__(self, body, status=200, headers=None, interrupt=False):
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.interrupt = interrupt
        self.reads = 0

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def read1(self, size):
        self.reads += 1
        if self.interrupt and self.reads > 1:
            raise ConnectionResetError('private provider URL and debug content')
        return self.body.read(size)

    def close(self):
        self.body.close()


class Connection:
    sock = None

    def __init__(self, response):
        self.response = response
        self.sent = []

    def connect(self):
        pass

    def request(self, method, path, body, headers):
        # Consume file-like HTTP entity exactly as http.client does, not json.dumps.
        chunks = []
        while chunk := body.read(8192):
            chunks.append(chunk)
        raw = b''.join(chunks)
        assert len(raw) == int(headers['Content-Length'])
        self.sent.append((method, path, raw, headers))

    def getresponse(self):
        return self.response

    def close(self):
        pass


@pytest.fixture
def wire(tmp_path, monkeypatch):
    settings = SimpleNamespace(model_base_url='https://private-provider.invalid/v1',
        model_api_key='secret-key-never-archive', model_name='configured-model', model_timeout=5,
        model_api_style='chat_completions', data_dir=tmp_path)
    monkeypatch.setattr(acquisition.runtime, 'settings', settings)
    connection = Connection(Response(json.dumps(envelope({'accepted': True})).encode()))
    monkeypatch.setattr(acquisition, '_resolve_addresses', lambda *args: ['127.0.0.1'])
    monkeypatch.setattr(acquisition, '_PinnedHTTPS', lambda *args: connection)
    return settings, connection


def receipt(settings):
    path, = (settings.data_dir / 'model-audit').glob('wire-*/receipt.json')
    return path, json.loads(path.read_text())


@pytest.mark.parametrize('style', ['chat_completions', 'responses'])
def test_exact_request_prepared_images_and_response_are_private_and_durable_before_admission(wire, style):
    settings, connection = wire
    settings.model_api_style = style
    output = {'accepted': True}
    response = envelope(output)
    if style == 'responses':
        response.update(status='completed', output=[{'type': 'message', 'role': 'assistant',
            'status': 'completed', 'content': [{'type': 'output_text', 'text': json.dumps(output)}]}])
    raw_response = (' \n' + json.dumps(response, indent=2) + '\n').encode()
    connection.response = Response(raw_response, headers={'X-Request-ID': 'req-123',
        'Authorization': 'secret-key-never-archive', 'Set-Cookie': 'private-cookie',
        'Location': 'https://private-provider.invalid/private'})
    original = acquisition._image(image_bytes(), 'https://public.example/poster.png')
    preserved = deepcopy(original)
    admitted = []

    def before_send():
        path, metadata = receipt(settings)
        request = (path.parent / 'request.json').read_bytes()
        assert metadata['phase'] == 'prepared'
        assert metadata['request']['sha256'] == hashlib.sha256(request).hexdigest()
        assert (path.parent / 'response.bin').stat().st_size == acquisition.MAX_MODEL_BODY + 1
        assert connection.sent == []
        admitted.append(True)

    schema = {'type': 'object', 'properties': {'accepted': {'type': 'boolean'}},
        'required': ['accepted'], 'additionalProperties': False}
    result, name = acquisition.request_model_json('Private instructions 正文', {'text': 'literal\n原文'},
        [original], response_schema=schema, before_send=before_send)
    assert result == output and name == settings.model_name and admitted == [True]
    assert original == preserved
    path, metadata = receipt(settings)
    sent = connection.sent[0][2]
    assert (path.parent / 'request.json').read_bytes() == sent
    assert (path.parent / 'response.bin').read_bytes() == raw_response
    assert metadata['response']['sha256'] == hashlib.sha256(raw_response).hexdigest()
    assert metadata['response']['complete'] is True
    assert metadata['upload_complete'] is True
    timing = metadata['network_timing']
    assert timing['budget_seconds'] == settings.model_timeout
    assert timing['elapsed_seconds'] >= 0
    assert set(timing['phases_seconds']) == {'dns', 'connect', 'upload', 'response_headers', 'response_body'}
    assert all(seconds >= 0 for seconds in timing['phases_seconds'].values())
    assert sum(timing['phases_seconds'].values()) == pytest.approx(timing['elapsed_seconds'])
    assert 'timeout_stage' not in timing
    assert metadata['http_status'] == 200
    assert metadata['diagnostic_headers'] == {'x-request-id': 'req-123'}
    assert metadata['provider_response']['model'] == 'actual-model'
    assert metadata['provider_response']['usage']['input_tokens'] == 12
    assert metadata['context']['instructions_sha256'] == hashlib.sha256('Private instructions 正文'.encode()).hexdigest()
    assert [event['phase'] for event in metadata['lifecycle']] == [
        'prepared', 'sending', 'response_headers', 'response_archived', 'transport_complete', 'output_parsed']
    payload = json.loads(sent)
    if style == 'responses':
        assert payload['text']['format'] == dict(type='json_schema', name='historical_evidence_review', strict=True, schema=schema)
        image_url = payload['input'][0]['content'][1]['image_url']
    else:
        assert payload['response_format']['json_schema']['schema'] == schema
        assert payload['response_format']['json_schema']['strict'] is True
        image_url = payload['messages'][1]['content'][1]['image_url']['url']
    jpeg = base64.b64decode(image_url.split(',', 1)[1], validate=True)
    assert jpeg.startswith(b'\xff\xd8') and jpeg != base64.b64decode(original['data'])
    for file in path.parent.iterdir():
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
        assert b'secret-key-never-archive' not in file.read_bytes()
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert len(connection.sent) == 1


@pytest.mark.parametrize(('status', 'body', 'code'), [
    (429, b'private upstream body and secret-key-never-archive', 'model_http_error'),
    (500, b'private upstream body and secret-key-never-archive', 'model_http_error'),
    (503, b'private upstream body and secret-key-never-archive', 'model_http_error'),
    (200, b'\xffnot valid JSON', 'model_response_invalid'),
    (200, json.dumps(envelope({'not': 'important'})).replace('stop', 'length').encode(), 'model_output_invalid'),
])
def test_failure_bytes_survive_without_public_provider_dump_or_paid_retry(wire, status, body, code):
    settings, connection = wire
    connection.response = Response(body, status=status)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.request_model_json('JSON', {})
    assert caught.value.code == code
    assert 'private upstream' not in str(caught.value)
    assert settings.model_api_key not in str(caught.value)
    assert settings.model_base_url not in str(caught.value)
    path, metadata = receipt(settings)
    assert (path.parent / 'response.bin').read_bytes() == body
    assert metadata['response']['sha256'] == hashlib.sha256(body).hexdigest()
    assert len(connection.sent) == 1


def test_partial_transport_response_is_preserved_and_not_resent(wire):
    settings, connection = wire
    connection.response = Response(b'partial private bytes', interrupt=True)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.request_model_json('JSON', {})
    assert caught.value.code == 'model_unavailable'
    path, metadata = receipt(settings)
    assert (path.parent / 'response.bin').read_bytes() == b'partial private bytes'
    assert metadata['response']['complete'] is False
    assert len(connection.sent) == 1


def test_oversize_response_retains_only_bounded_original_prefix(wire, monkeypatch):
    settings, connection = wire
    monkeypatch.setattr(acquisition, 'MAX_MODEL_BODY', 16)
    connection.response = Response(b'x' * 80, headers={'Content-Length': '80'})
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.request_model_json('JSON', {})
    assert caught.value.code == 'model_response_invalid'
    path, metadata = receipt(settings)
    assert (path.parent / 'response.bin').read_bytes() == b'x' * 17
    assert metadata['response']['bounded_prefix'] is True
    assert metadata['response']['complete'] is False
    assert len(connection.sent) == 1


@pytest.mark.parametrize('failure', ['disk_reservation', 'request_fsync', 'symlink'])
def test_audit_preparation_failure_precedes_paid_admission_and_send(wire, monkeypatch, tmp_path, failure):
    settings, connection = wire
    calls = []
    if failure == 'disk_reservation':
        monkeypatch.setattr(model_audit.os, 'posix_fallocate', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    elif failure == 'request_fsync':
        monkeypatch.setattr(model_audit.os, 'fsync', lambda *args: (_ for _ in ()).throw(OSError('disk failed')))
    else:
        (tmp_path / 'elsewhere').mkdir()
        (tmp_path / 'model-audit').symlink_to(tmp_path / 'elsewhere', target_is_directory=True)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.request_model_json('JSON', {}, before_send=lambda: calls.append('reserved'))
    assert caught.value.code == 'model_audit_failed'
    assert calls == [] and connection.sent == []


@pytest.mark.parametrize('phase', ['response_headers', 'response_archived', 'transport_complete', 'output_parsed'])
def test_receipt_write_failure_after_send_never_discards_original_response(wire, monkeypatch, phase):
    settings, connection = wire
    original_save = model_audit.ModelAudit.save

    def save(self, current=None, **kwargs):
        if current == phase:
            raise OSError('private audit disk diagnostic')
        return original_save(self, current, **kwargs)

    monkeypatch.setattr(model_audit.ModelAudit, 'save', save)
    expected = connection.response.body.getvalue()
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.request_model_json('JSON', {})
    assert caught.value.code == 'model_audit_failed'
    path, _ = receipt(settings)
    assert (path.parent / 'response.bin').read_bytes() == expected
    assert len(connection.sent) == 1


@pytest.mark.parametrize('failure', ['none', 'evidence', 'ledger', 'finalize'])
def test_historical_wire_receipt_survives_validation_and_ledger_failures_without_resending(
        wire, database, monkeypatch, tmp_path, failure):
    settings, connection = wire
    rows, originals, _ = repair.date_snapshot(database)
    occurrence = rows[0]
    attempt_store = AttemptStore(tmp_path / 'evidence.sqlite3')
    cache = {SOURCE_URL: {'document': _fetched_document(), 'media': []}}
    answers = []

    def answer():
        payload = json.loads(connection.sent[-1][2])
        data = source_data_from_payload(payload)
        output = reference_response(_model_response(), data)
        if failure == 'evidence':
            output['activity']['content']['evidence'][0]['ref'] = 'invented-reference'
        raw = json.dumps(envelope(output), ensure_ascii=False, indent=2).encode()
        answers.append(raw)
        return Response(raw)

    monkeypatch.setattr(connection, 'getresponse', answer)
    if failure == 'ledger':
        def finish(*args, **kwargs):
            raise OSError('private ledger disk failure')
        monkeypatch.setattr(attempt_store, 'finish', finish)
    elif failure == 'finalize':
        def finalize(*args, **kwargs):
            raise OSError('private audit metadata disk failure')
        monkeypatch.setattr(model_audit, 'finalize_receipt', finalize)
    first = repair.model_decision(occurrence, originals[occurrence['id']],
        source_cache=cache, attempt_store=attempt_store)
    identity = first['_model_audit']
    path = Path(identity['receipt'])
    assert path.parent.parent == attempt_store.audit_directory
    metadata = json.loads(path.read_text())
    assert (path.parent / 'response.bin').read_bytes() == answers[0]
    assert metadata['context']['logical_request_sha256'] != identity['request_sha256']
    attempt = attempt_store.lookup(metadata['context']['logical_request_sha256'])
    assert json.loads(attempt['request_json'])['rules_version'] == repair.RULES_VERSION
    frozen = deepcopy(attempt)
    if failure == 'none':
        assert first['source_review']['status'] == 'source_verified'
        assert metadata['review_outcome'] == 'validated'
        assert attempt['state'] == 'completed'
    else:
        assert first['_paid_failure'] is True and first['status'] == 'unresolved'
        assert first['_model_failure_code'] == ('model_evidence_invalid' if failure == 'evidence' else 'model_audit_failed')
        assert attempt['state'] == ('reserved' if failure == 'ledger' else 'failed')
    second = repair.model_decision(occurrence, originals[occurrence['id']],
        source_cache=cache, attempt_store=attempt_store)
    assert len(connection.sent) == len(answers) == 1
    assert attempt_store.lookup(metadata['context']['logical_request_sha256']) == frozen
    if failure != 'none':
        assert second['_request_state'] == attempt['state']


def test_request_bound_rejection_never_reaches_paid_admission_or_send(wire, monkeypatch):
    settings, connection = wire
    monkeypatch.setattr(acquisition, 'MAX_MODEL_REQUEST_BYTES', 100)
    calls = []
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.request_model_json('JSON' * 100, {}, before_send=lambda: calls.append(True))
    assert caught.value.code == 'model_input_too_large'
    assert calls == [] and connection.sent == []


def test_incomplete_read_exception_preserves_its_original_partial_bytes(wire, monkeypatch):
    settings, connection = wire
    calls = []

    def read(size):
        calls.append(size)
        raise http.client.IncompleteRead(b'original partial bytes', 10)

    monkeypatch.setattr(connection.response, 'read1', read)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.request_model_json('JSON', {})
    assert caught.value.code == 'model_response_invalid'
    path, metadata = receipt(settings)
    assert (path.parent / 'response.bin').read_bytes() == b'original partial bytes'
    assert metadata['response']['complete'] is False
    assert len(connection.sent) == len(calls) == 1


def test_admission_cancellation_sends_nothing_and_releases_reserved_response_disk(wire):
    settings, connection = wire

    def cancel():
        raise RuntimeError('admission cancelled before reservation')

    with pytest.raises(RuntimeError, match='admission cancelled'):
        acquisition.request_model_json('JSON', {}, before_send=cancel)
    path, metadata = receipt(settings)
    assert (path.parent / 'response.bin').stat().st_size == 0
    assert metadata['response']['bytes'] == 0
    assert metadata['phase'] == 'failed'
    assert connection.sent == []


def test_strict_schema_provider_refusal_never_downgrades_or_resends(wire):
    settings, connection = wire
    body = b'{"error":"json_schema not supported; private upstream diagnostic"}'
    connection.response = Response(body, status=400)
    schema = {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.request_model_json('JSON', {}, response_schema=schema)
    assert caught.value.code == 'model_http_error'
    assert 'private upstream diagnostic' not in str(caught.value)
    assert len(connection.sent) == 1
    payload = json.loads(connection.sent[0][2])
    assert payload['response_format']['type'] == 'json_schema'
    assert payload['response_format']['json_schema']['strict'] is True
    path, metadata = receipt(settings)
    assert metadata['http_status'] == 400
    assert (path.parent / 'response.bin').read_bytes() == body


@pytest.mark.parametrize('status', [200, 429])
@pytest.mark.parametrize('declared_length', ['too_long', 'invalid'])
def test_plain_eof_with_invalid_declared_length_is_incomplete_before_first_archive(
        wire, monkeypatch, status, declared_length):
    settings, connection = wire
    body = json.dumps(envelope({'accepted': True})).encode() if status == 200 else b'original private error body'
    length = str(len(body) + 10) if declared_length == 'too_long' else 'not-a-length'
    connection.response = Response(body, status=status, headers={'Content-Length': length})
    archived_completeness = []
    preserve = model_audit.ModelAudit.preserve_response

    def archive(self, *, complete):
        archived_completeness.append(complete)
        return preserve(self, complete=complete)

    monkeypatch.setattr(model_audit.ModelAudit, 'preserve_response', archive)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.request_model_json('JSON', {})
    assert caught.value.code == ('model_response_invalid' if status == 200 else 'model_http_error')
    path, metadata = receipt(settings)
    assert (path.parent / 'response.bin').read_bytes() == body
    assert metadata['http_status'] == status
    assert metadata['response']['complete'] is False
    assert archived_completeness and all(value is False for value in archived_completeness)
    assert len(connection.sent) == 1
