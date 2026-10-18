"""Article capture excludes site chrome without dropping actual article images."""
import base64
import hashlib
import http.client
import io
import json
from unittest.mock import Mock

import pytest

from nannanews import acquisition


@pytest.mark.parametrize('container', ['wp_articlecontent', 'w-content', 'wzy-content'])
def test_article_container_excludes_navigation_images_but_retains_original_bytes(monkeypatch, container):
    navigation = ''.join(f'<img src="/layout/nav-{index}.png">' for index in range(acquisition.MAX_IMAGES + 2))
    body = (f'<html><head><title>公开讲座</title></head><body><div class="navigation">导航文字{navigation}</div>'
            f'<div class="{container}"><h1>公开讲座</h1><p>报告时间与报名方式见本讲座海报。</p>'
            '<img src="/article/poster.png"></div><footer>其他学院活动</footer></body></html>').encode()
    url = 'https://college.nju.edu.cn/notices/lecture.html'
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda requested: (body, 'text/html; charset=utf-8', url))
    document = acquisition.fetch_document(url)
    assert document['images'] == ['https://college.nju.edu.cn/article/poster.png']
    assert '报告时间与报名方式' in document['text']
    assert '导航文字' not in document['text'] and '其他学院活动' not in document['text']
    assert base64.b64decode(document['raw_response_base64']) == body
    assert document['raw_response_digest'] == hashlib.sha256(body).hexdigest()


def test_multiple_article_images_are_preserved_without_four_image_rejection(monkeypatch):
    urls = [f'https://college.nju.edu.cn/article/page-{index}.png' for index in range(6)]
    images = ''.join(f'<img src="{url}">' for url in urls)
    body = f'<html><body><article><h1>多页公开通知</h1><p>全部图片均属于原文，不能静默截取。</p>{images}</article></body></html>'.encode()
    url = 'https://college.nju.edu.cn/notices/multipage.html'
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda requested: (body, 'text/html; charset=utf-8', url))
    document = acquisition.fetch_document(url)
    assert document['images'] == urls
    assert base64.b64decode(document['raw_response_base64']) == body


def test_pdf_viewer_declared_pages_are_capture_evidence_not_executed_script(monkeypatch):
    pages = [f'/previews/page-{index}.jpg' for index in range(5)]
    script = ('var vsb_pdf_image_data = ' + json.dumps(pages) + ';'
              'showVsbpdfIframe("/original.pdf","100%","600","0","",vsb_pdf_image_data);')
    body = ('<html><head><title>完整招生通知</title></head><body><nav>网站导航</nav>'
            '<div class="v_news_content"><p><script>' + script + '</script></p></div></body></html>').encode()
    url = 'https://college.nju.edu.cn/info/notice.htm'
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda requested: (body, 'text/html; charset=utf-8', url))
    document = acquisition.fetch_document(url)
    assert document['text'] == ''
    assert document['images'] == ['https://college.nju.edu.cn' + page for page in pages]
    assert base64.b64decode(document['raw_response_base64']) == body
    assert '/original.pdf' in document['raw_content']


@pytest.mark.parametrize('value', ['["/first.jpg"].concat(["/missing.jpg"])', 'loadPages()', '["/first.jpg", null]',
                                 '["/first.jpg"]; vsb_pdf_image_data = ["/first.jpg", "/second.jpg"]',
                                 '["/first.jpg"]; vsb_pdf_image_data.push("/second.jpg")'])
def test_pdf_viewer_never_executes_dynamic_or_partially_parsed_page_lists(monkeypatch, value):
    body = ('<html><body><article><script>var vsb_pdf_image_data = ' + value + ';'
            'showVsbpdfIframe("/original.pdf","100%","600","0","",vsb_pdf_image_data);'
            '</script></article></body></html>').encode()
    url = 'https://college.nju.edu.cn/info/notice.htm'
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda requested: (body, 'text/html', url))
    document = acquisition.fetch_document(url)
    assert document['capture_complete'] is False
    assert document['capture_errors'][0]['code'] == 'source_incomplete'
    assert document['images'] == []
    assert base64.b64decode(document['raw_response_base64']) == body


