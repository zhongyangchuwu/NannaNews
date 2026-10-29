"""Bounded, offline normalization of private historical review evidence.

Inputs are immutable private evidence, not a new activity catalogue. Only existing
activity identities are linked. Exact model requests, responses and receipts are
retained privately; readable source evidence is distinct from those wire bytes.
"""
from __future__ import annotations

import base64
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import stat
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from defusedxml import ElementTree as SafeXML
from defusedxml.common import DefusedXmlException

from nannanews.data_foundation import connect_foundation, normalize_date_claim
from nannanews.data_foundation_blobs import BlobStore


# A record/envelope, never a batch, is the unit of resident memory. Oversized or
# unknown inputs remain held and cannot become cleanup candidates.
MAX_RECORD_BYTES = 128 * 1024 * 1024
MAX_SEMANTIC_BYTES = 4 * 1024 * 1024
MAX_ERRORS = 100
IMAGE_MIMES = {'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp',
               'image/tiff', 'image/svg+xml'}
LEGACY_REFS = ('activity_id', 'item_id', 'source_id', 'local_key', 'occurrence_id', 'document_sha256')
ACTIVITY_FIELDS = ('title', 'summary', 'content', 'time', 'venue', 'organizer',
                   'audience', 'participation', 'colleges', 'image_indices')
CLAIM_FIELDS = ('kind', 'raw_text', 'raw_text_sha256', 'raw_text_omitted',
                'normalized_date', 'date', 'resolution', 'evidence', 'locator',
                'quote', 'url', 'source', 'source_id', 'image_index')
REVIEW_FIELDS = set(LEGACY_REFS) | {
    'input_sha256', 'related_context_sha256', 'context_sha256', 'rules_version',
    'reviewer', 'status', 'reason', 'claims', 'source_review', 'activity', 'fields',
    'parsed_result', 'answer', 'output_text', 'error', 'error_summary', 'workflow',
    '_model_failure_code', 'capture_artifact', '_model_audit', 'origin',
    '_paid_failure', '_receipt_finalize_failed', 'progress',
}
SOURCE_REVIEW_FIELDS = {'status', 'reason', 'activity', 'sources', 'attempts'}


class HeldInput(ValueError):
    """The input cannot safely authorize removal of its source."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _fingerprint(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise HeldInput('Input is not a regular file.')
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if _signature(before) != _signature(after):
        raise HeldInput('Source changed while computing its digest.')
    return digest.hexdigest(), before


@contextmanager
def _reading_input(path, expected_digest, expected_info):
    """Bind consumed bytes and descriptor identity to the original fingerprint."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or _signature(before) != _signature(expected_info):
            raise HeldInput('Source identity changed before parsing; original retained.')
        consumed = hashlib.sha256()
        yield stream, consumed
        after = os.fstat(stream.fileno())
        current = path.stat(follow_symlinks=False)
        if (consumed.hexdigest() != expected_digest
                or _signature(after) != _signature(expected_info)
                or _signature(current) != _signature(expected_info)):
            raise HeldInput('Source bytes or identity changed during parsing; cleanup refused.')


def _load_json(path, *, expected_digest, expected_info):
    if expected_info.st_size > MAX_RECORD_BYTES:
        raise HeldInput('JSON envelope exceeds the bounded record limit.')
    with _reading_input(path, expected_digest, expected_info) as (stream, consumed):
        raw = stream.read(MAX_RECORD_BYTES + 1)
        consumed.update(raw)
        if len(raw) > MAX_RECORD_BYTES:
            raise HeldInput('JSON envelope exceeds the bounded record limit.')
    return json.loads(raw)


def _walk(path):
    """Walk lazily without following links or materializing a 124 GB tree."""
    if path.is_dir() and not path.is_symlink():
        with os.scandir(path) as entries:
            for entry in entries:
                yield from _walk(Path(entry.path))
    else:
        yield path


def _safe_url(value):
    if not isinstance(value, str) or len(value) > 8192:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {'https', 'http'} or not parts.hostname or parts.username or parts.password:
            return None
        # Credentials are not source identity, including historical signed URLs.
        if re.search(r'(?i)(?:^|&)(?:access_token|api_key|token|password|secret|signature)=', parts.query):
            return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))
    except ValueError:
        return None


def _message(value):
    text = str(value or '')
    text = re.sub(r'(?i)(bearer\s+)[\w.\-]+', r'\1[redacted]', text)
    text = re.sub(r'(?i)((?:api[_-]?key|authorization|password|cookie|access_token|secret)\s*[=:]\s*)[^\s,;]+',
                  r'\1[redacted]', text)
    return text[:2000]


def _error(stage, code, reason, url=None):
    if isinstance(reason, dict):
        code = reason.get('code') or code
        reason = reason.get('reason') or reason.get('message') or 'Stored error has no human-readable reason.'
    result = {'stage': stage, 'code': str(code or 'unknown')[:100], 'reason': _message(reason)}
    if _safe_url(url):
        result['url'] = _safe_url(url)
    return result


def _claim(value):
    if not isinstance(value, dict):
        raise HeldInput('Parsed date claims must be objects.')
    normalized = normalize_date_claim(value)
    return {key: _semantic(normalized[key]) for key in CLAIM_FIELDS if key in normalized}


