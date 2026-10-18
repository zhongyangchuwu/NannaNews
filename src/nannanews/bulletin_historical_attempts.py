"""Private, persistent request prevention for explicitly requested historical reviews."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile


MAX_REVIEW_ARTIFACT_BYTES = 128 * 1024 * 1024


def _encoded_size(value):
    return sum(len(part) if part.isascii() else len(part.encode('utf-8')) for part in json.JSONEncoder(
        ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).iterencode(value)) + 1


class StorageBlocked(Exception):
    def __init__(self, reason, capture_artifact):
        super().__init__(reason)
        self.capture_artifact = capture_artifact


class ReviewStorage:
    """Admit a complete receipt before paying; keep oversized evidence separately."""
    def __init__(self, artifact, output, *, response_bytes):
        self.artifact = artifact
        self.output = output
        self.response_bytes = response_bytes
        self.progress_paths = set()

    def _fits(self, size):
        existing = self.artifact.stat().st_size if self.artifact.exists() else 0
        return existing + size <= min(MAX_REVIEW_ARTIFACT_BYTES, 128 * 1024 * 1024)

    def preserve(self, value):
        directory = self.output / 'captures'
        if directory.is_symlink():
            raise ValueError('Private capture directory must not be a symlink.')
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        digest = hashlib.sha256()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix='.capture-', delete=False) as stream:
                temporary = Path(stream.name)
                os.fchmod(stream.fileno(), 0o600)
                for part in json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                        separators=(',', ':'), allow_nan=False).iterencode(value):
                    data = part.encode('utf-8')
                    digest.update(data)
                    stream.write(data)
                stream.write(b'\n')
                stream.flush()
                os.fsync(stream.fileno())
            target = directory / ('capture-' + digest.hexdigest() + '.json')
            temporary.replace(target)
            temporary = None
            return str(target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def admit(self, source_review, identity):
        # The response body is bounded by the transport. Its claims/reasons appear
        # once and citations at most twice (quotes + quote_evidence); four bodies
        # plus verdict/identity overhead safely reserve the receipt expansion.
        evidence = dict(identity, source_review=source_review)
        required = _encoded_size(evidence) + 4 * self.response_bytes + 64 * 1024
        if not self._fits(required):
            raise StorageBlocked('Complete captured evidence and bounded model output cannot fit the admissible review artifact; no model request was sent.',
                                 self.preserve(evidence))

    def check_record(self, record):
        if not self._fits(_encoded_size(record)):
            raise StorageBlocked('Complete review receipt cannot fit the admissible review artifact; evidence is retained privately and this occurrence is not complete.',
                                 self.preserve(record))

    def compact_progress(self, record, *, progress, capture_artifact=None, **details):
        capture_artifact = capture_artifact or self.preserve(record)
        value = dict(record, status='unresolved', claims=[], source_review={
            'status': 'secondary_only', 'reason': '完整采集证据保留在 capture_artifact；本记录仅为未完成进度，不是来源核验结论。',
            'sources': [], 'attempts': []}, progress=progress, capture_artifact=capture_artifact)
        value.update(details)
        return value

    def progress_path(self, record):
        size = _encoded_size(record)
        if size > 128 * 1024 * 1024:
            raise ValueError('Even the compact progress identity exceeds the private artifact limit.')
        index = 0
        while True:
            name = 'review-progress.jsonl' if index == 0 else 'review-progress-' + str(index).zfill(6) + '.jsonl'
            path = self.output / name
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError('Review progress artifact must be a regular private file.')
            existing = path.stat().st_size if path.exists() else 0
            if existing + size <= 128 * 1024 * 1024:
                self.progress_paths.add(str(path))
                return path
            index += 1


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def ledger_path(database):
    """Return the canonical private sibling path, including before restore creates the DB."""
    database = Path(database).expanduser().resolve()
    return database.with_name('.' + database.name + '.historical-attempts.sqlite3')


def validate_schema(db):
    """Validate a sidecar's schema without creating or modifying anything.

    Backup/restore callers must separately run SQLite integrity validation and use
    Connection.backup(), rather than copying a possibly active database file.
    """
    objects = {(row[0], row[1]) for row in db.execute(
        "SELECT type,name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'")}
    if objects != {('table', 'historical_attempts'), ('table', 'historical_outcomes')}:
        raise ValueError('Historical attempt ledger has an unexpected schema.')
    expected = {
        'historical_attempts': [
            ('request_sha256', 'TEXT', 0, 1), ('request_json', 'TEXT', 1, 0),
            ('state', 'TEXT', 1, 0), ('reserved_at', 'TEXT', 1, 0),
            ('finished_at', 'TEXT', 0, 0), ('error', 'TEXT', 0, 0),
            ('raw_output_json', 'TEXT', 0, 0), ('decision_json', 'TEXT', 0, 0)],
        'historical_outcomes': [
            ('occurrence_id', 'TEXT', 1, 1), ('input_sha256', 'TEXT', 1, 2),
            ('context_sha256', 'TEXT', 1, 3), ('rules_version', 'TEXT', 1, 4),
            ('model_name', 'TEXT', 1, 5)],
    }
    for table, columns in expected.items():
        actual = [(row[1], row[2], row[3], row[5]) for row in db.execute('PRAGMA table_info(' + table + ')')]
        if actual != columns:
            raise ValueError('Historical attempt ledger has unexpected columns or primary keys.')


def logical_request(occurrence, original_claims, source_data, *, rules_version, model_name):
    """Hash semantic activity evidence, never receipt IDs or capture timestamps."""
    publications = []
    for related in occurrence.get('related_occurrences') or [occurrence]:
        corrections = [{key: value for key, value in correction.items() if key != 'fetched_at'}
                       for correction in related.get('source_corrections', [])]
        publications.append({'kind': related.get('kind', ''), 'title': related.get('title', ''),
            'body_text': related.get('body_text', ''),
            'issue_dates': sorted(set(related.get('issue_dates', [related.get('issue_date')])), key=lambda value: value or ''),
            'source_urls': sorted(set(related.get('source_urls', []))),
            'source_corrections': sorted(corrections, key=_json)})
    sources = []
    for source in source_data['linked_primary_sources']:
        semantic = {key: value for key, value in source.items() if key not in {'fetched_at', 'images'}}
        semantic['images'] = sorted(({key: value for key, value in image.items() if key != 'image_index'}
                                     for image in source.get('images', [])), key=_json)
        sources.append(semantic)
    claims = [{key: value for key, value in claim.items() if key != 'locator'}
              for claim in original_claims if claim['kind'] in {'event', 'deadline', 'registration', 'application'}]
    # Transport failure messages may contain volatile timestamps. Their URL and code
    # capture the semantic absence; no response body exists to include in the key.
    failures = [{key: attempt[key] for key in ('url', 'status', 'code')}
                for attempt in source_data['source_acquisition_attempts']]
    payload = {'rules_version': rules_version, 'model_name': model_name,
        'publications': sorted({_json(value) for value in publications}),
        'date_claims': sorted({_json(value) for value in claims}),
        'primary_sources': sorted(sources, key=_json),
        'source_failures': sorted(failures, key=_json)}
    encoded = _json(payload)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest(), encoded


class AttemptBlocked(Exception):
    def __init__(self, previous):
        super().__init__('An equivalent historical model request was already reserved; it was not resent.')
        self.previous = previous


class AttemptStore:
    """One sidecar per canonical evidence DB; no formal evidence schema changes."""
    def __init__(self, database):
        self.path = ledger_path(database)
        self.audit_directory = self.path.with_name(self.path.name.removesuffix('.historical-attempts.sqlite3') + '.model-audit')

    @contextmanager
    def _connect(self):
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute('''CREATE TABLE IF NOT EXISTS historical_attempts (
                request_sha256 TEXT PRIMARY KEY, request_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('reserved','completed','failed')),
                reserved_at TEXT NOT NULL, finished_at TEXT, error TEXT,
                raw_output_json TEXT, decision_json TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS historical_outcomes (
                occurrence_id TEXT NOT NULL, input_sha256 TEXT NOT NULL,
                context_sha256 TEXT NOT NULL, rules_version TEXT NOT NULL, model_name TEXT NOT NULL,
                PRIMARY KEY(occurrence_id,input_sha256,context_sha256,rules_version,model_name))''')
            db.commit()
            validate_schema(db)
            yield db

    def lookup(self, request_key):
        if not self.path.exists():
            return None
        with self._connect() as db:
            previous = db.execute('SELECT * FROM historical_attempts WHERE request_sha256=?', (request_key,)).fetchone()
            return dict(previous) if previous is not None else None

    def reserve(self, request_key, request_json):
        """Commit before returning permission to send; interrupted claims never expire."""
        with self._connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM historical_attempts WHERE request_sha256=?', (request_key,)).fetchone()
            if previous is not None:
                raise AttemptBlocked(dict(previous))
            db.execute('''INSERT INTO historical_attempts
                (request_sha256,request_json,state,reserved_at) VALUES(?,?,'reserved',?)''',
                (request_key, request_json, datetime.now(timezone.utc).isoformat()))

    def finish(self, request_key, *, decision, raw_output=None, error=None):
        with self._connect() as db, db:
            changed = db.execute('''UPDATE historical_attempts
                SET state=?,finished_at=?,error=?,raw_output_json=?,decision_json=?
                WHERE request_sha256=? AND state='reserved' ''',
                ('failed' if error else 'completed', datetime.now(timezone.utc).isoformat(),
                 error, _json(raw_output) if raw_output is not None else None,
                 _json(decision), request_key)).rowcount
            if changed != 1:
                raise ValueError('Historical attempt reservation is missing or was already finalized.')

    def bound_outcomes(self):
        if not self.path.exists():
            return set()
        with self._connect() as db:
            return {tuple(row) for row in db.execute('SELECT * FROM historical_outcomes')}

    def record_outcome(self, record, model_name):
        with self._connect() as db, db:
            db.execute('''INSERT OR IGNORE INTO historical_outcomes
                (occurrence_id,input_sha256,context_sha256,rules_version,model_name) VALUES(?,?,?,?,?)''',
                (record['occurrence_id'], record['input_sha256'], record['related_context_sha256'],
                 record['rules_version'], model_name))


@contextmanager
def lock_output(output):
    """Serialize same-directory resumes without holding any database lock."""
    descriptor = os.open(output / '.review.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        # A second process should fail clearly, not wait for a paid network call.
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('This historical review output directory is already in use.') from None
        yield
    finally:
        os.close(descriptor)
