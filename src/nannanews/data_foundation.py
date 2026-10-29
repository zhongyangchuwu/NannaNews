"""Explicit, private preparation of existing activity identities and offline evidence.

This database is not the serving database. No network, model calls, collection,
publication cutover, or mutation of the canonical source is performed here.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from urllib.parse import parse_qsl, urlsplit


_SCHEMA_VERSION = '2'
_SCHEMA = '''
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS activities(
 activity_id TEXT PRIMARY KEY,title TEXT NOT NULL,summary TEXT NOT NULL,category TEXT,
 time_text TEXT,venue TEXT,organizer TEXT,audience TEXT,participation TEXT,
 review_status TEXT NOT NULL,record_version TEXT NOT NULL,input_sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS activity_dates(
 activity_id TEXT REFERENCES activities,kind TEXT,date TEXT,raw_text TEXT,evidence TEXT,source_id TEXT,
 raw_text_sha256 TEXT);
CREATE TABLE IF NOT EXISTS issues(issue_id TEXT PRIMARY KEY,title TEXT,issue_date TEXT,issue_number INTEGER);
CREATE TABLE IF NOT EXISTS documents(document_id TEXT PRIMARY KEY,sha256 TEXT NOT NULL,format TEXT NOT NULL,
 filename TEXT,claimed_date TEXT,issue_date TEXT,issue_number INTEGER,parse_status TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS documents_sha256 ON documents(sha256);
CREATE TABLE IF NOT EXISTS originals(path TEXT PRIMARY KEY,document_id TEXT REFERENCES documents,sha256 TEXT,
 size INTEGER,mtime_ns INTEGER,filename_date TEXT,content_date TEXT,date_status TEXT);
CREATE TABLE IF NOT EXISTS source_records(source_id TEXT PRIMARY KEY,activity_id TEXT REFERENCES activities,
 occurrence_id TEXT UNIQUE,document_id TEXT REFERENCES documents,issue_id TEXT REFERENCES issues,issue_date TEXT,
 title TEXT,kind TEXT,source_urls_json TEXT NOT NULL,locator_json TEXT NOT NULL,summary TEXT,original_ref TEXT,
 parse_status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS activity_aliases(alias_id TEXT PRIMARY KEY,activity_id TEXT REFERENCES activities);
CREATE TABLE IF NOT EXISTS revisions(revision_id TEXT PRIMARY KEY,activity_id TEXT REFERENCES activities,
 occurrence_id TEXT,status TEXT,reason TEXT,reviewed_at TEXT,fields_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS blobs(sha256 TEXT PRIMARY KEY,size INTEGER NOT NULL,mime TEXT NOT NULL,kind TEXT NOT NULL,
 local_path TEXT,owned_local INTEGER NOT NULL DEFAULT 0,remote_key TEXT,verified_at TEXT);
CREATE INDEX IF NOT EXISTS blobs_original_paths ON blobs(local_path) WHERE owned_local=0;
CREATE TABLE IF NOT EXISTS snapshots(snapshot_id TEXT PRIMARY KEY,url TEXT,title TEXT,text_blob_sha TEXT REFERENCES blobs,
 fetched_at TEXT,status TEXT NOT NULL,manifest_json TEXT NOT NULL,remote_key TEXT,remote_verified_at TEXT);
CREATE TABLE IF NOT EXISTS snapshot_blobs(snapshot_id TEXT REFERENCES snapshots,blob_sha TEXT REFERENCES blobs,
 role TEXT,ordinal INTEGER,PRIMARY KEY(snapshot_id,blob_sha,role,ordinal));
CREATE TABLE IF NOT EXISTS activity_snapshots(activity_id TEXT REFERENCES activities,snapshot_id TEXT REFERENCES snapshots,
 source_id TEXT,PRIMARY KEY(activity_id,snapshot_id,source_id));
CREATE TABLE IF NOT EXISTS reviews(review_id TEXT PRIMARY KEY,activity_id TEXT REFERENCES activities,occurrence_id TEXT,
 input_sha256 TEXT,context_sha256 TEXT,rules_version TEXT,reviewer TEXT,status TEXT,reason TEXT,
 answer_blob_sha TEXT REFERENCES blobs,fields_json TEXT NOT NULL,error_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_snapshots(review_id TEXT REFERENCES reviews,snapshot_id TEXT REFERENCES snapshots,
 PRIMARY KEY(review_id,snapshot_id));
CREATE TABLE IF NOT EXISTS attempts(request_key TEXT PRIMARY KEY,state TEXT NOT NULL,model TEXT,rules_version TEXT,
 input_sha256 TEXT,reserved_at TEXT,finished_at TEXT,error_summary TEXT,result_review_id TEXT,source_ledger TEXT);
CREATE TABLE IF NOT EXISTS attempt_origins(request_key TEXT REFERENCES attempts,source_ledger TEXT,source_ref TEXT,state TEXT,
 PRIMARY KEY(request_key,source_ledger,source_ref));
CREATE TABLE IF NOT EXISTS ingestions(source_path TEXT PRIMARY KEY,sha256 TEXT NOT NULL,size INTEGER NOT NULL,
 mtime_ns INTEGER NOT NULL,status TEXT NOT NULL,record_count INTEGER NOT NULL DEFAULT 0,
 snapshot_count INTEGER NOT NULL DEFAULT 0,error_json TEXT NOT NULL DEFAULT '[]',completed_at TEXT,cleaned_at TEXT);
CREATE TABLE IF NOT EXISTS ingestion_outputs(source_path TEXT REFERENCES ingestions,
 kind TEXT CHECK(kind IN ('review','snapshot','blob','attempt')),row_id TEXT,PRIMARY KEY(source_path,kind,row_id));
CREATE TABLE IF NOT EXISTS cleanup_log(source_path TEXT PRIMARY KEY,sha256 TEXT NOT NULL,size INTEGER NOT NULL,
 cleaned_at TEXT NOT NULL,proof_json TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS dates_identity ON activity_dates(
 coalesce(activity_id,''),coalesce(kind,''),coalesce(date,''),coalesce(raw_text_sha256,''),coalesce(source_id,''));
CREATE INDEX IF NOT EXISTS dates_activity_source ON activity_dates(activity_id,source_id,kind,date);
CREATE UNIQUE INDEX IF NOT EXISTS activity_snapshot_identity ON activity_snapshots(
 coalesce(activity_id,''),coalesce(snapshot_id,''),coalesce(source_id,''));
CREATE UNIQUE INDEX IF NOT EXISTS snapshot_blob_identity ON snapshot_blobs(
 coalesce(snapshot_id,''),coalesce(blob_sha,''),coalesce(role,''),coalesce(ordinal,-1));
CREATE UNIQUE INDEX IF NOT EXISTS attempt_origin_identity ON attempt_origins(
 coalesce(request_key,''),coalesce(source_ledger,''),coalesce(source_ref,''));
CREATE UNIQUE INDEX IF NOT EXISTS ingestion_output_identity ON ingestion_outputs(
 coalesce(source_path,''),coalesce(kind,''),coalesce(row_id,''));
CREATE INDEX IF NOT EXISTS sources_activity ON source_records(activity_id);
CREATE INDEX IF NOT EXISTS sources_document ON source_records(document_id);
CREATE INDEX IF NOT EXISTS sources_legacy_occurrence ON source_records(
 json_extract(locator_json,'$.legacy_source_db'),json_extract(locator_json,'$.legacy_occurrence_id'));
CREATE UNIQUE INDEX IF NOT EXISTS review_snapshot_identity ON review_snapshots(
 coalesce(review_id,''),coalesce(snapshot_id,''));
'''
_TABLES = ('activities', 'activity_dates', 'issues', 'documents', 'originals', 'source_records',
           'activity_aliases', 'revisions', 'blobs', 'snapshots', 'snapshot_blobs', 'activity_snapshots',
           'reviews', 'review_snapshots', 'attempts', 'attempt_origins', 'ingestions',
           'ingestion_outputs', 'cleanup_log')
_ACTIVITY_FIELDS = ('title', 'summary', 'category', 'time', 'venue', 'organizer', 'audience', 'participation')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _identity(prefix, value):
    return prefix + hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _no_symlinks(path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Foundation paths must not contain symlinks.')


def normalize_date_claim(claim: dict) -> dict:
    """Keep short literal date evidence, not misclassified raw HTTP payloads.

    The returned copy preserves all semantic fields. A suppressed fragment keeps
    the SHA-256 of its exact original UTF-8 text and an explicit omission reason;
    it is never replaced with the normalized date. Existing normalized claims are
    unchanged, including their original digest and evidence.
    """
    result = dict(claim)
    raw = result.get('raw_text')
    if not isinstance(raw, str):
        return result
    result['raw_text_sha256'] = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    stripped = raw.lstrip()
    payload = stripped.startswith('{') or bool(re.search(r'<(?:!doctype\b|/?[a-z][^>]*>)', raw, re.I))
    if stripped.startswith('['):
        try:
            payload = payload or isinstance(json.loads(stripped), list)
        except (ValueError, RecursionError):
            payload = payload or bool(re.match(r'\[\s*[\[{\"]', stripped))
    reason = 'non_date_payload' if payload else 'oversized' if len(raw) > 512 else None
    if reason:
        result['raw_text'] = None
        result['raw_text_omitted'] = reason
    return result


def _normalize_date_fields(value):
    if isinstance(value, list):
        return [_normalize_date_fields(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _normalize_date_fields(item) for key, item in value.items()}
    if 'kind' in result and any(key in result for key in ('raw_text', 'normalized_date', 'date')):
        return normalize_date_claim(result)
    return result


def _record_date_omission(db, claim):
    if claim.get('raw_text_omitted') and claim.get('raw_text_sha256'):
        db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)',
                   ('date_raw_omitted:' + claim['raw_text_sha256'], claim['raw_text_omitted']))


def _migrate_v2(db):
    """Rewrite only the prepared foundation, preserving semantic facts and IDs."""
    with db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE activity_dates_v2(
            activity_id TEXT REFERENCES activities,kind TEXT,date TEXT,raw_text TEXT,
            evidence TEXT,source_id TEXT,raw_text_sha256 TEXT)''')
        for row in db.execute('SELECT * FROM activity_dates ORDER BY rowid'):
            claim = normalize_date_claim(dict(row))
            db.execute('INSERT INTO activity_dates_v2 VALUES(?,?,?,?,?,?,?)',
                       (claim['activity_id'], claim['kind'], claim['date'], claim['raw_text'],
                        claim['evidence'], claim['source_id'], claim.get('raw_text_sha256')))
            _record_date_omission(db, claim)
        db.execute('DROP TABLE activity_dates')
        db.execute('ALTER TABLE activity_dates_v2 RENAME TO activity_dates')
        db.execute('''CREATE UNIQUE INDEX dates_identity ON activity_dates(
            coalesce(activity_id,''),coalesce(kind,''),coalesce(date,''),
            coalesce(raw_text_sha256,''),coalesce(source_id,''))''')
        for row in db.execute('SELECT revision_id,fields_json FROM revisions ORDER BY rowid'):
            fields = _json(_normalize_date_fields(json.loads(row['fields_json'])))
            if fields != row['fields_json']:
                db.execute('UPDATE revisions SET fields_json=? WHERE revision_id=?', (fields, row['revision_id']))
        db.execute("UPDATE metadata SET value=? WHERE key='schema_version'", (_SCHEMA_VERSION,))


def connect_foundation(root: Path) -> sqlite3.Connection:
    """Explicitly create/validate the isolated private preparation database."""
    root = Path(root).absolute()
    _no_symlinks(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    path = root / 'foundation.sqlite3'
    _no_symlinks(path)
    # Create privately before SQLite opens it; never briefly expose private data.
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    path.chmod(0o600)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA journal_mode=DELETE')
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and 'metadata' not in tables:
            raise ValueError('Refusing a database that is not a foundation database.')
        if 'metadata' in tables:
            version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if version is None or version[0] not in ('1', _SCHEMA_VERSION):
                raise ValueError('Unsupported foundation schema version.')
            if version[0] == '1':
                _migrate_v2(db)
        db.executescript(_SCHEMA)
        db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)', ('schema_version', _SCHEMA_VERSION))
        db.commit()
    except Exception:
        db.close()
        raise
    return db


class SourceDB:
    """Immutable read-only canonical input; never use the application's writable connection."""

    def __init__(self, path):
        path = Path(path).resolve(strict=True)
        if not path.is_file():
            raise ValueError('Canonical input must be a SQLite database file.')
        self.path = path
        self.db = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA query_only=ON')
        self.tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.db.close()


