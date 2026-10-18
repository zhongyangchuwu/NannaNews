"""Consumer regressions using small 2.14.4-shaped tagged extraction evidence.

Only the OS process boundary is substituted. Normalization, artifact handling,
public parse_pdf errors and preserved evidence all use production code. The real
supplied PDFs and actual local CLI are exercised by the integrated import owner.
"""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from nannanews import pdf_adapter


PDF_BYTES = b'%PDF-2.0\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n'
SOURCE_URI = 'https://mp.weixin.qq.com/s/complete-original-source?lang=zh_CN'


def node(kind, mcids=(), children=(), **fields):
    return {'type': kind, 'marked_content_ids': list(mcids),
            'children': list(children), 'annotations': [], **fields}


def fragment(mcid, text, x=20, y=20, width=160, height=12):
    return {'mcid': mcid, 'text': text, 'x': x, 'y': y, 'width': width,
            'height': height, 'font_size': height}


def annotation(uri=SOURCE_URI, x=20, y=120, width=160):
    return {'subtype': 'link', 'uri': uri, 'contents': uri,
            'rect': {'x': x, 'y': y, 'width': width, 'height': 12}}


def cell(kind, mcid):
    # Direct TH/TD MCIDs are empty, just like the real tagged newspaper.
    return node(kind, children=[node('Span', [mcid])])


def table(base, titles=('第一场讲座', '第二场讲座')):
    head = node('TR', children=[cell('TH', base + i) for i in range(3)])
    rows = [node('TR', children=[cell('TD', base + 3 + i * 3 + j) for j in range(3)])
            for i in range(len(titles))]
    tags = node('Table', children=[node('THead', children=[head]), node('TBody', children=rows)])
    texts = [fragment(base + i, text, x=20 + i * 180, y=60)
             for i, text in enumerate(('活动', '截止时间', '刊载时间'))]
    for i, title in enumerate(titles):
        texts += [fragment(base + 3 + i * 3, title, y=80 + i * 20),
                  fragment(base + 4 + i * 3, '10-14', x=200, y=80 + i * 20),
                  fragment(base + 5 + i * 3, '10-13', x=380, y=80 + i * 20)]
    return tags, texts


def page(number, tags, texts, *, raw_text=None, annotations=()):
    return {'page': number, 'width': 800, 'height': 1000,
            'text': raw_text if raw_text is not None else '\n'.join(t['text'] for t in texts),
            'text_items': list(texts), 'annotations': list(annotations),
            'structure_tree': {'roots': [node('Document', children=tags)]},
            # Deliberately wrong heuristic blocks must never determine items.
            'blocks': [{'type': 'table', 'text': 'FAKE CROSS-COLUMN ACTIVITY'}]}


def header():
    return [node('P', [0]), node('Div', children=[node('Em', [1])])], [
        fragment(0, '南哪大专醒前消息', y=0), fragment(1, '2026-10-13 No. 451', y=20)]


@pytest.fixture
def source(tmp_path):
    path = tmp_path / '南哪消息2099.01.01.pdf'
    path.write_bytes(PDF_BYTES)
    return path


@pytest.fixture
def local_parser(monkeypatch):
    """A deterministic pipe-backed OS boundary, not a fake normalized adapter."""
    state = {'payload': None, 'assets': {}, 'version': 'lit 2.14.4\n',
             'returncode': 0, 'diagnostic': '', 'raw_json': None, 'commands': []}
    monkeypatch.setattr(pdf_adapter.shutil, 'which', lambda binary: '/private/bin/lit')
    monkeypatch.setattr(pdf_adapter.os, 'killpg', lambda *args: None)

    def launch(command, **options):
        state['commands'].append(command)
        version = '--version' in command
        if not version:
            root = Path(options['cwd'])
            for relative, content in state['assets'].items():
                asset = root / relative
                asset.parent.mkdir(parents=True, exist_ok=True)
                asset.write_bytes(content)
            output = Path(command[command.index('-o') + 1])
            if state['raw_json'] is not None:
                output.write_text(state['raw_json'], encoding='utf-8')
            else:
                output.write_text(json.dumps(state['payload'], ensure_ascii=False), encoding='utf-8')
        read_fd, write_fd = os.pipe()
        os.write(write_fd, (state['version'] if version else state['diagnostic']).encode())
        os.close(write_fd)
        code = 0 if version else state['returncode']
        return SimpleNamespace(stdout=os.fdopen(read_fd, 'rb'), pid=123456789,
                               returncode=code, wait=lambda **kwargs: code, poll=lambda: code)

    monkeypatch.setattr(pdf_adapter.subprocess, 'Popen', launch)
    return state


