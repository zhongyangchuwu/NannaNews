"""Offline extraction of literal bulletin tables and article sections.

No year, publisher, summary or remote image content is inferred. Table cells and
section HTML survive in locators; the complete original is retained by the store.
"""
import hashlib
from importlib.metadata import version
import mimetypes
from pathlib import Path
import re
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from nannanews.bulletin_common import SCHEMA_VERSION, date_field, normalized_text, source_url as canonical_source_url

MAX_HTML_BYTES = 8 * 1024 * 1024
BOILERPLATE = re.compile(r'编辑部招聘|订阅|联系我们|消息来源|来源说明|目录|版权|关于我们')
COLLEGES = re.compile(r'(?:匡亚明|历史|文|哲学|新闻传播|外国语|社会|政府管理|信息管理|经济管理|经济|商|法|数学|物理|化学化工|化学|生命科学|地理与海洋科学|大气科学|环境|地球科学与工程|电子科学与工程|现代工程与应用科学|计算机|人工智能|软件|医|建筑与城市规划|建城|艺术|国际|教育研究|智能科学与技术|集成电路|能源与资源|数字经济与管理)学院')
DATE_LABEL = re.compile(r'(?P<label>[^：:。；;\n]{0,20}(?:截止(?:时间|日期)?|报名时间|申报时间|活动时间|讲座时间|报告时间|演出时间|比赛时间|会议时间|开展日期|刊载时间|刊载日期|发布时间|发布日期|时间))\s*[：:]\s*(?P<value>[^\n；;]+)')
TEXT_URL = re.compile(r'''https?://[^\s<>"'“”‘’《》，。；！？]+''', re.I)


class HtmlAdapterError(ValueError):
    def __init__(self, message, code='invalid_html'):
        super().__init__(message)
        self.code = code


def _text(node):
    # Use only actual text and explicit line breaks, not generated summaries.
    fragments = []
    for element in node.descendants:
        if isinstance(element, Tag):
            if element.name == 'br':
                fragments.append('\n')
        elif element.parent.name not in {'script', 'style'}:
            fragments.append(str(element))
    return ''.join(fragments).strip()


def _links(node, base):
    hrefs = [anchor['href'].strip() for anchor in node.find_all('a', href=True)]
    for text in node.find_all(string=True):
        # Anchor labels may be shortened or differ from their actual targets.
        if text.find_parent(['a', 'script', 'style', 'noscript', 'template']):
            continue
        for match in TEXT_URL.finditer(str(text)):
            value = match.group(0).rstrip('.,;:!?')
            for closing, opening in ((')', '('), (']', '['), ('}', '{'), ('）', '（'), ('】', '【')):
                while value.endswith(closing) and value.count(closing) > value.count(opening):
                    value = value[:-1]
            hrefs.append(value)
    result = []
    for href in hrefs:
        if href.startswith('#'):
            continue
        url = canonical_source_url(urljoin(base, href) if base and href not in {'None', 'null'} else href)
        if url and url not in result:
            result.append(url)
    return result


def _date_kind(label):
    if '截止' in label:
        return 'deadline'
    if '刊载' in label:
        return 'listed'
    if '发布' in label:
        return 'source'
    if '报名' in label:
        return 'registration'
    if '申报' in label:
        return 'application'
    return 'event'


def _colleges(text, locator):
    result = []
    for line in text.splitlines():
        for name in dict.fromkeys(COLLEGES.findall(line)):
            audience = bool(re.search(r'(?:面向|活动对象|参赛对象|参与对象|招募对象|报名对象|服务对象)[^。；\n]*' + re.escape(name), line))
            result.append({'name': name, 'relation': 'audience' if audience else 'mentioned',
                           'evidence': line.strip(), 'locator': locator})
    return result


def _category_labels(headings, tables, issue_heading):
    """Only a heading introducing a table or child sections is a category.

    A notice may itself use h2. Its own prose must not become the category
    of later h3 articles merely because it is the nearest preceding h2.
    """
    heading_ids = {id(heading) for heading in headings}
    category_ids = set()
    for heading in headings:
        if heading is issue_heading or heading.name not in {'h1', 'h2'} or BOILERPLATE.search(_text(heading)):
            continue
        for element in heading.next_elements:
            if any(parent is heading for parent in element.parents):
                continue
            if element.find_parent('nav') or element.find_parent(class_='toc'):
                continue
            if isinstance(element, Tag):
                if id(element) in heading_ids:
                    if int(element.name[1]) > int(heading.name[1]):
                        category_ids.add(id(heading))
                    break
                if element.name == 'table' or (element.name == 'div' and 'tabular' in element.get('class', [])):
                    category_ids.add(id(heading))
                    break
            elif element.strip() and element.parent.name not in {'script', 'style'}:
                break

    def inherited(node):
        owner = node.find_previous(['h1', 'h2'])
        return _text(owner) if owner is not None and id(owner) in category_ids else None

    labels = {f'table-{index}': inherited(table) for index, table in enumerate(tables, 1)}
    labels.update({f'section-{index}': inherited(heading) if int(heading.name[1]) > 2 else None
                   for index, heading in enumerate(headings, 1)})
    return labels