def _read_json(path):
    with Path(path).open(encoding='utf-8') as stream:
        return json.load(stream)


def _relative_file(root, relative):
    if not isinstance(relative, str) or not relative or '\\' in relative:
        raise ValueError('Invalid public dataset reference.')
    path = root / relative
    if Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('Public dataset reference escapes its root.')
    _no_symlinks(path)
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Public dataset reference is unavailable.')
    return path


def _value(value):
    return value.get('value') if isinstance(value, dict) else value


def _activity(occurrence):
    manual = occurrence.get('manual_review')
    if isinstance(manual, dict) and isinstance(manual.get('activity'), dict):
        fields = manual['activity']
    else:
        review = occurrence.get('date_review') or {}
        fields = ((review.get('source_review') or {}).get('activity') or {}) if occurrence.get('content_status') == 'verified' else {}
    # Never promote raw body/captured page text to a canonical activity field.
    result = {key: _value(fields.get(key)) for key in _ACTIVITY_FIELDS}
    result['title'] = result['title'] if result['title'] is not None else occurrence['title']
    result['summary'] = result['summary'] if result['summary'] is not None else occurrence.get('preview', '')
    result['category'] = result['category'] if result['category'] is not None else occurrence.get('category')
    for key, value in (occurrence.get('activity_facts') or {}).items():
        if key in result and result[key] is None:
            result[key] = value
    return result


