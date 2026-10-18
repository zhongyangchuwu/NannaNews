"""Source-only Fake-IP recovery; DNS and socket-facing HTTP are substituted."""
from collections import OrderedDict
import io
import json
import ssl
import threading
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

import pytest

from nannanews import acquisition
from nannanews import source_dns

HOST = 'mp.weixin.qq.com'
PROXY = ('operator-proxy.example.org', 7890)
PUBLIC4 = '93.184.216.34'
PUBLIC6 = '2606:4700:4700::1111'


def answer(host, kind, data=None, *, ttl=60):
    if data is None:
        data = PUBLIC4 if kind == 1 else PUBLIC6
    return {'Status': 0, 'TC': False, 'RD': True, 'RA': True, 'AD': False, 'CD': False,
            'Question': [{'name': host + '.', 'type': kind}],
            'Answer': [{'name': host + '.', 'type': kind, 'TTL': ttl, 'data': data}]}


def http_response(body, *, status=200, headers=None):
    response = Mock()
    response.status = status
    values = {'Content-Type': 'application/dns-json', 'Content-Length': str(len(body))}
    values.update(headers or {})
    response.getheader.side_effect = lambda key, default=None: values.get(key, default)
    response.read1.side_effect = io.BytesIO(body).read
    return response


@pytest.fixture
def transport(monkeypatch):
    state = {'os_answers': ['198.18.0.22', '2001:2::12'], 'proxy': PROXY,
             'origin_proxy': True, 'resolver_proxy': True, 'different_resolver_proxy': False,
             'dns': [], 'connections': [], 'queries': [], 'payloads': {}, 'headers': {},
             'resolver_status': 200, 'resolver_failure': None, 'raw_json': None,
             'clock': 100.0, 'ttl': 60, 'origin_status': 200, 'origin_location': None}
    monkeypatch.setattr(source_dns, '_CACHE', OrderedDict())
    monkeypatch.setattr(source_dns, '_RESOLVER_LOCK', threading.Lock())
    monkeypatch.setattr(source_dns.time, 'monotonic', lambda: state['clock'])

    def proxy(parts):
        if parts.hostname == source_dns.RESOLVER_HOST:
            if not state['resolver_proxy']:
                return None
            return ('different-proxy.example.org', 7891) if state['different_resolver_proxy'] else state['proxy']
        return state['proxy'] if state['origin_proxy'] else None

    def dns(host, port, timeout):
        state['dns'].append((host, port, timeout))
        if host == state['proxy'][0]:
            return state.get('proxy_answers', ['127.0.0.1'])
        if isinstance(state['os_answers'], Exception):
            raise state['os_answers']
        return list(state['os_answers'])

    def connection(host, port, address, proxy_address, proxy_port, deadline, secure):
        conn = Mock()
        conn.sock = None
        record = {'host': host, 'port': port, 'address': address, 'proxy_address': proxy_address,
                  'proxy_port': proxy_port, 'deadline': deadline, 'secure': secure, 'connection': conn}
        state['connections'].append(record)

        def request(method, path, *, headers):
            record.update(method=method, path=path, headers=headers)
            if host == source_dns.RESOLVER_HOST:
                state['queries'].append(record)
                state['clock'] += state.get('advance_on_query', 0)
                if state['resolver_failure'] is not None:
                    raise state['resolver_failure']
                query = parse_qs(urlsplit(path).query)
                name, kind = query['name'][0], 1 if query['type'] == ['A'] else 28
                payload = state['payloads'].get((name, kind), answer(name, kind, ttl=state['ttl']))
                body = state['raw_json'] if state['raw_json'] is not None else json.dumps(payload).encode()
                record['response'] = http_response(body, status=state['resolver_status'], headers=state['headers'])
            else:
                headers = {'Content-Type': 'text/html'}
                if state['origin_location'] is not None:
                    headers['Location'] = state['origin_location']
                record['response'] = http_response(b'original public source', status=state['origin_status'], headers=headers)
            conn.getresponse.return_value = record['response']

        conn.request.side_effect = request
        return conn

    monkeypatch.setattr(acquisition, '_source_proxy', proxy)
    monkeypatch.setattr(acquisition, '_resolve_addresses', dns)
    monkeypatch.setattr(acquisition, '_PublicTunnelConnection', connection)
    def direct_forbidden(*args, **kwargs):
        pytest.fail('Fake-IP compatibility must never open a direct origin or resolver connection.')
    monkeypatch.setattr(acquisition, '_PinnedHTTP', direct_forbidden)
    monkeypatch.setattr(acquisition, '_PinnedHTTPS', direct_forbidden)
    return state