def category_labels(html):
    """Project category labels from original HTML without reparsing its records."""
    soup = BeautifulSoup(html, 'html.parser')
    headings = [h for h in soup.find_all(re.compile(r'^h[1-6]$'))
                if not h.find_parent('nav') and not h.find_parent(class_='toc')]
    if not headings:
        return {}
    issue_heading = next((h for h in headings if h.name == 'h1' and '南哪' in _text(h)), headings[0])
    tables = [t for t in soup.find_all('table') if not t.find_parent('nav') and not t.find_parent(class_='toc')]
    return _category_labels(headings, tables, issue_heading)


def parse_html(path, *, source_url=None):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise HtmlAdapterError('HTML input must be a regular non-symlink file.')
    if path.stat().st_size > MAX_HTML_BYTES:
        raise HtmlAdapterError('HTML input exceeds the offline bulletin limit.', 'html_too_large')
    raw = path.read_bytes()
    if len(raw) > MAX_HTML_BYTES:
        raise HtmlAdapterError('HTML input exceeds the offline bulletin limit.', 'html_too_large')
    try:
        html = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise HtmlAdapterError('HTML input must contain valid UTF-8.') from None
    soup = BeautifulSoup(html, 'html.parser')
    if soup.body is None or soup.find('h1') is None:
        raise HtmlAdapterError('HTML bulletin requires a body and an issue heading.')
    base = canonical_source_url(source_url)
    headings = [h for h in soup.find_all(re.compile(r'^h[1-6]$'))
                if not h.find_parent('nav') and not h.find_parent(class_='toc')]
    issue_heading = next((h for h in headings if h.name == 'h1' and '南哪' in _text(h)), headings[0])
    title = _text(issue_heading)
    issue_claim = date_field('source', title, title, {'tag': issue_heading.name, 'id': issue_heading.get('id')})
    issue_number_match = re.search(r'第\s*(\d+)\s*期', title)
    warnings, items, assets = [], [], []
    if not issue_claim['normalized_date']:
        warnings.append({'code': 'issue_date_unknown', 'message': 'Issue heading has no single explicit-year date.'})
    image_ids = {}
    asset_sizes = {}
    for index, image in enumerate(soup.find_all('img', src=True), 1):
        image_id = f'html-image-{index}'
        image_ids[id(image)] = image_id
        src = image['src']
        parts = urlsplit(src)
        relative = Path(unquote(parts.path))
        if parts.scheme or parts.netloc or relative.is_absolute() or '..' in relative.parts or not relative.parts:
            warnings.append({'code': 'image_not_acquired', 'message': 'Image reference retained without network acquisition.', 'src': src})
            continue
        original = path.parent / relative
        if original.is_symlink() or not original.resolve().is_relative_to(path.parent.resolve()) or not original.is_file():
            warnings.append({'code': 'image_missing', 'message': 'Local image unavailable or outside input directory.', 'src': src})
            continue
        if original.stat().st_size > 32 * 1024 * 1024:
            raise HtmlAdapterError('Local HTML image exceeds the bulletin asset limit.', 'asset_too_large')
        if len(assets) >= 1000:
            raise HtmlAdapterError('HTML image count exceeds the bulletin asset limit.', 'too_many_assets')
        asset_sizes[relative.as_posix()] = original.stat().st_size
        if sum(asset_sizes.values()) > 256 * 1024 * 1024:
            raise HtmlAdapterError('HTML image evidence exceeds the total bulletin limit.', 'assets_too_large')
        assets.append({'id': image_id, 'path': relative.as_posix(), 'mime': mimetypes.guess_type(src)[0] or 'application/octet-stream',
                       'sha256': hashlib.sha256(original.read_bytes()).hexdigest(), 'page': None, 'bbox': None,
                       'width': image.get('width'), 'height': image.get('height')})
    tables = [t for t in soup.find_all('table') if not t.find_parent('nav') and not t.find_parent(class_='toc')]
    categories = _category_labels(headings, tables, issue_heading)
    table_headings = set()
    for table_index, table in enumerate(tables, 1):
        heading = table.find_previous(re.compile(r'^h[1-6]$'))
        category = categories[f'table-{table_index}']
        if heading:
            table_headings.add(id(heading))
        rows = [r for r in table.find_all('tr') if r.find_parent('table') is table]
        if not rows:
            continue
        header_row = rows[0]
        headers = [_text(c) for c in header_row.find_all(['th', 'td'], recursive=False)]
        if not header_row.find('th') and not any(re.search(r'标题|消息|讲座|活动|日期|时间', h) for h in headers):
            warnings.append({'code': 'unsupported_table', 'message': 'Table has no identifiable bulletin header.', 'table': table_index})
            continue
        for row_index, row in enumerate(rows[1:], 1):
            cells = row.find_all(['td', 'th'], recursive=False)
            if not cells or not _text(cells[0]):
                continue
            values = [_text(c) for c in cells]
            locator = {'tag': 'tr', 'table': table_index, 'row': row_index, 'section_id': heading.get('id') if heading else None,
                       'headers': headers, 'cells': values, 'raw_html': str(row),
                       'internal_targets': [a['href'][1:] for a in row.find_all('a', href=True) if a['href'].startswith('#')]}
            dates = [date_field(_date_kind(header), value, f'{header}: {value}', dict(locator, column=i))
                     for i, (header, value) in enumerate(zip(headers, values)) if i and re.search(r'日期|时间|截止|刊载', header) and value]
            body = '\n'.join(f'{headers[i] if i < len(headers) else str(i + 1)}: {value}' for i, value in enumerate(values))
            items.append({'local_key': f'table-{table_index}-row-{row_index}', 'kind': 'summary', 'title': values[0],
                          'category': category, 'body_text': body, 'source_urls': _links(row, base), 'locator': locator,
                          'dates': dates, 'college_mentions': _colleges(body, locator), 'image_refs': []})
    for heading_index, heading in enumerate(headings, 1):
        if heading is issue_heading or id(heading) in table_headings or heading.name == 'h1' or BOILERPLATE.search(_text(heading)):
            continue
        rank = int(heading.name[1])
        fragments = []
        for sibling in heading.next_elements:
            if isinstance(sibling, Tag) and re.fullmatch(r'h[1-6]', sibling.name or '') and int(sibling.name[1]) <= rank:
                break
            if isinstance(sibling, Tag) and sibling.name in {'p', 'ul', 'ol', 'div'} and sibling.find_parent(['p', 'ul', 'ol']) is None:
                if not sibling.find('table') and not sibling.find(re.compile(r'^h[1-6]$')):
                    fragments.append(str(sibling))
        # Late-generation articles place text directly after h3 inside li.
        if heading.parent.name == 'li':
            fragments = [str(s) for s in heading.next_siblings]
        section = BeautifulSoup(''.join(fragments), 'html.parser')
        body = _text(section)
        if not body:
            continue
        category = categories[f'section-{heading_index}']
        locator = {'tag': heading.name, 'id': heading.get('id'), 'heading_index': heading_index,
                   'raw_html': ''.join(fragments)}
        dates = [date_field(_date_kind(m['label']), m['value'].strip(), m.group(0), locator) for m in DATE_LABEL.finditer(body)]
        section_image_sources = {image.get('src') for image in section.find_all('img')}
        items.append({'local_key': f'section-{heading_index}', 'kind': 'detail', 'title': _text(heading), 'category': category,
                      'body_text': body, 'source_urls': list(dict.fromkeys(_links(heading, base) + _links(section, base))),
                      'locator': locator, 'dates': dates,
                      'college_mentions': _colleges(_text(heading) + '\n' + body, locator),
                      'image_refs': [image_ids[id(image)] for image in soup.find_all('img')
                                     if image.get('src') in section_image_sources and id(image) in image_ids]})
    if not items:
        warnings.append({'code': 'no_activity_items', 'message': 'No supported activity rows or detail sections; original text preserved.'})
    return {'schema_version': SCHEMA_VERSION, 'format': 'html', 'source_sha256': hashlib.sha256(raw).hexdigest(),
            'source_filename': path.name, 'source_url': base, 'title': title, 'issue_date': issue_claim['normalized_date'],
            'issue_date_evidence': title, 'issue_number': int(issue_number_match[1]) if issue_number_match else None,
            'parser': {'name': 'beautifulsoup4-bulletin', 'version': version('beautifulsoup4')},
            'text': _text(soup.body), 'items': items, 'pages': [], 'assets': assets, 'warnings': warnings,
            'raw_artifact_path': None}
