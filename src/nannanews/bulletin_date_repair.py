"""Append-only model date reviews over immutable bulletin source evidence."""
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
from functools import lru_cache
import base64
import binascii
import hashlib
import json
import os
import math
import tempfile
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit

from nannanews.bulletin_common import normalized_text, source_url as canonical_source_url
from nannanews.content_rules import HISTORICAL_REVIEW_INSTRUCTIONS, RULES_VERSION
from nannanews.activity_content import activity_citations, public_activity, validate_activity
from nannanews.historical_evidence_refs import build_evidence_catalog, decode_evidence_response

RESOLUTIONS = frozenset({'explicit', 'contextual', 'year_unknown', 'ambiguous', 'invalid'})
KINDS = frozenset({'event', 'deadline', 'registration', 'application', 'listed', 'source'})
STATUSES = frozenset({'resolved', 'needs_context', 'source_missing', 'recurring_no_end',
                      'no_relevant_date', 'invalid_source', 'uncertain', 'unresolved'})
KNOWN_RESOLUTIONS = ('explicit', 'contextual')

# Private diagnostics only: never include these in model evidence or request keys.
_SOURCE_TIMING_FIELDS = frozenset({'source_fetch_seconds', 'source_media_prepare_seconds'})
_TIMING_SECONDS = _SOURCE_TIMING_FIELDS | frozenset({
    'source_capture_seconds', 'source_cache_lookup_seconds', 'source_cache_store_seconds',
    'model_prepare_seconds', 'review_preparation_seconds', 'model_request_seconds',
    'model_response_validation_seconds', 'storage_admission_seconds',
    'review_storage_seconds', 'review_seconds'})
_TIMING_COUNTS = frozenset({'source_cache_hits', 'source_cache_misses'})
_TIMING_FIELDS = _TIMING_SECONDS | _TIMING_COUNTS


def _validated_timing(value, *, fields=_TIMING_FIELDS):
    """Missing means unknown; admit only bounded, finite private measurements."""
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - fields:
        raise ValueError('Unsupported private review timing metadata.')
    for key, number in value.items():
        if (type(number) not in (int, float)
                or not 0 <= number <= (1000000 if key in _TIMING_COUNTS else 7 * 86400)
                or not math.isfinite(number)
                or (key in _TIMING_COUNTS and type(number) is not int)):
            raise ValueError('Invalid private review timing measurement.')
    return dict(value)


@contextmanager
def _measure(timing, key):
    started = time.monotonic()
    try:
        yield
    finally:
        if timing is not None:
            timing[key] = timing.get(key, 0.0) + (time.monotonic() - started)


class ReviewDeferred(Exception):
    """Defer unpaid work for a trusted operational admission policy."""
    def __init__(self, code, reason):
        if not isinstance(code, str) or not code.strip() or len(code) > 100:
            raise ValueError('Review deferral requires a bounded operational code.')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
            raise ValueError('Review deferral requires a bounded explanation.')
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def current_revisions_sql(db):
    """Select current AI heads, excluding explicitly invalidated source inputs."""
    invalidations = db.execute("SELECT 1 FROM sqlite_master WHERE name='bulletin_source_date_invalidations'").fetchone()
    return '''SELECT r.* FROM bulletin_date_revisions r WHERE r.id IN
        (SELECT max(id) FROM bulletin_date_revisions
         WHERE origin='luna_review' GROUP BY occurrence_id)''' + (
        ''' AND NOT EXISTS (SELECT 1 FROM bulletin_source_date_invalidations s
            WHERE s.date_revision_id=r.id)''' if invalidations else '')


