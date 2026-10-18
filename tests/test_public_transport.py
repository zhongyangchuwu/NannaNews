"""Public GET routing/security regressions; no external network or model calls."""
import http.client
import io
import json
import socket
import socketserver
import ssl

import threading
import time
import unittest
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


from nannanews import acquisition

PUBLIC_IP = '93.184.216.34'
OTHER_PUBLIC_IP = '1.1.1.1'


def address_info(address, port):
    family = socket.AF_INET6 if ':' in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, '', (address, port))]


class Response:
    def __init__(self, status=200, headers=None, body=b'ok'):
        self.status = status
        self.headers = headers or {}
        self.body = io.BytesIO(body)
        self.closed = False

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, size):
        return self.body.read(size)

    def close(self):
        self.closed = True
        self.body.close()


class ScriptedConnections:
    """Replace only the socket-facing connection boundary for retry cases."""
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.connections = []

    def __call__(self, host, port, address, timeout):
        outcome = next(self.outcomes)
        conn = Mock()
        conn.sock = None
        conn.address = address
        conn.request.side_effect = outcome if isinstance(outcome, BaseException) else None
        conn.getresponse.return_value = outcome
        self.connections.append(conn)
        return conn


@contextmanager
def connect_proxy(respond=None, connect_status=200):
    """A real loopback proxy socket; it never dials any CONNECT destination."""
    requests = []
    errors = []
    respond = respond or (lambda path: (200, {}, b'proxied'))

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(2)
            try:
                connect_line = self.rfile.readline(8192).decode('ascii').strip()
                connect_headers = http.client.parse_headers(self.rfile)
                requests.append({'connect': connect_line, 'connect_headers': connect_headers})
                record = requests[-1]
                if connect_status != 200:
                    self.wfile.write(f'HTTP/1.1 {connect_status} Refused\r\nContent-Length: 0\r\n\r\n'.encode())
                    self.wfile.flush()
                    return
                self.wfile.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                self.wfile.flush()
                request_line = self.rfile.readline(8192).decode('ascii').strip()
                if not request_line:
                    return
                record['request'] = request_line
                record['headers'] = http.client.parse_headers(self.rfile)
                _, path, _ = request_line.split(' ', 2)
                status, headers, body = respond(path)
                self.wfile.write(f'HTTP/1.1 {status} Result\r\nContent-Length: {len(body)}\r\nConnection: close\r\n'.encode())
                for name, value in headers.items():
                    self.wfile.write(f'{name}: {value}\r\n'.encode())
                self.wfile.write(b'\r\n' + body)
                self.wfile.flush()
            except BaseException as error:
                errors.append(error)

    with socketserver.TCPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        try:
            yield server.server_address[1], requests
        finally:
            server.shutdown()
            thread.join(3)
            if thread.is_alive():
                raise AssertionError('local CONNECT proxy did not stop')
    if errors:
        raise AssertionError(f'local CONNECT proxy failed: {errors!r}')


