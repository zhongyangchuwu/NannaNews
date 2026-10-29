"""Public-source intake. Evidence stays private; model output is never published here."""
import base64
import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import math
import re
import socket
import ssl
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import partial, wraps
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlsplit, urlunsplit
from urllib.request import getproxies_environment, proxy_bypass_environment

from bs4 import BeautifulSoup
from cairosvg.surface import PNGSurface
from defusedxml import ElementTree as SafeXML
from defusedxml.common import DefusedXmlException
import tinycss2
from flask import Blueprint, Response, abort, jsonify, render_template, request, session, url_for
from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError
from nannanews import runtime
from nannanews.content_rules import EXTRACTION_INSTRUCTIONS

MAX_BODY = 16 * 1024 * 1024
SOURCE_CAPTURE_VERSION = 'public-source-v9-article-media-20261023'
MAX_SOURCE_ERROR_BODY = 256 * 1024
# Source evidence limits are independent of the smaller model representations.
MAX_IMAGE = 32 * 1024 * 1024
MAX_IMAGES = 64
# Aggregate model admission is separate from the per-source/upload/PDF limit.
MAX_MODEL_IMAGES = 128
MAX_MEDIA_BYTES = 128 * 1024 * 1024
MAX_IMAGE_PIXELS = 400_000_000
# Pillow raises a hard error at twice this finite process-wide threshold.
# Match the acquisition ceiling (including other source/GUI Pillow consumers).
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS // 2
MAX_IMAGE_FRAMES = 64
MAX_IMAGE_FRAME_PIXELS = 512_000_000
MAX_DOCUMENT_FRAME_PIXELS = 1_024_000_000
MAX_MODEL_IMAGE_PATCHES = 28000
MAX_MODEL_TOTAL_PATCHES = 56000
MAX_MODEL_IMAGE_BYTES = 4 * 1024 * 1024
MAX_MODEL_MEDIA_BYTES = 32 * 1024 * 1024
MAX_MODEL_REQUEST_BYTES = 48_000_000
MEDIA_CAPTURE_TIMEOUT = 120
IMAGE_MIMES = {'image/png', 'image/jpeg', 'image/webp', 'image/gif', 'image/bmp', 'image/tiff', 'image/svg+xml'}
MAX_TEXT = 60000
MAX_MODEL_BODY = 2 * 1024 * 1024
SOURCE_TIMEOUT = 30
SOURCE_TIMEZONE = timezone(timedelta(hours=8))
TAGS = {'讲座', '院级活动', '社团活动', '招聘升学宣讲', '校级活动', '新闻', '通知', '参考消息'}
_DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix='public-dns')
_DNS_SLOTS = threading.BoundedSemaphore(4)
bp = Blueprint('acquisition', __name__)