def create_date_schema(db):
    """Install the derived layer within the explicit schema migration."""
    from nannanews import schema
    schema.execute_script(db, '''
        CREATE TABLE IF NOT EXISTS bulletin_date_revisions (
            id INTEGER PRIMARY KEY,
            occurrence_id INTEGER NOT NULL REFERENCES bulletin_occurrences(id),
            input_sha256 TEXT NOT NULL,
            revision_sha256 TEXT NOT NULL,
            rules_version TEXT NOT NULL,
            origin TEXT NOT NULL CHECK(origin IN ('automatic','luna_review')),
            reviewer TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            source_review_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(occurrence_id,revision_sha256));
        CREATE TABLE IF NOT EXISTS bulletin_date_revision_claims (
            id INTEGER PRIMARY KEY,
            revision_id INTEGER NOT NULL REFERENCES bulletin_date_revisions(id),
            kind TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            normalized_date TEXT,
            resolution TEXT NOT NULL,
            evidence TEXT NOT NULL,
            locator_json TEXT);
        CREATE INDEX IF NOT EXISTS bulletin_date_revision_head
            ON bulletin_date_revisions(occurrence_id,id);
        CREATE INDEX IF NOT EXISTS bulletin_date_revision_claim_lookup
            ON bulletin_date_revision_claims(revision_id,kind,resolution,normalized_date);
    ''')
    revision_columns = {row[1] for row in db.execute('PRAGMA table_info(bulletin_date_revisions)')}
    if 'source_review_json' not in revision_columns:
        db.execute("ALTER TABLE bulletin_date_revisions ADD COLUMN source_review_json TEXT NOT NULL DEFAULT '{}'")
    current = current_revisions_sql(db)
    db.execute('DROP VIEW IF EXISTS bulletin_effective_dates')
    db.execute('''CREATE VIEW bulletin_effective_dates AS
        SELECT c.id,r.occurrence_id,c.kind,c.raw_text,c.normalized_date,c.resolution,c.evidence,c.locator_json
        FROM bulletin_date_revision_claims c JOIN (''' + current + ''') r ON r.id=c.revision_id
        UNION ALL
        SELECT d.id,d.occurrence_id,d.kind,d.raw_text,d.normalized_date,d.resolution,d.evidence,d.locator_json
        FROM bulletin_dates d WHERE NOT EXISTS (SELECT 1 FROM (''' + current + ''') r
            WHERE r.occurrence_id=d.occurrence_id)''')
    for table in ('bulletin_date_revisions', 'bulletin_date_revision_claims'):
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.lower()}
                BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'bulletin date revisions are immutable'); END''')


def validate_claims(claims):
    """Validate claim shape and calendar days without deciding date semantics."""
    if not isinstance(claims, list) or len(claims) > 1000:
        raise ValueError('Date decision must contain a bounded claim list.')
    required = {'kind', 'raw_text', 'normalized_date', 'resolution', 'evidence', 'locator'}
    result = []
    seen = set()
    for claim in claims:
        if (not isinstance(claim, dict) or set(claim) != required
                or not isinstance(claim['kind'], str) or claim['kind'] not in KINDS
                or not isinstance(claim['resolution'], str) or claim['resolution'] not in RESOLUTIONS
                or not isinstance(claim['raw_text'], str) or not isinstance(claim['evidence'], str)
                or not claim['raw_text'].strip() or not claim['evidence'].strip()
                or (claim['locator'] is not None and not isinstance(claim['locator'], dict))):
            raise ValueError('Date decision has invalid claim fields or evidence.')
        day = claim['normalized_date']
        if day is not None:
            try:
                if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError('Date decision contains an invalid calendar day.') from None
            if claim['resolution'] not in KNOWN_RESOLUTIONS:
                raise ValueError('Unresolved date must not contain a normalized calendar day.')
        elif claim['resolution'] in KNOWN_RESOLUTIONS:
            raise ValueError('Resolved date must contain a normalized calendar day.')
        if claim['resolution'] == 'contextual' and normalized_text(claim['evidence']) == normalized_text(claim['raw_text']):
            raise ValueError('Contextual dates require a separate explanation of their basis.')
        value = dict(claim)
        key = _json(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def input_digest(occurrence, original_claims):
    """Bind a decision to the immutable document/local key and actual input text."""
    payload = {key: occurrence[key] for key in ('source_sha256', 'local_key', 'title', 'body_text', 'issue_date')}
    payload['source_urls'] = sorted(set(occurrence.get('source_urls') or []))
    payload['dates'] = original_claims
    if any(related.get('source_corrections') for related in occurrence.get('related_occurrences', [])):
        payload['source_context_sha256'] = occurrence['related_context_sha256']
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def record_revision(db, occurrence, original_claims, claims, *, rules_version, origin, reviewer, status, reason, source_review=None):
    """Append one decision inside the caller's transaction; never replace history."""
    claims = validate_claims(claims)
    if rules_version != RULES_VERSION and isinstance(source_review, dict):
        source_review = dict(source_review, activity=None)
    source_review = _validate_source_review(source_review, require_activity=rules_version == RULES_VERSION)
    if origin != 'luna_review' or status not in STATUSES:
        raise ValueError('Unsupported date decision origin or status.')
    if not all(isinstance(value, str) and value.strip() for value in (rules_version, reviewer, reason)):
        raise ValueError('Date decisions require a version, reviewer and explanation.')
    digest = input_digest(occurrence, original_claims)
    current = db.execute('''SELECT id,input_sha256 FROM bulletin_date_revisions
        WHERE occurrence_id=? ORDER BY id DESC LIMIT 1''', (occurrence['id'],)).fetchone()
    decision = {'input_sha256': digest, 'rules_version': rules_version, 'origin': origin,
                'reviewer': reviewer, 'status': status, 'reason': reason,
                'source_review': source_review, 'claims': claims}
    revision_hash = hashlib.sha256(_json(decision).encode()).hexdigest()
    existing = db.execute('SELECT id FROM bulletin_date_revisions WHERE occurrence_id=? AND revision_sha256=?',
                          (occurrence['id'], revision_hash)).fetchone()
    if existing:
        if current and existing['id'] != current['id']:
            raise ValueError('An older decision cannot silently replace a newer revision.')
        return False
    revision = db.execute('''INSERT INTO bulletin_date_revisions
        (occurrence_id,input_sha256,revision_sha256,rules_version,origin,reviewer,status,reason,source_review_json,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)''', (occurrence['id'], digest, revision_hash, rules_version, origin,
        reviewer, status, reason, _json(source_review), datetime.now(timezone.utc).isoformat())).lastrowid
    db.executemany('''INSERT INTO bulletin_date_revision_claims
        (revision_id,kind,raw_text,normalized_date,resolution,evidence,locator_json) VALUES(?,?,?,?,?,?,?)''',
        [(revision, claim['kind'], claim['raw_text'], claim['normalized_date'], claim['resolution'],
          claim['evidence'], _json(claim['locator']) if claim['locator'] is not None else None) for claim in claims])
    return True


SOURCE_REVIEW_STATUSES = frozenset({'source_verified', 'source_conflict', 'source_unavailable',
                                    'source_mismatch', 'secondary_only'})


def _valid_day(value, *, nullable=True):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError('Source evidence date must be an ISO calendar day or null.')
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise ValueError('Source evidence contains an invalid calendar day.') from None
    return value


def _validate_source_review(source_review, *, require_activity=False):
    """Admit current verdicts strictly; allow old envelopes only for legacy reads."""
    if require_activity and (not isinstance(source_review, dict) or 'activity' not in source_review):
        raise ValueError('Current source review requires an explicit activity node.')
    if source_review is None:
        source_review = {'status': 'secondary_only',
                         'reason': 'No captured primary source was included in this review.',
                         'sources': [], 'attempts': []}
    if not isinstance(source_review, dict) or set(source_review) - {'status', 'reason', 'sources', 'attempts', 'activity'}:
        raise ValueError('Source review has an unsupported shape.')
    status, reason = source_review.get('status'), source_review.get('reason')
    sources, attempts = source_review.get('sources'), source_review.get('attempts', [])
    if (not isinstance(status, str) or status not in SOURCE_REVIEW_STATUSES
            or not isinstance(reason, str) or not reason.strip() or len(reason) > 4000
            or not isinstance(sources, list) or len(sources) > 100
            or not isinstance(attempts, list) or len(attempts) > 100):
        raise ValueError('Source review status, reason or evidence list is invalid.')
    normalized_sources = []
    seen_urls = set()
    legacy_fields = {'url', 'resolved_url', 'title', 'text', 'raw_response_base64', 'raw_response_digest',
                     'fetched_at', 'published_date', 'raw_date_text', 'source_type', 'content_type', 'quotes'}
    fields = legacy_fields | {'images', 'quote_evidence'}
    image_indexes = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) not in (legacy_fields, fields):
            raise ValueError('Captured source has an unsupported shape.')
        legacy = set(source) == legacy_fields
        if legacy:
            source = dict(source, images=[], quote_evidence=[
                {'text': quote, 'image_index': None} for quote in source['quotes']])
        url, resolved = source['url'], source['resolved_url']
        if (canonical_source_url(url) != url or canonical_source_url(resolved) != resolved or url in seen_urls):
            raise ValueError('Captured source URL is invalid or duplicated.')
        seen_urls.add(url)
        if (not isinstance(source['title'], str) or len(source['title']) > 500
                or not isinstance(source['text'], str) or not isinstance(source['fetched_at'], str)
                or not source['fetched_at'] or not isinstance(source['raw_date_text'], (str, type(None)))
                or not isinstance(source['source_type'], str) or not isinstance(source['content_type'], str)):
            raise ValueError('Captured source metadata is invalid.')
        published = _valid_day(source['published_date'])
        digest = source['raw_response_digest']
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError('Captured source response digest is invalid.')
        try:
            int(digest, 16)
            raw = base64.b64decode(source['raw_response_base64'], validate=True)
        except (TypeError, ValueError, binascii.Error):
            raise ValueError('Captured source response bytes are invalid.') from None
        if hashlib.sha256(raw).hexdigest() != digest.lower():
            raise ValueError('Captured source response hash does not match its bytes.')
        images = source['images']
        from nannanews import acquisition
        if not isinstance(images, list) or len(images) > acquisition.MAX_IMAGES:
            raise ValueError('Captured source image list is invalid.')
        source_images = set()
        for image in images:
            image_fields = {'image_index', 'url', 'sha256', 'mime', 'data'}
            if not isinstance(image, dict) or set(image) != image_fields:
                raise ValueError('Captured source image has an unsupported shape.')
            index, image_url, image_digest = image['image_index'], image['url'], image['sha256']
            if (type(index) is not int or index < 0 or index in image_indexes
                    or canonical_source_url(image_url) != image_url or image_url in {'', None}
                    or not isinstance(image_digest, str) or len(image_digest) != 64
                    or not isinstance(image['mime'], str)
                    or image['mime'] not in acquisition.IMAGE_MIMES | {'application/octet-stream'}
                    or not isinstance(image['data'], str)):
                raise ValueError('Captured source image identity is invalid.')
            try:
                int(image_digest, 16)
                if len(image['data']) > 4*((acquisition.MAX_IMAGE+2)//3):
                    raise ValueError('Captured image exceeds the source byte limit.')
                image_bytes = base64.b64decode(image['data'], validate=True)
            except (TypeError, ValueError, binascii.Error):
                raise ValueError('Captured source image bytes are invalid.') from None
            if hashlib.sha256(image_bytes).hexdigest() != image_digest.lower():
                raise ValueError('Captured source image hash does not match its bytes.')
            image_indexes.add(index)
            source_images.add(index)
        evidence = source['quote_evidence']
        if not isinstance(evidence, list) or len(evidence) > 100:
            raise ValueError('Captured source quote evidence is invalid.')
        quote_texts = []
        for quote in evidence:
            if not isinstance(quote, dict) or set(quote) != {'text', 'image_index'}:
                raise ValueError('Captured source quotation has an unsupported shape.')
            text, index = quote['text'], quote['image_index']
            if not isinstance(text, str) or not text.strip() or len(text) > 12000:
                raise ValueError('Captured source quotation is invalid.')
            if index is None:
                if text not in source['text']:
                    raise ValueError('Captured source contains a fabricated text quotation.')
            elif type(index) is not int or index not in source_images:
                raise ValueError('Captured source image quotation has no matching captured image.')
            quote_texts.append(text)
        quotes = source['quotes']
        if (not isinstance(quotes, list) or quotes != list(dict.fromkeys(quote_texts))
                or any(not isinstance(quote, str) for quote in quotes)):
            raise ValueError('Captured source quote projection is inconsistent.')
        normalized_sources.append(dict(source, published_date=published,
            raw_response_digest=digest.lower(), images=images, quotes=quotes, quote_evidence=evidence))
    normalized_attempts = []
    for attempt in attempts:
        if (not isinstance(attempt, dict) or set(attempt) != {'url', 'status', 'code', 'reason'}
                or not isinstance(attempt['url'], str) or canonical_source_url(attempt['url']) != attempt['url']
                or any(not isinstance(attempt[key], str) for key in ('status', 'code', 'reason'))
                or len(attempt['reason']) > 2000):
            raise ValueError('Source acquisition attempt is invalid.')
        normalized_attempts.append(dict(attempt))
    if status in {'source_verified', 'source_conflict'} and not any(source['quotes'] for source in normalized_sources):
        raise ValueError('A verified or conflicting source review requires a captured, cited primary source.')
    activity = source_review.get('activity')
    if status == 'source_verified':
        if require_activity or activity is not None:
            activity = validate_activity(activity, normalized_sources)
    elif activity is not None:
        raise ValueError('Only a verified primary-source review may contain activity content.')
    return {'status': status, 'reason': reason, 'sources': normalized_sources,
            'attempts': normalized_attempts, 'activity': activity}


def public_source_review(review, *, rules_version=None):
    """Project legacy receipts as metadata only; never relax new admission."""
    if isinstance(review, str):
        try:
            review = json.loads(review)
        except (TypeError, ValueError):
            review = None
    if not isinstance(review, dict):
        return {'status': 'secondary_only',
                'reason': 'No captured primary-source review is available.', 'sources': [], 'activity': None}
    try:
        if rules_version != RULES_VERSION:
            sources = []
            for source in review.get('sources', []):
                source = dict(source, quotes=[])
                if 'quote_evidence' in source:
                    source['quote_evidence'] = []
                sources.append(source)
            review = dict(review, status='secondary_only', sources=sources, activity=None,
                reason='历史规则复核记录仅保留来源元数据，尚未按当前完整内容及逐字证据规则完成复核。')
        clean = _validate_source_review(review, require_activity=rules_version == RULES_VERSION)
    except (TypeError, ValueError):
        return {'status': 'secondary_only',
                'reason': 'Stored primary-source evidence is unavailable or invalid.', 'sources': [], 'activity': None}
    status = clean['status']
    if status in {'source_verified', 'source_conflict'} and not any(source['quotes'] for source in clean['sources']):
        status = 'secondary_only'
    return {'status': status, 'reason': clean['reason'], 'activity': public_activity(clean['activity']), 'sources': [
        {'url': source['url'], 'resolved_url': source['resolved_url'], 'title': source['title'],
         'sha256': source['raw_response_digest'], 'fetched_at': source['fetched_at'],
         'published_date': source['published_date'], 'quotes': list(source['quotes'])}
        for source in clean['sources']]}


def _homepage_redirect(url, resolved_url):
    requested, resolved = urlsplit(url), urlsplit(resolved_url)
    return bool(resolved.path in {'', '/'} and not resolved.query and requested.path not in {'', '/'})


def _linked_source_urls(occurrence):
    urls = list(occurrence.get('source_urls') or [])
    for related in occurrence.get('related_occurrences') or []:
        urls.extend(related.get('source_urls') or [])
    result = []
    for url in urls:
        canonical = canonical_source_url(url)
        if canonical != url:
            raise ValueError('Occurrence contains a non-canonical public source URL.')
        if canonical not in result:
            result.append(canonical)
    return result


def _is_secondary_newsletter_url(url):
    parts = urlsplit(url)
    return parts.hostname == 'nik-nul.github.io' and parts.path.startswith('/news/')


def _secondary_source_urls(occurrence):
    return [url for url in _linked_source_urls(occurrence) if _is_secondary_newsletter_url(url)]


def _occurrence_source_urls(occurrence):
    return [url for url in _linked_source_urls(occurrence) if not _is_secondary_newsletter_url(url)]


def _capture_sources(occurrence, source_cache, *, _timing=None):
    from nannanews import acquisition

    sources, attempts, media, mismatched, source_media = [], [], [], False, []
    for url in _occurrence_source_urls(occurrence):
        with _measure(_timing, 'source_cache_lookup_seconds'):
            cached = source_cache.get(url)
        if _timing is not None:
            key = 'source_cache_misses' if cached is None else 'source_cache_hits'
            _timing[key] = _timing.get(key, 0) + 1
        if cached is None:
            capture_timing = {}
            try:
                with _measure(capture_timing, 'source_fetch_seconds'):
                    document = acquisition.fetch_document(url)
                if document.get('canonical_url') != url:
                    raise ValueError('Fetched source identity differs from the linked URL.')
                try:
                    prepared_media = []
                    if document.get('images') or document.get('pdf_sources'):
                        with _measure(capture_timing, 'source_media_prepare_seconds'):
                            prepared_media = acquisition._prepare_media(document)
                    media_error = '' if document.get('capture_complete', True) else 'Source article or embedded media were not captured completely; available original evidence is retained.'
                except Exception as error:
                    prepared_media = []
                    media_error = str(error)[:1000]
                cached = {'document': document, 'media': prepared_media, 'media_error': media_error}
            except Exception as error:
                cached = {'error': {'code': str(getattr(error, 'code', 'source_unavailable')),
                                    'reason': str(error)[:1000]}}
                source_response = getattr(error, 'source_response', None)
                if isinstance(source_response, dict):
                    cached['error']['source_response'] = source_response
            cached['_timing'] = capture_timing
            if _timing is not None:
                for key, seconds in capture_timing.items():
                    _timing[key] = _timing.get(key, 0.0) + seconds
            with _measure(_timing, 'source_cache_store_seconds'):
                source_cache[url] = cached
        if cached.get('error'):
            failure = cached['error']
            attempts.append({'url': url, 'status': 'unavailable', 'code': failure['code'],
                             'reason': failure['reason']})
            continue
        document = cached['document']
        if document.get('canonical_url') != url:
            attempts.append({'url': url, 'status': 'mismatch', 'code': 'source_identity_mismatch',
                             'reason': 'Cached capture identity differs from the linked public URL.'})
            mismatched = True
            continue
        resolved = document.get('resolved_url')
        if not isinstance(resolved, str) or canonical_source_url(resolved) != resolved:
            attempts.append({'url': url, 'status': 'mismatch', 'code': 'source_identity_mismatch',
                             'reason': 'Fetched source resolved to an invalid public URL.'})
            mismatched = True
            continue
        mismatched |= _homepage_redirect(url, resolved)
        source_media.append({'url': url, 'media_policy': document.get('media_policy'),
                             'skipped_media': document.get('skipped_media', [])})
        source_images = []
        for image in cached.get('media') or []:
            image_index = len(media)
            source_images.append({'image_index': image_index, 'url': canonical_source_url(image['url']),
                'sha256': image['digest'], 'mime': image['mime'], 'data': image['data']})
            media.append(image)
        snapshot = {'url': url, 'resolved_url': resolved, 'title': document.get('title', ''),
            'text': document.get('text', ''), 'raw_response_base64': document.get('raw_response_base64'),
            'raw_response_digest': document.get('raw_response_digest'), 'fetched_at': document.get('fetched_at'),
            'published_date': document.get('source_published_date'),
            'raw_date_text': document.get('raw_date_text'), 'source_type': document.get('source_type', ''),
            'content_type': document.get('content_type', ''), 'images': source_images,
            'quotes': [], 'quote_evidence': []}
        sources.append(snapshot)
        if cached.get('media_error'):
            attempts.append({'url': url, 'status': 'media_unavailable', 'code': 'source_image_unavailable',
                             'reason': cached['media_error']})
    return sources, attempts, media, mismatched, source_media


def _unresolved_decision(source_review, reason):
    source_review = dict(source_review)
    source_review['activity'] = None
    if not source_review['sources'] and source_review['attempts']:
        source_review.update(status='source_unavailable',
                             reason='Linked primary source could not be captured; the date review remains unresolved.')
    elif any(_homepage_redirect(source['url'], source['resolved_url']) for source in source_review['sources']):
        source_review.update(status='source_mismatch',
                             reason='The linked URL resolved to a homepage or a different public host; it is not verified as the linked article.')
    else:
        source_review.update(status='secondary_only',
                             reason='No completed primary-source verdict is available; captured evidence is unverified.')
    return {'claims': [], 'rules_version': RULES_VERSION, 'origin': 'luna_review',
            'reviewer': 'luna:unavailable', 'status': 'unresolved',
            'reason': ('AI date review was not completed: ' + str(reason))[:4000],
            'source_review': _validate_source_review(source_review)}

def unreviewed_decision(reason):
    """Keep parser output non-semantic when no model review was configured."""
    return {'claims': [], 'rules_version': RULES_VERSION, 'origin': 'luna_review',
        'reviewer': 'luna:unconfigured', 'status': 'unresolved', 'reason': reason,
        'source_review': {'status': 'secondary_only',
            'reason': 'No configured model completed a primary-source review.', 'sources': [], 'attempts': [], 'activity': None}}


def _validate_model_citations(citations, sources, *, field_citations=()):
    if not isinstance(citations, list) or len(citations) > 100:
        raise ValueError('AI source citations have an invalid shape.')
    by_url = {source['url']: source for source in sources}
    cited = {}
    citations = citations + [{'url': item['url'], 'quotes': [
        {'text': item['text'], 'image_index': item['image_index']}]} for item in field_citations]
    for citation in citations:
        if not isinstance(citation, dict) or set(citation) != {'url', 'quotes'}:
            raise ValueError('AI source citation has an invalid shape.')
        citation_url = citation['url']
        if not isinstance(citation_url, str):
            raise ValueError('AI source citation URL is invalid.')
        source = by_url.get(citation_url)
        quotes = citation['quotes']
        if source is None or not isinstance(quotes, list) or not quotes or len(quotes) > 100:
            raise ValueError('AI source citation URL or quotation list is invalid.')
        source_images = {image['image_index'] for image in source['images']}
        checked = cited.setdefault(source['url'], [])
        for quote in quotes:
            if not isinstance(quote, dict) or set(quote) != {'text', 'image_index'}:
                raise ValueError('AI source citation must identify text or an image index.')
            text, image_index = quote['text'], quote['image_index']
            if not isinstance(text, str) or not text.strip() or len(text) > 12000:
                raise ValueError('AI source citation text is invalid.')
            if image_index is None:
                if text not in source['text']:
                    raise ValueError('AI text citation is not present in the captured primary article.')
            elif type(image_index) is not int or image_index not in source_images:
                raise ValueError('AI image citation does not identify an image captured from this source.')
            if quote not in checked:
                checked.append(dict(quote))
                if len(checked) > 100:
                    raise ValueError('AI source quotation union exceeds the captured evidence bound.')
    for source in sources:
        evidence = cited.get(source['url'], [])
        source['quote_evidence'] = evidence
        source['quotes'] = list(dict.fromkeys(quote['text'] for quote in evidence))
    return cited


def _validate_review_claim_sources(claims, occurrence, source_review):
    bulletin = [occurrence.get('title', ''), occurrence.get('body_text', '')]
    for related in occurrence.get('related_occurrences') or []:
        bulletin.extend([related.get('title', ''), related.get('body_text', '')])
    bulletin_fields = [value for value in bulletin if isinstance(value, str)]
    source_quotes = ([quote for source in source_review['sources'] for quote in source['quotes']]
                     if source_review['status'] in {'source_verified', 'source_conflict'} else [])
    for claim in claims:
        quote = claim['raw_text']
        if not any(quote in field for field in bulletin_fields) and not any(quote in cited for cited in source_quotes):
            raise ValueError('AI date quote is absent from same-item bulletin or cited primary-source evidence.')


def decision_from_response(response, model_name, occurrence, *, sources, attempts=(), mismatched=False):
    """Admit an actual retained or live response without I/O or input mutation.

    Sources are the actual captured snapshots, not public projections. The caller
    binds the returned decision to original claims/context using input_digest and
    prepared-review metadata; this function never reserves or changes a request.
    """
    fields = {'status', 'reason', 'claims', 'source_status', 'source_reason', 'citations', 'activity'}
    if not isinstance(response, dict) or set(response) != fields:
        raise ValueError('AI returned an unsupported date-review shape.')
    if (not isinstance(response['status'], str) or response['status'] not in STATUSES
            or not isinstance(response['reason'], str) or not response['reason'].strip()
            or len(response['reason']) > 4000
            or not isinstance(response['source_status'], str) or response['source_status'] not in SOURCE_REVIEW_STATUSES
            or not isinstance(response['source_reason'], str) or not response['source_reason'].strip()
            or len(response['source_reason']) > 4000
            or not isinstance(model_name, str) or not model_name.strip()):
        raise ValueError('AI returned an invalid date-review verdict or reviewer.')
    claims = validate_claims(response['claims'])
    if response['status'] == 'resolved' and not any(claim['normalized_date'] for claim in claims):
        raise ValueError('Resolved AI review must contain a supported calendar fact.')
    base = _validate_source_review({'status': 'secondary_only', 'reason': 'Captured sources awaiting admission.',
        'sources': sources, 'attempts': list(attempts), 'activity': None})
    sources, attempts = base['sources'], base['attempts']
    linked_urls = set(_occurrence_source_urls(occurrence))
    if any(item['url'] not in linked_urls for item in sources + attempts):
        raise ValueError('Captured source or attempt is not linked to this same item.')
    if any(attempt['status'] == 'media_unavailable' for attempt in attempts):
        raise ValueError('Primary-source images were not captured completely.')
    source_status, source_reason = response['source_status'], response['source_reason']
    if mismatched or any(_homepage_redirect(source['url'], source['resolved_url']) for source in sources):
        source_status = 'source_mismatch'
        source_reason = 'The linked URL resolved to a homepage or a different public host; it is not verified as the linked article.'
    elif not sources and linked_urls:
        source_status = 'source_unavailable'
        source_reason = 'Linked primary source could not be captured; secondary evidence is not primary verification.'
    elif not linked_urls:
        source_status = 'secondary_only'
        source_reason = 'No primary article URL is linked to this bulletin occurrence.'
    field_citations = activity_citations(response['activity']) if response['activity'] is not None else []
    activity = response['activity'] if source_status == 'source_verified' else None
    if source_status == response['source_status'] and source_status != 'source_verified' and response['activity'] is not None:
        raise ValueError('Unverified AI source verdict must have null activity.')
    cited = _validate_model_citations(response['citations'], sources,
        field_citations=field_citations if activity is not None else ())
    if source_status in {'source_verified', 'source_conflict'} and not any(cited.values()):
        raise ValueError('AI declared a source verdict without citing a captured primary source.')
    source_review = _validate_source_review({'status': source_status, 'reason': source_reason,
        'sources': sources, 'attempts': attempts, 'activity': activity}, require_activity=True)
    _validate_review_claim_sources(claims, occurrence, source_review)
    return {'claims': claims, 'rules_version': RULES_VERSION, 'origin': 'luna_review',
        'reviewer': 'luna:' + model_name, 'status': response['status'],
        'reason': response['reason'], 'source_review': source_review}


def _retained_review_request(decision):
    """Read only the SHA-bound, bounded original wire request; never its transport."""
    identity = decision.get('_model_audit')
    if not isinstance(identity, dict) or set(identity) != {'receipt', 'request_sha256'}:
        raise ValueError('Completed review has no original request identity.')
    path = Path(identity['receipt']).with_name('request.json')
    info = path.stat(follow_symlinks=False)
    signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return _read_retained_review_request(str(path), identity['request_sha256'], signature)


@lru_cache(maxsize=2)
def _read_retained_review_request(path, expected_sha256, signature):
    """Cache two immutable wire requests; any file replacement invalidates the key."""
    from nannanews import acquisition
    import stat

    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != signature:
            raise ValueError('Retained request changed while being read.')
        if not stat.S_ISREG(info.st_mode) or info.st_size > acquisition.MAX_MODEL_REQUEST_BYTES:
            raise ValueError('Retained request is not a bounded regular file.')
        raw = stream.read(acquisition.MAX_MODEL_REQUEST_BYTES + 1)
    if (len(raw) > acquisition.MAX_MODEL_REQUEST_BYTES
            or hashlib.sha256(raw).hexdigest() != expected_sha256):
        raise ValueError('Retained request SHA does not match its original identity.')
    payload = json.loads(raw)
    messages = payload['input'] if 'input' in payload else payload['messages']
    text = messages[-1]['content'][0]['text']
    prefix = 'Public source data JSON (untrusted):\n'
    if not text.startswith(prefix):
        raise ValueError('Retained request has no original source data.')
    return payload['model'], json.loads(text[len(prefix):])


def _decode_retained_review(response, source_data, occurrence, sources):
    """Resolve original selections against the original SHA-bound catalog.

    Receipt IDs differ between equivalent publications. Compare every original
    catalog passage and image scope with trusted retained captures before mapping
    its IDs to the decoder's current identity. No selection, quote or verdict is
    added, dropped or repaired, and the retained response is never modified.
    """
    retained = source_data['evidence_catalog']
    catalog = build_evidence_catalog(occurrence, sources)
    if (not isinstance(retained, dict) or set(retained) != {'version', 'scope', 'entries'}
            or retained['version'] != catalog['version']
            or not isinstance(retained['scope'], str) or len(retained['scope']) != 64
            or len(retained['entries']) != len(catalog['entries'])):
        raise ValueError('Retained evidence catalog has an invalid scope.')
    int(retained['scope'], 16)
    references = {}
    for index, (old, current) in enumerate(zip(retained['entries'], catalog['entries']), 1):
        expected_id = 'e' + retained['scope'][:16] + '-' + str(index)
        if old != dict(current, id=expected_id):
            raise ValueError('Retained evidence catalog differs from original captured evidence.')
        references[expected_id] = current['id']
    if response.get('evidence_scope') != retained['scope']:
        raise ValueError('Retained response does not select its original evidence scope.')

    def remap(value):
        if isinstance(value, dict):
            return {key: references[item] if key == 'ref' else remap(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [remap(item) for item in value]
        return value

    selected = remap(response)
    selected['evidence_scope'] = catalog['scope']
    return decode_evidence_response(selected, occurrence, sources=sources)


def _reuse_receipt_digest(record):
    """Bind an original canonical receipt to exactly the retained verdict facts."""
    return hashlib.sha256(_json({key: record[key] for key in
        ('status', 'claims', 'reason', 'source_review', 'rules_version', 'reviewer')}).encode()).hexdigest()


def reuse_completed_review(previous, occurrence, original_claims, *, model_name, original_occurrence=None):
    """Return a strictly readmitted completed verdict, or None, with no network/model.

    The retained logical request must match ALL current publication, date, model,
    rule and source semantics. The original wire request anchors its selected
    bulletin metadata and references. For pre-audit history ONLY, the caller may
    supply original_occurrence proven by an exact request-SHA intent and a strictly
    validated original receipt/frozen snapshot. Its _review_receipt_sha256 must
    equal _reuse_receipt_digest(retained decision); original_date_claims on that row
    supplies the required original parser inputs. Both original/current logical
    keys must match; reference responses must
    also match the rebuilt ORIGINAL catalog scope exactly. An invalid present audit
    never falls back to this anchor. Sources, images, quotations, activity and
    colleges pass the ordinary validators again. Mixed, contextual, unknown and
    legitimate unconfirmed verdicts survive unchanged; old listed dates are never
    rebased. No attempt, audit, input or canonical receipt schema is changed.
    """
    from nannanews.bulletin_historical_attempts import logical_request

    try:
        if previous.get('state') != 'completed' or previous.get('error'):
            return None
        request = json.loads(previous['request_json'])
        if hashlib.sha256(_json(request).encode()).hexdigest() != previous['request_sha256']:
            return None
        retained = json.loads(previous['decision_json'])
        if (retained['rules_version'] != RULES_VERSION or retained['origin'] != 'luna_review'
                or retained['reviewer'] != 'luna:' + model_name):
            return None
        source_review = _validate_source_review(retained['source_review'], require_activity=True)
        sources, attempts = source_review['sources'], source_review['attempts']
        captured_data = {'linked_primary_sources': [{
            'url': source['url'], 'resolved_url': source['resolved_url'], 'title': source['title'],
            'text': source['text'], 'raw_date_text': source['raw_date_text'],
            'source_published_date': source['published_date'], 'source_type': source['source_type'],
            'images': [{key: image[key] for key in ('image_index', 'url', 'sha256', 'mime')}
                       for image in source['images']]} for source in sources],
            'source_acquisition_attempts': attempts}
        source_data = None
        if '_model_audit' in retained:
            wire_model, source_data = _retained_review_request(retained)
            if wire_model != model_name:
                return None
            bulletin = source_data['bulletin']
            original_row = dict(occurrence, **{key: bulletin[key] for key in
                ('title', 'body_text', 'issue_date', 'source_corrections')},
                related_occurrences=bulletin['related_occurrences'])
            publications = [json.loads(value) for value in request['publications']]
            matching = [value for value in publications if value['title'] == bulletin['title']
                and value['body_text'] == bulletin['body_text']
                and bulletin['issue_date'] in value['issue_dates']]
            if not matching:
                return None
            original_row['source_urls'] = sorted({url for value in matching for url in value['source_urls']})
            retained_key, retained_json = logical_request(original_row, bulletin['original_date_claims'], source_data,
                rules_version=RULES_VERSION, model_name=model_name)
            wire_images = {source['url']: source.get('images', []) for source in source_data['linked_primary_sources']}
            if any(wire_images[source['url']] != source['images'] for source in captured_data['linked_primary_sources']):
                return None
        else:
            if original_occurrence is None:
                return None
            original_row = dict(original_occurrence)
            if original_row.get('_review_receipt_sha256') != _reuse_receipt_digest(retained):
                return None
            retained_key, retained_json = logical_request(original_row,
                original_row['original_date_claims'], captured_data,
                rules_version=RULES_VERSION, model_name=model_name)
        if retained_key != previous['request_sha256'] or json.loads(retained_json) != request:
            return None
        current_key, _ = logical_request(occurrence, original_claims, captured_data,
            rules_version=RULES_VERSION, model_name=model_name)
        if current_key != retained_key:
            return None
        raw = json.loads(previous['raw_output_json'])
        if 'evidence_scope' in raw:
            canonical = (_decode_retained_review(raw, source_data, original_row, sources)
                if source_data is not None else decode_evidence_response(raw, original_row, sources=sources))
        else:
            canonical = raw
        admitted = decision_from_response(canonical, model_name, original_row,
            sources=sources, attempts=attempts)
        if any(admitted[key] != retained.get(key) for key in admitted):
            return None
        _validate_review_claim_sources(admitted['claims'], occurrence, admitted['source_review'])
        original_issue_date = original_row['issue_date']
        admitted['_reuse'] = {'request_sha256': retained_key, 'original_issue_date': original_issue_date}
        if original_issue_date != occurrence.get('issue_date'):
            admitted['reason'] = ('Free reuse of the unchanged AI verdict originally reviewed with bulletin issue_date='
                + str(original_issue_date) + '; current bulletin issue_date=' + str(occurrence.get('issue_date'))
                + '. All dates, including listed dates, retain their original publication/source anchors; '
                'they are not new judgments about the current publication.\n' + admitted['reason'])
            if len(admitted['reason']) > 4000:
                return None
        return admitted
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OSError, OverflowError, RecursionError):
        # Corrupt history is a permanent unpaid block, never permission to POST.
        return None


def model_decision(occurrence, original_claims, *, source_cache=None, attempt_store=None, counters=None,
                   storage_check=None, request_gate=None, original_occurrence_resolver=None, _timing=None):
    """Review dates against linked primary sources; fetched evidence stays auditable."""
    from nannanews import acquisition
    from nannanews.bulletin_historical_attempts import AttemptBlocked, StorageBlocked, logical_request
    from nannanews.historical_evidence_refs import historical_response_schema
    from nannanews.model_audit import finalize_receipt

    counters = counters if counters is not None else Counter()
    timing = {} if _timing is None else _timing
    preparation_started = time.monotonic()
    model_preparation_started = None

    def prepared():
        if 'review_preparation_seconds' not in timing:
            now = time.monotonic()
            timing['review_preparation_seconds'] = now - preparation_started
            if model_preparation_started is not None:
                timing['model_prepare_seconds'] = now - model_preparation_started

    def diagnosed(decision):
        prepared()
        return decision

    if source_cache is None:
        source_cache = {}
    if not isinstance(source_cache, dict):
        raise ValueError('Source cache must be a URL-to-capture mapping.')
    with _measure(timing, 'source_capture_seconds'):
        sources, attempts, media, mismatched, source_media = _capture_sources(occurrence, source_cache, _timing=timing)
    model_preparation_started = time.monotonic()
    linked_urls = _occurrence_source_urls(occurrence)
    base_review = {'status': 'source_unavailable' if linked_urls and not sources else 'secondary_only',
                   'reason': 'Primary-source capture is pending a model verdict.',
                   'sources': sources, 'attempts': attempts, 'activity': None}
    if any(attempt['status'] == 'media_unavailable' for attempt in attempts):
        counters['preparation_failures'] += 1
        decision = _unresolved_decision(base_review, 'Primary-source images were not captured completely; no model request was sent.')
        decision['_preparation_failed'] = True
        return diagnosed(decision)
    if (any(item['skipped_media'] for item in source_media)
            and not any(source['text'].strip() or source['images'] for source in sources)):
        counters['preparation_failures'] += 1
        decision = _unresolved_decision(base_review, 'Primary sources contain only explicitly excluded GIF/video media; no model request was sent.')
        decision['_preparation_failed'] = True
        return diagnosed(decision)
    if mismatched:
        base_review.update(status='source_mismatch',
                           reason='The linked URL resolved to a homepage or a different public host; it is not verified as the linked article.')
    related = [{'kind': value.get('kind', ''), 'title': value.get('title', ''),
                'body_text': value.get('body_text', ''), 'source_urls': value.get('source_urls', []),
                'issue_dates': value.get('issue_dates', [value.get('issue_date')]),
                'source_corrections': value.get('source_corrections', [])}
               for value in occurrence.get('related_occurrences') or []]
    secondary_urls = _secondary_source_urls(occurrence)
    source_data = {'bulletin': {'title': occurrence.get('title', ''),
        'body_text': occurrence.get('body_text', ''), 'issue_date': occurrence.get('issue_date'),
        'source_corrections': occurrence.get('source_corrections', []),
        'related_occurrences': related,
        'original_date_claims': original_claims},
        'secondary_newsletter_links': secondary_urls,
        'linked_primary_sources': [{'url': source['url'], 'resolved_url': source['resolved_url'],
            'title': source['title'], 'text': source['text'], 'raw_date_text': source['raw_date_text'],
            'source_published_date': source['published_date'], 'source_type': source['source_type'],
            'fetched_at': source['fetched_at'],
            'images': [{'image_index': image['image_index'], 'url': image['url'],
                        'sha256': image['sha256'], 'mime': image['mime']} for image in source['images']]}
            for source in sources],
        'source_acquisition_attempts': attempts, 'source_media': source_media}
    request_key = None
    reserved = False
    response = None
    audit_identity = None

    def audit_ready(identity):
        nonlocal audit_identity
        audit_identity = identity

    def before_send():
        nonlocal reserved
        previous = attempt_store.lookup(request_key)
        if previous is not None:
            raise AttemptBlocked(previous)
        if storage_check is not None:
            with _measure(timing, 'storage_admission_seconds'):
                storage_check(base_review)
        if request_gate is not None:
            request_gate(request_key, base_review)
        attempt_store.reserve(request_key, request_json)
        reserved = True
        counters['model_request_attempts'] += 1

    try:
        _validate_source_review(base_review)
        source_data['evidence_catalog'] = build_evidence_catalog(occurrence, sources)
        schema = historical_response_schema(source_data['evidence_catalog'], sources)
        if request_gate is not None and attempt_store is None:
            raise ReviewDeferred('attempt_store_required',
                'Operational review admission requires the canonical historical attempt store; no model request was sent.')
        kwargs = {'response_schema': schema, 'audit_ready': audit_ready,
                  'audit_context': {'logical_request_sha256': request_key, 'rules_version': RULES_VERSION}}
        if attempt_store is not None:
            model_name = acquisition._model_config().model_name
            request_key, request_json = logical_request(occurrence, original_claims, source_data,
                rules_version=RULES_VERSION, model_name=model_name)
            kwargs['audit_context']['logical_request_sha256'] = request_key
            kwargs.update(before_send=before_send, audit_directory=attempt_store.audit_directory)
            previous = attempt_store.lookup(request_key)
            if previous is not None:
                raise AttemptBlocked(previous)
        from nannanews.source_media import MEDIA_POLICY_INSTRUCTIONS
        prepared()
        with _measure(timing, 'model_request_seconds'):
            response, model_name = acquisition.request_model_json(HISTORICAL_REVIEW_INSTRUCTIONS + '\n\n' + MEDIA_POLICY_INSTRUCTIONS,
                source_data, media=media, **kwargs)
        with _measure(timing, 'model_response_validation_seconds'):
            canonical_response = decode_evidence_response(response, occurrence,
                sources=sources, mismatched=mismatched)
            decision = decision_from_response(canonical_response, model_name, occurrence,
                sources=sources, attempts=attempts, mismatched=mismatched)
        if audit_identity is not None:
            decision['_model_audit'] = audit_identity
            try:
                finalize_receipt(audit_identity, outcome='validated')
            except Exception:
                raise acquisition.AcquisitionError('Model response is preserved but private receipt finalization failed; no automatic retry is allowed.',
                    'model_audit_failed', 503) from None
    except ReviewDeferred as error:
        counters['deferred_reviews'] += 1
        return diagnosed({'claims': [], 'rules_version': RULES_VERSION, 'origin': 'luna_review',
            'reviewer': 'luna:unavailable', 'status': 'unresolved', 'reason': error.reason,
            'source_review': _validate_source_review(base_review), '_review_deferred': error.code})
    except StorageBlocked as error:
        counters['preparation_failures'] += 1
        counters['storage_blocked'] += 1
        decision = _unresolved_decision(dict(base_review, sources=[], attempts=[]), str(error))
        decision['_storage_blocked'] = {'capture_artifact': error.capture_artifact, 'reason': str(error)}
        return diagnosed(decision)
    except AttemptBlocked as error:
        previous = error.previous
        original_occurrence = (original_occurrence_resolver(previous['request_sha256'])
            if previous.get('state') == 'completed' and original_occurrence_resolver is not None else None)
        previous_decision = reuse_completed_review(previous, occurrence, original_claims,
            model_name=model_name, original_occurrence=original_occurrence)
        if previous_decision is not None:
            counters['reused_reviews'] += 1
            return diagnosed(previous_decision)
        counters['blocked_requests'] += 1
        reason = (str(error) if previous['state'] == 'reserved' else
            'An equivalent model request already finished but its verdict is not eligible for reuse; no new AI judgment was made and the existing date evidence is unchanged.')
        decision = _unresolved_decision(base_review, reason)
        decision['_request_pending'] = previous['request_sha256']
        decision['_request_state'] = previous['state']
        return diagnosed(decision)
    except Exception as error:
        reason = str(error) if isinstance(error, acquisition.AcquisitionError) else (
            'Historical model evidence failed strict validation; the original response is retained privately.'
            if response is not None else 'Historical model request preparation failed; no request was sent.')
        decision = _unresolved_decision(base_review, reason)
        code = getattr(error, 'code', 'model_evidence_invalid' if response is not None else 'model_preparation_failed')
        decision['_model_failure_code'] = code
        audit_identity = getattr(error, '_model_audit', audit_identity)
        if getattr(error, '_model_audit_finalize_failed', False):
            decision['_receipt_finalize_failed'] = True
        if audit_identity is not None:
            decision['_model_audit'] = audit_identity
            try:
                finalize_receipt(audit_identity, outcome='failed', failure_code=code)
            except Exception:
                decision['_receipt_finalize_failed'] = True
        if not reserved:
            counters['preparation_failures'] += 1
            decision['_preparation_failed'] = True
        if reserved:
            try:
                attempt_store.finish(request_key, decision=decision,
                    raw_output=getattr(error, '_model_response', response), error=reason)
            except Exception:
                # Reservation remains permanent. Durable wire files already exist.
                decision['_receipt_finalize_failed'] = True
                decision['_model_failure_code'] = 'model_audit_failed'
            decision['_paid_failure'] = True
        return diagnosed(decision)
    if reserved:
        try:
            attempt_store.finish(request_key, decision=decision, raw_output=response)
        except Exception:
            decision = _unresolved_decision(base_review,
                'Model response is preserved but the attempt receipt could not be finalized; no automatic retry is allowed.')
            decision.update(_paid_failure=True, _receipt_finalize_failed=True,
                            _model_failure_code='model_audit_failed')
            if audit_identity is not None:
                decision['_model_audit'] = audit_identity
                try:
                    finalize_receipt(audit_identity, outcome='ledger_finalize_failed', failure_code='model_audit_failed')
                except Exception:
                    pass
    return diagnosed(decision)


def date_snapshot(db):
    """Read immutable bulletin evidence and canonical same-activity context."""
    from nannanews import bulletin_activities

    canonical_ids = bulletin_activities.activity_map(db)
    rows = [dict(row) for row in db.execute('''SELECT o.id,o.item_id,o.document_id,o.local_key,o.kind,
        o.title,o.body_text,o.locator_json,d.source_sha256,i.issue_date,b.identity_key
        FROM bulletin_occurrences o JOIN bulletin_documents d ON d.id=o.document_id
        JOIN bulletin_issues i ON i.id=d.issue_id JOIN bulletin_items b ON b.id=o.item_id
        ORDER BY coalesce(i.issue_date,'') DESC,o.document_id DESC,
                 CASE o.kind WHEN 'detail' THEN 0 ELSE 1 END,o.id DESC''')]
    from nannanews import bulletin_source_repair
    linked_sources = defaultdict(list)
    for source in db.execute('SELECT occurrence_id,source_url FROM ' + bulletin_source_repair.sources_table(db)
                             + ' ORDER BY occurrence_id,source_url'):
        linked_sources[source['occurrence_id']].append(source['source_url'])
    corrections = bulletin_source_repair.public_corrections(db)
    for row in rows:
        row['source_urls'] = linked_sources[row['id']]
        row['source_corrections'] = corrections[row['id']]
    originals = defaultdict(list)
    for row in db.execute('''SELECT occurrence_id,kind,raw_text,normalized_date,resolution,evidence,locator_json
                             FROM bulletin_dates ORDER BY id'''):
        claim = dict(row)
        occurrence_id = claim.pop('occurrence_id')
        claim['locator'] = json.loads(claim.pop('locator_json') or 'null')
        originals[occurrence_id].append(claim)
    publications = defaultdict(dict)
    members = defaultdict(list)
    for row in rows:
        canonical = canonical_ids[row['item_id']]
        members[canonical].append(row)
        key = (row['kind'], row['title'], row['body_text'], tuple(row['source_urls']), _json(row['source_corrections']))
        record = publications[canonical].setdefault(key, {
            'kind': row['kind'], 'title': row['title'], 'body_text': row['body_text'],
            'source_urls': row['source_urls'], 'issue_dates': set(),
            'source_corrections': row['source_corrections']})
        record['issue_dates'].add(row['issue_date'])
    context_by_canonical = {}
    for canonical, records in publications.items():
        evidence = [dict(record, issue_dates=sorted(record['issue_dates'], key=lambda value: value or ''))
                    for record in records.values()]
        identity = {'item_ids': sorted({row['item_id'] for row in members[canonical]}),
                    'occurrence_ids': sorted(row['id'] for row in members[canonical]),
                    'publications': evidence}
        context_by_canonical[canonical] = (evidence, hashlib.sha256(_json(identity).encode()).hexdigest())
    for row in rows:
        related, context_digest = context_by_canonical[canonical_ids[row['item_id']]]
        row['related_occurrences'] = related
        row['related_context_sha256'] = context_digest
    heads = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bulletin_date_revisions'").fetchone():
        columns = 'id,occurrence_id,input_sha256,revision_sha256,rules_version,origin,reviewer,status,reason,created_at'
        heads = {row['occurrence_id']: dict(row) for row in db.execute(
            'SELECT ' + columns + ' FROM (' + current_revisions_sql(db) + ')')}
    return rows, originals, heads


def _fingerprint(rows, originals, heads):
    return hashlib.sha256(_json([(row['id'], input_digest(row, originals[row['id']]),
        heads.get(row['id'], {}).get('revision_sha256'), row.get('related_context_sha256')) for row in rows]).encode()).hexdigest()


def _public_id(prefix, value):
    return prefix + hashlib.sha256(value.encode()).hexdigest()[:24]


def _read_review_values(path):
    text = path.read_text(encoding='utf-8')
    if not text.strip():
        return []
    try:
        values = json.loads(text)
    except json.JSONDecodeError:
        try:
            values = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError:
            raise ValueError('Review input must be a JSON array or JSONL review artifact.') from None
    if isinstance(values, dict):
        values = [values]
    if not isinstance(values, list):
        raise ValueError('Review input must be a JSON decision array.')
    return values


def _review_decisions(files, rows, originals):
    """Admit only current-rule reviews bound to all current input/context."""
    by_id = {_public_id('occ-', row['source_sha256'] + ':' + row['local_key']): row for row in rows}
    decisions = {}
    fields = {'item_id', 'occurrence_id', 'document_sha256', 'local_key', 'status', 'claims',
              'reason', 'rules_version', 'reviewer', 'source_review', 'input_sha256',
              'related_context_sha256'}
    for filename in files:
        path = Path(filename)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024 * 1024:
            raise ValueError('Review input must be a bounded regular JSON file.')
        values = _read_review_values(path)
        for value in values:
            if not isinstance(value, dict) or set(value) != fields:
                raise ValueError('Prepared review requires a fresh input-bound date review with current rules and context.')
            if any(not isinstance(value[key], str) for key in ('item_id', 'occurrence_id', 'document_sha256', 'local_key', 'status', 'reason')):
                raise ValueError('Review decision has invalid identity or verdict fields.')
            row = by_id.get(value['occurrence_id'])
            if (row is None or value['item_id'] != _public_id('item-', row['identity_key'])
                    or value['document_sha256'] != row['source_sha256'] or value['local_key'] != row['local_key']):
                raise ValueError('Review decision does not match immutable source identity.')
            if value['rules_version'] != RULES_VERSION:
                raise ValueError('Prepared date review rules changed; prepare a fresh current-rule review.')
            if (value['related_context_sha256'] != row['related_context_sha256']
                    or value['input_sha256'] != input_digest(row, originals[row['id']])):
                raise ValueError('Prepared date review input changed; prepare a fresh review.')
            if row['id'] in decisions:
                raise ValueError('Duplicate review decision for an occurrence.')
            if (value['status'] not in STATUSES
                    or not value['reason'].strip() or len(value['reason']) > 4000):
                raise ValueError('Review decision requires a supported status and explanation.')
            if not isinstance(value['reviewer'], str) or not value['reviewer'].strip():
                raise ValueError('Prepared review requires model and reviewer metadata.')
            source_review = _validate_source_review(value['source_review'], require_activity=True)
            allowed_urls = set(_occurrence_source_urls(row))
            for source in source_review['sources']:
                if source['url'] not in allowed_urls:
                    raise ValueError('Prepared source review includes a URL not linked to this same item.')
            for attempt in source_review['attempts']:
                if attempt['url'] not in allowed_urls:
                    raise ValueError('Prepared source attempt includes a URL not linked to this same item.')
            claims = validate_claims(value['claims'])
            _validate_review_claim_sources(claims, row, source_review)
            if value['status'] == 'resolved' and not any(claim['normalized_date'] for claim in claims):
                raise ValueError('Resolved review must supply a supported calendar fact: ' + value['occurrence_id'])
            decisions[row['id']] = dict(value, claims=claims,
                source_review=source_review, origin='luna_review')
    return decisions


def _counts(rows, claims, statuses=None):
    groups = defaultdict(list)
    statuses = statuses or {}
    for row in rows:
        groups[(row['item_id'], row['document_id'])].extend(
            [] if statuses.get(row['id']) in {'recurring_no_end', 'uncertain', 'invalid_source'} else claims[row['id']])
    expiry = {}
    for key, dates in groups.items():
        deadlines = [c['normalized_date'] for c in dates if c['kind'] == 'deadline'
                     and c['resolution'] in KNOWN_RESOLUTIONS and c['normalized_date']]
        events = [c['normalized_date'] for c in dates if c['kind'] == 'event'
                  and c['resolution'] in KNOWN_RESOLUTIONS and c['normalized_date']]
        expiry[key] = max(deadlines or events, default=None)
    latest = {}
    unknown = set()
    for row in rows:
        day = expiry[(row['item_id'], row['document_id'])]
        latest.setdefault(row['item_id'], day)
        if day is None:
            unknown.add(row['item_id'])
    return {'items': len(latest), 'occurrences': len(rows), 'unknown_items': len(unknown),
            'latest_unknown_items': sum(day is None for day in latest.values()),
            'unknown_publication_groups': sum(day is None for day in expiry.values()),
            'claim_resolutions': dict(Counter(c['resolution'] for values in claims.values() for c in values))}



def _effective_claims(db, rows, originals, heads):
    effective = {row['id']: [] for row in rows}
    has_view = db.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name='bulletin_effective_dates'").fetchone()
    if has_view:
        query = 'SELECT occurrence_id,kind,raw_text,normalized_date,resolution,evidence,locator_json FROM bulletin_effective_dates ORDER BY id'
        evidence_rows = db.execute(query)
    elif not heads:
        for row in rows:
            effective[row['id']] = originals[row['id']]
        return effective
    else:
        return effective
    for row in evidence_rows:
        claim = dict(row)
        occurrence_id = claim.pop('occurrence_id')
        claim['locator'] = json.loads(claim.pop('locator_json') or 'null')
        effective.setdefault(occurrence_id, []).append(claim)
    return effective


def plan_repairs(db, *, review_files=()):
    """Build an append-only plan from prepared AI reviews only; never calls a model."""
    rows, originals, heads = date_snapshot(db)
    reviews = _review_decisions(review_files, rows, originals)
    effective_before = _effective_claims(db, rows, originals, heads)
    effective = {occurrence_id: list(claims) for occurrence_id, claims in effective_before.items()}
    statuses = {occurrence_id: head['status'] for occurrence_id, head in heads.items()
                if head['origin'] == 'luna_review'}
    statuses_before = dict(statuses)
    entries = []
    for row in rows:
        review = reviews.get(row['id'])
        if review is None:
            continue
        effective[row['id']] = review['claims']
        statuses[row['id']] = review['status']
        entries.append({'occurrence': row, 'original_claims': originals[row['id']],
            'claims': review['claims'], 'rules_version': review['rules_version'],
            'origin': 'luna_review', 'reviewer': review['reviewer'], 'status': review['status'],
            'reason': review['reason'], 'source_review': review['source_review']})
    return {'snapshot_sha256': _fingerprint(rows, originals, heads), 'entries': entries,
            'summary': {'before': _counts(rows, effective_before, statuses_before), 'after': _counts(rows, effective, statuses),
                        'reviewed_occurrences': len(reviews),
                        'preserved_manual_occurrences': sum(head['origin'] == 'luna_review' for head in heads.values()),
                        'statuses': dict(Counter(entry['status'] for entry in entries))}}


def apply_repairs(db, plan):
    """Append decisions atomically to a prepared component schema; reject stale heads."""
    db.commit()
    with db:
        db.execute('BEGIN IMMEDIATE')
        rows, originals, heads = date_snapshot(db)
        if _fingerprint(rows, originals, heads) != plan['snapshot_sha256']:
            raise ValueError('Date repair snapshot changed; create a fresh dry-run plan.')
        changed = 0
        for entry in plan['entries']:
            changed += record_revision(db, entry['occurrence'], entry['original_claims'], entry['claims'],
                **{key: entry[key] for key in ('rules_version', 'origin', 'reviewer', 'status', 'reason', 'source_review')})
    return changed


def _append_private_line(path, value):
    if path.is_symlink():
        raise ValueError('Review artifact files must not be symlinks.')
    line = (_json(value) + '\n').encode('utf-8')
    if path.exists():
        if not path.is_file() or path.stat().st_size + len(line) > 128 * 1024 * 1024:
            raise ValueError('Review artifact exceeds its private size limit.')
        path.chmod(0o600)
        stream = path.open('ab')
    else:
        stream = path.open('xb')
        path.chmod(0o600)
    with stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())


def review_occurrence(row, original_claims, *, model_name, source_cache, attempt_store, storage,
                      counters, legacy_interrupted=False, request_gate=None, original_occurrence_resolver=None):
    """Persist one bound receipt or nonterminal capture under the caller's lock.

    Both manual and full review use this lifecycle. The caller owns selection,
    resume and output locking; no database transaction spans model transport.
    """
    from nannanews.bulletin_historical_attempts import StorageBlocked
    review_started = time.monotonic()
    timing = {}

    identity = {'item_id': _public_id('item-', row['identity_key']),
        'occurrence_id': _public_id('occ-', row['source_sha256'] + ':' + row['local_key']),
        'document_sha256': row['source_sha256'], 'local_key': row['local_key'],
        'rules_version': RULES_VERSION, 'reviewer': 'luna:' + model_name,
        'input_sha256': input_digest(row, original_claims),
        'related_context_sha256': row['related_context_sha256']}
    attempts_before = counters['model_request_attempts']
    if legacy_interrupted:
        counters['blocked_requests'] += 1
        decision = _unresolved_decision({'status': 'source_unavailable' if _occurrence_source_urls(row) else 'secondary_only',
            'reason': 'A previous review attempt was interrupted; it was not retried.', 'sources': [], 'attempts': []},
            'A previous AI review attempt did not save a result; this occurrence was not retried.')
        decision['_request_state'] = 'legacy_interrupted'
    else:
        try:
            decision = model_decision(row, original_claims, source_cache=source_cache,
                attempt_store=attempt_store, counters=counters,
                storage_check=lambda source_review: storage.admit(source_review, identity),
                request_gate=request_gate, original_occurrence_resolver=original_occurrence_resolver, _timing=timing)
        except Exception as error:
            decision = _unresolved_decision({'status': 'source_unavailable' if _occurrence_source_urls(row) else 'secondary_only',
                'reason': 'The AI review failed before a prepared verdict was saved.', 'sources': [], 'attempts': []},
                str(error) or error.__class__.__name__)
            if counters['model_request_attempts'] == attempts_before:
                counters['preparation_failures'] += 1
            decision['_preparation_failed'] = True
    record = dict(identity, status=decision['status'], claims=decision['claims'],
        reason=decision['reason'], source_review=decision['source_review'])
    _validate_source_review(record['source_review'], require_activity=True)
    result = {'terminal': False, 'record': record, 'progress': None,
        'new_request': counters['model_request_attempts'] > attempts_before,
        'paid_failure': bool(decision.get('_paid_failure'))}
    if decision.get('_reuse'):
        result['_reuse'] = decision['_reuse']
    if result['new_request'] and result['paid_failure'] and decision.get('_model_failure_code'):
        result['model_failure_code'] = decision['_model_failure_code']
    timing.setdefault('review_preparation_seconds', time.monotonic() - review_started)
    storage_started = time.monotonic()

    def finished():
        timing['review_storage_seconds'] = time.monotonic() - storage_started
        timing['review_seconds'] = time.monotonic() - review_started
        result['_timing'] = dict(timing)
        return result

    details = {}
    if decision.get('_review_deferred'):
        kind = 'review_deferred'
        details['defer_code'] = decision['_review_deferred']
    elif decision.get('_preparation_failed'):
        kind = 'review_preparation_failed'
    elif decision.get('_request_state'):
        state = decision['_request_state']
        kind = ('legacy_attempt_interrupted' if state == 'legacy_interrupted' else
                'equivalent_request_reserved' if state == 'reserved' else 'equivalent_request_not_reusable')
        details.update(request_sha256=decision.get('_request_pending'), request_state=state)
    else:
        blocker = decision.get('_storage_blocked')
        if blocker is None:
            try:
                storage.check_record(record)
            except StorageBlocked as error:
                counters['storage_blocked'] += 1
                blocker = {'capture_artifact': error.capture_artifact, 'reason': str(error)}
        if blocker is None:
            _append_private_line(storage.artifact, record)
            attempt_store.record_outcome(record, model_name)
            result['terminal'] = True
            return finished()
        kind = 'review_storage_blocked'
        details.update(blocker)
    progress = storage.compact_progress(record, progress=kind, _timing=dict(timing), **details)
    progress['source_review']['activity'] = None
    _append_private_line(storage.progress_path(progress), progress)
    counters['pending_reviews'] += 1
    result.update(details, progress=kind, capture_artifact=progress['capture_artifact'])
    return finished()


def review_dates(*, output_dir, settings=None, occurrence_ids=(), selection='pending', limit=None, source_cache=None):
    """Create/resume private JSONL reviews, optionally reusing public fetch captures."""
    from nannanews import acquisition
    from nannanews import runtime
    from nannanews.bulletin_historical_attempts import AttemptStore, ReviewStorage, lock_output

    settings = settings or runtime.settings
    model_name = acquisition._model_config().model_name
    if source_cache is None:
        source_cache = {}
    if not isinstance(source_cache, dict):
        raise ValueError('Source cache must be a URL-to-capture mapping.')
    if selection not in {'pending', 'unknown', 'all'}:
        raise ValueError('Review selection must be pending, unknown or all.')
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000):
        raise ValueError('Review limit must be between 1 and 10000.')
    requested = list(occurrence_ids or [])
    if (len(requested) > 10000 or any(not isinstance(value, str) or not value.startswith('occ-')
                                      for value in requested) or len(set(requested)) != len(requested)):
        raise ValueError('Occurrence selectors must be distinct public occurrence IDs.')
    database = Path(settings.database_path).resolve(strict=True)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        rows, originals, heads = date_snapshot(db)
        effective = _effective_claims(db, rows, originals, heads)
    public_rows = {_public_id('occ-', row['source_sha256'] + ':' + row['local_key']): row for row in rows}
    if requested and set(requested) - public_rows.keys():
        raise ValueError('One or more selected occurrence IDs do not exist in the current snapshot.')
    output = Path(output_dir).expanduser().absolute()
    if output.is_symlink():
        raise ValueError('Review output directory must not be a symlink.')
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not output.is_dir():
        raise ValueError('Review output path must be a directory.')
    output.chmod(0o700)
    with lock_output(output):
        artifact = output / 'reviews.jsonl'
        progress_artifact = output / 'review-progress.jsonl'
        storage = ReviewStorage(artifact, output, response_bytes=acquisition.MAX_MODEL_BODY)
        attempted_file = output / '.attempted'
        if artifact.is_symlink() or (artifact.exists() and
                (not artifact.is_file() or artifact.stat().st_size > 128 * 1024 * 1024)):
            raise ValueError('Review artifact must be a bounded regular file.')
        if artifact.exists() and artifact.read_text(encoding='utf-8').lstrip().startswith('['):
            raise ValueError('Resumable review output must use its reviews.jsonl artifact path.')
        existing_values = _read_review_values(artifact) if artifact.exists() else []
        finished_ids = {value.get('occurrence_id') for value in existing_values if isinstance(value, dict)}
        current_values = []
        for value in existing_values:
            row = public_rows.get(value.get('occurrence_id')) if isinstance(value, dict) else None
            if (row is not None and value.get('rules_version') == RULES_VERSION
                    and value.get('input_sha256') == input_digest(row, originals[row['id']])
                    and value.get('related_context_sha256') == row['related_context_sha256']
                    and value.get('reviewer') == 'luna:' + model_name):
                current_values.append(value)
        if len(current_values) != len(existing_values):
            # Keep old receipts byte-for-byte, but never admit them as this audit.
            previous = output / ('reviews.previous-' + hashlib.sha256(artifact.read_bytes()).hexdigest() + '.jsonl')
            if previous.is_symlink():
                raise ValueError('Previous review receipt must not be a symlink.')
            artifact.replace(previous)
            previous.chmod(0o600)
            for value in current_values:
                _append_private_line(artifact, value)
        if current_values:
            _review_decisions((artifact,), rows, originals)
        completed_ids = {value['occurrence_id'] for value in current_values}
        if attempted_file.is_symlink() or (attempted_file.exists() and
                (not attempted_file.is_file() or attempted_file.stat().st_size > 16 * 1024 * 1024)):
            raise ValueError('Review attempt cache must be a bounded regular private file.')
        try:
            attempted_ids = {json.loads(line) for line in attempted_file.read_text(encoding='utf-8').splitlines()
                             if line.strip()} if attempted_file.exists() else set()
        except (ValueError, TypeError):
            raise ValueError('Review attempt cache contains an invalid JSONL identity.') from None
        if any(not isinstance(value, str) or value not in public_rows for value in attempted_ids):
            raise ValueError('Review attempt cache does not match the current bulletin snapshot.')
        # Legacy markers without any receipt remain interrupted, never automatically retried.
        attempted_ids -= finished_ids
        attempt_store = AttemptStore(database)
        for value in current_values:
            attempt_store.record_outcome(value, model_name)
        bound_outcomes = attempt_store.bound_outcomes()
        chosen = []
        for occurrence_id in (requested or list(public_rows)):
            row = public_rows[occurrence_id]
            head = heads.get(row['id'])
            binding = (occurrence_id, input_digest(row, originals[row['id']]),
                       row['related_context_sha256'], RULES_VERSION, model_name)
            current_head = bool(head and head['origin'] == 'luna_review'
                and head['rules_version'] == RULES_VERSION and head['input_sha256'] == binding[1]
                and binding in bound_outcomes)
            claims = effective.get(row['id'], [])
            unknown = not any(claim['kind'] in {'event', 'deadline'} and claim['normalized_date']
                              and claim['resolution'] in KNOWN_RESOLUTIONS for claim in claims)
            eligible = selection == 'all' or (selection == 'pending' and not current_head)
            if selection == 'unknown':
                eligible = unknown
            if requested or eligible:
                chosen.append((occurrence_id, row))
        if limit is not None:
            chosen = chosen[:limit]
        statuses, counters = Counter(), Counter()
        for occurrence_id, row in chosen:
            if occurrence_id in completed_ids:
                continue
            result = review_occurrence(row, originals[row['id']], model_name=model_name,
                source_cache=source_cache, attempt_store=attempt_store, storage=storage,
                counters=counters, legacy_interrupted=occurrence_id in attempted_ids)
            if result['terminal']:
                completed_ids.add(occurrence_id)
                statuses[result['record']['status']] += 1
        if not artifact.exists():
            artifact.touch(mode=0o600)
        artifact.chmod(0o600)
        return {'artifact': str(artifact), 'selected': len(chosen), 'completed': len(completed_ids),
                'new_reviews': sum(statuses.values()),
                'model_request_attempts': counters['model_request_attempts'],
                'pending_reviews': counters['pending_reviews'], 'progress_artifact': str(progress_artifact),
                'blocked_requests': counters['blocked_requests'], 'reused_reviews': counters['reused_reviews'],
                'storage_blocked': counters['storage_blocked'], 'progress_artifacts': sorted(storage.progress_paths),
                'preparation_failures': counters['preparation_failures'], 'statuses': dict(statuses)}


def repair_dates(*, settings=None, review_files=(), apply=False, audit_dir=None):
    if settings is None:
        from nannanews import runtime
        settings = runtime.settings
    database = Path(settings.database_path).resolve(strict=True)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        plan = plan_repairs(db, review_files=review_files)
    summary = dict(plan['summary'], applied=False, appended_revisions=0, snapshot_sha256=plan['snapshot_sha256'])
    if audit_dir:
        output = Path(audit_dir)
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(output, 0o700)
        for name, value in (('plan.json', plan), ('summary.json', summary)):
            with (output / name).open('x', encoding='utf-8') as stream:
                os.chmod(stream.name, 0o600)
                stream.write(_json(value) + '\n')
    if apply:
        with closing(sqlite3.connect(database, timeout=30)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            summary['appended_revisions'] = apply_repairs(db, plan)
        summary['applied'] = True
        if audit_dir:
            with (Path(audit_dir) / 'application.json').open('x', encoding='utf-8') as stream:
                os.chmod(stream.name, 0o600)
                stream.write(_json(summary) + '\n')
    return summary
