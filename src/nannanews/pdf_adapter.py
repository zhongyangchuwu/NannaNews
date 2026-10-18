"""Offline, evidence-preserving adapter for tagged 南哪 bulletin PDFs.

LiteParse 2.14.4 is an external, pinned local executable, never installed here.
Tags, not heuristic layout blocks, determine reading order. Unsupported papers
retain their complete text and artifacts without invented activity records.
Limits reject documents, never truncate them. No OCR, network, models or shell.
"""
from collections import defaultdict
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import time

from nannanews.bulletin_common import SCHEMA_VERSION, date_field, normalized_text, source_url

MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_LOG_BYTES = 1024 * 1024
MAX_PAGES = 64
MAX_NODES = 100000
MAX_DEPTH = 64
PARSER_TIMEOUT = 60
PARSER_VERSION = '2.14.4'

_DATE = re.compile(r'(?<!\d)\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}(?:日|号)?(?!\d)')
_NUMBER = re.compile(r'(?:\bNo\.?\s*|第\s*)(\d+)(?:\s*期)?', re.I)
_HEADING = re.compile(r'^H[1-6]$')
_PREFIX = re.compile(r'^\s*\d+(?:\.\d+)*[.、．]?\s*')
_LABEL = re.compile(r'(报名截止时间|材料提交截止时间|报名截止|投稿截止|截止时间|截止日期|活动时间|演出时间|开课时间|大赛周期|时间|日期|活动地点|演出地点|地点|主讲人|主持人|面向人群|报名方式|投稿方式|报名对象|活动对象|参赛对象|服务时段|服务内容|详见)\s*[：:]')
_DATE_KINDS = {'报名截止时间': 'deadline', '材料提交截止时间': 'deadline',
               '报名截止': 'deadline', '投稿截止': 'deadline', '截止时间': 'deadline',
               '截止日期': 'deadline', '活动时间': 'event', '演出时间': 'event',
               '开课时间': 'event', '大赛周期': 'event', '时间': 'event', '日期': 'event'}
_HEADER_KIND = {'截止时间': 'deadline', '截止日期': 'deadline', '刊载时间': 'listed',
                '刊载日期': 'listed', '活动时间': 'event', '日期': 'source',
                '来源时间': 'source', '发布时间': 'source'}
_CATEGORIES = {'讲座', '院级活动', '社团活动', '招聘升学宣讲'}
_FIGURE = re.compile(r'Figure\s*(\d+)\s*[:：]', re.I)
_IMAGE_CITATION = re.compile(r'第\s*([一二三四五六七八九十\d]+)\s*页\s*Figure\s*(\d+)', re.I)
_COLLEGE = re.compile(r'(匡亚明|历史|文|哲学|新闻传播|外国语|社会|政府管理|信息管理|商|经济|经济管理|法|数学|物理|化学|化学化工|生命科学|地理与海洋科学|大气科学|环境|地球科学与工程|电子科学与工程|现代工程与应用科学|计算机|人工智能|软件|医|建城|建筑与城市规划|艺术|国际|教育研究|智能科学与技术|集成电路|能源与资源|数字经济与管理)学院|(?:安邦|毓琇|健雄|开甲|秉文|行知|有训)书院')


