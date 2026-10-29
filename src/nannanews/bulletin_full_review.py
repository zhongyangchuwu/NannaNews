"""Private serial full-corpus review; decisions and paid-once authority stay canonical.

The scope is immutable. journal/status are operational summaries, never date evidence.
Only the foreground worker may invoke the canonical per-occurrence reviewer.
"""
from collections import Counter, OrderedDict, deque
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import base64
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import tempfile
import threading
import uuid

from nannanews import bulletin_date_repair as repair
from nannanews.bulletin_historical_attempts import AttemptStore, ReviewStorage, MAX_REVIEW_ARTIFACT_BYTES, lock_output

FORMAT = 'nanna-bulletin-full-review-v1'
MIN_FREE_BYTES = 5 * 1024 ** 3
HOT_CACHE_BYTES = 256 * 1024 ** 2
CACHE_RECORD_BYTES = 256 * 1024 ** 2
BATCH_RECORDS = 64
TIMING_WINDOW = 100
BEIJING = timezone(timedelta(hours=8))
TERMINAL_PHASES = {'completed', 'stopped', 'failed'}
OPERATIONAL_PAUSES = {'daily_priority', 'disk_low', 'stop_requested', 'operator_pause',
                      'quality_circuit', 'request_budget', 'scope_changed', 'configuration_changed',
                      'transport_cooldown'}
INTENTIONAL_PAUSES = {'daily_priority', 'operator_pause', 'stop_requested'}
MONITOR_ALERT_FORMAT = 'nanna-full-review-monitor-alerts-v1'
MAX_MONITOR_ALERT_BYTES = 256 * 1024
MAX_MONITOR_ALERT_TRANSITIONS = 128
MODEL_QUALITY_WINDOW = 100
MODEL_QUALITY_ALERT_MIN_REQUESTS = 10
MODEL_QUALITY_ALERT_FAILURE_RATE = 0.25
MODEL_QUALITY_KINDS = ('verified', 'valid_unconfirmed', 'malformed_model_failure',
                       'model_failure_other', 'model_failure_unknown', 'transport_failure',
                       'unknown_outcome')
MODEL_QUALITY_FAILURE_KINDS = {'malformed_model_failure', 'model_failure_other',
                               'model_failure_unknown', 'transport_failure'}
MODEL_TRANSPORT_FAILURE_CODES = {'model_unavailable', 'model_http_error'}
MODEL_MALFORMED_FAILURE_CODES = {'model_response_invalid', 'model_output_invalid',
                                 'model_evidence_invalid'}
SOURCE_COVERAGE_KINDS = ('readable_primary', 'no_readable_primary', 'source_mismatch',
                         'capture_incomplete', 'not_recorded')
# Fixed operator guidance: never copy provider exceptions or model content into alerts.
MONITOR_ALERTS = {
    'quality_circuit': ('error', 'Inspect consecutive paid failures and the cumulative model-quality counters; only an operator may acknowledge quality and resume.'),
    'model_quality_degraded': ('warning', 'Inspect the rolling paid-request quality counts and retained evidence; do not retry, resume or restart automatically.'),
    'prior_attempt_result_missing': ('warning', 'A prior request intent has no retained result in this scope; automatic resubmission is blocked. Inspect the original ledger and audit without clearing protection.'),
    'disk_low': ('error', 'Restore free disk space without deleting paid evidence; inspect worker status before resuming.'),
    'scope_changed': ('error', 'Reconcile the frozen scope with canonical inputs before an operator authorizes further work.'),
    'configuration_changed': ('error', 'Reconcile the frozen model, protocol and instructions before an operator authorizes further work.'),
    'request_budget': ('warning', 'Inspect frozen request-budget exhaustion; do not increase paid permission or retry automatically.'),
    'worker_failed': ('error', 'Inspect retained worker failure evidence before any operator-controlled recovery.'),
    'worker_not_running_or_pid_reused': ('error', 'Inspect unexpected worker exit or PID reuse; do not signal or restart this PID automatically.'),
    'heartbeat_stale': ('error', 'Inspect worker health and retained evidence; do not restart or interrupt an in-flight request automatically.'),
    'progress_stale': ('warning', 'Inspect stalled progress and the current worker operation before taking action.'),
}

def _source_coverage_kind(record):
    review = record.get('source_review') if isinstance(record, dict) else None
    if not isinstance(review, dict):
        return 'not_recorded'
    if review.get('status') == 'source_mismatch':
        return 'source_mismatch'
    attempts = review.get('attempts', [])
    if isinstance(attempts, list) and any(isinstance(attempt, dict) and
            attempt.get('status') in {'mismatch', 'media_unavailable'} for attempt in attempts):
        return 'capture_incomplete'
    sources = review.get('sources', [])
    if isinstance(sources, list) and any(isinstance(source, dict) and
            ((isinstance(source.get('text'), str) and bool(source['text'].strip())) or
             bool(source.get('images'))) for source in sources):
        return 'readable_primary'
    return 'no_readable_primary'


def _model_quality_kind(result, category):
    if result.get('paid_failure'):
        if result.get('model_transport_failure') is True and _transient_model_failure(result):
            return 'transport_failure'
        failure_code = result.get('model_failure_code')
        if isinstance(failure_code, str):
            if failure_code in MODEL_TRANSPORT_FAILURE_CODES:
                return 'transport_failure'
            if failure_code in MODEL_MALFORMED_FAILURE_CODES:
                return 'malformed_model_failure'
            if failure_code == 'model_audit_failed':
                return 'model_failure_other'
        return 'model_failure_unknown'
    record = result.get('record')
    review = record.get('source_review') if isinstance(record, dict) else None
    if isinstance(review, dict) and review.get('status') == 'source_verified' and review.get('activity'):
        return 'verified'
    if category in {'primary_confirmed', 'model_terminal_unconfirmed'} or result.get('terminal'):
        return 'valid_unconfirmed'
    return 'unknown_outcome'


def _transport_fields(result):
    fields = {}
    if result.get('model_transport_failure') is True:
        fields['model_transport_failure'] = True
    status = result.get('model_http_status')
    if type(status) is int and 100 <= status <= 599:
        fields['model_http_status'] = status
    retry = result.get('model_retry_after_seconds')
    if type(retry) in (int, float) and retry > 0 and (type(retry) is int or math.isfinite(retry)):
        fields['model_retry_after_seconds'] = retry
    return fields


def _transient_model_failure(facts):
    status = _transport_fields(facts).get('model_http_status')
    # A truncated refusal is still a refusal, never permission to auto-resume.
    if status is not None and 400 <= status <= 499 and status not in {408, 429}:
        return False
    code = facts.get('model_failure_code')
    if code == 'model_unavailable':
        return True
    if code not in {'model_http_error', 'model_response_invalid'}:
        # Audit/preparation/evidence failures stay manual even if earlier HTTP
        # facts survive alongside their final, authoritative failure code.
        return False
    if facts.get('model_transport_failure') is True:
        return True
    return code == 'model_http_error' and (status in {408, 429} or
        (status is not None and 500 <= status <= 599))


def _cooldown_remaining(until, now):
    return max(0, (datetime.fromisoformat(until) - now).total_seconds()) if until else 0


def _model_quality_degraded(metrics):
    rolling = metrics.get('rolling') if isinstance(metrics, dict) else None
    if not isinstance(rolling, dict):
        return False
    window_limit = metrics.get('window_limit')
    sample_size = rolling.get('sample_size')
    failures = rolling.get('failures')
    return (type(window_limit) is int and 0 < window_limit <= MODEL_QUALITY_WINDOW
            and type(sample_size) is int and MODEL_QUALITY_ALERT_MIN_REQUESTS <= sample_size <= window_limit
            and type(failures) is int and 0 <= failures <= sample_size
            and failures / sample_size >= MODEL_QUALITY_ALERT_FAILURE_RATE)


def _now():
    return datetime.now(timezone.utc)


def _stamp():
    return _now().isoformat()


def _digest(value):
    digest = hashlib.sha256()
    for part in json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                                 allow_nan=False).iterencode(value):
        digest.update(part.encode('utf-8'))
    return digest.hexdigest()


def _safe_path(path):
    path = Path(path).expanduser().absolute()
    if '..' in path.parts:
        raise ValueError('Full review artifacts must not traverse parent directories.')
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError('Full review artifacts must not traverse symlinks.')
    return path


def _directory(path, *, create=True):
    path = _safe_path(path)
    if create:
        missing = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)
    if not path.is_dir():
        raise ValueError('Full review directory is missing.')
    if create:
        path.chmod(0o700)
    elif stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError('Full review directory must be private.')
    return path


def _file(path, maximum=MAX_REVIEW_ARTIFACT_BYTES):
    path = _safe_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        raise ValueError('Full review input must be a bounded regular file.')
    return path


