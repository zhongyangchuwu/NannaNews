"""Explicit operator-reviewed source replacements over immutable bulletin evidence.

Preparation, planning and application never fetch a URL or call a model. Captures
must be supplied by the operator; their bytes and review binding remain private.
"""
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from nannanews.bulletin_common import source_url


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _exists(db, name):
    return db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone() is not None


def create_source_schema(db):
    from nannanews import schema
    schema.execute_script(db, '''
        CREATE TABLE IF NOT EXISTS bulletin_source_revisions (
            id INTEGER PRIMARY KEY,
            occurrence_id INTEGER NOT NULL REFERENCES bulletin_occurrences(id),
            original_url TEXT NOT NULL, corrected_url TEXT NOT NULL,
            input_sha256 TEXT NOT NULL, previous_head_sha256 TEXT,
            revision_sha256 TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin='operator_review'),
            actor TEXT NOT NULL, reason TEXT NOT NULL, review_json TEXT NOT NULL,
            created_at TEXT NOT NULL, UNIQUE(occurrence_id,revision_sha256));
        CREATE INDEX IF NOT EXISTS bulletin_source_revision_head
            ON bulletin_source_revisions(occurrence_id,id);
        CREATE TABLE IF NOT EXISTS bulletin_source_date_invalidations (
            source_revision_id INTEGER NOT NULL REFERENCES bulletin_source_revisions(id),
            date_revision_id INTEGER NOT NULL REFERENCES bulletin_date_revisions(id),
            PRIMARY KEY(source_revision_id,date_revision_id));
        CREATE VIEW IF NOT EXISTS bulletin_effective_sources AS
            SELECT DISTINCT s.occurrence_id,coalesce(r.corrected_url,s.source_url) AS source_url
            FROM bulletin_sources s LEFT JOIN bulletin_source_revisions r ON r.id=(
                SELECT max(h.id) FROM bulletin_source_revisions h
                WHERE h.occurrence_id=s.occurrence_id AND h.original_url=s.source_url);
    ''')
    for table in ('bulletin_source_revisions', 'bulletin_source_date_invalidations'):
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.lower()}
                BEFORE {operation} ON {table} BEGIN
                SELECT RAISE(ABORT,'bulletin source revisions are immutable'); END''')


def sources_table(db):
    """Allow old databases to be read without initializing or migrating them."""
    return 'bulletin_effective_sources' if _exists(db, 'bulletin_effective_sources') else 'bulletin_sources'


def public_corrections(db):
    result = defaultdict(list)
    if not _exists(db, 'bulletin_source_revisions'):
        return result
    for row in db.execute('''SELECT r.occurrence_id,r.original_url,r.corrected_url,r.reason,
            json_extract(r.review_json,'$.capture.title') AS title,
            json_extract(r.review_json,'$.capture.raw_response_digest') AS sha256,
            json_extract(r.review_json,'$.capture.fetched_at') AS fetched_at
            FROM bulletin_source_revisions r JOIN
            (SELECT occurrence_id,original_url,max(id) id FROM bulletin_source_revisions
             GROUP BY occurrence_id,original_url) h ON h.id=r.id ORDER BY r.id'''):
        value = dict(row)
        result[value.pop('occurrence_id')].append(value)
    return result


def _snapshot(db):
    from nannanews import bulletin_date_repair as dates
    from nannanews import bulletin_activities

    rows, originals, heads = dates.date_snapshot(db)
    source_heads = {}
    if _exists(db, 'bulletin_source_revisions'):
        source_heads = {row['occurrence_id']: dict(row) for row in db.execute('''SELECT r.occurrence_id,r.revision_sha256
            FROM bulletin_source_revisions r JOIN (SELECT occurrence_id,max(id) id
            FROM bulletin_source_revisions GROUP BY occurrence_id) h ON h.id=r.id''')}
    raw_sources = defaultdict(list)
    for row in db.execute('SELECT occurrence_id,source_url FROM bulletin_sources ORDER BY source_url'):
        raw_sources[row['occurrence_id']].append(row['source_url'])
    for row in rows:
        row['original_source_urls'] = raw_sources[row['id']]
    fingerprint = _hash([dates._fingerprint(rows, originals, heads),
        sorted((key, value['revision_sha256']) for key, value in source_heads.items()),
        sorted(bulletin_activities.activity_map(db).items())])
    return rows, source_heads, fingerprint


def _input(row):
    return _hash({key: row[key] for key in ('source_sha256', 'local_key', 'title', 'body_text',
                                          'locator_json', 'original_source_urls')})


def _capture(value, corrected_url):
    from nannanews import bulletin_date_repair as dates

    clean = dates._validate_source_review({'status': 'secondary_only',
        'reason': 'Operator-supplied capture, not an AI verdict.', 'sources': [value], 'attempts': []})['sources'][0]
    if (clean['url'] != corrected_url or not clean['title'].strip() or not clean['text'].strip()
            or not isinstance(clean['resolved_url'], str) or not clean['resolved_url']
            or not clean['raw_response_base64'] or dates._homepage_redirect(clean['url'], clean['resolved_url'])):
        raise ValueError('Correction requires a captured, identified primary article at the reviewed URL.')
    try:
        timestamp = datetime.fromisoformat(clean['fetched_at'])
        if timestamp.tzinfo is None:
            raise ValueError
    except ValueError:
        raise ValueError('Capture fetched_at requires an ISO timestamp with timezone.') from None
    return clean


def prepare_review(db, *, occurrence_id, original_url, corrected_url, capture, reason, actor):
    """Bind explicit reviewed material to raw evidence and the current source head."""
    rows, heads, _ = _snapshot(db)
    by_id = {'occ-' + hashlib.sha256((row['source_sha256'] + ':' + row['local_key']).encode()).hexdigest()[:24]: row
             for row in rows}
    row = by_id.get(occurrence_id)
    if row is None:
        raise ValueError('Unknown public occurrence ID.')
    value = {'schema_version': 1, 'origin': 'operator_review', 'occurrence_id': occurrence_id,
        'document_sha256': row['source_sha256'], 'local_key': row['local_key'],
        'input_sha256': _input(row), 'expected_head_sha256': heads.get(row['id'], {}).get('revision_sha256'),
        'original_url': original_url, 'corrected_url': corrected_url,
        'actor': actor, 'reason': reason, 'capture': capture}
    return _validate(value, row, heads.get(row['id']))[0]


_FIELDS = {'schema_version', 'origin', 'occurrence_id', 'document_sha256', 'local_key',
           'input_sha256', 'expected_head_sha256', 'original_url', 'corrected_url', 'actor', 'reason', 'capture'}


def _validate(value, row, head):
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError('Source correction review has unsupported fields.')
    public_id = 'occ-' + hashlib.sha256((row['source_sha256'] + ':' + row['local_key']).encode()).hexdigest()[:24]
    if (type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['origin'] != 'operator_review' or value['occurrence_id'] != public_id
            or value['document_sha256'] != row['source_sha256'] or value['local_key'] != row['local_key']
            or value['input_sha256'] != _input(row)):
        raise ValueError('Source correction does not match immutable source evidence.')
    for key in ('actor', 'reason'):
        if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > (4000 if key == 'reason' else 200):
            raise ValueError('Source correction requires an operator actor and public explanation.')
    if value['actor'].lower().startswith(('luna:', 'ai:', 'model:')):
        raise ValueError('Source correction actor must identify an operator, not an AI reviewer.')
    old, new = value['original_url'], value['corrected_url']
    if (not isinstance(old, str) or not isinstance(new, str) or not new
            or source_url(old) != old or source_url(new) != new or old == new
            or old not in row['original_source_urls']):
        raise ValueError('Correction must replace one original linked URL with a canonical public URL.')
    clean = dict(value, capture=_capture(value['capture'], new))
    digest = _hash(clean)
    if head and head['revision_sha256'] == digest:
        return clean, digest, True
    if value['expected_head_sha256'] != (head['revision_sha256'] if head else None):
        raise ValueError('Source correction head changed; prepare a fresh review.')
    return clean, digest, False


def plan_repairs(db, *, review_files=(), reviews=()):
    from nannanews import bulletin_date_repair as dates

    rows, heads, fingerprint = _snapshot(db)
    by_id = {'occ-' + hashlib.sha256((row['source_sha256'] + ':' + row['local_key']).encode()).hexdigest()[:24]: row
             for row in rows}
    values = list(reviews)
    for filename in review_files:
        path = Path(filename)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024 * 1024:
            raise ValueError('Source review must be a bounded regular JSON file.')
        values.extend(dates._read_review_values(path))
    entries, seen = [], set()
    for value in values:
        row = by_id.get(value.get('occurrence_id')) if isinstance(value, dict) else None
        if row is None or row['id'] in seen:
            raise ValueError('Source review requires a distinct known occurrence.')
        seen.add(row['id'])
        clean, digest, unchanged = _validate(value, row, heads.get(row['id']))
        entries.append({'occurrence_id': row['id'], 'review': clean, 'revision_sha256': digest,
                        'unchanged': unchanged})
    return {'schema_version': 1, 'snapshot_sha256': fingerprint, 'entries': entries,
            'summary': {'reviewed_occurrences': len(entries),
                        'new_revisions': sum(not entry['unchanged'] for entry in entries)}}


def apply_repairs(db, plan):
    """Append atomically to a prepared component schema; replay is a no-op."""
    from nannanews import bulletin_date_repair as dates
    from nannanews import bulletin_activities

    db.commit()
    with db:
        db.execute('BEGIN IMMEDIATE')
        current = plan_repairs(db, reviews=[entry['review'] for entry in plan['entries']])
        if all(entry['unchanged'] for entry in current['entries']):
            return 0
        if current['snapshot_sha256'] != plan['snapshot_sha256']:
            raise ValueError('Source repair snapshot changed; create a fresh dry-run plan.')
        canonical = bulletin_activities.activity_map(db)
        changed = 0
        for entry in current['entries']:
            if entry['unchanged']:
                continue
            review, occurrence_id = entry['review'], entry['occurrence_id']
            revision_id = db.execute('''INSERT INTO bulletin_source_revisions
                (occurrence_id,original_url,corrected_url,input_sha256,previous_head_sha256,
                 revision_sha256,origin,actor,reason,review_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (occurrence_id, review['original_url'], review['corrected_url'], review['input_sha256'],
                 review['expected_head_sha256'], entry['revision_sha256'], 'operator_review',
                 review['actor'], review['reason'], _json(review), datetime.now(timezone.utc).isoformat())).lastrowid
            item_id = db.execute('SELECT item_id FROM bulletin_occurrences WHERE id=?', (occurrence_id,)).fetchone()[0]
            members = [key for key, target in canonical.items() if target == canonical[item_id]]
            placeholders = ','.join('?' for _ in members)
            # Reviews may have consumed any same-activity publication as context.
            db.execute('''INSERT INTO bulletin_source_date_invalidations(source_revision_id,date_revision_id)
                SELECT ?,r.id FROM bulletin_date_revisions r JOIN bulletin_occurrences o ON o.id=r.occurrence_id
                WHERE o.item_id IN (''' + placeholders + ')', [revision_id, *members])
            changed += 1
    return changed


