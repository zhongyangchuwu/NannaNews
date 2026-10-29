"""Offline WeChat source contracts, without network or model calls.

The ordinary-header/minimized-DOM cases follow Main's saved 2026-10-17
response. content_noencode cases remain synthetic external-report cases,
not evidence that a live alternate layout or a gated article was recovered.
"""
import base64
import io
from unittest.mock import Mock

import pytest

from nannanews import acquisition

URL = 'https://mp.weixin.qq.com/s?__biz=ABC%3D%3D&mid=42&idx=1&x=1&x=2'


def source(monkeypatch, script, *, before='', resolved=URL):
    raw = ('<html><head><meta property="og:title" content="原文标题"></head><body>'
           + before + '<script>' + script + '</script></body></html>').encode()
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda requested: (raw, 'text/html; charset=utf-8', resolved))
    return raw


def test_static_literal_recovers_text_and_real_image_urls_without_running_javascript(monkeypatch):
    raw = source(monkeypatch, r'''window.article = { content_noencode: '\x3cp\x3e公开通知\x0a第二段\u3002\uD83D\uDE00</p><img data-src="https://mmbiz.qpic.cn/page?x=1&amp;x=2">' };''')
    document = acquisition.fetch_document(URL)
    assert document['canonical_url'] == URL
    assert document['title'] == '原文标题'
    assert document['text'] == '公开通知\n第二段。😀'
    assert document['images'] == ['https://mmbiz.qpic.cn/page?x=1&x=2']
    assert document['source_published_date'] is None
    assert document['raw_date_text'] is None
    assert base64.b64decode(document['raw_response_base64']) == raw


def test_picture_article_collects_only_literal_page_images_not_cover_or_original_metadata(monkeypatch):
    script = '''window.article = {
        item_show_type: '8' * 1,
        content_noencode: '<p>讲座时间：2026年5月17日，地点：报告厅。</p>',
        picture_page_info_list: [
            {cdn_url: 'https://mmbiz.qpic.cn/poster-one?wx_fmt=png', width: '2657' * 1,
             original_info: {cdn_url: 'https://private.example.org/original'},
             watermark_info: {cdn_url: 'https://static.example.org/watermark'}},
            {"cdn_url": 'https://mmbiz.qpic.cn/poster-two?wx_fmt=png&from=appmsg'}
        ], share_cover: 'https://static.example.org/cover'
    };'''
    raw = source(monkeypatch, script)
    document = acquisition.fetch_document(URL)
    assert document['images'] == ['https://mmbiz.qpic.cn/poster-one?wx_fmt=png',
                                  'https://mmbiz.qpic.cn/poster-two?wx_fmt=png&from=appmsg']
    assert document['text'] == '讲座时间：2026年5月17日，地点：报告厅。'
    assert base64.b64decode(document['raw_response_base64']) == raw


@pytest.mark.parametrize('pages', [
    'loadPictures()',
    '[{cdn_url: choosePrivateImage()}]',
    '[{cdn_url: "https://mmbiz.qpic.cn/first"}].concat(otherPages)',
    '[{cdn_url: "https://mmbiz.qpic.cn/first", cdn_url: "https://mmbiz.qpic.cn/second"}]',
    '[{cdn_url: "https://mmbiz.qpic.cn/first", ...otherPage}]',
    '[{cdn_url: "https://mmbiz.qpic.cn/first", ["cdn_url"]: "https://mmbiz.qpic.cn/second"}]',
    '[{cdn_url: "https://mmbiz.qpic.cn/first", otherPage}]',
])
def test_picture_article_dynamic_or_ambiguous_page_lists_are_not_partial_captures(monkeypatch, pages):
    source(monkeypatch, "window.article = {content_noencode: '<p>正文与海报共同构成通知。</p>', "
           + 'picture_page_info_list: ' + pages + '};')
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.fetch_document(URL)
    assert caught.value.code == 'source_incomplete'


def test_existing_article_container_wins_over_an_unneeded_dynamic_fallback(monkeypatch):
    source(monkeypatch, "window.article = { content_noencode: loadPrivateContent() };",
           before='<h1 id="activity-name">真实标题</h1><div id="js_content">真实公开正文</div>')
    document = acquisition.fetch_document(URL)
    assert document['text'] == '真实公开正文'
    assert document['title'] == '真实标题'


@pytest.mark.parametrize('expression', [
    'loadContent()', "'first' + 'second'", "'first'.replace('f', 'x')",
    '`template ${loadContent()}`', "'first' || 'second'", r"'bad\xGG'",
    r"'bad\uD800'", r"'bad\uDC00'", r"'bad\uD800\u1111'", r"'bad\z'",
    "'unterminated", "'first', content_noencode: 'second'",
])
def test_dynamic_ambiguous_or_malformed_literal_fails_with_original_source_diagnostics(monkeypatch, expression):
    raw = source(monkeypatch, 'window.article = { content_noencode: ' + expression + ' };')
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.fetch_document(URL)
    assert caught.value.code == 'source_incomplete'
    assert base64.b64decode(caught.value.source_response['raw_response_base64']) == raw