def _load(path, maximum=MAX_REVIEW_ARTIFACT_BYTES):
    return json.loads(_file(path, maximum).read_text(encoding='utf-8'))


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic(path, value):
    path = _safe_path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.write-', delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            for part in json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                                         allow_nan=False).iterencode(value):
                stream.write(part.encode('utf-8'))
            stream.write(b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        _sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _append(path, value):
    _safe_path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'a', encoding='utf-8', closefd=False) as stream:
            stream.write(repair._json(value) + '\n')
            stream.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _lines(path):
    path = _file(path)
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


@contextmanager
def _readonly(database):
    database = _file(database, maximum=2 ** 63 - 1)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        yield db


def _configuration(settings):
    from nannanews import acquisition
    from nannanews import content_rules
    from nannanews import historical_evidence_refs
    from nannanews import model_audit
    from nannanews.source_media import REMOTE_MEDIA_POLICY_VERSION
    return {'model': settings.model_name, 'protocol': settings.model_api_style,
            'rules_version': repair.RULES_VERSION,
            'instructions_sha256': hashlib.sha256(repair.HISTORICAL_REVIEW_INSTRUCTIONS.encode()).hexdigest(),
            'source_capture_version': acquisition.SOURCE_CAPTURE_VERSION,
            'source_media_policy_version': REMOTE_MEDIA_POLICY_VERSION,
            'historical_request_version': content_rules.HISTORICAL_REQUEST_VERSION,
            'model_audit_version': model_audit.VERSION,
            'response_schema_version': historical_evidence_refs.SCHEMA_VERSION,
            'max_model_request_bytes': acquisition.MAX_MODEL_REQUEST_BYTES,
            'max_model_response_bytes': acquisition.MAX_MODEL_BODY}


def _public_id(row):
    return repair._public_id('occ-', row['source_sha256'] + ':' + row['local_key'])


def _signature(db):
    # Only immutable/append-only bulletin inputs and derived heads, never daily tables.
    result = {}
    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'bulletin_%' ORDER BY name"):
        name = row[0]
        if not name.replace('_', '').isalnum():
            raise ValueError('Invalid bulletin table name.')
        try:
            count, maximum = db.execute('SELECT count(*),max(rowid) FROM "' + name + '"').fetchone()
        except sqlite3.OperationalError:
            count = db.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0]
            maximum = None
        result[name] = [count, maximum]
    return result


def _snapshot(database):
    with _readonly(database) as db:
        db.execute('BEGIN')
        rows, originals, heads = repair.date_snapshot(db)
        signature = _signature(db)
        # Canonical context contains all grouped item IDs, including accepted merges.
        canonical_count = len({row['related_context_sha256'] for row in rows})
        fingerprint = repair._fingerprint(rows, originals, heads)
    return rows, originals, fingerprint, signature, canonical_count


