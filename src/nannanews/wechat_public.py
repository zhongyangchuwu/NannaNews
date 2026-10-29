"""Validate public WeChat article identities and publication metadata."""
from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from nannanews import acquisition
from nannanews.acquisition import AcquisitionError, MAX_BODY, parse_source_date


SOURCE_KINDS = ('wechat_gui', 'wechat_official_links')

_HOST = 'mp.weixin.qq.com'
_IDENTITY_KEYS = ('__biz', 'mid', 'idx', 'sn')
_BIZ_PATTERN = re.compile(r"(?<![\w$])(?:var\s+)?(?:window\s*\.\s*)?biz\s*[:=]\s*(['\"])([A-Za-z0-9+/=]+)\1")
_DATE_ASSIGNMENT = re.compile(
    r"(?<![\w$])(?:var\s+)?(?:ct|create_time|createTime|publish_time|publishTime)\s*[:=]\s*(?:\"(\d{9,13})\"|'(\d{9,13})'|(\d{9,13})(?!\d))"
)
_PUBLICATION_LABEL = re.compile(
    r'(?:发布时间|发布日期|发布于)\s*[:：]?\s*(\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}日?(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?)'
)
_EPOCH_TZ = timezone(timedelta(hours=8))
_META_PUBLICATION_KEYS = {
    'article:published_time', 'datepublished', 'pubdate', 'publishdate',
    'publish_date', 'publication_date',
}


def _invalid_url():
    raise AcquisitionError('微信文章链接无效或缺少稳定文章标识。', 'invalid_url')


def _invalid_definition():
    raise AcquisitionError('微信公众账号来源定义无效。', 'source_definition_invalid')


def _valid_biz(value):
    if not isinstance(value, str) or not value or len(value) > 128:
        return False
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return False
    return (base64.b64encode(decoded).decode('ascii') == value
            and re.fullmatch(rb'[0-9]{6,20}', decoded) is not None)


def _query_pairs(query):
    if re.search(r'%(?![0-9A-Fa-f]{2})', query):
        _invalid_url()
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True,
                          encoding='utf-8', errors='strict', max_num_fields=64)
    except (ValueError, UnicodeError):
        _invalid_url()
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        _invalid_url()
    return dict(pairs)