def _semantic(value):
    """Allow only explicit review fields, never arbitrary transport dictionaries."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    if isinstance(value, dict):
        allowed = set(ACTIVITY_FIELDS) | {'value', 'evidence', 'text', 'url', 'image_index', 'ref', 'id',
                   'name', 'college', 'kind', 'raw_text', 'raw_text_sha256', 'raw_text_omitted',
                   'normalized_date', 'date', 'resolution', 'locator', 'quote',
                   'source', 'source_id', 'page', 'line', 'start', 'end', 'label',
                   'status', 'reason', 'code', 'claims', 'dates', 'citations',
                   'source_reason', 'source_status', 'activity'}
        result = {}
        for key, item in value.items():
            if key not in allowed:
                continue
            if key in {'claims', 'dates'}:
                if not isinstance(item, list):
                    raise HeldInput('Parsed date claims must be lists.')
                result[key] = [_claim(claim) for claim in item]
            else:
                result[key] = _semantic(item)
        return result
    raise HeldInput('Review fields contain a non-JSON value.')


def _meaningful(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_meaningful(item) for item in value.values())
    if isinstance(value, list):
        return any(_meaningful(item) for item in value)
    return isinstance(value, (bool, int, float))


def _visible_answer(value):
    if not isinstance(value, dict):
        return None
    for key in ('answer', 'output_text'):
        if isinstance(value.get(key), str) and value[key].strip():
            return value[key]
    texts = []
    for message in value.get('output', []) if isinstance(value.get('output'), list) else []:
        if not isinstance(message, dict) or message.get('type') != 'message' or message.get('role') != 'assistant':
            continue
        for part in message.get('content', []):
            if isinstance(part, dict) and part.get('type') in {'output_text', 'refusal'}:
                text = part.get('text') or part.get('refusal')
                if isinstance(text, str):
                    texts.append(text)
    for choice in value.get('choices', []) if isinstance(value.get('choices'), list) else []:
        if not isinstance(choice, dict) or not isinstance(choice.get('message'), dict):
            continue
        message = choice['message']
        content = message.get('content')
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(part['text'] for part in content if isinstance(part, dict)
                         and part.get('type') == 'text' and isinstance(part.get('text'), str))
        if isinstance(message.get('refusal'), str):
            texts.append(message['refusal'])
    return '\n'.join(texts) or None


def _wire_shape(value):
    if not isinstance(value, dict):
        return False
    if isinstance(value.get('error'), dict) and any(isinstance(value['error'].get(key), str) for key in ('message', 'code')):
        return True
    return ((isinstance(value.get('choices'), list) or isinstance(value.get('output'), list))
            and (isinstance(value.get('id'), str) or isinstance(value.get('model'), str)
                 or value.get('object') in {'response', 'chat.completion'}))


def _reject_page(document):
    title = str(document.get('title') or '').strip()
    resolved = str(document.get('resolved_url') or document.get('canonical_url') or document.get('url') or '')
    text = str(document.get('text') or '')[:8000]
    try:
        parts = urlsplit(resolved)
        auth_path = re.search(r'(?i)(?:^|/)(?:authserver|login|signin|captcha|wappoc_appmsgcaptcha)(?:/|$|\.)', parts.path)
        auth_host = (parts.hostname or '').lower().split('.')[0] in {'authserver', 'sso', 'login'}
    except ValueError:
        auth_path = auth_host = False
    if auth_path or auth_host or re.search(r'统一身份认证|身份认证平台|(?:^|\s)(?:Sign in|Log in)(?:\s|$)|验证码|人机验证|安全验证', title, re.I):
        return 'Source is an authentication/login/captcha page, not activity content.'
    if len(text) < 4000 and re.search(r'请先登录|登录后(?:查看|访问)|请输入.{0,12}(?:密码|验证码)|请完成.{0,12}验证|访问过于频繁|仅限校园网|环境异常', text):
        return 'Source content is unavailable behind login, access control or verification.'
    return None


def _decode_media(image):
    if not isinstance(image, dict):
        raise HeldInput('Captured image has an unsupported shape.')
    mime = str(image.get('mime') or '').lower().split(';')[0]
    encoded = image.get('data')
    if mime not in IMAGE_MIMES | {'application/octet-stream'} or not isinstance(encoded, str) or len(encoded) > MAX_RECORD_BYTES:
        raise HeldInput('Captured image has unsupported media or exceeds its byte limit.')
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise HeldInput('Captured image base64 is malformed.') from None
    digest = hashlib.sha256(data).hexdigest()
    expected = image.get('digest') or image.get('sha256')
    if not data or not isinstance(expected, str) or expected.lower() != digest:
        raise HeldInput('Captured image digest does not match its bytes.')
    svg = False
    if mime in {'image/svg+xml', 'application/octet-stream'} and len(data) <= 2 * 1024 * 1024:
        try:
            root = SafeXML.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
            svg = root.tag in {'svg', '{http://www.w3.org/2000/svg}svg'}
        except (SafeXML.ParseError, DefusedXmlException):
            pass
    # Archive original bytes only. SVG is never rendered or executed here;
    # future consumers still require their normal safe-media admission checks.
    signatures = {'image/png': data.startswith(b'\x89PNG\r\n\x1a\n'),
                  'image/jpeg': data.startswith(b'\xff\xd8\xff'),
                  'image/gif': data.startswith((b'GIF87a', b'GIF89a')),
                  'image/webp': data.startswith(b'RIFF') and data[8:12] == b'WEBP',
                  'image/bmp': data.startswith(b'BM'),
                  'image/tiff': data.startswith((b'II*\x00', b'MM\x00*', b'II+\x00', b'MM\x00+')),
                  'image/svg+xml': svg}
    if mime == 'application/octet-stream':
        mime = next((kind for kind, matches in signatures.items() if matches), '')
    if not signatures.get(mime):
        raise HeldInput('Captured image bytes do not match the declared media type.')
    return data, mime


class _Extractor:
    def __init__(self, db, store, source):
        self.db, self.store, self.source = db, store, str(source)
        self.records = 0
        self.snapshots = 0
        self.errors = []
        self.error_count = 0
        self.hold_original = False

    def error(self, value):
        self.error_count += 1
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(value)

    def output(self, kind, identity):
        return self.db.execute('INSERT OR IGNORE INTO ingestion_outputs(source_path,kind,row_id) VALUES(?,?,?)',
                               (self.source, kind, identity)).rowcount

    def blob(self, data, *, mime, kind):
        identity = self.store.put_bytes(data, mime=mime, kind=kind)
        self.output('blob', identity)
        return identity

    def identity(self, value):
        matches = set()
        for key in ('activity_id', 'item_id'):
            identity = value.get(key)
            if not isinstance(identity, str) or identity.isdecimal():
                continue
            row = self.db.execute('SELECT activity_id FROM activities WHERE activity_id=?', (identity,)).fetchone()
            if row is None:
                row = self.db.execute('SELECT activity_id FROM activity_aliases WHERE alias_id=?', (identity,)).fetchone()
            if row is not None and row[0]:
                matches.add(row[0])
        occurrence = value.get('occurrence_id')
        candidates = {occurrence} if isinstance(occurrence, str) and not occurrence.isdecimal() else set()
        document_sha, local_key = value.get('document_sha256'), value.get('local_key')
        if isinstance(document_sha, str) and re.fullmatch('[0-9a-f]{64}', document_sha) and isinstance(local_key, str) and local_key:
            # This is the existing static export's identity, not a new matching
            # heuristic. Bare numeric IDs are never portable across DB clones.
            candidates.add('occ-' + hashlib.sha256((document_sha + ':' + local_key).encode('utf-8')).hexdigest()[:24])
        rows = {}
        for candidate in candidates:
            row = self.db.execute('SELECT activity_id,source_id FROM source_records WHERE occurrence_id=?', (candidate,)).fetchone()
            if row:
                rows[row['source_id']] = row
                if row['activity_id']:
                    matches.add(row['activity_id'])
        source = next(iter(rows)) if len(rows) == 1 else None
        if len(matches) > 1:
            self.error(_error('identity', 'conflicting_legacy_links', 'Legacy IDs point to different existing activities; link left empty.'))
        return (next(iter(matches)) if len(matches) == 1 else None), source

    def source_response(self, document, *, snapshot_id=None):
        """Keep original HTTP bytes privately, never relabel them as article images."""
        if not isinstance(document, dict) or 'raw_response_base64' not in document:
            return None
        encoded = document['raw_response_base64']
        if not isinstance(encoded, str) or len(encoded) > MAX_RECORD_BYTES:
            raise HeldInput('Captured response exceeds the bounded byte limit.')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise HeldInput('Captured response base64 is malformed; original retained.') from None
        actual = self.blob(raw, mime='application/octet-stream', kind='source_http_response')
        metadata = {key: document[key] for key in ('canonical_url', 'resolved_url', 'content_type',
                    'content_encoding', 'http_status', 'fetched_at', 'raw_response_complete', 'image_index') if key in document}
        metadata.update(format='nannanews-source-response-v1', raw_response_blob_sha=actual,
                        declared_sha256=document.get('raw_response_digest'),
                        digest_verified=actual == document.get('raw_response_digest'))
        if not metadata['digest_verified']:
            self.error(_error('source', 'response_digest_unverified', 'Original response bytes retained; declared digest is absent or differs.'))
        metadata_sha = self.blob(_json(metadata).encode('utf-8'), mime='application/json', kind='source_response_metadata')
        if snapshot_id is None:
            url = _safe_url(document.get('canonical_url')) or _safe_url(document.get('resolved_url'))
            if not url:
                return None
            snapshot_id = 'snapshot:' + _digest(metadata)
            self.db.execute('''INSERT OR IGNORE INTO snapshots
                (snapshot_id,url,title,text_blob_sha,fetched_at,status,manifest_json)
                VALUES(?,?,?,NULL,?,'failed',?)''',
                (snapshot_id, url, '', document.get('fetched_at'), _json(metadata)))
            self.snapshots += self.output('snapshot', snapshot_id)
        for digest, role in ((actual, 'raw_http'), (metadata_sha, 'response_metadata')):
            self.db.execute('INSERT OR IGNORE INTO snapshot_blobs(snapshot_id,blob_sha,role,ordinal) VALUES(?,?,?,0)',
                            (snapshot_id, digest, role))
        return snapshot_id

    def snapshot(self, document, media=(), *, media_ordinals=None):
        if not isinstance(document, dict):
            raise HeldInput('Source document must be an object.')
        rejection = _reject_page(document)
        if rejection:
            error = _error('source', 'source_unavailable', rejection,
                           document.get('url') or document.get('canonical_url'))
            self.error(error)
            return self.source_response(document), error
        url = _safe_url(document.get('resolved_url')) or _safe_url(document.get('canonical_url')) or _safe_url(document.get('url'))
        text, title = document.get('text', ''), document.get('title', '')
        if not url or not isinstance(text, str) or not isinstance(title, str):
            raise HeldInput('Source lacks a readable public URL/title/text shape.')
        images = list(media)
        if not images:
            images = [image for image in document.get('images', []) if isinstance(image, dict)]
        if media_ordinals is not None and (len(media_ordinals) != len(images)
                or any(type(value) is not int or value < 0 for value in media_ordinals)
                or len(set(media_ordinals)) != len(media_ordinals)):
            raise HeldInput('Captured media ordinals must be distinct nonnegative positions matching the media.')
        image_records, failed_media = [], []
        for index, image in enumerate(images):
            ordinal = index if media_ordinals is None else media_ordinals[index]
            try:
                data, mime = _decode_media(image)
            except HeldInput as error:
                self.hold_original = True
                item = image if isinstance(image, dict) else {}
                failure = {'ordinal': ordinal, 'url': _safe_url(item.get('url')),
                    'declared_mime': item.get('mime'),
                    'declared_sha256': item.get('digest') or item.get('sha256'),
                    'sha256': None, 'digest_verified': False, 'error': str(error)}
                encoded = item.get('data')
                if isinstance(encoded, str) and len(encoded) <= MAX_RECORD_BYTES:
                    try:
                        raw = base64.b64decode(encoded, validate=True)
                    except ValueError:
                        pass
                    else:
                        failure['sha256'] = self.blob(raw, mime='application/octet-stream', kind='source_failed_media')
                        failure['digest_verified'] = failure['sha256'] == failure['declared_sha256']
                failed_media.append(failure)
                continue
            digest = self.blob(data, mime=mime, kind='source_image')
            image_records.append({'sha256': digest, 'mime': mime,
                                  'url': _safe_url(image.get('url')), 'ordinal': ordinal})
        if not text.strip() and not image_records and not failed_media:
            raise HeldInput('Source has no readable text or captured activity image.')
        pdf_records = []
        pdfs = document.get('pdf_documents', [])
        if not isinstance(pdfs, list):
            raise HeldInput('Retained PDF catalog must be a list.')
        for ordinal, pdf in enumerate(pdfs):
            if not isinstance(pdf, dict) or not isinstance(pdf.get('raw_response_base64'), str):
                raise HeldInput('Retained PDF has an unsupported shape.')
            try:
                data = base64.b64decode(pdf['raw_response_base64'], validate=True)
            except ValueError:
                raise HeldInput('Retained PDF base64 is malformed.') from None
            if not data.startswith(b'%PDF-') or hashlib.sha256(data).hexdigest() != pdf.get('raw_response_digest'):
                raise HeldInput('Retained PDF digest or signature is invalid.')
            digest = self.blob(data, mime='application/pdf', kind='source_pdf')
            pdf_records.append({'sha256': digest, 'url': _safe_url(pdf.get('canonical_url')),
                                'ordinal': ordinal})
        text_sha = self.blob(text.encode('utf-8'), mime='text/plain; charset=utf-8', kind='source_text') if text else None
        semantic = {'url': url, 'title': title, 'text_blob_sha': text_sha, 'images': image_records,
                    'pdfs': pdf_records,
                    'published_date': document.get('published_date') or document.get('source_published_date'),
                    'source_type': document.get('source_type'), 'content_type': document.get('content_type')}
        if failed_media:
            semantic['failed_media'] = failed_media
        snapshot_id = 'snapshot:' + _digest(semantic)
        # Capture timestamps, raw HTTP digests, quotes and image_index are NOT
        # content identity. Repeated captures of the same source share one graph.
        manifest = dict(semantic, format='nannanews-readable-source-v1',
                        capture_complete=False if failed_media else document.get('capture_complete'))
        errors = document.get('capture_errors') or []
        if not isinstance(errors, list):
            raise HeldInput('Source capture errors have an unsupported shape.')
        for error in errors:
            if isinstance(error, dict):
                self.error(_error('source', error.get('code'), error.get('reason') or error.get('message'), url))
            else:
                self.error(_error('source', 'capture_error', error, url))
        self.db.execute('''INSERT OR IGNORE INTO snapshots
            (snapshot_id,url,title,text_blob_sha,fetched_at,status,manifest_json)
            VALUES(?,?,?,?,?,?,?)''', (snapshot_id, url, title, text_sha, document.get('fetched_at'),
                                      'partial' if failed_media and (text.strip() or image_records) else 'failed' if failed_media else 'captured', _json(manifest)))
        if text_sha:
            self.db.execute('INSERT OR IGNORE INTO snapshot_blobs(snapshot_id,blob_sha,role,ordinal) VALUES(?,?,?,?)',
                            (snapshot_id, text_sha, 'text', 0))
        for image in image_records:
            self.db.execute('INSERT OR IGNORE INTO snapshot_blobs(snapshot_id,blob_sha,role,ordinal) VALUES(?,?,?,?)',
                            (snapshot_id, image['sha256'], 'image', image['ordinal']))
        self.snapshots += self.output('snapshot', snapshot_id)
        for pdf in pdf_records:
            self.db.execute('INSERT OR IGNORE INTO snapshot_blobs(snapshot_id,blob_sha,role,ordinal) VALUES(?,?,?,?)',
                            (snapshot_id, pdf['sha256'], 'pdf', pdf['ordinal']))
        for item in failed_media:
            if item['sha256']:
                self.db.execute('INSERT OR IGNORE INTO snapshot_blobs(snapshot_id,blob_sha,role,ordinal) VALUES(?,?,?,?)',
                                (snapshot_id, item['sha256'], 'failed_media', item['ordinal']))
        self.source_response(document, snapshot_id=snapshot_id)
        for error in errors:
            if isinstance(error, dict) and isinstance(error.get('source_response'), dict):
                self.source_response(dict(error['source_response'], image_index=error.get('image_index')), snapshot_id=snapshot_id)
        error = _error('source', 'failed_media', failed_media[0]['error'], url) if failed_media else None
        if error:
            self.error(error)
        return snapshot_id, error

    def review(self, record, *, answer=None, extra_errors=(), paid=None, wire=None, source_snapshots=()):
        if not isinstance(record, dict):
            raise HeldInput('Review record must be an object.')
        if set(record) - REVIEW_FIELDS:
            raise HeldInput('Unsupported review fields; unrecognized evidence must be retained in its source.')
        source_review = record.get('source_review', {})
        if not isinstance(source_review, dict):
            raise HeldInput('Review source_review must be an object, not a null or malformed wrapper.')
        if set(source_review) - SOURCE_REVIEW_FIELDS:
            raise HeldInput('Unsupported source_review fields; original evidence retained.')
        for node in (record, source_review):
            for key in ('status', 'reason'):
                if node.get(key) is not None and not isinstance(node[key], str):
                    raise HeldInput('Review status and reason must be text when supplied.')
        activity = source_review.get('activity')
        if activity is None:
            activity = record.get('activity')
        if activity is None:
            activity = {}
        if not isinstance(activity, dict):
            raise HeldInput('Parsed activity fields must be an object.')
        claims = record.get('claims', [])
        if not isinstance(claims, list) or any(not isinstance(claim, dict) for claim in claims):
            raise HeldInput('Review claims must be a list of objects.')
        fields = {'legacy_refs': {key: record[key] for key in LEGACY_REFS if isinstance(record.get(key), str) or type(record.get(key)) is int},
                  'activity': {key: _semantic(activity[key]) for key in ACTIVITY_FIELDS if key in activity},
                  'claims': [_claim(claim) for claim in claims],
                  'source_status': source_review.get('status'), 'source_reason': _message(source_review.get('reason'))}
        if isinstance(record.get('fields'), dict):
            fields['field_checks'] = _semantic(record['fields'])
        if isinstance(record.get('parsed_result'), dict):
            fields['parsed_result'] = _semantic(record['parsed_result'])
        if 'workflow' in record:
            workflow = record['workflow']
            allowed = {'state', 'actor', 'dataset', 'request_state', 'request_key', 'admission_code'}
            if (not isinstance(workflow, dict) or set(workflow) - allowed
                    or any(value is not None and not isinstance(value, str) for value in workflow.values())):
                raise HeldInput('Workflow provenance must contain only explicit text metadata.')
            fields['workflow'] = dict(workflow)
        retained = {}
        if isinstance(record.get('capture_artifact'), str):
            retained['capture_artifact'] = record['capture_artifact']
        if isinstance(record.get('_model_audit'), dict):
            retained['model_audit'] = {key: record['_model_audit'][key] for key in ('receipt', 'request_sha256')
                                       if isinstance(record['_model_audit'].get(key), str)}
        if retained:
            fields['retained_evidence'] = retained
        if paid:
            fields['paid_facts'] = paid
        if wire is not None:
            fields['_model_wire'] = wire
        errors = list(extra_errors)
        for key in ('error', 'error_summary', '_model_failure_code'):
            if record.get(key):
                detail = record[key]
                if isinstance(detail, dict) and not any(_meaningful(detail.get(name)) for name in ('code', 'reason', 'message')):
                    raise HeldInput('Review error has no recognized error code or explanation.')
                if not isinstance(detail, (str, dict)):
                    raise HeldInput('Review error must contain readable text or a recognized error object.')
                if isinstance(detail, str) and not detail.strip():
                    raise HeldInput('Review error text must contain a readable explanation.')
                errors.append(_error('model', key, record[key]))
        attempts = source_review.get('attempts', [])
        if not isinstance(attempts, list):
            raise HeldInput('Review source attempts must be a list.')
        for attempt in attempts:
            if not isinstance(attempt, dict):
                raise HeldInput('Source acquisition attempt must be an object.')
            if not isinstance(attempt.get('reason'), str) or not attempt['reason'].strip() or not isinstance(attempt.get('code') or attempt.get('status'), str):
                raise HeldInput('Source acquisition attempt lacks a recorded error code or explanation.')
            errors.append(_error('source', attempt.get('code') or attempt.get('status'),
                                 attempt.get('reason'), attempt.get('url')))
        snapshots = list(source_snapshots)
        sources = source_review.get('sources', [])
        if not isinstance(sources, list):
            raise HeldInput('Review sources must be a list.')
        source_evidence = []
        for source in sources:
            snapshot, error = self.snapshot(source)
            if snapshot:
                snapshots.append(snapshot)
                source_evidence.append({'snapshot_id': snapshot,
                    'quotes': _semantic(source.get('quotes', [])),
                    'quote_evidence': _semantic(source.get('quote_evidence', [])),
                    'image_refs': [{'image_index': image.get('image_index'),
                                    'sha256': image.get('sha256') or image.get('digest')}
                                   for image in source.get('images', []) if isinstance(image, dict)]})
            if error:
                errors.append(error)
        fields['source_evidence'] = source_evidence
        for error in errors:
            self.error(error)
        activity_id, source_id = self.identity(record)
        status = str(record.get('status') or source_review.get('status') or 'unresolved')
        reason = _message(record.get('reason') or source_review.get('reason'))
        encoded = _json(fields)
        if len(encoded.encode('utf-8')) > MAX_SEMANTIC_BYTES:
            raise HeldInput('Useful review fields exceed the bounded semantic record limit.')
        actual_answer = answer or _visible_answer(record)
        if actual_answer is not None and (not isinstance(actual_answer, str) or not actual_answer.strip()):
            actual_answer = None
        # IDs, status-only wrappers and our explanatory defaults are not review
        # evidence. Check before synthesizing an answer or saving a review row.
        useful_fields = any(_meaningful(fields.get(key)) for key in ('activity', 'claims', 'field_checks', 'parsed_result'))
        paid_attempt = paid and _meaningful(paid.get('request_key')) and _meaningful(paid.get('state'))
        if not (actual_answer or _meaningful(reason) or useful_fields or errors or snapshots or paid_attempt):
            raise HeldInput('Review contains no meaningful recognized result, answer or error evidence; original retained.')
        fields['answer_kind'] = 'visible_answer' if actual_answer else 'parsed_result'
        if not actual_answer:
            actual_answer = 'Parsed review result (not a verbatim model answer)\n' + _json(
                {'status': status, 'reason': reason, 'fields': fields, 'errors': errors})
        if len(actual_answer.encode('utf-8')) > MAX_RECORD_BYTES:
            raise HeldInput('Readable model answer exceeds the bounded answer limit.')
        answer_sha = self.blob(actual_answer.encode('utf-8'), mime='text/plain; charset=utf-8', kind='review_answer')
        identity_fields = {'fields': fields, 'status': status, 'reason': reason, 'answer': answer_sha,
                           'errors': errors, 'input_sha256': record.get('input_sha256'),
                           'context_sha256': record.get('related_context_sha256') or record.get('context_sha256'),
                           'rules_version': record.get('rules_version'), 'reviewer': record.get('reviewer')}
        review_id = 'review:' + _digest(identity_fields)
        self.db.execute('''INSERT OR IGNORE INTO reviews
            (review_id,activity_id,occurrence_id,input_sha256,context_sha256,rules_version,
             reviewer,status,reason,answer_blob_sha,fields_json,error_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
            (review_id, activity_id, record.get('occurrence_id'), record.get('input_sha256'),
             identity_fields['context_sha256'], record.get('rules_version'), record.get('reviewer'),
             status, reason, answer_sha, _json(fields), _json(errors)))
        self.records += self.output('review', review_id)
        for snapshot in set(snapshots):
            self.db.execute('INSERT OR IGNORE INTO review_snapshots(review_id,snapshot_id) VALUES(?,?)', (review_id, snapshot))
            if activity_id:
                self.db.execute('INSERT OR IGNORE INTO activity_snapshots(activity_id,snapshot_id,source_id) VALUES(?,?,?)',
                                (activity_id, snapshot, source_id))
        return review_id

    def attempt(self, key, value, *, source_ref, review_id=None):
        if not isinstance(key, str) or not key:
            raise HeldInput('Paid attempt has no stable request key.')
        state = str(value.get('state') or value.get('phase') or 'unknown')
        self.db.execute('''INSERT INTO attempts
            (request_key,state,model,rules_version,input_sha256,reserved_at,finished_at,
             error_summary,result_review_id,source_ledger) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(request_key) DO UPDATE SET
             model=coalesce(attempts.model,excluded.model),
             rules_version=coalesce(attempts.rules_version,excluded.rules_version),
             reserved_at=coalesce(attempts.reserved_at,excluded.reserved_at),
             finished_at=coalesce(attempts.finished_at,excluded.finished_at),
             error_summary=coalesce(attempts.error_summary,excluded.error_summary),
             result_review_id=coalesce(attempts.result_review_id,excluded.result_review_id)''',
            (key, state, value.get('model') or value.get('model_name') or value.get('configured_model'),
             value.get('rules_version'), value.get('input_sha256'), value.get('reserved_at') or value.get('created_at'),
             value.get('finished_at'), _message(value.get('error') or value.get('failure_code')) or None,
             review_id, self.source))
        self.db.execute('INSERT OR IGNORE INTO attempt_origins(request_key,source_ledger,source_ref,state) VALUES(?,?,?,?)',
                        (key, self.source, str(source_ref), state))
        self.output('attempt', key)

    def envelope(self, value):
        if not isinstance(value, dict):
            raise HeldInput('Input JSON is not a recognized object envelope.')
        if 'capture' in value and 'url' in value:
            capture = value['capture']
            if not isinstance(capture, dict):
                raise HeldInput('Source cache capture must be an object.')
            document = capture.get('document')
            if document is None:
                if not isinstance(capture.get('error'), dict):
                    raise HeldInput('Source cache has neither a document nor a recognized capture error.')
                errors = [_error('source', 'source_unavailable', capture['error'], value['url'])]
                response = capture['error'].get('source_response')
                snapshot = self.source_response(dict(response, canonical_url=value['url'])) if isinstance(response, dict) else None
                self.review({'status': 'source_unavailable', 'reason': errors[0]['reason']}, extra_errors=errors,
                            source_snapshots=[snapshot] if snapshot else [])
                raise HeldInput('Failed capture normalized; original failure evidence remains held.')
            media = capture.get('media', [])
            if not isinstance(media, list):
                raise HeldInput('Source cache media must be a list.')
            snapshot, error = self.snapshot(document, media)
            if snapshot and _safe_url(value['url']):
                # The captured origin, not a guessed redirect alias, identifies sources.
                self.db.execute('''INSERT OR IGNORE INTO activity_snapshots(activity_id,snapshot_id,source_id)
                    SELECT source.activity_id,?,source.source_id
                    FROM source_records AS source JOIN json_each(source.source_urls_json) AS declared
                    WHERE source.activity_id IS NOT NULL AND declared.type='text' AND declared.value=?''',
                    (snapshot, value['url']))
            if capture.get('media_error'):
                self.error(_error('source', 'media_error', capture['media_error'], value['url']))
            if error:
                self.review({'status': 'source_unavailable', 'reason': error['reason']}, extra_errors=[error],
                            source_snapshots=[snapshot] if snapshot else [])
                raise HeldInput('Rejected source preserved as failure evidence; original retained.')
            return
        if value.get('version') == 'model-wire-audit-v1':
            self.wire_receipt(value)
            return
        if 'source_review' in value or ('status' in value and 'reason' in value and ('claims' in value or 'activity' in value)):
            self.review(value)
            if value.get('capture_artifact'):
                raise HeldInput('Progress record normalized; separate capture_artifact remains held as original evidence.')
            return
        if _wire_shape(value):
            errors = []
            if value.get('error'):
                detail = value['error']
                errors.append(_error('model', detail.get('code') if isinstance(detail, dict) else 'provider_error',
                                     detail.get('message') if isinstance(detail, dict) else detail))
            answer = _visible_answer(value)
            if not answer and not errors:
                errors.append(_error('model', 'visible_answer_missing', 'No visible assistant answer was stored; provider status alone is not an activity review.'))
            self.review({'status': value.get('status') or ('failed' if errors else 'received'),
                         'reason': 'Stored provider response; activity identity was not supplied.'},
                        answer=answer, extra_errors=errors,
                        paid=_paid_facts(value))
            return
        raise HeldInput('Unsupported historical input shape; original retained.')

    def wire_receipt(self, receipt):
        path = Path(self.source)
        if path.name != 'receipt.json' or path != path.resolve():
            raise HeldInput('Wire evidence requires a canonical nonsymlink receipt.json path.')
        context, request, response = (receipt.get(key) or {} for key in ('context', 'request', 'response'))
        if any(not isinstance(value, dict) for value in (context, request, response)):
            raise HeldInput('Wire receipt metadata is malformed.')

        def read_artifact(name, metadata=None):
            artifact = path.with_name(name)
            digest, info = _fingerprint(artifact)
            if info.st_size > MAX_RECORD_BYTES:
                raise HeldInput('Wire artifact exceeds the bounded record limit.')
            if metadata is not None and (metadata.get('file', name) != name
                    or type(metadata.get('bytes')) is not int
                    or digest != metadata.get('sha256') or info.st_size != metadata['bytes']):
                raise HeldInput('Wire ' + name + ' differs from its durable receipt.')
            with _reading_input(artifact, digest, info) as (stream, consumed):
                raw = stream.read(MAX_RECORD_BYTES + 1)
                consumed.update(raw)
            return raw, digest

        raw_receipt, receipt_sha = read_artifact('receipt.json')
        ingestion = self.db.execute('SELECT sha256 FROM ingestions WHERE source_path=?', (self.source,)).fetchone()
        if (ingestion is not None and receipt_sha != ingestion[0]) or json.loads(raw_receipt) != receipt:
            raise HeldInput('Wire receipt changed during extraction.')
        wire = {'receipt_blob_sha': self.blob(raw_receipt, mime='application/json', kind='model_receipt')}
        errors = []
        try:
            raw_request, _ = read_artifact('request.json', request)
        except FileNotFoundError:
            errors.append(_error('model', 'request_missing', 'Exact sent request was not retained; it is not reconstructed from source evidence.'))
        else:
            wire['request_blob_sha'] = self.blob(raw_request, mime='application/json', kind='model_request')
            del raw_request
        value = {}
        if 'sha256' in response or 'bytes' in response:
            try:
                raw_response, _ = read_artifact('response.bin', response)
            except FileNotFoundError:
                errors.append(_error('model', 'response_missing', 'Receipt-bound response bytes are unavailable; originals retained.'))
            else:
                wire['response_blob_sha'] = self.blob(raw_response, mime='application/octet-stream', kind='model_response')
                if raw_response:
                    try:
                        value = json.loads(raw_response)
                    except (ValueError, UnicodeError):
                        errors.append(_error('model', 'malformed_response' if response.get('complete') else 'incomplete_response',
                                             'Response is not complete JSON; exact received bytes are retained.'))
                del raw_response
        else:
            errors.append(_error('model', 'response_unavailable', 'No durable response byte count and digest were recorded; reserved file space is not a response.'))
        if receipt.get('failure_code'):
            errors.append(_error('model', receipt['failure_code'], 'Stored model request failed at phase ' + str(receipt.get('phase'))))
        if response.get('complete') is not True:
            errors.append(_error('model', 'incomplete_response', 'Provider response was not fully received; charging is unknown.'))
        record = {'status': receipt.get('phase') or 'unknown', 'reason': 'Stored model-wire receipt.',
                  'rules_version': context.get('rules_version')}
        for key in LEGACY_REFS:
            if key in context:
                record[key] = context[key]
        answer = _visible_answer(value)
        if answer:
            try:
                parsed = json.loads(answer)
            except (ValueError, UnicodeError):
                pass
            else:
                if isinstance(parsed, dict) and len(_json(parsed).encode('utf-8')) <= MAX_SEMANTIC_BYTES // 2:
                    record['parsed_result'] = parsed
        if isinstance(value, dict) and value.get('error'):
            detail = value['error']
            errors.append(_error('model', detail.get('code') if isinstance(detail, dict) else 'provider_error',
                                 detail.get('message') if isinstance(detail, dict) else detail))
        paid = _paid_facts(value)
        paid.update({key: receipt[key] for key in ('http_status', 'upload_complete') if key in receipt})
        key = context.get('logical_request_sha256') or request.get('sha256')
        if key:
            paid.update(request_key=key, state=record['status'])
        review_id = self.review(record, answer=answer, extra_errors=errors, paid=paid, wire=wire)
        if key:
            self.attempt(key, dict(receipt, rules_version=context.get('rules_version')), source_ref='receipt', review_id=review_id)
        # These bytes depend on separately retained originals. Normalization and
        # archive proof must never authorize deleting a receipt on its own.
        raise HeldInput('Wire receipt normalized; dependent wire originals are held for preservation.')



def _paid_facts(value):
    facts = {'charge': 'unknown'}
    if not isinstance(value, dict):
        return facts
    for key in ('id', 'model', 'status'):
        if isinstance(value.get(key), str):
            facts[key] = value[key]
    usage = value.get('usage')
    if isinstance(usage, dict):
        facts['usage'] = {key: usage[key] for key in ('input_tokens', 'output_tokens', 'total_tokens',
                                                   'prompt_tokens', 'completion_tokens')
                          if type(usage.get(key)) is int and usage[key] >= 0}
    return facts



def _cleaned_missing(db, previous):
    log = db.execute('SELECT * FROM cleanup_log WHERE source_path=?', (previous['source_path'],)).fetchone()
    if log is None or log['sha256'] != previous['sha256'] or log['size'] != previous['size']:
        return False
    try:
        proof = json.loads(log['proof_json'])
        recorded = {(row['kind'], row['row_id']) for row in proof['outputs']}
    except (ValueError, KeyError, TypeError):
        return False
    actual = {(row[0], row[1]) for row in db.execute('SELECT kind,row_id FROM ingestion_outputs WHERE source_path=?',
                                                   (previous['source_path'],))}
    return (proof.get('phase') == 'deleted' and proof.get('mtime_ns') == previous['mtime_ns']
            and bool(proof.get('device')) and bool(proof.get('inode'))
            and bool(actual) and actual == recorded)


def extract_paths(root: Path, inputs: list[Path]) -> dict:
    """Normalize supported local evidence without collecting or mutating sources.

    Each input is hashed before and after extraction. Completed unchanged inputs
    are reused; changed, unsupported, malformed and externally dependent inputs
    are held. Original input files are always retained.
    """
    db = connect_foundation(Path(root))
    result = {'records': 0, 'snapshots': 0, 'blobs': 0, 'errors': 0, 'held': 0,
              'files': 0, 'skipped': 0, 'bytes': 0}
    try:
        store = BlobStore(Path(root), db)
        for input_path in inputs:
            for discovered in _walk(Path(input_path).expanduser().absolute()):
                path = discovered.absolute()
                previous = db.execute('SELECT * FROM ingestions WHERE source_path=?', (str(path),)).fetchone()
                if previous and previous['status'] == 'cleaned':
                    if not path.exists() and _cleaned_missing(db, previous):
                        result['skipped'] += 1
                    else:
                        result['held'] += 1
                        result['errors'] += 1
                    continue
                result['files'] += 1
                if path.suffix.lower() in {'.sqlite3', '.sqlite', '.db'}:
                    # SQLite sources use the guarded native preservation commands;
                    # generic extraction must not open sidecars or hash huge ledgers.
                    result['held'] += 1
                    result['errors'] += 1
                    continue
                try:
                    digest, info = _fingerprint(path)
                except (OSError, HeldInput) as error:
                    result['held'] += 1
                    result['errors'] += 1
                    # A nonexistent/unreadable source has no trustworthy digest;
                    # never invent one to create a cleanup-capable ingestion.
                    continue
                result['bytes'] += info.st_size
                if previous and (previous['sha256'] != digest or previous['size'] != info.st_size
                                 or previous['mtime_ns'] != info.st_mtime_ns):
                    db.execute("UPDATE ingestions SET status='held',error_json=? WHERE source_path=?",
                               (_json([_error('ingestion', 'source_changed', 'Previously ingested source changed; original retained.')]), str(path)))
                    db.commit()
                    result['held'] += 1
                    result['errors'] += 1
                    continue
                if previous and previous['status'] == 'completed':
                    result['skipped'] += 1
                    continue
                db.execute('''INSERT INTO ingestions(source_path,sha256,size,mtime_ns,status)
                    VALUES(?,?,?,?,'extracting') ON CONFLICT(source_path) DO UPDATE SET status='extracting',
                    error_json='[]',completed_at=NULL''', (str(path), digest, info.st_size, info.st_mtime_ns))
                db.commit()
                extractor = _Extractor(db, store, path)
                status = 'completed'
                try:
                    if path.suffix.lower() == '.jsonl':
                        with _reading_input(path, digest, info) as (stream, consumed):
                            for line_number in range(1, 2**63):
                                line = stream.readline(MAX_RECORD_BYTES + 1)
                                consumed.update(line)
                                if not line:
                                    break
                                if len(line) > MAX_RECORD_BYTES:
                                    raise HeldInput('JSONL record exceeds bounded record limit at line ' + str(line_number))
                                if not line.strip():
                                    continue
                                try:
                                    extractor.envelope(json.loads(line))
                                except (ValueError, TypeError, KeyError, UnicodeError) as error:
                                    raise HeldInput('JSONL line ' + str(line_number) + ': ' + _message(error)) from None
                                db.commit()
                    elif path.suffix.lower() == '.json' or path.name == 'response.bin':
                        extractor.envelope(_load_json(path, expected_digest=digest, expected_info=info))
                        if path.name == 'response.bin' and path.with_name('receipt.json').exists():
                            raise HeldInput('Wire response normalized; dependent wire originals are held for preservation.')
                    else:
                        raise HeldInput('Unsupported source file type; original retained.')
                    final_digest, final_info = _fingerprint(path)
                    if final_digest != digest or _signature(final_info) != _signature(info):
                        raise HeldInput('Source changed during extraction; cleanup refused.')
                    if extractor.hold_original:
                        raise HeldInput('Failed media normalized as private evidence; original input must be retained.')
                except (OSError, ValueError, TypeError, KeyError, UnicodeError, sqlite3.Error, RuntimeError, subprocess.SubprocessError) as error:
                    status = 'held'
                    extractor.error(_error('ingestion', 'held_input', error))
                counts = dict(db.execute("SELECT kind,count(*) FROM ingestion_outputs WHERE source_path=? GROUP BY kind", (str(path),)))
                extractor.records = counts.get('review', 0)
                extractor.snapshots = counts.get('snapshot', 0)
                if status == 'completed' and extractor.records + extractor.snapshots == 0:
                    status = 'held'
                    extractor.error(_error('ingestion', 'empty_input', 'Input produced no useful content; original retained.'))
                db.execute('''UPDATE ingestions SET status=?,record_count=?,snapshot_count=?,
                    error_json=?,completed_at=? WHERE source_path=?''',
                    (status, extractor.records, extractor.snapshots, _json(extractor.errors),
                     _now() if status == 'completed' else None, str(path)))
                db.commit()
                result['records'] += extractor.records
                result['snapshots'] += extractor.snapshots
                result['errors'] += extractor.error_count
                result['blobs'] += db.execute("SELECT count(*) FROM ingestion_outputs WHERE source_path=? AND kind='blob'", (str(path),)).fetchone()[0]
                if status != 'completed':
                    result['held'] += 1
                    continue
        # Cache captures carry no activity ID. Reconcile only exact URLs already
        # declared by existing source records, retaining every source association.
        db.execute('''INSERT OR IGNORE INTO activity_snapshots(activity_id,snapshot_id,source_id)
            SELECT source.activity_id,snapshot.snapshot_id,source.source_id
            FROM source_records AS source
            JOIN json_each(source.source_urls_json) AS declared
            JOIN snapshots AS snapshot ON snapshot.url=declared.value
            WHERE source.activity_id IS NOT NULL AND declared.type='text' ''')
        db.commit()
        return result
    finally:
        db.close()