def parse(source, tmp_path, state, pages, images=()):
    state['payload'] = {'total_pages': len(pages), 'pages': pages, 'images': list(images)}
    return pdf_adapter.parse_pdf(source, tmp_path / 'work', parser_binary='configured-lit')


def test_explicit_activity_audience_keeps_distinct_title_and_body_evidence(source, tmp_path, local_parser):
    tags, texts = header()
    title = '健雄书院2026级班徽设计大赛'
    audience = '活动对象：健雄书院各班级'
    tags += [node('H1', [2], title='院级活动'), node('H2', [3], title=title), node('P', [4])]
    texts += [fragment(2, '院级活动', y=40), fragment(3, title, y=60), fragment(4, audience, y=80)]
    result = parse(source, tmp_path, local_parser, [page(1, tags, texts)])
    mentions = [mention for mention in result['items'][0]['college_mentions'] if mention['name'] == '健雄书院']
    assert {(mention['relation'], mention['evidence']) for mention in mentions} == {
        ('mentioned', title), ('audience', audience)}
    assert all(mention['locator']['page'] == 1 for mention in mentions)


def test_tagged_summary_tables_never_mix_newspaper_columns(source, tmp_path, local_parser):
    tags, texts = header()
    left, left_texts = table(10, ('左列活动一', '左列活动二'))
    right, right_texts = table(30, ('右列活动一', '右列活动二'))
    # Physical extraction alternates columns; tagged order is the authority.
    texts += [t for pair in zip(left_texts, right_texts) for t in pair]
    tags += [node('H1', [2], title='活动清单'), node('Div', children=[left]), node('Div', children=[right])]
    texts += [fragment(2, '2. 活动清单', y=40)]
    result = parse(source, tmp_path, local_parser, [page(1, tags, texts)])
    assert [item['title'] for item in result['items']] == ['左列活动一', '左列活动二', '右列活动一', '右列活动二']
    assert all(item['kind'] == 'summary' for item in result['items'])
    assert 'FAKE CROSS-COLUMN' not in result['text']
    assert result['issue_date'] == '2026-10-13'
    assert result['issue_number'] == 451
    assert result['source_filename'] == source.name
    assert result['source_sha256'] == hashlib.sha256(PDF_BYTES).hexdigest()
    assert (tmp_path / 'work' / result['source_original_path']).read_bytes() == PDF_BYTES
    assert json.loads((tmp_path / 'work' / result['raw_artifact_path']).read_text()) == local_parser['payload']
    assert result['parser'] == {'name': 'liteparse', 'version': '2.14.4'}
    for item in result['items']:
        assert [(field['kind'], field['raw_text'], field['resolution'], field['normalized_date']) for field in item['dates']] == [
            ('deadline', '10-14', 'year_unknown', None), ('listed', '10-13', 'year_unknown', None)]
        assert item['locator']['mcids']
        assert item['locator']['cells'][1]['header'] == '截止时间'


def test_merged_header_mcid_retains_literal_deadline_and_listed_roles(source, tmp_path, local_parser):
    tags, texts = header()
    tagged_table, data = table(10, ('讲座一',))
    data[1]['text'] = '截止时间 刊载时间'
    data = [t for t in data if t['mcid'] != 12]
    result = parse(source, tmp_path, local_parser, [page(1, tags + [tagged_table], texts + data)])
    assert [field['kind'] for field in result['items'][0]['dates']] == ['deadline', 'listed']
    assert result['items'][0]['dates'][1]['raw_text'] == '10-13'