class AcquisitionError(Exception):
    def __init__(self, message, code='acquisition_failed', status=422, *,
                 model_http_status=None, model_retry_after_seconds=None,
                 model_transport_failure=False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.model_http_status = (model_http_status if type(model_http_status) is int
                                  and 100 <= model_http_status <= 599 else None)
        self.model_retry_after_seconds = None
        if type(model_retry_after_seconds) in (int, float):
            try:
                retry_after_seconds = float(model_retry_after_seconds)
            except (OverflowError, ValueError):
                pass
            else:
                if math.isfinite(retry_after_seconds) and retry_after_seconds > 0:
                    self.model_retry_after_seconds = retry_after_seconds
        self.model_transport_failure = model_transport_failure is True


def canonical_url(url):
    """Preserve path and query identity, including duplicate query parameters."""
    if not isinstance(url, str) or not url.strip() or len(url) > 8192:
        raise AcquisitionError('请输入完整的公开 HTTP(S) 链接。', 'invalid_url')
    url = url.strip()
    if re.search(r'[\x00-\x20\x7f\\]', url):
        raise AcquisitionError('链接包含不安全字符。', 'invalid_url')
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {'http', 'https'} or not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError
        host = parts.hostname.encode('idna').decode('ascii').lower()
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        host = '[' + host + ']' if ':' in host else host
        authority = host + (f':{port}' if port else '')
        return urlunsplit((parts.scheme.lower(), authority, quote(parts.path or '/', safe="/%:@!$&'()*+,;=-._~"), quote(parts.query, safe="%/:?@!$&'()*+,;=-._~"), ''))
    except (ValueError, UnicodeError):
        raise AcquisitionError('链接必须是无账号密码的 HTTP(S) URL。', 'invalid_url') from None


def _resolve_addresses(host, port, timeout):
    dns_budget = min(5, timeout)
    started = time.monotonic()
    if dns_budget <= 0 or not _DNS_SLOTS.acquire(timeout=dns_budget):
        raise AcquisitionError('公共来源 DNS 查询繁忙，请稍后重试。', 'source_unavailable', 503)
    remaining = dns_budget - (time.monotonic() - started)
    if remaining <= 0:
        _DNS_SLOTS.release()
        raise AcquisitionError('公共来源域名解析失败或超时。', 'source_unavailable')
    try:
        future = _DNS_POOL.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    except BaseException:
        _DNS_SLOTS.release()
        raise
    future.add_done_callback(lambda _: _DNS_SLOTS.release())
    try:
        results = future.result(timeout=remaining)
    except (FutureTimeout, OSError):
        raise AcquisitionError('公共来源域名解析失败或超时。', 'source_unavailable') from None
    addresses = list(dict.fromkeys(item[4][0] for item in results))
    if not addresses:
        raise AcquisitionError('公共来源没有可用地址。', 'unsafe_source')
    return addresses


def _public_addresses(host, port, timeout):
    addresses = _resolve_addresses(host, port, timeout)
    try:
        return _require_public_addresses(addresses)
    except AcquisitionError as error:
        # Private diagnostic context only. This API still rejects every unsafe
        # OS result; only the source fetcher may consider proxy compatibility.
        error._os_dns_host = host
        error._os_dns_addresses = tuple(addresses)
        raise


def _require_public_addresses(addresses):
    for address in addresses:
        ip = ipaddress.ip_address(address)
        mapped = getattr(ip, 'ipv4_mapped', None)
        transition = getattr(ip, 'sixtofour', None)
        teredo = getattr(ip, 'teredo', None)
        if not ip.is_global or (mapped is not None and not mapped.is_global) or (transition is not None and not transition.is_global) or (teredo is not None and any(not value.is_global for value in teredo)):
            raise AcquisitionError('来源或重定向指向非公共网络，已拒绝。', 'unsafe_source')
    return addresses


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address = address
        self._model_budget = None
        self._source_deadline = None

    def connect(self):
        budget = self._model_budget
        timeout = min(self.timeout, budget.remaining()) if budget is not None else self.timeout
        if budget is None and self._source_deadline is not None:
            timeout = _source_timeout(self._source_deadline)
        self.sock = socket.create_connection((self.address, self.port), timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self.address = address
        self._model_budget = None
        self._source_deadline = None

    def connect(self):
        budget = self._model_budget
        timeout = min(self.timeout, budget.remaining()) if budget is not None else self.timeout
        if budget is None and self._source_deadline is not None:
            timeout = _source_timeout(self._source_deadline)
        sock = socket.create_connection((self.address, self.port), timeout)
        try:
            if budget is not None:
                sock.settimeout(min(self.timeout, budget.remaining()))
            elif self._source_deadline is not None:
                sock.settimeout(_source_timeout(self._source_deadline))
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def _source_timeout(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AcquisitionError('公共来源读取超时。', 'source_unavailable')
    return min(10, remaining)


def _source_proxy(parts):
    """Read only inherited process settings; never accept a proxy from source data."""
    proxies = getproxies_environment()
    if proxy_bypass_environment(parts.netloc, proxies):
        return None
    configured = proxies.get(parts.scheme) or proxies.get('all')
    if not configured:
        return None
    try:
        if re.search(r'[\x00-\x20\x7f\\]', configured):
            raise ValueError
        proxy = urlsplit(configured)
        # Fail closed, without disclosing credentials or bypassing routing policy.
        if (proxy.scheme != 'http' or not proxy.hostname or
                proxy.username is not None or proxy.password is not None or
                proxy.path not in {'', '/'} or proxy.query or proxy.fragment):
            raise ValueError
        port = proxy.port if proxy.port is not None else 80
        if not 1 <= port <= 65535:
            raise ValueError
        return proxy.hostname.encode('idna').decode('ascii'), port
    except (ValueError, UnicodeError):
        raise AcquisitionError('公共来源代理配置无效；仅支持无账号密码的 HTTP CONNECT 代理。', 'source_proxy_invalid', 503) from None


class _DeadlineRawReader(io.RawIOBase):
    """Recompute the remaining budget for each underlying socket read."""

    def __init__(self, sock, mode, deadline, timeout=None):
        self._socket = sock
        self._timeout = timeout if timeout is not None else partial(_source_timeout, deadline)
        self._raw = sock.makefile(mode, buffering=0)

    def readable(self):
        return True

    def readinto(self, target):
        self._socket.settimeout(self._timeout())
        return self._raw.readinto(target)

    def close(self):
        try:
            self._raw.close()
        finally:
            super().close()


class _DeadlineResponseSocket:
    def __init__(self, sock, deadline, timeout=None):
        self._socket = sock
        self._deadline = deadline
        self._timeout = timeout

    def makefile(self, mode):
        return io.BufferedReader(_DeadlineRawReader(self._socket, mode, self._deadline, self._timeout))


class _DeadlineHTTPResponse(http.client.HTTPResponse):
    """Bound raw reads and require complete chunk framing before reporting EOF.

    Framing failures carry no entity partial: callers archive IncompleteRead.partial.
    """

    def __init__(self, sock, *args, deadline, timeout=None, **kwargs):
        # Covers origin headers/chunk framing and HTTPConnection's CONNECT parser.
        super().__init__(_DeadlineResponseSocket(sock, deadline, timeout), *args, **kwargs)

    def _read_next_chunk_size(self):
        line = self.fp.readline(http.client._MAXLINE + 1)
        if len(line) > http.client._MAXLINE:
            raise http.client.LineTooLong('chunk size')
        if not line.endswith(b'\r\n'):
            raise http.client.IncompleteRead(b'')
        try:
            return int(line[:-2].split(b';', 1)[0], 16)
        except ValueError:
            self._close_conn()
            raise http.client.IncompleteRead(b'') from None

    def _read_and_discard_trailer(self):
        trailers_read = 0
        while True:
            line = self.fp.readline(http.client._MAXLINE + 1)
            if len(line) > http.client._MAXLINE:
                raise http.client.LineTooLong('trailer line')
            if not line.endswith(b'\r\n'):
                raise http.client.IncompleteRead(b'')
            if line == b'\r\n':
                return
            trailers_read += 1
            if trailers_read > http.client._MAXHEADERS:
                raise http.client.HTTPException(f'got more than {http.client._MAXHEADERS} trailers')

    def _get_chunk_left(self):
        chunk_left = self.chunk_left
        if not chunk_left:
            if chunk_left is not None:
                try:
                    delimiter = self._safe_read(2)
                except http.client.IncompleteRead:
                    # _safe_read's partial belongs to framing, not the entity.
                    raise http.client.IncompleteRead(b'') from None
                if delimiter != b'\r\n':
                    raise http.client.IncompleteRead(b'')
            chunk_left = self._read_next_chunk_size()
            if chunk_left == 0:
                self._read_and_discard_trailer()
                self._close_conn()
                chunk_left = None
            self.chunk_left = chunk_left
        return chunk_left


class _PublicTunnelConnection(http.client.HTTPConnection):
    """CONNECT a validated IP, then use the original origin's Host and TLS SNI."""

    def __init__(self, host, port, address, proxy_address, proxy_port, deadline, secure):
        super().__init__(host, port, timeout=_source_timeout(deadline))
        self.proxy_address = proxy_address
        self.proxy_port = proxy_port
        self.deadline = deadline
        self._context = ssl.create_default_context() if secure else None
        authority = f'[{address}]' if ':' in address else address
        self.set_tunnel(address, port, headers={'Host': f'{authority}:{port}'})

    def connect(self):
        self.sock = socket.create_connection(
            (self.proxy_address, self.proxy_port), _source_timeout(self.deadline))
        try:
            self.sock.settimeout(_source_timeout(self.deadline))
            try:
                self._tunnel()
            except OSError as error:
                # http.client reports a completed non-200 CONNECT as OSError.
                # An HTTP refusal is not a retryable transport failure.
                if str(error).startswith('Tunnel connection failed:'):
                    raise AcquisitionError('公共来源代理拒绝 CONNECT 请求。', 'source_blocked') from None
                raise
            self.sock.settimeout(_source_timeout(self.deadline))
            if self._context is not None:
                # HTTPSConnection would use _tunnel_host (the pinned IP) as SNI.
                self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
            self.sock.settimeout(_source_timeout(self.deadline))
        except BaseException:
            self.close()
            raise


_PUBLIC_FETCH_SCOPE = threading.local()
_MISSING_LIMITER = object()


def _normalized_hostname(hostname):
    try:
        return ipaddress.ip_address(hostname).compressed.lower()
    except ValueError:
        return hostname.encode('idna').decode('ascii').lower().rstrip('.')


class _PublicHostGate:
    def __init__(self):
        self.lock = threading.Lock()
        self.users = 0


class PublicFetchLimiter:
    """Bound scoped public fetches shared by workers in one collection run."""

    def __init__(self, max_concurrent=6):
        self._global = threading.BoundedSemaphore(max_concurrent)
        self._hosts_guard = threading.Lock()
        self._hosts = {}

    @contextmanager
    def scope(self):
        previous = getattr(_PUBLIC_FETCH_SCOPE, 'limiter', _MISSING_LIMITER)
        _PUBLIC_FETCH_SCOPE.limiter = self
        try:
            yield self
        finally:
            if previous is _MISSING_LIMITER:
                del _PUBLIC_FETCH_SCOPE.limiter
            else:
                _PUBLIC_FETCH_SCOPE.limiter = previous

    @contextmanager
    def _slot(self, hostname, deadline):
        hostname = _normalized_hostname(hostname)
        with self._hosts_guard:
            gate = self._hosts.get(hostname)
            if gate is None:
                gate = _PublicHostGate()
                self._hosts[hostname] = gate
            gate.users += 1

        host_acquired = False
        global_acquired = False
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not gate.lock.acquire(timeout=remaining):
                raise AcquisitionError('公共来源读取超时。', 'source_unavailable')
            host_acquired = True

            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._global.acquire(timeout=remaining):
                raise AcquisitionError('公共来源读取超时。', 'source_unavailable')
            global_acquired = True
            if deadline - time.monotonic() <= 0:
                raise AcquisitionError('公共来源读取超时。', 'source_unavailable')
            yield
        finally:
            try:
                if global_acquired:
                    self._global.release()
            finally:
                try:
                    if host_acquired:
                        gate.lock.release()
                finally:
                    with self._hosts_guard:
                        gate.users -= 1
                        if gate.users == 0 and self._hosts.get(hostname) is gate:
                            del self._hosts[hostname]


def _source_response(body, original, resolved, content_type, http_status=200, complete=True):
    """Private diagnostics are evidence, never a successful article document."""
    return {'canonical_url': original, 'resolved_url': resolved, 'content_type': content_type,
            'http_status': http_status, 'fetched_at': datetime.now(timezone.utc).isoformat(),
            'raw_response_base64': base64.b64encode(body).decode('ascii'),
            'raw_response_digest': hashlib.sha256(body).hexdigest(),
            'raw_response_complete': complete}


def _http_source_error(error, response, response_sock, deadline, original, resolved, max_bytes):
    """Retain a bounded refusal body without retrying or relaxing the GET budget."""
    limit = min(max_bytes, MAX_SOURCE_ERROR_BODY)
    chunks, total, complete = [], 0, False
    try:
        while total <= limit:
            remaining = _source_timeout(deadline)
            if response_sock is not None and response_sock.fileno() >= 0:
                response_sock.settimeout(remaining)
            chunk = response.read1(min(65536, limit + 1 - total))
            if not chunk:
                complete = True
                break
            chunks.append(chunk)
            total += len(chunk)
    except http.client.IncompleteRead as interrupted:
        chunks.append(interrupted.partial[:max(0, limit + 1 - total)])
    except (AcquisitionError, OSError, http.client.HTTPException):
        # Preserve the original refusal even if its diagnostic body is incomplete.
        pass
    declared = response.getheader('Content-Length')
    if declared is not None and (re.fullmatch('[0-9]{1,20}', declared) is None or int(declared) != total):
        complete = False
    error.source_response = _source_response(b''.join(chunks)[:limit], original, resolved,
        response.getheader('Content-Type', ''), response.status, complete)
    error.source_response['content_encoding'] = response.getheader('Content-Encoding', 'identity')
    return error


def fetch_public_resource(url, max_bytes=MAX_BODY, deadline=None):
    """Pinned public GET through optional inherited HTTP CONNECT configuration.

    Revalidate every redirect and retry only DNS/transport failures, at most
    three times for the entire fetch. Never send cookies or source credentials.
    """
    deadline = deadline if deadline is not None else time.monotonic() + SOURCE_TIMEOUT
    limiter = getattr(_PUBLIC_FETCH_SCOPE, 'limiter', None)
    original = current = canonical_url(url)
    retries = 0
    for _ in range(5):
        parts = urlsplit(current)
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        proxy = _source_proxy(parts)
        address_attempt = 0
        while True:
            slot = limiter._slot(parts.hostname, deadline) if limiter is not None else nullcontext()
            try:
                with slot:
                    try:
                        addresses = _public_addresses(parts.hostname, port, _source_timeout(deadline))
                    except AcquisitionError as error:
                        if (error.code != 'unsafe_source'
                                or getattr(error, '_os_dns_host', None) != parts.hostname):
                            raise
                        from nannanews.source_dns import SourceDnsError, is_fake_ip_answer, resolve_public_addresses
                        if not is_fake_ip_answer(parts.hostname, getattr(error, '_os_dns_addresses', ())):
                            raise
                        try:
                            addresses = resolve_public_addresses(parts.hostname, proxy, deadline)
                        except SourceDnsError as failure:
                            raise AcquisitionError(str(failure), 'source_dns_unavailable') from None
                    address = addresses[address_attempt % len(addresses)]
                    if proxy is not None:
                        proxy_addresses = _resolve_addresses(proxy[0], proxy[1], _source_timeout(deadline))
                        from nannanews.source_dns import _fake_address
                        if any(_fake_address(value) for value in proxy_addresses):
                            raise AcquisitionError('继承代理本身解析到基准测试/Fake-IP 地址；未连接该地址或改变路由。', 'source_dns_unavailable')
                        conn = _PublicTunnelConnection(
                            parts.hostname, port, address,
                            proxy_addresses[address_attempt % len(proxy_addresses)], proxy[1],
                            deadline, parts.scheme == 'https')
                    else:
                        connection_type = _PinnedHTTPS if parts.scheme == 'https' else _PinnedHTTP
                        conn = connection_type(parts.hostname, port, address, _source_timeout(deadline))
                        conn._source_deadline = deadline
                    conn.response_class = partial(_DeadlineHTTPResponse, deadline=deadline)
                    response = None
                    chunks = []
                    try:
                        path = parts.path + ('?' + parts.query if parts.query else '')
                        _source_timeout(deadline)
                        headers = {
                            'Host': parts.netloc, 'User-Agent': 'NannaPublicNews/1.0',
                            'Accept': 'text/html,application/json,image/*;q=0.8',
                            'Accept-Encoding': 'identity'}
                        if parts.hostname == 'mp.weixin.qq.com':
                            # The saved public-response probe recovered the full
                            # article with these ordinary first-request headers.
                            # Never switch profiles or retry after an access gate.
                            headers['User-Agent'] = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                'AppleWebKit/537.36 (KHTML, like Gecko) '
                                'Chrome/131.0.0.0 Safari/537.36')
                            headers['Accept-Language'] = 'zh-CN,zh;q=0.9'
                        conn.request('GET', path, headers=headers)
                        remaining = _source_timeout(deadline)
                        # getresponse() may detach a close-delimited socket from
                        # conn; retain it so body reads still share the deadline.
                        response_sock = conn.sock
                        if response_sock is not None:
                            response_sock.settimeout(remaining)
                        response = conn.getresponse()
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.getheader('Location')
                            if not location:
                                raise _http_source_error(AcquisitionError('来源重定向没有目标。', 'source_blocked'),
                                    response, response_sock, deadline, original, current, max_bytes)
                            current = canonical_url(urljoin(current, location))
                            break
                        if response.status != 200:
                            raise _http_source_error(AcquisitionError(f'公开来源返回 HTTP {response.status}；可提供有权公开的正文或图片。', 'source_blocked'),
                                response, response_sock, deadline, original, current, max_bytes)
                        if response.getheader('Content-Encoding', 'identity').lower() not in {'identity', ''}:
                            raise _http_source_error(AcquisitionError('来源返回不支持的压缩内容。', 'source_blocked'),
                                response, response_sock, deadline, original, current, max_bytes)
                        declared = response.getheader('Content-Length')
                        if declared is not None and (re.fullmatch('[0-9]{1,20}', declared) is None or int(declared) > max_bytes):
                            raise _http_source_error(AcquisitionError('来源内容超过读取上限。', 'source_too_large'),
                                response, response_sock, deadline, original, current, max_bytes)
                        total = 0
                        while True:
                            remaining = _source_timeout(deadline)
                            if response_sock is not None and response_sock.fileno() >= 0:
                                response_sock.settimeout(remaining)
                            chunk = response.read1(min(65536, max_bytes + 1 - total))
                            if not chunk:
                                break
                            chunks.append(chunk)
                            total += len(chunk)
                            if total > max_bytes:
                                error = AcquisitionError('来源内容超过读取上限。', 'source_too_large')
                                error.source_response = _source_response(b''.join(chunks)[:MAX_SOURCE_ERROR_BODY],
                                    original, current, response.getheader('Content-Type', ''), response.status, False)
                                raise error
                        if declared is not None and total != int(declared):
                            error = AcquisitionError('来源响应不完整；请重新提供完整公开内容。', 'source_incomplete')
                            error.source_response = _source_response(b''.join(chunks)[:MAX_SOURCE_ERROR_BODY],
                                original, current, response.getheader('Content-Type', ''), response.status, False)
                            raise error
                        return b''.join(chunks), response.getheader('Content-Type', ''), current
                    except (AcquisitionError, OSError, http.client.HTTPException) as error:
                        if response is not None and getattr(error, 'source_response', None) is None:
                            if isinstance(error, http.client.IncompleteRead) and error.partial:
                                chunks.append(error.partial)
                            error.source_response = _source_response(b''.join(chunks)[:MAX_SOURCE_ERROR_BODY],
                                original, current, response.getheader('Content-Type', ''), response.status, False)
                        raise
                    finally:
                        try:
                            if response is not None:
                                response.close()
                        finally:
                            conn.close()
            except AcquisitionError as error:
                if error.code != 'source_unavailable' or retries >= 3 or deadline <= time.monotonic():
                    raise
            except ssl.SSLCertVerificationError:
                raise AcquisitionError('公共来源 TLS 证书验证失败。', 'source_blocked') from None
            except (OSError, http.client.IncompleteRead) as error:
                if retries >= 3 or deadline <= time.monotonic():
                    failure = AcquisitionError('公共来源连接失败（首次请求及至多 3 次网络重试均未成功）；可提供有权公开的正文或图片。', 'source_unavailable')
                    if getattr(error, 'source_response', None) is not None:
                        failure.source_response = error.source_response
                    raise failure from None
            except http.client.HTTPException as error:
                failure = AcquisitionError('公共来源 HTTP 响应无效；可提供有权公开的正文或图片。', 'source_unavailable')
                if getattr(error, 'source_response', None) is not None:
                    failure.source_response = error.source_response
                raise failure from None
            _source_timeout(deadline)
            retries += 1
            address_attempt += 1
    raise AcquisitionError('来源重定向次数超过上限。', 'source_blocked')


def _job_api(url):
    parts = urlsplit(url)
    if parts.hostname != 'job.nju.edu.cn':
        return None
    match = re.fullmatch(r'/career/(specifics|mutual-selections|info)/([A-Za-z0-9][A-Za-z0-9_-]*)', parts.path.rstrip('/'))
    if match:
        kind, identity = match.groups()
        api = 'content/informations' if kind == 'info' else f'job/fair/{kind}'
        return f'https://job.nju.edu.cn/api/career/{api}/{identity}'
    if parts.path.rstrip('/') == '/career/jobs-v2':
        ids = parse_qs(parts.query).get('recruitmentId', [])
        if len(ids) == 1 and re.fullmatch(r'[0-9]+', ids[0]):
            return f'https://job.nju.edu.cn/api/career/job/recruitments/{ids[0]}'
    raise AcquisitionError('就业门户需提供具体详情链接（含完整活动或招聘 ID），不能导入列表空壳。', 'source_empty')


def parse_source_date(value):
    """Parse explicit source publication metadata, never a date found in prose."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if re.match(r'^\d{4}-\d{2}-\d{2}[ T]\d{2}:', value):
        try:
            timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if timestamp.tzinfo is not None:
                timestamp = timestamp.astimezone(SOURCE_TIMEZONE)
            parsed = timestamp.date()
        except (ValueError, OverflowError):
            return None
        return parsed.isoformat() if parsed.year > 1970 else None
    match = re.match(r'^\s*(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})(?:日)?(?:$|[ T])', value)
    if not match:
        return None
    try:
        parsed = date(*map(int, match.groups()))
    except ValueError:
        return None
    return parsed.isoformat() if parsed.year > 1970 else None


def _job_publication_text(record):
    return record.get('publishAt') or record.get('publishedAt') or record.get('publishTime') or record.get('publicationDate')


def _source_publication_date(document):
    raw_date = document.get('raw_date_text')
    if document.get('source_type') == 'nju_job_api':
        # Historic intake used applyAt here; it is not publication metadata.
        raw_date = None
        try:
            payload = json.loads(document.get('raw_content', ''))
            record = payload.get('data', payload) if isinstance(payload, dict) else None
        except (TypeError, ValueError):
            record = None
        if isinstance(record, dict):
            raw_date = _job_publication_text(record)
    return parse_source_date(raw_date) or parse_source_date(document.get('source_published_date'))


def _publication_metadata(soup):
    for node in soup.find_all('meta'):
        key = str(node.get('property') or node.get('name') or node.get('itemprop') or '').lower()
        if key in ('article:published_time', 'datepublished', 'pubdate', 'publishdate', 'publish_date', 'publication_date'):
            value = node.get('content', '')
            if parse_source_date(value):
                return value
    node = soup.select_one('#publish_time, time[itemprop="datePublished"], time.pubdate, .publish_time, .publish-date')
    if node:
        value = node.get('datetime') or node.get_text(' ', strip=True)
        if parse_source_date(value):
            return value
    for node in soup.find_all('script', type='application/ld+json'):
        try:
            value = json.loads(node.string or node.get_text())
        except (ValueError, TypeError):
            continue
        objects = value if isinstance(value, list) else [value]
        for obj in objects:
            if isinstance(obj, dict) and parse_source_date(obj.get('datePublished')):
                return obj['datePublished']
    label = re.search(r'(?:发布时间|发布日期|发布于)\s*[:：]?\s*(\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}日?)', soup.get_text(' ', strip=True))
    return label.group(1) if label and parse_source_date(label.group(1)) else None


def _pdf_preview_images(script, base_url):
    """Accept only a complete static viewer declaration, without executing JS."""
    source = str(script.string or '')
    if not re.search(r'\bshowVsbpdfIframe\s*\(', source):
        return []

    def incomplete():
        raise AcquisitionError('PDF 预览页清单含动态代码或不完整数据；原始页面已保留，未执行脚本。', 'source_incomplete')

    assignment = re.match(r'\s*(?:var|let|const)\s+vsb_pdf_image_data\s*=\s*', source)
    if assignment is None:
        incomplete()
    decoder = json.JSONDecoder()
    remaining = source[assignment.end():]
    try:
        pages, end = decoder.raw_decode(remaining)
        if not isinstance(pages, list) or not pages or any(not isinstance(page, str) or not page.strip() for page in pages):
            incomplete()
        call = re.match(r'\s*;\s*showVsbpdfIframe\s*\(\s*', remaining[end:])
        if call is None:
            incomplete()
        remaining = remaining[end:][call.end():]
        # The publisher's viewer has five literal arguments followed by the page list.
        for index in range(5):
            value, end = decoder.raw_decode(remaining)
            if type(value) not in (str, int, float) or (isinstance(value, float) and not math.isfinite(value)):
                incomplete()
            if index == 0:
                if not isinstance(value, str):
                    incomplete()
                canonical_url(urljoin(base_url, value))
            remaining = remaining[end:].lstrip()
            if not remaining.startswith(','):
                incomplete()
            remaining = remaining[1:].lstrip()
        if not re.fullmatch(r'vsb_pdf_image_data\s*\)\s*;\s*', remaining):
            incomplete()
    except ValueError:
        incomplete()
    return [canonical_url(urljoin(base_url, page)) for page in pages]


def _wechat_static_content(soup):
    """Read literal article HTML and picture-page CDN declarations, never source JS.

    Recovery still requires actual retained article bytes, not an access gate.
    Picture metadata contributes only each page's direct public cdn_url.
    """
    def incomplete():
        raise AcquisitionError('微信文章静态正文声明含动态表达式、歧义或不完整转义；原始响应已保留，未执行脚本。', 'source_incomplete')

    def literal(source, start):
        quote, index, result = source[start], start + 1, []
        escapes = {'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f', 'v': '\v',
                   "'": "'", '"': '"', '\\': '\\', '/': '/'}
        while index < len(source):
            char = source[index]
            index += 1
            if char == quote:
                return ''.join(result), index
            if char in '\r\n\u2028\u2029':
                incomplete()
            if char != '\\':
                result.append(char)
                continue
            if index >= len(source):
                incomplete()
            escape = source[index]
            index += 1
            if escape in escapes:
                result.append(escapes[escape])
            elif escape == '0' and (index == len(source) or not source[index].isdigit()):
                result.append('\0')
            elif escape in {'x', 'u'}:
                count = 2 if escape == 'x' else 4
                digits = source[index:index + count]
                if len(digits) != count or re.fullmatch('[0-9a-fA-F]+', digits) is None:
                    incomplete()
                value = int(digits, 16)
                index += count
                if 0xD800 <= value <= 0xDBFF:
                    if source[index:index + 2] != '\\u':
                        incomplete()
                    low = source[index + 2:index + 6]
                    if len(low) != 4 or re.fullmatch('[0-9a-fA-F]{4}', low) is None or not 0xDC00 <= int(low, 16) <= 0xDFFF:
                        incomplete()
                    value = 0x10000 + ((value - 0xD800) << 10) + int(low, 16) - 0xDC00
                    index += 6
                elif 0xDC00 <= value <= 0xDFFF:
                    incomplete()
                result.append(chr(value))
            elif escape in '\r\n':
                if escape == '\r' and source[index:index + 1] == '\n':
                    index += 1
            else:
                incomplete()
        incomplete()

    def picture_pages(source, start):
        # Only direct cdn_url fields of literal page objects are article media;
        # nested covers, watermarks and original_info are not alternate sources.
        if source[start:start + 1] != '[':
            incomplete()
        index, stack, urls, current, previous, expect_record = start + 1, [']'], [], None, None, True
        while index < len(source):
            char = source[index]
            if char.isspace():
                index += 1
                continue
            if source.startswith('//', index):
                end = source.find('\n', index + 2)
                index = len(source) if end < 0 else end + 1
                continue
            if source.startswith('/*', index):
                end = source.find('*/', index + 2)
                if end < 0:
                    incomplete()
                index = end + 2
                continue
            if len(stack) == 1:
                if char == ']':
                    return urls, index + 1
                if not expect_record:
                    if char != ',':
                        incomplete()
                    expect_record = True
                    index += 1
                    continue
                if char != '{':
                    incomplete()
                stack.append('}')
                current, previous, expect_record = None, '{', False
                index += 1
                continue
            member = stack[-1] == '}' and previous in {'{', ','}
            word = None
            if char in "'\"":
                word, index = literal(source, index)
            else:
                token = identifier.match(source, index)
                if token is not None:
                    word, index = token.group(), token.end()
            if member and char != '}':
                if word is None:
                    incomplete()
                while index < len(source) and source[index].isspace():
                    index += 1
                if source[index:index + 1] != ':':
                    incomplete()
                index += 1
                while index < len(source) and source[index].isspace():
                    index += 1
            if word is not None:
                if member and len(stack) == 2 and word == 'cdn_url':
                    if current is not None or source[index:index + 1] not in {"'", '"'}:
                        incomplete()
                    current, index = literal(source, index)
                    while index < len(source) and source[index].isspace():
                        index += 1
                    if not current or source[index:index + 1] not in {',', '}'}:
                        incomplete()
                previous = 'value'
                continue
            if char in '{[':
                stack.append('}' if char == '{' else ']')
                if len(stack) > 64:
                    incomplete()
            elif char in '}]':
                if char != stack[-1]:
                    incomplete()
                stack.pop()
                if len(stack) == 1:
                    if current is None or len(urls) >= MAX_IMAGES:
                        incomplete()
                    urls.append(current)
            elif char in '()`;=/':
                incomplete()
            previous = char
            index += 1
        incomplete()


    values, picture_values = [], []
    identifier = re.compile(r'[A-Za-z_$][A-Za-z0-9_$]*')
    for script in soup.find_all('script'):
        source, index, previous = str(script.string or ''), 0, None
        while index < len(source):
            char = source[index]
            if char.isspace():
                index += 1
                continue
            if source.startswith('//', index):
                end = source.find('\n', index + 2)
                index = len(source) if end < 0 else end + 1
                continue
            if source.startswith('/*', index):
                end = source.find('*/', index + 2)
                index = len(source) if end < 0 else end + 2
                continue
            if char in "'\"`/":
                # Ignore unrelated strings/templates/regular expressions, not
                # their apparent field names. No string is run as source code.
                delimiter, index, bracket = char, index + 1, False
                while index < len(source):
                    current = source[index]
                    index += 1
                    if current == '\\':
                        index += 1
                    elif delimiter == '/' and current in '[]':
                        bracket = current == '['
                    elif current == delimiter and not bracket:
                        break
                    elif delimiter == '/' and current in '\r\n':
                        break
                previous = 'value'
                continue
            token = identifier.match(source, index)
            if token is not None:
                word = token.group()
                index += len(word)
                if word in {'content_noencode', 'picture_page_info_list'} and previous in {'{', ','}:
                    tail = index
                    while tail < len(source) and source[tail].isspace():
                        tail += 1
                    if source[tail:tail + 1] == ':':
                        tail += 1
                        while tail < len(source) and source[tail].isspace():
                            tail += 1
                        if word == 'content_noencode':
                            if source[tail:tail + 1] not in {"'", '"'}:
                                incomplete()
                            value, index = literal(source, tail)
                        else:
                            if picture_values:
                                incomplete()
                            value, index = picture_pages(source, tail)
                        tail = index
                        while tail < len(source) and source[tail].isspace():
                            tail += 1
                        if source[tail:tail + 1] not in {',', '}'}:
                            incomplete()
                        if word == 'content_noencode':
                            values.append(value)
                            if len(values) > 1:
                                incomplete()
                        else:
                            picture_values.append(value)
                previous = word
            else:
                previous = char
                index += 1
    if not values and not picture_values:
        return None
    content = BeautifulSoup(values[0] if values else '', 'html.parser')
    for url in picture_values[0] if picture_values else []:
        content.append(content.new_tag('img', src=url))
    return content


def _html_content(raw, base_url, wechat=False):
    from nannanews.source_media import excluded_media_type, merge_skipped_media, skipped_media_record

    soup = BeautifulSoup(raw, 'html.parser')
    raw_date = _publication_metadata(soup)
    content = soup.select_one('#js_content') if wechat else (
        soup.select_one('.infobox .article') or soup.select_one('.v_news_content')
        or soup.select_one('.wp_articlecontent') or soup.select_one('.w-content')
        or soup.select_one('.wzy-content')
        or soup.select_one('.article_content')
        or soup.select_one('.mn_content .table-style')
        or soup.find('article') or soup.select_one('.article.w') or soup.find('main') or soup.body or soup)
    if not wechat:
        article_body = content.select_one('.wp_articlecontent')
        if article_body is None and 'wp_articlecontent' in content.get('class', []):
            article_body = content
        if article_body is not None:
            article = article_body.find_parent(class_='article')
            # A single titled article with labelled sibling details owns more
            # than its rich-text body (including metadata and a sibling poster).
            # Ordinary WebPlus title/publisher/counter chrome still stays out.
            structured_article = (
                article is not None
                and article.find('h1', recursive=False) is not None
                and len(article.select('.wp_articlecontent')) == 1
                and not article.select('article, .article')
                and any(detail.find_parent(class_='wp_articlecontent') is None
                        for detail in article.select('.detailbox > .tt + .con'))
            )
            content = article if structured_article else article_body
    if wechat:
        # The observed captcha page has no article container. A missing container
        # alone proves neither deletion nor an access gate.
        empty_article = content is None or (not content.get_text(strip=True) and not content.find('img'))
        if urlsplit(base_url).path == '/mp/wappoc_appmsgcaptcha':
            raise AcquisitionError('微信来源返回环境验证页面；未尝试完成或绕过验证。', 'source_blocked')
        if empty_article:
            visible = soup.get_text(' ', strip=True)
            if re.search(r'访问过于频繁|操作频繁', visible):
                raise AcquisitionError('微信来源返回访问频率限制；请稍后通过正常公开渠道取得正文或图片。', 'source_blocked')
            if re.search(r'环境异常|请完成.{0,8}验证|完成验证后', visible):
                raise AcquisitionError('微信来源返回环境或安全验证；未尝试完成或绕过验证。', 'source_blocked')
            if re.search(r'该内容已被发布者删除|内容已删除|此内容因违规无法查看', visible):
                raise AcquisitionError('微信来源明确提示内容已被移除；当前响应不含公开正文。', 'source_blocked')
            if re.search(r'付费后(?:可)?(?:阅读|查看)(?:全文|完整内容)|该内容需付费|请登录后(?:查看|阅读)|登录后(?:可)?阅读全文', visible):
                raise AcquisitionError('微信来源明确要求登录或付费后阅读；未尝试获取受限内容。', 'source_blocked')
            if content is None and not visible and soup.find(['script', 'img']) is None:
                raise AcquisitionError('微信来源返回空页面，未取得公开正文或图片。', 'source_empty')
        if content is None:
            content = _wechat_static_content(soup)
            if content is None:
                raise AcquisitionError('微信页面未包含已支持的公开文章布局；原始响应已保留，不能据此判定验证或删除。', 'source_unsupported_layout')
    title_node = soup.select_one('#activity-name') if wechat else None
    title_node = title_node or content.find('h1') or soup.find('h1') or soup.find('title')
    title = title_node.get_text(' ', strip=True) if title_node else ''
    title_metadata = soup.find('meta', attrs={'property': 'og:title'}) if wechat else None
    if title_metadata is not None and soup.select_one('#activity-name') is None:
        title = str(title_metadata.get('content') or title)
    decorations = ['style', 'noscript', 'nav', 'footer', 'form']
    if content is soup or content is soup.body:
        decorations.append('header')
    for node in content(decorations):
        node.decompose()
    images, capture_errors, pdf_sources, skipped_media = [], [], [], []

    def resource_url(src):
        try:
            return canonical_url(urljoin(base_url, src))
        except ValueError:
            raise AcquisitionError('公开来源媒体地址格式无效；原始响应已保留。', 'invalid_url') from None

    for viewer in content.select('.wp_pdf_player, .edui-pdf'):
        pdf = viewer.get('pdfsrc') or viewer.get('data-pdf')
        if pdf:
            pdf_sources.append(resource_url(pdf))
        else:
            capture_errors.append({'stage': 'parse', 'url': base_url, 'code': 'source_incomplete',
                'message': '嵌入式 PDF 阅读器缺少公开 PDF 地址；未执行脚本或导入不完整正文。'})

    def add_image(src, declared_type=''):
        if not src or src.startswith('data:'):
            return
        kind = excluded_media_type(src, declared_type=declared_type)
        try:
            image_url = resource_url(src)
        except AcquisitionError:
            if kind is None:
                raise
            image_url = None
        if kind is not None:
            merge_skipped_media(skipped_media, skipped_media_record(kind, image_url, 'declared_' + kind + '_excluded'))
        else:
            images.append(image_url)

    nodes = content.find_all(True)
    if content.has_attr('style'):
        nodes.insert(0, content)
    for node in nodes:
        if node.name != 'script' and node.has_attr('style'):
            for declaration in tinycss2.parse_blocks_contents(node['style'], skip_comments=True, skip_whitespace=True):
                if declaration.type != 'declaration' or declaration.lower_name != 'background-image':
                    continue
                for token in declaration.value:
                    src = token.value if token.type == 'url' else None
                    if token.type == 'function' and token.lower_name == 'url':
                        values = [value for value in token.arguments if value.type not in {'whitespace', 'comment'}]
                        if len(values) == 1 and values[0].type == 'string':
                            src = values[0].value
                    if src and not src.startswith('#'):
                        add_image(src)
        if node.name == 'script':
            try:
                images.extend(_pdf_preview_images(node, base_url))
            except AcquisitionError as error:
                capture_errors.append({'stage': 'parse', 'url': base_url, 'code': error.code, 'message': str(error)})
        elif node.name == 'img':
            src = node.get('data-src') or node.get('src')
            # VSB republishes WeChat articles with a local rendered image but
            # may leave the original, stale lazy-loading URL in data-src.
            if (not wechat and str(node.get('src') or '').startswith('/__local/')
                    and node.find_parent(class_='v_news_content') is not None):
                src = node['src']
            add_image(src, node.get('data-type') or node.get('type') or '')
        elif node.name in {'video', 'source', 'iframe', 'object', 'embed'}:
            src = node.get('data-src') or node.get('src') or node.get('data')
            declared_type = str(node.get('type') or node.get('data-type') or '')
            try:
                media_path = urlsplit(urljoin(base_url, src)).path if src else ''
            except ValueError:
                media_path = ''
            if declared_type.split(';', 1)[0].lower() == 'application/pdf' or media_path.lower().endswith('.pdf'):
                if src:
                    pdf_sources.append(resource_url(src))
                else:
                    capture_errors.append({'stage': 'parse', 'url': base_url, 'code': 'source_incomplete',
                        'message': '嵌入式 PDF 缺少公开地址，未将其当作可忽略的视频。'})
                continue
            if node.name == 'video':
                add_image(node.get('poster'))
                declared_type = 'video'
                if not src and any(child.get('src') or child.get('data-src') for child in node.find_all('source')):
                    continue
            elif node.name == 'source' and node.parent.name == 'video':
                declared_type = 'video'
            elif node.name == 'iframe' and 'video_iframe' in node.get('class', []):
                declared_type = 'video'
            kind = excluded_media_type(src or '', declared_type=declared_type)
            if kind is not None:
                try:
                    media_url = resource_url(src) if src else None
                except AcquisitionError:
                    media_url = None  # No invented HTTP URL for blob/JS-only media.
                merge_skipped_media(skipped_media, skipped_media_record(kind, media_url, 'declared_' + kind + '_excluded'))
    for node in content('script'):
        node.decompose()
    text = content.get_text('\n', strip=True)
    if len(text) > MAX_TEXT:
        raise AcquisitionError('正文超过提取上限，请提供聚焦的公开内容。', 'source_too_large')
    if len(set(pdf_sources)) > MAX_IMAGES:
        raise AcquisitionError('公开文章声明的 PDF 数量超过完整采集上限；未截取部分附件。', 'source_too_large')
    # Publication metadata was captured before removing script/header decorations.
    return title, text, list(dict.fromkeys(images)), raw_date, capture_errors, list(dict.fromkeys(pdf_sources)), skipped_media


def _job_content(value):
    """Exclude portal engagement/audit metadata, retaining actual article fields."""
    if isinstance(value, dict):
        volatile = {'trafficStat', 'createdBy', 'updatedBy', 'createdAt', 'updatedAt', 'sticky'}
        return {key: _job_content(item) for key, item in value.items() if key not in volatile}
    if isinstance(value, list):
        return [_job_content(item) for item in value]
    return value


def _digest(document):
    identity = {key: document.get(key) for key in ('canonical_url', 'title', 'text', 'images', 'source_type', 'raw_date_text', 'raw_content')}
    if document.get('source_type') == 'nju_job_api':
        try:
            payload = json.loads(document.get('raw_content', ''))
            record = payload.get('data', payload) if isinstance(payload, dict) else None
        except (TypeError, ValueError):
            record = None
        if isinstance(record, dict):
            # Raw responses remain immutable evidence, not semantic article identity.
            # This also normalizes historical saved job evidence without a model call.
            for key in ('text', 'raw_content', 'raw_date_text'):
                identity.pop(key)
            identity['content'] = _job_content(record)
    identity['media'] = [item['digest'] for item in document.get('media', [])]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def fetch_document(url):
    original = canonical_url(url)
    api = _job_api(original)
    body = None
    try:
        body, content_type, resolved = fetch_public_resource(api or original)
        return _document_from_response(original, api, body, content_type, resolved)
    except AcquisitionError as error:
        if getattr(error, 'source_response', None) is not None:
            # The documented job API is a transport location, not article identity.
            error.source_response['canonical_url'] = original
        elif body is not None:
            error.source_response = _source_response(body, original, resolved, content_type)
        raise


def reparse_source_document(document):
    """Pure local reparse of immutable response bytes; never fetch or reuse media."""
    original = canonical_url(document.get('canonical_url'))
    resolved = canonical_url(document.get('resolved_url'))
    encoded = document.get('raw_response_base64')
    if (original != document.get('canonical_url') or resolved != document.get('resolved_url')
            or not isinstance(encoded, str) or len(encoded) > 4 * math.ceil(MAX_BODY / 3)
            or not isinstance(document.get('content_type'), str)
            or not isinstance(document.get('fetched_at'), str) or not document['fetched_at']):
        raise AcquisitionError('原始来源捕获格式无效，未重新获取来源。', 'source_invalid_capture')
    try:
        body = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise AcquisitionError('原始来源响应字节无效，未重新获取来源。', 'source_invalid_capture') from None
    if len(body) > MAX_BODY or hashlib.sha256(body).hexdigest() != document.get('raw_response_digest'):
        raise AcquisitionError('原始来源响应摘要不匹配，未重新获取来源。', 'source_invalid_capture')
    try:
        reparsed = _document_from_response(original, _job_api(original), body, document['content_type'], resolved)
    except AcquisitionError as error:
        error.source_response = _source_response(body, original, resolved, document['content_type'])
        error.source_response['fetched_at'] = document['fetched_at']
        raise
    reparsed['fetched_at'] = document['fetched_at']
    return reparsed


def _document_from_response(original, api, body, content_type, resolved):
    from nannanews.source_media import media_policy, merge_skipped_media

    charset = re.search(r'charset=([\w-]+)', content_type, re.I)
    try:
        raw = body.decode(charset.group(1) if charset else 'utf-8', errors='replace')
    except LookupError:
        raise AcquisitionError('来源编码不受支持。', 'source_blocked') from None
    source_type = 'nju_job_api' if api else ('wechat' if urlsplit(original).hostname == 'mp.weixin.qq.com' else 'public_html')
    capture_errors, pdf_sources, skipped_media = [], [], []
    if api:
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
            record = payload.get('data', payload)
            if not isinstance(record, dict) or record.get('status', 'PUBLISHED') != 'PUBLISHED' or record.get('enabled') is False or record.get('loginRequired') is True:
                raise ValueError
            if not any(record.get(key) for key in ('content', 'intro', 'invitationContent', 'positions', 'startAt', 'endAt', 'venue', 'address', 'deadline', 'company')):
                raise ValueError
            title = str(record.get('theme') or record.get('title') or '')
            # Parse HTML fields after JSON decoding, never escaped HTML inside raw JSON.
            rendered = json.dumps(_job_content(record), ensure_ascii=False, indent=2)
            images = []
            for key in ('content', 'intro', 'invitationContent'):
                if isinstance(record.get(key), str):
                    _, fragment, fragment_images, _, fragment_errors, fragment_pdfs, fragment_skips = _html_content(record[key], resolved)
                    rendered += '\n' + fragment
                    images += fragment_images
                    capture_errors.extend(fragment_errors)
                    pdf_sources.extend(fragment_pdfs)
                    for skipped in fragment_skips:
                        merge_skipped_media(skipped_media, skipped)
            text = rendered
            if len(text) > MAX_TEXT:
                raise AcquisitionError('就业门户详情超过正文上限，请提供相关公开内容。', 'source_too_large')
            raw_date = _job_publication_text(record)
        except (ValueError, TypeError):
            raise AcquisitionError('就业门户 API 未返回公开详情正文。', 'source_empty') from None
    else:
        if 'html' not in content_type.lower() and 'text/plain' not in content_type.lower():
            raise AcquisitionError('链接不是可读公开文章；请上传公开图片或粘贴正文。', 'source_empty')
        title, text, images, raw_date, capture_errors, pdf_sources, skipped_media = _html_content(raw, resolved, source_type == 'wechat')
        if source_type != 'wechat' and re.search(r'访问过于频繁|环境异常|请完成.{0,8}验证|该内容已被发布者删除|仅限校园网|当前IP.*非校园', text):
            raise AcquisitionError('来源返回访问限制或验证提示；请提供可公开的正文或图片。', 'source_blocked')
    if not text.strip() and not images and not pdf_sources and not capture_errors and not skipped_media:
        raise AcquisitionError('来源正文为空；请提供公开正文或图片，未创建草稿。', 'source_empty')
    if not api and source_type != 'wechat' and len(text.strip()) < 40 and not images and not pdf_sources and not capture_errors and not skipped_media:
        raise AcquisitionError('来源仅返回空壳或不足的正文；请提供公开正文或图片。', 'source_empty')
    document = {'canonical_url': original, 'resolved_url': resolved, 'title': title[:500], 'text': text, 'images': list(dict.fromkeys(images)), 'fetched_at': datetime.now(timezone.utc).isoformat(), 'source_type': source_type, 'raw_date_text': str(raw_date) if raw_date else None, 'raw_content': raw}
    document['raw_response_base64'] = base64.b64encode(body).decode('ascii')
    document['raw_response_digest'] = hashlib.sha256(body).hexdigest()
    document['content_type'] = content_type
    document['source_capture_version'] = SOURCE_CAPTURE_VERSION
    document['media_policy'] = media_policy()
    document['skipped_media'] = skipped_media
    document['pdf_sources'] = list(dict.fromkeys(pdf_sources))
    document['pdf_documents'] = []
    document['source_published_date'] = parse_source_date(raw_date)
    document['capture_complete'] = not capture_errors
    document['capture_errors'] = capture_errors
    document['digest'] = _digest(document)
    return document


def _image(data, source_url=''):
    """Capture original bytes; decoding/adaptation must not destroy raw evidence."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE:
        raise AcquisitionError('图片为空或超过 32 MiB 原始证据上限。', 'invalid_image')
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        mime = 'image/png'
    elif data.startswith(b'\xff\xd8\xff'):
        mime = 'image/jpeg'
    elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        mime = 'image/webp'
    elif data.startswith((b'GIF87a', b'GIF89a')):
        mime = 'image/gif'
    elif data.startswith(b'BM'):
        mime = 'image/bmp'
    elif data.startswith((b'II*\x00', b'MM\x00*', b'II+\x00', b'MM\x00+')):
        mime = 'image/tiff'
    elif re.search(br'<(?:[A-Za-z_][\w.-]*:)?svg(?:\s|>)', data[:4096]):
        mime = 'image/svg+xml'
    else:
        mime = 'application/octet-stream'
    return {'mime': mime, 'digest': hashlib.sha256(data).hexdigest(), 'data': base64.b64encode(data).decode('ascii'), 'url': source_url}


def _prepare_media(document, uploaded_images=None):
    from nannanews.source_media import excluded_media_type, media_policy, merge_skipped_media, skipped_media_record

    supplied = list(uploaded_images or [])
    remote = document.get('images', [])
    deadline = time.monotonic() + MEDIA_CAPTURE_TIMEOUT
    media, total = [], 0
    pdf_documents, pdf_pixels = [], 0
    errors = list(document.get('capture_errors') or [])
    capture_complete = document.get('capture_complete', True)
    skipped_media = [dict(item) for item in document.get('skipped_media', [])]
    excluded_urls = {item['url'] for item in skipped_media if item['url']}
    sources = [(url, None) for url in remote] + [('', data) for data in supplied]
    for index, (url, uploaded) in enumerate(sources):
        try:
            if uploaded is None:
                kind = excluded_media_type(url)
                if kind is not None:
                    merge_skipped_media(skipped_media, skipped_media_record(kind, url, 'declared_' + kind + '_excluded'))
                    excluded_urls.add(url)
                    continue
            if len(media) >= MAX_IMAGES:
                raise AcquisitionError(f'来源超过 {MAX_IMAGES} 张图片，未完整采集；已取得的原始证据已保留。', 'source_incomplete')
            remaining = MAX_MEDIA_BYTES - total
            if remaining <= 0:
                raise AcquisitionError('来源图片总量超过 128 MiB，未完整采集。', 'source_too_large')
            data = uploaded
            if data is None:
                image_deadline = min(deadline, time.monotonic() + SOURCE_TIMEOUT)
                data, content_type, _ = fetch_public_resource(url, min(MAX_IMAGE, remaining), image_deadline)
                kind = excluded_media_type(url, content_type=content_type, data=data)
                if kind is not None:
                    merge_skipped_media(skipped_media, skipped_media_record(kind, url, 'received_' + kind + '_excluded', data=data, complete=True))
                    excluded_urls.add(url)
                    total += len(data)
                    continue
            if len(data) > remaining:
                raise AcquisitionError('来源图片总量超过 128 MiB，未完整采集。', 'source_too_large')
            image = _image(data, url)
            # Keep the original index even when another source failed to download.
            if errors:
                image['source_image_index'] = index
            media.append(image)
            total += len(data)
        except AcquisitionError as error:
            response = getattr(error, 'source_response', None)
            if uploaded is None and isinstance(response, dict) and response.get('http_status') == 200:
                received = base64.b64decode(response['raw_response_base64'], validate=True)
                kind = excluded_media_type(url, content_type=response.get('content_type', ''), data=received)
                if kind is not None:
                    merge_skipped_media(skipped_media, skipped_media_record(kind, url,
                        'received_' + kind + '_excluded', data=received, complete=response['raw_response_complete']))
                    excluded_urls.add(url)
                    total += len(received)
                    continue
            errors.append({'image_index': index, 'url': url, 'code': error.code, 'message': str(error)})
            if getattr(error, 'source_response', None) is not None:
                errors[-1]['source_response'] = error.source_response
            if len(media) >= MAX_IMAGES:
                break
    if document.get('pdf_sources'):
        from nannanews.public_pdf import PublicPdfError, render_pdf_pages

        for pdf_url in document['pdf_sources']:
            try:
                remaining = MAX_MEDIA_BYTES - total
                if remaining <= 0 or len(media) >= MAX_IMAGES:
                    raise AcquisitionError('完整 PDF 超过文档剩余的图片数量或字节上限；未截取部分页面。', 'source_incomplete')
                pdf_deadline = min(deadline, time.monotonic() + SOURCE_TIMEOUT)
                body, content_type, resolved = fetch_public_resource(pdf_url, min(MAX_IMAGE, remaining), pdf_deadline)
                if len(body) > remaining:
                    raise AcquisitionError('完整 PDF 超过文档剩余的原始证据字节上限。', 'source_too_large')
                total += len(body)
                captured = _source_response(body, pdf_url, resolved, content_type)
                captured.update(render_complete=False, page_count=None, renderer=None, pages=[])
                pdf_documents.append(captured)
                rendered = render_pdf_pages(body, deadline=deadline, max_pages=MAX_IMAGES - len(media),
                    max_image_bytes=MAX_IMAGE, max_media_bytes=MAX_MEDIA_BYTES - total,
                    max_image_pixels=MAX_IMAGE_PIXELS, max_total_pixels=MAX_DOCUMENT_FRAME_PIXELS - pdf_pixels)
                for page in rendered['pages']:
                    image = _image(page['data'], document['canonical_url'])
                    image.update(source_pdf_url=pdf_url, source_pdf_digest=captured['raw_response_digest'],
                        source_pdf_page=page['page_number'], width=page['width'], height=page['height'])
                    media.append(image)
                    total += len(page['data'])
                    pdf_pixels += page['width'] * page['height']
                    captured['pages'].append({'page_number': page['page_number'], 'width': page['width'],
                        'height': page['height'], 'digest': image['digest'], 'diagnostics': page['diagnostics']})
                captured.update(render_complete=True, page_count=rendered['page_count'], renderer=rendered['renderer'])
            except (AcquisitionError, PublicPdfError, OSError) as error:
                failure = {'stage': 'pdf', 'url': pdf_url, 'code': getattr(error, 'code', 'source_pdf_unavailable'),
                           'message': str(error)}
                if getattr(error, 'source_response', None) is not None:
                    failure['source_response'] = error.source_response
                errors.append(failure)
    document['pdf_documents'] = pdf_documents
    document['media_policy'] = media_policy()
    document['skipped_media'] = skipped_media
    document['images'] = [url for url in remote if url not in excluded_urls]
    document['media'] = media
    document['capture_complete'] = capture_complete and not errors
    document['capture_errors'] = errors
    document['digest'] = _digest(document)
    return media


def _model_config():
    settings = runtime.settings
    if not settings.model_base_url:
        raise AcquisitionError('未配置模型 API URL。管理员请设置 NANNA_MODEL_BASE_URL（OpenAI 兼容 API 根地址，如 /v1）。', 'model_config_missing', 503)
    if not settings.model_api_key:
        raise AcquisitionError('未配置模型凭据。管理员请设置 NANNA_MODEL_API_KEY；凭据不会显示在页面。', 'model_config_missing', 503)
    if not isinstance(settings.model_api_key, str) or re.search(r'[\x00-\x1f\x7f]', settings.model_api_key):
        raise AcquisitionError('NANNA_MODEL_API_KEY 格式错误；请管理员重新设置凭据。', 'model_config_invalid', 503)
    try:
        parts = urlsplit(settings.model_base_url)
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or (parts.port is not None and not 1 <= parts.port <= 65535):
            raise ValueError
    except ValueError:
        raise AcquisitionError('NANNA_MODEL_BASE_URL 必须是无账号、查询或片段的 HTTP(S) API 根地址。', 'model_config_invalid', 503) from None
    if settings.model_api_style not in {'chat_completions', 'responses'}:
        raise AcquisitionError('NANNA_MODEL_API_STYLE 必须是 chat_completions 或 responses。', 'model_config_invalid', 503)
    return settings


class _ModelNetworkBudget:
    """One monotonic budget; optional diagnostics contain measured durations only."""

    def __init__(self, seconds):
        self.started = self.phase_started = time.monotonic()
        self.deadline = self.started + seconds
        self.phase = 'dns'
        self.finished = False
        self.metadata = {'budget_seconds': seconds, 'phases_seconds': {}}

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Model network deadline exceeded.')
        return remaining

    def stage(self, phase):
        now = time.monotonic()
        phases = self.metadata['phases_seconds']
        phases[self.phase] = phases.get(self.phase, 0.0) + now - self.phase_started
        self.phase, self.phase_started = phase, now

    def finish(self, error=None):
        if self.finished:
            return
        now = time.monotonic()
        phases = self.metadata['phases_seconds']
        phases[self.phase] = phases.get(self.phase, 0.0) + now - self.phase_started
        self.metadata['elapsed_seconds'] = now - self.started
        if isinstance(error, TimeoutError):
            self.metadata['timeout_stage'] = self.phase
        self.finished = True


class _ModelDeadlineSocket:
    """Reset the absolute remainder for every partial write, including TLS writes."""

    def __init__(self, sock, budget):
        self._socket = sock
        self._budget = budget

    def __getattr__(self, name):
        return getattr(self._socket, name)

    def sendall(self, data):
        view = memoryview(data)
        while view:
            self._socket.settimeout(self._budget.remaining())
            sent = self._socket.send(view)
            if sent <= 0:
                raise ConnectionResetError('Model connection closed during upload.')
            view = view[sent:]


def _parse_retry_after_seconds(value):
    """Return a safe positive delay from Retry-After, or None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if value.isascii() and value.isdigit():
        try:
            seconds = float(value)
        except (OverflowError, ValueError):
            return None
    else:
        try:
            retry_at = parsedate_to_datetime(value)
        except (OverflowError, TypeError, ValueError):
            return None
        if not isinstance(retry_at, datetime) or retry_at.utcoffset() is None:
            return None
        try:
            seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
        except (OverflowError, TypeError, ValueError):
            return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _model_post(payload, *, audit=None):
    """One direct paid POST, retaining bounded original response bytes privately."""
    from nannanews.model_audit import ModelAudit, response_metadata

    settings = _model_config()
    owned_audit = audit is None
    if owned_audit:
        try:
            audit = ModelAudit(settings.data_dir / 'model-audit', payload,
                request_limit=MAX_MODEL_REQUEST_BYTES, response_limit=MAX_MODEL_BODY,
                context={'api_style': settings.model_api_style})
        except Exception:
            raise AcquisitionError('模型审计准备失败；未发送请求。', 'model_audit_failed', 503) from None
    endpoint = '/responses' if settings.model_api_style == 'responses' else '/chat/completions'
    parts = urlsplit(settings.model_base_url.rstrip('/') + endpoint)
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    budget = _ModelNetworkBudget(settings.model_timeout)
    audit.metadata.update(upload_complete=False, network_timing=budget.metadata)
    conn = None
    response = None
    complete = False
    archive_error = None
    model_http_status = None
    model_retry_after_seconds = None
    transport_truncated = False
    try:
        try:
            addresses = _resolve_addresses(parts.hostname, port, budget.remaining())
        except AcquisitionError:
            budget.remaining()
            raise AcquisitionError('模型 API 域名解析失败或超时。请管理员检查配置。', 'model_unavailable', 502) from None
        connection_type = _PinnedHTTPS if parts.scheme == 'https' else _PinnedHTTP
        budget.stage('connect')
        for address in addresses:
            remaining = budget.remaining()
            candidate = connection_type(parts.hostname, port, address, min(10, remaining))
            candidate._model_budget = budget
            try:
                candidate.connect()
            except (OSError, http.client.HTTPException):
                candidate.close()
                continue
            conn = candidate
            break
        if conn is None:
            budget.remaining()
            raise AcquisitionError('模型 API 无可连接地址或连接超时。请管理员检查配置。', 'model_unavailable', 502)
        try:
            audit.save('sending')
        except Exception:
            raise AcquisitionError('模型审计写入失败；未发送请求。', 'model_audit_failed', 503) from None
        budget.stage('upload')
        budget.remaining()
        if conn.sock is not None:
            conn.sock = _ModelDeadlineSocket(conn.sock, budget)
        conn.response_class = partial(_DeadlineHTTPResponse, deadline=budget.deadline, timeout=budget.remaining)
        # Exactly the durable JSON entity, streamed without a second serialization.
        conn.request('POST', parts.path, body=audit.request, headers={
            'Authorization': 'Bearer ' + settings.model_api_key, 'Content-Type': 'application/json',
            'Content-Length': str(audit.metadata['request']['bytes']),
            'Accept': 'application/json', 'Accept-Encoding': 'identity'})
        audit.metadata['upload_complete'] = True
        budget.remaining()
        budget.stage('response_headers')
        response = conn.getresponse()
        model_http_status = response.status
        if type(model_http_status) is not int or not 100 <= model_http_status <= 599:
            model_http_status = None
        model_retry_after_seconds = _parse_retry_after_seconds(response.getheader('Retry-After'))
        budget.stage('response_body')
        try:
            audit.headers(response)
        except Exception as error:
            archive_error = error
        declared = response.getheader('Content-Length', '')
        while audit.response_size <= MAX_MODEL_BODY:
            budget.remaining()
            try:
                chunk = response.read1(min(65536, MAX_MODEL_BODY + 1 - audit.response_size))
            except http.client.IncompleteRead as error:
                transport_truncated = True
                try:
                    audit.append(error.partial[:MAX_MODEL_BODY + 1 - audit.response_size])
                except Exception as disk_error:
                    archive_error = disk_error
                    raise
                raise AcquisitionError('模型 API 响应不完整，未创建草稿。', 'model_response_invalid', 502,
                    model_transport_failure=True) from None
            if not chunk:
                transport_truncated = declared.isdigit() and int(declared) > audit.response_size
                complete = not declared or (declared.isdigit() and int(declared) == audit.response_size)
                break
            try:
                audit.append(chunk)
            except Exception as error:
                archive_error = error
                raise
        budget.finish()
        try:
            audit.preserve_response(complete=complete)
        except Exception as error:
            archive_error = error
            raise
        if archive_error is not None:
            raise AcquisitionError('模型响应已保留，但审计元数据写入失败；不会自动重试。', 'model_audit_failed', 503)
        if response.status != 200:
            raise AcquisitionError(
                f'模型 API 返回 HTTP {response.status}；未创建草稿，不会自动重试。',
                'model_http_error', 502, model_http_status=model_http_status,
                model_retry_after_seconds=model_retry_after_seconds,
                model_transport_failure=transport_truncated)
        if (audit.response_size > MAX_MODEL_BODY or (declared and
                (not declared.isdigit() or int(declared) > MAX_MODEL_BODY or int(declared) != audit.response_size))):
            raise AcquisitionError('模型响应大小无效或响应不完整，未创建草稿。', 'model_response_invalid', 502,
                model_transport_failure=transport_truncated)
        if response.getheader('Content-Encoding', 'identity').lower() not in {'identity', ''}:
            raise AcquisitionError('模型 API 返回不支持的压缩响应。', 'model_response_invalid', 502)
        try:
            result = audit.response_json()
        except (ValueError, UnicodeError):
            error = AcquisitionError('模型 API 没有返回合法 JSON。', 'model_response_invalid', 502)
            audit.response.seek(0)
            error._model_response = {'body_base64': base64.b64encode(audit.response.read()).decode('ascii')}
            raise error from None
        # Envelope metadata is private; retain original body regardless of validation.
        metadata = response_metadata(result)
        try:
            audit.save('transport_complete', provider_response=metadata)
        except Exception as error:
            archive_error = error
            raise
        return result
    except Exception as error:
        budget.finish(error)
        try:
            audit.preserve_response(complete=complete)
        except Exception as disk_error:
            archive_error = disk_error  # Bytes remain in the original file; durability is uncertain.
        if isinstance(error, AcquisitionError) and archive_error is None:
            safe_error = error
        elif isinstance(error, (OSError, http.client.HTTPException)) and archive_error is None:
            safe_error = AcquisitionError('模型 API 连接或审计写入失败；不会自动重试。', 'model_unavailable', 502)
        else:
            safe_error = AcquisitionError('模型审计写入失败；响应文件保留，不会自动重试。', 'model_audit_failed', 503)
        if model_http_status is not None:
            safe_error.model_http_status = model_http_status
        if model_retry_after_seconds is not None:
            safe_error.model_retry_after_seconds = model_retry_after_seconds
        if transport_truncated:
            safe_error.model_transport_failure = True
        safe_error._model_audit = audit.identity
        if archive_error is not None:
            safe_error._model_audit_finalize_failed = True
        safe_error._model_name = settings.model_name if hasattr(settings, 'model_name') else payload.get('model')
        try:
            audit.save('failed', failure_code=safe_error.code)
        except Exception:
            safe_error._model_audit_finalize_failed = True
        raise safe_error from None
    finally:
        if response is not None:
            response.close()
        if conn is not None:
            conn.close()
        if owned_audit:
            audit.close()




def _string(value, name, limit, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise AcquisitionError(f'模型字段 {name} 缺失或格式不正确。', 'model_output_invalid', 502)
    return value


def _optional_value(mapping, key, default):
    value = mapping.get(key)
    return default if value is None else value


def _validate_date(value):
    if value is not None:
        try:
            if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
                raise ValueError
            date.fromisoformat(value)
        except ValueError:
            raise AcquisitionError('模型日期不是有效的完整 ISO 日期。', 'model_output_invalid', 502) from None
    return value


def _validate_items(output, document):
    if not isinstance(output, dict) or not isinstance(output.get('items'), list) or not output['items']:
        raise AcquisitionError('模型必须返回包含非空事件列表的 items JSON 对象。', 'model_output_invalid', 502)
    normalized = []
    seen_events = set()
    for item in output['items']:
        if not isinstance(item, dict):
            raise AcquisitionError('模型事件不是 JSON 对象。', 'model_output_invalid', 502)
        if not isinstance(item.get('tag'), str) or item['tag'] not in TAGS:
            raise AcquisitionError('模型事件分类不符合草稿格式。', 'model_output_invalid', 502)
        draft = {key: _string(item.get(key), key, limit, True).strip()
                 for key, limit in (('title', 250), ('description', 12000))}
        draft['short_title'] = _string(_optional_value(item, 'short_title', ''), 'short_title', 250).strip() or draft['title']
        draft['date_uncertainty'] = _string(_optional_value(item, 'date_uncertainty', ''), 'date_uncertainty', 1000)
        quotes = _optional_value(item, 'evidence_quotes', [])
        if not isinstance(quotes, list) or len(quotes) > 20:
            raise AcquisitionError('模型依据列表格式不正确。', 'model_output_invalid', 502)
        normalized_quotes = []
        for evidence in quotes:
            if not isinstance(evidence, dict):
                raise AcquisitionError('模型依据格式不正确。', 'model_output_invalid', 502)
            text = _string(evidence.get('text'), 'evidence_quotes.text', 4000, True)
            index = evidence.get('image_index')
            if index is not None and (type(index) is not int or not 0 <= index < len(document.get('media', []))):
                raise AcquisitionError('模型引用了不存在的图片。', 'model_output_invalid', 502)
            normalized_quotes.append({'text': text, 'image_index': index})
        for kind in ('event', 'deadline'):
            draft[kind + '_date'] = _validate_date(item.get(kind + '_date'))
            resolution = _optional_value(item, kind + '_date_resolution', 'unknown')
            if not isinstance(resolution, str) or resolution not in ('explicit', 'contextual', 'unknown'):
                raise AcquisitionError('模型日期解析状态不受支持。', 'model_output_invalid', 502)
            draft[kind + '_date_resolution'] = resolution
            draft[kind + '_date_basis'] = _string(_optional_value(item, kind + '_date_basis', ''), kind + '_date_basis', 4000)
        colleges = _optional_value(item, 'colleges', [])
        if not isinstance(colleges, list) or len(colleges) > 30:
            raise AcquisitionError('学院关系格式错误。', 'model_output_invalid', 502)
        unique_colleges = []
        seen_colleges = set()
        for college in colleges:
            if not isinstance(college, dict) or college.get('relation') not in ('associated', 'audience'):
                raise AcquisitionError('学院关系只能是 associated/audience。', 'model_output_invalid', 502)
            name = _string(college.get('name'), 'college.name', 200, True).strip()
            evidence = _string(_optional_value(college, 'evidence', ''), 'college.evidence', 4000)
            key = (name, college['relation'], evidence)
            if key not in seen_colleges:
                seen_colleges.add(key)
                unique_colleges.append({'name': name, 'relation': college['relation'], 'evidence': evidence})
        draft['colleges'] = unique_colleges
        lecture = item.get('lecture_data')
        if lecture is not None:
            lecture = _validate_lecture(lecture, draft['title'], draft['event_date'])
        draft.update(tag=item['tag'], due_time=draft['deadline_date'] or draft['event_date'],
                     lecture_data=lecture, evidence_quotes=normalized_quotes)
        # Identity includes every normalized fact, even when no quotes were supplied.
        event_key = hashlib.sha256(json.dumps(draft, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        draft.update(event_key=event_key, source_url=document['canonical_url'])
        if event_key not in seen_events:
            seen_events.add(event_key)
            normalized.append(draft)
    return normalized


def _validate_lecture(lecture, title, event_date):
    if not isinstance(lecture, dict):
        raise AcquisitionError('讲座结构必须是 JSON 对象。', 'model_output_invalid', 502)
    normalized = {}
    for key, defaults in (
            ('series', {'name': '', 'number': ''}),
            ('title', {'primary': title, 'secondary': ''}),
            ('schedule', {'date': event_date or '', 'start_time': '', 'end_time': ''}),
            ('inviter', {'name': '', 'title': ''}),
            ('moderator', {'name': '', 'title': ''})):
        value = _optional_value(lecture, key, {})
        if not isinstance(value, dict):
            raise AcquisitionError(f'讲座 {key} 字段格式错误。', 'model_output_invalid', 502)
        normalized[key] = {field: _string(_optional_value(value, field, default), f'lecture.{key}.{field}', 2000)
                           for field, default in defaults.items()}
    for key in ('speakers', 'discussants'):
        people = _optional_value(lecture, key, [])
        if not isinstance(people, list) or len(people) > 30:
            raise AcquisitionError(f'讲座 {key} 格式错误。', 'model_output_invalid', 502)
        normalized[key] = []
        for person in people:
            if not isinstance(person, dict):
                raise AcquisitionError(f'讲座 {key} 格式错误。', 'model_output_invalid', 502)
            normalized[key].append({field: _string(_optional_value(person, field, ''), f'lecture.{key}.{field}', 2000)
                                    for field in ('name', 'title')})
    for key in ('venue', 'organizer', 'abstract', 'target_audience', 'registration', 'online_access'):
        normalized[key] = _string(_optional_value(lecture, key, ''), 'lecture.' + key, 12000)
    schedule = normalized['schedule']
    if schedule['date']:
        _validate_date(schedule['date'])
    for key in ('start_time', 'end_time'):
        if schedule[key] and not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', schedule[key]):
            raise AcquisitionError('讲座时间须为 HH:MM 或空值。', 'model_output_invalid', 502)
    return normalized


def _responses_text(response):
    """Read raw HTTP Responses output, not the SDK-only output_text shortcut."""
    if not isinstance(response, dict) or response.get('status') != 'completed' or response.get('error') is not None or response.get('incomplete_details') is not None:
        raise ValueError
    output = response.get('output')
    if not isinstance(output, list) or not output:
        raise ValueError
    texts = []
    for message in output:
        if not isinstance(message, dict) or message.get('status', 'completed') != 'completed':
            raise ValueError
        if message.get('type') == 'reasoning':
            continue
        if message.get('type') != 'message' or message.get('role') != 'assistant':
            raise ValueError
        content = message.get('content')
        if not isinstance(content, list) or not content:
            raise ValueError
        for part in content:
            if not isinstance(part, dict) or part.get('type') != 'output_text' or not isinstance(part.get('text'), str) or not part['text'].strip():
                # Includes refusal parts: never turn refused/partial output into drafts.
                raise ValueError
            texts.append(part['text'])
    if not texts:
        raise ValueError
    return ''.join(texts)


def _preview_size(width, height, patches):
    scale = min(1.0, 8192 / max(width, height), math.sqrt(patches * 1024 / (width * height)))
    while True:
        size = (max(1, int(width * scale)), max(1, int(height * scale)))
        if math.ceil(size[0] / 32) * math.ceil(size[1] / 32) <= patches:
            return size
        scale *= 0.95


def _svg_preview(raw, patches):
    """Render bounded static vectors and validated inline PNGs without external access."""
    def unsupported():
        raise AcquisitionError('SVG 包含不安全或不支持的内容；原始证据已保留。', 'image_unsupported')

    resources = {}
    embedded_pixels, decoded_bytes = 0, 0
    max_svg_bytes = 2 * 1024 * 1024

    def inline_png(uri):
        nonlocal decoded_bytes
        if uri in resources:
            return resources[uri]
        prefix = 'data:image/png;base64,'
        if not uri.startswith(prefix):
            unsupported()
        try:
            data = base64.b64decode(uri[len(prefix):], validate=True)
            if (not data.startswith(b'\x89PNG\r\n\x1a\n') or
                    base64.b64encode(data).decode('ascii') != uri[len(prefix):] or
                    decoded_bytes + len(data) > max_svg_bytes):
                unsupported()
            # Require a complete PNG stream, no animation chunks or trailing payload.
            offset = 8
            while offset < len(data):
                if offset + 12 > len(data):
                    unsupported()
                length = int.from_bytes(data[offset:offset + 4], 'big')
                kind = data[offset + 4:offset + 8]
                offset += length + 12
                if offset > len(data) or kind in {b'acTL', b'fcTL', b'fdAT'}:
                    unsupported()
                if kind == b'IEND':
                    if length or offset != len(data) or data[-4:] != b'\xaeB`\x82':
                        unsupported()
                    break
            else:
                unsupported()
            with Image.open(io.BytesIO(data), formats=('PNG',)) as image:
                pixels = image.width * image.height
                if (image.format != 'PNG' or min(image.size) <= 0 or
                        pixels > 16_777_216 or embedded_pixels + pixels > 32_000_000 or
                        image.get_format_mimetype() != 'image/png' or image.n_frames != 1):
                    unsupported()
                image.verify()
            # Verification checks chunk integrity; load also checks the entire pixel stream.
            with Image.open(io.BytesIO(data), formats=('PNG',)) as image:
                image.load()
                try:
                    image.seek(1)
                except EOFError:
                    pass
                else:
                    unsupported()
        except (ValueError, SyntaxError, OSError, EOFError, Image.DecompressionBombError):
            unsupported()
        decoded_bytes += len(data)
        resources[uri] = (data, pixels)
        return resources[uri]

    def fetch_inline_png(url, resource_type):
        if resource_type != 'image/*' or url not in resources:
            unsupported()
        return resources[url][0]

    def check_css(tokens):
        for token in tokens:
            if token.type in {'at-keyword', 'error'}:
                unsupported()
            if token.type == 'url' and not re.fullmatch(r'#(?:[A-Za-z_][\w:.-]*)?', token.value):
                unsupported()
            if token.type == 'function':
                if token.lower_name == 'url':
                    value = tinycss2.serialize(token.arguments).strip().strip('\"\'')
                    if not re.fullmatch(r'#(?:[A-Za-z_][\w:.-]*)?', value):
                        unsupported()
                else:
                    check_css(token.arguments)
            if hasattr(token, 'content'):
                check_css(token.content)

    if len(raw) > max_svg_bytes or re.search(br'<\?(?!xml(?:\s|\?>))', raw, re.I):
        unsupported()
    try:
        root = SafeXML.fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (SafeXML.ParseError, DefusedXmlException):
        unsupported()
    allowed = {'svg', 'g', 'defs', 'title', 'desc', 'rect', 'circle', 'ellipse', 'line', 'polyline',
               'polygon', 'path', 'text', 'tspan', 'textPath', 'linearGradient', 'radialGradient',
               'stop', 'clipPath', 'style', 'image'}
    if root.tag not in {'svg', '{http://www.w3.org/2000/svg}svg'}:
        unsupported()
    pending, nodes = [(root, 0)], 0
    while pending:
        element, depth = pending.pop()
        nodes += 1
        if nodes > 10000 or depth > 64:
            unsupported()
        tag = element.tag
        if tag.startswith('{http://www.w3.org/2000/svg}'):
            tag = tag.split('}', 1)[1]
        # Patterns/masks/nested SVGs can allocate independent, attacker-sized surfaces.
        if tag not in allowed or (tag == 'svg' and element is not root):
            unsupported()
        resource = None
        for name, value in element.attrib.items():
            local = name.split('}')[-1].lower()
            if local.startswith('on') or local == 'base':
                unsupported()
            if local == 'href':
                if tag == 'image':
                    if name not in {'href', '{http://www.w3.org/1999/xlink}href'} or resource is not None:
                        unsupported()
                    resource = value
                    continue
                if not re.fullmatch(r'#[A-Za-z_][\w:.-]*', value):
                    unsupported()
            check_css(tinycss2.parse_component_value_list(value))
        if tag == 'image':
            if resource is None:
                unsupported()
            _, pixels = inline_png(resource)
            embedded_pixels += pixels
            if embedded_pixels > 32_000_000:
                unsupported()
        if tag == 'style':
            check_css(tinycss2.parse_component_value_list(element.text or ''))
        pending.extend((child, depth + 1) for child in element)

    def dimension(value):
        match = re.fullmatch(r'\s*(\d+(?:\.\d+)?|\.\d+)\s*(px|pt|pc|in|cm|mm|q)?\s*', value or '')
        if not match:
            return None
        factors = {'px':1, 'pt':96/72, 'pc':16, 'in':96, 'cm':96/2.54, 'mm':96/25.4, 'q':96/101.6}
        return float(match[1]) * factors.get(match[2], 1)

    width, height = dimension(root.get('width')), dimension(root.get('height'))
    if width is None or height is None:
        try:
            viewbox = [float(value) for value in re.split(r'[\s,]+', root.get('viewBox', '').strip())]
            if len(viewbox) != 4 or not all(math.isfinite(value) for value in viewbox) or min(viewbox[2:]) <= 0:
                unsupported()
            ratio = viewbox[2] / viewbox[3]
            if ratio <= 0:
                unsupported()
            if width is not None:
                height = width / ratio
            elif height is not None:
                width = height * ratio
            else:
                width, height = viewbox[2:]
        except ValueError:
            unsupported()
    if not math.isfinite(width) or not math.isfinite(height) or min(width, height) <= 0 or width * height > MAX_IMAGE_PIXELS:
        unsupported()
    size = _preview_size(width, height, patches)
    try:
        raster = PNGSurface.convert(bytestring=raw, output_width=size[0], output_height=size[1],
                                    parent_width=width, parent_height=height, unsafe=False, url_fetcher=fetch_inline_png)
    except Exception:
        unsupported()
    working, _ = _raster_preview(raster, 0, patches)
    return working, math.ceil(width) * math.ceil(height) + embedded_pixels


def _raster_preview(raw, index, patches):
    """Decode every bounded frame, composing incrementally rather than retaining frames."""
    with warnings.catch_warnings():
        # The explicit frame guard and Pillow's hard error both cap frames at 400M.
        warnings.simplefilter('ignore', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw), formats=('JPEG', 'PNG', 'WEBP', 'GIF', 'BMP', 'TIFF')) as image:
            # n_frames may enumerate an entire TIFF IFD chain. Inspect at most
            # the allowed frames plus one sentinel, checking pixels before load.
            count, total_pixels, largest_edge = 0, 0, 0
            for frame in range(MAX_IMAGE_FRAMES + 1):
                try:
                    image.seek(frame)
                except EOFError:
                    break
                if frame == MAX_IMAGE_FRAMES:
                    raise AcquisitionError('图片超过 64 帧/页，无法完整生成模型预览；原始证据已保留。', 'image_unsupported')
                width, height = image.size
                pixels = width * height
                total_pixels += pixels
                if width <= 0 or height <= 0 or pixels > MAX_IMAGE_PIXELS or total_pixels > MAX_IMAGE_FRAME_PIXELS:
                    raise AcquisitionError('图片超过 4 亿像素/帧或 5.12 亿累计帧像素；原始证据已保留。', 'invalid_image')
                largest_edge = max(largest_edge, width, height)
                count += 1
            if not count:
                raise AcquisitionError('图片没有可安全解码的帧；原始证据已保留。', 'invalid_image')
            columns = math.ceil(math.sqrt(count))
            rows = math.ceil(count / columns)
            # A fixed cell avoids trusting the first TIFF page's dimensions for later pages.
            cell = min(max(32, largest_edge + 20), max(32, int(math.sqrt(patches / (columns * rows))) * 32))
            sheet = Image.new('RGB', (columns * cell, rows * cell), 'white') if count > 1 else None
            try:
                for frame in range(count):
                    image.seek(frame)
                    # Reduce single frames before orientation makes a copy. Multi-frame
                    # decoder canvases must stay intact for disposal/composition.
                    if count == 1:
                        size = _preview_size(*image.size, patches)
                        if image.format == 'JPEG':
                            image.draft(image.mode, size)
                        # draft reduces JPEG resolution, not stream validation: still
                        # load to EOF with Pillow's strict truncation handling.
                        image.load()
                        image.thumbnail(size, Image.Resampling.LANCZOS, reducing_gap=3.0)
                    else:
                        image.load()
                    with ImageOps.exif_transpose(image) as oriented:
                        size = _preview_size(*oriented.size, patches) if count == 1 else (cell, max(1, cell - 20))
                        oriented.thumbnail(size, Image.Resampling.LANCZOS, reducing_gap=3.0)
                        with oriented.convert('RGBA') as rgba:
                            working = Image.new('RGB', rgba.size, 'white')
                            working.paste(rgba, mask=rgba.getchannel('A'))
                        if sheet is None:
                            return working, total_pixels
                        try:
                            x, y = (frame % columns) * cell, (frame // columns) * cell
                            sheet.paste(working, (x + (cell - working.width) // 2, y + 20))
                            ImageDraw.Draw(sheet).text((x + 4, y + 3), f'Image {index} / frame {frame + 1} of {count}', fill='black')
                        finally:
                            working.close()
                result, sheet = sheet, None
                return result, total_pixels
            finally:
                if sheet is not None:
                    sheet.close()


def _model_images(media):
    """One JPEG preview per source index, including every supported frame/page."""
    if len(media) > MAX_MODEL_IMAGES:
        raise AcquisitionError('图片数量超过完整模型输入上限；原始证据已保留。', 'image_unsupported')
    prepared, total_bytes, total_pixels, source_bytes = [], 0, 0, 0
    patches = min(MAX_MODEL_IMAGE_PATCHES, MAX_MODEL_TOTAL_PATCHES // max(1, len(media)))
    for index, original in enumerate(media):
        try:
            if len(original['data']) > 4 * math.ceil(MAX_IMAGE / 3):
                raise ValueError
            raw = base64.b64decode(original['data'], validate=True)
            source_bytes += len(raw)
            if not raw or len(raw) > MAX_IMAGE or source_bytes > MAX_MEDIA_BYTES:
                raise ValueError
            if original.get('mime') not in IMAGE_MIMES:
                raise AcquisitionError('图片格式不受支持，未发送给模型；原始证据已保留。', 'image_unsupported')
            if original['mime'] == 'image/svg+xml':
                working, pixels = _svg_preview(raw, patches)
            else:
                working, pixels = _raster_preview(raw, original.get('source_image_index', index), patches)
            try:
                total_pixels += pixels
                if total_pixels > MAX_DOCUMENT_FRAME_PIXELS:
                    raise AcquisitionError('文档累计图片像素超过 10.24 亿；原始证据已保留。', 'image_unsupported')
                for attempt in range(8):
                    output = io.BytesIO()
                    working.save(output, format='JPEG', quality=max(60, 90 - attempt * 5))
                    data = output.getvalue()
                    if len(data) <= MAX_MODEL_IMAGE_BYTES:
                        break
                    working.thumbnail((max(1, int(working.width * 0.8)), max(1, int(working.height * 0.8))), Image.Resampling.LANCZOS)
                else:
                    raise AcquisitionError('模型预览超过 4 MiB；原始证据已保留。', 'image_unsupported')
            finally:
                working.close()
            total_bytes += len(data)
            if total_bytes > MAX_MODEL_MEDIA_BYTES:
                raise AcquisitionError('完整模型图片输入超过 32 MiB；原始证据已保留，未分批调用模型。', 'image_unsupported')
            prepared.append(dict(original, mime='image/jpeg', data=base64.b64encode(data).decode('ascii'),
                                 digest=hashlib.sha256(data).hexdigest()))
        except (ValueError, KeyError, IndexError, TypeError, SyntaxError, OSError, EOFError, RecursionError,
                UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise AcquisitionError('图片无法安全完整解码，未发送给模型；原始证据已保留。', 'invalid_image') from None
    return prepared


def _image_response(image, index):
    """Never serve active source formats on the application origin."""
    try:
        preview = _model_images([dict(image, source_image_index=index)])[0]
        data = base64.b64decode(preview['data'], validate=True)
        if image.get('mime') in {'image/png', 'image/jpeg', 'image/webp'}:
            # These are inert raster formats, already fully decoded above.
            data = base64.b64decode(image['data'], validate=True)
            preview = image
    except (AcquisitionError, ValueError, KeyError, TypeError):
        abort(422, description='原始图片已保留，但无法安全生成完整预览。')
    return Response(data, mimetype=preview['mime'], headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "default-src 'none'; sandbox"})


def request_model_json(instructions: str, source_data: dict, media=(), *, before_send=None,
                       response_schema=None, audit_directory=None, audit_context=None,
                       audit_ready=None) -> tuple[dict, str]:
    """Return one JSON object; archive the exact request before paid admission."""
    settings = _model_config()
    return _request_prepared_model_json(instructions, source_data, _model_images(media), settings,
        before_send=before_send, response_schema=response_schema, audit_directory=audit_directory,
        audit_context=audit_context, audit_ready=audit_ready)


def _request_prepared_model_json(instructions, source_data, media, settings, *, before_send=None,
                                 response_schema=None, audit_directory=None, audit_context=None,
                                 audit_ready=None):
    """Send already bounded raster inputs once; only trusted preflight callers use this."""
    source_text = 'Public source data JSON (untrusted):\n' + json.dumps(source_data, ensure_ascii=False)
    if settings.model_api_style == 'responses':
        content = [{'type': 'input_text', 'text': source_text}]
        content.extend({'type': 'input_image', 'image_url': 'data:' + image['mime'] + ';base64,' + image['data']} for image in media)
        payload = {'model': settings.model_name, 'instructions': instructions, 'input': [{'role': 'user', 'content': content}], 'text': {'format': {'type': 'json_object'}}, 'store': False, 'stream': False}
    else:
        content = [{'type': 'text', 'text': source_text}]
        content.extend({'type': 'image_url', 'image_url': {'url': 'data:' + image['mime'] + ';base64,' + image['data']}} for image in media)
        payload = {'model': settings.model_name, 'messages': [{'role': 'system', 'content': instructions}, {'role': 'user', 'content': content}], 'response_format': {'type': 'json_object'}}
    payload['tools'] = []
    payload['tool_choice'] = 'none'
    if response_schema is not None:
        schema_format = {'name': 'historical_evidence_review', 'strict': True, 'schema': response_schema}
        if settings.model_api_style == 'responses':
            payload['text']['format'] = dict(schema_format, type='json_schema')
        else:
            payload['response_format'] = {'type': 'json_schema', 'json_schema': schema_format}
    from nannanews.model_audit import ModelAudit

    context = dict(audit_context or {}, api_style=settings.model_api_style,
        instructions_sha256=hashlib.sha256(instructions.encode('utf-8')).hexdigest())
    if 'evidence_catalog' in source_data:
        context['catalog_sha256'] = hashlib.sha256(json.dumps(source_data['evidence_catalog'],
            ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    if response_schema is not None:
        context['schema_sha256'] = hashlib.sha256(json.dumps(response_schema, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    try:
        directory = audit_directory if audit_directory is not None else runtime.settings.data_dir / 'model-audit'
        audit = ModelAudit(directory, payload, request_limit=MAX_MODEL_REQUEST_BYTES,
            response_limit=MAX_MODEL_BODY, context=context)
    except OverflowError:
        raise AcquisitionError('完整模型请求超过 48 MB；未发送或分批调用模型。', 'model_input_too_large') from None
    except Exception:
        raise AcquisitionError('模型审计准备失败；未发送请求。', 'model_audit_failed', 503) from None
    response = None
    try:
        if audit_ready is not None:
            audit_ready(audit.identity)
        if before_send is not None:
            before_send()
        response = _model_post(payload, audit=audit)
        try:
            if settings.model_api_style == 'responses':
                raw = _responses_text(response)
            else:
                choice = response['choices'][0]
                if not isinstance(choice, dict) or not isinstance(choice.get('message'), dict):
                    raise ValueError
                if choice.get('finish_reason') != 'stop' or choice['message'].get('refusal'):
                    raise ValueError
                raw = choice['message']['content']
                if not isinstance(raw, str):
                    raise ValueError
            output = json.loads(raw)
            if not isinstance(output, dict):
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError):
            raise AcquisitionError('模型未返回完整、合法的草稿 JSON（可能拒绝或被截断）。', 'model_output_invalid', 502) from None
        try:
            audit.save('output_parsed')
        except Exception:
            raise AcquisitionError('模型响应已保留，但审计收尾失败；不会自动重试。', 'model_audit_failed', 503) from None
        return output, settings.model_name
    except Exception as error:
        if isinstance(error, AcquisitionError):
            error._model_audit = audit.identity
            error._model_name = settings.model_name
            if response is not None:
                error._model_response = response
        if 'reserved_bytes' in audit.metadata['response']:
            try:
                audit.preserve_response(complete=False)
            except Exception:
                pass
        try:
            audit.save('failed', failure_code=getattr(error, 'code', 'admission_blocked'))
        except Exception:
            if isinstance(error, AcquisitionError):
                error._model_audit_finalize_failed = True
        raise
    finally:
        audit.close()


def preflight_document_media(document):
    """Prepare complete image input before any paid-attempt ledger is claimed."""
    if not document.get('capture_complete', True):
        raise AcquisitionError('来源图片未完整采集；原始证据已保留，未调用模型。', 'source_incomplete')
    return _model_images(document.get('media', []))


def extract_document(document, uploaded_images=None, *, prepared_media=None, before_send=None):
    from nannanews.source_media import MEDIA_POLICY_INSTRUCTIONS

    settings = _model_config()
    if not isinstance(document.get('text'), str) or len(document['text']) > MAX_TEXT:
        raise AcquisitionError('公开正文格式错误或过长。', 'invalid_content')
    media = document.get('media') if 'media' in document and uploaded_images is None else _prepare_media(document, uploaded_images)
    if not document.get('capture_complete', True):
        raise AcquisitionError('来源图片未完整采集；原始证据已保留，未调用模型。', 'source_incomplete')
    if not document['text'].strip() and not media:
        raise AcquisitionError('没有可提取的公开正文或图片。', 'source_empty')
    source_data = {key: document.get(key) for key in ('canonical_url', 'title', 'text', 'raw_date_text', 'source_type')}
    source_data['source_published_date'] = _source_publication_date(document)
    source_data['source_media'] = [{'url': document.get('canonical_url'),
        'media_policy': document.get('media_policy'), 'skipped_media': document.get('skipped_media', [])}]
    instructions = EXTRACTION_INSTRUCTIONS + '\n\n' + MEDIA_POLICY_INSTRUCTIONS
    admission = {'before_send': before_send} if before_send is not None else {}
    try:
        if prepared_media is None:
            output, model_name = request_model_json(instructions, source_data, media, **admission)
        else:
            output, model_name = _request_prepared_model_json(instructions, source_data, prepared_media, settings, **admission)
    except AcquisitionError as error:
        if hasattr(error, '_model_response'):
            document['model_response'] = error._model_response
            document['model_name'] = error._model_name
        raise
    # Retain the unmodified answer before structural validation can reject it.
    document['model_output'] = output
    document['model_name'] = model_name
    return _validate_items(output, document)


def _migrate_schema(conn):
    """Install acquisition tables within the explicit migration transaction."""
    conn.execute('''CREATE TABLE IF NOT EXISTS acquisitions (
        id INTEGER PRIMARY KEY, source_url TEXT NOT NULL, digest TEXT NOT NULL,
        document_json TEXT NOT NULL, model_name TEXT NOT NULL, acquired_by TEXT NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(source_url, digest))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS acquisition_candidates (
        acquisition_id INTEGER NOT NULL REFERENCES acquisitions(id),
        event_key TEXT NOT NULL, entry_id INTEGER NOT NULL UNIQUE REFERENCES entries(id),
        draft_json TEXT NOT NULL, PRIMARY KEY(acquisition_id, event_key))''')




def _insert_drafts(conn, document, items, actor):
    """Insert within the caller's transaction; return IDs in candidate order."""
    now = datetime.now(timezone.utc).isoformat()
    row = conn.execute('SELECT id FROM acquisitions WHERE source_url=? AND digest=?', (document['canonical_url'], document['digest'])).fetchone()
    if row:
        rows = conn.execute('SELECT entry_id FROM acquisition_candidates WHERE acquisition_id=? ORDER BY entry_id', (row[0],)).fetchall()
        return [row[0] for row in rows], False, row[0]
    cursor = conn.execute('INSERT INTO acquisitions (source_url,digest,document_json,model_name,acquired_by,created_at) VALUES (?,?,?,?,?,?)', (document['canonical_url'], document['digest'], json.dumps(document, ensure_ascii=False), document['model_name'], actor, now))
    acquisition_id = cursor.lastrowid
    ids = []
    for item in items:
        cursor = conn.execute('''INSERT INTO entries (uploader,upload_time,title,link,description,describer,reviewer,due_time,status,type,use_image,publish_date,tag,short_title,lecture_data,priority)
            VALUES (?,?,?,?,?,?,NULL,?,'described','link',0,NULL,?,?,?,?)''', (actor, now, item['title'], document['canonical_url'] or None, item['description'], 'model:' + document['model_name'], item['due_time'], item['tag'], item['short_title'], json.dumps(item['lecture_data'], ensure_ascii=False) if item['lecture_data'] else '', 4 if item['tag'] == '讲座' else 0))
        entry_id = cursor.lastrowid
        conn.execute('INSERT INTO acquisition_candidates (acquisition_id,event_key,entry_id,draft_json) VALUES (?,?,?,?)', (acquisition_id, item['event_key'], entry_id, json.dumps(item, ensure_ascii=False)))
        ids.append(entry_id)
    return ids, True, acquisition_id


def insert_drafts(document, items, actor):
    """One transaction inserts candidates and canonical metadata or none."""
    from nannanews import information
    saved = information.save_document(document, None, actor)
    conn = runtime.connect_db()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            ids, created, acquisition_id = _insert_drafts(conn, document, items, actor)
            if created:
                for entry_id, item in zip(ids, items):
                    information._index_candidate(conn, saved['id'], entry_id, item)
            conn.execute("UPDATE source_documents SET model_status='completed',acquisition_id=? WHERE id=?", (acquisition_id,saved['id']))
            return sorted(ids), created
    finally:
        conn.close()


def get_entry_evidence(entry_id):
    conn = runtime.connect_db()
    try:
        row = conn.execute('''SELECT a.document_json,c.draft_json,a.model_name,a.created_at
            FROM acquisition_candidates c JOIN acquisitions a ON a.id=c.acquisition_id WHERE c.entry_id=?''', (entry_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    document, draft = json.loads(row[0]), json.loads(row[1])
    return {'entry_id': entry_id, 'source_url': document['canonical_url'], 'resolved_url': document.get('resolved_url', ''), 'title': document['title'], 'text': document['text'], 'raw_content': document.get('raw_content', ''), 'source_type': document['source_type'], 'raw_date_text': document.get('raw_date_text'), 'fetched_at': document['fetched_at'], 'digest': document['digest'], 'model_producer': 'model:' + row[2], 'created_at': row[3], 'draft': draft, 'images': [{'index': i, 'mime': image['mime'], 'digest': image['digest'], 'source_url': image.get('url', '')} for i, image in enumerate(document.get('media', []))]}


def load_catalog():
    path = runtime.path('source_catalog.json')
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding='utf-8'))


def _editor_required(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        username = session.get('username')
        if not username:
            abort(401)
        if runtime.get_user_role(username) not in {'admin', 'editor'}:
            abort(403)
        return function(*args, **kwargs)
    return wrapped


def _check_csrf():
    token = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token', '')
    expected = session.get('csrf_token', '')
    if not isinstance(token, str) or not isinstance(expected, str) or not expected or not hmac.compare_digest(token.encode('utf-8'), expected.encode('utf-8')):
        abort(400, description='CSRF token missing or invalid')


def _config_status():
    try:
        settings = _model_config()
        return {'configured': True, 'model_name': settings.model_name, 'message': '模型配置已提供；尚未代表后端连通或提取成功。'}
    except AcquisitionError as error:
        return {'configured': False, 'model_name': runtime.settings.model_name, 'code': error.code, 'message': str(error)}


@bp.route('/acquire', methods=['GET', 'POST'])
@_editor_required
def acquire():
    if request.method == 'GET':
        return render_template('acquire.html', config=_config_status(), result=None, evidence=None)
    # Flask's global 16 MiB limit remains in force for unrelated routes.
    request.max_content_length = MAX_MEDIA_BYTES + MAX_TEXT * 4 + 1024 * 1024
    _check_csrf()
    values = {'source_url': request.form.get('source_url', ''), 'public_text': request.form.get('public_text', ''), 'source_title': request.form.get('source_title', '')}
    try:
        from nannanews import information
        if request.form.get('public_confirm') != 'yes':
            raise AcquisitionError('请确认输入内容和图片已公开，且不含私聊或私人联系信息。', 'invalid_content')
        files = [file for file in request.files.getlist('public_images') if file.filename]
        if len(files) > MAX_IMAGES:
            raise AcquisitionError(f'最多上传 {MAX_IMAGES} 张关键公开图片。', 'invalid_image')
        uploads, upload_bytes = [], 0
        for file in files:
            data = file.read(min(MAX_IMAGE, MAX_MEDIA_BYTES - upload_bytes) + 1)
            if len(data) > MAX_IMAGE or upload_bytes + len(data) > MAX_MEDIA_BYTES:
                raise AcquisitionError('上传图片超过单张 32 MiB 或总计 128 MiB 上限。', 'invalid_image')
            uploads.append(data)
            upload_bytes += len(data)
        text, title = values['public_text'].strip(), values['source_title'].strip()
        if len(text) > MAX_TEXT or len(title) > 500:
            raise AcquisitionError('提供的正文或标题超过上限。', 'invalid_content')
        if text or uploads:
            source = canonical_url(values['source_url']) if values['source_url'].strip() else ''
            if source:
                parts = urlsplit(source)
                _public_addresses(parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80), 5)
            document = {'canonical_url': source, 'title': title, 'text': text, 'images': [], 'raw_content': text, 'raw_date_text': None, 'source_type': 'user_supplied_public', 'fetched_at': datetime.now(timezone.utc).isoformat()}
        else:
            document = fetch_document(values['source_url'])
        # Capture complete source evidence before any paid extraction attempt.
        _prepare_media(document, uploads)
        saved = information.save_document(document, None, session['username'])
        enriched = information.enrich_document(saved['id'], session['username'])
        ids, created = enriched['entry_ids'], enriched['created']
        result = {'created': created, 'entries': [{'id': entry_id, 'edit_url': url_for('describe', entry_id=entry_id), 'review_url': url_for('review', entry_id=entry_id), 'evidence_url': url_for('acquisition.evidence', entry_id=entry_id)} for entry_id in ids], 'message': '已创建待人工审核的模型草稿，未发布。' if created else '同一来源与内容已导入，返回已有草稿；未重复创建。'}
        if request.accept_mimetypes.best == 'application/json':
            return jsonify(result), 201 if created else 200
        return render_template('acquire.html', config=_config_status(), result=result, evidence=None, values=values)
    except AcquisitionError as error:
        if request.accept_mimetypes.best == 'application/json':
            return jsonify({'code': error.code, 'message': str(error)}), error.status
        return render_template('acquire.html', config=_config_status(), result=None, evidence=None, error={'code': error.code, 'message': str(error)}, values=values), error.status




@bp.get('/acquire/model-status')
@_editor_required
def model_status():
    return jsonify(_config_status())


@bp.get('/acquire/evidence/<int:entry_id>')
@_editor_required
def evidence(entry_id):
    data = get_entry_evidence(entry_id)
    if data is None:
        abort(404)
    if request.accept_mimetypes.best == 'application/json':
        return jsonify(data)
    return render_template('acquire.html', config=_config_status(), result=None, evidence=data)


@bp.get('/acquire/evidence/<int:entry_id>/images/<int:index>')
@_editor_required
def evidence_image(entry_id, index):
    conn = runtime.connect_db()
    try:
        row = conn.execute('SELECT a.document_json FROM acquisitions a JOIN acquisition_candidates c ON c.acquisition_id=a.id WHERE c.entry_id=?', (entry_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        abort(404)
    media = json.loads(row[0]).get('media', [])
    if not 0 <= index < len(media):
        abort(404)
    image = media[index]
    return _image_response(image, index)


@bp.before_request
def _request_bounds():
    if request.content_length is not None and request.content_length > MAX_MEDIA_BYTES + MAX_TEXT * 4 + 1024 * 1024:
        abort(413)


@bp.after_request
def _private_response(response):
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def register(app):
    app.register_blueprint(bp)
