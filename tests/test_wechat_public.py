"""Public WeChat article identity and publication metadata contracts."""
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import urlencode

import pytest

from nannanews import acquisition, wechat_public


BIZ = 'MzAxODAzMjQ1NQ=='
OTHER_BIZ = 'OTg3NjU0MzIxMA=='
REFERENCE_URL = 'https://mp.weixin.qq.com/s/reference'
LISTING_URL = 'https://med.nju.edu.cn/news'
LISTING_IDENTITY = '南京大学医学院'



def definition(**overrides):
    result = {
        'kind': 'wechat_gui',
        'url': REFERENCE_URL,
        'identity_url': REFERENCE_URL,
        'expected_identity': '南京大学',
        'wechat_biz': BIZ,
    }
    result.update(overrides)
    return result

def official_definition(**overrides):
    result = {
        'kind': 'wechat_official_links',
        'url': LISTING_URL,
        'identity_url': REFERENCE_URL,
        'expected_identity': LISTING_IDENTITY,
        'wechat_biz': BIZ,
        'listing_identity': LISTING_IDENTITY,
    }
    result.update(overrides)
    return result


def install_listing_fetch(monkeypatch, markup, *, resolved_listing=None,
                          article_biz=None, unavailable=(), unexpected=()):
    calls = []

    def fetch(url, *, max_bytes, deadline):
        calls.append(url)
        if url == LISTING_URL:
            return markup.encode('utf-8'), 'text/html; charset=utf-8', resolved_listing or url
        if url in unavailable:
            raise acquisition.AcquisitionError('metadata unavailable', 'source_unavailable')
        if url in unexpected:
            raise RuntimeError('unexpected metadata failure')
        biz = (article_biz or {}).get(url, BIZ)
        raw = f'<html><script>var biz = "{biz}";</script></html>'
        return raw.encode('utf-8'), 'text/html; charset=utf-8', url

    monkeypatch.setattr(acquisition, 'fetch_public_resource', fetch)
    return calls


def listing_page(body='', *, title=LISTING_IDENTITY):
    return f'<html><head><title>{title}</title></head><body>{body}</body></html>'


def article_anchor(url):
    return f'<a href="{url}">文章</a>'



def document(raw_content, **overrides):
    result = {
        'source_type': 'wechat',
        'canonical_url': REFERENCE_URL,
        'resolved_url': REFERENCE_URL,
        'raw_content': raw_content,
    }
    result.update(overrides)
    return result


def query_url(biz=BIZ):
    query = urlencode([
        ('__biz', biz), ('mid', '123456789'), ('idx', '1'), ('sn', 'a' * 32),
    ])
    return 'https://mp.weixin.qq.com/s?' + query


def test_article_url_canonicalizes_slug_and_drops_tracking_and_fragment():
    assert wechat_public.article_url(
        REFERENCE_URL + '?utm_source=private-fragment#read-more'
    ) == REFERENCE_URL


def test_article_url_preserves_only_complete_ordered_query_identity():
    raw = 'https://mp.weixin.qq.com/s?sn=abc123&idx=2&mid=42&__biz=' + BIZ + '&from=timeline#fragment'
    expected = 'https://mp.weixin.qq.com/s?__biz=MzAxODAzMjQ1NQ%3D%3D&mid=42&idx=2&sn=abc123'
    assert wechat_public.article_url(raw) == expected
    assert wechat_public.article_url(query_url()) == query_url()


