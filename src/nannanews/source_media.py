"""Versioned public-source exclusions authorized for GIF and video only.

Exclusions are not missing-image failures and are never reviewed evidence.
Explicit uploads, retained static formats, and PDF-derived pages stay intact.
"""
import base64
import hashlib
import re
from urllib.parse import parse_qs, urlsplit

REMOTE_MEDIA_POLICY_VERSION = 'remote-gif-video-excluded-v1-20261017'
MEDIA_POLICY_INSTRUCTIONS = (
    'REMOTE SOURCE MEDIA POLICY: GIF and video files identified in source_media '
    'were intentionally excluded, not viewed, transcribed, or reviewed. Never infer '
    'their contents, dates, locations, participants, or other facts. Use only captured '
    'source text and actual supplied static images/PDF pages as primary evidence; '
    'if an essential fact exists only in excluded media, use the existing output '
    'contract\'s permitted unknown or unresolved outcome rather than inventing it. '
    'This policy does not permit new output fields or status values. '
    'Exclusion metadata is not a citation.'
)
_VIDEO_EXTENSIONS = ('.mp4', '.webm', '.mov', '.m4v', '.avi', '.mpeg', '.mpg', '.ogv', '.flv', '.m3u8')
_IMAGE_BRANDS = {b'avif', b'avis', b'heic', b'heix', b'hevc', b'hevx', b'mif1', b'msf1'}


def media_policy():
    return {'version': REMOTE_MEDIA_POLICY_VERSION, 'scope': 'remote_source', 'excluded_types': ['gif', 'video']}