def prepare_full_review(output_dir, acceptance_file, settings=None, *, resume_from=None,
                        recover_unpaid_sources=False):
    """Freeze every public occurrence, reading MAIN and acceptance without writes."""
    if settings is None:
        from nannanews import runtime
        settings = runtime.settings
    database = _file(settings.database_path, 2 ** 63 - 1).resolve(strict=True)
    output = _safe_path(output_dir)
    if output.exists():
        raise ValueError('Prepare requires a new output directory.')
    if recover_unpaid_sources and resume_from is None:
        raise ValueError('Source recovery requires a previous immutable output scope.')
    acceptance = _load(acceptance_file)
    config = _configuration(settings)
    if (acceptance.get('schema_version') != 1 or acceptance.get('accepted') is not True or acceptance.get('automatic_live_apply') is not False
            or config['model'] != 'gpt-6-luna' or config['protocol'] != 'responses'
            or any(acceptance.get(key) != value for key, value in config.items())):
        raise ValueError('Accepted samples do not bind the current full-review configuration.')
    rows, originals, fingerprint, signature, canonical_count = _snapshot(database)
    by_id = {_public_id(row): row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError('Public occurrence identities are not unique.')
    samples = acceptance.get('samples')
    if not isinstance(samples, list) or len(samples) != 3:
        raise ValueError('Exactly three accepted sample receipts are required.')
    seeds, seed_ids = [], set()
    for sample in samples:
        path = _file(sample['review_file'])
        if hashlib.sha256(path.read_bytes()).hexdigest() != sample['sha256']:
            raise ValueError('Accepted receipt SHA changed.')
        admitted = repair._review_decisions((path,), rows, originals)
        values = repair._read_review_values(path)
        if len(values) != 1 or len(admitted) != 1:
            raise ValueError('Each accepted sample must bind exactly one occurrence.')
        record = values[0]
        if (record['occurrence_id'] != sample['occurrence_id'] or record['occurrence_id'] in seed_ids
                or record['reviewer'] != 'luna:' + config['model']
                or record['source_review']['status'] != sample['source_status']):
            raise ValueError('Accepted receipt metadata does not match current admitted evidence.')
        seed_ids.add(record['occurrence_id'])
        seeds.append({'occurrence_id': record['occurrence_id'], 'sha256': sample['sha256'],
                      'source_file': str(path), 'record_sha256': _digest(record), 'record': record})
    scope = {'format': FORMAT, 'created_at': _stamp(), 'run_id': uuid.uuid4().hex,
             'database_path': str(database), 'configuration': config, 'fingerprint': fingerprint,
             'signature': signature, 'canonical_count': canonical_count, 'occurrence_count': len(rows),
             'max_new_requests': len(rows) - len(seeds), 'batch_records': BATCH_RECORDS,
             'shard_bytes': MAX_REVIEW_ARTIFACT_BYTES,
             'acceptance_sha256': hashlib.sha256(_file(acceptance_file).read_bytes()).hexdigest(),
             'seeds': [{key: value for key, value in seed.items() if key != 'record'} for seed in seeds],
             'occurrences': [{'occurrence_id': identity, 'input_sha256': repair.input_digest(row, originals[row['id']]),
                              'related_context_sha256': row['related_context_sha256']}
                             for identity, row in by_id.items()]}
    _directory(output)
    seed_dir = _directory(output / 'seeds')
    for index, seed in enumerate(seeds):
        _atomic(seed_dir / (str(index) + '.json'), seed['record'])
    if resume_from is not None:
        from nannanews.bulletin_full_resume import resume_previous_output
        scope['resume'] = resume_previous_output(resume_from, output, scope, rows, originals,
            settings, recover_unpaid_sources=recover_unpaid_sources)
    _atomic(output / 'scope.json', scope)
    _atomic(output / 'status.json', {'format': FORMAT, 'run_id': scope['run_id'], 'phase': 'prepared',
            'terminal': False, 'heartbeat_at': _stamp(), 'occurrence_count': len(rows), 'seeded': 3})
    return {'output': str(output), 'scope': str(output / 'scope.json'), 'occurrences': len(rows),
            'canonical_activities': canonical_count, 'seeded': len(seeds),
            'max_new_requests': scope['max_new_requests'], 'resume': scope.get('resume')}


class SourceCache(dict):
    """Exact-URL immutable disk captures with bounded LRU; no draft/model fields."""
    DOCUMENT_FIELDS = ('canonical_url', 'resolved_url', 'title', 'text', 'images', 'fetched_at',
        'source_type', 'raw_date_text', 'raw_response_base64', 'raw_response_digest',
        'content_type', 'source_published_date', 'capture_complete', 'capture_errors',
        'source_capture_version', 'pdf_sources', 'pdf_documents', 'media_policy', 'skipped_media')

    def __init__(self, directory, database, *, hot_bytes=HOT_CACHE_BYTES, on_capture=None):
        super().__init__()
        self.directory = _directory(directory)
        self.database = Path(database)
        self.hot_bytes = hot_bytes
        self.hot = OrderedDict()
        self.hot_size = 0
        self.on_capture = on_capture

    def _path(self, url):
        return self.directory / (hashlib.sha256(url.encode()).hexdigest() + '.json')

    @staticmethod
    def _validate(url, capture, *, reusable=False):
        from nannanews.bulletin_common import source_url as canonical_source_url
        if canonical_source_url(url) != url:
            raise ValueError('Cache requires an exact canonical public URL.')
        repair._validated_timing(capture.get('_timing'), fields=repair._SOURCE_TIMING_FIELDS)
        if capture.get('error'):
            failure = capture['error']
            if (not isinstance(failure, dict) or not isinstance(failure.get('code'), str)
                    or not isinstance(failure.get('reason'), str)):
                raise ValueError('Invalid cached source failure.')
            response = failure.get('source_response')
            if response is not None:
                from nannanews import acquisition
                import ipaddress
                from urllib.parse import urlsplit
                required = {'canonical_url', 'resolved_url', 'content_type', 'http_status',
                            'fetched_at', 'raw_response_base64', 'raw_response_digest',
                            'raw_response_complete'}
                if (not isinstance(response, dict)
                        or set(response) not in (required, required | {'content_encoding'})
                        or response['canonical_url'] != url
                        or not isinstance(response['resolved_url'], str)
                        or canonical_source_url(response['resolved_url']) != response['resolved_url']
                        or not isinstance(response['content_type'], str)
                        or type(response['http_status']) is not int
                        or not 100 <= response['http_status'] <= 599
                        or type(response['raw_response_complete']) is not bool
                        or not isinstance(response['fetched_at'], str)
                        or not isinstance(response['raw_response_base64'], str)
                        or len(response['raw_response_base64']) > 4 * ((acquisition.MAX_BODY + 2) // 3)
                        or ('content_encoding' in response and not isinstance(response['content_encoding'], str))):
                    raise ValueError('Invalid cached source failure response.')
                try:
                    address = ipaddress.ip_address(urlsplit(response['resolved_url']).hostname)
                except ValueError:
                    address = None
                if address is not None:
                    mapped = getattr(address, 'ipv4_mapped', None)
                    transition = getattr(address, 'sixtofour', None)
                    teredo = getattr(address, 'teredo', None)
                    if (not address.is_global or (mapped is not None and not mapped.is_global)
                            or (transition is not None and not transition.is_global)
                            or (teredo is not None and any(not value.is_global for value in teredo))):
                        raise ValueError('Cached source response cannot originate from a non-public address.')
                captured_at = datetime.fromisoformat(response['fetched_at'])
                raw = base64.b64decode(response['raw_response_base64'], validate=True)
                if (captured_at.tzinfo is None or len(raw) > acquisition.MAX_BODY
                        or hashlib.sha256(raw).hexdigest() != response['raw_response_digest']):
                    raise ValueError('Cached source failure response digest or timestamp is invalid.')
            return
        document = capture['document']
        if document.get('canonical_url') != url or document.get('source_type') not in {'public_html', 'wechat', 'nju_job_api'}:
            raise ValueError('Only real public-fetch captures may seed the source cache.')
        if canonical_source_url(document.get('resolved_url', '')) != document.get('resolved_url'):
            raise ValueError('Invalid source resolved URL.')
        raw = base64.b64decode(document['raw_response_base64'], validate=True)
        if not raw or hashlib.sha256(raw).hexdigest() != document['raw_response_digest']:
            raise ValueError('Source raw HTTP response digest is invalid.')
        media = capture.get('media', [])
        for image in media:
            data = base64.b64decode(image['data'], validate=True)
            if not data or hashlib.sha256(data).hexdigest() != image['digest']:
                raise ValueError('Source media digest is invalid.')
        complete = document.get('capture_complete', True) and not document.get('capture_errors') and not capture.get('media_error')
        from nannanews.public_pdf import validate_pdf_capture
        validate_pdf_capture(document, media, require_complete=reusable or bool(complete))
        from nannanews.source_media import validate_media_policy
        validate_media_policy(document, media)
        remote_urls = [image['url'] for image in media if 'source_pdf_url' not in image]
        if reusable and not complete:
            raise ValueError('Persisted public source is not a complete corresponding capture.')
        if complete and document.get('images', []) != remote_urls:
            raise ValueError('Source remote/media correspondence is incomplete.')

    def _remember(self, url, value, size):
        if url in self.hot:
            self.hot_size -= self.hot.pop(url)[1]
            dict.__delitem__(self, url)
        if size <= self.hot_bytes:
            while self.hot and self.hot_size + size > self.hot_bytes:
                key, (_, previous_size) = self.hot.popitem(last=False)
                self.hot_size -= previous_size
                dict.__delitem__(self, key)
            self.hot[url] = (value, size)
            self.hot_size += size
            dict.__setitem__(self, url, value)
        return value

    def _from_database(self, url):
        from nannanews import acquisition
        from nannanews.source_media import apply_remote_media_policy
        with _readonly(self.database) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_documents'").fetchone():
                return None
            # Lazy exact-URL projection; failed extraction does not invalidate raw HTTP.
            for row in db.execute('SELECT document_json FROM source_documents WHERE source_url=? AND length(document_json)<=? ORDER BY id DESC',
                                  (url, CACHE_RECORD_BYTES)):
                try:
                    document = json.loads(row[0])
                    value = {'document': {key: document[key] for key in self.DOCUMENT_FIELDS if key in document},
                             'media': document.get('media', []), 'media_error': ''}
                    self._validate(url, value)
                    if document.get('source_capture_version') != acquisition.SOURCE_CAPTURE_VERSION:
                        reparsed = acquisition.reparse_source_document(document)
                        value['document'] = {key: reparsed[key] for key in self.DOCUMENT_FIELDS if key in reparsed}
                    document, retained = apply_remote_media_policy(value['document'], value['media'])
                    value['document'] = {key: document[key] for key in self.DOCUMENT_FIELDS if key in document}
                    value['media'] = retained
                    self._validate(url, value, reusable=True)
                except (KeyError, TypeError, ValueError, acquisition.AcquisitionError):
                    continue
                return value
        return None

    def get(self, url, default=None):
        if url in self.hot:
            self.hot.move_to_end(url)
            return self.hot[url][0]
        path = self._path(url)
        if path.exists() or path.is_symlink():
            envelope = _load(path, CACHE_RECORD_BYTES)
            if envelope.get('url') != url or envelope.get('sha256') != _digest(envelope.get('capture')):
                raise ValueError('Durable source cache integrity changed; refusing refetch.')
            capture = envelope['capture']
            self._validate(url, capture)
            return self._remember(url, capture, path.stat().st_size)
        capture = self._from_database(url)
        if capture is not None:
            self[url] = capture
            return capture
        # A crash during GET is an honest unknown, never an automatic second GET.
        self[url] = {'error': {'code': 'capture_interrupted', 'reason': 'Public source capture started but did not finish; automatic refetch is disabled.'}}
        return default

    def __getitem__(self, url):
        sentinel = object()
        value = self.get(url, sentinel)
        if value is sentinel:
            raise KeyError(url)
        return value

    def __setitem__(self, url, capture):
        self._validate(url, capture)
        timing = capture.get('_timing')
        if not capture.get('error'):
            capture = {'document': {key: capture['document'][key] for key in self.DOCUMENT_FIELDS if key in capture['document']},
                       'media': capture.get('media', []), 'media_error': capture.get('media_error', '')}
        if timing is not None:
            capture = dict(capture, _timing=repair._validated_timing(timing, fields=repair._SOURCE_TIMING_FIELDS))
        envelope = {'url': url, 'sha256': _digest(capture), 'capture': capture}
        path = self._path(url)
        # Stream serialization retains complete bytes without a second giant JSON string.
        _atomic(path, envelope)
        size = path.stat().st_size
        self._remember(url, capture, size)
        if self.on_capture:
            self.on_capture(path)


@contextmanager
def worker_lock(database):
    """One OS owner per canonical MAIN database, across all output directories."""
    path = _safe_path(database).with_name('.' + Path(database).name + '.full-review.lock')
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('A full-review worker already owns this canonical database.') from None
        yield
    finally:
        os.close(descriptor)


def process_identity(pid):
    """Linux boot identity + process start ticks, immune to numeric PID reuse."""
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        value = Path('/proc') / str(int(pid)) / 'stat'
        fields = value.read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return boot + ':' + fields[19]
    except (OSError, ValueError, IndexError):
        return None


def daily_pause_reason(settings, now=None):
    clock = (now or _now()).astimezone(BEIJING)
    hour, minute = map(int, getattr(settings, 'collection_time', '04:00').split(':'))
    slot = clock.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot - timedelta(minutes=5) <= clock < slot:
        return 'daily_priority'
    with _readonly(settings.database_path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_runs'").fetchone():
            return 'daily_priority' if getattr(settings, 'collection_enabled', False) and clock >= slot else None
        columns = {row[1] for row in db.execute('PRAGMA table_info(daily_runs)')}
        if 'mode' not in columns:
            return 'daily_priority' if getattr(settings, 'collection_enabled', False) and clock >= slot else None
        latest = db.execute("SELECT started_at,finished_at,status FROM daily_runs WHERE mode='new' ORDER BY id DESC LIMIT 1").fetchone()
        if latest and latest['status'] in {'running', 'interrupted'}:
            return 'daily_priority'
        if getattr(settings, 'collection_enabled', False) and clock >= slot:
            if latest is None or datetime.fromisoformat(latest['started_at']).astimezone(BEIJING) < slot:
                return 'daily_priority'
    return None


def _category(record, terminal, progress=None, defer_code=None):
    review = record.get('source_review', {})
    if terminal:
        return 'primary_confirmed' if review.get('status') == 'source_verified' and review.get('activity') else 'model_terminal_unconfirmed'
    if defer_code == 'prior_attempt_protected':
        return 'request_blocked'
    if (defer_code == 'no_readable_primary' or not review.get('sources')
            or review.get('status') == 'source_mismatch'
            or any(attempt.get('status') in {'mismatch', 'media_unavailable'} for attempt in review.get('attempts', []))):
        return 'blocked_no_primary'
    return {'review_preparation_failed': 'preparation_blocked', 'review_storage_blocked': 'storage_blocked'}.get(progress, 'request_blocked')


class FullReviewWorker:
    def __init__(self, output, settings):
        self.output = _directory(output, create=False)
        self.settings = settings
        self.scope = _load(self.output / 'scope.json')
        if self.scope.get('format') != FORMAT:
            raise ValueError('Unsupported full-review scope.')
        from nannanews.bulletin_full_resume import validate_resume_manifest
        self.prior_attempt_protected_ids = validate_resume_manifest(self.output, self.scope)
        database = _file(settings.database_path, 2 ** 63 - 1).resolve(strict=True)
        if str(database) != self.scope['database_path']:
            raise ValueError('Canonical database path differs from frozen scope; preview clones cannot authorize requests.')
        self.database = database
        if _configuration(settings) != self.scope['configuration']:
            raise ValueError('Frozen model, protocol, rules or instructions changed.')
        rows, self.originals, fingerprint, signature, _ = _snapshot(database)
        if fingerprint != self.scope['fingerprint'] or signature != self.scope['signature']:
            raise ValueError('Frozen bulletin input, context or date head changed.')
        self.rows = {_public_id(row): row for row in rows}
        self.bindings = {value['occurrence_id']: value for value in self.scope['occurrences']}
        if set(self.rows) != set(self.bindings):
            raise ValueError('Frozen scope does not cover exactly all occurrences.')
        for identity, row in self.rows.items():
            binding = self.bindings[identity]
            if (binding['input_sha256'] != repair.input_digest(row, self.originals[row['id']])
                    or binding['related_context_sha256'] != row['related_context_sha256']):
                raise ValueError('Frozen per-occurrence binding changed.')
        if (len(self.scope['seeds']) != 3 or self.scope['occurrence_count'] != len(rows)
                or not 0 <= self.scope['max_new_requests'] <= len(rows) - 3
                or self.scope['batch_records'] != BATCH_RECORDS
                or self.scope['shard_bytes'] != MAX_REVIEW_ARTIFACT_BYTES):
            raise ValueError('Frozen scope limits or seed coverage changed.')
        self.attempt_store = AttemptStore(database)
        self.done = {}
        self.terminal_ids = set()
        self.blocked_progress = {}
        self.receipt_digests = {}
        self.intents = {}
        self.counters = Counter()
        self.stop = threading.Event()
        self.status_lock = threading.RLock()
        self.status = {'format': FORMAT, 'run_id': self.scope['run_id'], 'pid': os.getpid(),
            'start_identity': process_identity(os.getpid()), 'started_at': _stamp(), 'phase': 'starting',
            'terminal': False, 'current_occurrence': None, 'last_progress_at': _stamp(),
            'last_output': None, 'last_output_at': None, 'pause_reason': None}
        self.quality_failures = 0
        self.transport_failures = 0
        self.cooldown_until = None
        self.model_quality_totals = Counter({kind: 0 for kind in MODEL_QUALITY_KINDS})
        self.model_quality_window = deque(maxlen=MODEL_QUALITY_WINDOW)
        self.timing_totals = {}
        self.timing_measured = 0
        self.timing_unknown = 0
        self.timing_window = deque(maxlen=TIMING_WINDOW)
        self.source_coverage_counts = Counter({kind: 0 for kind in SOURCE_COVERAGE_KINDS})
        self.source_coverage_by_id = {}
        self.batch_counts = Counter()
        self.journal_mtime = None
        self.journal_index = 0
        self._reconcile()
        self.cache = SourceCache(self.output / 'source-cache', database, on_capture=self._captured)

    def _record_source_coverage(self, identity, record=None, kind=None):
        if identity not in self.bindings:
            return None
        with self.status_lock:
            previous = self.source_coverage_by_id.get(identity)
            if kind not in SOURCE_COVERAGE_KINDS:
                kind = _source_coverage_kind(record)
            if previous == kind:
                return None
            if previous is not None:
                self.source_coverage_counts[previous] -= 1
            self.source_coverage_by_id[identity] = kind
            self.source_coverage_counts[kind] += 1
            return kind

    def _record_model_quality(self, kind):
        if kind not in MODEL_QUALITY_KINDS:
            kind = 'unknown_outcome'
        with self.status_lock:
            self.model_quality_totals[kind] += 1
            self.model_quality_window.append(kind)


    def _quality_from_event(self, event):
        if not event.get('new_request'):
            return
        kind = event.get('model_quality_kind')
        if kind not in MODEL_QUALITY_KINDS:
            kind = {
                'primary_confirmed': 'verified',
                'model_terminal_unconfirmed': 'valid_unconfirmed',
                'model_terminal_failed': 'model_failure_unknown',
            }.get(event.get('category'), 'unknown_outcome')
        self._record_model_quality(kind)

    def _failure_policy(self, kind, facts, at):
        # Free/reused outcomes never reach this transition: only a new paid response
        # can prove provider recovery or reset the independent content-quality streak.
        if kind in {'verified', 'valid_unconfirmed'}:
            self.quality_failures = 0
            self.transport_failures = 0
            self.cooldown_until = None
        elif kind in MODEL_QUALITY_FAILURE_KINDS:
            transient = _transient_model_failure(facts)
            if transient:
                self.transport_failures += 1
                seconds = max(600 if self.transport_failures == 1 else 1800,
                              _transport_fields(facts).get('model_retry_after_seconds', 0))
                try:
                    until = datetime.fromisoformat(at) + timedelta(seconds=seconds)
                except OverflowError:
                    until = datetime.max.replace(tzinfo=timezone.utc)
                self.cooldown_until = until.isoformat()
            else:
                self.quality_failures += 1

    def _attempt_metadata(self, key):
        if not self.attempt_store.path.exists():
            return None
        with _readonly(self.attempt_store.path) as db:
            row = db.execute('SELECT state,finished_at,error FROM historical_attempts '
                             'WHERE request_sha256=?', (key,)).fetchone()
        return dict(row) if row is not None else None

    def _attempt_failure_facts(self, key):
        if not self.attempt_store.path.exists():
            return None
        # Indexed metadata only: replay must not load paid request/response payloads.
        with _readonly(self.attempt_store.path) as db:
            row = db.execute('''SELECT state,finished_at,error,
                CASE WHEN json_valid(decision_json) THEN
                    CASE WHEN json_type(decision_json,'$._model_failure_code')='text'
                              AND length(json_extract(decision_json,'$._model_failure_code'))<=80
                         THEN json_extract(decision_json,'$._model_failure_code') END
                END AS model_failure_code,
                CASE WHEN json_valid(decision_json) THEN
                    CASE WHEN json_type(decision_json,'$._model_http_status')='integer'
                              AND json_extract(decision_json,'$._model_http_status') BETWEEN 100 AND 599
                         THEN json_extract(decision_json,'$._model_http_status') END
                END AS model_http_status,
                CASE WHEN json_valid(decision_json) THEN
                    CASE WHEN json_type(decision_json,'$._model_retry_after_seconds') IN ('integer','real')
                              AND json_extract(decision_json,'$._model_retry_after_seconds')>0
                              AND json_extract(decision_json,'$._model_retry_after_seconds')<=1.7976931348623157e308
                         THEN json_extract(decision_json,'$._model_retry_after_seconds') END
                END AS model_retry_after_seconds,
                CASE WHEN json_valid(decision_json) THEN
                    CASE WHEN json_type(decision_json,'$._model_transport_failure')='true' THEN 1 END
                END AS model_transport_failure
                FROM historical_attempts WHERE request_sha256=? AND state='failed' ''', (key,)).fetchone()
        if row is None:
            return None
        metadata = dict(row)
        metadata['model_transport_failure'] = metadata['model_transport_failure'] == 1
        if metadata['model_failure_code'] not in (MODEL_TRANSPORT_FAILURE_CODES |
                MODEL_MALFORMED_FAILURE_CODES | {'model_audit_failed', 'model_preparation_failed'}):
            metadata['model_failure_code'] = None
        return metadata

    def _replay_failure_policy(self, event, previous, intent):
        if previous['state'] == 'failed' and (event is None or not event.get('model_failure_code')
                or 'transport_failures' not in event or 'cooldown_until' not in event):
            # Current outcomes are self-contained. Only an incomplete/missing
            # failed outcome needs indexed scalar recovery from the paid ledger.
            previous = {**previous, **(self._attempt_failure_facts(intent['request_sha256']) or {})}
        facts = {**_transport_fields(previous), **_transport_fields(event or {})}
        code = (event or {}).get('model_failure_code') or previous.get('model_failure_code')
        if code:
            facts['model_failure_code'] = code
        kind = (event or {}).get('model_quality_kind')
        if kind not in MODEL_QUALITY_KINDS:
            kind = ('model_failure_unknown' if previous['state'] == 'failed' else
                    'valid_unconfirmed' if previous['state'] == 'completed' else 'unknown_outcome')
        if (facts.get('model_failure_code') in {None, 'model_http_error'}
                and kind in {'transport_failure', 'model_failure_unknown'} and not facts.get('model_http_status')):
            match = re.fullmatch(r'模型 API 返回 HTTP ([1-5][0-9]{2})；未创建草稿，不会自动重试。',
                                 previous.get('error') or '')
            if match:
                facts['model_http_status'] = int(match.group(1))
                facts['model_failure_code'] = 'model_http_error'
            elif not facts.get('model_failure_code') and previous.get('error') in {
                    '模型 API 域名解析失败或超时。请管理员检查配置。',
                    '模型 API 无可连接地址或连接超时。请管理员检查配置。',
                    '模型 API 连接或审计写入失败；不会自动重试。'}:
                facts['model_failure_code'] = 'model_unavailable'
        if (kind in MODEL_QUALITY_FAILURE_KINDS
                and previous.get('error') == '模型 API 响应不完整，未创建草稿。'):
            # Legacy counters remain in their recorded metric category; only the
            # operational cooldown/circuit reconstruction uses this safe fact.
            facts['model_transport_failure'] = True
            if not facts.get('model_failure_code'):
                facts['model_failure_code'] = 'model_response_invalid'
        self._failure_policy(kind, facts,
            (event or {}).get('at') or previous.get('finished_at') or intent['at'])

    def _model_quality_summary(self):
        rolling_counts = Counter(self.model_quality_window)
        rolling_requests = len(self.model_quality_window)
        cumulative_requests = sum(self.model_quality_totals.values())
        rolling_failures = sum(rolling_counts[kind] for kind in MODEL_QUALITY_FAILURE_KINDS)
        cumulative_failures = sum(self.model_quality_totals[kind] for kind in MODEL_QUALITY_FAILURE_KINDS)
        return {
            'window_limit': MODEL_QUALITY_WINDOW,
            'consecutive_failures': self.quality_failures,
            'cumulative': {**dict(self.model_quality_totals), 'new_paid_outcomes': cumulative_requests,
                           'failures': cumulative_failures},
            'rolling': {**{kind: rolling_counts[kind] for kind in MODEL_QUALITY_KINDS},
                        'sample_size': rolling_requests, 'failures': rolling_failures,
                        'failure_rate': rolling_failures / rolling_requests if rolling_requests else None},
        }

    def _source_coverage_summary(self):
        return {**{kind: self.source_coverage_counts[kind] for kind in SOURCE_COVERAGE_KINDS},
                'reviewed_occurrences': sum(self.source_coverage_counts.values())}

    def _record_timing(self, value):
        timing = repair._validated_timing(value)
        with self.status_lock:
            self.timing_window.append(timing)
            if not timing:
                self.timing_unknown += 1
                return
            self.timing_measured += 1
            for key, number in timing.items():
                totals = self.timing_totals.setdefault(key, {'samples': 0, 'total': 0, 'max': 0})
                totals['samples'] += 1
                totals['total'] += number
                totals['max'] = max(totals['max'], number)

    def _timing_summary(self):
        rolling = {}
        for timing in self.timing_window:
            for key, number in timing.items():
                totals = rolling.setdefault(key, {'samples': 0, 'total': 0, 'max': 0})
                totals['samples'] += 1
                totals['total'] += number
                totals['max'] = max(totals['max'], number)
        def fields(totals):
            return {key: dict(value, mean=value['total'] / value['samples']) for key, value in totals.items()}
        return {'window_limit': TIMING_WINDOW,
            'last': dict(self.timing_window[-1]) if self.timing_window and self.timing_window[-1] else None,
            'cumulative': {'measured_outcomes': self.timing_measured, 'unknown_outcomes': self.timing_unknown,
                           'fields': fields(self.timing_totals)},
            'rolling': {'sample_size': len(self.timing_window),
                        'measured_outcomes': sum(bool(value) for value in self.timing_window),
                        'fields': fields(rolling)}}


    def _validate_binding(self, value):
        identity = value.get('occurrence_id')
        binding = self.bindings.get(identity)
        if binding is None or any(value.get(key) != binding[key] for key in ('input_sha256', 'related_context_sha256')):
            raise ValueError('Persisted full-review result no longer matches frozen input.')
        if value.get('rules_version') != repair.RULES_VERSION or value.get('reviewer') != 'luna:' + self.scope['configuration']['model']:
            raise ValueError('Persisted full-review result configuration changed.')
        return identity

    def _reconcile(self):
        # Canonical receipts/progress win over a checkpoint interrupted after append.
        for path in sorted((self.output / 'seeds').glob('*.json')):
            value = _load(path)
            identity = self._validate_binding(value)
            expected = next((seed for seed in self.scope['seeds'] if seed['occurrence_id'] == identity), None)
            if expected is None or expected['record_sha256'] != _digest(value):
                raise ValueError('Accepted seed receipt changed.')
            repair._review_decisions((path,), list(self.rows.values()), self.originals)
            self.receipt_digests[identity] = repair._reuse_receipt_digest(value)
            self._record_source_coverage(identity, value)
            self.done[identity] = _category(value, True)
            self.terminal_ids.add(identity)
            self.attempt_store.record_outcome(value, self.scope['configuration']['model'])
        if set(self.done) != {seed['occurrence_id'] for seed in self.scope['seeds']}:
            raise ValueError('Accepted seed receipts are missing.')
        for directory in sorted((self.output / 'batches').glob('batch-*')):
            _directory(directory, create=False)
            artifact = directory / 'reviews.jsonl'
            if artifact.exists():
                repair._review_decisions((artifact,), list(self.rows.values()), self.originals)
                for value in _lines(artifact):
                    identity = self._validate_binding(value)
                    self.receipt_digests[identity] = repair._reuse_receipt_digest(value)
                    self.batch_counts[str(directory)] += 1
                    self._record_source_coverage(identity, value)
                    self.done[identity] = _category(value, True)
                    self.terminal_ids.add(identity)
                    self.attempt_store.record_outcome(value, self.scope['configuration']['model'])
            for path in sorted(directory.glob('review-progress*.jsonl')):
                for value in _lines(path):
                    identity = self._validate_binding(value)
                    self.blocked_progress[identity] = {key: value.get(key) for key in
                        ('request_sha256', 'request_state', 'progress', 'defer_code')}
                    self.batch_counts[str(directory)] += 1
                    code = value.get('defer_code')
                    if code not in OPERATIONAL_PAUSES and identity not in self.terminal_ids:
                        capture = value
                        if value.get('capture_artifact'):
                            capture_path = _safe_path(value['capture_artifact'])
                            if not capture_path.is_relative_to(self.output):
                                raise ValueError('Progress capture escapes private output.')
                            capture = _load(capture_path, CACHE_RECORD_BYTES)
                            if self._validate_binding(capture) != identity:
                                raise ValueError('Progress references a different occurrence capture.')
                        self._record_source_coverage(identity, capture)
                        self.done[identity] = _category(capture, False, value.get('progress'), code)
        pending_intent = None
        for path in sorted((self.output / 'journal').glob('event-*.json')):
            event = _load(path, 65536)
            self.journal_mtime = path.stat().st_mtime
            self.journal_index = max(self.journal_index, int(path.stem.split('-')[1]))
            if event.get('event') in {'intent', 'quality_acknowledged'} and pending_intent:
                intent, previous = pending_intent
                self._replay_failure_policy(None, previous, intent)
                pending_intent = None
            if event.get('event') == 'intent':
                key = event['request_sha256']
                if key not in self.intents:
                    self.intents[key] = event
                    previous = self._attempt_metadata(key)
                    if previous is not None:
                        self.counters['model_request_attempts'] += 1
                        pending_intent = (event, previous)
                        if previous['state'] == 'failed':
                            identity = event['occurrence_id']
                            if self.done.get(identity) == 'model_terminal_unconfirmed':
                                self.done[identity] = 'model_terminal_failed'
            elif event.get('event') == 'quality_acknowledged':
                self.quality_failures = 0
            elif event.get('event') == 'outcome':
                identity = event.get('occurrence_id')
                if event.get('new_request') and pending_intent:
                    intent, previous = pending_intent
                    if identity == intent['occurrence_id']:
                        self._replay_failure_policy(event, previous, intent)
                        pending_intent = None
                if identity not in self.done:
                    self._record_source_coverage(identity, kind=event.get('source_coverage'))
                self._quality_from_event(event)
                self._record_timing(event.get('_timing'))
        if pending_intent:
            intent, previous = pending_intent
            self._replay_failure_policy(None, previous, intent)
        missing = self.prior_attempt_protected_ids.difference(self.done)
        self.status['protected_prior_attempts_without_result'] = len(missing)
        for identity in missing:
            self.done[identity] = 'request_blocked'
        self._checkpoint()

    def _journal(self, event, **details):
        value = {'at': _stamp(), 'event': event, **details}
        directory = _directory(self.output / 'journal')
        self.journal_index += 1
        path = directory / ('event-' + str(self.journal_index).zfill(8) + '.json')
        _atomic(path, value)
        self.journal_mtime = path.stat().st_mtime
        return path

    def _reuse_original_occurrence(self, request_key):
        intent = self.intents.get(request_key)
        identity = intent.get('occurrence_id') if intent else None
        if identity not in self.terminal_ids or identity not in self.receipt_digests:
            return None
        row = self.rows[identity]
        return dict(row, original_date_claims=self.originals[row['id']],
                    _review_receipt_sha256=self.receipt_digests[identity])

    def _request_resolution_summary(self):
        blocked = Counter()
        for identity, category in self.done.items():
            if category != 'request_blocked':
                continue
            progress = self.blocked_progress.get(identity, {})
            state = progress.get('request_state')
            if state == 'completed':
                kind = 'completed_not_reusable'
            elif state == 'failed':
                kind = 'prior_failed'
            elif state in {'reserved', 'legacy_interrupted'}:
                kind = 'in_flight_or_unknown'
            elif identity in self.prior_attempt_protected_ids or progress.get('defer_code') == 'prior_attempt_protected':
                kind = 'prior_attempt_protected'
            else:
                kind = 'other'
            blocked[kind] += 1
        paid_ids = {event['occurrence_id'] for event in self.intents.values()}
        seed_ids = {seed['occurrence_id'] for seed in self.scope['seeds']}
        reused = self.terminal_ids.difference(paid_ids, seed_ids)
        return {'unit': 'occurrences', 'reused_primary_confirmed': sum(
            self.done[identity] == 'primary_confirmed' for identity in reused),
            'reused_valid_unconfirmed': sum(self.done[identity] != 'primary_confirmed' for identity in reused),
            **{kind: blocked[kind] for kind in ('completed_not_reusable', 'prior_failed',
                'in_flight_or_unknown', 'prior_attempt_protected', 'other')}}

    def _activity_coverage_summary(self):
        checked, confirmed, blocked = set(), set(), set()
        for identity, category in self.done.items():
            context = self.bindings[identity]['related_context_sha256']
            checked.add(context)
            if category == 'primary_confirmed':
                confirmed.add(context)
            elif category == 'request_blocked':
                blocked.add(context)
        return {'unit': 'activity_contexts',
            'total': len({value['related_context_sha256'] for value in self.bindings.values()}),
            'checked': len(checked), 'primary_confirmed': len(confirmed),
            'request_blocked': len(blocked), 'blocked_already_confirmed': len(blocked & confirmed)}

    def _checkpoint(self):
        with self.status_lock:
            counts = Counter(self.done.values())
            counts.update({'checked_occurrences': len(self.done), 'pending_unseen': len(self.rows) - len(self.done),
                           'model_request_attempts': self.counters['model_request_attempts']})
            model_quality = self._model_quality_summary()
            source_coverage = self._source_coverage_summary()
            request_resolution = self._request_resolution_summary()
            activity_coverage = self._activity_coverage_summary()
            self.status.update(request_resolution=request_resolution, activity_coverage=activity_coverage)
            _atomic(self.output / 'state.json', {'run_id': self.scope['run_id'], 'updated_at': _stamp(),
                    'counts': dict(counts), 'quality_failures': self.quality_failures,
                    'consecutive_quality_failures': self.quality_failures,
                    'transport_failures': self.transport_failures, 'cooldown_until': self.cooldown_until,
                    'cooldown_remaining_seconds': _cooldown_remaining(self.cooldown_until, _now()),
                    'model_quality': model_quality, 'source_coverage': source_coverage,
                    'request_resolution': request_resolution, 'activity_coverage': activity_coverage,
                    '_timing': self._timing_summary()})
            self.status['counts'] = dict(counts)
            self._publish()

    def _publish(self, **fields):
        with self.status_lock:
            self.status.update(fields)
            self.status['heartbeat_at'] = _stamp()
            # quality_failures is the consecutive circuit counter, not lifetime failures.
            self.status['quality_failures'] = self.quality_failures
            self.status['consecutive_quality_failures'] = self.quality_failures
            self.status['transport_failures'] = self.transport_failures
            self.status['cooldown_until'] = self.cooldown_until
            self.status['cooldown_remaining_seconds'] = _cooldown_remaining(self.cooldown_until, _now())
            self.status['model_quality'] = self._model_quality_summary()
            self.status['source_coverage'] = self._source_coverage_summary()
            self.status['_timing'] = self._timing_summary()
            self.status['journal_mtime'] = self.journal_mtime
            self.status['terminal'] = self.status['phase'] in TERMINAL_PHASES
            _atomic(self.output / 'status.json', self.status)

    def _quality(self, result, category):
        if not result.get('new_request'):
            return None
        with self.status_lock:
            kind = _model_quality_kind(result, category)
            self._failure_policy(kind, result, _stamp())
            self._record_model_quality(kind)
            return kind

    def _captured(self, path):
        self._publish(phase='captured', last_progress_at=_stamp(), last_output=str(path), last_output_at=_stamp())

    def _flag(self, name):
        path = _safe_path(self.output / name)
        if path.exists():
            _file(path, 65536)
            return True
        return False

    def pause_reason(self):
        if self.stop.is_set() or self._flag('stop-request'):
            return 'stop_requested'
        if self._flag('pause-request'):
            return 'operator_pause'
        if self.quality_failures >= 3:
            return 'quality_circuit'
        if shutil.disk_usage(self.output).free < MIN_FREE_BYTES:
            return 'disk_low'
        if _configuration(self.settings) != self.scope['configuration']:
            return 'configuration_changed'
        with _readonly(self.database) as db:
            if _signature(db) != self.scope['signature']:
                return 'scope_changed'
        reason = daily_pause_reason(self.settings)
        if reason:
            return reason
        if _cooldown_remaining(self.cooldown_until, _now()) > 0:
            if self.counters['model_request_attempts'] >= self.scope['max_new_requests']:
                return 'request_budget'
            return 'transport_cooldown'
        return None

    def request_gate(self, request_key, captured_review):
        reason = self.pause_reason()
        if reason:
            raise repair.ReviewDeferred(reason, 'Full historical submission is paused: ' + reason)
        if self.status.get('current_occurrence') in self.prior_attempt_protected_ids:
            raise repair.ReviewDeferred('prior_attempt_protected',
                'Previous paid outcome or request intent is protected from automatic resubmission; no new model request is authorized.')
        if (not any(source.get('text', '').strip() or source.get('images') for source in captured_review.get('sources', []))
                or captured_review.get('status') == 'source_mismatch'
                or any(attempt.get('status') in {'mismatch', 'media_unavailable'} for attempt in captured_review.get('attempts', []))):
            raise repair.ReviewDeferred('no_readable_primary', 'No complete readable linked primary capture; no model request is authorized.')
        if self.counters['model_request_attempts'] >= self.scope['max_new_requests']:
            raise repair.ReviewDeferred('request_budget', 'Frozen new-request budget is exhausted.')
        if request_key not in self.intents:
            event = {'request_sha256': request_key, 'occurrence_id': self.status['current_occurrence']}
            path = self._journal('intent', **event)
            self.intents[request_key] = event
            self._publish(phase='request_intent', last_progress_at=_stamp(), last_output=str(path), last_output_at=_stamp())

    def _storage(self):
        from nannanews import acquisition
        root = _directory(self.output / 'batches')
        paths = sorted(root.glob('batch-*'))
        directory = paths[-1] if paths else root / 'batch-000000'
        if directory.exists():
            count = self.batch_counts[str(directory)]
            size = (directory / 'reviews.jsonl').stat().st_size if (directory / 'reviews.jsonl').exists() else 0
            if count >= self.scope['batch_records'] or size > MAX_REVIEW_ARTIFACT_BYTES // 2:
                directory = root / ('batch-' + str(int(directory.name.split('-')[1]) + 1).zfill(6))
        _directory(directory)
        return ReviewStorage(directory / 'reviews.jsonl', directory, response_bytes=acquisition.MAX_MODEL_BODY)


    def run(self):
        heartbeat_stop = threading.Event()
        def heartbeat():
            while not heartbeat_stop.wait(60):
                self._publish()
        thread = threading.Thread(target=heartbeat, name='full-review-heartbeat', daemon=True)
        handlers = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                handlers[signum] = signal.signal(signum, lambda number, frame: self.stop.set())
        thread.start()
        print('FULL_REVIEW_READY ' + repair._json({'run_id': self.scope['run_id'], 'pid': os.getpid(), 'output': str(self.output)}), flush=True)
        try:
            for identity in self.bindings:
                if identity in self.done:
                    continue
                self._publish(current_occurrence=identity)
                while True:
                    reason = self.pause_reason()
                    if reason:
                        if reason == 'stop_requested':
                            self._publish(phase='stopped', pause_reason=reason)
                            return dict(self.status)
                        self._publish(phase='paused', pause_reason=reason)
                        # Explicit quality acknowledgement never retries the paid failed row.
                        if reason == 'quality_circuit' and self._flag('resume-request'):
                            (self.output / 'resume-request').unlink()
                            self.quality_failures = 0
                            self._journal('quality_acknowledged')
                            self._checkpoint()
                            continue
                        remaining = _cooldown_remaining(self.cooldown_until, _now())
                        self.stop.wait(min(60, remaining) if reason == 'transport_cooldown' else 60)
                        continue
                    self._publish(phase='reviewing', pause_reason=None)
                    storage = self._storage()
                    with lock_output(storage.output):
                        result = repair.review_occurrence(self.rows[identity], self.originals[self.rows[identity]['id']],
                            model_name=self.scope['configuration']['model'], source_cache=self.cache,
                            attempt_store=self.attempt_store, storage=storage, counters=self.counters,
                            original_occurrence_resolver=self._reuse_original_occurrence,
                            request_gate=self.request_gate)
                    self.batch_counts[str(storage.output)] += 1
                    if (result.get('progress') == 'review_storage_blocked' and not result.get('new_request')
                            and storage.artifact.exists() and storage.artifact.stat().st_size):
                        # Rotate only unpaid work, once. The first full capture remains retained.
                        self.batch_counts[str(storage.output)] = self.scope['batch_records']
                        storage = self._storage()
                        with lock_output(storage.output):
                            result = repair.review_occurrence(self.rows[identity], self.originals[self.rows[identity]['id']],
                                model_name=self.scope['configuration']['model'], source_cache=self.cache,
                                attempt_store=self.attempt_store, storage=storage, counters=self.counters,
                                original_occurrence_resolver=self._reuse_original_occurrence,
                                request_gate=self.request_gate)
                        self.batch_counts[str(storage.output)] += 1
                    with self.status_lock:
                        self.status['_last_review_timing'] = result.get('_timing')
                    code = result.get('defer_code')
                    if code in OPERATIONAL_PAUSES:
                        self._publish(phase='paused', pause_reason=code)
                        continue
                    category = _category(result['record'], result['terminal'], result.get('progress'), code)
                    if result.get('paid_failure') and result['terminal']:
                        category = 'model_terminal_failed'
                    model_quality_kind = self._quality(result, category)
                    source_coverage = self._record_source_coverage(identity, result['record'])
                    self._record_timing(result.get('_timing'))
                    self.done[identity] = category
                    if not result['terminal']:
                        self.blocked_progress[identity] = {key: result.get(key) for key in
                            ('request_sha256', 'request_state', 'progress', 'defer_code')}
                    if result['terminal']:
                        self.terminal_ids.add(identity)
                        self.receipt_digests[identity] = repair._reuse_receipt_digest(result['record'])
                    artifact = str(storage.artifact) if result['terminal'] else result.get('capture_artifact')
                    path = self._journal('outcome', occurrence_id=identity, category=category,
                        terminal=result['terminal'], progress=result.get('progress'),
                        new_request=bool(result.get('new_request', False)), quality_failures=self.quality_failures,
                        model_failure_code=result.get('model_failure_code'), **_transport_fields(result),
                        transport_failures=self.transport_failures, cooldown_until=self.cooldown_until,
                        model_quality_kind=model_quality_kind, source_coverage=source_coverage, output=artifact,
                        _timing=result.get('_timing'))
                    self._publish(last_progress_at=_stamp(), last_output=artifact or str(path), last_output_at=_stamp())
                    self._checkpoint()
                    print('FULL_REVIEW_PROGRESS ' + repair._json({'occurrence_id': identity, 'category': category,
                          'counts': self.status['counts']}), flush=True)
                    break
            self._publish(phase='completed', current_occurrence=None, pause_reason=None)
            _atomic(self.output / 'summary.json', self.status)
            return dict(self.status)
        except BaseException as error:
            # Do not leak provider exception strings, URLs, configuration or raw content.
            self._publish(phase='failed', failure_type=type(error).__name__)
            raise
        finally:
            heartbeat_stop.set()
            thread.join(timeout=5)
            for signum, handler in handlers.items():
                signal.signal(signum, handler)


def run_full_review(output_dir, settings=None):
    if settings is None:
        from nannanews import runtime
        settings = runtime.settings
    # Check path binding before creating any sibling lock or paid ledger.
    scope = _load(_directory(output_dir, create=False) / 'scope.json')
    database = _file(settings.database_path, 2 ** 63 - 1).resolve(strict=True)
    if str(database) != scope.get('database_path'):
        raise ValueError('Canonical database path differs from frozen scope.')
    with worker_lock(database):
        return FullReviewWorker(output_dir, settings).run()


def reuse_full_reviews(output_dir, settings=None):
    """Append free bound receipts for old blocked requests; never fetch or send.

    Existing progress and paid receipts remain immutable. A receipt wins over
    progress on restart, including a crash between append and the journal entry.
    Quality acknowledgement is deliberately not part of this operation.
    """
    if settings is None:
        from nannanews import runtime
        settings = runtime.settings
    from nannanews.bulletin_historical_attempts import StorageBlocked

    output = _directory(output_dir, create=False)
    scope = _load(output / 'scope.json')
    database = _file(settings.database_path, 2 ** 63 - 1).resolve(strict=True)
    if str(database) != scope.get('database_path'):
        raise ValueError('Canonical database path differs from frozen scope.')
    with worker_lock(database):
        previous_status = _load(output / 'status.json', 1024 * 1024)
        if previous_status.get('phase') not in TERMINAL_PHASES | {'prepared'}:
            raise ValueError('Stop the full-review worker before free-only receipt recovery.')
        worker = FullReviewWorker(output, settings)
        results = Counter()
        worker._publish(phase='reusing', current_occurrence=None, pause_reason=None)
        try:
            for identity, progress in worker.blocked_progress.items():
                if identity in worker.terminal_ids or not progress.get('request_sha256'):
                    continue
                key = progress['request_sha256']
                previous = worker.attempt_store.lookup(key)
                if previous is None or previous['state'] != 'completed':
                    results['protected_or_missing'] += 1
                    continue
                row = worker.rows[identity]
                decision = repair.reuse_completed_review(previous, row, worker.originals[row['id']],
                    model_name=worker.scope['configuration']['model'],
                    original_occurrence=worker._reuse_original_occurrence(key))
                if decision is None:
                    results['not_reusable'] += 1
                    continue
                if shutil.disk_usage(output).free < MIN_FREE_BYTES:
                    raise ValueError('Insufficient free disk space for complete reused receipts.')
                record = {'item_id': repair._public_id('item-', row['identity_key']),
                    'occurrence_id': identity, 'document_sha256': row['source_sha256'],
                    'local_key': row['local_key'], 'rules_version': repair.RULES_VERSION,
                    'reviewer': 'luna:' + worker.scope['configuration']['model'],
                    'input_sha256': repair.input_digest(row, worker.originals[row['id']]),
                    'related_context_sha256': row['related_context_sha256'],
                    **{field: decision[field] for field in ('status', 'claims', 'reason', 'source_review')}}
                worker._validate_binding(record)
                storage = worker._storage()
                try:
                    storage.check_record(record)
                except StorageBlocked:
                    worker.batch_counts[str(storage.output)] = worker.scope['batch_records']
                    storage = worker._storage()
                    try:
                        storage.check_record(record)
                    except StorageBlocked:
                        results['storage_blocked'] += 1
                        continue
                with lock_output(storage.output):
                    worker._journal('reuse_intent', occurrence_id=identity,
                        request_sha256=key, record_sha256=_digest(record))
                    repair._append_private_line(storage.artifact, record)
                    worker.attempt_store.record_outcome(record, worker.scope['configuration']['model'])
                worker.batch_counts[str(storage.output)] += 1
                category = _category(record, True)
                worker._record_source_coverage(identity, record)
                worker.done[identity] = category
                worker.terminal_ids.add(identity)
                worker.receipt_digests[identity] = repair._reuse_receipt_digest(record)
                worker._journal('outcome', occurrence_id=identity, category=category,
                    terminal=True, progress=None, new_request=False, reused_request_sha256=key,
                    quality_failures=worker.quality_failures, model_quality_kind=None,
                    source_coverage=None, output=str(storage.artifact))
                results[category] += 1
                worker._publish(current_occurrence=identity, last_progress_at=_stamp(),
                    last_output=str(storage.artifact), last_output_at=_stamp())
                worker._checkpoint()
            worker._publish(phase='stopped', current_occurrence=None,
                pause_reason='quality_circuit' if worker.quality_failures >= 3 else 'operator_pause')
            worker._checkpoint()
        except BaseException:
            worker._publish(phase='failed', current_occurrence=None)
            raise
        summary = {'format': 'nanna-full-review-free-reuse-v1', 'run_id': scope['run_id'],
            'at': _stamp(), 'new_model_requests': 0, 'results': dict(results),
            'counts': worker.status['counts'], 'request_resolution': worker.status['request_resolution'],
            'activity_coverage': worker.status['activity_coverage'],
            'consecutive_quality_failures': worker.quality_failures,
            'paid_request_history_reset': False, 'automatic_live_apply': False}
        _atomic(output / ('reuse-summary-' + uuid.uuid4().hex + '.json'), summary)
        return summary


def monitor_observation(output_dir, now=None):
    """Read worker evidence only. No runtime/model configuration is needed."""
    output = _directory(output_dir, create=False)
    status = _load(output / 'status.json', 1024 * 1024)
    clock = now or _now()
    def age(value):
        return max(0, (clock - datetime.fromisoformat(value)).total_seconds()) if value else None
    heartbeat_age = age(status.get('heartbeat_at'))
    progress_age = age(status.get('last_progress_at'))
    alive = bool(status.get('pid') and status.get('start_identity')
                 and process_identity(status['pid']) == status['start_identity'])
    terminal = bool(status.get('terminal') and status.get('phase') in TERMINAL_PHASES)
    model_quality = status.get('model_quality') if isinstance(status.get('model_quality'), dict) else {}
    source_coverage = status.get('source_coverage') if isinstance(status.get('source_coverage'), dict) else {}
    consecutive_failures = status.get('consecutive_quality_failures', status.get('quality_failures'))
    cooldown_remaining = _cooldown_remaining(status.get('cooldown_until'), clock)
    intentional_pause = (status.get('phase') == 'paused' and
        (status.get('pause_reason') in INTENTIONAL_PAUSES or
         (status.get('pause_reason') == 'transport_cooldown' and cooldown_remaining > 0)))
    warnings = []
    if status.get('phase') == 'failed':
        warnings.append('worker_failed')
    if status.get('protected_prior_attempts_without_result', 0) > 0:
        warnings.append('prior_attempt_result_missing')
    if not terminal:
        if not alive:
            warnings.append('worker_not_running_or_pid_reused')
        if heartbeat_age is None or heartbeat_age > 180:
            warnings.append('heartbeat_stale')
        if progress_age is not None and progress_age > 900 and not intentional_pause:
            warnings.append('progress_stale')
        if status.get('phase') == 'paused' and status.get('pause_reason') in MONITOR_ALERTS:
            warnings.append(status['pause_reason'])
    if not intentional_pause and _model_quality_degraded(model_quality):
        warnings.append('model_quality_degraded')
    output_path = status.get('last_output')
    output_mtime = None
    if output_path:
        path = _safe_path(output_path)
        if not path.is_relative_to(output):
            raise ValueError('Worker output escapes private run directory.')
        if path.exists():
            output_mtime = _file(path, 2 ** 63 - 1).stat().st_mtime
    cumulative = model_quality.get('cumulative', {})
    rolling = model_quality.get('rolling', {})
    quality_details = {'consecutive_failures': consecutive_failures,
        'cumulative_failures': cumulative.get('failures'),
        'cumulative_requests': cumulative.get('new_paid_outcomes'),
        'rolling_failures': rolling.get('failures'), 'rolling_requests': rolling.get('sample_size')}
    alerts = []
    for code in warnings:
        alert = {'code': code, 'severity': MONITOR_ALERTS[code][0], 'action': MONITOR_ALERTS[code][1]}
        if code in {'quality_circuit', 'model_quality_degraded'}:
            alert['details'] = quality_details
        alerts.append(alert)
    return {'at': clock.isoformat(), 'run_id': status.get('run_id'), 'phase': status.get('phase'),
        'terminal': terminal, 'pid': status.get('pid'), 'start_identity': status.get('start_identity'),
        'process_alive': alive,
        'heartbeat_age_seconds': heartbeat_age, 'progress_age_seconds': progress_age,
        'pause_reason': status.get('pause_reason'), 'counts': status.get('counts', {}),
        'protected_prior_attempts_without_result': status.get('protected_prior_attempts_without_result', 0),
        'quality_failures': consecutive_failures, 'consecutive_quality_failures': consecutive_failures,
        'transport_failures': status.get('transport_failures', 0),
        'cooldown_until': status.get('cooldown_until'), 'cooldown_remaining_seconds': cooldown_remaining,
        'model_quality': model_quality, 'source_coverage': source_coverage,
        '_timing': status.get('_timing') if isinstance(status.get('_timing'), dict) else None,
        'pause_intentional': intentional_pause,
        'last_output': output_path, 'output_mtime': output_mtime,
        'journal_mtime': status.get('journal_mtime'),
        'warnings': warnings, 'alerts': alerts, 'attention_required': bool(alerts),
        'severity': 'error' if any(alert['severity'] == 'error' for alert in alerts) else 'warning' if alerts else 'ok'}


def record_monitor_observation(output_dir, now=None):
    """Persist one read-only worker observation and monitor-owned alert state.

    monitor-alerts.json atomically holds latest active alerts plus the last 128
    opened/resolved transitions (at most 256 KiB). Repeated observations retain
    active attention but have no alert_transitions/notification_required. A
    monitor-only lock serializes consumers; it never acquires a worker/paid lock.
    """
    output = _directory(output_dir, create=False)
    latest_path = _safe_path(output / 'monitor-alerts.json')
    log_path = _safe_path(output / 'monitor.jsonl')
    lock_path = _safe_path(output / '.monitor.lock')
    for path, maximum in ((latest_path, MAX_MONITOR_ALERT_BYTES),
                          (log_path, MAX_REVIEW_ARTIFACT_BYTES), (lock_path, 1024)):
        if path.exists():
            _file(path, maximum)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another monitor observation is being recorded.') from None
        observation = monitor_observation(output, now)
        previous = _load(latest_path, MAX_MONITOR_ALERT_BYTES) if latest_path.exists() else {}
        if previous and (previous.get('format') != MONITOR_ALERT_FORMAT
                or not isinstance(previous.get('active_alerts'), list)
                or not isinstance(previous.get('transitions'), list)
                or len(previous['active_alerts']) > len(MONITOR_ALERTS)
                or len(previous['transitions']) > MAX_MONITOR_ALERT_TRANSITIONS):
            raise ValueError('Invalid bounded monitor alert state.')
        worker = {key: observation[key] for key in ('run_id', 'pid', 'start_identity')}
        previous_active = {alert['code']: alert for alert in previous.get('active_alerts', [])}
        same_worker = previous.get('worker') == worker
        current_codes = set(observation['warnings'])
        transitions = []
        for code, alert in previous_active.items():
            if not same_worker or code not in current_codes:
                transitions.append({**alert, 'state': 'resolved', 'at': observation['at'],
                                    'worker': previous['worker']})
        active = []
        for alert in observation['alerts']:
            prior = previous_active.get(alert['code']) if same_worker else None
            value = {**alert, 'opened_at': prior['opened_at'] if prior else observation['at']}
            active.append(value)
            if prior is None:
                transitions.append({**value, 'state': 'opened', 'at': observation['at'], 'worker': worker})
        state = {'format': MONITOR_ALERT_FORMAT, 'at': observation['at'], 'worker': worker,
                 'attention_required': observation['attention_required'], 'severity': observation['severity'],
                 'active_alerts': active,
                 'transitions': (previous.get('transitions', []) + transitions)[-MAX_MONITOR_ALERT_TRANSITIONS:]}
        if len(repair._json(state).encode('utf-8')) + 1 > MAX_MONITOR_ALERT_BYTES:
            raise ValueError('Monitor alert state exceeds its byte limit.')
        observation.update(alert_transitions=transitions, notification_required=bool(transitions))
        if log_path.exists() and _file(log_path).stat().st_size > MAX_REVIEW_ARTIFACT_BYTES - 65536:
            log_path = _safe_path(output / ('monitor-' + _now().strftime('%Y%m%dT%H%M%S') + '.jsonl'))
            if log_path.exists():
                _file(log_path)
        # Latest state and its transition history commit together. Periodic logs
        # are supplementary; after interruption the durable state remains usable.
        _atomic(latest_path, state)
        _append(log_path, observation)
        return observation
    finally:
        os.close(descriptor)


def watch_full_review(output_dir, interval=600):
    if isinstance(interval, bool) or not isinstance(interval, int) or interval < 1:
        raise ValueError('Monitor interval must be a positive number of seconds.')
    output = _directory(output_dir, create=False)
    stopped = threading.Event()
    handlers = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            handlers[signum] = signal.signal(signum, lambda number, frame: stopped.set())
    print('FULL_REVIEW_WATCH_READY ' + repair._json({'output': str(output), 'interval': interval}), flush=True)
    try:
        while not stopped.is_set():
            observation = record_monitor_observation(output)
            for transition in observation['alert_transitions']:
                print('FULL_REVIEW_ALERT ' + repair._json(transition), flush=True)
            print('FULL_REVIEW_WATCH ' + repair._json(observation), flush=True)
            if observation['terminal']:
                return observation
            stopped.wait(interval)
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