def _save_revision(db, activity_id, occurrence_id, revision, *, manual=False):
    if not isinstance(revision, dict):
        return
    fields = revision.get('activity') if manual else {
        'claims': revision.get('claims', []), 'rules_version': revision.get('rules_version')}
    identity = revision.get('revision') or _identity('date-review-', occurrence_id + ':' + _json(revision))
    db.execute('''INSERT OR IGNORE INTO revisions VALUES(?,?,?,?,?,?,?)''',
               (identity, activity_id, occurrence_id, revision.get('status'), revision.get('reason'),
                revision.get('reviewed_at'), _json(_normalize_date_fields(fields or {}))))


def _save_dates(db, activity_id, source_id, dates):
    db.execute('DELETE FROM activity_dates WHERE source_id=?', (source_id,))
    for claim in dates:
        claim = normalize_date_claim(claim)
        db.execute('INSERT OR IGNORE INTO activity_dates VALUES(?,?,?,?,?,?,?)',
                   (activity_id, claim.get('kind'), claim.get('normalized_date', claim.get('date')),
                    claim.get('raw_text'), claim.get('evidence'), source_id, claim.get('raw_text_sha256')))
        _record_date_omission(db, claim)


def _import_public(db, root):
    manifest = _read_json(root / 'manifest.json')
    catalog = _read_json(_relative_file(root, manifest['files']['catalog']))
    issues = _read_json(_relative_file(root, manifest['files']['issues']))
    version = manifest['data_version']
    seen_documents = set()
    for issue in issues:
        db.execute('INSERT OR IGNORE INTO issues VALUES(?,?,?,?)',
                   (issue['id'], issue.get('title'), issue.get('issue_date'), issue.get('issue_number')))
        for document in issue.get('documents', []):
            identity = document['id']
            seen_documents.add(identity)
            db.execute('INSERT OR IGNORE INTO documents VALUES(?,?,?,?,?,?,?,?)',
                       (identity, identity, document['format'], document.get('filename'),
                        document.get('claimed_issue_date'), document.get('issue_date'),
                        issue.get('issue_number'), 'public_imported'))
    summaries = {row['id']: row for row in catalog['occurrences']}
    if len(summaries) != len(catalog['occurrences']):
        raise ValueError('Duplicate public occurrence identity.')
    shard_cache = {}
    imported_occurrences = set()
    for item in catalog['items']:
        relative = item['detail_path']
        # Only hold one bounded shard rather than the full historical details tree.
        if relative not in shard_cache:
            shard_cache = {relative: _read_json(_relative_file(root, relative))}
        detail = shard_cache[relative][item['id']]
        occurrences = detail['occurrences']
        selected = next((row for row in occurrences if row['id'] == item['latest_occurrence_id']), None)
        if selected is None:
            raise ValueError('Public activity has no declared latest occurrence.')
        # Catalog reflects manual overlays even when raw detail titles are unchanged.
        selected = dict(selected, **summaries[selected['id']]) | {
            key: selected[key] for key in ('manual_review', 'date_review') if key in selected}
        fields = _activity(selected)
        digest = hashlib.sha256(_json({'fields': fields, 'head': selected['id'],
                                      'dates': selected.get('dates', []), 'source_urls': selected.get('source_urls', []),
                                      'occurrences': [r['id'] for r in occurrences]}).encode()).hexdigest()
        db.execute('''INSERT INTO activities VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(activity_id) DO UPDATE SET title=excluded.title,summary=excluded.summary,
            category=excluded.category,time_text=excluded.time_text,venue=excluded.venue,
            organizer=excluded.organizer,audience=excluded.audience,participation=excluded.participation,
            review_status=excluded.review_status,record_version=excluded.record_version,input_sha256=excluded.input_sha256''',
                   (item['id'], fields['title'], fields['summary'], fields['category'], fields['time'],
                    fields['venue'], fields['organizer'], fields['audience'], fields['participation'],
                    selected.get('content_status', 'unreviewed'), version, digest))
        db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',
                   ('activity_head:' + item['id'], item['latest_occurrence_id']))
        for alias in item.get('aliases', []):
            existing = db.execute('SELECT activity_id FROM activity_aliases WHERE alias_id=?', (alias,)).fetchone()
            if existing and existing[0] != item['id']:
                raise ValueError('Conflicting canonical activity alias.')
            db.execute('INSERT OR IGNORE INTO activity_aliases VALUES(?,?)', (alias, item['id']))
        for occurrence in occurrences:
            occurrence_id = occurrence['id']
            public = summaries.get(occurrence_id)
            if public is None or public['item_id'] != item['id'] or occurrence_id in imported_occurrences:
                raise ValueError('Inconsistent public occurrence mapping.')
            imported_occurrences.add(occurrence_id)
            previous = db.execute('SELECT locator_json FROM source_records WHERE source_id=?', (occurrence_id,)).fetchone()
            locator = json.loads(previous[0]) if previous else {}
            locator.update(occurrence.get('locator', {}))
            db.execute('''INSERT INTO source_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET activity_id=excluded.activity_id,
                occurrence_id=excluded.occurrence_id,document_id=excluded.document_id,issue_id=excluded.issue_id,
                issue_date=excluded.issue_date,title=excluded.title,kind=excluded.kind,
                source_urls_json=excluded.source_urls_json,locator_json=excluded.locator_json,
                summary=excluded.summary,parse_status=excluded.parse_status''',
                       (occurrence_id, item['id'], occurrence_id, occurrence['document_id'], occurrence['issue_id'],
                        public.get('issue_date'), public['title'], public.get('kind'),
                        _json(public.get('source_urls', [])), _json(locator),
                        public.get('preview', ''), None, 'public_imported'))
            _save_dates(db, item['id'], occurrence_id, occurrence.get('dates', []))
            _save_revision(db, item['id'], occurrence_id, occurrence.get('manual_review'), manual=True)
            _save_revision(db, item['id'], occurrence_id, occurrence.get('date_review'))
    counts = {'items': len(catalog['items']), 'occurrences': len(imported_occurrences),
              'issues': len(issues), 'documents': len(seen_documents)}
    if imported_occurrences != set(summaries):
        raise ValueError('Public details do not cover all source records.')
    for key, count in counts.items():
        if key in manifest.get('counts', {}) and manifest['counts'][key] != count:
            raise ValueError('Public manifest count mismatch: ' + key)
    db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('public_data_version', version))
    db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('public_counts', _json(counts)))
    return counts