def excluded_media_type(url='', *, declared_type='', content_type='', data=None):
    """Recognize declarations or actual GIF/video bytes, not all animation/media."""
    mime = str(content_type).split(';', 1)[0].strip().lower()
    if data is not None:
        if data.startswith((b'GIF87a', b'GIF89a')):
            return 'gif'
        # Known static image signatures take precedence over a wrong MIME header.
        if (data.startswith((b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff', b'BM', b'II*\x00', b'MM\x00*', b'II+\x00', b'MM\x00+'))
                or (data.startswith(b'RIFF') and data[8:12] == b'WEBP')):
            return None
        if mime.startswith('audio/'):
            return None
        if len(data) >= 16 and data[4:8] == b'ftyp':
            end = min(len(data), int.from_bytes(data[:4], 'big'), 4096)
            brands = {data[8:12]} | {data[index:index + 4] for index in range(16, end - 3, 4)}
            if brands & _IMAGE_BRANDS:
                return None
        # Container signatures alone do not distinguish audio-only files.
        if (data.startswith(b'\x00\x00\x01\xb3')
                or (len(data) >= 9 and data.startswith(b'FLV\x01') and data[4] & 1)):
            return 'video'
        if (data[:4096].lstrip().startswith((b'<', b'\xef\xbb\xbf'))
                and re.search(br'<(?:[A-Za-z_][\w.-]*:)?svg(?:\s|>)', data[:4096])):
            return None
    hint = str(declared_type).strip().lower()
    if mime == 'image/gif' or hint in {'gif', 'image/gif'}:
        return 'gif'
    if mime.startswith('video/') or hint == 'video' or hint.startswith('video/'):
        return 'video'
    if data is not None:
        # Once bytes are received, a misleading URL is not evidence of their type.
        return None
    try:
        parts = urlsplit(url or '')
    except ValueError:
        return None
    formats = {value.lower() for value in parse_qs(parts.query).get('wx_fmt', [])}
    if parts.path.lower().endswith('.gif') or formats == {'gif'}:
        return 'gif'
    if parts.path.lower().endswith(_VIDEO_EXTENSIONS) or formats in ({'mp4'}, {'webm'}):
        return 'video'
    return None


def skipped_media_record(kind, url, reason, *, data=None, complete=False):
    return {'type': kind, 'reason': reason, 'url': url, 'downloaded': data is not None,
            'digest': hashlib.sha256(data).hexdigest() if data is not None else None,
            'bytes': len(data) if data is not None else 0, 'complete': bool(complete)}


def merge_skipped_media(records, incoming):
    """One entry per original URL/type; received-byte evidence outranks a hint."""
    for index, existing in enumerate(records):
        if (existing['url'], existing['type']) == (incoming['url'], incoming['type']):
            if (incoming['downloaded'] and (not existing['downloaded']
                    or incoming['complete'] or incoming['bytes'] > existing['bytes'])):
                records[index] = incoming
            return
    records.append(incoming)


def apply_remote_media_policy(document, media):
    """Pure cached continuation: return new metadata/media, never mutate originals."""
    from nannanews import acquisition

    result = dict(document)
    skipped = [dict(item) for item in document.get('skipped_media', [])]
    images = list(document.get('images', []))
    for url in images:
        kind = excluded_media_type(url)
        if kind is not None:
            merge_skipped_media(skipped, skipped_media_record(kind, url, 'declared_' + kind + '_excluded'))
    excluded_urls = {item['url'] for item in skipped if item['url']}
    retained = []
    for image in media:
        if not image.get('url') or image.get('source_pdf_url'):
            retained.append(image)
            continue
        encoded = image.get('data')
        if not isinstance(encoded, str) or len(encoded) > 4 * ((acquisition.MAX_IMAGE + 2) // 3):
            raise ValueError('Cached remote media exceed their original byte limit.')
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise ValueError('Cached remote media bytes are invalid.') from None
        if not data or len(data) > acquisition.MAX_IMAGE or hashlib.sha256(data).hexdigest() != image.get('digest'):
            raise ValueError('Cached remote media digest differs from its received bytes.')
        kind = excluded_media_type(image['url'], content_type=image.get('mime', ''), data=data)
        if kind is not None:
            merge_skipped_media(skipped, skipped_media_record(kind, image['url'], 'received_' + kind + '_excluded', data=data, complete=True))
            excluded_urls.add(image['url'])
            continue
        # Actual static bytes override an earlier URL/declaration-only type hint.
        skipped = [item for item in skipped if item['url'] != image['url']]
        excluded_urls.discard(image['url'])
        if image['url'] not in images:
            images.append(image['url'])
        retained.append(image)
    images = [url for url in images if url not in excluded_urls]
    ordinary = [image for image in retained if image.get('url') and not image.get('source_pdf_url')]
    order = {url: index for index, url in enumerate(images)}
    ordinary.sort(key=lambda image: order[image['url']])
    other = [image for image in retained if not image.get('url') or image.get('source_pdf_url')]
    result.update(images=images, media=ordinary + other, media_policy=media_policy(), skipped_media=skipped)
    result['digest'] = acquisition._digest(result)
    return result, result['media']


def validate_media_policy(document, media):
    """Validate optional new policy metadata without altering legacy captures."""
    if 'media_policy' not in document and 'skipped_media' not in document:
        return
    if document.get('media_policy') != media_policy() or not isinstance(document.get('skipped_media'), list):
        raise ValueError('Public source media policy metadata are invalid.')
    seen = set()
    for item in document['skipped_media']:
        if (not isinstance(item, dict)
                or set(item) != {'type', 'reason', 'url', 'downloaded', 'digest', 'bytes', 'complete'}
                or item['type'] not in {'gif', 'video'} or not isinstance(item['reason'], str) or not item['reason']
                or not isinstance(item['url'], (str, type(None))) or (isinstance(item['url'], str) and len(item['url']) > 8192)
                or type(item['downloaded']) is not bool or type(item['complete']) is not bool
                or type(item['bytes']) is not int or not 0 <= item['bytes'] <= 32 * 1024 * 1024):
            raise ValueError('Skipped source media metadata are invalid.')
        key = item['type'], item['url']
        if key in seen:
            raise ValueError('Skipped source media metadata are duplicated.')
        seen.add(key)
        if item['downloaded']:
            if not isinstance(item['digest'], str) or re.fullmatch('[0-9a-f]{64}', item['digest']) is None:
                raise ValueError('Received excluded media do not retain their actual byte digest.')
        elif item['digest'] is not None or item['bytes'] != 0 or item['complete']:
            raise ValueError('Undownloaded excluded media claim received-byte evidence.')
    excluded_urls = {item['url'] for item in document['skipped_media'] if item['url']}
    if any(url in excluded_urls for url in document.get('images', [])):
        raise ValueError('Excluded media remain in the static source-image list.')
    for image in media:
        if not image.get('url') or image.get('source_pdf_url'):
            continue
        encoded = image.get('data')
        try:
            if not isinstance(encoded, str):
                raise ValueError
            prefix = base64.b64decode(encoded[:5504], validate=True)
        except ValueError:
            raise ValueError('Retained source media bytes are invalid.') from None
        if (image['url'] in excluded_urls
                or excluded_media_type(content_type=image.get('mime', ''), data=prefix) is not None):
            raise ValueError('Excluded source media remain in reviewed media.')