def test_multiline_heading_full_annotation_uri_and_distinct_same_uri_lectures(source, tmp_path, local_parser):
    tags, texts = header()
    tags += [node('H1', [2], title='讲座'),
             node('H2', [3, 4], title='Structural Stability of Supersonic Spiral Flows'),
             node('P', children=[node('Span', [5]), node('Link', [6, 7], annotations=[annotation()])]),
             node('H2', [8], title='第二场独立讲座'),
             node('P', [9], annotations=[annotation()])]
    texts += [fragment(2, '6. 讲座', y=40),
              fragment(3, '6.2. Structural Stability of', y=60),
              fragment(4, 'Supersonic Spiral Flows', y=80),
              fragment(5, '时间：10 月 14 日 13:30\n地点：南雍楼西122\n主讲人：南京大学商学院教授\n面向计算机学院学生', y=100),
              fragment(6, 'https://mp.weixin.qq.com/s/comple', y=120),
              fragment(7, 'te-original-source?lang=zh_CN', y=140),
              fragment(8, '6.3. 第二场独立讲座', y=160),
              fragment(9, '时间：2026年10月15日 15:00\n地点：教202', y=180)]
    result = parse(source, tmp_path, local_parser, [page(1, tags, texts)])
    assert len(result['items']) == 2
    first, second = result['items']
    assert first['title'] == 'Structural Stability of Supersonic Spiral Flows'
    assert first['locator']['mcids'] == [3, 4]
    assert '南雍楼西122' in first['body_text'] and '13:30' in first['body_text']
    assert first['source_urls'] == second['source_urls'] == [SOURCE_URI]
    assert first['local_key'] != second['local_key']
    assert first['dates'][0]['normalized_date'] is None
    assert first['dates'][0]['resolution'] == 'year_unknown'
    assert second['dates'][0]['normalized_date'] == '2026-10-15'
    assert {(m['name'], m['relation']) for m in first['college_mentions']} == {('商学院', 'mentioned'), ('计算机学院', 'audience')}
    assert all(m['evidence'] in first['body_text'] for m in first['college_mentions'])
    assert first['annotations'][0]['rect']['y'] == 120


def test_page_specific_mcid_reset_and_caption_image_reference(source, tmp_path, local_parser):
    tags, texts = header()
    tags += [node('H1', [2], title='讲座'), node('H2', [3], title='第一场'),
             node('P', [4]), node('P', [5])]
    texts += [fragment(2, '6. 讲座', y=40), fragment(3, '6.7. 第一场', y=60),
              fragment(4, '时间：10月20日14:00\n地点：安中楼1412', y=80),
              fragment(5, '图片见 PDF 文档第二页 Figure 1', y=100)]
    poster = {'id': 'p2_1', 'path': 'assets/poster.png', 'page': 2,
              'bbox': {'x': 20, 'y': 100, 'width': 160, 'height': 200},
              'width': 320, 'height': 400, 'format': 'png'}
    local_parser['assets'] = {'assets/poster.png': b'preserved-poster-pixels'}
    second = page(2, [node('P', [0]), node('Div', children=[node('Caption', children=[node('Span', [2])]), node('Figure', [1])])],
                  [fragment(0, '图片附页', y=20), fragment(2, 'Figure 1: 第一场', y=310)])
    result = parse(source, tmp_path, local_parser, [page(1, tags, texts), second], [poster])
    assert len(result['items']) == 1
    item = result['items'][0]
    assert item['image_refs'] == ['p2_1']
    assert '图片附页' not in item['body_text']
    assert '南哪大专醒前消息' not in result['pages'][1]['text']
    asset = result['assets'][0]
    assert asset['path'] == 'assets/poster.png'
    assert asset['page'] == 2 and asset['bbox'] == poster['bbox']
    assert asset['width'] == 320 and asset['height'] == 400
    assert asset['caption_text'] == 'Figure 1: 第一场'
    assert asset['caption_locator']['mcids'] == [2]
    assert asset['sha256'] == hashlib.sha256(b'preserved-poster-pixels').hexdigest()
    assert 'no_ocr' in {w['code'] for w in result['warnings']}


