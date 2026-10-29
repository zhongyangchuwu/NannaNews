"""Source-only recovery from benchmark/Fake-IP DNS answers.

Cloudflare documents /dns-query JSON and bootstrap 1.1.1.1 at:
https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/
https://developers.cloudflare.com/1.1.1.1/ip-addresses/
Preserve inherited proxy/direct policy; no alternate provider, redirects,
credentials, private origin addresses, or model DNS changes.
"""
from collections import OrderedDict
from functools import partial
import http.client
import ipaddress
import json
import re
import socket
import threading
import time
from urllib.parse import quote, urlsplit

RESOLVER_HOST = 'cloudflare-dns.com'
RESOLVER_ADDRESS = '1.1.1.1'
RESOLVER_URL = 'https://cloudflare-dns.com/dns-query'
MAX_BODY = 64 * 1024
MAX_RECORDS = 128
MAX_ADDRESSES = 64
MAX_CNAME_DEPTH = 8
MAX_CACHE_ENTRIES = 128
MAX_CACHE_TTL = 300
RESOLUTION_TIMEOUT = 10
# RFC2544 and RFC5180, with RFC5180 erratum1752's corrected IPv6 prefix.
_FAKE_POOLS = (ipaddress.ip_network('198.18.0.0/15'), ipaddress.ip_network('2001:2::/48'))
_RESOLVER_LOCK = threading.Lock()
_CACHE = OrderedDict()


class SourceDnsError(ValueError):
    """An unavailable/invalid trusted resolver is never a reason to weaken SSRF."""


def _hostname(value):
    if not isinstance(value, str) or not value or not value.isascii():
        raise SourceDnsError('Source DNS requires a public, non-literal DNS hostname.')
    host = value[:-1].lower() if value.endswith('.') else value.lower()
    labels = host.split('.')
    if (len(host) > 253 or len(labels) < 2 or not re.search('[a-z]', labels[-1])
            or any(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) is None for label in labels)
            or labels[-1] in {'localhost', 'local', 'localdomain', 'internal', 'lan', 'home', 'corp', 'test', 'invalid', 'example', 'onion'}
            or host == 'home.arpa' or host.endswith(('.home.arpa', '.in-addr.arpa', '.ip6.arpa'))):
        raise SourceDnsError('Source DNS requires a public, non-literal DNS hostname.')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise SourceDnsError('Literal source addresses cannot use Fake-IP compatibility.')
    try:
        socket.inet_aton(host)
    except OSError:
        return host
    raise SourceDnsError('Legacy literal source addresses cannot use Fake-IP compatibility.')


def _fake_address(value):
    if not isinstance(value, str) or '%' in value:
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address.version == pool.version and address in pool for pool in _FAKE_POOLS)


def is_fake_ip_answer(host, addresses):
    """Only an entirely recognized native benchmark answer set is eligible."""
    try:
        _hostname(host)
    except SourceDnsError:
        return False
    return bool(addresses) and all(_fake_address(address) for address in addresses)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SourceDnsError('Trusted source DNS returned duplicate JSON fields.')
        result[key] = value
    return result


def _invalid_constant(value):
    raise SourceDnsError('Trusted source DNS returned a non-JSON numeric value.')