def _canonical_inputs(db, path):
    """Preserve canonical revision history and original references without writes."""
    path = Path(path)
    if path.is_dir():
        path = path / 'database.db'
    originals = []
    with SourceDB(path) as source:
        if 'bulletin_documents' in source.tables:
            for row in source.db.execute('''SELECT source_sha256,format,source_filename,original_path,parsed_json
                                            FROM bulletin_documents ORDER BY id'''):
                parsed = json.loads(row['parsed_json'] or '{}')
                relative = row['original_path']
                if not relative:
                    continue
                original = Path(relative)
                if not original.is_absolute():
                    original = source.path.parent / 'uploads' / original
                originals.append({'source': str(original), 'sha256': row['source_sha256'], 'format': row['format'],
                                  'filename_date': parsed.get('claimed_issue_date'),
                                  'content_date': parsed.get('issue_date'), 'issue_number': parsed.get('issue_number')})
        if 'reader_review_revisions' in source.tables:
            for row in source.db.execute('SELECT * FROM reader_review_revisions ORDER BY id'):
                payload = json.loads(row['payload_json'])
                identity = row['item_id']
                match = db.execute('SELECT activity_id FROM activities WHERE activity_id=? UNION ALL '
                                   'SELECT activity_id FROM activity_aliases WHERE alias_id=? LIMIT 1',
                                   (identity, identity)).fetchone()
                fields = {'activity': payload.get('activity'), 'legacy_item_id': identity,
                          'base_version': row['base_version'], 'previous_head': row['previous_head']}
                db.execute('INSERT OR IGNORE INTO revisions VALUES(?,?,?,?,?,?,?)',
                           (row['revision'], match[0] if match else None, None, payload.get('status'),
                            payload.get('reason'), row['created_at'], _json(_normalize_date_fields(fields))))
        # The public IDs are deterministic projections of these exact source IDs,
        # not title/date matching. This enriches evidence only, never creates activities.
        if {'bulletin_occurrences', 'bulletin_documents'} <= source.tables:
            for row in source.db.execute('''SELECT o.id,o.item_id,o.local_key,o.locator_json,d.source_sha256
                FROM bulletin_occurrences o JOIN bulletin_documents d ON d.id=o.document_id'''):
                identity = _identity('occ-', row['source_sha256'] + ':' + row['local_key'])
                existing = db.execute('SELECT locator_json,activity_id FROM source_records WHERE occurrence_id=?', (identity,)).fetchone()
                if existing:
                    locator = _bounded_locator(json.loads(row['locator_json']))
                    locator.update(json.loads(existing['locator_json']))
                    locator.update(local_key=row['local_key'], legacy_occurrence_id=row['id'],
                                   legacy_item_id=row['item_id'], legacy_source_db=str(source.path))
                    db.execute('UPDATE source_records SET locator_json=? WHERE occurrence_id=?', (_json(locator), identity))
    return originals