@pytest.mark.parametrize('script', [
    "// ignored { content_noencode: 'not article' }\nwindow.other = 1;",
    "/* ignored { content_noencode: 'not article' } */ window.other = 1;",
    '''window.other = "{ content_noencode: 'not article' }";''',
    "window.other = `{ content_noencode: 'not article' }`;",
    "window.other = /{ content_noencode: 'not article' }/;",
])
def test_comments_strings_templates_and_regular_expressions_do_not_become_article_evidence(monkeypatch, script):
    source(monkeypatch, script)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.fetch_document(URL)
    assert caught.value.code == 'source_unsupported_layout'


@pytest.mark.parametrize('before,resolved', [
    ('<h2>环境异常</h2><p>当前环境异常，完成验证后即可继续访问。</p>', URL),
    ('<h2>该内容已被发布者删除</h2>', URL),
    ('<h2>访问过于频繁</h2>', URL),
    ('', 'https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?target_url=original'),
    ('<h2>付费后可阅读全文</h2>', URL),
    ('<h2>请登录后查看</h2>', URL),
])
def test_real_gate_or_removal_markers_take_precedence_over_a_static_article_looking_literal(monkeypatch, before, resolved):
    source(monkeypatch, "window.article = { content_noencode: '<p>article-looking fallback</p>' };",
           before=before, resolved=resolved)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.fetch_document(URL)
    assert caught.value.code == 'source_blocked'


def test_static_image_only_article_is_not_replaced_by_generic_page_chrome(monkeypatch):
    source(monkeypatch, '''window.article = { content_noencode: '<img data-src="https://mmbiz.qpic.cn/poster">' };''',
           before='<nav>页面导航和推荐，不是正文</nav>')
    document = acquisition.fetch_document(URL)
    assert document['text'] == ''
    assert document['images'] == ['https://mmbiz.qpic.cn/poster']


def test_static_literal_over_text_limit_fails_instead_of_silently_truncating(monkeypatch):
    source(monkeypatch, "window.article = { content_noencode: '" + '文' * (acquisition.MAX_TEXT + 1) + "' };")
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.fetch_document(URL)
    assert caught.value.code == 'source_too_large'


def test_observed_public_article_lazy_images_are_complete_deduplicated_and_exclude_reader_chrome(monkeypatch):
    # Minimized from Main's real 3,517,912-byte response, SHA-256
    # 86b2fd8a10ab88e585fddf613cbfd2e1b9aec122a8b0a362295755e3a08271bc.
    # Eleven image nodes refer to five unique original assets; no bytes below
    # pretend to be the full response or to prove image-download success.
    original = 'https://mp.weixin.qq.com/s/LmbixZk-Xd8CrZrMJ2FbKA'
    images = [
        'https://mmbiz.qpic.cn/sz_mmbiz_png/Eqib9L8NPXdIlmABQgsOZE81Kz5neZDwtO1aGs6NbxLco7B6hPmz18ukhCXO7Hv71m8nWjj5QLPEofnTgCWzz3g/640?wx_fmt=png',
        'https://mmbiz.qpic.cn/sz_mmbiz_png/Eqib9L8NPXdIlmABQgsOZE81Kz5neZDwtpRiaH5icKBydoWrViaDhMMPl9n4y83aTup7LBzeGdRY0m5XVtYlKr4iaqw/640?wx_fmt=png',
        'https://mmbiz.qpic.cn/sz_mmbiz_gif/ojjsov6OyAMAHgTNzF7lCZzZyAry90NicjDcYV759ic3XsyYjMaT8NnyXYwiahl9Iibib8xCsOE55ibqRB2vu5I86P0Q/640?wx_fmt=gif',
        'https://mmbiz.qpic.cn/sz_mmbiz_png/fuMA1Xhhmse6JeRGBtd8PlV94gt1QLFd5HtSUYJPtwaX3DPQoDexboJdt3qx7dSPdwpp8GUDDayh07NLichicfDaLBBgONZOdkfMAibmFYJsNw/640?wx_fmt=png',
        'https://mmbiz.qpic.cn/sz_mmbiz_jpg/fuMA1Xhhmsc7frTYnQGdZCjNYVZzWN872pCIKibYfYfKicnrK4ZynzmicK6foJtgVdCa2ia7UaqqLJwfgaruJJlEHXupCw0N6aDjmnOZXN7QbKE/640?wx_fmt=jpeg',
    ]
    title = '星成长丨满天星学术银星训练营第三期——学术研究起点探析（经济学专场）报名启动！'
    nodes = ''.join('<img class="raw-image" data-src="' + images[index] + '">'
                    for index in [0, 1, 2, 0, 1, 2, 0, 1, 3, 2, 4])
    raw = ('<html><body><h1 id="activity-name">' + title + '</h1>'
           '<div>在小说阅读器读本章 去阅读 在公众号小说中沉浸阅读</div>'
           '<em id="publish_time"></em><div id="js_content"><p>前言</p>'
           '<p>本次活动可作为项目制课程同学的过程性学习。</p>' + nodes + '</div></body></html>').encode()
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda requested: (raw, 'text/html; charset=UTF-8, text/html; charset=UTF-8', original))
    document = acquisition.fetch_document(original)
    assert document['canonical_url'] == original and document['resolved_url'] == original
    assert document['title'] == title
    assert document['images'] == [url for index, url in enumerate(images) if index != 2]
    assert len(document['skipped_media']) == 1
    assert document['skipped_media'][0]['type'] == 'gif'
    assert document['skipped_media'][0]['url'] == images[2]
    assert document['skipped_media'][0]['downloaded'] is False
    assert '本次活动' in document['text'] and '小说阅读器' not in document['text']
    assert document['capture_complete'] is True
    assert base64.b64decode(document['raw_response_base64']) == raw


