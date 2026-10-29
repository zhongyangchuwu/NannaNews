"""Literal offline adapter behavior; no archived/private input or network required."""
import hashlib
from pathlib import Path


import pytest


from nannanews.html_bulletin_adapter import HtmlAdapterError, parse_html


EARLY = '''<!doctype html><html><head><title>南哪消息 2024-09-24</title></head><body>
<header><h1 class="title">南哪消息 2024-09-24</h1></header>
<nav id="TOC"><h2>重复目录</h2><a href="#history">历史学院培训</a></nav>
<h1 id="activities">活动预告</h1>
<h2>编辑部招聘人才</h2><p>想订阅消息可加QQ群：123456。</p>
<h2 id="deadline">Deadline Ongoing</h2>
<table><thead><tr><th>消息</th><th>截止日期</th><th>刊载日期</th></tr></thead>
<tbody><tr><td>历史学院培训</td><td>9.28</td><td>9.24</td></tr></tbody></table>
<h2 id="history">历史学院宣传技能培训</h2>
<p>面向全体历史学院学生。<br>活动时间：2024年9月28日 14:00<br>
原文：<a href="https://mp.weixin.qq.com/s/training?x=1&amp;x=2">查看原文</a></p>
</body></html>'''


def save(tmp_path, html, name='2020-01-01.html'):
    path = tmp_path / name
    path.write_text(html, encoding='utf-8')
    return path