def _bounded_locator(locator):
    # Preserve precise page/table/section coordinates, not embedded copies of HTML.
    keys = ('tag', 'id', 'page', 'bbox', 'bboxes', 'tagpath', 'mcids', 'table', 'row', 'column',
            'section_id', 'heading_index', 'anchor', 'local_key')
    return {key: locator[key] for key in keys if key in locator}


def _register_original(db, entry):
    path = Path(entry['source']).absolute()
    _no_symlinks(path)
    if not path.is_file() or path.suffix.lower() not in {'.pdf', '.html', '.htm'}:
        raise ValueError('Original must be an available regular HTML/PDF file.')
    before = path.stat()
    digest = _sha(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Original changed while being registered.')
    if entry.get('sha256') and entry['sha256'] != digest:
        raise ValueError('Original digest differs from the supplied inventory.')
    fmt = 'pdf' if path.suffix.lower() == '.pdf' else 'html'
    filename_date, content_date = entry.get('filename_date'), entry.get('content_date')
    status = 'conflict' if filename_date and content_date and filename_date != content_date else entry.get('date_status')
    db.execute('INSERT OR IGNORE INTO documents VALUES(?,?,?,?,?,?,?,?)',
               (digest, digest, fmt, path.name, filename_date, content_date, entry.get('issue_number'), 'registered'))
    document = db.execute('SELECT * FROM documents WHERE sha256=?', (digest,)).fetchone()
    document_id = document['document_id']
    if document['parse_status'] == 'parsed' and document['issue_date']:
        content_date = document['issue_date']
        status = 'conflict' if filename_date and filename_date != content_date else 'matched' if filename_date else 'unknown'
    existing = db.execute('SELECT sha256 FROM originals WHERE path=?', (str(path),)).fetchone()
    if existing and existing[0] != digest:
        raise ValueError('Registered original path now contains different bytes.')
    db.execute('''INSERT INTO originals VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
        size=excluded.size,mtime_ns=excluded.mtime_ns,filename_date=coalesce(excluded.filename_date,originals.filename_date),
        content_date=coalesce(excluded.content_date,originals.content_date),date_status=coalesce(excluded.date_status,originals.date_status)''',
               (str(path), document_id, digest, before.st_size, before.st_mtime_ns, filename_date, content_date, status))
    return path, document_id, digest


def _parse_original(db, root, store, path, document_id, digest):
    from nannanews.html_bulletin_adapter import parse_html
    from nannanews.pdf_adapter import parse_pdf

    document = db.execute('SELECT * FROM documents WHERE document_id=?', (document_id,)).fetchone()
    if document['parse_status'] == 'parsed':
        return
    with tempfile.TemporaryDirectory(prefix='.parse-', dir=root) as work:
        parsed = parse_pdf(path, Path(work)) if path.suffix.lower() == '.pdf' else parse_html(path)
        if parsed.get('source_sha256') != digest or _sha(path) != digest:
            raise ValueError('Original changed during parsing.')
        # One page/coordinate evidence blob per document. The original remains
        # the authoritative full text; parser copies/assets die with the workdir.
        evidence = {'document_id': document_id, 'parser': parsed.get('parser'),
                    'title': parsed.get('title'), 'issue_date': parsed.get('issue_date'),
                    'issue_date_evidence': parsed.get('issue_date_evidence'),
                    'issue_number': parsed.get('issue_number'),
                    'pages': [{key: page[key] for key in ('page', 'width', 'height') if key in page}
                              for page in parsed.get('pages', [])],
                    'warnings': [{'code': row.get('code')} for row in parsed.get('warnings', [])],
                    'entries': [{'local_key': row['local_key'], 'locator': _bounded_locator(row.get('locator', {})),
                                 'dates': [normalize_date_claim({key: claim.get(key) for key in
                                            ('kind', 'normalized_date', 'raw_text', 'resolution', 'evidence')})
                                           for claim in row.get('dates', [])]} for row in parsed['items']]}
        evidence_sha = store.put_bytes(_json(evidence).encode('utf-8'), mime='application/json', kind='document_evidence')
        db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('document_evidence:' + document_id, evidence_sha))
        issue_date = parsed.get('issue_date')
        issue_number = parsed.get('issue_number')
        # Match only explicit issue facts. Do not merge ambiguous same-day issues.
        candidates = list(db.execute('SELECT issue_id FROM issues WHERE issue_date IS ? AND issue_number IS ?',
                                     (issue_date, issue_number))) if issue_date or issue_number else []
        issue_id = candidates[0][0] if len(candidates) == 1 else 'document-' + digest
        db.execute('INSERT OR IGNORE INTO issues VALUES(?,?,?,?)',
                   (issue_id, parsed.get('title'), issue_date, issue_number))
        for item in parsed['items']:
            occurrence_id = _identity('occ-', digest + ':' + item['local_key'])
            locator = _bounded_locator(item.get('locator', {}))
            locator.update(local_key=item['local_key'], evidence_blob_sha256=evidence_sha)
            existing = db.execute('SELECT * FROM source_records WHERE occurrence_id=?', (occurrence_id,)).fetchone()
            if existing:
                old_locator = json.loads(existing['locator_json'])
                old_locator.update(locator)
                db.execute('UPDATE source_records SET locator_json=?,original_ref=? WHERE occurrence_id=?',
                           (_json(old_locator), str(path), occurrence_id))
            else:
                db.execute('INSERT INTO source_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (occurrence_id, None, occurrence_id, document_id, issue_id, issue_date, item['title'],
                            item.get('kind'), _json(item.get('source_urls', [])), _json(locator),
                            item.get('body_text', '')[:240], str(path), 'pending_review'))
        db.execute("UPDATE documents SET issue_date=coalesce(?,issue_date),issue_number=coalesce(?,issue_number),parse_status='parsed' WHERE document_id=?",
                   (issue_date, issue_number, document_id))
        db.execute('''UPDATE originals SET content_date=coalesce(?,content_date),date_status=CASE
            WHEN filename_date IS NOT NULL AND coalesce(?,content_date) IS NOT NULL
                 AND filename_date<>coalesce(?,content_date) THEN 'conflict'
            WHEN filename_date IS NOT NULL AND filename_date=coalesce(?,content_date) THEN 'matched'
            ELSE coalesce(date_status,'unknown') END WHERE document_id=?''',
                   (issue_date, issue_date, issue_date, issue_date, document_id))