def fetch(host=HOST):
    return acquisition.fetch_public_resource('https://' + host + '/s/source?x=1&x=2')


def origins(state):
    return [record for record in state['connections'] if record['host'] != source_dns.RESOLVER_HOST]


def test_all_fake_os_answers_use_fixed_verified_tls_bootstrap_via_the_same_proxy_then_pin_actual_origin(transport):
    assert fetch()[0] == b'original public source'
    assert len(transport['queries']) == 2
    assert len(origins(transport)) == 1
    for record in transport['queries']:
        assert record['host'] == 'cloudflare-dns.com' and record['address'] == '1.1.1.1'
        assert record['port'] == 443 and record['secure'] is True
        assert record['proxy_address'] == '127.0.0.1' and record['proxy_port'] == PROXY[1]
        assert record['headers']['Host'] == 'cloudflare-dns.com'
        assert record['headers']['Accept'] == 'application/dns-json'
        assert record['headers']['Accept-Encoding'] == 'identity'
        assert not {'Cookie', 'Authorization', 'Proxy-Authorization'} & record['headers'].keys()
        assert record['deadline'] <= 110.0
        record['connection'].close.assert_called_once()
    origin = origins(transport)[0]
    assert origin['host'] == HOST and origin['address'] == PUBLIC4 and origin['secure'] is True
    assert origin['headers']['Host'] == HOST
    assert origin['path'] == '/s/source?x=1&x=2'
    assert all(host != source_dns.RESOLVER_HOST for host, _, _ in transport['dns'])


def test_public_addresses_itself_still_rejects_fake_answers_without_resolver_or_connections(transport):
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition._public_addresses(HOST, 443, 5)
    assert caught.value.code == 'unsafe_source'
    assert caught.value._os_dns_addresses == tuple(transport['os_answers'])
    assert transport['connections'] == []


@pytest.mark.parametrize('addresses', [
    ['127.0.0.1'], ['10.0.0.1'], ['169.254.169.254'], ['::1'], ['fe80::1'], ['fd00::1'],
    ['198.18.0.22', PUBLIC4], ['198.18.0.22', '10.0.0.1'],
    ['2001:2::12', PUBLIC6], ['198.18.0.22', '::ffff:198.18.0.22'],
])
def test_private_linklocal_and_mixed_answers_never_trigger_compatibility(transport, addresses):
    transport['os_answers'] = addresses
    with pytest.raises(acquisition.AcquisitionError) as caught:
        fetch()
    assert caught.value.code == 'unsafe_source'
    assert transport['connections'] == []


@pytest.mark.parametrize('host', ['198.18.0.22', '[2001:2::12]', '127.1', '2130706433',
                                  'localhost', 'service.local', 'device.home.arpa', 'source.internal'])
def test_literal_or_nonpublic_hostnames_cannot_use_fake_ip_compatibility(transport, host):
    with pytest.raises(acquisition.AcquisitionError) as caught:
        fetch(host)
    assert caught.value.code == 'unsafe_source'
    assert transport['connections'] == []


@pytest.mark.parametrize('setting', ['origin_proxy', 'resolver_proxy', 'different_resolver_proxy'])
def test_absent_or_changed_inherited_route_never_uses_a_direct_or_other_proxy(transport, setting):
    transport[setting] = setting == 'different_resolver_proxy'
    with pytest.raises(acquisition.AcquisitionError) as caught:
        fetch()
    assert caught.value.code == ('unsafe_source' if setting == 'origin_proxy' else 'source_dns_unavailable')
    assert transport['connections'] == []


def test_ordinary_dns_failure_does_not_invoke_a_trusted_resolver(transport):
    transport['os_answers'] = acquisition.AcquisitionError('OS DNS failed', 'source_unavailable')
    with pytest.raises(acquisition.AcquisitionError) as caught:
        fetch()
    assert caught.value.code == 'source_unavailable'
    assert transport['connections'] == []
    assert len(transport['dns']) == 4


@pytest.mark.parametrize('data', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '198.18.0.22'])
def test_actual_private_a_record_is_rejected_by_the_original_public_guard(transport, data):
    transport['payloads'][(HOST, 1)] = answer(HOST, 1, data)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        fetch()
    assert caught.value.code == 'unsafe_source'
    assert origins(transport) == []