def _answer(body, host, kind, validate_public):
    try:
        value = json.loads(body.decode('utf-8'), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise SourceDnsError('Trusted source DNS returned invalid bounded JSON.') from None
    if (not isinstance(value, dict) or type(value.get('Status')) is not int or value['Status'] != 0
            or value.get('TC') is not False or value.get('CD') is not False
            or value.get('RD') is not True or value.get('RA') is not True):
        raise SourceDnsError('Trusted source DNS did not return an untruncated validated recursive answer.')
    question = value.get('Question')
    if (not isinstance(question, list) or len(question) != 1 or not isinstance(question[0], dict)
            or _hostname(question[0].get('name')) != host
            or type(question[0].get('type')) is not int or question[0]['type'] != kind):
        raise SourceDnsError('Trusted source DNS question differs from the requested hostname and type.')
    aliases, address_records, all_addresses, ttls, count = {}, [], [], [], 0
    for section in ('Answer', 'Authority', 'Additional'):
        records = value.get(section, [])
        if not isinstance(records, list):
            raise SourceDnsError('Trusted source DNS returned an invalid record section.')
        count += len(records)
        if count > MAX_RECORDS:
            raise SourceDnsError('Trusted source DNS exceeded its record-count limit.')
        for record in records:
            if (not isinstance(record, dict) or type(record.get('type')) is not int
                    or type(record.get('TTL')) is not int or not 0 <= record['TTL'] <= 2**31 - 1):
                raise SourceDnsError('Trusted source DNS returned an invalid record type or TTL.')
            ttls.append(record['TTL'])
            record_type = record['type']
            if record_type in {1, 28}:
                owner, data = _hostname(record.get('name')), record.get('data')
                try:
                    if not isinstance(data, str) or '%' in data:
                        raise ValueError
                    address = ipaddress.ip_address(data)
                    if address.version != (4 if record_type == 1 else 6):
                        raise ValueError
                except ValueError:
                    raise SourceDnsError('Trusted source DNS returned a malformed address record.') from None
                all_addresses.append(str(address))
                if section == 'Answer':
                    if record_type != kind:
                        raise SourceDnsError('Trusted source DNS returned an unexpected address family in its answer.')
                    address_records.append((owner, str(address)))
            elif section == 'Answer' and record_type == 5:
                owner, target = _hostname(record.get('name')), _hostname(record.get('data'))
                if owner in aliases and aliases[owner] != target:
                    raise SourceDnsError('Trusted source DNS returned ambiguous CNAME targets.')
                aliases[owner] = target
            elif section == 'Answer':
                raise SourceDnsError('Trusted source DNS returned an unsupported answer record.')
    # Validate every A/AAAA value, including glue, before selecting a destination.
    if all_addresses:
        validate_public(all_addresses)
    reachable, current = {host}, host
    for _ in range(MAX_CNAME_DEPTH):
        if current not in aliases:
            break
        current = aliases[current]
        if current in reachable:
            raise SourceDnsError('Trusted source DNS returned a cyclic CNAME chain.')
        reachable.add(current)
    if current in aliases or any(owner not in reachable for owner in aliases):
        raise SourceDnsError('Trusted source DNS returned an unbound or excessive CNAME chain.')
    if any(owner not in reachable for owner, _ in address_records):
        raise SourceDnsError('Trusted source DNS returned addresses unrelated to the requested hostname.')
    return [address for _, address in address_records], min(ttls, default=0)


def _query(acquisition, host, kind, proxy, proxy_address, deadline):
    if proxy is None:
        connection = acquisition._PinnedHTTPS(RESOLVER_HOST, 443, RESOLVER_ADDRESS,
            acquisition._source_timeout(deadline))
        connection._source_deadline = deadline
    else:
        connection = acquisition._PublicTunnelConnection(RESOLVER_HOST, 443, RESOLVER_ADDRESS,
            proxy_address, proxy[1], deadline, True)
    connection.response_class = partial(acquisition._DeadlineHTTPResponse, deadline=deadline)
    response = None
    try:
        connection.request('GET', '/dns-query?name=' + quote(host, safe='') + '&type=' + ('A' if kind == 1 else 'AAAA'),
            headers={'Host': RESOLVER_HOST, 'User-Agent': 'NannaPublicNews/1.0',
                     'Accept': 'application/dns-json', 'Accept-Encoding': 'identity'})
        response_socket = connection.sock
        if response_socket is not None:
            response_socket.settimeout(acquisition._source_timeout(deadline))
        response = connection.getresponse()
        if response.status != 200:
            raise SourceDnsError('Trusted source DNS returned HTTP ' + str(response.status) + '; no redirect or alternate resolver was used.')
        if (response.getheader('Content-Type', '').split(';', 1)[0].strip().lower() not in {'application/dns-json', 'application/json'}
                or response.getheader('Content-Encoding', 'identity').lower() not in {'identity', ''}):
            raise SourceDnsError('Trusted source DNS returned an unsupported representation.')
        declared, age = response.getheader('Content-Length'), response.getheader('Age', '0')
        if ((declared is not None and (re.fullmatch('[0-9]{1,20}', declared) is None or int(declared) > MAX_BODY))
                or re.fullmatch('[0-9]{1,10}', age) is None or int(age) > 2**31 - 1):
            raise SourceDnsError('Trusted source DNS returned invalid size or cache-age metadata.')
        chunks, total = [], 0
        while True:
            remaining = acquisition._source_timeout(deadline)
            if response_socket is not None and response_socket.fileno() >= 0:
                response_socket.settimeout(remaining)
            chunk = response.read1(min(16384, MAX_BODY + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_BODY:
                raise SourceDnsError('Trusted source DNS response exceeded its byte limit.')
        if declared is not None and total != int(declared):
            raise SourceDnsError('Trusted source DNS response was incomplete.')
        addresses, ttl = _answer(b''.join(chunks), host, kind, acquisition._require_public_addresses)
        return addresses, time.monotonic() + max(0, min(MAX_CACHE_TTL, ttl - int(age)))
    finally:
        try:
            if response is not None:
                response.close()
        finally:
            connection.close()


def resolve_public_addresses(host, proxy, deadline):
    """Resolve through fixed verified-TLS DoH with unchanged proxy/direct policy.

    The caller establishes the all-Fake-IP OS-answer prerequisite. Resolver
    policy is checked even for cache hits; failures never change the route.
    """
    from nannanews import acquisition

    host = _hostname(host)
    if acquisition._source_proxy(urlsplit(RESOLVER_URL)) != proxy:
        raise SourceDnsError('Inherited proxy/direct policy does not permit the same trusted-DNS route; no alternate route was used.')
    acquisition._require_public_addresses([RESOLVER_ADDRESS])
    deadline = min(deadline, time.monotonic() + RESOLUTION_TIMEOUT)
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _RESOLVER_LOCK.acquire(timeout=remaining):
        raise SourceDnsError('Trusted source DNS exceeded its shared deadline or concurrency bound.')
    try:
        if deadline <= time.monotonic():
            raise SourceDnsError('Trusted source DNS exceeded its shared deadline.')
        key = (host, proxy)
        cached = _CACHE.get(key)
        if cached is not None:
            if time.monotonic() < cached[0]:
                _CACHE.move_to_end(key)
                return list(acquisition._require_public_addresses(cached[1]))
            del _CACHE[key]
        proxy_address = None
        if proxy is not None:
            proxy_addresses = acquisition._resolve_addresses(proxy[0], proxy[1], acquisition._source_timeout(deadline))
            if not proxy_addresses or any(_fake_address(address) for address in proxy_addresses):
                raise SourceDnsError('The configured proxy itself has benchmark/Fake-IP DNS answers; no benchmark address was connected.')
            proxy_address = proxy_addresses[0]
        addresses, expirations = [], []
        for kind in (1, 28):
            resolved, expires = _query(acquisition, host, kind, proxy, proxy_address, deadline)
            addresses.extend(resolved)
            expirations.append(expires)
        addresses = list(dict.fromkeys(addresses))
        if not addresses or len(addresses) > MAX_ADDRESSES:
            raise SourceDnsError('Trusted source DNS returned no usable addresses or exceeded its address-count bound.')
        acquisition._require_public_addresses(addresses)
        expires = min(expirations)
        if time.monotonic() < expires:
            _CACHE[key] = (expires, tuple(addresses))
            _CACHE.move_to_end(key)
            while len(_CACHE) > MAX_CACHE_ENTRIES:
                _CACHE.popitem(last=False)
        return addresses
    except acquisition.AcquisitionError as error:
        if error.code == 'unsafe_source':
            raise
        raise SourceDnsError('Trusted source DNS could not complete through the inherited route within its deadline.') from None
    except (OSError, http.client.HTTPException):
        raise SourceDnsError('Trusted source DNS transport or TLS verification failed; no alternate route was used.') from None
    finally:
        _RESOLVER_LOCK.release()