@pytest.mark.parametrize('html,code', [
    ('<h2>环境异常</h2><p>当前环境异常，完成验证后即可继续访问。</p>', 'source_blocked'),
    ('<div class="weui-msg"><h2>该内容已被发布者删除</h2></div>', 'source_blocked'),
    ('<h2>访问过于频繁</h2><p>请稍后再试</p>', 'source_blocked'),
    ('<html><body><h2>未知展示格式</h2><p>页面不含可核对的文章容器。</p></body></html>', 'source_unsupported_layout'),
    ('<div id="js_content">   </div>', 'source_empty'),
    ('<html><body></body></html>', 'source_empty'),
])
def test_wechat_failures_are_classified_without_discarding_the_response(monkeypatch, html, code):
    url = 'https://mp.weixin.qq.com/s?__biz=ABC%3D%3D&mid=1&idx=1&x=1&x=2'
    resolved = 'https://mp.weixin.qq.com/s/article-response'
    body = html.encode()
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda requested: (body, 'text/html; charset=utf-8', resolved))
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.fetch_document(url)
    assert caught.value.code == code
    response = caught.value.source_response
    assert response['canonical_url'] == url
    assert response['resolved_url'] == resolved
    assert response['http_status'] == 200
    assert response['content_type'] == 'text/html; charset=utf-8'
    assert response['raw_response_complete'] is True
    assert base64.b64decode(response['raw_response_base64']) == body
    assert response['raw_response_digest'] == hashlib.sha256(body).hexdigest()


def test_wechat_article_about_verification_is_not_itself_a_verification_gate(monkeypatch):
    body = ('<h1 id="activity-name">网络安全通知</h1><div id="js_content">'
            '如遇“环境异常”或“访问过于频繁”，请完成安全验证。'
            '这是本次网络安全讲座讨论的真实提示语，不是来源访问限制。</div>').encode()
    url = 'https://mp.weixin.qq.com/s/public-security-notice'
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda requested: (body, 'text/html', url))
    document = acquisition.fetch_document(url)
    assert '网络安全讲座' in document['text']
    assert document['capture_complete'] is True


def test_failed_source_diagnostics_remain_private_and_never_become_primary_evidence(monkeypatch):
    from nannanews import bulletin_date_repair as repair

    body = b'<html><body>Unsupported public article layout</body></html>'
    url = 'https://mp.weixin.qq.com/s/unknown-layout'
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda requested: (body, 'text/html', url))
    cache = {}
    sources, attempts, media, mismatched, source_media = repair._capture_sources({'source_urls': [url]}, cache)
    assert sources == [] and media == [] and mismatched is False
    assert source_media == []
    assert attempts == [{'url': url, 'status': 'unavailable', 'code': 'source_unsupported_layout',
                         'reason': cache[url]['error']['reason']}]
    response = cache[url]['error']['source_response']
    assert base64.b64decode(response['raw_response_base64']) == body
    assert 'source_response' not in attempts[0]

    def refetch_forbidden(requested):
        pytest.fail('Immutable failed source capture must not be fetched again.')

    monkeypatch.setattr(acquisition, 'fetch_public_resource', refetch_forbidden)
    assert repair._capture_sources({'source_urls': [url]}, cache) == (sources, attempts, media, mismatched, source_media)


@pytest.mark.parametrize('status,body_size', [(403, 80), (404, 80), (429, 80), (500, 80), (403, 600)])
def test_http_refusals_preserve_bounded_body_and_status_without_retry(monkeypatch, status, body_size):
    body = b'original refusal response ' + b'x' * body_size
    response = Mock()
    response.status = status
    headers = {'Content-Type': 'text/html; charset=utf-8', 'Content-Length': str(len(body))}
    response.getheader.side_effect = lambda key, default=None: headers.get(key, default)
    response.read1.side_effect = io.BytesIO(body).read
    connection = Mock()
    connection.sock = None
    connection.getresponse.return_value = response
    factory = Mock(return_value=connection)
    monkeypatch.setattr(acquisition, '_source_proxy', lambda parts: None)
    monkeypatch.setattr(acquisition, '_public_addresses', lambda *args: ['93.184.216.34'])
    monkeypatch.setattr(acquisition, '_PinnedHTTP', factory)
    url = 'http://article.example/public?x=1&x=2'
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.fetch_public_resource(url, max_bytes=512)
    assert caught.value.code == 'source_blocked'
    diagnostic = caught.value.source_response
    assert diagnostic['canonical_url'] == url and diagnostic['resolved_url'] == url
    assert diagnostic['http_status'] == status
    assert base64.b64decode(diagnostic['raw_response_base64']) == body[:512]
    assert diagnostic['raw_response_digest'] == hashlib.sha256(body[:512]).hexdigest()
    assert diagnostic['raw_response_complete'] is (len(body) <= 512)
    assert factory.call_count == 1
    connection.close.assert_called_once()
    response.close.assert_called_once()