class PublicProxyTests(unittest.TestCase):
    def dns(self, host, port, *args):
        return address_info('127.0.0.1' if host == '127.0.0.1' else PUBLIC_IP, port)

    def test_https_connect_pins_ip_and_preserves_origin_sni_host_without_auth(self):
        sni = []
        context = Mock()
        context.wrap_socket.side_effect = lambda sock, server_hostname: (sni.append(server_hostname) or sock)
        with connect_proxy() as (port, requests), \
                patch.dict('os.environ', {'HTTPS_PROXY': f'http://127.0.0.1:{port}'}, clear=True), \
                patch.object(socket, 'getaddrinfo', side_effect=self.dns), \
                patch.object(ssl, 'create_default_context', return_value=context):
            result = acquisition.fetch_public_resource('https://article.example:8443/a?q=one')
        self.assertEqual(result, (b'proxied', '', 'https://article.example:8443/a?q=one'))
        self.assertEqual(sni, ['article.example'])
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0]['connect'].startswith(f'CONNECT {PUBLIC_IP}:8443 HTTP/'))
        self.assertEqual(requests[0]['connect_headers']['Host'], f'{PUBLIC_IP}:8443')
        self.assertEqual(requests[0]['request'], 'GET /a?q=one HTTP/1.1')
        self.assertEqual(requests[0]['headers']['Host'], 'article.example:8443')
        for record_headers in (requests[0]['connect_headers'], requests[0]['headers']):
            for name in ('Authorization', 'Proxy-Authorization', 'Cookie'):
                self.assertIsNone(record_headers[name])

    def test_http_redirect_uses_new_validated_ip_and_origin_host(self):
        def dns(host, port, *args):
            address = {'first.example': PUBLIC_IP, 'second.example': OTHER_PUBLIC_IP}.get(host, '127.0.0.1')
            return address_info(address, port)

        def respond(path):
            if path == '/first':
                return 302, {'Location': 'http://second.example/second'}, b''
            return 200, {}, b'redirected'

        with connect_proxy(respond) as (port, requests), \
                patch.dict('os.environ', {'http_proxy': f'http://127.0.0.1:{port}'}, clear=True), \
                patch.object(socket, 'getaddrinfo', side_effect=dns):
            result = acquisition.fetch_public_resource('http://first.example/first')
        self.assertEqual(result[0], b'redirected')
        self.assertEqual(result[2], 'http://second.example/second')
        self.assertEqual([r['connect'].split()[1] for r in requests], [f'{PUBLIC_IP}:80', f'{OTHER_PUBLIC_IP}:80'])
        self.assertEqual([r['headers']['Host'] for r in requests], ['first.example', 'second.example'])
        for record in requests:
            self.assertIsNone(record['headers']['Authorization'])
            self.assertIsNone(record['headers']['Proxy-Authorization'])
            self.assertIsNone(record['headers']['Cookie'])

    def test_private_mixed_and_rebound_dns_rejected_before_connect(self):
        for addresses in ([PUBLIC_IP, '127.0.0.1'], ['10.0.0.1'], ['::1']):
            with self.subTest(addresses=addresses), \
                    patch.object(acquisition, 'getproxies_environment', return_value={'http': 'http://127.0.0.1:7897'}), \
                    patch.object(socket, 'getaddrinfo', return_value=sum((address_info(ip, 80) for ip in addresses), [])), \
                    patch.object(socket, 'create_connection') as connect:
                with self.assertRaises(acquisition.AcquisitionError) as caught:
                    acquisition.fetch_public_resource('http://article.example/')
                self.assertEqual(caught.exception.code, 'unsafe_source')
                connect.assert_not_called()

        resolutions = iter([address_info(PUBLIC_IP, 80), address_info('127.0.0.1', 80)])

        def rebound_dns(host, port, *args):
            if host == '127.0.0.1':
                return address_info(host, port)
            return next(resolutions)

        with connect_proxy(lambda path: (302, {'Location': '/again'}, b'')) as (port, requests), \
                patch.dict('os.environ', {'HTTP_PROXY': f'http://127.0.0.1:{port}'}, clear=True), \
                patch.object(socket, 'getaddrinfo', side_effect=rebound_dns):
            with self.assertRaises(acquisition.AcquisitionError) as caught:
                acquisition.fetch_public_resource('http://article.example/')
        self.assertEqual(caught.exception.code, 'unsafe_source')
        self.assertEqual(len(requests), 1)

    def test_no_proxy_bypasses_connect_but_not_public_validation(self):
        factory = ScriptedConnections([Response()])
        with patch.dict('os.environ', {
                'HTTP_PROXY': 'http://127.0.0.1:7897', 'no_proxy': '.example:8080'}, clear=True), \
                patch.object(socket, 'getaddrinfo', return_value=address_info(PUBLIC_IP, 8080)), \
                patch.object(acquisition, '_PinnedHTTP', side_effect=factory), \
                patch.object(acquisition, '_PublicTunnelConnection') as tunnel:
            self.assertEqual(acquisition.fetch_public_resource('http://article.example:8080/')[0], b'ok')
        tunnel.assert_not_called()
        self.assertEqual(factory.connections[0].request.call_args.kwargs['headers']['Host'], 'article.example:8080')
        with patch.dict('os.environ', {'HTTP_PROXY': 'http://127.0.0.1:7897', 'NO_PROXY': '*'}, clear=True), \
                patch.object(socket, 'getaddrinfo', return_value=address_info('127.0.0.1', 80)), \
                patch.object(socket, 'create_connection') as connect:
            with self.assertRaises(acquisition.AcquisitionError) as caught:
                acquisition.fetch_public_resource('http://private.example/')
        self.assertEqual(caught.exception.code, 'unsafe_source')
        connect.assert_not_called()

    def test_malformed_unsupported_and_credentialed_proxies_fail_closed_without_leaks(self):
        for configured in ('socks5://127.0.0.1:7897', 'https://127.0.0.1:7897',
                           'http://user:secret@127.0.0.1:7897', 'http://host:0',
                           'http://host:bad', 'http://host/path', 'http://host\nInjected: bad'):
            with self.subTest(configured=configured), \
                    patch.object(acquisition, 'getproxies_environment', return_value={'https': configured}), \
                    patch.object(socket, 'create_connection') as connect:
                with self.assertRaises(acquisition.AcquisitionError) as caught:
                    acquisition.fetch_public_resource('https://article.example/')
                self.assertEqual(caught.exception.code, 'source_proxy_invalid')
                self.assertNotIn('secret', str(caught.exception))
                self.assertNotIn(configured, str(caught.exception))
                connect.assert_not_called()

    def test_connect_http_refusal_is_not_retried(self):
        with connect_proxy(connect_status=407) as (port, requests), \
                patch.dict('os.environ', {'HTTP_PROXY': f'http://127.0.0.1:{port}'}, clear=True), \
                patch.object(socket, 'getaddrinfo', side_effect=self.dns):
            with self.assertRaises(acquisition.AcquisitionError) as caught:
                acquisition.fetch_public_resource('http://article.example/')
        self.assertEqual(caught.exception.code, 'source_blocked')
        self.assertEqual(len(requests), 1)

    def test_proxy_connect_failures_recover_on_fourth_socket_attempt(self):
        real_connect = socket.create_connection
        attempts = []

        def connect(address, timeout):
            attempts.append(address)
            if len(attempts) <= 3:
                raise ConnectionRefusedError('temporary proxy connection failure')
            return real_connect(address, timeout)

        with connect_proxy() as (port, requests), \
                patch.dict('os.environ', {'HTTP_PROXY': f'http://127.0.0.1:{port}'}, clear=True), \
                patch.object(socket, 'getaddrinfo', side_effect=self.dns), \
                patch.object(socket, 'create_connection', side_effect=connect):
            self.assertEqual(acquisition.fetch_public_resource('http://article.example/')[0], b'proxied')
        self.assertEqual(attempts, [('127.0.0.1', port)] * 4)
        self.assertEqual(len(requests), 1)

    def test_redirect_with_embedded_credentials_is_rejected_without_forwarding(self):
        with connect_proxy(lambda path: (302, {'Location': 'http://user:secret@other.example/'}, b'')) as (port, requests), \
                patch.dict('os.environ', {'HTTP_PROXY': f'http://127.0.0.1:{port}'}, clear=True), \
                patch.object(socket, 'getaddrinfo', side_effect=self.dns):
            with self.assertRaises(acquisition.AcquisitionError) as caught:
                acquisition.fetch_public_resource('http://article.example/')
        self.assertEqual(caught.exception.code, 'invalid_url')
        self.assertNotIn('secret', str(caught.exception))
        self.assertEqual(len(requests), 1)
        self.assertIsNone(requests[0]['headers']['Authorization'])
        self.assertIsNone(requests[0]['headers']['Proxy-Authorization'])


