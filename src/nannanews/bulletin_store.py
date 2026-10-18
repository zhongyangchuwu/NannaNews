"""Private, additive bulletin evidence storage; never writes editorial entries.

One immutable input hash is one document. Every extracted row/section remains an
occurrence even when cautiously linked to a logical item. Imports stage evidence
before their SQLite transaction, and failed attempts remain inspectable.
"""
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

from nannanews.bulletin_common import SCHEMA_VERSION, normalized_text, source_url as canonical_source_url
from nannanews import bulletin_date_repair
from nannanews import runtime
from nannanews import schema

MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_ASSET_BYTES = 32 * 1024 * 1024
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_ITEMS = 10000
BEIJING = timezone(timedelta(hours=8))


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _db():
    connection = runtime.connect_db()
    connection.row_factory = sqlite3.Row
    return connection

def validate_model_configuration():
    """Validate a configured model before a multi-document import starts."""
    settings = runtime.settings
    configured = bool(settings.model_base_url or settings.model_api_key)
    if configured:
        from nannanews import acquisition
        acquisition._model_config()
    return configured


def _migrate_schema(db):
    """Apply legacy schema/backfills only inside the explicit migration."""
    schema.execute_script(db, '''
    CREATE TABLE IF NOT EXISTS bulletin_issues (
        id INTEGER PRIMARY KEY, identity_key TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL, issue_date TEXT, issue_number INTEGER);
    CREATE TABLE IF NOT EXISTS bulletin_documents (
        id INTEGER PRIMARY KEY, issue_id INTEGER NOT NULL REFERENCES bulletin_issues(id),
        source_sha256 TEXT NOT NULL UNIQUE, format TEXT NOT NULL CHECK(format IN ('html','pdf')),
        source_filename TEXT NOT NULL, issue_date_evidence TEXT NOT NULL,
        parser_json TEXT NOT NULL, parsed_json TEXT NOT NULL, text TEXT NOT NULL,
        original_path TEXT NOT NULL, raw_artifact_path TEXT, normalized_path TEXT NOT NULL,
        imported_at TEXT NOT NULL, actor TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS bulletin_document_sources (
        document_id INTEGER NOT NULL REFERENCES bulletin_documents(id),
        source_url TEXT NOT NULL, evidence_json TEXT NOT NULL,
        PRIMARY KEY(document_id,source_url));
    CREATE TABLE IF NOT EXISTS bulletin_document_provenance (
        document_id INTEGER NOT NULL REFERENCES bulletin_documents(id),
        evidence_sha256 TEXT NOT NULL, evidence_json TEXT NOT NULL,
        PRIMARY KEY(document_id,evidence_sha256));
    CREATE TABLE IF NOT EXISTS bulletin_assets (
        document_id INTEGER NOT NULL REFERENCES bulletin_documents(id),
        asset_id TEXT NOT NULL, private_path TEXT NOT NULL, sha256 TEXT NOT NULL,
        evidence_json TEXT NOT NULL, PRIMARY KEY(document_id,asset_id));
    CREATE TABLE IF NOT EXISTS bulletin_items (
        id INTEGER PRIMARY KEY, identity_key TEXT NOT NULL UNIQUE, identity_basis TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS bulletin_occurrences (
        id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES bulletin_items(id),
        document_id INTEGER NOT NULL REFERENCES bulletin_documents(id), local_key TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('summary','detail','notice')), title TEXT NOT NULL,
        category TEXT, body_text TEXT NOT NULL, locator_json TEXT NOT NULL, image_refs_json TEXT NOT NULL,
        raw_item_json TEXT NOT NULL, UNIQUE(document_id,local_key));
    CREATE TABLE IF NOT EXISTS bulletin_dates (
        id INTEGER PRIMARY KEY, occurrence_id INTEGER NOT NULL REFERENCES bulletin_occurrences(id),
        kind TEXT NOT NULL, raw_text TEXT NOT NULL, normalized_date TEXT,
        resolution TEXT NOT NULL, evidence TEXT NOT NULL, locator_json TEXT);
    CREATE TABLE IF NOT EXISTS bulletin_colleges (
        id INTEGER PRIMARY KEY, occurrence_id INTEGER NOT NULL REFERENCES bulletin_occurrences(id),
        name TEXT NOT NULL, relation TEXT NOT NULL CHECK(relation IN ('mentioned','audience')),
        evidence TEXT NOT NULL, locator_json TEXT);
    CREATE TABLE IF NOT EXISTS bulletin_sources (
        occurrence_id INTEGER NOT NULL REFERENCES bulletin_occurrences(id), source_url TEXT NOT NULL,
        PRIMARY KEY(occurrence_id,source_url));
    CREATE TABLE IF NOT EXISTS bulletin_imports (
        id INTEGER PRIMARY KEY, source_sha256 TEXT, source_filename TEXT NOT NULL,
        source_url TEXT, actor TEXT NOT NULL, status TEXT NOT NULL,
        started_at TEXT NOT NULL, finished_at TEXT, document_id INTEGER REFERENCES bulletin_documents(id),
        error_code TEXT, error_message TEXT, metadata_json TEXT NOT NULL DEFAULT '{}');
    CREATE INDEX IF NOT EXISTS bulletin_occurrence_item ON bulletin_occurrences(item_id,document_id);
    CREATE INDEX IF NOT EXISTS bulletin_issue_date ON bulletin_issues(issue_date,id);
    CREATE INDEX IF NOT EXISTS bulletin_date_filter ON bulletin_dates(kind,normalized_date,occurrence_id);
    CREATE INDEX IF NOT EXISTS bulletin_date_occurrence ON bulletin_dates(occurrence_id,kind,resolution,normalized_date);
    CREATE INDEX IF NOT EXISTS bulletin_college_filter ON bulletin_colleges(name,occurrence_id);
    CREATE INDEX IF NOT EXISTS bulletin_source_filter ON bulletin_sources(source_url,occurrence_id);
    CREATE VIEW IF NOT EXISTS bulletin_occurrences_view AS
        SELECT o.*,d.issue_id,d.source_sha256,d.format,d.source_filename,d.original_path,
            d.raw_artifact_path,d.normalized_path,d.imported_at,i.issue_date,i.issue_number,
            i.title AS issue_title FROM bulletin_occurrences o
        JOIN bulletin_documents d ON d.id=o.document_id JOIN bulletin_issues i ON i.id=d.issue_id;
    CREATE VIEW IF NOT EXISTS bulletin_items_view AS
        SELECT * FROM bulletin_occurrences_view o WHERE o.id=(
            SELECT x.id FROM bulletin_occurrences_view x WHERE x.item_id=o.item_id
            ORDER BY coalesce(x.issue_date,'' ) DESC,x.document_id DESC,
                CASE x.kind WHEN 'detail' THEN 0 ELSE 1 END,x.id DESC LIMIT 1);
    CREATE VIEW IF NOT EXISTS bulletin_dates_view AS
        SELECT x.*,o.item_id,o.document_id,o.issue_id,o.issue_date,o.local_key,o.source_sha256
        FROM bulletin_dates x JOIN bulletin_occurrences_view o ON o.id=x.occurrence_id;
    CREATE VIEW IF NOT EXISTS bulletin_colleges_view AS
        SELECT x.*,o.item_id,o.document_id,o.issue_id,o.issue_date,o.local_key,o.source_sha256
        FROM bulletin_colleges x JOIN bulletin_occurrences_view o ON o.id=x.occurrence_id;
    ''')
    # Evidence and raw occurrence rows have no mutable-head or delete path.
    for table in ('bulletin_issues', 'bulletin_documents', 'bulletin_document_sources', 'bulletin_assets',
                  'bulletin_document_provenance', 'bulletin_items', 'bulletin_occurrences',
                  'bulletin_dates', 'bulletin_colleges', 'bulletin_sources'):
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.lower()}
                BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'bulletin evidence is immutable'); END''')
    from nannanews import bulletin_source_repair
    bulletin_source_repair.create_source_schema(db)
    bulletin_date_repair.create_date_schema(db)
    from nannanews import bulletin_activities
    bulletin_activities.create_activity_schema(db)


def _safe_file(root, relative, *, maximum=MAX_ARTIFACT_BYTES):
    if not isinstance(relative, str) or not relative or '\\' in relative or '\x00' in relative:
        raise ValueError('Invalid evidence file path.')
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('Evidence path escapes its private work directory.')
    candidate = root / path
    if any(part.is_symlink() for part in (candidate, *candidate.parents) if part.is_relative_to(root)):
        raise ValueError('Evidence files must not use symlinks.')
    if not candidate.resolve().is_relative_to(root.resolve()) or not candidate.is_file():
        raise ValueError('Evidence file is missing or outside its private work directory.')
    if candidate.stat().st_size > maximum:
        raise ValueError('Evidence file exceeds the bulletin limit.')
    return candidate


def _private_root():
    upload = Path(runtime.settings.upload_dir)
    if upload.is_symlink():
        raise ValueError('Upload directory must not be a symlink.')
    root = upload / 'bulletins'
    if root.is_symlink():
        raise ValueError('Bulletin directory must not be a symlink.')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def _private_path(digest, relative):
    return (Path('bulletins') / digest / relative).as_posix()


def _validate(parsed, digest):
    required = {'schema_version', 'format', 'source_sha256', 'source_filename', 'title', 'issue_date',
                'issue_date_evidence', 'issue_number', 'parser', 'text', 'items', 'pages', 'assets', 'warnings', 'raw_artifact_path'}
    if not isinstance(parsed, dict) or not required <= parsed.keys() or parsed['schema_version'] != SCHEMA_VERSION:
        raise ValueError('Parser returned an incompatible bulletin document.')
    if parsed['source_sha256'] != digest or parsed['format'] not in {'html', 'pdf'}:
        raise ValueError('Parsed evidence does not match the staged original.')
    if not isinstance(parsed['items'], list) or len(parsed['items']) > MAX_ITEMS:
        raise ValueError('Parsed item list exceeds the bulletin limit.')
    if not isinstance(parsed['assets'], list) or len(parsed['assets']) > 1000:
        raise ValueError('Parsed asset list exceeds the bulletin limit.')
    if parsed['issue_date'] is not None:
        _iso(parsed['issue_date'])
    keys = set()
    for item in parsed['items']:
        fields = {'local_key', 'kind', 'title', 'category', 'body_text', 'source_urls', 'locator', 'dates', 'college_mentions', 'image_refs'}
        if not isinstance(item, dict) or not fields <= item.keys() or item['kind'] not in {'summary', 'detail', 'notice'}:
            raise ValueError('Parser returned an incompatible bulletin item.')
        if not isinstance(item['local_key'], str) or not item['local_key'] or item['local_key'] in keys:
            raise ValueError('Parser returned duplicate or invalid item keys.')
        keys.add(item['local_key'])
        for claim in item['dates']:
            if not {'kind','raw_text','normalized_date','resolution','evidence','locator'} <= claim.keys():
                raise ValueError('Parser returned incomplete date evidence.')
            if claim['resolution'] not in {'explicit','year_unknown','invalid','ambiguous'}:
                raise ValueError('Parser returned invalid date resolution.')
            if claim['normalized_date'] is not None:
                _iso(claim['normalized_date'])
                if claim['resolution'] != 'explicit':
                    raise ValueError('Only explicit-year date claims may be normalized.')
        for college in item['college_mentions']:
            if college.get('relation') not in {'audience','mentioned'} or not college.get('name') or not college.get('evidence'):
                raise ValueError('Parser returned unsupported college evidence.')
        for url in item['source_urls']:
            if canonical_source_url(url) != url:
                raise ValueError('Parser returned an invalid source URL.')


def _claims(item):
    return sorted({(d['kind'], normalized_text(d['raw_text'])) for d in item['dates'] if d['kind'] != 'listed'})


def _identity(item, digest):
    title = normalized_text(item['title'])
    urls = sorted(item['source_urls'])
    claims = _claims(item)
    if urls and title and claims:
        basis = 'source_title_date_claim'
        signature = [urls, title, claims]
    elif normalized_text(item['body_text']):
        basis = 'exact_content'
        signature = [title, normalized_text(item['body_text']), urls, claims]
    else:
        basis = 'document_local'
        signature = [digest, item['local_key']]
    return basis + ':' + hashlib.sha256(_json(signature).encode()).hexdigest(), basis


def _within_document_link(item, previous):
    candidates = set()
    for other, item_id in previous:
        if item['kind'] == other['kind'] or normalized_text(item['title']) != normalized_text(other['title']):
            continue
        internal = item['locator'].get('id') in other['locator'].get('internal_targets', [])
        internal |= other['locator'].get('id') in item['locator'].get('internal_targets', [])
        shared = bool(set(item['source_urls']) & set(other['source_urls']))
        if not (internal or shared):
            continue
        left = {normalized_text(d['raw_text']) for d in item['dates'] if d['kind'] != 'listed'}
        right = {normalized_text(d['raw_text']) for d in other['dates'] if d['kind'] != 'listed'}
        normalized_left = {d['normalized_date'] for d in item['dates'] if d['kind'] != 'listed' and d['normalized_date']}
        normalized_right = {d['normalized_date'] for d in other['dates'] if d['kind'] != 'listed' and d['normalized_date']}
        if not left or not right or left & right or normalized_left & normalized_right:
            candidates.add(item_id)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _save_provenance(db, document_id, provenance):
    if provenance:
        evidence = _json(provenance)
        db.execute('INSERT OR IGNORE INTO bulletin_document_provenance VALUES(?,?,?)',
                   (document_id,hashlib.sha256(evidence.encode()).hexdigest(),evidence))


def _import_result(db, document_id, *, created, warnings=None):
    document = db.execute('SELECT * FROM bulletin_documents WHERE id=?', (document_id,)).fetchone()
    if not created:
        upload = Path(runtime.settings.upload_dir)
        original = _safe_file(upload, document['original_path'], maximum=MAX_DOCUMENT_BYTES)
        if _digest(original) != document['source_sha256']:
            raise ValueError('Stored original evidence hash no longer matches its document.')
        _safe_file(upload, document['normalized_path'])
        if document['raw_artifact_path']:
            _safe_file(upload, document['raw_artifact_path'])
        for asset in db.execute('SELECT private_path,sha256 FROM bulletin_assets WHERE document_id=?',(document_id,)):
            saved = _safe_file(upload, asset['private_path'], maximum=MAX_ASSET_BYTES)
            if _digest(saved) != asset['sha256']:
                raise ValueError('Stored asset evidence hash no longer matches its file.')
    counts = db.execute('SELECT count(DISTINCT item_id),count(*) FROM bulletin_occurrences WHERE document_id=?', (document_id,)).fetchone()
    return {'status': 'created' if created else 'already_imported', 'created': created, 'document_id': document_id,
            'issue_id': document['issue_id'], 'item_count': counts[0], 'occurrence_count': counts[1],
            'warnings': warnings if warnings is not None else json.loads(document['parsed_json'])['warnings']}


def import_document(path, *, source_url=None, actor='system:bulletin-import'):
    return _import_document(path, source_url=source_url, actor=actor)


def _import_document(path, *, source_url=None, actor='system:bulletin-import', provenance=None):
    """Import one local HTML/PDF, retaining failure state without partial domain rows."""
    schema.require_schema()
    path = Path(path)
    url = canonical_source_url(source_url)
    if source_url and url is None:
        raise ValueError('Document source URL must be a public HTTP(S) URL.')
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
        raise ValueError('Import actor is invalid.')
    with closing(_db()) as db, db:
        attempt_id = db.execute('''INSERT INTO bulletin_imports(source_filename,source_url,actor,status,started_at)
            VALUES(?,?,?,'running',?)''', (path.name, url, actor, _now())).lastrowid
    stage, final, digest = None, None, None
    moved = False
    try:
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in {'.html', '.htm', '.pdf'}:
            raise ValueError('Input must be a regular local HTML or PDF file.')
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise ValueError('Input exceeds the offline bulletin document limit.')
        root = _private_root()
        stage = Path(tempfile.mkdtemp(prefix='.import-', dir=root))
        original_name = 'original.pdf' if path.suffix.lower() == '.pdf' else 'original.html'
        shutil.copyfile(path, stage / original_name)
        if (stage / original_name).stat().st_size > MAX_DOCUMENT_BYTES:
            raise ValueError('Input exceeds the offline bulletin document limit.')
        digest = _digest(stage / original_name)
        with closing(_db()) as db, db:
            db.execute('UPDATE bulletin_imports SET source_sha256=? WHERE id=?', (digest, attempt_id))
            existing = db.execute('SELECT id FROM bulletin_documents WHERE source_sha256=?', (digest,)).fetchone()
            if existing:
                if url:
                    db.execute('INSERT OR IGNORE INTO bulletin_document_sources VALUES(?,?,?)',
                               (existing['id'], url, _json({'source_url': url, 'actor': actor})))
                _save_provenance(db, existing['id'], provenance)
                db.execute("UPDATE bulletin_imports SET status='already_imported',finished_at=?,document_id=? WHERE id=?",
                           (_now(), existing['id'], attempt_id))
                # No reparsing or fake cached parser result; original provenance must exist.
                return _import_result(db, existing['id'], created=False)
        if original_name.endswith('.pdf'):
            from nannanews.pdf_adapter import parse_pdf
            parsed = parse_pdf(stage / original_name, stage,
                               parser_binary=runtime.settings.pdf_parser_binary)
        else:
            from nannanews.html_bulletin_adapter import parse_html
            parsed = parse_html(path, source_url=url)
            for asset in parsed['assets']:
                source = _safe_file(path.parent.resolve(), asset['path'], maximum=MAX_ASSET_BYTES)
                destination = stage / asset['path']
                if asset['path'] in {original_name, 'normalized.json', 'rawparser.json', 'provenance.json'}:
                    raise ValueError('HTML asset conflicts with a reserved evidence path.')
                if destination.exists():
                    if _digest(_safe_file(stage, asset['path'], maximum=MAX_ASSET_BYTES)) != asset['sha256']:
                        raise ValueError('Repeated HTML asset references disagree on their evidence hash.')
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(source, destination)
        parsed['source_filename'] = path.name
        if url:
            parsed['source_url'] = url
        if provenance:
            parsed['claimed_issue_date'] = provenance.get('claimed_issue_date')
            if parsed['claimed_issue_date'] != parsed['issue_date']:
                parsed['warnings'].append({'code':'claimed_issue_date_mismatch',
                    'message':'Archive date differs from the actual issue heading.',
                    'claimed_issue_date':parsed['claimed_issue_date'],'issue_date':parsed['issue_date']})
        _validate(parsed, digest)
        original = _safe_file(stage, original_name, maximum=MAX_DOCUMENT_BYTES)
        if _digest(original) != digest:
            raise ValueError('Parser changed the staged original.')
        artifact = parsed['raw_artifact_path']
        if artifact:
            _safe_file(stage, artifact)
        asset_ids = set()
        for asset in parsed['assets']:
            if not asset.get('id') or asset['id'] in asset_ids:
                raise ValueError('Parser returned duplicate asset identities.')
            asset_ids.add(asset['id'])
            saved = _safe_file(stage, asset['path'], maximum=MAX_ASSET_BYTES)
            if _digest(saved) != asset.get('sha256'):
                raise ValueError('Asset evidence hash does not match its file.')
        normalized = stage / 'normalized.json'
        if normalized.exists():
            raise ValueError('Parser overwrote a reserved normalized evidence file.')
        normalized.write_text(_json(parsed), encoding='utf-8')
        _safe_file(stage, 'normalized.json')
        if provenance:
            evidence_file = stage / 'provenance.json'
            if evidence_file.exists():
                raise ValueError('Parser overwrote a reserved provenance evidence file.')
            evidence_file.write_text(_json(provenance), encoding='utf-8')
            _safe_file(stage, 'provenance.json')
        staged_size = 0
        for entry in stage.rglob('*'):
            if entry.is_symlink():
                raise ValueError('Parser evidence contains a symlink.')
            entry.chmod(0o700 if entry.is_dir() else 0o600)
            if entry.is_file():
                staged_size += entry.stat().st_size
                if staged_size > 256 * 1024 * 1024:
                    raise ValueError('Staged bulletin evidence exceeds the total size limit.')
        stage.chmod(0o700)
        model_configured = bool(runtime.settings.model_base_url or runtime.settings.model_api_key)
        source_cache = {}
        date_decisions = []
        for item in parsed['items']:
            related = [{'title': other['title'], 'body_text': other['body_text'],
                        'source_urls': other['source_urls'], 'issue_date': parsed['issue_date'],
                        'local_key': other['local_key']}
                       for other in parsed['items'] if other is not item
                       and normalized_text(other['title']) == normalized_text(item['title'])
                       and set(other['source_urls']) & set(item['source_urls'])]
            date_input = {'source_sha256': digest, 'local_key': item['local_key'],
                'title': item['title'], 'body_text': item['body_text'], 'issue_date': parsed['issue_date'],
                'locator_json': _json(item['locator']), 'source_urls': sorted(set(item['source_urls'])),
                'related_occurrences': related}
            if model_configured:
                decision = bulletin_date_repair.model_decision(date_input, item['dates'], source_cache=source_cache)
            else:
                decision = bulletin_date_repair.unreviewed_decision(
                    'AI date review was not run because no model API URL or key is configured.')
            date_decisions.append(decision)
        final = root / digest
        with closing(_db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT id FROM bulletin_documents WHERE source_sha256=?', (digest,)).fetchone()
            if existing:
                if url:
                    db.execute('INSERT OR IGNORE INTO bulletin_document_sources VALUES(?,?,?)',
                               (existing['id'], url, _json({'source_url': url, 'actor': actor})))
                _save_provenance(db, existing['id'], provenance)
                db.execute("UPDATE bulletin_imports SET status='already_imported',finished_at=?,document_id=? WHERE id=?",
                           (_now(), existing['id'], attempt_id))
                return _import_result(db, existing['id'], created=False)
            if final.exists() or final.is_symlink():
                raise ValueError('Unregistered evidence directory already exists; recovery requires inspection.')
            issue_key = _json([parsed['issue_date'], parsed['issue_number'], normalized_text(parsed['title'])]) if parsed['issue_date'] else 'document:' + digest
            db.execute('INSERT OR IGNORE INTO bulletin_issues(identity_key,title,issue_date,issue_number) VALUES(?,?,?,?)',
                       (issue_key, parsed['title'], parsed['issue_date'], parsed['issue_number']))
            issue_id = db.execute('SELECT id FROM bulletin_issues WHERE identity_key=?', (issue_key,)).fetchone()['id']
            document_id = db.execute('''INSERT INTO bulletin_documents(issue_id,source_sha256,format,source_filename,
                issue_date_evidence,parser_json,parsed_json,text,original_path,raw_artifact_path,normalized_path,imported_at,actor)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (issue_id,digest,parsed['format'],path.name,parsed['issue_date_evidence'],_json(parsed['parser']),_json(parsed),
                 parsed['text'],_private_path(digest,original_name),_private_path(digest,artifact) if artifact else None,
                 _private_path(digest,'normalized.json'),_now(),actor)).lastrowid
            if url:
                db.execute('INSERT INTO bulletin_document_sources VALUES(?,?,?)', (document_id,url,_json({'source_url':url,'actor':actor})))
            _save_provenance(db, document_id, provenance)
            for asset in parsed['assets']:
                db.execute('INSERT INTO bulletin_assets VALUES(?,?,?,?,?)',
                           (document_id,asset['id'],_private_path(digest,asset['path']),asset['sha256'],_json(asset)))
            previous = []
            for item, decision in zip(parsed['items'], date_decisions):
                key, basis = _identity(item, digest)
                item_id = _within_document_link(item, previous)
                if item_id is None:
                    db.execute('INSERT OR IGNORE INTO bulletin_items(identity_key,identity_basis) VALUES(?,?)', (key,basis))
                    item_id = db.execute('SELECT id FROM bulletin_items WHERE identity_key=?', (key,)).fetchone()['id']
                occurrence_id = db.execute('''INSERT INTO bulletin_occurrences(item_id,document_id,local_key,kind,title,
                    category,body_text,locator_json,image_refs_json,raw_item_json) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (item_id,document_id,item['local_key'],item['kind'],item['title'],item['category'],item['body_text'],
                     _json(item['locator']),_json(item['image_refs']),_json(item))).lastrowid
                for claim in item['dates']:
                    db.execute('''INSERT INTO bulletin_dates(occurrence_id,kind,raw_text,normalized_date,resolution,evidence,locator_json)
                        VALUES(?,?,?,?,?,?,?)''', (occurrence_id,claim['kind'],claim['raw_text'],claim['normalized_date'],claim['resolution'],claim['evidence'],_json(claim['locator'])))
                date_input = {'id': occurrence_id, 'source_sha256': digest, 'local_key': item['local_key'],
                    'title': item['title'], 'body_text': item['body_text'], 'issue_date': parsed['issue_date'],
                    'locator_json': _json(item['locator']), 'source_urls': sorted(set(item['source_urls']))}
                bulletin_date_repair.record_revision(db, date_input, item['dates'], **decision)
                for college in item['college_mentions']:
                    db.execute('INSERT INTO bulletin_colleges(occurrence_id,name,relation,evidence,locator_json) VALUES(?,?,?,?,?)',
                               (occurrence_id,college['name'],college['relation'],college['evidence'],_json(college.get('locator'))))
                for source in set(item['source_urls']):
                    db.execute('INSERT INTO bulletin_sources VALUES(?,?)', (occurrence_id,source))
                previous.append((item,item_id))
            os.rename(stage, final)
            moved = True
            db.execute("UPDATE bulletin_imports SET status='created',finished_at=?,document_id=? WHERE id=?",
                       (_now(),document_id,attempt_id))
            result = _import_result(db, document_id, created=True, warnings=parsed['warnings'])
        return result
    except Exception as error:
        if moved and final is not None:
            # Only this attempt's newly renamed directory is eligible for rollback.
            shutil.rmtree(final)
        code = getattr(error, 'code', 'bulletin_import_failed')
        message = str(error) if isinstance(error, ValueError) else 'Local bulletin import failed; inspect import state and input evidence.'
        with closing(_db()) as db, db:
            db.execute("UPDATE bulletin_imports SET status='failed',finished_at=?,error_code=?,error_message=? WHERE id=?",
                       (_now(),code,message,attempt_id))
        return {'status':'failed','created':False,'document_id':None,'issue_id':None,'item_count':0,'occurrence_count':0,
                'warnings':[], 'error':{'code':code,'message':message},'import_id':attempt_id}
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)


def _iso(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('Date filters require YYYY-MM-DD.')
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError('Date filters require a valid calendar date.') from None


def _values(value):
    if value in (None, ''):
        return []
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list,tuple)) or len(values) > 30 or any(not isinstance(v,str) or not v or len(v)>8192 for v in values):
        raise ValueError('Filter values must be a bounded string or string list.')
    return list(dict.fromkeys(values))


def _filters(filters):
    filters = dict(filters or {})
    allowed = {'q','issue_date_from','issue_date_to','category','college','date_type','date_from','date_to','source','date_status','freshness','as_of'}
    if set(filters) - allowed:
        raise ValueError('Unsupported bulletin filter.')
    q = filters.get('q') or ''
    if not isinstance(q,str) or len(q)>200:
        raise ValueError('Keyword filter must contain at most 200 characters.')
    filters['q'] = q.strip()
    for key in ('issue_date_from','issue_date_to','date_from','date_to','as_of'):
        if filters.get(key):
            filters[key] = _iso(filters[key])
    for start,end in (('issue_date_from','issue_date_to'),('date_from','date_to')):
        if filters.get(start) and filters.get(end) and filters[start]>filters[end]:
            raise ValueError('Date filter start must not follow its end.')
    filters['as_of'] = filters.get('as_of') or datetime.now(BEIJING).date().isoformat()
    filters['date_type'] = filters.get('date_type') or 'event'
    if filters['date_type'] not in {'event','deadline','listed','source'}:
        raise ValueError('Unsupported date type.')
    if filters.get('date_status') not in {None,'known','unknown'} or filters.get('freshness') not in {None,'not_expired','expired','unknown'}:
        raise ValueError('Unsupported date status or freshness.')
    for key in ('category','college','source'):
        filters[key] = _values(filters.get(key))
    return filters


EXPIRY_JOIN = '''LEFT JOIN (
    SELECT x.item_id,x.document_id,
        coalesce(max(CASE WHEN d.kind='deadline' THEN d.normalized_date END),
                 max(CASE WHEN d.kind='event' THEN d.normalized_date END)) AS expiry_date
    FROM bulletin_effective_dates d JOIN bulletin_occurrences x ON x.id=d.occurrence_id
    LEFT JOIN bulletin_date_revisions r ON r.id=(SELECT max(h.id) FROM bulletin_date_revisions h
        WHERE h.occurrence_id=x.id AND h.origin='luna_review')
        AND NOT EXISTS (SELECT 1 FROM bulletin_source_date_invalidations s WHERE s.date_revision_id=r.id)
    WHERE d.resolution IN ('explicit','contextual') AND d.normalized_date IS NOT NULL AND d.kind IN ('deadline','event')
        AND (r.status IS NULL OR r.status NOT IN ('recurring_no_end','uncertain','invalid_source'))
    GROUP BY x.item_id,x.document_id
) expiry ON expiry.item_id=o.item_id AND expiry.document_id=o.document_id'''


def _where(filters):
    clauses, params = [], []
    if filters['q']:
        escaped = filters['q'].replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        clauses.append("(o.title LIKE ? ESCAPE '\\' OR o.body_text LIKE ? ESCAPE '\\')")
        params.extend(['%'+escaped+'%']*2)
    for field,start,end in (('issue_date','issue_date_from','issue_date_to'),):
        for key,op in ((start,'>='),(end,'<=')):
            if filters.get(key):
                clauses.append('o.'+field+op+'?')
                params.append(filters[key])
    if filters['category']:
        clauses.append('o.category IN ('+','.join('?'*len(filters['category']))+')')
        params.extend(filters['category'])
    for key,table,field in (('college','bulletin_colleges','name'),('source','bulletin_effective_sources','source_url')):
        if filters[key]:
            # A detail and its explicitly linked summary share document-level evidence.
            placeholders = ','.join('?'*len(filters[key]))
            clause = f'''EXISTS(SELECT 1 FROM {table} f JOIN bulletin_occurrences x ON x.id=f.occurrence_id
                WHERE x.item_id=o.item_id AND x.document_id=o.document_id AND f.{field} IN ({placeholders}))'''
            params.extend(filters[key])
            if key == 'source':
                clause = '(' + clause + f''' OR EXISTS(SELECT 1 FROM bulletin_document_sources f
                    WHERE f.document_id=o.document_id AND f.source_url IN ({placeholders})
                    AND NOT EXISTS (SELECT 1 FROM bulletin_source_revisions r
                        JOIN bulletin_occurrences x ON x.id=r.occurrence_id
                        WHERE x.item_id=o.item_id AND x.document_id=o.document_id
                            AND r.original_url=f.source_url)))'''
                params.extend(filters[key])
            clauses.append(clause)
    date_parts = ["d.kind=?", "d.resolution IN ('explicit','contextual')", 'd.normalized_date IS NOT NULL']
    date_params = [filters['date_type']]
    for key,op in (('date_from','>='),('date_to','<=')):
        if filters.get(key):
            date_parts.append('d.normalized_date'+op+'?')
            date_params.append(filters[key])
    if filters.get('date_from') or filters.get('date_to') or filters.get('date_status') == 'known':
        exists = '''EXISTS(SELECT 1 FROM bulletin_effective_dates d JOIN bulletin_occurrences x ON x.id=d.occurrence_id
            WHERE x.item_id=o.item_id AND x.document_id=o.document_id AND '''+' AND '.join(date_parts)+')'
        clauses.append(exists)
        params.extend(date_params)
    if filters.get('date_status') == 'unknown':
        clauses.append('''NOT EXISTS(SELECT 1 FROM bulletin_effective_dates d JOIN bulletin_occurrences x ON x.id=d.occurrence_id
            WHERE x.item_id=o.item_id AND x.document_id=o.document_id AND d.kind=?
                AND d.resolution IN ('explicit','contextual') AND d.normalized_date IS NOT NULL)''')
        params.append(filters['date_type'])
    freshness = filters.get('freshness')
    if freshness:
        clauses.append('expiry.expiry_date' + (' IS NULL' if freshness=='unknown' else '>=?' if freshness=='not_expired' else '<?'))
        if freshness != 'unknown':
            params.append(filters['as_of'])
    return ' AND '.join(clauses) or '1=1', params


def _activity_join(db):
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bulletin_activity_groups'").fetchone()
    if not exists:
        return '', 'o.item_id'
    return '''LEFT JOIN (
        SELECT m.item_id,g.canonical_item_id FROM bulletin_activity_members m
        JOIN bulletin_activity_groups g ON g.id=m.group_id
        JOIN (SELECT item_id,max(group_id) group_id FROM bulletin_activity_members GROUP BY item_id) h
          ON h.item_id=m.item_id AND h.group_id=m.group_id
    ) activity ON activity.item_id=o.item_id ''', 'coalesce(activity.canonical_item_id,o.item_id)'


def _occurrences(db, rows, canonical_ids):
    rows = list(rows)
    if not rows:
        return []
    ids = [row['id'] for row in rows]
    placeholders = ','.join('?' * len(ids))
    dates, colleges, sources = {}, {}, {}
    for row in db.execute('SELECT * FROM bulletin_effective_dates WHERE occurrence_id IN ('+placeholders+') ORDER BY id', ids):
        value = dict(row)
        value['locator'] = json.loads(value.pop('locator_json') or 'null')
        dates.setdefault(value['occurrence_id'], []).append(value)
    for row in db.execute('SELECT * FROM bulletin_colleges WHERE occurrence_id IN ('+placeholders+') ORDER BY id', ids):
        value = dict(row)
        value['locator'] = json.loads(value.pop('locator_json'))
        colleges.setdefault(value['occurrence_id'], []).append(value)
    from nannanews import bulletin_source_repair
    for row in db.execute('SELECT * FROM ' + bulletin_source_repair.sources_table(db)
                          + ' WHERE occurrence_id IN ('+placeholders+') ORDER BY source_url', ids):
        sources.setdefault(row['occurrence_id'], []).append(row['source_url'])
    corrections = bulletin_source_repair.public_corrections(db)
    reviews = {row['occurrence_id']: {'status': row['status'], 'reason': row['reason'],
        'rules_version': (row['rules_version'] if row['rules_version'] == bulletin_date_repair.RULES_VERSION
                          else 'legacy-content-evidence'),
        'source_review': bulletin_date_repair.public_source_review(row['source_review_json'],
            rules_version=row['rules_version'])}
        for row in db.execute('SELECT * FROM (' + bulletin_date_repair.current_revisions_sql(db)
            + ') WHERE occurrence_id IN (' + placeholders + ')', ids)}
    result = []
    for row in rows:
        value = dict(row)
        occurrence_id = value['id']
        raw_item_id = value['item_id']
        canonical_id = canonical_ids.get(raw_item_id, raw_item_id)
        value['occurrence_id'] = occurrence_id
        value['raw_item_id'] = raw_item_id
        value['item_id'] = canonical_id
        value['id'] = canonical_id
        value.pop('canonical_item_id', None)
        value['locator'] = json.loads(value.pop('locator_json'))
        value['image_refs'] = json.loads(value.pop('image_refs_json'))
        value['raw_item'] = json.loads(value.pop('raw_item_json'))
        value['dates'] = dates.get(occurrence_id, [])
        value['date_review'] = reviews.get(occurrence_id) or {
            'status': 'unreviewed',
            'reason': 'Parser date claims, if any, are literal evidence only and have not received an AI review.',
            'source_review': bulletin_date_repair.public_source_review(None)}
        value['college_mentions'] = colleges.get(occurrence_id, [])
        value['source_urls'] = sources.get(occurrence_id, [])
        if corrections[occurrence_id]:
            value['source_corrections'] = corrections[occurrence_id]
        result.append(value)
    return result


def query_items(filters=None, *, page=1, per_page=20):
    """Collapse publication records by AI-reviewed canonical activity identity."""
    filters = _filters(filters)
    if isinstance(page, bool) or not isinstance(page, int) or page < 1 or page > 1000000:
        raise ValueError('Page must be between 1 and 1000000.')
    if isinstance(per_page, bool) or not isinstance(per_page, int) or not 1 <= per_page <= 100:
        raise ValueError('Page size must be between 1 and 100.')
    where, params = _where(filters)
    with closing(_db()) as db:
        from nannanews import bulletin_activities
        canonical_ids = bulletin_activities.activity_map(db)
        activity_join, canonical_expression = _activity_join(db)
        cte = '''WITH matched AS (SELECT o.*,expiry.expiry_date,
            ''' + canonical_expression + ''' AS canonical_item_id,
            row_number() OVER(PARTITION BY ''' + canonical_expression + ''' ORDER BY
                coalesce(o.issue_date,'') DESC,o.document_id DESC,
                CASE o.kind WHEN 'detail' THEN 0 ELSE 1 END,o.id DESC) AS selection_rank
            FROM bulletin_occurrences_view o ''' + activity_join + EXPIRY_JOIN + ' WHERE ' + where + ') '
        total = db.execute(cte + 'SELECT count(*) FROM matched WHERE selection_rank=1', params).fetchone()[0]
        rows = db.execute(cte + '''SELECT * FROM matched WHERE selection_rank=1
            ORDER BY coalesce(issue_date,'') DESC,document_id DESC,canonical_item_id DESC LIMIT ? OFFSET ?''',
            [*params, per_page, (page - 1) * per_page]).fetchall()
        canonical_page = sorted({row['canonical_item_id'] for row in rows})
        counts = {}
        if canonical_page:
            count_params = ','.join('?' * len(canonical_page))
            for row in db.execute('''SELECT ''' + canonical_expression + ''' AS canonical_item_id,count(*) AS occurrence_count
                FROM bulletin_occurrences_view o ''' + activity_join + ''' WHERE ''' + canonical_expression +
                ' IN (' + count_params + ') GROUP BY ' + canonical_expression, canonical_page):
                counts[row['canonical_item_id']] = row['occurrence_count']
        records = _occurrences(db, rows, canonical_ids)
        for record in records:
            record.pop('selection_rank', None)
            canonical_id = record.pop('canonical_item_id', record['id'])
            expiry = record['expiry_date']
            record['freshness'] = 'unknown' if expiry is None else 'expired' if expiry < filters['as_of'] else 'not_expired'
            record['occurrence_count'] = counts.get(canonical_id, 0)
    return {'records': records, 'total': total, 'pages': math.ceil(total / per_page),
            'page': page, 'per_page': per_page, 'filters': filters}


def get_item(item_id):
    with closing(_db()) as db:
        from nannanews import bulletin_activities
        canonical_ids = bulletin_activities.activity_map(db)
        canonical_id = canonical_ids.get(item_id)
        if canonical_id is None:
            return None
        item = db.execute('SELECT * FROM bulletin_items WHERE id=?', (canonical_id,)).fetchone()
        if item is None:
            return None
        activity_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bulletin_activity_groups'").fetchone()
        if activity_table:
            predicate = '''(o.item_id=? OR o.item_id IN (
                SELECT m.item_id FROM bulletin_activity_members m
                JOIN bulletin_activity_groups g ON g.id=m.group_id
                WHERE g.canonical_item_id=? AND m.group_id=(
                    SELECT max(latest.group_id) FROM bulletin_activity_members latest WHERE latest.item_id=m.item_id)))'''
            params = (canonical_id, canonical_id)
        else:
            predicate, params = 'o.item_id=?', (canonical_id,)
        rows = db.execute('''SELECT o.* FROM bulletin_occurrences_view o WHERE ''' + predicate +
            ''' ORDER BY coalesce(o.issue_date,'') DESC,o.document_id DESC,
                CASE o.kind WHEN 'detail' THEN 0 ELSE 1 END,o.id DESC''', params).fetchall()
        occurrences = _occurrences(db, rows, canonical_ids)
        result = dict(occurrences[0])
        raw_item_ids = sorted(raw_id for raw_id, target in canonical_ids.items() if target == canonical_id)
        result.update(identity_basis=item['identity_basis'], occurrence_count=len(occurrences),
                      occurrences=occurrences, raw_item_ids=raw_item_ids)
        result['dates'] = [dict(claim, document_id=occurrence['document_id'], issue_id=occurrence['issue_id'],
            issue_date=occurrence['issue_date'], source_sha256=occurrence['source_sha256'])
            for occurrence in occurrences for claim in occurrence['dates']]
        result['college_mentions'] = [dict(claim, document_id=occurrence['document_id'], issue_id=occurrence['issue_id'],
            issue_date=occurrence['issue_date'], source_sha256=occurrence['source_sha256'])
            for occurrence in occurrences for claim in occurrence['college_mentions']]
        return result

def list_issues(filters=None):
    filters = _filters(filters)
    where, params = _where(filters)
    if not any(filters.get(k) for k in ('q','category','college','source','date_from','date_to','date_status','freshness')):
        clauses,params = [],[]
        for key,op in (('issue_date_from','>='),('issue_date_to','<=')):
            if filters.get(key):
                clauses.append('i.issue_date'+op+'?')
                params.append(filters[key])
        where = ' AND '.join(clauses) or '1=1'
    else:
        join = EXPIRY_JOIN if filters.get('freshness') else ''
        where = 'EXISTS(SELECT 1 FROM bulletin_occurrences_view o '+join+' WHERE o.issue_id=i.id AND '+where+')'
    with closing(_db()) as db:
        return [dict(row) for row in db.execute('''SELECT i.id,i.title,i.issue_date,i.issue_number,
            (SELECT count(*) FROM bulletin_documents d WHERE d.issue_id=i.id) AS document_count,
            (SELECT count(*) FROM bulletin_occurrences_view o WHERE o.issue_id=i.id) AS occurrence_count
            FROM bulletin_issues i WHERE '''+where+' ORDER BY coalesce(i.issue_date,\'\') DESC,i.id DESC LIMIT 1000',params)]


def get_document(document_id):
    with closing(_db()) as db:
        row = db.execute('SELECT * FROM bulletin_documents WHERE id=?',(document_id,)).fetchone()
        if row is None:
            return None
        parsed = json.loads(row['parsed_json'])
        parsed.update(id=row['id'],document_id=row['id'],issue_id=row['issue_id'],original_path=row['original_path'],
                      raw_artifact_path=row['raw_artifact_path'],normalized_path=row['normalized_path'],
                      imported_at=row['imported_at'],actor=row['actor'])
        parsed['source_urls'] = [r[0] for r in db.execute('SELECT source_url FROM bulletin_document_sources WHERE document_id=? ORDER BY source_url',(document_id,))]
        parsed['provenance'] = [json.loads(r[0]) for r in db.execute(
            'SELECT evidence_json FROM bulletin_document_provenance WHERE document_id=? ORDER BY evidence_sha256',(document_id,))]
        parsed['assets'] = [dict(json.loads(r['evidence_json']),private_path=r['private_path'],document_id=document_id)
                            for r in db.execute('SELECT * FROM bulletin_assets WHERE document_id=? ORDER BY asset_id',(document_id,))]
        return parsed


def import_archive(archive_root, *, actor='system:bulletin-import'):
    """Import dated HTML representations listed in the actual index and manifest.

    Aliases and legacy PDFs are deliberately never discovered by a filesystem glob.
    Index date is acquisition metadata, never a fallback publication/event year.
    """
    validate_model_configuration()
    schema.require_schema()
    root = Path(archive_root).resolve()
    index_file = _safe_file(root,'issue-index.json',maximum=16*1024*1024)
    manifest_file = _safe_file(root,'manifest.json',maximum=32*1024*1024)
    index = json.loads(index_file.read_text(encoding='utf-8'))
    manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
    issues = index.get('dated_issues')
    files = manifest.get('files')
    if not isinstance(issues,list) or len(issues)>5000 or not isinstance(files,dict):
        raise ValueError('Archive index or manifest has an unsupported shape.')
    results, seen = [],set()
    for issue in issues:
        representations = issue.get('representations',{}).get('html',[])
        for representation in representations:
            relative = representation.get('relative_path')
            if relative in seen:
                continue
            seen.add(relative)
            source = representation.get('url')
            claimed = issue.get('date')
            metadata = {'claimed_issue_date':claimed,'archive_relative_path':relative,'representation':representation,
                        'manifest':files.get(source)}
            try:
                if not isinstance(relative,str) or not re.fullmatch(r'site/\d{4}-\d{2}-\d{2}\.html',relative):
                    raise ValueError('Archive representation is not a dated HTML input.')
                saved = _safe_file(root,relative,maximum=MAX_DOCUMENT_BYTES)
                entry = files.get(source)
                if representation.get('outcome')!='success' or not isinstance(entry,dict) or entry.get('outcome')!='success':
                    raise ValueError('Archive representation is not a successful manifest capture.')
                digest = _digest(saved)
                if digest!=representation.get('sha256') or digest!=entry.get('sha256') or entry.get('relative_path')!=relative:
                    raise ValueError('Archive HTML hash or path disagrees with its index/manifest.')
                result = _import_document(saved,source_url=source,actor=actor,provenance=metadata)
                with closing(_db()) as db,db:
                    db.execute('UPDATE bulletin_imports SET metadata_json=? WHERE id=(SELECT max(id) FROM bulletin_imports WHERE source_sha256=? AND source_url=?)',
                               (_json(metadata),digest,source))
            except (ValueError,OSError) as error:
                message = str(error) if isinstance(error,ValueError) else 'Archive HTML input is unavailable.'
                with closing(_db()) as db,db:
                    attempt = db.execute('''INSERT INTO bulletin_imports(source_filename,source_url,actor,status,started_at,finished_at,
                        error_code,error_message,metadata_json) VALUES(?,?,?,'failed',?,?,'archive_input_invalid',?,?)''',
                        (Path(relative).name if isinstance(relative,str) else '',source,actor,_now(),_now(),message,_json(metadata))).lastrowid
                result = {'status':'failed','created':False,'document_id':None,'issue_id':None,'item_count':0,'occurrence_count':0,
                          'warnings':[],'error':{'code':'archive_input_invalid','message':message},'import_id':attempt}
            results.append(dict(result,relative_path=relative,claimed_issue_date=claimed))
    return {'inputs':len(results),'created':sum(r['created'] for r in results),
            'already_imported':sum(r['status']=='already_imported' for r in results),
            'failed':sum(r['status']=='failed' for r in results),'results':results}