def test_pandoc_header_raw_rows_details_and_boilerplate(tmp_path):
    path = save(tmp_path, EARLY)
    parsed = parse_html(path)
    assert parsed['source_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert parsed['issue_date'] == '2024-09-24'
    assert parsed['issue_date_evidence'] == '南哪消息 2024-09-24'
    assert parsed['format'] == 'html'
    assert parsed['schema_version'] == 1
    assert 'QQ群：123456' in parsed['text']  # evidence, not an activity
    assert len(parsed['items']) == 2
    summary, detail = parsed['items']
    assert summary['title'] == '历史学院培训'
    assert summary['locator']['headers'] == ['消息', '截止日期', '刊载日期']
    assert summary['locator']['cells'] == ['历史学院培训', '9.28', '9.24']
    assert '<td>9.28</td>' in summary['locator']['raw_html']
    assert [(d['kind'], d['raw_text'], d['normalized_date'], d['resolution']) for d in summary['dates']] == [
        ('deadline', '9.28', None, 'year_unknown'), ('listed', '9.24', None, 'year_unknown')]
    assert detail['kind'] == 'detail'
    assert detail['locator']['id'] == 'history'
    assert detail['dates'][0]['kind'] == 'event'
    assert detail['dates'][0]['normalized_date'] == '2024-09-28'
    assert detail['source_urls'] == ['https://mp.weixin.qq.com/s/training?x=1&x=2']
    assert any(c['name'] == '历史学院' and c['relation'] == 'audience' and '面向' in c['evidence'] for c in detail['college_mentions'])
    assert all(c['relation'] != 'publisher' for c in detail['college_mentions'])


def test_late_iso_tables_do_not_relabel_lecture_deadline(tmp_path):
    html = '''<html><body><h1>南哪消息 2026-09-13</h1>
    <div class="toc"><h2>目录</h2><a href="#section-1">讲座</a></div>
    <div class="section"><h2 id="section-1">讲座</h2>
    <table><thead><tr><th>活动标题</th><th>截止时间</th><th>刊载时间</th></tr></thead><tbody>
    <tr><td><a href="https://mp.weixin.qq.com/s/post">讲座甲</a></td><td>2026-10-14</td><td>2026-09-13</td></tr>
    <tr><td><a href="None">讲座乙</a></td><td>10月15日</td><td>2026-09-13</td></tr>
    </tbody></table></div><p>南哪小报编辑部</p></body></html>'''
    parsed = parse_html(save(tmp_path, html), source_url='https://nik-nul.github.io/news/2026-09-13.html')
    assert len(parsed['items']) == 2
    first, second = parsed['items']
    assert first['category'] == '讲座'
    assert [d['kind'] for d in first['dates']] == ['deadline', 'listed']
    assert first['dates'][0]['normalized_date'] == '2026-10-14'
    assert second['dates'][0]['normalized_date'] is None
    assert second['source_urls'] == []
    assert all('summary' not in item and 'publisher_college' not in item for item in parsed['items'])


def test_late_li_details_keep_dates_range_and_internal_target(tmp_path):
    html = '''<html><body><h1>南哪消息 2026-10-13</h1><div class="section"><h2>校级活动</h2>
    <table><tr><th>活动标题</th><th>截止时间</th></tr>
    <tr><td><a href="#a">活动甲</a></td><td>2026-10-15</td></tr></table>
    <ul><li><h3 id="a">活动甲</h3>活动对象：外国语学院同学<br>
    活动时间：2026年10月15日—10月16日<br>原文：<a href="https://example.edu.cn/notice/1">详情</a></li>
    <li><h3 id="b">活动乙</h3>时间：10月14日<br>地点：校园</li></ul></div></body></html>'''
    parsed = parse_html(save(tmp_path, html))
    assert len(parsed['items']) == 3
    row, detail, other = parsed['items']
    assert row['locator']['internal_targets'] == ['a']
    assert detail['category'] == '校级活动'
    assert detail['dates'][0]['resolution'] == 'ambiguous'
    assert detail['dates'][0]['normalized_date'] is None
    assert '活动乙' not in detail['body_text']
    assert other['dates'][0]['resolution'] == 'year_unknown'


def test_article_h2_does_not_become_later_article_category(tmp_path):
    html = '''<html><body><h1>南哪消息 2025-03-25</h1>
    <h2>院级活动</h2><table><tr><th>活动</th></tr><tr><td>活动甲</td></tr></table>
    <h3>活动甲</h3><p>活动的原文正文。</p>
    <h2>2025年4月普通话水平测试网络报名通知</h2><p>这是具体报名通知的正文。</p>
    <h3>另一条消息</h3><p>这条消息不能继承上一条通知的标题。</p>
    <h2>校庆相关活动</h2><h3>校庆讲座</h3><p>来自明确栏目下的消息。</p>
    </body></html>'''
    parsed = parse_html(save(tmp_path, html))
    details = {item['title']: item for item in parsed['items'] if item['kind'] == 'detail'}
    assert details['活动甲']['category'] == '院级活动'
    assert details['另一条消息']['category'] is None
    assert details['另一条消息']['body_text'] == '这条消息不能继承上一条通知的标题。'
    assert details['校庆讲座']['category'] == '校庆相关活动'
    assert '2025年4月普通话水平测试网络报名通知' in details


def test_nested_article_table_uses_section_not_article_title(tmp_path):
    html = '''<html><body><h1>南哪消息 2026-10-13</h1>
    <h2>校级活动</h2><h3>具体活动标题</h3><p>活动说明。</p>
    <table><tr><th>活动</th></tr><tr><td>表内消息</td></tr></table>
    </body></html>'''
    parsed = parse_html(save(tmp_path, html))
    assert [(item['title'], item['category']) for item in parsed['items'] if item['kind'] == 'summary'] == [
        ('表内消息', '校级活动')]


def test_marked_tabular_section_keeps_its_category(tmp_path):
    html = '''<html><body><h1>南哪消息 2025-04-27</h1><h2>院级活动</h2>
    <div class="tabular"><p>活动 &amp; 开展时间<br>活动甲 &amp; 4.30</p></div>
    <h3>活动甲</h3><p>该栏目下的原文正文。</p></body></html>'''
    parsed = parse_html(save(tmp_path, html))
    detail = next(item for item in parsed['items'] if item['title'] == '活动甲')
    assert detail['category'] == '院级活动'


def test_missing_issue_year_never_uses_filename(tmp_path):
    parsed = parse_html(save(tmp_path, '<html><body><h1>南哪消息 10月13日</h1><h2>说明</h2><p>实际文字</p></body></html>', '2026-10-13.html'))
    assert parsed['issue_date'] is None
    assert any(w['code'] == 'issue_date_unknown' for w in parsed['warnings'])


def test_local_assets_hashes_and_external_missing_refs_are_offline(tmp_path):
    images = tmp_path / 'imgs'
    images.mkdir()
    image = images / 'poster.png'
    image.write_bytes(b'original poster bytes')
    html = '''<html><body><h1>南哪消息 2026-10-13</h1><h2>海报</h2><p>原文海报
    <img src="imgs/poster.png"><img src="https://example.edu.cn/a.png">
    <img src="../outside.png"><img src="data:image/png;base64,YQ=="></p></body></html>'''
    parsed = parse_html(save(tmp_path, html))
    assert len(parsed['assets']) == 1
    assert parsed['assets'][0]['path'] == 'imgs/poster.png'
    assert parsed['assets'][0]['sha256'] == hashlib.sha256(image.read_bytes()).hexdigest()
    assert len([w for w in parsed['warnings'] if w['code'] == 'image_not_acquired']) == 3
    assert 'data:image/png;base64,YQ==' in parsed['items'][0]['locator']['raw_html']


@pytest.mark.parametrize('contents', [b'not a bulletin', b'<html><body><h1>bad\xff</h1></body></html>'])
def test_malformed_inputs_fail_without_fake_parse(tmp_path, contents):
    path = tmp_path / 'bad.html'
    path.write_bytes(contents)
    with pytest.raises(HtmlAdapterError):
        parse_html(path)


def test_symlink_asset_is_not_acquired(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + '-outside.png')
    outside.write_bytes(b'outside')
    (tmp_path / 'poster.png').symlink_to(outside)
    parsed = parse_html(save(tmp_path, '<html><body><h1>南哪消息 2026-10-13</h1><h2>海报</h2><p><img src="poster.png">原文</p></body></html>'))
    assert parsed['assets'] == []
    assert any(w['code'] == 'image_missing' for w in parsed['warnings'])


@pytest.mark.parametrize('section', [
    '<h2>接种通知</h2><p>详情：https://hospital.nju.edu.cn//ggtz/20241114/i301674.html。</p>',
    '<h2>https://hospital.nju.edu.cn//ggtz/20241114/i301674.html</h2><p>接种安排见标题网址。</p>',
    '<h2><a href="https://hospital.nju.edu.cn//ggtz/20241114/i301674.html">接种通知</a></h2><p>接种安排。</p>',
])
def test_detail_retains_literal_source_urls_in_body_and_heading(tmp_path, section):
    parsed = parse_html(save(tmp_path, '<html><body><h1>南哪消息 2026-10-13</h1>' + section + '</body></html>'))
    detail = next(item for item in parsed['items'] if item['kind'] == 'detail')
    assert detail['source_urls'] == ['https://hospital.nju.edu.cn//ggtz/20241114/i301674.html']


def test_literal_table_url_keeps_query_and_balanced_path_punctuation(tmp_path):
    html = '''<html><body><h1>南哪消息 2026-10-13</h1><h2>活动列表</h2>
    <table><tr><th>活动标题</th><th>说明</th></tr><tr><td>技术分享</td>
    <td>报名（https://example.edu.cn/archive/(2026)/call?q=1&amp;tag=2）。</td></tr></table></body></html>'''
    parsed = parse_html(save(tmp_path, html))
    summary = next(item for item in parsed['items'] if item['kind'] == 'summary')
    assert summary['source_urls'] == ['https://example.edu.cn/archive/(2026)/call?q=1&tag=2']


def test_literal_links_do_not_use_script_text_anchor_labels_or_adjacent_activity(tmp_path):
    html = '''<html><body><h1>南哪消息 2026-10-13</h1><h2>活动甲</h2>
    <p>原文：<a href="https://example.edu.cn/actual">https://display.invalid/short...</a>
    <script>const link = "https://script.invalid/helper";</script>
    <style>.notice { background: url(https://style.invalid/helper); }</style>
    同一原文 https://example.edu.cn/actual。</p>
    <h2>活动乙</h2><p>原文 https://example.edu.cn/other。</p></body></html>'''
    parsed = parse_html(save(tmp_path, html))
    details = {item['title']: item for item in parsed['items'] if item['kind'] == 'detail'}
    assert details['活动甲']['source_urls'] == ['https://example.edu.cn/actual']
    assert details['活动乙']['source_urls'] == ['https://example.edu.cn/other']