def test_wechat_captcha_redirect_cannot_be_promoted_by_an_article_looking_container(monkeypatch):
    body = '<div id="js_content">完成验证后即可继续访问。</div>'.encode()
    original = 'https://mp.weixin.qq.com/s/public-identity?x=1&x=2'
    resolved = 'https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?target_url=article'
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda requested: (body, 'text/html', resolved))
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.fetch_document(original)
    assert caught.value.code == 'source_blocked'
    assert caught.value.source_response['canonical_url'] == original
    assert caught.value.source_response['resolved_url'] == resolved


@pytest.mark.parametrize('content_type,body', [('application/json', b'{}'),
                                             ('text/html; charset=missing-charset', b'original undecodable response')])
def test_non_article_and_unknown_encoding_failures_keep_original_response(monkeypatch, content_type, body):
    url = 'https://college.nju.edu.cn/public/notice'
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda requested: (body, content_type, url))
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.fetch_document(url)
    assert base64.b64decode(caught.value.source_response['raw_response_base64']) == body
    assert caught.value.source_response['content_type'] == content_type


def test_wechat_image_only_article_retains_order_and_parent_url_identity(monkeypatch):
    original = 'https://mp.weixin.qq.com/s?__biz=ABC%3D%3D&mid=3&idx=1&x=1&x=2'
    body = ('<h1 id="activity-name">海报通知</h1><img src="/site-navigation.jpg">'
            '<div id="js_content"><img data-src="//mmbiz.qpic.cn/page-one?wx_fmt=png">'
            '<img data-src="https://mmbiz.qpic.cn/page-two?wx_fmt=jpeg">'
            '<img src="https://mmbiz.qpic.cn/page-one?wx_fmt=png"></div>').encode()
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda requested: (body, 'text/html', original))
    document = acquisition.fetch_document(original)
    assert document['canonical_url'] == original
    assert document['text'] == ''
    assert document['images'] == ['https://mmbiz.qpic.cn/page-one?wx_fmt=png',
                                  'https://mmbiz.qpic.cn/page-two?wx_fmt=jpeg']
    assert base64.b64decode(document['raw_response_base64']) == body


def test_wechat_script_generated_layout_is_not_executed_or_replaced_by_body_text(monkeypatch):
    body = ('<html><body><h1>文章标题但没有公开正文</h1><script>'
            'document.write("<div id=js_content>动态正文</div>");'
            '</script></body></html>').encode()
    url = 'https://mp.weixin.qq.com/s/unsupported-script-layout'
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda requested: (body, 'text/html', url))
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.fetch_document(url)
    assert caught.value.code == 'source_unsupported_layout'
    assert base64.b64decode(caught.value.source_response['raw_response_base64']) == body


@pytest.mark.parametrize('status,attempts,code', [(200, 4, 'source_unavailable'), (403, 1, 'source_blocked')])
def test_interrupted_response_retains_observed_bytes_without_claiming_completeness(monkeypatch, status, attempts, code):
    connections = []
    for index in range(attempts):
        response = Mock()
        response.status = status
        headers = {'Content-Type': 'text/html', 'Content-Length': '100'}
        response.getheader.side_effect = lambda key, default=None: headers.get(key, default)
        response.read1.side_effect = [b'first bytes ', http.client.IncompleteRead(b'partial bytes', 100)]
        connection = Mock()
        connection.sock = None
        connection.getresponse.return_value = response
        connections.append(connection)
    factory = Mock(side_effect=connections)
    monkeypatch.setattr(acquisition, '_source_proxy', lambda parts: None)
    monkeypatch.setattr(acquisition, '_public_addresses', lambda *args: ['93.184.216.34'])
    monkeypatch.setattr(acquisition, '_PinnedHTTP', factory)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.fetch_public_resource('http://article.example/partial')
    assert caught.value.code == code
    diagnostic = caught.value.source_response
    assert base64.b64decode(diagnostic['raw_response_base64']) == b'first bytes partial bytes'
    assert diagnostic['http_status'] == status
    assert diagnostic['raw_response_complete'] is False
    assert factory.call_count == attempts