@pytest.mark.parametrize('url', [
    None,
    'http://mp.weixin.qq.com/s/slug',
    'https://mp.weixin.qq.com.evil.example/s/slug',
    'https://mp.weixin.qq.com@evil.example/s/slug?token=DO_NOT_ECHO',
    'https://mp.weixin.qq.com:443/s/slug',
    'https://mp.weixin.qq.com/mp/appmsg',
    'https://mp.weixin.qq.com/s/',
    'https://mp.weixin.qq.com/s?__biz=' + BIZ + '&mid=42&idx=1',
    'https://mp.weixin.qq.com/s?__biz=' + BIZ + '&__biz=' + BIZ + '&mid=42&idx=1&sn=abc',
    'https://mp.weixin.qq.com/s?__biz=' + BIZ + '&mid=42&idx=1&sn=abc&x=1&x=DO_NOT_ECHO',
    'https://mp.weixin.qq.com/s?__biz=not-a-public-biz&mid=42&idx=1&sn=abc',
    'https://mp.weixin.qq.com/s?__biz=' + BIZ + '&mid=42&idx=0&sn=abc',
    'https://mp.weixin.qq.com/s?__biz=' + BIZ + '&mid=42&idx=1&sn=bad%ZZ',
])
def test_article_url_rejects_unsafe_or_ambiguous_inputs_without_echoing_values(url):
    with pytest.raises(acquisition.AcquisitionError) as caught:
        wechat_public.article_url(url)
    assert 'DO_NOT_ECHO' not in str(caught.value)
    assert 'not-a-public-biz' not in str(caught.value)


def test_validate_definition_normalizes_urls_and_mutates_the_same_mapping():
    source = definition(
        url=REFERENCE_URL + '?utm_source=tracking#section',
        identity_url=REFERENCE_URL + '?from=timeline',
        expected_identity=' 南京大学 ',
    )
    result = wechat_public.validate_definition(source)
    assert result is source
    assert source['url'] == REFERENCE_URL
    assert source['identity_url'] == REFERENCE_URL
    assert source['expected_identity'] == '南京大学'


@pytest.mark.parametrize('overrides', [
    {'url': 'https://mp.weixin.qq.com/s/first', 'identity_url': 'https://mp.weixin.qq.com/s/second'},
    {'expected_identity': '  '},
    {'wechat_biz': 'private-not-base64'},
])
def test_validate_definition_rejects_nonmatching_or_missing_identity(overrides):
    with pytest.raises(acquisition.AcquisitionError) as caught:
        wechat_public.validate_definition(definition(**overrides))
    assert 'private-not-base64' not in str(caught.value)


def test_validate_definition_leaves_non_gui_contracts_unchanged():
    source = {'kind': 'html', 'url': 'https://example.edu/news', 'extension': {'x': 1}}
    before = source.copy()
    assert wechat_public.validate_definition(source) is source
    assert source == before



def test_official_definition_normalizes_listing_and_reference_independently():
    source = official_definition(
        url=' https://med.nju.edu.cn/news#top ',
        identity_url=REFERENCE_URL + '?from=timeline#section',
        expected_identity=' 南京大学医学院 ',
        listing_identity=' 南京大学医学院 ',
    )
    assert wechat_public.validate_definition(source) is source
    assert source['url'] == LISTING_URL
    assert source['identity_url'] == REFERENCE_URL
    assert source['expected_identity'] == LISTING_IDENTITY
    assert source['listing_identity'] == LISTING_IDENTITY


@pytest.mark.parametrize('overrides', [
    {'url': REFERENCE_URL},
    {'listing_identity': ''},
    {'listing_identity': ' '},
    {'listing_identity': 'x' * 257},
    {'identity_url': 'https://example.edu/article'},
    {'wechat_biz': 'private-not-base64'},
])
def test_validate_definition_rejects_invalid_official_listing_sources(overrides):
    with pytest.raises(acquisition.AcquisitionError):
        wechat_public.validate_definition(official_definition(**overrides))


def test_discovery_requires_visible_publisher_and_rejects_cross_publisher_redirect(monkeypatch):
    source = official_definition()
    markup = listing_page(
        f'<script>{LISTING_IDENTITY}</script><div hidden>{LISTING_IDENTITY}</div>', title='')
    calls = install_listing_fetch(monkeypatch, markup)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        wechat_public.discover_official_articles(source, page_limit=2)
    assert caught.value.code == 'source_identity'
    assert calls == [LISTING_URL]

    markup = listing_page(article_anchor(REFERENCE_URL))
    calls = install_listing_fetch(
        monkeypatch, markup, resolved_listing='https://evil.example/news')
    with pytest.raises(acquisition.AcquisitionError) as caught:
        wechat_public.discover_official_articles(official_definition(), page_limit=2)
    assert caught.value.code == 'source_identity'
    assert calls == [LISTING_URL]