def article_url(value: str) -> str:
    """Return a canonical public article URL without tracking data or fragments."""
    if (not isinstance(value, str) or not value or len(value) > 8192
            or value != value.strip() or re.search(r'[\x00-\x20\x7f\\]', value)):
        _invalid_url()
    try:
        parts = urlsplit(value)
        if (parts.scheme.lower() != 'https' or parts.netloc.lower() != _HOST
                or parts.hostname != _HOST or parts.username is not None
                or parts.password is not None or parts.port is not None):
            _invalid_url()
        query = _query_pairs(parts.query)
    except (ValueError, UnicodeError):
        _invalid_url()

    slug = re.fullmatch(r'/s/([A-Za-z0-9_-]{1,256})', parts.path)
    if slug:
        return urlunsplit(('https', _HOST, parts.path, '', ''))
    if parts.path != '/s' or not set(_IDENTITY_KEYS).issubset(query):
        _invalid_url()

    biz, mid, idx, sn = (query[key] for key in _IDENTITY_KEYS)
    if (not _valid_biz(biz) or not re.fullmatch(r'[0-9]{1,32}', mid)
            or not re.fullmatch(r'[0-9]{1,8}', idx) or int(idx) < 1
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', sn)):
        _invalid_url()
    identity = urlencode([(key, query[key]) for key in _IDENTITY_KEYS])
    return urlunsplit(('https', _HOST, '/s', identity, ''))


def validate_definition(definition):
    """Validate and normalize public WeChat source fields in-place."""
    if not isinstance(definition, dict):
        _invalid_definition()
    kind = definition.get('kind')
    if kind not in SOURCE_KINDS:
        return definition

    name = definition.get('expected_identity')
    biz = definition.get('wechat_biz')
    try:
        identity_url = article_url(definition.get('identity_url'))
        if kind == 'wechat_gui':
            url = article_url(definition.get('url'))
        else:
            url = acquisition.canonical_url(definition.get('url'))
            if urlsplit(url).hostname == _HOST:
                _invalid_definition()
    except AcquisitionError:
        _invalid_definition()
    if (not isinstance(name, str) or not name.strip()
            or not _valid_biz(biz)):
        _invalid_definition()
    if kind == 'wechat_gui' and url != identity_url:
        _invalid_definition()

    if kind == 'wechat_official_links':
        listing_identity = definition.get('listing_identity')
        if (not isinstance(listing_identity, str)
                or not listing_identity.strip()
                or len(listing_identity.strip()) > 256):
            _invalid_definition()
        definition['listing_identity'] = listing_identity.strip()

    definition['expected_identity'] = name.strip()
    definition['url'] = url
    definition['identity_url'] = identity_url
    return definition


def _decode_public_html(body, content_type):
    if not isinstance(content_type, str) or 'html' not in content_type.lower():
        raise AcquisitionError('公开页面不是 HTML。', 'source_identity')
    charset = re.search(r'charset=([\w-]+)', content_type, re.I)
    try:
        return body.decode(charset.group(1) if charset else 'utf-8', errors='strict')
    except (LookupError, UnicodeError):
        raise AcquisitionError('公开页面编码不可读取。', 'source_blocked') from None


def _public_html_soup(body, content_type):
    try:
        return BeautifulSoup(_decode_public_html(body, content_type), 'html.parser')
    except (ValueError, RecursionError):
        raise AcquisitionError('公开页面无法解析。', 'source_blocked') from None

def discover_official_articles(definition, *, page_limit, deadline=None):
    """Return bounded, BIZ-verified article links visible on one official page."""
    validate_definition(definition)
    if definition.get('kind') != 'wechat_official_links':
        _invalid_definition()
    if type(page_limit) is not int or page_limit < 1:
        raise ValueError('Official listing page limit must be positive.')

    stop_at = None
    if deadline is not None:
        if (not isinstance(deadline, datetime) or deadline.tzinfo is None
                or deadline.utcoffset() is None):
            raise ValueError('Official listing deadline must be timezone-aware.')
        remaining = (deadline.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
        stop_at = time.monotonic() + max(0.0, remaining)

    body, content_type, resolved = acquisition.fetch_public_resource(
        definition['url'], max_bytes=MAX_BODY,
        deadline=min(stop_at, time.monotonic() + acquisition.SOURCE_TIMEOUT) if stop_at is not None else None)
    try:
        resolved = acquisition.canonical_url(resolved)
    except AcquisitionError:
        raise AcquisitionError('官方列表页面重定向目标无效。', 'source_identity') from None
    if urlsplit(resolved).hostname != urlsplit(definition['url']).hostname:
        raise AcquisitionError('官方列表页面重定向到不同发布者。', 'source_identity')

    soup = _public_html_soup(body, content_type)
    for node in soup(['script', 'style', 'noscript', 'template']):
        node.decompose()
    for node in soup.select('[hidden], [aria-hidden="true"]'):
        node.decompose()
    for node in soup.find_all(style=True):
        if node.attrs is None:  # A hidden ancestor may already have decomposed this node.
            continue
        if re.search(r'(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b', node['style'], re.I):
            node.decompose()

    listing_identity = definition['listing_identity']
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    visible = soup.get_text(' ', strip=True)
    if listing_identity not in title and listing_identity not in visible:
        raise AcquisitionError('官方列表页面未显示预期发布者身份。', 'source_identity')

    limit = min(4 * page_limit, 24)
    candidates = []
    seen = set()
    for anchor in soup.find_all('a', href=True):
        try:
            url = article_url(urljoin(resolved, anchor['href']))
        except (AcquisitionError, ValueError):
            continue
        if url in seen:
            continue
        seen.add(url)
        candidates.append(url)
        if len(candidates) >= limit:
            break

    links = []
    probe_error = None
    skipped_unverified = False
    for url in candidates:
        try:
            article_body, article_type, article_resolved = acquisition.fetch_public_resource(
                url, max_bytes=MAX_BODY,
                deadline=min(stop_at, time.monotonic() + acquisition.SOURCE_TIMEOUT) if stop_at is not None else None)
            article = {
                'source_type': 'wechat',
                'canonical_url': url,
                'resolved_url': article_resolved,
                'raw_content': _decode_public_html(article_body, article_type),
            }
            verify_document(article, definition)
        except AcquisitionError as error:
            if error.code == 'source_identity_mismatch':
                continue
            if error.code == 'source_identity_unverified':
                skipped_unverified = True
                continue
            probe_error = error
            break
        links.append({'url': url, 'published_date': None, 'disallowed': False})

    message = '仅发现配置的官方页面中当前可见且 BIZ 匹配的微信公众号文章链接，不代表账号完整文章历史。'
    if skipped_unverified:
        message += ' 部分链接缺少可核验的发布者元数据，未采集这些链接，仍有发现覆盖缺口。'
    if probe_error is not None:
        message += f' 部分文章链接的发布者核验未完成（{probe_error.code}）：{str(probe_error)[:200]}；后续链接未继续检查。'
    return {'links': links, 'complete': False, 'message': message}





def _failure(code='source_identity_unverified'):
    raise AcquisitionError('微信文章发布者身份无法核验。', code)


def _is_verification_page(soup, document):
    resolved = document.get('resolved_url')
    try:
        if isinstance(resolved, str) and urlsplit(resolved).path == '/mp/wappoc_appmsgcaptcha':
            return True
    except ValueError:
        return True
    if soup.select_one('#js_verify'):
        return True
    title = soup.select_one('.weui-msg__title')
    if title and re.search(r'环境异常|验证', title.get_text(' ', strip=True)):
        return True
    visible = soup.get_text(' ', strip=True)
    return bool(re.search(
        r'当前环境异常|环境异常.{0,20}验证|完成验证后|请完成.{0,8}验证|'
        r'访问过于频繁|操作频繁|登录后阅读全文|付费后(?:可)?(?:阅读|查看)', visible
    ))


def _soup_for_document(document):
    if not isinstance(document, dict) or document.get('source_type') != 'wechat':
        _failure()
    raw = document.get('raw_content')
    if not isinstance(raw, str) or not raw or len(raw) > MAX_BODY:
        _failure()
    try:
        return BeautifulSoup(raw, 'html.parser')
    except (ValueError, RecursionError):
        _failure()


def _public_biz_values(soup):
    values = set()
    for script in soup.find_all('script'):
        text = script.string or script.get_text()
        values.update(match.group(2) for match in _BIZ_PATTERN.finditer(text))
    for node in soup.find_all('meta', attrs={'property': 'og:url'}):
        value = node.get('content')
        if not isinstance(value, str):
            continue
        try:
            parts = urlsplit(value)
            if parts.scheme.lower() != 'https' or parts.netloc.lower() != _HOST or parts.path != '/s':
                continue
            query = _query_pairs(parts.query)
        except (ValueError, UnicodeError, AcquisitionError):
            continue
        biz = query.get('__biz')
        if biz is not None:
            values.add(biz)
    return values


def verify_document(document, definition) -> None:
    """Refuse source attribution unless fetched WeChat article metadata proves its public biz."""
    validate_definition(definition)
    soup = _soup_for_document(document)
    if _is_verification_page(soup, document):
        _failure('source_blocked')
    try:
        article_url(document.get('canonical_url'))
        resolved = document.get('resolved_url')
        if resolved is not None:
            article_url(resolved)
    except AcquisitionError:
        _failure()

    values = _public_biz_values(soup)
    if not values or any(not _valid_biz(value) for value in values):
        _failure()
    if values != {definition['wechat_biz']}:
        _failure('source_identity_mismatch')


def _epoch_date(value):
    try:
        seconds = int(value)
        if len(value) == 13:
            seconds //= 1000
        return datetime.fromtimestamp(seconds, timezone.utc).astimezone(_EPOCH_TZ).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _jsonld_publication_dates(script, candidates, canonical_url):
    try:
        payload = json.loads(script.string or script.get_text())
        current = article_url(canonical_url)
    except (ValueError, TypeError, RecursionError, AcquisitionError):
        return
    pending = [(item, False) for item in payload] if isinstance(payload, list) else [(payload, False)]
    while pending:
        value, require_identity = pending.pop()
        if not isinstance(value, dict):
            continue
        graph = value.get('@graph')
        if isinstance(graph, list):
            pending.extend((item, True) for item in graph)
        types = value.get('@type')
        types = [types] if isinstance(types, str) else types if isinstance(types, list) else []
        if not any(isinstance(kind, str) and kind.rsplit('/', 1)[-1] in
                   {'Article', 'NewsArticle', 'BlogPosting'} for kind in types):
            continue
        identity = value.get('url') or value.get('mainEntityOfPage') or value.get('@id')
        if isinstance(identity, dict):
            identity = identity.get('@id')
        if require_identity and identity is None:
            continue
        if identity is not None:
            try:
                if article_url(identity) != current:
                    continue
            except AcquisitionError:
                continue
        parsed = parse_source_date(value.get('datePublished'))
        if parsed:
            candidates.add(parsed)


def published_date(document) -> str | None:
    """Return one unambiguous explicit WeChat publication date, or None."""
    if not isinstance(document, dict) or document.get('source_type') != 'wechat':
        return None
    candidates = set()
    for field in ('raw_date_text', 'source_published_date'):
        parsed = parse_source_date(document.get(field))
        if parsed:
            candidates.add(parsed)

    raw = document.get('raw_content')
    if isinstance(raw, str) and raw and len(raw) <= MAX_BODY:
        try:
            soup = BeautifulSoup(raw, 'html.parser')
        except (ValueError, RecursionError):
            return None
        if _is_verification_page(soup, document):
            return None
        for node in soup.find_all('meta'):
            key = str(node.get('property') or node.get('name') or node.get('itemprop') or '').lower()
            if key in _META_PUBLICATION_KEYS:
                parsed = parse_source_date(node.get('content', ''))
                if parsed:
                    candidates.add(parsed)
        for node in soup.select('#publish_time, time[itemprop="datePublished"], time.pubdate, .publish_time, .publish-date'):
            parsed = parse_source_date(node.get('datetime') or node.get_text(' ', strip=True))
            if parsed:
                candidates.add(parsed)
        for script in soup.find_all('script', type='application/ld+json'):
            _jsonld_publication_dates(script, candidates, document.get('canonical_url'))
        visible = soup.get_text(' ', strip=True)
        candidates.update(parsed for match in _PUBLICATION_LABEL.finditer(visible)
                          if (parsed := parse_source_date(match.group(1))))
        for script in soup.find_all('script'):
            text = script.string or script.get_text()
            for match in _DATE_ASSIGNMENT.finditer(text):
                timestamp = next(group for group in match.groups() if group is not None)
                parsed = parse_source_date(_epoch_date(timestamp))
                if parsed:
                    candidates.add(parsed)

    return next(iter(candidates)) if len(candidates) == 1 else None