def test_page_mcid_reset_keeps_two_body_sections_distinct(source, tmp_path, local_parser):
    tags, texts = header()
    first = page(1, tags + [node('H1', [2], title='第一页活动'), node('P', [3])],
                 texts + [fragment(2, '3. 第一页活动'), fragment(3, '时间：10月14日\n地点：甲楼')])
    second = page(2, [node('H1', [2], title='第二页活动'), node('P', [3])],
                  [fragment(2, '4. 第二页活动'), fragment(3, '时间：10月15日\n地点：乙楼')])
    result = parse(source, tmp_path, local_parser, [first, second])
    assert [item['title'] for item in result['items']] == ['第一页活动', '第二页活动']
    assert '甲楼' in result['items'][0]['body_text'] and '乙楼' not in result['items'][0]['body_text']
    assert '乙楼' in result['items'][1]['body_text'] and '甲楼' not in result['items'][1]['body_text']
    assert [item['locator']['page'] for item in result['items']] == [1, 2]


def test_subscription_contacts_are_evidence_not_activities(source, tmp_path, local_parser):
    tags, texts = header()
    tags += [node('H1', [2], title='订阅方式和加入编辑部'), node('P', [3]),
             node('H1', [4], title='真正活动'), node('P', [5])]
    texts += [fragment(2, '1. 订阅方式和加入编辑部'), fragment(3, 'QQ群：1014708350\n联系邮箱：editor@example.org'),
              fragment(4, '3. 真正活动'), fragment(5, '活动时间：10月14日\n活动地点：仙林校区')]
    result = parse(source, tmp_path, local_parser, [page(1, tags, texts)])
    assert [item['title'] for item in result['items']] == ['真正活动']
    assert '1014708350' in result['text'] and 'editor@example.org' in result['text']
    assert '1014708350' not in result['items'][0]['body_text']


def test_unsupported_layout_preserves_all_raw_text_without_fake_events(source, tmp_path, local_parser):
    raw = 'Unknown four-column layout\n时间：2026-10-14 13:30\n地点：报告厅\n' + ('原文证据' * 20000)
    unsupported = page(1, [], [], raw_text=raw)
    unsupported['structure_tree'] = None
    result = parse(source, tmp_path, local_parser, [unsupported])
    assert result['text'] == raw
    assert result['pages'][0]['raw_text'] == raw
    assert result['items'] == []
    assert result['issue_date'] is None and result['issue_number'] is None
    assert result['warnings'][0]['code'] == 'unsupported_layout'


def test_image_only_pdf_keeps_assets_without_fake_transcription(source, tmp_path, local_parser):
    local_parser['assets'] = {'assets/scan.jpg': b'original-image-evidence'}
    image = {'id': 'p1_1', 'path': 'assets/scan.jpg', 'page': 1,
             'bbox': {'x': 0, 'y': 0, 'width': 800, 'height': 1000}, 'width': 1600, 'height': 2000}
    result = parse(source, tmp_path, local_parser, [page(1, [], [], raw_text='')], [image])
    assert result['text'] == '' and result['items'] == []
    assert result['assets'][0]['path'] == 'assets/scan.jpg'
    assert {'image_only_evidence', 'unsupported_layout', 'no_ocr'} <= {w['code'] for w in result['warnings']}


def test_date_range_does_not_inherit_endpoint_year(source, tmp_path, local_parser):
    tags, texts = header()
    tags += [node('H1', [2], title='学期活动'), node('P', [3])]
    texts += [fragment(2, '学期活动'), fragment(3, '大赛周期：2026年10月14日-12月31日\n活动地点：校内')]
    result = parse(source, tmp_path, local_parser, [page(1, tags, texts)])
    field = result['items'][0]['dates'][0]
    assert field['raw_text'] == '2026年10月14日-12月31日'
    assert field['normalized_date'] is None and field['resolution'] == 'ambiguous'


@pytest.mark.parametrize('content,code', [
    (b'not a PDF', 'invalid_pdf'),
    (b'%PDF-2.0\nmissing end marker', 'invalid_pdf'),
    (b'%PDF-2.0\ntrailer << /Encrypt 7 0 R >>\n%%EOF', 'encrypted_pdf'),
])
def test_invalid_and_encrypted_sources_rejected_before_parser(tmp_path, monkeypatch, content, code):
    path = tmp_path / 'bad.pdf'
    path.write_bytes(content)
    monkeypatch.setattr(pdf_adapter.subprocess, 'Popen', lambda *a, **k: pytest.fail('parser must not execute'))
    with pytest.raises(pdf_adapter.PdfAdapterError) as error:
        pdf_adapter.parse_pdf(path, tmp_path / 'work')
    assert error.value.code == code