@pytest.fixture
def pinned_http(monkeypatch):
    state = {'responses': [], 'connections': []}

    def connect(host, port, address, timeout):
        connection = Mock()
        connection.sock = None
        connection.getresponse.return_value = state['responses'].pop(0)
        state['connections'].append((host, connection))
        return connection

    monkeypatch.setattr(acquisition, '_source_proxy', lambda parts: None)
    monkeypatch.setattr(acquisition, '_public_addresses', lambda *args: ['93.184.216.34'])
    monkeypatch.setattr(acquisition, '_PinnedHTTPS', connect)
    return state


def response(body=b'public response', *, status=200, location=None):
    result = Mock()
    result.status = status
    headers = {'Content-Type': 'text/html; charset=utf-8', 'Content-Length': str(len(body))}
    if location is not None:
        headers['Location'] = location
    result.getheader.side_effect = lambda name, default=None: headers.get(name, default)
    result.read1.side_effect = io.BytesIO(body).read
    return result


@pytest.mark.parametrize('host,is_wechat', [('mp.weixin.qq.com', True), ('college.nju.edu.cn', False),
                                         ('mmbiz.qpic.cn', False), ('mp.weixin.qq.com.evil.example', False)])
def test_compatible_profile_is_used_on_first_request_only_for_exact_wechat_host(pinned_http, host, is_wechat):
    pinned_http['responses'] = [response()]
    url = f'https://{host}/source?x=1&x=2'
    assert acquisition.fetch_public_resource(url)[2] == url
    assert len(pinned_http['connections']) == 1
    _, connection = pinned_http['connections'][0]
    headers = connection.request.call_args.kwargs['headers']
    if is_wechat:
        assert headers['User-Agent'] == ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
        assert headers['Accept-Language'] == 'zh-CN,zh;q=0.9'
    else:
        assert headers['User-Agent'] == 'NannaPublicNews/1.0'
        assert 'Accept-Language' not in headers
    assert headers['Host'] == host
    assert headers['Accept'] == 'text/html,application/json,image/*;q=0.8'
    assert headers['Accept-Encoding'] == 'identity'
    assert 'Cookie' not in headers and 'Authorization' not in headers


def test_redirect_to_other_host_does_not_keep_wechat_compatibility_headers(pinned_http):
    pinned_http['responses'] = [response(status=302, location='https://college.nju.edu.cn/public'), response()]
    acquisition.fetch_public_resource('https://mp.weixin.qq.com/s/source')
    assert [host for host, _ in pinned_http['connections']] == ['mp.weixin.qq.com', 'college.nju.edu.cn']
    first, second = [connection.request.call_args.kwargs['headers'] for _, connection in pinned_http['connections']]
    assert 'Chrome/131.0.0.0' in first['User-Agent']
    assert second['User-Agent'] == 'NannaPublicNews/1.0'
    assert 'Accept-Language' not in second


def test_gate_after_compatible_first_request_remains_failure_without_profile_switch_or_retry(pinned_http):
    original = 'https://mp.weixin.qq.com/s/LmbixZk-Xd8CrZrMJ2FbKA'
    gate = 'https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?target_url=original'
    body = '<h2>环境异常</h2><p>当前环境异常，完成验证后即可继续访问。</p>'.encode()
    pinned_http['responses'] = [response(status=302, location=gate), response(body)]
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.fetch_document(original)
    assert caught.value.code == 'source_blocked'
    assert caught.value.source_response['canonical_url'] == original
    assert caught.value.source_response['resolved_url'] == gate
    assert base64.b64decode(caught.value.source_response['raw_response_base64']) == body
    assert len(pinned_http['connections']) == 2  # Original GET plus its one redirect, no retry.
    for _, connection in pinned_http['connections']:
        assert 'Chrome/131.0.0.0' in connection.request.call_args.kwargs['headers']['User-Agent']
        connection.close.assert_called_once()