def _private_json(path, value):
    with path.open('x', encoding='utf-8') as stream:
        os.chmod(stream.name, 0o600)
        stream.write(_json(value) + '\n')


def _read_database(settings):
    database = Path(settings.database_path).resolve(strict=True)
    db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    return db


def prepare_source(*, occurrence_id, original_url, corrected_url, capture_file, reason, actor, output_dir, settings=None):
    if settings is None:
        from nannanews import runtime
        settings = runtime.settings
    path = Path(capture_file)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError('Capture must be a bounded regular JSON file.')
    capture = json.loads(path.read_text(encoding='utf-8'))
    with closing(_read_database(settings)) as db:
        value = prepare_review(db, occurrence_id=occurrence_id, original_url=original_url,
            corrected_url=corrected_url, capture=capture, reason=reason, actor=actor)
    output = Path(output_dir)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    output.chmod(0o700)
    _private_json(output / 'review.json', [value])
    return {'artifact': str(output / 'review.json'), 'prepared_occurrences': 1, 'applied': False}


def repair_sources(*, review_files=(), apply=False, audit_dir, settings=None):
    if settings is None:
        from nannanews import runtime
        settings = runtime.settings
    with closing(_read_database(settings)) as db:
        plan = plan_repairs(db, review_files=review_files)
    output = Path(audit_dir)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    output.chmod(0o700)
    summary = dict(plan['summary'], applied=False, appended_revisions=0, snapshot_sha256=plan['snapshot_sha256'])
    _private_json(output / 'plan.json', plan)
    _private_json(output / 'summary.json', summary)
    if apply:
        with closing(sqlite3.connect(Path(settings.database_path).resolve(strict=True), timeout=30)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            summary['appended_revisions'] = apply_repairs(db, plan)
        summary['applied'] = True
        _private_json(output / 'application.json', summary)
    return summary
