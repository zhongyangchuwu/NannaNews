"""Consumer-visible model deadlines against loopback only; never a paid provider."""
from contextlib import contextmanager
import hashlib
import http.client
import json
import socket
import socketserver
from threading import Event, Lock, Thread
import time
from types import SimpleNamespace

import pytest

from nannanews import acquisition


OUTPUT = {'accepted': True}
RESPONSE = (' \n' + json.dumps({'status': 'completed', 'output': [{'type': 'message',
    'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text',
    'text': json.dumps(OUTPUT)}]}]}, indent=2) + '\n').encode()
PREFIX = b'prefix!'
CHUNK_ENDINGS = {
    'terminal_no_crlf': b'\r\n0',
    'trailer_eof': b'\r\n0\r\nX-Trace: interrupted',
    'trailer_terminator_eof': b'\r\n0\r\nX-Trace: present\r\n',
    'delimiter_eof': b'\r',
    'chunk_complete': b'\r\n0\r\n\r\n',
    'chunk_trailer_complete': b'\r\n0;done=1\r\nX-Trace: complete\r\n\r\n',
}


@contextmanager
def model_server(mode):
    stop, done = Event(), Event()
    lock = Lock()
    state = {'connections': 0, 'requests': [], 'errors': []}

    class Handler(socketserver.StreamRequestHandler):
        def trickle(self, data):
            # Progress beats an inactivity timeout, but not an absolute budget.
            for byte in data:
                if stop.wait(0.02):
                    return
                self.request.sendall(bytes([byte]))

        def handle(self):
            self.request.settimeout(2)
            with lock:
                state['connections'] += 1
            try:
                if mode == 'tls':
                    # Receive a real ClientHello, then leave the handshake pending.
                    state['client_hello'] = self.rfile.read(5)
                    stop.wait(2)
                    return
                line = self.rfile.readline(8192)
                if not line:
                    return
                headers = http.client.parse_headers(self.rfile)
                record = {'line': line, 'headers': headers, 'body': bytearray()}
                state['requests'].append(record)
                length = int(headers['Content-Length'])
                while len(record['body']) < length:
                    chunk = self.rfile.read1(min(4096, length - len(record['body'])))
                    if not chunk:
                        return
                    record['body'].extend(chunk)
                    if mode == 'upload' and stop.wait(0.02):
                        return
                if mode == 'headers':
                    self.request.sendall(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                    self.trickle(b'x' * 160 + b'\r\nContent-Length: 2\r\n\r\n{}')
                elif mode in CHUNK_ENDINGS:
                    self.request.sendall(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n'
                        b'Connection: close\r\n\r\n' + f'{len(RESPONSE):x}\r\n'.encode()
                        + RESPONSE + CHUNK_ENDINGS[mode])
                elif mode in {'chunk_header', 'chunk_trailer', 'combined'}:
                    if mode == 'combined' and stop.wait(0.12):
                        return
                    self.request.sendall(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n'
                        b'Connection: close\r\n\r\n7\r\n' + PREFIX + b'\r\n')
                    if mode == 'combined' and stop.wait(0.12):
                        return
                    if mode == 'chunk_trailer':
                        self.request.sendall(b'0\r\n')
                        self.trickle(b'X-Slow: ' + b'x' * 160 + b'\r\n\r\n')
                    else:
                        self.trickle(b'0' * 160 + b'1\r\nx\r\n0\r\n\r\n')
                elif mode == 'body':
                    self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n'
                        b'Connection: close\r\n\r\n' + PREFIX)
                    self.trickle(b'x' * 160)
                else:
                    self.request.sendall(f'HTTP/1.1 200 OK\r\nContent-Length: {len(RESPONSE)}\r\n'
                        'Connection: close\r\n\r\n'.encode() + RESPONSE)
            except OSError:
                pass  # Deadline cancellation closes the client mid-read/write.
            except Exception as error:
                state['errors'].append(error)
            finally:
                done.set()

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

        def server_bind(self):
            # Keep the real TCP receive window bounded so upload cannot all queue.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
            super().server_bind()

    server = Server(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()  # Constructor has already bound/listened: readiness is observable.
    try:
        yield server.server_address[1], state
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive(), 'local model server did not stop'
        if state['connections']:
            assert done.wait(3), 'local model connection did not close'
        assert not state['errors'], state['errors']


def configure(monkeypatch, tmp_path, port, budget, *, secure=False, dns_delay=0):
    settings = SimpleNamespace(model_base_url=f'{"https" if secure else "http"}://model.invalid:{port}/v1',
        model_api_key='test-only-key', model_name='configured-model', model_timeout=budget,
        model_api_style='responses', data_dir=tmp_path)
    monkeypatch.setattr(acquisition.runtime, 'settings', settings)

    def resolve(*args):
        if dns_delay:
            # Deterministic DNS-stage latency; no external lookup is performed.
            Event().wait(dns_delay)
        return ['127.0.0.1']

    monkeypatch.setattr(acquisition, '_resolve_addresses', resolve)
    return settings


def archived(settings, error=None):
    path, = (settings.data_dir / 'model-audit').glob('wire-*/receipt.json')
    if error is not None:
        assert error._model_audit['receipt'] == str(path)
    metadata = json.loads(path.read_text())
    request = (path.parent / 'request.json').read_bytes()
    response = (path.parent / 'response.bin').read_bytes()
    assert metadata['request']['bytes'] == len(request)
    assert metadata['request']['sha256'] == hashlib.sha256(request).hexdigest()
    assert metadata['response']['bytes'] == len(response)
    assert metadata['response']['sha256'] == hashlib.sha256(response).hexdigest()
    return metadata, request, response


def assert_single_request(state, request, *, complete=True):
    assert state['connections'] == len(state['requests']) == 1
    record = state['requests'][0]
    assert record['line'] == b'POST /v1/responses HTTP/1.1\r\n'
    assert int(record['headers']['Content-Length']) == len(request)
    received = bytes(record['body'])
    assert received == (request if complete else request[:len(received)])
    if not complete:
        assert 0 < len(received) < len(request)
    assert record['headers']['Authorization'] == 'Bearer test-only-key'
    assert record['headers']['Proxy-Authorization'] is None


def test_real_consumer_archives_exact_upload_and_original_response_before_admission(monkeypatch, tmp_path):
    with model_server('success') as (port, state):
        settings = configure(monkeypatch, tmp_path, port, 2)
        admitted = []

        def before_send():
            path, = (tmp_path / 'model-audit').glob('wire-*/receipt.json')
            metadata = json.loads(path.read_text())
            assert metadata['phase'] == 'prepared'
            assert 'network_timing' not in metadata and 'upload_complete' not in metadata
            assert (path.parent / 'request.json').stat().st_size == metadata['request']['bytes']
            assert (path.parent / 'response.bin').stat().st_size == acquisition.MAX_MODEL_BODY + 1
            assert state['connections'] == 0
            admitted.append(True)

        # An inherited source proxy must never reroute this model POST.
        monkeypatch.setattr(acquisition, 'getproxies_environment',
            lambda: pytest.fail('model transport inspected source proxy configuration'))
        output, name = acquisition.request_model_json('Return JSON 正文', {'text': 'literal\ncontent'},
            before_send=before_send)
        assert output == OUTPUT and name == settings.model_name and admitted == [True]
        metadata, request, response = archived(settings)
        assert_single_request(state, request)
        assert state['requests'][0]['headers']['Host'] == f'model.invalid:{port}'
        assert response == RESPONSE and metadata['response']['complete'] is True
        assert metadata['upload_complete'] is True
        assert 'timeout_stage' not in metadata['network_timing']
        assert b'test-only-key' not in json.dumps(metadata).encode()


def test_progressive_upload_uses_one_budget_and_keeps_full_prepared_request(monkeypatch, tmp_path):
    with model_server('upload') as (port, state):
        budget = 0.4
        settings = configure(monkeypatch, tmp_path, port, budget)
        connect = socket.create_connection

        def bounded_send_buffer(*args, **kwargs):
            sock = connect(*args, **kwargs)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
            return sock

        monkeypatch.setattr(socket, 'create_connection', bounded_send_buffer)
        started = []
        with pytest.raises(acquisition.AcquisitionError) as caught:
            acquisition.request_model_json('Return JSON', {'text': 'x' * (1024 * 1024)},
                before_send=lambda: started.append(time.monotonic()))
        assert time.monotonic() - started[0] < budget + 0.35
        assert caught.value.code == 'model_unavailable'
        metadata, request, response = archived(settings, caught.value)
        assert_single_request(state, request, complete=False)
        assert b'x' * (1024 * 1024) in request
        assert metadata['upload_complete'] is False
        assert metadata['response']['complete'] is False and response == b''
        assert metadata['network_timing']['timeout_stage'] == 'upload'


@pytest.mark.parametrize(('mode', 'stage'), [
    ('headers', 'response_headers'), ('chunk_header', 'response_body'),
    ('chunk_trailer', 'response_body'), ('body', 'response_body'),
    ('combined', 'response_body'),
])
def test_progressive_response_framing_and_combined_stages_share_deadline(monkeypatch, tmp_path, mode, stage):
    with model_server(mode) as (port, state):
        budget = 0.5 if mode == 'combined' else 0.3
        settings = configure(monkeypatch, tmp_path, port, budget,
            dns_delay=0.12 if mode == 'combined' else 0)
        started = []
        with pytest.raises(acquisition.AcquisitionError) as caught:
            acquisition.request_model_json('Return JSON', {},
                before_send=lambda: started.append(time.monotonic()))
        assert time.monotonic() - started[0] < budget + 0.35
        assert caught.value.code == 'model_unavailable'
        metadata, request, response = archived(settings, caught.value)
        assert_single_request(state, request)
        assert metadata['phase'] == 'failed'
        assert metadata['upload_complete'] is True and metadata['response']['complete'] is False
        timing = metadata['network_timing']
        assert timing['timeout_stage'] == stage
        assert budget * 0.8 <= timing['elapsed_seconds'] < budget + 0.35
        assert sum(timing['phases_seconds'].values()) == pytest.approx(timing['elapsed_seconds'])
        if mode == 'headers':
            assert response == b'' and 'http_status' not in metadata
        elif mode == 'body':
            assert response.startswith(PREFIX)
            assert response[len(PREFIX):] == b'x' * (len(response) - len(PREFIX))
        else:
            assert response == PREFIX
        if mode != 'headers':
            assert metadata['http_status'] == 200
        if mode == 'combined':
            assert timing['phases_seconds']['dns'] >= 0.12
            assert timing['phases_seconds']['response_headers'] >= 0.12
            assert timing['phases_seconds']['response_body'] < budget


def test_tcp_and_real_tls_handshake_share_remaining_budget(monkeypatch, tmp_path):
    with model_server('tls') as (port, state):
        budget = 0.5
        settings = configure(monkeypatch, tmp_path, port, budget, secure=True)
        connect = socket.create_connection

        def delayed_tcp(*args, **kwargs):
            sock = connect(*args, **kwargs)
            # Represent time already spent connecting, before TLS can start.
            Event().wait(0.3)
            return sock

        monkeypatch.setattr(socket, 'create_connection', delayed_tcp)
        started = []
        with pytest.raises(acquisition.AcquisitionError) as caught:
            acquisition.request_model_json('Return JSON', {},
                before_send=lambda: started.append(time.monotonic()))
        assert time.monotonic() - started[0] < budget + 0.2
        assert caught.value.code == 'model_unavailable'
        metadata, _, response = archived(settings, caught.value)
        assert state['connections'] == 1 and state['requests'] == []
        assert state['client_hello'].startswith(b'\x16\x03')
        assert metadata['upload_complete'] is False and response == b''
        assert metadata['response']['complete'] is False
        assert metadata['network_timing']['timeout_stage'] == 'connect'


@pytest.mark.parametrize('mode', [
    'terminal_no_crlf', 'trailer_eof', 'trailer_terminator_eof', 'delimiter_eof',
])
def test_chunk_framing_eof_rejects_valid_json_and_preserves_only_entity(monkeypatch, tmp_path, mode):
    with model_server(mode) as (port, state):
        settings = configure(monkeypatch, tmp_path, port, 2)
        with pytest.raises(acquisition.AcquisitionError) as caught:
            acquisition.request_model_json('Return JSON', {})
        assert caught.value.code == 'model_response_invalid'
        metadata, request, response = archived(settings, caught.value)
        assert_single_request(state, request)
        assert response == RESPONSE
        assert metadata['response']['complete'] is False
        assert metadata['upload_complete'] is True and metadata['http_status'] == 200
        assert metadata['phase'] == 'failed'
        assert 'timeout_stage' not in metadata['network_timing']


@pytest.mark.parametrize('mode', ['chunk_complete', 'chunk_trailer_complete'])
def test_complete_chunked_json_with_optional_trailers_remains_accepted(monkeypatch, tmp_path, mode):
    with model_server(mode) as (port, state):
        settings = configure(monkeypatch, tmp_path, port, 2)
        output, name = acquisition.request_model_json('Return JSON', {})
        assert output == OUTPUT and name == settings.model_name
        metadata, request, response = archived(settings)
        assert_single_request(state, request)
        assert response == RESPONSE
        assert metadata['response']['complete'] is True