def test_missing_binary_is_a_safe_actionable_error(source, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_adapter.shutil, 'which', lambda _: None)
    with pytest.raises(pdf_adapter.PdfAdapterError) as error:
        pdf_adapter.parse_pdf(source, tmp_path / 'work')
    assert error.value.code == 'missing_parser'
    assert str(source) not in str(error.value)


def test_oversize_source_rejects_without_truncation(source, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_adapter, 'MAX_SOURCE_BYTES', len(PDF_BYTES) - 1)
    with pytest.raises(pdf_adapter.PdfAdapterError) as error:
        pdf_adapter.parse_pdf(source, tmp_path / 'work')
    assert error.value.code == 'source_too_large'
    assert not (tmp_path / 'work').exists()


def test_existing_adapter_artifacts_are_never_overwritten(source, tmp_path, local_parser):
    root = tmp_path / 'work'
    root.mkdir()
    evidence = root / 'rawparser.json'
    evidence.write_text('private prior evidence')
    with pytest.raises(pdf_adapter.PdfAdapterError) as error:
        pdf_adapter.parse_pdf(source, root)
    assert error.value.code == 'workdir_conflict'
    assert evidence.read_text() == 'private prior evidence'
    assert local_parser['commands'] == []


@pytest.mark.parametrize('diagnostic,code', [
    ('fatal: invalid document /private/source.pdf secret', 'parser_failed'),
    ('password required for encrypted PDF /private/source.pdf secret', 'encrypted_pdf'),
])
def test_parser_failure_never_returns_partial_items_or_private_diagnostics(source, tmp_path, local_parser, diagnostic, code):
    local_parser['returncode'] = 1
    local_parser['diagnostic'] = diagnostic
    local_parser['payload'] = {'total_pages': 1, 'pages': [page(1, [], [])]}
    with pytest.raises(pdf_adapter.PdfAdapterError) as error:
        pdf_adapter.parse_pdf(source, tmp_path / 'work')
    assert error.value.code == code
    assert '/private/' not in str(error.value) and 'secret' not in str(error.value)


def test_wrong_parser_version_does_not_run_parse(source, tmp_path, local_parser):
    local_parser['version'] = 'lit 2.13.0\n'
    with pytest.raises(pdf_adapter.PdfAdapterError) as error:
        pdf_adapter.parse_pdf(source, tmp_path / 'work')
    assert error.value.code == 'unsupported_parser_version'
    assert len(local_parser['commands']) == 1


def test_parser_timeout_rejects_partial_output(source, tmp_path, local_parser, monkeypatch):
    local_parser['payload'] = {'total_pages': 1, 'pages': [page(1, [], [])]}
    monkeypatch.setattr(pdf_adapter, 'PARSER_TIMEOUT', 0)
    with pytest.raises(pdf_adapter.PdfAdapterError) as error:
        pdf_adapter.parse_pdf(source, tmp_path / 'work')
    assert error.value.code == 'parser_timeout'


def test_asset_path_escape_is_rejected(source, tmp_path, local_parser):
    local_parser['assets'] = {'../escape.png': b'private-outside-file'}
    image = {'id': 'p1_1', 'path': '../escape.png', 'page': 1}
    with pytest.raises(pdf_adapter.PdfAdapterError) as error:
        parse(source, tmp_path, local_parser, [page(1, [], [])], [image])
    assert error.value.code == 'unsafe_asset_path'


def test_symlink_asset_escape_is_rejected(source, tmp_path, local_parser, monkeypatch):
    outside = tmp_path / 'outside.png'
    outside.write_bytes(b'private-outside-file')
    original_launch = pdf_adapter.subprocess.Popen

    def symlink_launch(command, **options):
        process = original_launch(command, **options)
        if '--version' not in command:
            (Path(options['cwd']) / 'assets' / 'poster.png').symlink_to(outside)
        return process

    monkeypatch.setattr(pdf_adapter.subprocess, 'Popen', symlink_launch)
    with pytest.raises(pdf_adapter.PdfAdapterError) as error:
        parse(source, tmp_path, local_parser, [page(1, [], [])], [{'id': 'p1_1', 'path': 'assets/poster.png', 'page': 1}])
    assert error.value.code == 'unsafe_asset_path'


