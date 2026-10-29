"""Bounded regressions for retained WebPlus sibling metadata/poster recovery."""
import base64
import hashlib

import pytest

from nannanews import acquisition


SOURCE_URL = 'https://njusz.nju.edu.cn/23/97/c58578a795543/page.htm'
POSTER_PATH = '/_upload/article/images/21/e3/19623c4247adb37adb7e3018417a/9732c198-8c66-46cd-b16d-d07d75dc989d.jpg'
TITLE = '翻译的世界文学：从中国走向世界'
# Minimized from retained response SHA-256
# f03467245ca0ea063e8bc89a30e3e6695819249bc227c3e8527f5fc53d7a7401.
# Keep the actual article/title/detailbox/read1/body sibling relationships.
ARTICLE = f'''<div class="article" frag="窗口3" portletmode="simpleArticleAttri">
<h1 class="arti_title">{TITLE}</h1>
<div class="entry clearfix">
<div class="ml">
<div class="detailbox"><div class="tt">时间</div><div class="con">2025-10-31 14:00:00</div></div>
<div class="detailbox"><div class="tt">地点</div><div class="con">南雍楼东218</div></div>
<div class="detailbox"><div class="tt">主题</div><div class="con"></div></div>
<div class="detailbox"><div class="tt">类别</div><div class="con"></div></div>
<div class="detailbox"><div class="tt">主办</div><div class="con"></div></div>
</div>
<div class="mr">
<div class="read1" type=""><img src="{POSTER_PATH}"></div>
<div class="read"><div class="wp_articlecontent">
<p>主讲人：西奥·德汉(Theo D'hacn)</p><p>主讲人简介：比利时鲁汶大学英语与美国文学教授、比较文学学者。</p>
</div></div>
</div>
</div>
</div>'''


def _reparse(monkeypatch, html, url=SOURCE_URL):
    def forbid_fetch(*args, **kwargs):
        pytest.fail('Immutable source reparse must not fetch any resource.')

    monkeypatch.setattr(acquisition, 'fetch_public_resource', forbid_fetch)
    body = html.encode('utf-8')
    capture = {
        'canonical_url': url,
        'resolved_url': url,
        'content_type': 'text/html; charset=utf-8',
        'fetched_at': '2026-10-22T00:00:00+00:00',
        'raw_response_base64': base64.b64encode(body).decode('ascii'),
        'raw_response_digest': hashlib.sha256(body).hexdigest(),
    }
    before = capture.copy()
    document = acquisition.reparse_source_document(capture)
    assert capture == before
    assert base64.b64decode(document['raw_response_base64']) == body
    assert document['raw_response_digest'] == capture['raw_response_digest']
    assert document['raw_content'] == html
    assert document['fetched_at'] == capture['fetched_at']
    return document


@pytest.mark.parametrize('infobox', [True, False])
def test_whole_structured_article_retains_title_time_venue_and_sibling_poster(monkeypatch, infobox):
    # The second case starts from .wp_articlecontent rather than .infobox .article
    # and uses a different host/path: this is a structural, not source-URL rule.
    url = SOURCE_URL if infobox else 'https://college.nju.edu.cn/notices/another.htm'
    navigation = ''.join(f'<img src="/nav-{index}.png">' for index in range(acquisition.MAX_IMAGES + 2))
    neighbor = ('<div class="article"><h1>相邻文章</h1><p>相邻活动不属于本场讲座。</p>'
                '<img src="/neighbor.jpg"><a href="/neighbor.pdf">相邻附件</a></div>')
    article_group = ARTICLE + neighbor
    if infobox:
        article_group = '<div class="infobox">' + article_group + '</div>'
    html = ('<html><head><title>网站标题</title></head><body><nav>网站导航' + navigation
            + '</nav>' + article_group + '<footer>网站页脚<img src="/footer.jpg"></footer></body></html>')
    document = _reparse(monkeypatch, html.replace('\n', '\r\n'), url)
    assert document['title'] == TITLE
    for value in (TITLE, '时间', '2025-10-31 14:00:00', '地点', '南雍楼东218', '主讲人简介'):
        assert value in document['text']
    for value in ('网站标题', '网站导航', '网站页脚', '相邻文章', '相邻活动', '相邻附件'):
        assert value not in document['text']
    origin = 'https://njusz.nju.edu.cn' if infobox else 'https://college.nju.edu.cn'
    assert document['images'] == [origin + POSTER_PATH]
    assert document['pdf_sources'] == []
    assert document['capture_complete'] is True
    # The event time is retained verbatim, not invented as a publication date.
    assert document['raw_date_text'] is None
    assert document['source_published_date'] is None