def build_foundation(root, *, public_data: Path, originals_plan: Path | None = None,
                     canonical_data: Path | None = None, parse_originals: bool = True) -> dict:
    """Import only existing identities; stage unknown offline entries for review."""
    root = Path(root).absolute()
    errors = []
    with closing(connect_foundation(root)) as db:
        with db:
            public_counts = _import_public(db, Path(public_data))
            entries = []
            if originals_plan is not None:
                plan = _read_json(originals_plan)
                entries.extend(plan['historical_files'])
            if canonical_data is not None:
                entries.extend(_canonical_inputs(db, canonical_data))
        from nannanews.data_foundation_blobs import BlobStore
        store = BlobStore(root, db)
        for entry in entries:
            try:
                with db:
                    path, document_id, digest = _register_original(db, entry)
                    store.put_file(path, mime='application/pdf' if path.suffix.lower() == '.pdf' else 'text/html', kind='original')
            except (OSError, ValueError, sqlite3.Error) as exc:
                errors.append({'stage': 'register_original', 'source_path': entry.get('source'),
                               'code': getattr(exc, 'code', 'original_unavailable'), 'message': str(exc)})
                continue
            if parse_originals:
                try:
                    with db:
                        _parse_original(db, root, store, path, document_id, digest)
                except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                    with db:
                        db.execute("UPDATE documents SET parse_status='failed' WHERE document_id=?", (document_id,))
                    errors.append({'stage': 'parse_original', 'source_path': str(path),
                                   'code': getattr(exc, 'code', 'parse_failed'), 'message': str(exc)})
        with db:
            db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('preparation_errors', _json(errors)))
        return {'public_counts': public_counts, 'counts': _counts(db), 'errors': errors,
                'pending_review': db.execute("SELECT count(*) FROM source_records WHERE parse_status='pending_review'").fetchone()[0],
                'root': str(root)}