def test_webplus_embedded_pdf_refusal_is_incomplete_not_header_only_success(monkeypatch):
    from nannanews import bulletin_date_repair as repair

    # Minimized from the saved life.nju.edu.cn/a5/ac/c12967a828844/page.htm
    # response: the public body declares a PDF, not an image or page manifest.
    url = 'https://life.nju.edu.cn/a5/ac/c12967a828844/page.htm'
    pdf = '/_upload/article/files/c3/f1/dd3bb29d4585ae1db6ba740ef741/45f24b0a-b664-43df-b7e2-9ae0711fba36.pdf'
    body = ('<html><body><div class="infobox"><div class="article">'
            '<h1 class="arti_title">公开学术讲座</h1>'
            '<p class="arti_metas">发布者：刘姝伶 发布时间：2026-03-31 浏览次数：10</p>'
            '<div class="entry"><div class="read"><div class="wp_articlecontent"><p>&nbsp;'
            '<div class="wp_pdf_player" contenteditable="false" pdfsrc="' + pdf + '" '
            'sudyplayer="wp_pdf_player" swsrc="/do-not-execute.swf"></div>&nbsp;</p>'
            '</div></div></div></div></div></body></html>').encode()
    requested = []

    def transport(requested_url, *args):
        requested.append(requested_url)
        if requested_url == url:
            return body, 'text/html', url
        raise acquisition.AcquisitionError('Public PDF returned an actual access refusal.', 'source_blocked')

    monkeypatch.setattr(acquisition, 'fetch_public_resource', transport)
    cache = {}
    sources, attempts, media, mismatched, _ = repair._capture_sources({'source_urls': [url]}, cache)
    document = cache[url]['document']
    assert document['title'] == '公开学术讲座'
    assert document['text'] == ''
    assert document['images'] == []
    assert document['capture_complete'] is False
    assert document['capture_errors'][0]['code'] == 'source_blocked'
    assert document['capture_errors'][0]['url'] == 'https://life.nju.edu.cn' + pdf
    assert base64.b64decode(document['raw_response_base64']) == body
    assert sources[0]['url'] == url
    assert attempts[0]['status'] == 'media_unavailable'
    assert media == [] and mismatched is False
    assert requested == [url, 'https://life.nju.edu.cn' + pdf]


def test_webplus_nested_article_body_excludes_outer_chrome_without_losing_poster(monkeypatch):
    url = 'https://college.nju.edu.cn/notices/public-poster.htm'
    body = ('<html><body><div class="infobox"><div class="article">'
            '<h1>公开讲座</h1><p>浏览次数：999 发布者：网站管理员</p><img src="/chrome.png">'
            '<div class="wp_articlecontent"><p>本次讲座的时间、地点和报告人见以下完整公开海报。</p>'
            '<img src="/article/poster.png"></div></div></div></body></html>').encode()
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda requested: (body, 'text/html', url))
    document = acquisition.fetch_document(url)
    assert document['title'] == '公开讲座'
    assert '浏览次数' not in document['text'] and '网站管理员' not in document['text']
    assert document['images'] == ['https://college.nju.edu.cn/article/poster.png']
    assert document['capture_complete'] is True


@pytest.mark.parametrize('status,declared', [(200, '100'), (403, '100'), (403, ''), (403, '-1'), (403, '9' * 5000)])
def test_eof_is_not_claimed_complete_when_declared_source_response_framing_is_invalid(monkeypatch, status, declared):
    body = b'short original body'
    response = Mock()
    response.status = status
    headers = {'Content-Type': 'text/html', 'Content-Length': declared}
    response.getheader.side_effect = lambda name, default=None: headers.get(name, default)
    response.read1.side_effect = io.BytesIO(body).read
    connection = Mock()
    connection.sock = None
    connection.getresponse.return_value = response
    monkeypatch.setattr(acquisition, '_source_proxy', lambda parts: None)
    monkeypatch.setattr(acquisition, '_public_addresses', lambda *args: ['93.184.216.34'])
    monkeypatch.setattr(acquisition, '_PinnedHTTP', lambda *args: connection)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.fetch_public_resource('http://article.example.org/original')
    assert caught.value.code == ('source_incomplete' if status == 200 else 'source_blocked')
    assert caught.value.source_response['http_status'] == status
    assert caught.value.source_response['raw_response_complete'] is False
    assert base64.b64decode(caught.value.source_response['raw_response_base64']) == body
    connection.close.assert_called_once()
