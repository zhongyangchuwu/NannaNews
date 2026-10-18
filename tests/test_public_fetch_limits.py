"""Scoped public transport concurrency tests without network access."""
import base64
import socket

import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


from nannanews import acquisition


PUBLIC_IP = '93.184.216.34'
PNG_BYTES = b'\x89PNG\r\n\x1a\n'


class SourceResponse:
    def __init__(self, status=200, headers=None, body=b'ok'):
        self.status = status
        self.headers = headers or {'Content-Type': 'text/plain'}
        self.body = body
        self.offset = 0

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, size):
        chunk = self.body[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk

    def close(self):
        pass


class PinnedTransport:
    """Track pinned connection lifetimes and let tests coordinate responses."""
    def __init__(self, respond=None):
        self.respond = respond or (lambda host, path: SourceResponse())
        self.condition = threading.Condition()
        self.active = 0
        self.max_active = 0
        self.active_by_host = {}
        self.max_by_host = {}

    def connection(self, host, port, address, timeout):
        return _PinnedConnection(self, host)

    def _opened(self, host):
        with self.condition:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            count = self.active_by_host.get(host, 0) + 1
            self.active_by_host[host] = count
            self.max_by_host[host] = max(self.max_by_host.get(host, 0), count)
            self.condition.notify_all()

    def _closed(self, host):
        with self.condition:
            self.active -= 1
            self.active_by_host[host] -= 1
            self.condition.notify_all()

    def wait_for_active(self, count, timeout=3):
        with self.condition:
            return self.condition.wait_for(lambda: self.active >= count, timeout)



class _PinnedConnection:
    def __init__(self, transport, host):
        self.transport = transport
        self.host = host
        self.path = ''
        self.sock = None
        self.closed = False
        transport._opened(host)

    def request(self, method, path, headers=None):
        self.path = path

    def getresponse(self):
        return self.transport.respond(self.host, self.path)

    def close(self):
        if not self.closed:
            self.closed = True
            self.transport._closed(self.host)


@contextmanager
def fake_transport(transport):
    with patch.object(acquisition, '_public_addresses', return_value=[PUBLIC_IP]), \
            patch.object(acquisition, '_PinnedHTTP', side_effect=transport.connection), \
            patch.object(acquisition, '_PinnedHTTPS', side_effect=transport.connection):
        yield


def wait_for_event(test, event, message, timeout=3):
    test.assertTrue(event.wait(timeout), message)


class PublicFetchLimiterTests(unittest.TestCase):
    def setUp(self):
        proxy_patch = patch.object(acquisition, 'getproxies_environment', return_value={})
        proxy_patch.start()
        self.addCleanup(proxy_patch.stop)

    def test_same_host_serializes_without_blocking_distinct_hosts_from_six_slots(self):
        release_shared = threading.Event()
        shared_entered = threading.Event()
        duplicate_attempted = threading.Event()
        all_ready = threading.Barrier(7)

        def respond(host, path):
            if host == 'shared.example':
                shared_entered.set()
            release_shared.wait()
            return SourceResponse()
        transport = PinnedTransport(respond)
        limiter = acquisition.PublicFetchLimiter(max_concurrent=6)
        errors = []
        errors_lock = threading.Lock()
        urls = ['http://shared.example/one', 'http://SHARED.EXAMPLE/two'] + [
            f'http://distinct{index}.example/' for index in range(5)
        ]

        def fetch(url, barrier=None):
            try:
                if barrier is not None:
                    barrier.wait()
                if url.endswith('/two'):
                    duplicate_attempted.set()
                with limiter.scope():
                    acquisition.fetch_public_resource(url, deadline=time.monotonic() + 5)
            except BaseException as error:
                with errors_lock:
                    errors.append(error)

        first_thread = threading.Thread(target=fetch, args=(urls[0],))
        threads = [threading.Thread(target=fetch, args=(url, all_ready)) for url in urls[1:]]
        with fake_transport(transport):
            try:
                first_thread.start()
                wait_for_event(self, shared_entered, 'first shared-host response did not start')
                for thread in threads:
                    thread.start()
                all_ready.wait()
                wait_for_event(self, duplicate_attempted, 'same-host waiter did not start')
                self.assertTrue(transport.wait_for_active(6), 'six independent HTTP operations should overlap')
                self.assertEqual(transport.max_active, 6)
                self.assertEqual(transport.max_by_host.get('shared.example', 0), 1)
            finally:
                release_shared.set()
                first_thread.join(5)
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(5)
        self.assertFalse(first_thread.is_alive())
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])

    def test_redirect_releases_previous_hop_and_image_fetch_uses_same_limiter(self):
        target_entered = threading.Event()
        release_target = threading.Event()
        image_wait_started = threading.Event()

        def respond(host, path):
            if host == 'origin.example':
                return SourceResponse(302, {'Location': 'http://target.example/article'})
            if path == '/article':
                target_entered.set()
                release_target.wait()
                return SourceResponse(body=b'article')
            return SourceResponse(headers={'Content-Type': 'image/png'}, body=PNG_BYTES)

        transport = PinnedTransport(respond)
        limiter = acquisition.PublicFetchLimiter(max_concurrent=1)
        result = []
        redirect_errors = []

        def follow_redirect():
            try:
                with limiter.scope():
                    result.append(acquisition.fetch_public_resource(
                        'http://origin.example/start', deadline=time.monotonic() + 5
                    ))
            except BaseException as error:
                redirect_errors.append(error)

        redirect_thread = threading.Thread(target=follow_redirect)
        image_errors = []

        def fetch_image():
            image_wait_started.set()
            try:
                with limiter.scope():
                    acquisition.fetch_public_resource(
                        'http://target.example/image.png', acquisition.MAX_IMAGE,
                        deadline=time.monotonic() + 0.2,
                    )
            except BaseException as error:
                image_errors.append(error)

        image_thread = threading.Thread(target=fetch_image)
        try:
            with fake_transport(transport):
                redirect_thread.start()
                wait_for_event(self, target_entered, 'redirect target did not acquire the single slot')
                image_thread.start()
                wait_for_event(self, image_wait_started, 'image fetch did not start')
                image_thread.join(3)
                self.assertFalse(image_thread.is_alive())
                self.assertEqual(len(image_errors), 1)
                self.assertIsInstance(image_errors[0], acquisition.AcquisitionError)
                self.assertEqual(transport.max_by_host.get('target.example'), 1)
                release_target.set()
                redirect_thread.join(3)
                self.assertFalse(redirect_thread.is_alive())
                self.assertEqual(redirect_errors, [])
                self.assertEqual(result[0][2], 'http://target.example/article')
                with limiter.scope():
                    media = acquisition._prepare_media({'images': ['http://target.example/image.png']})
                self.assertEqual(media[0]['data'], base64.b64encode(PNG_BYTES).decode('ascii'))
        finally:
            release_target.set()
            for thread in (redirect_thread, image_thread):
                if thread.ident is not None:
                    thread.join(5)

    def test_scope_is_thread_local_and_nested_scope_restores_previous_limiter(self):
        held = threading.Event()
        release = threading.Event()

        def respond(host, path):
            if path == '/held':
                held.set()
                release.wait()
            return SourceResponse()

        transport = PinnedTransport(respond)
        limiter = acquisition.PublicFetchLimiter(max_concurrent=1)
        nested_limiter = acquisition.PublicFetchLimiter(max_concurrent=1)
        errors = []

        def scoped_holder():
            try:
                with limiter.scope():
                    acquisition.fetch_public_resource('http://scope.example/held')
            except BaseException as error:
                errors.append(error)

        holder = threading.Thread(target=scoped_holder)
        try:
            with fake_transport(transport):
                holder.start()
                wait_for_event(self, held, 'scoped request did not start')
                with limiter.scope():
                    unscoped_done = threading.Event()
                    unscoped_errors = []

                    def unscoped_fetch():
                        try:
                            acquisition.fetch_public_resource('http://scope.example/unscoped')
                        except BaseException as error:
                            unscoped_errors.append(error)
                        finally:
                            unscoped_done.set()

                    unscoped = threading.Thread(target=unscoped_fetch)
                    unscoped.start()
                    wait_for_event(self, unscoped_done, 'another thread inherited the active limiter scope')
                    self.assertEqual(unscoped_errors, [])
                    with nested_limiter.scope():
                        acquisition.fetch_public_resource('http://nested.example/inner')
                    with self.assertRaises(acquisition.AcquisitionError):
                        acquisition.fetch_public_resource(
                            'http://scope.example/after-inner', deadline=time.monotonic() + 0.1
                        )
                    unscoped.join(3)
                    self.assertFalse(unscoped.is_alive())
        finally:
            release.set()
            holder.join(5)
        self.assertFalse(holder.is_alive())
        self.assertEqual(errors, [])

    def test_baseexception_and_host_and_global_wait_deadlines_release_slots(self):
        held = threading.Event()
        release = threading.Event()

        def respond(host, path):
            if path == '/cancel':
                raise KeyboardInterrupt('cancelled transport')
            if path == '/held':
                held.set()
                release.wait()
            return SourceResponse()

        transport = PinnedTransport(respond)
        limiter = acquisition.PublicFetchLimiter(max_concurrent=1)
        holder_errors = []

        def hold_slot():
            try:
                with limiter.scope():
                    acquisition.fetch_public_resource('http://slot.example/held')
            except BaseException as error:
                holder_errors.append(error)

        holder = threading.Thread(target=hold_slot)
        try:
            with fake_transport(transport):
                with self.assertRaises(KeyboardInterrupt):
                    with limiter.scope():
                        acquisition.fetch_public_resource('http://slot.example/cancel')
                holder.start()
                wait_for_event(self, held, 'held request did not start')
                with self.assertRaises(acquisition.AcquisitionError) as caught:
                    with limiter.scope():
                        acquisition.fetch_public_resource(
                            'http://slot.example/waiter', deadline=time.monotonic() + 0.1
                        )
                self.assertEqual(caught.exception.code, 'source_unavailable')
                with self.assertRaises(acquisition.AcquisitionError) as global_caught:
                    with limiter.scope():
                        acquisition.fetch_public_resource(
                            'http://other-slot.example/waiter', deadline=time.monotonic() + 0.1
                        )
                self.assertEqual(global_caught.exception.code, 'source_unavailable')
                release.set()
                holder.join(3)
                self.assertFalse(holder.is_alive())
                with limiter.scope():
                    self.assertEqual(
                        acquisition.fetch_public_resource('http://slot.example/recovered')[0], b'ok'
                    )
        finally:
            release.set()
            if holder.ident is not None:
                holder.join(5)
        self.assertEqual(holder_errors, [])

    def test_dns_waiters_wait_for_a_slot_and_respect_the_remaining_deadline(self):
        class ObservedSlots:
            def __init__(self, value):
                self.semaphore = threading.BoundedSemaphore(value)
                self.wait_attempted = threading.Event()

            def acquire(self, blocking=True, timeout=None):
                if not blocking:
                    acquired = self.semaphore.acquire(blocking=False)
                    self.wait_attempted.set()
                    return acquired
                self.wait_attempted.set()
                return self.semaphore.acquire(timeout=timeout)

            def release(self):
                self.semaphore.release()

        dns_entered = threading.Event()
        release_dns = threading.Event()

        def resolve(host, port, family, socktype):
            dns_entered.set()
            release_dns.wait()
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (PUBLIC_IP, port))]

        transport = PinnedTransport()
        slots = ObservedSlots(4)
        for _ in range(4):
            self.assertTrue(slots.acquire())
        slots.wait_attempted.clear()
        limiter = acquisition.PublicFetchLimiter()
        result = []
        errors = []

        def queued_fetch(deadline):
            try:
                with limiter.scope():
                    result.append(acquisition.fetch_public_resource('http://dns.example/', deadline=deadline))
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=queued_fetch, args=(time.monotonic() + 2,))
        try:
            with patch.object(acquisition, '_DNS_SLOTS', slots), \
                    patch.object(socket, 'getaddrinfo', side_effect=resolve), \
                    patch.object(acquisition, '_PinnedHTTP', side_effect=transport.connection):
                thread.start()
                wait_for_event(self, slots.wait_attempted, 'DNS caller did not attempt to acquire a slot')
                slots.release()
                wait_for_event(self, dns_entered, 'queued DNS caller did not proceed after a slot freed')
                release_dns.set()
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(result[0][0], b'ok')

                self.assertTrue(slots.acquire())
                slots.wait_attempted.clear()
                timed_out = []

                def expiring_fetch():
                    try:
                        with limiter.scope():
                            acquisition.fetch_public_resource(
                                'http://dns.example/', deadline=time.monotonic() + 0.1
                            )
                    except BaseException as error:
                        timed_out.append(error)

                timeout_thread = threading.Thread(target=expiring_fetch)
                timeout_thread.start()
                wait_for_event(self, slots.wait_attempted, 'DNS deadline caller did not attempt its slot')
                timeout_thread.join(2)
                self.assertFalse(timeout_thread.is_alive())
                self.assertEqual(len(timed_out), 1)
                self.assertIsInstance(timed_out[0], acquisition.AcquisitionError)
                self.assertEqual(timed_out[0].code, 'source_unavailable')
        finally:
            release_dns.set()
            for _ in range(4):
                try:
                    slots.release()
                except ValueError:
                    break
            if thread.ident is not None:
                thread.join(5)


if __name__ == '__main__':
    unittest.main()