def test_malformed_parser_json_is_a_safe_error(source, tmp_path, local_parser):
    local_parser['raw_json'] = '{unreadable extraction'
    with pytest.raises(pdf_adapter.PdfAdapterError) as error:
        pdf_adapter.parse_pdf(source, tmp_path / 'work')
    assert error.value.code == 'invalid_parser_output'


def test_parser_page_limit_rejects_instead_of_silently_dropping_pages(source, tmp_path, local_parser, monkeypatch):
    monkeypatch.setattr(pdf_adapter, 'MAX_PAGES', 1)
    with pytest.raises(pdf_adapter.PdfAdapterError) as error:
        parse(source, tmp_path, local_parser, [page(1, [], []), page(2, [], [])])
    assert error.value.code == 'page_limit'


def test_raw_artifact_limit_rejects_without_truncation(source, tmp_path, local_parser, monkeypatch):
    monkeypatch.setattr(pdf_adapter, 'MAX_ARTIFACT_BYTES', 20)
    with pytest.raises(pdf_adapter.PdfAdapterError) as error:
        parse(source, tmp_path, local_parser, [page(1, [], [], raw_text='complete evidence')])
    assert error.value.code == 'output_too_large'
    assert (tmp_path / 'work' / 'rawparser.json').stat().st_size > 20


def test_page_annotation_geometry_does_not_leak_neighbor_column_urls(source, tmp_path, local_parser):
    tags, texts = header()
    left, left_texts = table(10, ('左列讲座',))
    right, right_texts = table(30, ('右列讲座',))
    for text in right_texts:
        text['x'] += 420
    urls = [annotation('https://example.org/left', x=20, y=80),
            annotation('https://example.org/right', x=440, y=80)]
    result = parse(source, tmp_path, local_parser,
                   [page(1, tags + [left, right], texts + left_texts + right_texts, annotations=urls)])
    assert result['items'][0]['source_urls'] == ['https://example.org/left']
    assert result['items'][1]['source_urls'] == ['https://example.org/right']


def test_tagged_non_bulletin_is_not_promoted_to_fake_activity(source, tmp_path, local_parser):
    tags = [node('H1', [0], title='Project notes'), node('P', [1])]
    texts = [fragment(0, 'Project notes'), fragment(1, '时间：2026年10月14日\n地点：会议室')]
    result = parse(source, tmp_path, local_parser, [page(1, tags, texts)])
    assert result['items'] == []
    assert result['issue_date'] is None
    assert '2026年10月14日' in result['text']
    assert 'unsupported_layout' in {warning['code'] for warning in result['warnings']}


def test_missing_issue_year_never_uses_filename_or_event_year(source, tmp_path, local_parser):
    tags, texts = header()
    texts[1]['text'] = '10-13 No. 451'
    tags += [node('H1', [2], title='化学学院活动'), node('P', [3])]
    texts += [fragment(2, '化学学院活动'), fragment(3, '时间：2026年10月14日\n地点：校内')]
    result = parse(source, tmp_path, local_parser, [page(1, tags, texts)])
    assert result['issue_date'] is None and result['issue_number'] == 451
    assert result['items'][0]['dates'][0]['normalized_date'] == '2026-10-14'
    assert result['items'][0]['college_mentions'][0]['name'] == '化学学院'



def test_tagged_issue_layout_is_not_tied_to_a_single_publication_title(source, tmp_path, local_parser):
    tags, texts = header()
    texts[0]['text'] = '其他校园日报'
    tagged_table, table_texts = table(10, ('完整活动',))
    result = parse(source, tmp_path, local_parser, [page(1, tags + [tagged_table], texts + table_texts)])
    assert result['title'] == '其他校园日报'
    assert result['issue_date'] == '2026-10-13' and result['issue_number'] == 451
    assert result['items'][0]['title'] == '完整活动'