def _counts(db):
    return {table: db.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in _TABLES}


def _open_existing(root):
    path = Path(root).absolute() / 'foundation.sqlite3'
    _no_symlinks(path)
    if not path.is_file():
        raise ValueError('Foundation has not been explicitly prepared.')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not set((*_TABLES, 'metadata')) <= tables:
            raise ValueError('Database is not a prepared foundation.')
        version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if version is None or version[0] != _SCHEMA_VERSION:
            raise ValueError('Unsupported foundation schema version.')
    except Exception:
        db.close()
        raise
    return db


def _public_url(value):
    from nannanews.static_export import _public_url as safe_url
    if not isinstance(value, str) or safe_url(value) is None:
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname.lower()
        if 'authserver' in host or re.search(r'/(?:login|signin|oauth|authserver)(?:/|$)', parts.path, re.I):
            return None
        if any(re.search(r'password|passwd|token|secret|cookie|session|credential|authorization|api.?key', key, re.I)
               for key, _ in parse_qsl(parts.query)):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if '.' not in host or host.endswith(('.local', '.localhost', '.internal')):
                return None
        return value
    except (ValueError, AttributeError):
        return None


def export_public(root, output: Path) -> dict:
    """Write a new bounded whitelist, never the historical reader's private details."""
    output = Path(output).absolute()
    _no_symlinks(output)
    private_root = Path(root).resolve()
    if output == private_root / 'foundation.sqlite3' or output.is_relative_to(private_root):
        raise ValueError('Public output must be outside the private foundation root.')
    if output.exists():
        raise FileExistsError('Refusing to overwrite an existing public output target.')
    with closing(_open_existing(root)) as db:
        items = []
        for row in db.execute('SELECT * FROM activities ORDER BY activity_id'):
            item = {key: row[key] for key in ('activity_id', 'title', 'category', 'time_text', 'venue',
                                             'organizer', 'audience', 'participation', 'review_status')}
            item['summary'] = row['summary'][:240]
            head = db.execute('SELECT value FROM metadata WHERE key=?', ('activity_head:' + row['activity_id'],)).fetchone()
            source = db.execute('SELECT source_id,source_urls_json FROM source_records WHERE activity_id=? AND occurrence_id=?',
                                (row['activity_id'], head[0])).fetchone() if head else None
            if source is None:
                raise ValueError('Activity has no valid declared public head.')
            item['source_urls'] = sorted({url for value in json.loads(source['source_urls_json']) if (url := _public_url(value))})
            item['dates'] = [dict(kind=claim['kind'], date=claim['date']) for claim in db.execute(
                'SELECT DISTINCT kind,date FROM activity_dates WHERE activity_id=? AND source_id=? AND date IS NOT NULL ORDER BY kind,date',
                (row['activity_id'], source['source_id']))]
            items.append(item)
        payload = {'schema_version': 1, 'activities': items}
        data = (_json(payload) + '\n').encode('utf-8')
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.foundation-public-', dir=output.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {'activities': len(items), 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'output': str(output)}


def compact_foundation(root) -> dict:
    """Explicitly reclaim pages in an already migrated preparation database only."""
    with closing(_open_existing(root)):
        pass
    path = Path(root).absolute() / 'foundation.sqlite3'
    before = path.stat().st_size
    with closing(connect_foundation(root)) as db:
        db.execute('VACUUM')
    after = path.stat().st_size
    return {'schema_version': _SCHEMA_VERSION, 'bytes_before': before, 'bytes_after': after,
            'bytes_reclaimed': before - after}