class PublicRetryTests(unittest.TestCase):
    def setUp(self):
        self.audit_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.audit_root.cleanup)
        proxy = patch.object(acquisition, 'getproxies_environment', return_value={})
        proxy.start()
        self.addCleanup(proxy.stop)

    def test_three_transient_dns_failures_recover_on_fourth_attempt(self):
        factory = ScriptedConnections([Response()])
        outcomes = [socket.gaierror('temporary')] * 3 + [address_info(PUBLIC_IP, 80)]
        with patch.object(socket, 'getaddrinfo', side_effect=outcomes) as dns, \
                patch.object(acquisition, '_PinnedHTTP', side_effect=factory):
            self.assertEqual(acquisition.fetch_public_resource('http://article.example/')[0], b'ok')
        self.assertEqual(dns.call_count, 4)
        self.assertEqual(len(factory.connections), 1)

    def test_persistent_dns_failure_stops_after_four_attempts_without_http(self):
        with patch.object(socket, 'getaddrinfo', side_effect=socket.gaierror('temporary')) as dns, \
                patch.object(acquisition, '_PinnedHTTP') as connect:
            with self.assertRaises(acquisition.AcquisitionError) as caught:
                acquisition.fetch_public_resource('http://article.example/')
        self.assertEqual(caught.exception.code, 'source_unavailable')
        self.assertEqual(dns.call_count, 4)
        connect.assert_not_called()

    def test_transport_retries_cycle_public_addresses_and_release_connections_and_gates(self):
        success = Response()
        factory = ScriptedConnections([ConnectionResetError('reset')] * 3 + [success, Response()])
        limiter = acquisition.PublicFetchLimiter(max_concurrent=1)
        addresses = address_info(PUBLIC_IP, 80) + address_info(OTHER_PUBLIC_IP, 80)
        with patch.object(socket, 'getaddrinfo', return_value=addresses), \
                patch.object(acquisition, '_PinnedHTTP', side_effect=factory), limiter.scope():
            self.assertEqual(acquisition.fetch_public_resource('http://article.example/')[0], b'ok')
            self.assertEqual(acquisition.fetch_public_resource('http://article.example/next')[0], b'ok')
        self.assertEqual([conn.address for conn in factory.connections[:4]],
                         [PUBLIC_IP, OTHER_PUBLIC_IP, PUBLIC_IP, OTHER_PUBLIC_IP])
        self.assertTrue(success.closed)
        for conn in factory.connections:
            conn.close.assert_called_once()

    def test_persistent_transport_failure_stops_after_four_attempts(self):
        factory = ScriptedConnections([ConnectionRefusedError('refused')] * 4)
        with patch.object(socket, 'getaddrinfo', return_value=address_info(PUBLIC_IP, 80)), \
                patch.object(acquisition, '_PinnedHTTP', side_effect=factory):
            with self.assertRaises(acquisition.AcquisitionError) as caught:
                acquisition.fetch_public_resource('http://article.example/')
        self.assertEqual(caught.exception.code, 'source_unavailable')
        self.assertEqual(len(factory.connections), 4)
        for conn in factory.connections:
            conn.close.assert_called_once()

    def test_unsafe_rebound_during_retry_is_not_retried(self):
        factory = ScriptedConnections([ConnectionResetError('reset')])
        with patch.object(socket, 'getaddrinfo', side_effect=[address_info(PUBLIC_IP, 80), address_info('10.0.0.1', 80)]) as dns, \
                patch.object(acquisition, '_PinnedHTTP', side_effect=factory):
            with self.assertRaises(acquisition.AcquisitionError) as caught:
                acquisition.fetch_public_resource('http://article.example/')
        self.assertEqual(caught.exception.code, 'unsafe_source')
        self.assertEqual(dns.call_count, 2)
        self.assertEqual(len(factory.connections), 1)

    def test_http_failures_and_media_bounds_are_not_retried(self):
        cases = [(Response(status=status), 'source_blocked') for status in (401, 403, 404, 500)]
        cases += [(Response(headers={'Content-Length': '100'}), 'source_too_large'),
                  (Response(headers={'Content-Encoding': 'gzip'}), 'source_blocked')]
        for response, code in cases:
            factory = ScriptedConnections([response])
            with self.subTest(code=code, status=response.status), \
                    patch.object(socket, 'getaddrinfo', return_value=address_info(PUBLIC_IP, 80)), \
                    patch.object(acquisition, '_PinnedHTTP', side_effect=factory):
                with self.assertRaises(acquisition.AcquisitionError) as caught:
                    acquisition.fetch_public_resource('http://article.example/', max_bytes=4)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(factory.connections), 1)
                self.assertTrue(response.closed)

    def test_expired_deadline_never_starts_or_retries_http(self):
        clock = {'now': 100.0}
        factory = ScriptedConnections([ConnectionResetError('reset')])

        def dns(host, port, *args):
            clock['now'] = 105.0
            return address_info(PUBLIC_IP, port)

        with patch.object(acquisition.time, 'monotonic', side_effect=lambda: clock['now']), \
                patch.object(socket, 'getaddrinfo', side_effect=dns), \
                patch.object(acquisition, '_PinnedHTTP', side_effect=factory) as connect:
            with self.assertRaises(acquisition.AcquisitionError):
                acquisition.fetch_public_resource('http://article.example/', deadline=104.0)
            connect.assert_not_called()
        clock['now'] = 100.0
        factory = ScriptedConnections([ConnectionResetError('reset')])
        original_factory = factory.__call__

        def failing_connection(*args):
            conn = original_factory(*args)
            def fail(*args, **kwargs):
                clock['now'] = 105.0
                raise ConnectionResetError('reset')
            conn.request.side_effect = fail
            return conn

        with patch.object(acquisition.time, 'monotonic', side_effect=lambda: clock['now']), \
                patch.object(socket, 'getaddrinfo', return_value=address_info(PUBLIC_IP, 80)), \
                patch.object(acquisition, '_PinnedHTTP', side_effect=failing_connection):
            with self.assertRaises(acquisition.AcquisitionError):
                acquisition.fetch_public_resource('http://article.example/', deadline=104.0)
        self.assertEqual(len(factory.connections), 1)
        factory.connections[0].close.assert_called_once()

    def test_retry_budget_is_shared_across_redirects(self):
        factory = ScriptedConnections([
            ConnectionResetError('reset'), ConnectionResetError('reset'),
            Response(302, {'Location': 'http://second.example/'}),
            ConnectionResetError('reset'), ConnectionResetError('reset'),
        ])
        with patch.object(socket, 'getaddrinfo', return_value=address_info(PUBLIC_IP, 80)), \
                patch.object(acquisition, '_PinnedHTTP', side_effect=factory):
            with self.assertRaises(acquisition.AcquisitionError) as caught:
                acquisition.fetch_public_resource('http://first.example/')
        self.assertEqual(caught.exception.code, 'source_unavailable')
        self.assertEqual(len(factory.connections), 5)
        for conn in factory.connections:
            conn.close.assert_called_once()

    def test_model_post_remains_direct_single_request_with_provider_auth_only(self):
        settings = SimpleNamespace(model_timeout=10, model_api_style='responses',
                                   model_base_url='http://provider.example/v1', model_api_key='private-key',
                                   data_dir=Path(self.audit_root.name))
        response = Response(body=json.dumps({'output': []}).encode())
        factory = ScriptedConnections([response])
        with patch.object(acquisition, '_model_config', return_value=settings), \
                patch.object(acquisition, '_resolve_addresses', return_value=['127.0.0.1']), \
                patch.object(acquisition, '_PinnedHTTP', side_effect=factory), \
                patch.object(acquisition, '_PublicTunnelConnection') as tunnel, \
                patch.object(acquisition, 'getproxies_environment', return_value={'http': 'http://127.0.0.1:7897'}) as proxy:
            self.assertEqual(acquisition._model_post({'model': 'unchanged'}), {'output': []})
        self.assertEqual(len(factory.connections), 1)
        conn = factory.connections[0]
        conn.connect.assert_called_once()
        conn.request.assert_called_once()
        self.assertEqual(conn.request.call_args.args[:2], ('POST', '/v1/responses'))
        self.assertEqual(conn.request.call_args.kwargs['headers']['Authorization'], 'Bearer private-key')
        tunnel.assert_not_called()
        proxy.assert_not_called()

    def test_model_request_transport_failure_is_not_retried_or_proxied(self):
        settings = SimpleNamespace(model_timeout=10, model_api_style='responses',
                                   model_base_url='http://provider.example/v1', model_api_key='private-key',
                                   data_dir=Path(self.audit_root.name))
        factory = ScriptedConnections([ConnectionResetError('request interrupted')])
        with patch.object(acquisition, '_model_config', return_value=settings), \
                patch.object(acquisition, '_resolve_addresses', return_value=['127.0.0.1']), \
                patch.object(acquisition, '_PinnedHTTP', side_effect=factory), \
                patch.object(acquisition, '_PublicTunnelConnection') as tunnel:
            with self.assertRaises(acquisition.AcquisitionError) as caught:
                acquisition._model_post({'model': 'unchanged'})
        self.assertEqual(caught.exception.code, 'model_unavailable')
        self.assertEqual(len(factory.connections), 1)
        factory.connections[0].request.assert_called_once()
        factory.connections[0].close.assert_called_once()
        tunnel.assert_not_called()


if __name__ == '__main__':
    unittest.main()
