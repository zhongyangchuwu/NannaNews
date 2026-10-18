"""Absolute source deadlines cover HTTP framing, not just socket inactivity."""
from contextlib import contextmanager
import base64
import hashlib
import http.client
import socket
import socketserver
from threading import Event, Lock, Thread
import time

import pytest

from nannanews import acquisition
from test_model_deadlines import model_server


SOURCE_ENTITY = b'original public entity'
CHUNK_ENDINGS = {
    'terminal_no_crlf': b'\r\n0',
    'trailer_eof': b'\r\n0\r\nX-Trace: interrupted',
    'trailer_terminator_eof': b'\r\n0\r\nX-Trace: present\r\n',
    'delimiter_eof': b'\r',
    'chunk_complete': b'\r\n0\r\n\r\n',
    'chunk_trailer_complete': b'\r\n0;done=1\r\nX-Trace: complete\r\n\r\n',
}


@contextmanager
def trickling_server(stage, *, status=200):
    stop = Event()
    lock = Lock()
    accepted = [0]

    class Handler(socketserver.BaseRequestHandler):
        def headers(self):
            request = b''
            while b'\r\n\r\n' not in request:
                chunk = self.request.recv(4096)
                if not chunk:
                    return b''
                request += chunk
            return request

        def trickle(self, value):
            # Deliberately beat the inactivity timeout while exceeding the total deadline.
            for byte in value:
                if stop.wait(0.015):
                    break
                self.request.sendall(bytes([byte]))

        def handle(self):
            self.request.settimeout(2)
            with lock:
                accepted[0] += 1
                first = accepted[0] == 1
            try:
                request = self.headers()
                if request.startswith(b'CONNECT '):
                    if stage == 'connect' and first:
                        self.trickle(b'HTTP/1.1 200 Connection established\r\nX-Slow: ' + b'x' * 160 + b'\r\n\r\n')
                        return
                    self.request.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                    request = self.headers()
                if b' /fast ' in request:
                    self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok')
                elif stage == 'headers':
                    self.trickle(b'HTTP/1.1 200 OK\r\nX-Slow: ' + b'x' * 160 + b'\r\nContent-Length: 2\r\n\r\nok')
                elif stage in CHUNK_ENDINGS:
                    self.request.sendall(f'HTTP/1.1 {status} Result\r\nTransfer-Encoding: chunked\r\n'
                        f'Connection: close\r\n\r\n{len(SOURCE_ENTITY):x}\r\n'.encode()
                        + SOURCE_ENTITY + CHUNK_ENDINGS[stage])
                else:
                    self.request.sendall(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n')
                    self.trickle(b'0' * 160 + b'1\r\nx\r\n0\r\n\r\n')
            except OSError:
                pass  # The deadline closes the client while the fixture is still emitting bytes.

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = Server(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive(), 'local public server did not stop'


@pytest.mark.parametrize('stage', ['headers', 'chunks', 'connect'])
def test_trickling_framing_expires_and_releases_source_slots(monkeypatch, stage):
    with trickling_server(stage) as port:
        monkeypatch.setattr(acquisition, '_public_addresses', lambda *args: ['93.184.216.34'])
        if stage == 'connect':
            monkeypatch.setattr(acquisition, 'getproxies_environment', lambda: {'http': f'http://127.0.0.1:{port}'})
            monkeypatch.setattr(acquisition, '_resolve_addresses', lambda *args: ['127.0.0.1'])
        else:
            monkeypatch.setattr(acquisition, 'getproxies_environment', lambda: {})
            monkeypatch.setattr(acquisition, '_PinnedHTTP',
                                lambda host, target_port, address, timeout: http.client.HTTPConnection('127.0.0.1', port, timeout=timeout))
        limiter = acquisition.PublicFetchLimiter(max_concurrent=1)
        started = time.monotonic()
        with limiter.scope():
            with pytest.raises(acquisition.AcquisitionError) as error:
                acquisition.fetch_public_resource('http://public.example/slow', deadline=started + 0.2)
            assert error.value.code == 'source_unavailable'
            assert time.monotonic() - started < 1.0
            # Same host and global slot must be usable after timeout, not left occupied.
            body, _, _ = acquisition.fetch_public_resource('http://public.example/fast', deadline=time.monotonic() + 1)
            assert body == b'ok'


@pytest.mark.parametrize('stage', [
    'terminal_no_crlf', 'trailer_eof', 'trailer_terminator_eof', 'delimiter_eof',
])
@pytest.mark.parametrize('status', [200, 403])
def test_chunk_framing_eof_keeps_exact_public_entity_diagnostics(monkeypatch, stage, status):
    with trickling_server(stage, status=status) as port:
        monkeypatch.setattr(acquisition, '_public_addresses', lambda *args: ['127.0.0.1'])
        monkeypatch.setattr(acquisition, 'getproxies_environment', lambda: {})
        with pytest.raises(acquisition.AcquisitionError) as caught:
            acquisition.fetch_public_resource(f'http://public.example:{port}/truncated',
                deadline=time.monotonic() + 2)
        assert caught.value.code == ('source_unavailable' if status == 200 else 'source_blocked')
        diagnostic = caught.value.source_response
        assert base64.b64decode(diagnostic['raw_response_base64']) == SOURCE_ENTITY
        assert diagnostic['raw_response_digest'] == hashlib.sha256(SOURCE_ENTITY).hexdigest()
        assert diagnostic['raw_response_complete'] is False
        assert diagnostic['http_status'] == status


@pytest.mark.parametrize('stage', ['chunk_complete', 'chunk_trailer_complete'])
def test_complete_public_chunks_with_optional_trailers_remain_accepted(monkeypatch, stage):
    with trickling_server(stage) as port:
        monkeypatch.setattr(acquisition, '_public_addresses', lambda *args: ['127.0.0.1'])
        monkeypatch.setattr(acquisition, 'getproxies_environment', lambda: {})
        body, _, _ = acquisition.fetch_public_resource(f'http://public.example:{port}/complete',
            deadline=time.monotonic() + 2)
        assert body == SOURCE_ENTITY


def test_direct_public_tcp_and_real_tls_share_remaining_budget(monkeypatch):
    with model_server('tls') as (port, state):
        budget = 0.5
        connect = socket.create_connection
        monkeypatch.setattr(acquisition, '_public_addresses', lambda *args: ['93.184.216.34'])
        monkeypatch.setattr(acquisition, 'getproxies_environment', lambda: {})

        def delayed_tcp(address, timeout):
            assert address == ('93.184.216.34', port)
            sock = connect(('127.0.0.1', port), timeout)
            # TCP consumes part of the deadline before the real TLS handshake.
            Event().wait(0.3)
            return sock

        monkeypatch.setattr(socket, 'create_connection', delayed_tcp)
        started = time.monotonic()
        with pytest.raises(acquisition.AcquisitionError) as caught:
            acquisition.fetch_public_resource(f'https://public.example:{port}/slow',
                deadline=started + budget)
        assert caught.value.code == 'source_unavailable'
        assert time.monotonic() - started < budget + 0.2
        assert state['connections'] == 1
        assert state['client_hello'].startswith(b'\x16\x03')