def verify_foundation(root, *, full: bool = False) -> dict:
    """Check local SQLite, references and file metadata; hash content only on request."""
    errors = []
    with closing(_open_existing(root)) as db:
        check = 'integrity_check' if full else 'quick_check'
        integrity = [row[0] for row in db.execute('PRAGMA ' + check)]
        foreign_keys = [dict(row) for row in db.execute('PRAGMA foreign_key_check')]
        if integrity != ['ok']:
            errors.append({'code': check, 'details': integrity})
        if foreign_keys:
            errors.append({'code': 'foreign_key_check', 'details': foreign_keys})
        for original in db.execute('SELECT * FROM originals ORDER BY path'):
            local = db.execute('SELECT local_path FROM blobs WHERE sha256=?', (original['sha256'],)).fetchone()
            local_path = local['local_path'] if local else None
            path = Path(local_path or original['path'])
            try:
                _no_symlinks(path)
                stat = path.stat()
                if (stat.st_size != original['size']
                        or (path == Path(original['path']) and stat.st_mtime_ns != original['mtime_ns'])
                        or (full and not local_path and _sha(path) != original['sha256'])):
                    errors.append({'code': 'original_changed', 'path': str(path)})
            except (OSError, ValueError):
                errors.append({'code': 'original_unavailable', 'path': str(path)})
        for blob in db.execute('SELECT * FROM blobs'):
            if blob['local_path']:
                try:
                    path = Path(blob['local_path'])
                    _no_symlinks(path)
                    if path.stat().st_size != blob['size'] or (full and _sha(path) != blob['sha256']):
                        errors.append({'code': 'blob_changed', 'sha256': blob['sha256']})
                except (OSError, ValueError):
                    errors.append({'code': 'blob_unavailable', 'sha256': blob['sha256']})
            else:
                errors.append({'code': 'blob_has_no_local_copy', 'sha256': blob['sha256']})
        for row in db.execute('SELECT source_id,locator_json FROM source_records'):
            locator = json.loads(row['locator_json'])
            sha = locator.get('evidence_blob_sha256')
            if sha and not db.execute('SELECT 1 FROM blobs WHERE sha256=?', (sha,)).fetchone():
                errors.append({'code': 'missing_document_evidence', 'source_id': row['source_id']})
        for row in db.execute("SELECT key,value FROM metadata WHERE key LIKE 'document_evidence:%'"):
            if not db.execute('SELECT 1 FROM blobs WHERE sha256=?', (row['value'],)).fetchone():
                errors.append({'code': 'missing_document_evidence', 'document_id': row['key'].split(':', 1)[1]})
        expected = db.execute("SELECT value FROM metadata WHERE key='public_counts'").fetchone()
        if expected:
            expected = json.loads(expected[0])
            actual = {'items': db.execute('SELECT count(*) FROM activities').fetchone()[0],
                      'occurrences': db.execute('SELECT count(*) FROM source_records WHERE activity_id IS NOT NULL').fetchone()[0]}
            for key, count in actual.items():
                if count != expected[key]:
                    errors.append({'code': 'public_count_mismatch', 'kind': key, 'expected': expected[key], 'actual': count})
        references = (('activity_dates', 'source_id', 'source_records', 'source_id'),
                      ('activity_snapshots', 'source_id', 'source_records', 'source_id'),
                      ('attempts', 'result_review_id', 'reviews', 'review_id'))
        for table, column, target, target_column in references:
            for row in db.execute(f'SELECT DISTINCT a.{column} FROM {table} a WHERE a.{column} IS NOT NULL '
                                  f'AND NOT EXISTS(SELECT 1 FROM {target} b WHERE b.{target_column}=a.{column})'):
                errors.append({'code': 'missing_reference', 'table': table, 'column': column, 'row_id': row[0]})
        output_tables = {'review': ('reviews', 'review_id'), 'snapshot': ('snapshots', 'snapshot_id'),
                         'blob': ('blobs', 'sha256'), 'attempt': ('attempts', 'request_key')}
        for row in db.execute('SELECT * FROM ingestion_outputs'):
            target = output_tables.get(row['kind'])
            if target is None or not db.execute(f'SELECT 1 FROM {target[0]} WHERE {target[1]}=?', (row['row_id'],)).fetchone():
                errors.append({'code': 'missing_ingestion_output', 'source_path': row['source_path'], 'kind': row['kind']})
        preparation = db.execute("SELECT value FROM metadata WHERE key='preparation_errors'").fetchone()
        preparation_errors = json.loads(preparation[0]) if preparation else []
        statuses = {table: {row[0]: row[1] for row in db.execute('SELECT ' + column + ',count(*) FROM ' + table + ' GROUP BY ' + column)}
                    for table, column in (('documents', 'parse_status'), ('source_records', 'parse_status'),
                                          ('activities', 'review_status'), ('ingestions', 'status'))}
        return {'ok': not errors and not preparation_errors, 'level': 'full' if full else 'metadata',
                'counts': _counts(db), 'statuses': statuses,
                'integrity': integrity, 'foreign_key_errors': foreign_keys, 'errors': errors,
                'preparation_errors': preparation_errors}