@pytest.mark.parametrize('hidden_style', ['display:none', 'visibility:hidden'])
def test_nested_hidden_styled_content_is_neither_discovered_nor_fetched(monkeypatch, hidden_style):
    visible = 'https://mp.weixin.qq.com/s/visible-article'
    hidden = 'https://mp.weixin.qq.com/s/hidden-article'
    markup = listing_page(
        f'<div style="{hidden_style}"><span style="color:red">{article_anchor(hidden)}</span></div>'
        + article_anchor(visible))
    calls = install_listing_fetch(monkeypatch, markup)
    result = wechat_public.discover_official_articles(official_definition(), page_limit=2)
    assert [link['url'] for link in result['links']] == [visible]
    assert hidden not in calls


@pytest.mark.parametrize(('unreadable_body', 'expected_count'), [
    ('<div class="weui-msg__title">该内容已删除</div>', 1),
    ('<div id="js_verify">请完成验证</div>', 0),
])
def test_unattributed_candidate_does_not_block_other_public_links_but_verification_does(
        monkeypatch, unreadable_body, expected_count):
    unreadable = 'https://mp.weixin.qq.com/s/unattributed-article'
    visible = 'https://mp.weixin.qq.com/s/later-public-article'
    markup = listing_page(article_anchor(unreadable) + article_anchor(visible))
    calls = install_listing_fetch(monkeypatch, markup)
    original_fetch = acquisition.fetch_public_resource

    def fetch(url, **kwargs):
        if url == unreadable:
            calls.append(url)
            return unreadable_body.encode('utf-8'), 'text/html; charset=utf-8', url
        return original_fetch(url, **kwargs)

    monkeypatch.setattr(acquisition, 'fetch_public_resource', fetch)
    result = wechat_public.discover_official_articles(official_definition(), page_limit=2)
    assert [link['url'] for link in result['links']] == [visible][:expected_count]
    assert (visible in calls) is bool(expected_count)
    assert result['complete'] is False


def test_discovery_uses_only_actual_anchors_and_filters_other_biz(monkeypatch):
    first = 'https://mp.weixin.qq.com/s/first-article'
    other = 'https://mp.weixin.qq.com/s/other-account'
    scripted = 'https://mp.weixin.qq.com/s/script-only'
    markup = listing_page(
        article_anchor(first) + article_anchor(first) + article_anchor(other)
        + f'<script>{LISTING_IDENTITY}<a href="{scripted}"></a></script>'
    )
    calls = install_listing_fetch(monkeypatch, markup, article_biz={other: OTHER_BIZ})

    result = wechat_public.discover_official_articles(official_definition(), page_limit=2)

    assert result['links'] == [
        {'url': first, 'published_date': None, 'disallowed': False},
    ]
    assert calls == [LISTING_URL, first, other]
    assert result['complete'] is False


@pytest.mark.parametrize(('page_limit', 'expected_count'), [(2, 8), (10, 24)])
def test_discovery_stably_deduplicates_and_caps_verified_links(
        monkeypatch, page_limit, expected_count):
    urls = [f'https://mp.weixin.qq.com/s/article-{index}' for index in range(30)]
    markup = listing_page(''.join(article_anchor(url) for url in [urls[0], *urls, urls[1]]))
    calls = install_listing_fetch(monkeypatch, markup)

    result = wechat_public.discover_official_articles(official_definition(), page_limit=page_limit)

    assert [link['url'] for link in result['links']] == urls[:expected_count]
    assert len(calls) == expected_count + 1


def test_empty_official_listing_is_an_honest_partial_snapshot(monkeypatch):
    calls = install_listing_fetch(monkeypatch, listing_page())

    result = wechat_public.discover_official_articles(official_definition(), page_limit=2)
    assert result['links'] == []
    assert result['complete'] is False
    assert calls == [LISTING_URL]


