"""Export immutable bulletin evidence as a public, backend-free reader dataset.

Only the field projections below cross the privacy boundary. Originals are never
published except verified PDFs; acquired raster posters are content-addressed.
The search builder must subsequently create manifest.files.search_index.
"""
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timezone
import base64
import io
import hashlib
import ipaddress
from itertools import chain
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
from urllib.parse import urlsplit

from nannanews.bulletin_date_repair import public_source_review
from nannanews.bulletin_activities import activity_map
from nannanews.html_bulletin_adapter import MAX_HTML_BYTES, category_labels
from nannanews.content_rules import RULES_VERSION

GENERATOR = 'webnews-static-v2'
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_WARNING_CODES = frozenset({
    'claimed_issue_date_mismatch', 'issue_date_unknown', 'image_not_acquired',
    'image_missing', 'unsupported_table', 'unsupported_table_row',
    'unresolved_table_title', 'unresolved_image_caption', 'unresolved_image_reference',
    'untagged_text', 'unparsed_section', 'unsupported_layout', 'no_ocr',
    'image_only_evidence', 'unresolved_dates', 'no_activity_items',
})


class StaticExportError(ValueError):
    """A safe diagnostic: never contains database rows or private file paths."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _id(prefix, identity):
    return prefix + hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]


def _iso(value):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise StaticExportError('Bulletin contains an invalid normalized date.') from None
    return value


def _positive(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdecimal() and int(value) > 0:
        return int(value)
    return None


def _public_url(value):
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if (parts.scheme not in {'https', 'http'} or not host or parts.username
                or parts.password or any(ord(c) < 32 for c in value)):
            return None
        if host.lower() == 'localhost' or host.lower().endswith(('.localhost', '.local', '.internal')):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        parts.port  # Reject malformed ports without exposing the URL.
        return value
    except (ValueError, TypeError):
        return None


def _no_symlinks(path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise StaticExportError('Export paths must not contain symlinks.')


def _asset_file(upload, relative):
    if (not isinstance(relative, str) or not relative or '\\' in relative
            or '\x00' in relative):
        raise StaticExportError('Invalid stored asset path.')
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts or relative.parts[0] != 'bulletins':
        raise StaticExportError('Stored asset path escapes the bulletin directory.')
    candidate = upload / relative
    _no_symlinks(candidate)
    if not candidate.resolve().is_relative_to(upload / 'bulletins'):
        raise StaticExportError('Stored asset path escapes the bulletin directory.')
    return candidate


def _image_type(header):
    # Raster signatures, not a filename/mime claim; SVG/HTML/script are disallowed.
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png', '.png'
    if header.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg', '.jpg'
    if header.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif', '.gif'
    if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return 'image/webp', '.webp'
    if header.startswith(b'BM'):
        return 'image/bmp', '.bmp'
    if header.startswith((b'II*\x00', b'MM\x00*')):
        return 'image/tiff', '.tiff'
    if header[4:8] == b'ftyp' and header[8:12] in {b'avif', b'avis'}:
        return 'image/avif', '.avif'
    raise StaticExportError('Stored poster is not an allowed raster image.')


def _copy_asset(stage, upload, relative, expected_hash, *, pdf=False):
    if not isinstance(expected_hash, str) or not _HASH.fullmatch(expected_hash):
        raise StaticExportError('Invalid stored asset digest.')
    source = _asset_file(upload, relative)
    # O_NOFOLLOW plus component checks avoids copying a final-component symlink.
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise StaticExportError('Stored public asset is missing or unreadable.') from None
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        maximum = (64 if pdf else 32) * 1024 * 1024
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
            raise StaticExportError('Stored public asset is not a bounded regular file.')
        header = stream.read(32)
        if pdf:
            if not header.startswith(b'%PDF-'):
                raise StaticExportError('Stored original is not a PDF.')
            mime, extension = 'application/pdf', '.pdf'
        else:
            mime, extension = _image_type(header)
        name = 'assets/' + expected_hash + extension
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        stream.seek(0)
        # Copy first, then verify the bytes that will actually be published.
        with target.open('wb') as output:
            size = 0
            while block := stream.read(1024 * 1024):
                size += len(block)
                if size > maximum:
                    raise StaticExportError('Stored public asset exceeds its size limit.')
                digest.update(block)
                output.write(block)
        if digest.hexdigest() != expected_hash:
            raise StaticExportError('Stored public asset hash does not match its bytes.')
    return name, mime


def _copy_source_image(stage, image):
    """Publish an inert full-frame preview, never private capture bytes or SVG."""
    from nannanews import acquisition
    from PIL import Image

    origin = _public_url(image['url'])
    if not origin:
        raise StaticExportError('Reviewed source image has an unsafe public URL.')
    try:
        preview = acquisition._model_images([
            dict(image, source_image_index=image['image_index'])])[0]
        raw = base64.b64decode(preview['data'], validate=True)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != preview['digest'] or _image_type(raw[:32])[0] != 'image/jpeg':
            raise ValueError
        with Image.open(io.BytesIO(raw)) as raster:
            width, height = raster.size
    except (acquisition.AcquisitionError, ValueError, KeyError, TypeError, OSError):
        raise StaticExportError('Reviewed source image cannot be safely published.') from None
    name = 'assets/' + digest + '.jpg'
    target = stage / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(raw)
    return {
        'id': _id('image-', image['sha256'] + ':' + str(image['image_index']) + ':' + origin),
        'path': name, 'mime': 'image/jpeg', 'width': width, 'height': height,
        'page': None, 'caption': '原文配图安全预览', 'source_url': origin,
    }


def _rows(db, sql):
    return [dict(row) for row in db.execute(sql)]


def _html_categories(documents, upload):
    """Verify original HTML and retain only its structural category headings.

    Imported occurrences are immutable evidence. Older parser mistakes are
    corrected in the reader projection, never by rewriting those raw rows.
    """
    categories = {}
    for document in documents:
        if document['format'] != 'html':
            continue
        source = _asset_file(upload, document['original_path'])
        try:
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_HTML_BYTES:
                    raise StaticExportError('Stored HTML category evidence is not a bounded regular file.')
                raw = stream.read(MAX_HTML_BYTES + 1)
            if len(raw) > MAX_HTML_BYTES or hashlib.sha256(raw).hexdigest() != document['source_sha256']:
                raise StaticExportError('Stored HTML category evidence does not match its digest.')
            labels = category_labels(raw.decode('utf-8-sig'))
        except (OSError, UnicodeDecodeError):
            raise StaticExportError('Stored HTML category evidence is missing or unreadable.') from None
        categories[document['id']] = {label for label in labels.values() if label is not None}
    return categories


def _unique(values):
    # A linked summary/detail may repeat identical evidence, but not lose claims.
    return [json.loads(value) for value in sorted({_json(v).decode('utf-8') for v in values})]


def _read_public(db, stage, upload, held_occurrence_ids):
    from nannanews import bulletin_source_repair
    from nannanews import bulletin_date_repair

    issues = _rows(db, '''SELECT id,identity_key,title,issue_date,issue_number
                         FROM bulletin_issues ORDER BY coalesce(issue_date,'') DESC,id DESC''')
    documents = _rows(db, '''SELECT id,issue_id,source_sha256,format,source_filename,original_path,
        CASE WHEN format='pdf' THEN original_path END AS pdf_original,
        json_extract(parsed_json,'$.claimed_issue_date') AS claimed_issue_date,
        json_extract(parsed_json,'$.issue_date') AS issue_date
        FROM bulletin_documents ORDER BY id DESC''')
    html_categories = _html_categories(documents, upload)
    items = _rows(db, 'SELECT id,identity_key FROM bulletin_items ORDER BY identity_key')
    occurrences = _rows(db, '''SELECT o.id,o.item_id,o.document_id,o.local_key,o.kind,o.title,
        o.category,o.body_text,o.image_refs_json,
        json_extract(o.locator_json,'$.page') AS page,
        json_extract(o.locator_json,'$.id') AS anchor,
        d.source_sha256,d.issue_id,i.issue_date
        FROM bulletin_occurrences o JOIN bulletin_documents d ON d.id=o.document_id
        JOIN bulletin_issues i ON i.id=d.issue_id
        ORDER BY coalesce(i.issue_date,'') DESC,o.document_id DESC,
                 CASE o.kind WHEN 'detail' THEN 0 ELSE 1 END,o.id DESC''')
    raw_item_ids = {row['id']: _id('item-', row['identity_key']) for row in items}
    canonical_items = activity_map(db)
    item_ids = {item_id: raw_item_ids[canonical] for item_id, canonical in canonical_items.items()}
    item_aliases = defaultdict(list)
    for item_id, canonical in canonical_items.items():
        if item_id != canonical:
            item_aliases[raw_item_ids[canonical]].append(raw_item_ids[item_id])
    items = [row for row in items if canonical_items[row['id']] == row['id']]
    issue_ids = {row['id']: _id('issue-', row['identity_key']) for row in issues}
    document_ids = {row['id']: row['source_sha256'] for row in documents}
    if any(not isinstance(v, str) or not _HASH.fullmatch(v) for v in document_ids.values()):
        raise StaticExportError('Invalid bulletin document digest.')
    if len(set(raw_item_ids.values())) != len(raw_item_ids) or len(set(issue_ids.values())) != len(issue_ids):
        raise StaticExportError('Public identity collision.')
    occurrence_public_ids = {row['id']: _id('occ-', row['source_sha256'] + ':' + row['local_key'])
                             for row in occurrences}
    if held_occurrence_ids - set(occurrence_public_ids.values()):
        raise StaticExportError('A held occurrence is not present in this bulletin snapshot.')
    held_items = {canonical_items[row['item_id']] for row in occurrences
                  if occurrence_public_ids[row['id']] in held_occurrence_ids}
    held_rows = {row['id'] for row in occurrences if canonical_items[row['item_id']] in held_items}
    group_keys = {row['id']: (canonical_items[row['item_id']], row['document_id']) for row in occurrences}
    date_reviews = {}
    review_cursor = db.execute(
        'SELECT id AS revision_id,occurrence_id,status,reason,rules_version,source_review_json FROM ('
        + bulletin_date_repair.current_revisions_sql(db) + ')')
    source_corrections = bulletin_source_repair.public_corrections(db)
    primary_images, source_media_cache, current_activities = {}, {}, {}
    primary_image_paths = set()
    for stored_review in review_cursor:
        review = dict(stored_review)
        occurrence_id = review.pop('occurrence_id')
        if any(not isinstance(review[key], str) for key in ('status', 'reason', 'rules_version')):
            raise StaticExportError('Invalid public date review explanation.')
        try:
            captured_review = json.loads(review.pop('source_review_json') or 'null')
            revision_id = review.pop('revision_id')
            source_review = public_source_review(captured_review, rules_version=review['rules_version'])
            if review['rules_version'] == RULES_VERSION:
                review['claims'] = [dict(claim) for claim in db.execute(
                    '''SELECT kind,raw_text,normalized_date,resolution,evidence
                       FROM bulletin_date_revision_claims WHERE revision_id=? ORDER BY id''', (revision_id,))]
                primary_quotes = ([quote for source in source_review['sources'] for quote in source['quotes']]
                    if source_review['status'] in {'source_verified', 'source_conflict'} else [])
                for claim in review['claims']:
                    claim['literal_source'] = ('primary' if any(claim['raw_text'] in quote for quote in primary_quotes)
                                               else 'bulletin')
                activity = source_review.get('activity')
                replaced = {correction['original_url'] for correction in source_corrections[occurrence_id]}
                current_source = not any(source['url'] in replaced for source in source_review['sources'])
                if activity is not None and current_source and occurrence_id not in held_rows:
                    current_activities[occurrence_id] = activity
                    selected = set(activity['image_indices'])
                    images = []
                    for source in captured_review['sources']:
                        for image in source.get('images', []):
                            if image['image_index'] not in selected:
                                continue
                            key = (image['sha256'], image['image_index'], image['url'])
                            if key not in source_media_cache:
                                source_media_cache[key] = _copy_source_image(stage, image)
                            public_image = source_media_cache[key]
                            images.append(public_image)
                            primary_image_paths.add(public_image['path'])
                    primary_images[occurrence_id] = images
            if review['rules_version'] != RULES_VERSION:
                review['rules_version'] = 'legacy-content-evidence'
            for source in source_review['sources']:
                if (not _public_url(source['url']) or not _public_url(source['resolved_url'])
                        or not _HASH.fullmatch(source['sha256'])
                        or not isinstance(source['title'], str) or not isinstance(source['fetched_at'], str)
                        or not isinstance(source['quotes'], list)
                        or any(not isinstance(quote, str) for quote in source['quotes'])):
                    raise ValueError
                _iso(source['published_date'])
            review['source_review'] = source_review
            date_reviews[occurrence_id] = review
        except (KeyError, TypeError, ValueError):
            raise StaticExportError('Invalid public source verification evidence.') from None
    groups = defaultdict(lambda: {'dates': [], 'expiry_dates': [], 'colleges': [], 'sources': set()})
    original_dates = defaultdict(list)
    for row in _rows(db, '''SELECT occurrence_id,kind,raw_text,normalized_date,resolution,evidence
                            FROM bulletin_dates ORDER BY id'''):
        occurrence_id = row.pop('occurrence_id')
        row['normalized_date'] = _iso(row['normalized_date'])
        original_dates[occurrence_id].append(row)
    for row in _rows(db, '''SELECT occurrence_id,kind,raw_text,normalized_date,resolution,evidence
                            FROM bulletin_effective_dates ORDER BY id'''):
        occurrence_id = row.pop('occurrence_id')
        key = group_keys[occurrence_id]
        if row['resolution'] not in {'explicit', 'contextual', 'year_unknown', 'ambiguous', 'invalid'}:
            raise StaticExportError('Invalid bulletin date resolution.')
        row['normalized_date'] = _iso(row['normalized_date'])
        if row['normalized_date'] is not None and row['resolution'] not in {'explicit', 'contextual'}:
            raise StaticExportError('Unresolved bulletin date has an inferred year.')
        if row['resolution'] == 'contextual' and (row['normalized_date'] is None
                or not isinstance(row['evidence'], str) or not row['evidence'].strip()
                or row['evidence'].strip() == row['raw_text'].strip()):
            raise StaticExportError('Contextual bulletin date lacks a separate source basis.')
        claims = date_reviews.get(occurrence_id, {}).get('claims', [])
        row['literal_source'] = 'primary' if any(
            claim['literal_source'] == 'primary' and all(row[key] == claim[key]
                for key in ('kind', 'raw_text', 'normalized_date', 'resolution', 'evidence'))
            for claim in claims) else 'bulletin'
        groups[key]['dates'].append(row)
        if date_reviews.get(occurrence_id, {}).get('status') not in {'recurring_no_end', 'uncertain', 'invalid_source'}:
            groups[key]['expiry_dates'].append(row)
    for row in _rows(db, 'SELECT occurrence_id,name,relation,evidence FROM bulletin_colleges ORDER BY id'):
        key = group_keys[row.pop('occurrence_id')]
        if row['relation'] not in {'audience', 'mentioned'}:
            raise StaticExportError('Invalid bulletin college relation.')
        groups[key]['colleges'].append(row)
    document_sources = defaultdict(set)
    for row in _rows(db, 'SELECT document_id,source_url FROM bulletin_document_sources ORDER BY source_url'):
        if url := _public_url(row['source_url']):
            document_sources[row['document_id']].add(url)
    for row in _rows(db, 'SELECT occurrence_id,source_url FROM '
                     + bulletin_source_repair.sources_table(db) + ' ORDER BY source_url'):
        if url := _public_url(row['source_url']):
            groups[group_keys[row['occurrence_id']]]['sources'].add(url)
    superseded_by_group = defaultdict(set)
    for occurrence_id, corrections in source_corrections.items():
        superseded_by_group[group_keys[occurrence_id]].update(correction['original_url'] for correction in corrections)
    for key, group in groups.items():
        group['dates'] = _unique(group['dates'])
        group['colleges'] = _unique(group['colleges'])
        group['sources'] = sorted(group['sources'] | (document_sources[key[1]] - superseded_by_group[key]))
        deadlines = [v['normalized_date'] for v in group['expiry_dates']
                     if v['resolution'] in {'explicit', 'contextual'} and v['normalized_date'] and v['kind'] == 'deadline']
        events = [v['normalized_date'] for v in group['expiry_dates']
                  if v['resolution'] in {'explicit', 'contextual'} and v['normalized_date'] and v['kind'] == 'event']
        group['expiry'] = max(deadlines or events, default=None)
    # Extract only safe warning codes, never messages, parser/fullparsed/provenance.
    warnings = defaultdict(set)
    for row in _rows(db, '''SELECT d.id,json_extract(w.value,'$.code') AS code
        FROM bulletin_documents d,json_each(d.parsed_json,'$.warnings') w WHERE w.type='object' '''):
        if row['code'] in _WARNING_CODES:
            warnings[row['id']].add(row['code'])
    assets = {}
    image_paths = set()
    for row in _rows(db, '''SELECT document_id,asset_id,private_path,sha256,
        json_extract(evidence_json,'$.width') AS width,
        json_extract(evidence_json,'$.height') AS height,
        json_extract(evidence_json,'$.page') AS page,
        json_extract(evidence_json,'$.caption_text') AS caption
        FROM bulletin_assets ORDER BY document_id,asset_id'''):
        path, mime = _copy_asset(stage, upload, row['private_path'], row['sha256'])
        image_paths.add(path)
        assets[(row['document_id'], row['asset_id'])] = {
            'id': _id('image-', document_ids[row['document_id']] + ':' + row['asset_id']),
            'path': path, 'mime': mime, 'width': _positive(row['width']),
            'height': _positive(row['height']), 'page': _positive(row['page']),
            'caption': row['caption'] if isinstance(row['caption'], str) else None,
        }
    # HTML cells are strings with headers; PDF cells are objects with extra private
    # locators. Project their header/text in SQL instead of retrieving raw HTML.
    table_cells = defaultdict(list)
    for row in _rows(db, '''SELECT o.id,
        CASE WHEN c.type='object' THEN json_extract(c.value,'$.header')
             ELSE json_extract(o.locator_json,'$.headers[' || c.key || ']') END AS header,
        CASE WHEN c.type='object' THEN json_extract(c.value,'$.text')
             WHEN c.type='text' THEN c.value END AS text
        FROM bulletin_occurrences o,json_each(o.locator_json,'$.cells') c
        ORDER BY o.id,CAST(c.key AS INTEGER)'''):
        if isinstance(row['text'], str):
            table_cells[row['id']].append({'header': row['header'] if isinstance(row['header'], str) else '',
                                          'text': row['text']})
    public_documents = defaultdict(list)
    pdf_paths = set()
    for row in documents:
        pdf_path = None
        if row['format'] == 'pdf':
            pdf_path, _ = _copy_asset(stage, upload, row['pdf_original'], row['source_sha256'], pdf=True)
            pdf_paths.add(pdf_path)
        elif row['format'] != 'html':
            raise StaticExportError('Invalid bulletin document format.')
        public_documents[row['issue_id']].append({
            'id': row['source_sha256'], 'format': row['format'],
            'filename': row['source_filename'].replace('\\', '/').rsplit('/', 1)[-1],
            'claimed_issue_date': _iso(row['claimed_issue_date']), 'issue_date': _iso(row['issue_date']),
            'pdf_path': pdf_path, 'source_urls': sorted(document_sources[row['id']]),
            'warnings': sorted(warnings[row['id']]),
        })
    catalog = {'items': [], 'occurrences': []}
    details = {}
    search = []
    issue_counts = defaultdict(int)
    occurrence_ids = set()
    for row in occurrences:
        if (row['kind'] not in {'summary', 'detail', 'notice'}
                or not isinstance(row['title'], str) or not isinstance(row['body_text'], str)
                or (row['category'] is not None and not isinstance(row['category'], str))):
            raise StaticExportError('Invalid public bulletin occurrence.')
        key = group_keys[row['id']]
        group = groups[key]
        # An evidence-free group still needs document sources and unknown expiry.
        if 'expiry' not in group:
            group.update(sources=sorted(document_sources[row['document_id']]), expiry=None)
        occurrence_id = occurrence_public_ids[row['id']]
        if occurrence_id in occurrence_ids:
            raise StaticExportError('Public occurrence identity collision.')
        occurrence_ids.add(occurrence_id)
        item_id = item_ids[row['item_id']]
        refs = json.loads(row['image_refs_json'])
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise StaticExportError('Invalid bulletin image references.')
        images = [assets[(row['document_id'], ref)] for ref in dict.fromkeys(refs)
                  if (row['document_id'], ref) in assets]
        missing = any((row['document_id'], ref) not in assets for ref in refs)
        if missing:
            for document in public_documents[row['issue_id']]:
                if document['id'] == row['source_sha256']:
                    document['warnings'] = sorted(set(document['warnings']) | {'image_not_acquired'})
        sources = group['sources']
        category = row['category']
        if row['document_id'] in html_categories and category not in html_categories[row['document_id']]:
            category = None
        activity = current_activities.get(row['id'])
        review = date_reviews.get(row['id'])
        replaced = {correction['original_url'] for correction in source_corrections[row['id']]}
        if row['id'] in held_rows:
            content_status = 'needs_review'
        elif replaced and (review is None or any(source['url'] in replaced
                                                for source in review['source_review']['sources'])):
            content_status = 'source_corrected'
        elif review is None:
            content_status = 'unreviewed'
        elif review['rules_version'] != RULES_VERSION:
            content_status = 'legacy'
        elif review['source_review']['status'] == 'source_conflict':
            content_status = 'conflict'
        else:
            content_status = 'verified' if activity is not None else 'unconfirmed'
        filter_colleges = activity['colleges'] if activity is not None else group['colleges']
        public = {
            'id': occurrence_id, 'item_id': item_id, 'issue_id': issue_ids[row['issue_id']],
            'document_id': row['source_sha256'], 'kind': row['kind'], 'title': row['title'],
            'category': category, 'preview': (activity['summary']['value'] if activity else row['body_text'])[:240],
            'content_status': content_status,
            'issue_date': _iso(row['issue_date']), 'source_urls': sources,
            'source_hosts': sorted({urlsplit(url).hostname.lower() for url in sources}),
            'colleges': sorted({c['name'] for c in filter_colleges}),
            'dates': [] if row['id'] in held_rows else [
                {k: d[k] for k in ('kind', 'normalized_date', 'resolution', 'literal_source')} for d in group['dates']],
            'expiry_date': None if row['id'] in held_rows else group['expiry'],
            'image_count': len(images) + len(primary_images.get(row['id'], [])),
        }
        if content_status == 'verified':
            public['activity_facts'] = {field: activity[field]['value']
                                        for field in ('venue', 'audience', 'participation', 'time')}
        if source_corrections[row['id']]:
            public['source_corrections'] = source_corrections[row['id']]
        catalog['occurrences'].append(public)
        locator = {}
        if page := _positive(row['page']):
            locator['page'] = page
        if isinstance(row['anchor'], str) and row['anchor']:
            locator['anchor'] = row['anchor']
        occurrence = dict(public, body_text=row['body_text'], dates=group['dates'],
                          college_mentions=group['colleges'], images=images,
                          original_dates=original_dates[row['id']], primary_images=primary_images.get(row['id'], []),
                          locator=locator, table_cells=table_cells[row['id']], date_review=date_reviews.get(row['id']))
        if item_id not in details:
            details[item_id] = {'id': item_id, 'latest_occurrence_id': occurrence_id, 'occurrences': []}
        details[item_id]['occurrences'].append(occurrence)
        search_text = row['body_text']
        if activity is not None:
            search_text += '\n' + '\n'.join(activity[field]['value'] for field in
                ('title', 'summary', 'content', 'time', 'venue', 'organizer', 'audience', 'participation')
                if activity[field]['value'] is not None)
        search.append({'id': occurrence_id, 'title': row['title'], 'text': search_text})
        issue_counts[row['issue_id']] += 1
    if len(details) != len(items):
        raise StaticExportError('Bulletin logical item has no occurrence.')
    shards = defaultdict(dict)
    for item_id in sorted(details):
        detail = details[item_id]
        bucket = item_id[5:7]
        shards[bucket][item_id] = detail
        catalog['items'].append({'id': item_id, 'latest_occurrence_id': detail['latest_occurrence_id'],
                                 'occurrence_count': len(detail['occurrences']), 'aliases': sorted(item_aliases[item_id]),
                                 'detail_path': 'items/' + bucket + '.json'})
    public_issues = [{
        'id': issue_ids[row['id']], 'title': row['title'], 'issue_date': _iso(row['issue_date']),
        'issue_number': row['issue_number'], 'occurrence_count': issue_counts[row['id']],
        'documents': public_documents[row['id']],
    } for row in issues]
    counts = {'items': len(items), 'occurrences': len(occurrences), 'issues': len(issues),
              'documents': len(documents), 'images': len(image_paths | primary_image_paths), 'pdfs': len(pdf_paths)}
    return catalog, public_issues, dict(shards), search, counts, sorted(image_paths | primary_image_paths | pdf_paths)


def _manifest_files(version):
    prefix = 'versions/' + version + '/'
    return {key: prefix + filename for key, filename in {
        'catalog': 'catalog.json', 'issues': 'issues.json',
        'search_documents': 'search-documents.json', 'search_index': 'search-index.json',
    }.items()}

def _manifest_paths(files, version, schema_version=2):
    """Validate version-pinned source or normalized deployment paths."""
    prefix = 'versions/' + version + '/'
    if schema_version == 3:
        if not isinstance(files, dict) or set(files) != {'issues', 'item_index', 'partitions', 'search_index'}:
            raise ValueError('Invalid deployment manifest files.')
        if (files['issues'] != prefix + 'issues.json'
                or files['item_index'] != prefix + 'item-index.json'
                or files['search_index'] != prefix + 'search-index.json'):
            raise ValueError('Invalid deployment manifest paths.')
        partitions = files['partitions']
        if not isinstance(partitions, dict) or any(
                not isinstance(key, str) or not re.fullmatch(r'issue-[0-9a-f]{24}|standalone', key)
                or value != prefix + 'facts/' + key + '.json'
                for key, value in partitions.items()):
            raise ValueError('Invalid partition mapping.')
        paths = [files['issues'], files['item_index'], files['search_index'], *partitions.values()]
    else:
        base = _manifest_files(version)
        if not isinstance(files, dict) or any(files.get(key) != value for key, value in base.items()):
            raise ValueError('Invalid manifest files.')
        extra = set(files) - set(base)
        if extra not in (set(), {'issue_catalogs', 'item_index'}):
            raise ValueError('Invalid manifest file pair.')
        paths = list(base.values())
        if extra:
            catalogs = files['issue_catalogs']
            if not isinstance(catalogs, dict) or any(not isinstance(key, str)
                    or not re.fullmatch(r'issue-[0-9a-f]{24}', key) for key in catalogs):
                raise ValueError('Invalid issue catalog mapping.')
            paths.extend([files['item_index'], *catalogs.values()])
    prefix = 'versions/' + version + '/'
    for relative in paths:
        if (not isinstance(relative, str) or not relative.startswith(prefix) or not relative.endswith('.json')
                or not re.fullmatch(r'[A-Za-z0-9_./-]+', relative)
                or any(part in ('', '.', '..') for part in relative.split('/'))):
            raise ValueError('Invalid versioned manifest path.')
    if len(set(paths)) != len(paths):
        raise ValueError('Duplicate manifest path.')
    return paths


def _existing_manifest(output, replace):
    if not output.exists():
        return None
    if not replace or not output.is_dir():
        raise StaticExportError('Output already exists; replacement requires a generated dataset.')
    manifest_path = output / 'manifest.json'
    _no_symlinks(manifest_path)
    try:
        if not manifest_path.is_file() or manifest_path.stat().st_size > 1024 * 1024:
            raise ValueError
        manifest = json.loads(manifest_path.read_bytes())
        version = manifest['data_version']
        if ((manifest['schema_version'], manifest['generator']) not in {(1, 'webnews-static-v1'), (2, GENERATOR), (3, 'nannanews-reader-v3')}
                or not isinstance(version, str) or not _HASH.fullmatch(version)):
            raise ValueError
        for relative in _manifest_paths(manifest['files'], version, manifest['schema_version']):
            _no_symlinks(output / relative)
    except (OSError, ValueError, KeyError, TypeError):
        raise StaticExportError('Replacement target has no valid generated manifest.') from None
    return manifest


def _publish(stage, output, version, previous):
    if previous is None:
        if output.exists():
            raise StaticExportError('Output appeared while export was being prepared.')
        os.rename(stage, output)
        return
    # Publish immutable version first, then atomically switch the manifest. Never
    # delete/rename the old dataset or unrelated files, and keep old links valid.
    if _existing_manifest(output, True) != previous:
        raise StaticExportError('Generated output changed during export.')
    versions = output / 'versions'
    _no_symlinks(versions)
    versions.mkdir(exist_ok=True)
    target = versions / version
    source = stage / 'versions' / version
    _no_symlinks(target)
    moved = False
    added = []
    try:
        if target.exists():
            if not target.is_dir():
                raise StaticExportError('Existing data version is not a directory.')
            manifest = json.loads((stage / 'manifest.json').read_bytes())
            generated = set(_manifest_paths(manifest['files'], version, manifest['schema_version'])) - set(_manifest_files(version).values())
            missing = []
            for candidate in source.rglob('*'):
                if candidate.is_file():
                    relative = candidate.relative_to(source)
                    existing = target / relative
                    _no_symlinks(existing)
                    generated_file = ('versions/' + version + '/' + relative.as_posix()) in generated
                    if not existing.exists() and (generated_file or manifest['schema_version'] == 3):
                        missing.append((candidate, existing))
                    elif not existing.is_file():
                        raise StaticExportError('Existing immutable version differs from exported content.')
                    elif generated_file and candidate.suffix == '.json':
                        if json.loads(existing.read_bytes()) != json.loads(candidate.read_bytes()):
                            raise StaticExportError('Existing immutable version differs from exported content.')
                    elif existing.read_bytes() != candidate.read_bytes():
                        raise StaticExportError('Existing immutable version differs from exported content.')
            for candidate, existing in missing:
                existing.parent.mkdir(parents=True, exist_ok=True)
                os.link(candidate, existing)
                added.append(existing)
        else:
            os.rename(source, target)
            moved = True
        os.replace(stage / 'manifest.json', output / 'manifest.json')
    except (OSError, ValueError):
        if moved:
            shutil.rmtree(target)
        for path in added:
            path.unlink(missing_ok=True)
        raise StaticExportError('Could not atomically publish the generated manifest.') from None


def export_static(output_dir, *, settings=None, replace=False, held_occurrence_ids=()):
    """Return counts/bytes/data_version/output after a read-only atomic export.

    ``bytes`` counts this generated dataset, excluding any separately built search
    index and retained historical versions.

    ``output_dir`` is the public data root containing manifest.json. Existing
    output is accepted only with ``replace=True`` and our valid manifest marker.
    Replacement switches the manifest atomically and retains historical versions.
    No database initialization, runtime connection, model, or network runs. Only
    category headings are read from verified original HTML; records are not reparsed.
    Explicit held occurrences protect their entire canonical activity: original
    evidence remains in detail records, but confirmed content and date filtering
    are withheld. This display gate never alters a stored model verdict or date.
    """
    if settings is None:
        from nannanews import runtime
        settings = runtime.settings
    held_occurrence_ids = frozenset(held_occurrence_ids)
    if any(not isinstance(value, str) or not re.fullmatch(r'occ-[0-9a-f]{24}', value)
           for value in held_occurrence_ids):
        raise StaticExportError('Held occurrences must use public occurrence identifiers.')
    output = Path(output_dir).expanduser().absolute()
    _no_symlinks(output)
    output = output.resolve()
    private = Path(settings.data_dir).resolve()
    database = Path(settings.database_path).resolve()
    upload = Path(settings.upload_dir).absolute()
    _no_symlinks(upload)
    upload = upload.resolve()
    if (output.is_relative_to(private) or private.is_relative_to(output)
            or database.is_relative_to(output) or output.is_relative_to(upload)
            or upload.is_relative_to(output)):
        raise StaticExportError('Public output must be separate from private data and uploads.')
    previous = _existing_manifest(output, replace)
    if previous is not None and previous['schema_version'] == 3:
        raise StaticExportError('A deployed v3 dataset cannot be replaced by a native source export.')
    if not database.is_file():
        raise StaticExportError('Bulletin database is unavailable.')
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.' + output.name + '-static-', dir=output.parent))
    try:
        payload = stage / 'payload'
        payload.mkdir()
        try:
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')  # One coherent SQLite read snapshot, including WAL.
                catalog, issues, shards, search, counts, assets = _read_public(
                    db, payload, upload, held_occurrence_ids)
                db.rollback()
        except (sqlite3.Error, KeyError, TypeError, json.JSONDecodeError):
            raise StaticExportError('Bulletin snapshot has incompatible public evidence.') from None
        files = {'catalog.json': catalog, 'issues.json': issues, 'search-documents.json': search}
        files.update({'items/' + bucket + '.json': shard for bucket, shard in shards.items()})
        digest = hashlib.sha256()
        digest.update(_json({'schema_version': 2, 'generator': GENERATOR, 'counts': counts,
                             'content_rules_version': RULES_VERSION}))
        for name, value in sorted(files.items()):
            digest.update(_json(name))
            digest.update(_json(value))
        for name in assets:
            digest.update(_json(name))
            with (payload / name).open('rb') as stream:
                digest.update(hashlib.file_digest(stream, 'sha256').digest())
        version = digest.hexdigest()
        prefix = 'versions/' + version + '/'
        for item in catalog['items']:
            item['detail_path'] = prefix + item['detail_path']
        for issue in issues:
            for document in issue['documents']:
                if document['pdf_path']:
                    document['pdf_path'] = prefix + document['pdf_path']
        # Images can be shared between occurrences; prefix each object once.
        seen = set()
        for shard in shards.values():
            for detail in shard.values():
                for occurrence in detail['occurrences']:
                    for image in chain(occurrence['images'], occurrence['primary_images']):
                        if image['id'] not in seen:
                            image['path'] = prefix + image['path']
                            seen.add(image['id'])
        for name, value in files.items():
            target = payload / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_json(value))
        versions = stage / 'versions'
        versions.mkdir()
        os.rename(payload, versions / version)
        manifest = {'schema_version': 2, 'generator': GENERATOR, 'data_version': version,
                    'content_rules_version': RULES_VERSION,
                    'generated_at': datetime.now(timezone.utc).isoformat(),
                    'counts': counts, 'files': _manifest_files(version)}
        (stage / 'manifest.json').write_bytes(_json(manifest))
        size = sum(path.stat().st_size for path in stage.rglob('*') if path.is_file())
        _publish(stage, output, version, previous)
        return {'counts': counts, 'bytes': size, 'data_version': version, 'output': str(output)}
    except OSError:
        raise StaticExportError('Static export filesystem operation failed.') from None
    finally:
        if stage.exists():
            shutil.rmtree(stage)