@pytest.mark.parametrize('data', ['::1', 'fd00::1', '2001:2::12', '::ffff:127.0.0.1', '2002:7f00:1::1'])
def test_actual_private_aaaa_record_is_rejected_before_any_origin_connect(transport, data):
    transport['payloads'][(HOST, 28)] = answer(HOST, 28, data)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        fetch()
    assert caught.value.code == 'unsafe_source'
    assert origins(transport) == []


def test_private_glue_is_not_ignored_even_though_it_would_not_be_used_as_origin(transport):
    payload = answer(HOST, 1)
    payload['Additional'] = [{'name': 'ns.nju.edu.cn.', 'type': 1, 'TTL': 30, 'data': '10.0.0.1'}]
    transport['payloads'][(HOST, 1)] = payload
    with pytest.raises(acquisition.AcquisitionError) as caught:
        fetch()
    assert caught.value.code == 'unsafe_source' and origins(transport) == []


@pytest.mark.parametrize('change', ['wrong_question', 'wrong_type', 'unrelated_owner', 'truncated',
                                   'checking_disabled', 'nxdomain', 'bad_ttl', 'boolean_ttl', 'unknown_record'])
def test_invalid_question_shape_flags_records_or_ttl_fail_closed(transport, change):
    payload = answer(HOST, 1)
    if change == 'wrong_question':
        payload['Question'][0]['name'] = 'different.nju.edu.cn.'
    elif change == 'wrong_type':
        payload['Question'][0]['type'] = 28
    elif change == 'unrelated_owner':
        payload['Answer'][0]['name'] = 'different.nju.edu.cn.'
    elif change == 'truncated':
        payload['TC'] = True
    elif change == 'checking_disabled':
        payload['CD'] = True
    elif change == 'nxdomain':
        payload['Status'] = 3
    elif change == 'bad_ttl':
        payload['Answer'][0]['TTL'] = -1
    elif change == 'boolean_ttl':
        payload['Answer'][0]['TTL'] = True
    elif change == 'unknown_record':
        payload['Answer'][0]['type'] = 16
    transport['payloads'][(HOST, 1)] = payload
    with pytest.raises(acquisition.AcquisitionError) as caught:
        fetch()
    assert caught.value.code == 'source_dns_unavailable'
    assert origins(transport) == []


@pytest.mark.parametrize('raw', [b'not JSON', b'{"Status":0,"Status":0}', b'{"Status":NaN}', b'[' * 2000])
def test_malformed_duplicate_or_excessively_nested_json_is_not_a_dns_answer(transport, raw):
    transport['raw_json'] = raw
    with pytest.raises(acquisition.AcquisitionError) as caught:
        fetch()
    assert caught.value.code == 'source_dns_unavailable' and origins(transport) == []


@pytest.mark.parametrize('status,headers,raw', [
    (302, {'Location': 'https://other-resolver.example.org/dns-query'}, None),
    (200, {'Content-Type': 'text/html'}, None),
    (200, {'Content-Encoding': 'gzip'}, None),
    (200, {'Content-Length': '999999'}, None),
    (200, {'Content-Length': '1'}, None),
    (200, {'Content-Length': ''}, None),
    (200, {'Content-Length': '9' * 5000}, None),
    (200, {'Age': '-1'}, None),
    (200, {}, b'x' * (source_dns.MAX_BODY + 1)),
])
def test_resolver_http_redirect_encoding_size_and_incomplete_failures_are_not_retried(transport, status, headers, raw):
    transport.update(resolver_status=status, headers=headers, raw_json=raw)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        fetch()
    assert caught.value.code == 'source_dns_unavailable'
    assert len(transport['queries']) == 1 and origins(transport) == []
    transport['queries'][0]['connection'].close.assert_called_once()


def test_resolver_tls_failure_is_closed_without_origin_or_provider_rotation(transport):
    transport['resolver_failure'] = ssl.SSLCertVerificationError('untrusted certificate')
    with pytest.raises(acquisition.AcquisitionError) as caught:
        fetch()
    assert caught.value.code == 'source_dns_unavailable'
    assert len(transport['queries']) == 1 and origins(transport) == []
    transport['queries'][0]['connection'].close.assert_called_once()


def test_bounded_cname_chain_binds_addresses_to_the_original_question(transport):
    for kind in (1, 28):
        payload = answer(HOST, kind)
        payload['Answer'][0]['name'] = 'cdn.nju.edu.cn.'
        payload['Answer'].insert(0, {'name': HOST + '.', 'type': 5, 'TTL': 30, 'data': 'cdn.nju.edu.cn.'})
        transport['payloads'][(HOST, kind)] = payload
    assert fetch()[0] == b'original public source'
    assert origins(transport)[0]['host'] == HOST and origins(transport)[0]['address'] == PUBLIC4