class PdfAdapterError(ValueError):
    """Safe import-facing failure with a stable machine-readable code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _error(code, message):
    raise PdfAdapterError(code, message)


def _relative_file(value, root):
    candidate = Path(value)
    candidate = candidate if candidate.is_absolute() else root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, ValueError):
        _error('unsafe_asset_path', 'PDF parser artifact escapes the private work directory or is missing.')
    if not resolved.is_file():
        _error('unsafe_asset_path', 'PDF parser artifact is not a regular file.')
    return resolved, relative.as_posix()


def _artifact_size(root):
    total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            candidate = Path(directory) / name
            if candidate.is_symlink():
                _error('unsafe_asset_path', 'PDF parser created a symbolic-link artifact.')
        for name in files:
            total += (Path(directory) / name).stat().st_size
            if total > MAX_ARTIFACT_BYTES + MAX_ASSET_BYTES + MAX_SOURCE_BYTES:
                _error('output_too_large', 'PDF parser artifacts exceed the local extraction limit.')
    return total


def _stop(process):
    # Kill the entire group even when the CLI exited but a child still holds
    # its diagnostic pipe open. The session was created exclusively for us.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _error('parser_stop_failed', 'The local PDF parser could not be stopped within its cleanup limit.')


def _run(command, workdir, *, timeout, monitor=False):
    """Bound time, diagnostics and extracted files while the process is running."""
    try:
        process = subprocess.Popen(command, cwd=workdir, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    except FileNotFoundError:
        _error('missing_parser', 'The configured local PDF parser is unavailable.')
    except OSError:
        _error('parser_start_failed', 'The configured local PDF parser could not be started.')
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    _error('parser_timeout', 'The local PDF parser exceeded its time limit.')
                if monitor:
                    _artifact_size(workdir)
                for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        if len(output) > MAX_LOG_BYTES:
                            _error('output_too_large', 'PDF parser diagnostics exceed the local extraction limit.')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _error('parser_timeout', 'The local PDF parser exceeded its time limit.')
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                _error('parser_timeout', 'The local PDF parser exceeded its time limit.')
    finally:
        _stop(process)
        process.stdout.close()
    diagnostic = output.decode('utf-8', errors='replace')
    if process.returncode:
        if re.search(r'encrypt|password', diagnostic, re.I):
            _error('encrypted_pdf', 'Encrypted or password-protected PDFs are not supported.')
        _error('parser_failed', 'The local PDF parser rejected the document; no partial import was produced.')
    return diagnostic


def _box(value):
    return {key: value[key] for key in ('x', 'y', 'width', 'height') if key in value}


def _overlap_area(a, b):
    if not all(k in a and k in b for k in ('x', 'y', 'width', 'height')):
        return 0
    width = max(0, min(a['x'] + a['width'], b['x'] + b['width']) - max(a['x'], b['x']))
    height = max(0, min(a['y'] + a['height'], b['y'] + b['height']) - max(a['y'], b['y']))
    return width * height


def _descendants(node):
    yield node
    for child in node.get('children', []):
        yield from _descendants(child)


def _mcids(node):
    return list(dict.fromkeys(mcid for child in _descendants(node)
                             for mcid in child.get('marked_content_ids', [])))


def _fragments(node, index):
    return [fragment for mcid in _mcids(node) for fragment in index.get(mcid, [])]


def _render(fragments):
    parts = []
    previous = None
    for fragment in fragments:
        text = fragment.get('text', '')
        if not text:
            continue
        if previous is not None:
            line_changed = abs(fragment.get('y', 0) - previous.get('y', 0)) > max(2, min(fragment.get('height', 12), previous.get('height', 12)) * 0.45)
            if line_changed:
                parts.append('\n')
            elif fragment.get('x', 0) - (previous.get('x', 0) + previous.get('width', 0)) > 2:
                parts.append(' ')
        parts.append(text)
        previous = fragment
    return ''.join(parts).strip()


def _locator(page, path, node, index):
    fragments = _fragments(node, index)
    return {'page': page['page'], 'tagpath': path, 'tag': node['type'],
            'mcids': _mcids(node), 'bboxes': [_box(fragment) for fragment in fragments]}


def _annotations(node, page, fragments):
    annotations = [annotation for child in _descendants(node)
                   for annotation in child.get('annotations', []) if isinstance(annotation, dict)]
    # Prefer the text with the greatest intersection when boxes from adjacent
    # columns overlap. Mere contact with a wide date-cell box is not ownership.
    for annotation in page.get('annotations', []):
        rectangle = annotation.get('rect', {})
        local = max((_overlap_area(fragment, rectangle) for fragment in fragments), default=0)
        if local and not any(_overlap_area(other, rectangle) > local for other in page.get('text_items', [])):
            annotations.append(annotation)
    unique = []
    seen = set()
    for annotation in annotations:
        key = json.dumps(annotation, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            unique.append(annotation)
    return unique


def _units(page, index):
    def visit(node, path):
        kind = node['type']
        if (kind in ('Table', 'P', 'Caption', 'Figure', 'L') or _HEADING.fullmatch(kind)
                or (node.get('marked_content_ids') and not node.get('children'))):
            yield {'node': node, 'path': path, 'text': _render(_fragments(node, index))}
            return
        for position, child in enumerate(node.get('children', [])):
            yield from visit(child, path + [position])
    for position, root in enumerate((page.get('structure_tree') or {}).get('roots', [])):
        yield from visit(root, [position])


def _rows(table):
    def visit(node, path):
        if node['type'] == 'TR':
            cells = [child for child in node.get('children', []) if child['type'] in ('TH', 'TD')]
            yield node, path, cells
        elif node is table or node['type'] in ('THead', 'TBody', 'TFoot'):
            for i, child in enumerate(node.get('children', [])):
                yield from visit(child, path + [i])
    yield from visit(table, [])


def _title(node, text):
    # A producer-supplied heading title retains spaces lost by extraction of
    # multiple font runs, while the complete numbered evidence remains in text.
    return str(node.get('title') or _PREFIX.sub('', text)).strip()


def _dates(body, locator):
    labels = list(_LABEL.finditer(body))
    dates = []
    for i, label in enumerate(labels):
        name = label.group(1)
        if name not in _DATE_KINDS:
            continue
        end = labels[i + 1].start() if i + 1 < len(labels) else len(body)
        raw = body[label.end():end].strip()
        if raw:
            dates.append(date_field(_DATE_KINDS[name], raw, body[label.start():end].strip(), locator))
    return dates


def _colleges(body, locator):
    mentions = []
    for line in body.splitlines():
        for match in _COLLEGE.finditer(line):
            mentions.append({'name': match.group(0), 'relation': 'audience' if re.search(r'面向|报名对象|参赛对象|招募对象|活动对象|服务对象', line[:match.start()]) else 'mentioned',
                             'evidence': line, 'locator': locator})
    return mentions


def _item(local_key, kind, title, category, body, locator, annotations):
    urls = list(dict.fromkeys(url for annotation in annotations
                             if (url := source_url(annotation.get('uri')))))
    return {'local_key': local_key, 'kind': kind, 'title': title, 'category': category,
            'body_text': body, 'source_urls': urls, 'locator': locator,
            'dates': [], 'college_mentions': _colleges(body, locator), 'image_refs': [],
            'annotations': annotations}


def _table_items(unit, page, index, category, warnings):
    rows = list(_rows(unit['node']))
    if not rows:
        warnings.append({'code': 'unsupported_table', 'page': page['page'], 'tagpath': unit['path']})
        return [], unit['text']
    header = []
    items = []
    rendered = []
    for row, relative_path, cells in rows:
        texts = [_render(_fragments(cell, index)) for cell in cells]
        rendered.append('\t'.join(texts))
        if cells and all(cell['type'] == 'TH' for cell in cells):
            header = [normalized_text(text) for text in texts]
            # 2.14.4 can coalesce adjacent header runs into the first MCID,
            # leaving the final TH empty. Only literal recognised labels may
            # repair those header cells; data rows are never geometry-inferred.
            labels = re.findall(r'活动时间|截止时间|截止日期|刊载时间|刊载日期|来源时间|发布时间|日期|活动', ' '.join(header))
            if len(labels) == len(cells):
                header = labels
            continue
        if not cells or not header or len(cells) != len(header) or header[0] not in ('活动', '讲座', '标题', '内容'):
            warnings.append({'code': 'unsupported_table_row', 'page': page['page'], 'tagpath': unit['path'] + relative_path})
            continue
        if not texts[0].strip():
            warnings.append({'code': 'unresolved_table_title', 'page': page['page'], 'tagpath': unit['path'] + relative_path})
            continue
        path = unit['path'] + relative_path
        locator = _locator(page, path, row, index)
        locator['cells'] = [{**_locator(page, path + [i], cell, index), 'header': header[i], 'text': texts[i]}
                            for i, cell in enumerate(cells)]
        annotations = _annotations(row, page, _fragments(row, index))
        body = '\n'.join(f'{label}：{text}' for label, text in zip(header, texts))
        item = _item('p' + str(page['page']) + '-tag-' + '-'.join(map(str, path)),
                     'summary', texts[0], category, body, locator, annotations)
        item['dates'] = [date_field(_HEADER_KIND.get(label, 'source'), text,
                                   f'{label}：{text}', locator['cells'][i])
                         for i, (label, text) in enumerate(zip(header, texts))
                         if i and text.strip() and label in _HEADER_KIND]
        items.append(item)
    return items, '\n'.join(rendered)


def _validate_payload(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('pages'), list):
        _error('invalid_parser_output', 'The local PDF parser returned an invalid JSON document.')
    pages = payload['pages']
    if not pages or len(pages) > MAX_PAGES or payload.get('total_pages') != len(pages):
        _error('page_limit', 'PDF page count is invalid or exceeds the local extraction limit.')
    nodes = 0
    for number, page in enumerate(pages, 1):
        if not isinstance(page, dict) or page.get('page') != number or not isinstance(page.get('text'), str):
            _error('invalid_parser_output', 'The local PDF parser returned invalid page evidence.')
        for key in ('width', 'height'):
            if not isinstance(page.get(key), (int, float)) or page[key] <= 0:
                _error('invalid_parser_output', 'The local PDF parser returned invalid page dimensions.')
        if not isinstance(page.get('text_items', []), list) or not isinstance(page.get('annotations', []), list):
            _error('invalid_parser_output', 'The local PDF parser returned invalid text metadata.')
        roots = (page.get('structure_tree') or {}).get('roots', [])
        stack = [(node, 1) for node in roots]
        while stack:
            node, depth = stack.pop()
            nodes += 1
            if nodes > MAX_NODES or depth > MAX_DEPTH:
                _error('structure_limit', 'PDF structure exceeds the local extraction limit.')
            if not isinstance(node, dict) or not isinstance(node.get('type'), str) or not isinstance(node.get('children', []), list) or not isinstance(node.get('marked_content_ids', []), list):
                _error('invalid_parser_output', 'The local PDF parser returned an invalid structure tree.')
            stack.extend((child, depth + 1) for child in node.get('children', []))
    if not isinstance(payload.get('images', []), list):
        _error('invalid_parser_output', 'The local PDF parser returned invalid image evidence.')


def _assets(payload, workdir):
    assets = []
    total = 0
    seen = set()
    for image in payload.get('images', []):
        if not isinstance(image, dict) or not isinstance(image.get('path'), str):
            _error('invalid_parser_output', 'The local PDF parser returned invalid image evidence.')
        path, relative = _relative_file(image['path'], workdir)
        total += path.stat().st_size
        if total > MAX_ASSET_BYTES:
            _error('output_too_large', 'Extracted PDF images exceed the local extraction limit.')
        image_id = str(image.get('id') or relative)
        if image_id in seen or image.get('page') not in range(1, len(payload['pages']) + 1):
            _error('invalid_parser_output', 'The local PDF parser returned invalid image identity.')
        seen.add(image_id)
        assets.append({'id': image_id, 'path': relative, 'mime': mimetypes.guess_type(path.name)[0] or 'application/octet-stream',
                       'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                       'page': image['page'], 'bbox': image.get('bbox'), 'width': image.get('width'),
                       'height': image.get('height'), 'rotation': image.get('rotation', 0)})
    return assets


def _page_number(raw):
    if raw.isdigit():
        return int(raw)
    numbers = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
    return numbers.get(raw)


def _attach_images(assets, captions, items, warnings):
    references = {}
    for page, unit, index in captions:
        fragments = _fragments(unit['node'], index)
        if not fragments:
            continue
        x = min(t['x'] for t in fragments)
        right = max(t['x'] + t['width'] for t in fragments)
        y = min(t['y'] for t in fragments)
        candidates = []
        for asset in assets:
            box = asset.get('bbox') or {}
            if asset['page'] != page['page'] or not all(k in box for k in ('x', 'y', 'width', 'height')):
                continue
            overlap = min(right, box['x'] + box['width']) - max(x, box['x'])
            distance = y - (box['y'] + box['height'])
            if overlap > 0 and -3 <= distance <= 100:
                candidates.append((overlap / max(1, min(right - x, box['width'])), -abs(distance), asset))
        candidates.sort(key=lambda candidate: candidate[:2], reverse=True)
        if not candidates or (len(candidates) > 1 and candidates[0][:2] == candidates[1][:2]):
            warnings.append({'code': 'unresolved_image_caption', 'page': page['page'], 'tagpath': unit['path']})
            continue
        asset = candidates[0][2]
        asset['caption_text'] = unit['text']
        asset['caption_locator'] = _locator(page, unit['path'], unit['node'], index)
        asset['caption_relation'] = 'geometry'
        figure = _FIGURE.search(unit['text'])
        if figure:
            references.setdefault((page['page'], int(figure.group(1))), []).append(asset['id'])
    for item in items:
        for citation in _IMAGE_CITATION.finditer(item['body_text']):
            key = (_page_number(citation.group(1)), int(citation.group(2)))
            matched = references.get(key, [])
            if len(matched) == 1:
                if matched[0] not in item['image_refs']:
                    item['image_refs'].append(matched[0])
            else:
                warnings.append({'code': 'unresolved_image_reference', 'local_key': item['local_key'], 'evidence': citation.group(0)})


def _normalize(payload, source, workdir, version):
    _validate_payload(payload)
    warnings = []
    assets = _assets(payload, workdir)
    pages = []
    units_by_page = []
    captions = []
    logical_text = []
    for page in payload['pages']:
        index = defaultdict(list)
        for fragment in page.get('text_items', []):
            index[fragment.get('mcid')].append(fragment)
        units = list(_units(page, index))
        units_by_page.append((page, index, units))
        page_parts = []
        covered = set()
        for unit in units:
            covered.update(_mcids(unit['node']))
            if unit['node']['type'] == 'Table':
                text = '\n'.join('\t'.join(_render(_fragments(cell, index)) for cell in cells)
                                 for _, _, cells in _rows(unit['node']))
            else:
                text = unit['text']
            if text:
                page_parts.append(text)
            if unit['node']['type'] == 'Caption':
                captions.append((page, unit, index))
        untagged = [fragment for fragment in page.get('text_items', []) if fragment.get('mcid') not in covered]
        if untagged and units:
            page_parts.append(_render(untagged))
            warnings.append({'code': 'untagged_text', 'page': page['page'], 'message': 'Unassigned text is retained after tagged content.'})
        text = '\n\n'.join(page_parts) if page_parts else page['text']
        pages.append({'page': page['page'], 'width': page['width'], 'height': page['height'],
                      'text': text, 'raw_text': page['text'], 'structure_tree': page.get('structure_tree'),
                      'annotations': page.get('annotations', [])})
        logical_text.append(text)
    text = '\n\n'.join(logical_text)
    first_units = units_by_page[0][2]
    header_text = '\n'.join(unit['text'] for unit in first_units[:3])
    # Untagged documents still expose issue metadata if an actual header says
    # so, but never acquire activities merely from dates in their raw text.
    if not first_units:
        header_text = payload['pages'][0]['text'].split('\n\n', 1)[0]
    title = first_units[0]['text'] if first_units and first_units[0]['text'] else None
    if not title:
        title = next((line.strip() for line in header_text.splitlines() if line.strip()), 'PDF document')
    issue_header = next((line for line in header_text.splitlines()
                         if _NUMBER.search(line) and (_DATE.search(line) or re.search(r'(?<!\d)\d{1,2}[-/.]\d{1,2}(?!\d)', line))), '')
    header_date = _DATE.search(issue_header)
    number = _NUMBER.search(issue_header)
    issue_date = date_field('source', header_date.group(0))['normalized_date'] if header_date else None
    issue_number = int(number.group(1)) if number else None
    supported = (issue_number is not None and bool(first_units)
                 and any(unit['node']['type'] == 'Table' or _HEADING.fullmatch(unit['node']['type'])
                         for _, _, units in units_by_page for unit in units))
    items = []
    category = None
    current = None
    blocked_section = False

    def finish():
        nonlocal current
        if current and current['body_text'].strip():
            current['dates'] = _dates(current['body_text'], current['locator'])
            items.append(current)
        elif current:
            warnings.append({'code': 'unparsed_section', 'local_key': current['local_key'],
                             'message': 'Heading has no extracted body; retained in document evidence, not inferred as an activity.'})
        current = None

    if supported:
        for page, index, units in units_by_page:
            if any(unit['node']['type'] == 'Caption' for unit in units) and not any(_HEADING.fullmatch(unit['node']['type']) for unit in units):
                finish()
            for unit in units:
                node = unit['node']
                kind = node['type']
                if _HEADING.fullmatch(kind):
                    finish()
                    heading = _title(node, unit['text'])
                    blocked_section = bool(re.search(r'订阅|编辑部|联系方式|联系我们', heading))
                    if heading in _CATEGORIES:
                        category = heading
                        continue
                    if '清单' in heading or blocked_section:
                        continue
                    locator = _locator(page, unit['path'], node, index)
                    locator['heading_text'] = unit['text']
                    current = _item('p' + str(page['page']) + '-tag-' + '-'.join(map(str, unit['path'])),
                                    'notice' if '通知' in heading else 'detail', heading,
                                    category, '', locator, _annotations(node, page, _fragments(node, index)))
                elif kind == 'Table':
                    finish()
                    if not blocked_section:
                        rows, _ = _table_items(unit, page, index, category, warnings)
                        items.extend(rows)
                elif current and kind not in ('Caption', 'Figure'):
                    current['body_text'] += ('\n\n' if current['body_text'] else '') + unit['text']
                    current['locator'].setdefault('body_nodes', []).append(_locator(page, unit['path'], node, index))
                    annotations = _annotations(node, page, _fragments(node, index))
                    current['annotations'].extend(annotations)
                    for annotation in annotations:
                        url = source_url(annotation.get('uri'))
                        if url and url not in current['source_urls']:
                            current['source_urls'].append(url)
        finish()
    else:
        warnings.append({'code': 'unsupported_layout', 'message': 'No supported tagged bulletin layout; full text and parser evidence retained without activity inference.'})
    for item in items:
        if item['kind'] != 'summary':
            item['college_mentions'] = _colleges(item['title'] + '\n' + item['body_text'], item['locator'])
    _attach_images(assets, captions, items, warnings)
    if assets:
        warnings.append({'code': 'no_ocr', 'message': 'Image pixels are preserved, not transcribed; no OCR was performed.', 'asset_count': len(assets)})
        if not any(page['text'].strip() for page in payload['pages']):
            warnings.append({'code': 'image_only_evidence', 'message': 'Image-only PDF has no extracted text; consult preserved images and original.'})
    if any(field['resolution'] in ('ambiguous', 'invalid') for item in items for field in item['dates']):
        warnings.append({'code': 'unresolved_dates', 'message': 'Ambiguous or invalid date claims remain literal and unresolved.'})
    return {'schema_version': SCHEMA_VERSION, 'format': 'pdf',
            'source_sha256': hashlib.sha256(source['bytes']).hexdigest(), 'source_filename': source['filename'],
            'source_original_path': 'source.pdf', 'title': title, 'issue_date': issue_date,
            'issue_date_evidence': header_date.group(0) if header_date else '', 'issue_number': issue_number,
            'parser': {'name': 'liteparse', 'version': version}, 'text': text,
            'items': items, 'pages': pages, 'assets': assets, 'warnings': warnings,
            'raw_artifact_path': 'rawparser.json'}


def parse_pdf(path, workdir, *, parser_binary=None):
    """Parse a bounded local PDF, preserving source.pdf, rawparser.json and assets.

    Raises PdfAdapterError with a stable safe .code on rejection. The caller owns
    the private work directory's lifecycle, including rejected parser evidence.
    Artifact names must be unused; existing files are never overwritten.
    """
    path = Path(path)
    if not path.is_file():
        _error('invalid_source', 'The PDF source is unavailable or is not a readable regular file.')
    try:
        with path.open('rb') as handle:
            source = handle.read(MAX_SOURCE_BYTES + 1)
    except OSError:
        _error('invalid_source', 'The PDF source is unavailable or is not a readable file.')
    if len(source) > MAX_SOURCE_BYTES:
        _error('source_too_large', 'The PDF source exceeds the local extraction limit.')
    if not re.match(rb'%PDF-\d\.\d', source[:16]) or b'%%EOF' not in source[-2048:]:
        _error('invalid_pdf', 'The source is not a complete PDF document.')
    if re.search(rb'/Encrypt\s*(?:\d+\s+\d+\s+R|<<)', source):
        _error('encrypted_pdf', 'Encrypted or password-protected PDFs are not supported.')
    binary = shutil.which(str(parser_binary or 'lit'))
    if not binary:
        _error('missing_parser', 'The configured local PDF parser is unavailable.')
    binary = str(Path(binary).resolve())
    root = Path(workdir).resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
        for name in ('source.pdf', 'rawparser.json', 'assets'):
            if (root / name).exists() or (root / name).is_symlink():
                _error('workdir_conflict', 'The private PDF extraction directory already contains adapter artifacts.')
        (root / 'assets').mkdir()
        with (root / 'source.pdf').open('xb') as handle:
            handle.write(source)
    except OSError:
        _error('invalid_workdir', 'The private PDF extraction directory cannot store artifacts.')
    version_text = _run([binary, '--version'], root, timeout=5)
    version = re.search(r'\blit\s+(\d+\.\d+\.\d+)\b', version_text)
    if not version or version.group(1) != PARSER_VERSION:
        _error('unsupported_parser_version', 'The PDF adapter requires the pinned LiteParse 2.14.4 executable.')
    command = [binary, 'parse', str(root / 'source.pdf'), '--format', 'json', '--no-ocr',
               '--extract-structure-tree', '--extract-text-metadata', '--extract-annotations',
               '--extract-images', '--image-output-dir', str(root / 'assets'), '-o', str(root / 'rawparser.json')]
    _run(command, root, timeout=PARSER_TIMEOUT, monitor=True)
    raw_path, _ = _relative_file(root / 'rawparser.json', root)
    if raw_path.stat().st_size > MAX_ARTIFACT_BYTES:
        _error('output_too_large', 'PDF parser JSON exceeds the local extraction limit.')
    try:
        payload = json.loads(raw_path.read_text(encoding='utf-8'))
        return _normalize(payload, {'bytes': source, 'filename': path.name}, root, version.group(1))
    except PdfAdapterError:
        raise
    except (ValueError, UnicodeError, TypeError, KeyError, AttributeError, RecursionError):
        _error('invalid_parser_output', 'The local PDF parser returned malformed extraction evidence.')
    except OSError:
        _error('invalid_parser_output', 'The local PDF parser evidence cannot be read.')