def test_structured_article_still_excludes_semantic_navigation_and_footer(monkeypatch):
    html = ARTICLE.replace('<div class="entry clearfix">',
                           '<nav>内部导航<img src="/nav.jpg"></nav>'
                           '<footer>内部页脚<img src="/footer.jpg"></footer><div class="entry clearfix">')
    document = _reparse(monkeypatch, html)
    assert '2025-10-31 14:00:00' in document['text']
    assert '内部导航' not in document['text'] and '内部页脚' not in document['text']
    assert document['images'] == ['https://njusz.nju.edu.cn' + POSTER_PATH]


def test_standard_webplus_body_still_excludes_publisher_counter_and_chrome(monkeypatch):
    html = ('<html><body><div class="infobox"><div class="article"><h1>普通公开讲座</h1>'
            '<p>浏览次数：999 发布者：网站管理员</p><img src="/chrome.jpg">'
            '<div class="wp_articlecontent"><p>本次讲座的时间、地点和报告人见以下完整公开海报。</p>'
            '<img src="/article/poster.jpg"></div></div></div></body></html>')
    document = _reparse(monkeypatch, html)
    assert document['title'] == '普通公开讲座'
    assert '浏览次数' not in document['text'] and '网站管理员' not in document['text']
    assert document['images'] == ['https://njusz.nju.edu.cn/article/poster.jpg']


def test_standalone_webplus_body_keeps_existing_content_boundary(monkeypatch):
    html = ('<html><body><div class="navigation">导航<img src="/chrome.jpg"></div>'
            '<div class="wp_articlecontent"><h1>独立正文</h1><p>完整讲座时间和地点请见以下公开海报。</p>'
            '<img src="/article/poster.jpg"></div><footer>页脚</footer></body></html>')
    document = _reparse(monkeypatch, html)
    assert document['title'] == '独立正文'
    assert '导航' not in document['text'] and '页脚' not in document['text']
    assert document['images'] == ['https://njusz.nju.edu.cn/article/poster.jpg']


def test_multiple_rich_text_bodies_do_not_promote_an_ambiguous_wrapper(monkeypatch):
    html = ARTICLE.replace('</h1>', '</h1><div class="wp_articlecontent">'
                           '<p>第一正文的完整讲座信息和公开海报。</p><img src="/first.jpg"></div>', 1)
    document = _reparse(monkeypatch, html)
    assert '第一正文' in document['text']
    assert '南雍楼东218' not in document['text'] and '主讲人简介' not in document['text']
    assert document['images'] == ['https://njusz.nju.edu.cn/first.jpg']


def test_labels_inside_rich_text_do_not_admit_unrelated_outer_images(monkeypatch):
    html = ('<div class="infobox"><div class="article"><h1>普通公开讲座</h1><img src="/chrome.jpg">'
            '<div class="wp_articlecontent"><div class="detailbox"><div class="tt">时间</div>'
            '<div class="con">2025-10-31 14:00:00</div></div><img src="/article/poster.jpg">'
            '</div></div></div>')
    document = _reparse(monkeypatch, html)
    assert '2025-10-31 14:00:00' in document['text']
    assert document['images'] == ['https://njusz.nju.edu.cn/article/poster.jpg']


def test_nested_neighbor_article_does_not_promote_the_outer_wrapper(monkeypatch):
    html = ARTICLE.replace('<div class="entry clearfix">',
                           '<div class="article"><h1>嵌套相邻文章</h1><img src="/neighbor.jpg"></div>'
                           '<div class="entry clearfix">')
    document = _reparse(monkeypatch, html)
    assert '嵌套相邻文章' not in document['text']
    assert document['images'] == []
    assert '主讲人简介' in document['text']


def test_recovered_sibling_poster_still_rejects_unsafe_resource_urls(monkeypatch):
    html = ARTICLE.replace(POSTER_PATH, 'javascript:alert(1)')
    with pytest.raises(acquisition.AcquisitionError) as caught:
        _reparse(monkeypatch, html)
    assert caught.value.code == 'invalid_url'