def test_cache_honors_ttl_and_never_overrides_a_later_non_fake_unsafe_os_answer(transport):
    transport['ttl'] = 2
    transport['headers']['Age'] = '1'
    fetch()
    transport['clock'] += 0.5
    fetch()
    assert len(transport['queries']) == 2
    transport['clock'] += 0.6
    fetch()
    assert len(transport['queries']) == 4
    transport['os_answers'] = ['127.0.0.1']
    with pytest.raises(acquisition.AcquisitionError) as caught:
        fetch()
    assert caught.value.code == 'unsafe_source' and len(transport['queries']) == 4


@pytest.mark.parametrize('ttl,age', [(0, 0), (1, 2)])
def test_zero_or_already_aged_ttl_answers_are_not_cached(transport, ttl, age):
    transport['ttl'], transport['headers']['Age'] = ttl, str(age)
    fetch()
    fetch()
    assert len(transport['queries']) == 4


def test_large_ttl_is_capped_and_cache_has_a_bounded_eviction_policy(transport, monkeypatch):
    monkeypatch.setattr(source_dns, 'MAX_CACHE_ENTRIES', 2)
    transport['ttl'] = 100000
    fetch('one.nju.edu.cn')
    fetch('two.nju.edu.cn')
    fetch('three.nju.edu.cn')
    fetch('one.nju.edu.cn')
    assert len(transport['queries']) == 8
    transport['clock'] += source_dns.MAX_CACHE_TTL + 1
    fetch('one.nju.edu.cn')
    assert len(transport['queries']) == 10


def test_redirect_revalidates_and_rejects_private_target_even_after_successful_fake_ip_recovery(transport):
    transport['origin_status'], transport['origin_location'] = 302, 'https://127.0.0.1/private'
    with pytest.raises(acquisition.AcquisitionError) as caught:
        fetch()
    assert caught.value.code == 'unsafe_source'
    assert len(origins(transport)) == 1


def test_normal_global_os_answers_do_not_contact_the_compatibility_resolver(transport):
    transport['os_answers'] = [PUBLIC4, PUBLIC6]
    assert fetch()[0] == b'original public source'
    assert transport['queries'] == []
    assert origins(transport)[0]['address'] == PUBLIC4


def test_fake_ip_answers_for_the_operator_proxy_itself_are_never_connected(transport):
    transport['proxy_answers'] = ['198.18.0.99']
    with pytest.raises(acquisition.AcquisitionError) as caught:
        fetch()
    assert caught.value.code == 'source_dns_unavailable'
    assert transport['connections'] == []


def test_resolver_body_deadline_failure_does_not_start_origin_or_retry_provider(transport):
    transport['advance_on_query'] = source_dns.RESOLUTION_TIMEOUT + 1
    with pytest.raises(acquisition.AcquisitionError) as caught:
        fetch()
    assert caught.value.code == 'source_dns_unavailable'
    assert len(transport['queries']) == 1 and origins(transport) == []
    transport['queries'][0]['connection'].close.assert_called_once()


@pytest.mark.parametrize('length,cycle', [(9, False), (2, True)])
def test_cyclic_or_excessive_cname_chains_are_not_followed(transport, length, cycle):
    payload = answer(HOST, 1)
    owners = [HOST] + [f'alias-{index}.nju.edu.cn' for index in range(length)]
    payload['Answer'] = [{'name': owners[index] + '.', 'type': 5, 'TTL': 60,
                          'data': owners[index + 1] + '.'} for index in range(length)]
    if cycle:
        payload['Answer'][-1]['data'] = HOST + '.'
    payload['Answer'].append({'name': owners[-1] + '.', 'type': 1, 'TTL': 60, 'data': PUBLIC4})
    transport['payloads'][(HOST, 1)] = payload
    with pytest.raises(acquisition.AcquisitionError) as caught:
        fetch()
    assert caught.value.code == 'source_dns_unavailable' and origins(transport) == []


def test_only_current_official_benchmark_prefixes_are_eligible():
    assert source_dns.is_fake_ip_answer(HOST, ['198.18.0.0', '198.19.255.255', '2001:2:0:ffff::1'])
    assert not source_dns.is_fake_ip_answer(HOST, ['198.17.255.255'])
    assert not source_dns.is_fake_ip_answer(HOST, ['2001:200::1'])  # Uncorrected RFC5180 typo is NOT a Fake-IP pool.