def test_discovery_stops_at_unverifiable_candidate_and_retains_prior_verified_links(monkeypatch):
    first = 'https://mp.weixin.qq.com/s/first-article'
    second = 'https://mp.weixin.qq.com/s/unavailable-article'
    third = 'https://mp.weixin.qq.com/s/not-probed-after-access-failure'
    markup = listing_page(article_anchor(first) + article_anchor(second) + article_anchor(third))
    calls = install_listing_fetch(monkeypatch, markup, unavailable={second})

    result = wechat_public.discover_official_articles(official_definition(), page_limit=1)

    assert [link['url'] for link in result['links']] == [first]
    assert result['complete'] is False
    assert calls == [LISTING_URL, first, second]


@pytest.mark.parametrize(('cutoff_seconds', 'accepted_count'), [(100, 2), (50, 1)])
def test_official_discovery_gives_each_request_its_budget_without_crossing_history_cutoff(
        monkeypatch, cutoff_seconds, accepted_count):
    urls = ['https://mp.weixin.qq.com/s/slow-first', 'https://mp.weixin.qq.com/s/slow-second']
    clock = [1000.0]
    markup = listing_page(''.join(article_anchor(url) for url in urls))

    def fetch(url, *, max_bytes, deadline):
        assert deadline is not None and deadline - clock[0] <= acquisition.SOURCE_TIMEOUT
        if deadline - clock[0] < 20:
            raise acquisition.AcquisitionError('公共来源读取超时。', 'source_unavailable')
        clock[0] += 20
        body = markup if url == LISTING_URL else f'<script>var biz="{BIZ}";</script>'
        return body.encode('utf-8'), 'text/html; charset=utf-8', url

    monkeypatch.setattr(wechat_public.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(acquisition, 'fetch_public_resource', fetch)
    cutoff = datetime.now(timezone.utc) + timedelta(seconds=cutoff_seconds)
    result = wechat_public.discover_official_articles(official_definition(), page_limit=1, deadline=cutoff)
    assert [link['url'] for link in result['links']] == urls[:accepted_count]


def test_discovery_does_not_swallow_unknown_metadata_errors(monkeypatch):
    candidate = 'https://mp.weixin.qq.com/s/unknown-error'
    markup = listing_page(article_anchor(candidate))
    install_listing_fetch(monkeypatch, markup, unexpected={candidate})

    with pytest.raises(RuntimeError, match='unexpected metadata failure'):
        wechat_public.discover_official_articles(official_definition(), page_limit=1)

def test_verify_document_accepts_matching_public_biz_assignment():
    captured = document(f'<script>var biz = "{BIZ}"; var ct = "1792281060";</script>')
    assert wechat_public.verify_document(captured, definition()) is None


@pytest.mark.parametrize('html', [
    f'<script>window.biz = \'{BIZ}\';</script>',
    f'<meta property="og:url" content="{query_url()}">',
])
def test_verify_document_accepts_public_biz_metadata_variants(html):
    assert wechat_public.verify_document(document(html), definition()) is None


def test_verify_document_rejects_mismatched_or_conflicting_business_identity():
    for html in (
        f'<script>var biz="{OTHER_BIZ}";</script>',
        f'<script>var biz="{BIZ}"; window.biz="{OTHER_BIZ}";</script>',
    ):
        with pytest.raises(acquisition.AcquisitionError) as caught:
            wechat_public.verify_document(document(html), definition())
        assert caught.value.code == 'source_identity_mismatch'
        assert OTHER_BIZ not in str(caught.value)


def test_verify_document_refuses_captcha_and_missing_or_non_wechat_evidence():
    captcha = document(
        f'<script>var biz="{BIZ}";</script><div id="js_verify">完成验证</div>'
    )
    with pytest.raises(acquisition.AcquisitionError) as caught:
        wechat_public.verify_document(captcha, definition())
    assert caught.value.code == 'source_blocked'

    for captured in (
        document('<div id="js_content">公开正文但没有发布者元数据</div>'),
        document(f'<script>var biz="{BIZ}";</script>', source_type='public_html'),
        document(f'<script>var biz="{BIZ}";</script>', canonical_url='https://evil.example/s/a'),
    ):
        with pytest.raises(acquisition.AcquisitionError):
            wechat_public.verify_document(captured, definition())


def test_published_date_reads_publication_meta_dom_and_jsonld_variants():
    cases = [
        ('<meta property="article:published_time" content="2026-10-18T23:30:00Z">', '2026-10-19'),
        ('<div id="publish_time">2026-10-18 07:51</div>', '2026-10-18'),
        ('<time itemprop="datePublished" datetime="2026-10-18T17:30:00-07:00"></time>', '2026-10-19'),
        ('<script type="application/ld+json">' + json.dumps({'@graph': [
            {'@type': 'NewsArticle', 'url': REFERENCE_URL, 'datePublished': '2026-10-18'}
        ]}) + '</script>', '2026-10-18'),
        ('<p>发布时间：2026年10月18日</p>', '2026-10-18'),
    ]
    for markup, expected in cases:
        assert wechat_public.published_date(document(markup)) == expected


def test_published_date_reads_wechat_ct_and_create_time_epoch_variants():
    instant = datetime(2026, 10, 17, 23, 51, tzinfo=timezone.utc)
    seconds = int(instant.timestamp())
    milliseconds = seconds * 1000
    cases = [
        (f'<script>var ct = "{seconds}";</script>', '2026-10-18'),
        (f'<script>var create_time={milliseconds};</script>', '2026-10-18'),
        (f'<script>var publishTime = \'{seconds}\';</script>', '2026-10-18'),
    ]
    for markup, expected in cases:
        assert wechat_public.published_date(document(markup)) == expected


def test_published_date_uses_validated_capture_fields_but_never_event_or_fetch_dates():
    assert wechat_public.published_date(document('', raw_date_text='2026-10-18 07:51')) == '2026-10-18'
    assert wechat_public.published_date(document('', source_published_date='2026-10-18')) == '2026-10-18'
    event_only = document(
        '<div id="js_content">活动时间：2026年10月18日</div>',
        fetched_at='2026-10-19T00:00:00+08:00',
    )
    assert wechat_public.published_date(event_only) is None
    assert wechat_public.published_date(None) is None


def test_published_date_returns_none_for_invalid_or_ambiguous_metadata():
    invalid = document('<meta property="article:published_time" content="2026-02-30">')
    before_epoch = document('', source_published_date='1970-01-01')
    conflict = document(
        '<meta property="article:published_time" content="2026-10-18">'
        '<script>var ct="1792367460";</script>'
    )
    assert wechat_public.published_date(invalid) is None
    assert wechat_public.published_date(before_epoch) is None
    assert wechat_public.published_date(conflict) is None
    assert wechat_public.published_date(document('<script>var ct="0";</script>')) is None


def test_published_date_accepts_duplicate_agreeing_publication_values():
    markup = (
        '<meta property="article:published_time" content="2026-10-18">'
        '<div id="publish_time">2026-10-18</div>'
    )
    assert wechat_public.published_date(document(markup)) == '2026-10-18'


@pytest.mark.parametrize('payload', [
    {'@type': 'NewsArticle', 'comment': {'@type': 'Comment', 'datePublished': '2026-10-19'}},
    {'@type': 'Comment', 'datePublished': '2026-10-19'},
    {'@graph': [{'datePublished': '2026-10-19'}]},
    {'@graph': [{'@type': 'NewsArticle', 'datePublished': '2026-10-19'}]},
    {'@graph': [{'@type': 'NewsArticle', 'url': 'https://mp.weixin.qq.com/s/other',
                 'datePublished': '2026-10-19'}]},
])
def test_nested_or_unrelated_jsonld_dates_do_not_date_the_article(payload):
    markup = '<script type="application/ld+json">' + json.dumps(payload) + '</script>'
    assert wechat_public.published_date(document(markup)) is None


def test_article_date_is_not_overridden_by_its_comments():
    payload = {'@type': 'NewsArticle', 'datePublished': '2026-10-18',
               'comment': {'@type': 'Comment', 'datePublished': '2026-10-19'}}
    markup = '<script type="application/ld+json">' + json.dumps(payload) + '</script>'
    assert wechat_public.published_date(document(markup)) == '2026-10-18'
