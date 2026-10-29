"""Independent administration. Run `nannanews --help` for commands."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import getpass
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import zipfile

from nannanews import runtime, schema
from nannanews.bulletin_historical_attempts import AttemptStore, ledger_path, validate_schema as validate_attempt_schema

HISTORICAL_AUDIT_NAME = AttemptStore(Path('database.db')).audit_directory.name
MODEL_AUDIT_PATHS_NAME = 'model-audit-paths.json'
BACKUP_DIRS = ('uploads', 'publications', 'archives', 'lecture_archives', 'model-audit', HISTORICAL_AUDIT_NAME)
BACKUP_FILES = ('source_catalog.json', 'automation_sources.json', MODEL_AUDIT_PATHS_NAME)
HISTORICAL_LEDGER_NAME = ledger_path(Path('database.db')).name


def _model_audit_roots():
    return {'model-audit': runtime.settings.data_dir / 'model-audit',
            HISTORICAL_AUDIT_NAME: AttemptStore(runtime.settings.database_path).audit_directory}


def _preserve_current_audits(staging, target, manifest):
    """Carry newer private wire evidence with the protective paid ledger."""
    roots = _model_audit_roots()
    mappings = {}
    for path in (staging / MODEL_AUDIT_PATHS_NAME, runtime.settings.data_dir / MODEL_AUDIT_PATHS_NAME):
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError('Invalid private model audit path mapping')
        saved = json.loads(path.read_text(encoding='utf-8'))
        if saved.get('format') != 'nanna-model-audit-paths-v1' or not isinstance(saved.get('roots'), list):
            raise ValueError('Unsupported private model audit path mapping')
        for row in saved['roots']:
            if (not isinstance(row, dict) or row.get('archive_root') not in roots
                    or not isinstance(row.get('original_root'), str)):
                raise ValueError('Invalid private model audit root mapping')
            mappings[(row['archive_root'], row['original_root'])] = None
    previous_roots = manifest.get('model_audit_roots', {})
    if (not isinstance(previous_roots, dict)
            or any(name not in roots or not isinstance(path, str) for name, path in previous_roots.items())):
        raise ValueError('Invalid backed-up model audit roots')
    for name, path in previous_roots.items():
        mappings[(name, path)] = None
    for name, directory in roots.items():
        if directory.is_symlink():
            raise ValueError('Restore refuses symlinked model audit directories')
        for source in sorted(directory.rglob('*')):
            if source.is_symlink():
                raise ValueError('Restore refuses symlinked model audit files')
            if not source.is_file():
                continue
            destination = staging / name / source.relative_to(directory)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if destination.exists():
                with source.open('rb') as current, destination.open('rb') as archived:
                    identical = hashlib.file_digest(current, 'sha256').digest() == hashlib.file_digest(archived, 'sha256').digest()
                if not identical:
                    raise ValueError('Conflicting model audit receipts; existing and backup originals were not replaced')
            else:
                with source.open('rb') as incoming, destination.open('xb') as outgoing:
                    os.fchmod(outgoing.fileno(), 0o600)
                    shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
        mappings[(name, str(directory))] = None
    value = {'format': 'nanna-model-audit-paths-v1', 'roots': [
        {'archive_root': name, 'original_root': original, 'restored_root': str(target / name)}
        for name, original in sorted(mappings)]}
    path = staging / MODEL_AUDIT_PATHS_NAME
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    path.chmod(0o600)


def create_user(username, password, role="member", initial_admin=False):
    from werkzeug.security import generate_password_hash
    if not username.strip() or len(username) > 128 or username != username.strip():
        raise ValueError("Username must be non-empty, at most 128 characters, without surrounding whitespace")
    if role not in runtime.ROLES:
        raise ValueError("Role must be admin, editor or member; reviewers use editor")
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    if initial_admin and role != "admin":
        raise ValueError("The bootstrap user must be admin")
    if initial_admin:
        schema.migrate()
    else:
        schema.require_schema()
    hashed = generate_password_hash(password)
    with closing(runtime.connect_db()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        admins = connection.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
        if initial_admin and admins:
            raise ValueError("An admin already exists; use add-user for additional accounts")
        if not initial_admin and not admins:
            raise ValueError("Initialize an admin with init-admin before adding users")
        connection.execute("INSERT INTO users(username,password,role) VALUES (?,?,?)", (username, hashed, role))


def _readonly_database(database):
    return sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)


def _validate_database(database):
    with closing(_readonly_database(database)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        entries = [row[1] for row in connection.execute("PRAGMA table_info(entries)")]
        users = [row[1] for row in connection.execute("PRAGMA table_info(users)")]
        if entries[:19] != [name for name, _ in runtime.ENTRY_COLUMNS]:
            raise ValueError("Backup does not contain the supported 19-column entry schema")
        if users[:4] != ["id", "username", "password", "role"]:
            raise ValueError("Backup does not contain the supported user/role schema")
        if connection.execute("SELECT COUNT(*) FROM users WHERE role NOT IN ('admin','editor','member') OR role IS NULL").fetchone()[0]:
            raise ValueError("Backup contains unsupported account roles")


def _validate_attempt_ledger(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Historical attempt ledger must be a regular private file')
    with closing(_readonly_database(path)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Historical attempt ledger integrity check failed')
        validate_attempt_schema(db)


def _snapshot_attempt_ledger(source, destination):
    if source.is_symlink():
        raise ValueError('Historical attempt ledger must not be a symlink')
    if not source.exists():
        return False
    _validate_attempt_ledger(source)
    with closing(_readonly_database(source)) as origin, closing(sqlite3.connect(destination)) as snapshot:
        origin.backup(snapshot)
    destination.chmod(0o600)
    _validate_attempt_ledger(destination)
    return True


def _preserve_current_attempts(staging):
    """Restoring old content cannot erase newer locally visible paid reservations."""
    destination = ledger_path(staging / 'database.db')
    if destination.exists() or destination.is_symlink():
        _validate_attempt_ledger(destination)
    current = staging / '.current-historical-attempts.sqlite3'
    if not _snapshot_attempt_ledger(ledger_path(runtime.settings.database_path), current):
        return
    if not destination.exists():
        os.replace(current, destination)
        return
    try:
        with closing(_readonly_database(current)) as origin, closing(sqlite3.connect(destination)) as target, target:
            origin.row_factory = target.row_factory = sqlite3.Row
            for row in origin.execute('SELECT * FROM historical_attempts'):
                previous = target.execute('SELECT * FROM historical_attempts WHERE request_sha256=?', (row['request_sha256'],)).fetchone()
                if previous is None:
                    target.execute('INSERT INTO historical_attempts VALUES(?,?,?,?,?,?,?,?)', tuple(row))
                    continue
                if previous['request_json'] != row['request_json']:
                    raise ValueError('Conflicting historical request identity; prevention history was not replaced')
                if previous['state'] == 'reserved' and row['state'] != 'reserved':
                    target.execute('''UPDATE historical_attempts SET state=?,reserved_at=?,finished_at=?,
                        error=?,raw_output_json=?,decision_json=? WHERE request_sha256=?''',
                        (row['state'], min(row['reserved_at'], previous['reserved_at']), row['finished_at'],
                         row['error'], row['raw_output_json'], row['decision_json'], row['request_sha256']))
                elif previous['state'] != 'reserved' and row['state'] != 'reserved':
                    fields = ('state', 'error', 'raw_output_json', 'decision_json')
                    if any(previous[field] != row[field] for field in fields):
                        raise ValueError('Conflicting historical request outcomes; resolve the private ledgers before restore')
            target.executemany('INSERT OR IGNORE INTO historical_outcomes VALUES(?,?,?,?,?)',
                               origin.execute('SELECT * FROM historical_outcomes'))
        _validate_attempt_ledger(destination)
    finally:
        current.unlink(missing_ok=True)


def _preserve_current_native_attempts(staging):
    """Union paid guards by immutable (canonical source URL, content digest).

    Only prevention/evidence crosses the rollback boundary, not newer drafts or
    acquisition/entry IDs. Absent documents get fresh local IDs; daily run IDs
    and source-attempt IDs are likewise allocated in the restored database.
    """
    current = runtime.settings.database_path
    if current.is_symlink():
        raise ValueError('Restore refuses a symlinked live database')
    if not current.exists():
        return
    with closing(_readonly_database(current)) as origin, closing(sqlite3.connect(staging / 'database.db')) as target, target:
        origin.row_factory = target.row_factory = sqlite3.Row
        origin.execute('BEGIN')
        tables = {row[0] for row in origin.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'source_documents' not in tables:
            return
        required = {'source_documents', 'source_document_model_attempts', 'daily_model_attempts', 'daily_runs', 'daily_links'}
        if not required.issubset(tables):
            raise ValueError('Migrate the live database before preserving native paid attempts')
        restored_tables = {row[0] for row in target.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        runs = {}
        rank = {'not_extracted': 0, 'failed': 1, 'extracting': 2, 'completed': 3}
        documents = origin.execute('''SELECT d.* FROM source_documents d WHERE
            d.model_status IN ('failed','extracting','completed')
            OR EXISTS(SELECT 1 FROM source_document_model_attempts a WHERE a.document_id=d.id)
            OR EXISTS(SELECT 1 FROM daily_model_attempts a WHERE a.document_id=d.id)
            OR EXISTS(SELECT 1 FROM daily_links l WHERE l.document_id=d.id
                AND l.state IN ('modelling','model_uncertain','model_failed','done'))''')
        for document in documents:
            if not (required | {'information_records', 'information_sources'}).issubset(restored_tables):
                raise ValueError('Backup schema cannot retain native paid prevention; migrate it before restore')
            previous = target.execute('SELECT * FROM source_documents WHERE source_url=? AND digest=?',
                                      (document['source_url'], document['digest'])).fetchone()
            if previous is None:
                # Source registry IDs are stable strings, unlike database row IDs.
                source_id = document['source_id']
                if target.execute('SELECT 1 FROM information_sources WHERE id=?', (source_id,)).fetchone() is None:
                    source_id = None
                document_id = target.execute('''INSERT INTO source_documents(source_id,source_url,digest,
                    document_json,title,content_text,source_published_date,fetched_at,captured_by,publisher_college)
                    VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (source_id, document['source_url'], document['digest'], document['document_json'], document['title'],
                     document['content_text'], document['source_published_date'], document['fetched_at'],
                     document['captured_by'], document['publisher_college'])).lastrowid
                previous_status = 'not_extracted'
            else:
                document_id, previous_status = previous['id'], previous['model_status']
            attempts = origin.execute('SELECT * FROM source_document_model_attempts WHERE document_id=? ORDER BY id',
                                      (document['id'],)).fetchall()
            status = document['model_status']
            resolved_previous_claim = False
            for attempt in attempts:
                # An unfinished reservation must remain uncertain even when another
                # branch of history contains a failed attempt for this content.
                attempt_status = attempt['model_status'] if attempt['finished_at'] else 'extracting'
                if rank.get(attempt_status, 2) > rank.get(status, 2):
                    status = attempt_status
                fields = ('actor', 'started_at', 'finished_at', 'model_status', 'model_error',
                          'model_name', 'output_json', 'response_json')
                saved = target.execute('''SELECT * FROM source_document_model_attempts
                    WHERE document_id=? AND actor=? AND started_at=?''',
                    (document_id, attempt['actor'], attempt['started_at'])).fetchall()
                if not saved:
                    target.execute('''INSERT INTO source_document_model_attempts(document_id,actor,started_at,
                        finished_at,model_status,model_error,model_name,output_json,response_json)
                        VALUES(?,?,?,?,?,?,?,?,?)''', (document_id, *(attempt[field] for field in fields)))
                else:
                    for saved_attempt in saved:
                        if saved_attempt['finished_at'] and attempt['finished_at']:
                            if any(saved_attempt[field] != attempt[field] for field in fields):
                                raise ValueError('Conflicting native model attempt outcomes; prevention history was not replaced')
                        elif not saved_attempt['finished_at'] and attempt['finished_at']:
                            target.execute('''UPDATE source_document_model_attempts SET finished_at=?,model_status=?,
                                model_error=?,model_name=?,output_json=?,response_json=? WHERE id=?''',
                                (*(attempt[field] for field in fields[2:]), saved_attempt['id']))
                            resolved_previous_claim = True
            daily = origin.execute('SELECT * FROM daily_model_attempts WHERE document_id=?', (document['id'],)).fetchone()
            if daily is not None:
                daily_status = {'done': 'completed', 'failed': 'failed'}.get(daily['status'], 'extracting')
                if rank[daily_status] > rank.get(status, 2):
                    status = daily_status
                saved_daily = target.execute('SELECT * FROM daily_model_attempts WHERE document_id=?', (document_id,)).fetchone()
                if saved_daily is None:
                    if daily['run_id'] not in runs:
                        run = origin.execute('SELECT * FROM daily_runs WHERE id=?', (daily['run_id'],)).fetchone()
                        if run is None:
                            raise ValueError('Native paid attempt has no originating collection run')
                        # Run results embed old row IDs and are not recovered output.
                        runs[daily['run_id']] = target.execute('''INSERT INTO daily_runs(actor,started_at,finished_at,status,mode)
                            VALUES(?,?,?,?,?)''', (run['actor'], run['started_at'], run['finished_at'],
                            'interrupted' if run['status'] == 'running' else run['status'], run['mode'])).lastrowid
                    target.execute('INSERT INTO daily_model_attempts(document_id,run_id,status) VALUES(?,?,?)',
                                   (document_id, runs[daily['run_id']], daily['status']))
                elif (saved_daily['status'] == 'attempted' and daily['status'] != 'attempted'
                      or saved_daily['status'] == 'uncertain' and daily['status'] in ('done', 'failed')):
                    target.execute('UPDATE daily_model_attempts SET status=? WHERE document_id=?', (daily['status'], document_id))
            # Older collection claims can predate either attempt table. Retain
            # their paid/uncertain guard without copying stale link or run IDs.
            for link in origin.execute('SELECT state FROM daily_links WHERE document_id=?', (document['id'],)):
                link_status = {'done': 'completed', 'model_failed': 'failed',
                               'modelling': 'extracting', 'model_uncertain': 'extracting'}.get(link['state'])
                if link_status and rank[link_status] > rank.get(status, 2):
                    status = link_status
            if status == 'not_extracted':
                status = 'extracting'  # A durable attempt alone is sufficient prevention.
            pending = target.execute('''SELECT 1 FROM source_document_model_attempts
                WHERE document_id=? AND finished_at IS NULL LIMIT 1''', (document_id,)).fetchone()
            if pending is not None and status != 'completed':
                status = 'extracting'
            if (rank.get(previous_status, 2) > rank.get(status, 2)
                    and not (previous_status == 'extracting' and resolved_previous_claim and pending is None)):
                status = previous_status
            error = document['model_error'] if status == document['model_status'] else (
                previous['model_error'] if previous is not None and status == previous_status else None)
            target.execute('UPDATE source_documents SET model_status=?,model_error=? WHERE id=?',
                           (status, error, document_id))
            if previous is None:
                target.execute('''INSERT INTO information_records(document_id,model_status,date_uncertainty)
                    VALUES(?,?,?)''', (document_id, status, '已保留回滚前的模型尝试；不自动重发或恢复候选内容。'))


def backup(output):
    """Consistent SQLite snapshot plus private files; stop writers for file coherence.

    Keys, environment files and account/provider configuration are never copied.
    The destination must not exist. Backups contain password hashes and are private.
    """
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Backup destination already exists")
    if output.resolve().is_relative_to(runtime.settings.data_dir):
        raise ValueError("Backup output must be outside NANNA_DATA_DIR")
    if not runtime.settings.database_path.is_file():
        raise ValueError("No database exists; initialize an admin first")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nanna-backup-") as temporary:
        snapshot = Path(temporary) / "database.db"
        with closing(_readonly_database(runtime.settings.database_path)) as source, closing(sqlite3.connect(snapshot)) as destination:
            source.backup(destination)
        _validate_database(snapshot)
        ledger_snapshot = Path(temporary) / HISTORICAL_LEDGER_NAME
        ledger_included = _snapshot_attempt_ledger(ledger_path(runtime.settings.database_path), ledger_snapshot)
        descriptor, staged = tempfile.mkstemp(prefix=".backup-", dir=output.parent)
        os.close(descriptor)
        try:
            with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps({
                    "format": "nanna-backup", "version": 1,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "session_secret_included": False,
                    "files_require_quiescent_writers": True,
                    "historical_attempts_included": ledger_included,
                    "model_audit_roots": {name: str(path) for name, path in _model_audit_roots().items()},
                }))
                archive.write(snapshot, "database.db")
                if ledger_included:
                    archive.write(ledger_snapshot, HISTORICAL_LEDGER_NAME)
                for name in BACKUP_DIRS:
                    directory = _model_audit_roots().get(name, runtime.settings.data_dir / name)
                    if directory.is_symlink():
                        raise ValueError("Backup refuses symlinked runtime directories")
                    for file in sorted(directory.rglob("*")):
                        if file.is_symlink():
                            raise ValueError("Backup refuses symlinked runtime files")
                        if file.is_file():
                            archive.write(file, str(Path(name) / file.relative_to(directory)))
                for name in BACKUP_FILES:
                    file = runtime.settings.data_dir / name
                    if file.exists():
                        if file.is_symlink() or not file.is_file():
                            raise ValueError("Backup refuses invalid runtime files")
                        archive.write(file, name)
            # A hard link publishes an already-complete file without overwriting.
            os.link(staged, output)
        finally:
            Path(staged).unlink(missing_ok=True)
    return output


def restore(backup_path, target):
    """Validate then restore into a NEW directory, never the live data directory."""
    backup_path = Path(backup_path).expanduser().resolve()
    target = Path(target).expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise ValueError("Restore target must not exist; choose a fresh --target directory")
    if target.resolve() == runtime.settings.data_dir or target.resolve().is_relative_to(runtime.settings.data_dir):
        raise ValueError("Restore target must be outside the live data directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    reserved = False
    with tempfile.TemporaryDirectory(prefix=".nanna-restore-", dir=target.parent) as temporary:
        staging = Path(temporary)
        with zipfile.ZipFile(backup_path) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if len(names) != len(set(names)) or "manifest.json" not in names or "database.db" not in names:
                raise ValueError("Backup is incomplete or contains duplicate paths")
            for member in members:
                name = PurePosixPath(member.filename)
                mode = member.external_attr >> 16
                if name.is_absolute() or ".." in name.parts or "\\" in member.filename or not name.parts:
                    raise ValueError("Backup contains an unsafe path")
                if stat.S_ISLNK(mode) or member.is_dir():
                    raise ValueError("Backup must contain regular files only")
                if len(name.parts) == 1:
                    allowed = name.name in ("manifest.json", "database.db", HISTORICAL_LEDGER_NAME, *BACKUP_FILES)
                else:
                    allowed = name.parts[0] in BACKUP_DIRS
                if not allowed:
                    raise ValueError("Backup contains unsupported state or configuration")
            if archive.getinfo("manifest.json").file_size > 65536:
                raise ValueError("Backup manifest is too large")
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict) or manifest.get("format") != "nanna-backup" or manifest.get("version") != 1:
                raise ValueError("Unsupported backup format")
            if manifest.get('historical_attempts_included') and HISTORICAL_LEDGER_NAME not in names:
                raise ValueError('Backup is missing its historical request prevention ledger')
            for member in members:
                if member.filename == "manifest.json":
                    continue
                destination = staging / member.filename
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with archive.open(member) as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output)
                destination.chmod(0o600)
        _validate_database(staging / "database.db")
        _preserve_current_native_attempts(staging)
        _preserve_current_attempts(staging)
        _preserve_current_audits(staging, target, manifest)
        for name in BACKUP_DIRS:
            (staging / name).mkdir(exist_ok=True, mode=0o700)
        try:
            # Exclusive reservation rejects even a target created during validation.
            target.mkdir(mode=0o700)
            reserved = True
            for item in staging.iterdir():
                os.replace(item, target / item.name)
        except BaseException:
            if reserved:
                shutil.rmtree(target)
            raise
    return target


def doctor():
    """Local prerequisite diagnostics only: never calls external services."""
    checks = []
    def report(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
    report("python", sys.version_info >= (3, 11), "Python 3.11+ required")
    for package in ("Flask", "Werkzeug", "requests", "beautifulsoup4", "waitress"):
        try:
            version = metadata.version(package)
            report(package, True, version)
        except metadata.PackageNotFoundError:
            report(package, False, "Install NannaNews and its declared dependencies")
    for binary in ('pdfinfo', 'pdftoppm', 'prlimit'):
        executable = shutil.which(binary)
        report('source_pdf_' + binary, executable is not None,
               executable or 'Install poppler-utils and util-linux for complete public PDF capture')
    report("data_directory", os.access(runtime.settings.data_dir, os.W_OK), str(runtime.settings.data_dir))
    secret_file = runtime.settings.data_dir / "session.key"
    report("session_secret", len(runtime.settings.session_secret) >= 32,
           "Persistent private key" if secret_file.is_file() else "Configured via NANNA_SECRET_KEY")
    if secret_file.is_file():
        report("secret_permissions", stat.S_IMODE(secret_file.stat().st_mode) == 0o600, "session.key must have mode 0600")
    try:
        _validate_database(runtime.settings.database_path)
        schema.require_schema()
        with closing(_readonly_database(runtime.settings.database_path)) as connection:
            has_admin = connection.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone() is not None
            views = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='view'")}
        report("database", True, "SQLite integrity and schema valid")
        report("admin", has_admin, "Initialized" if has_admin else "Run init-admin")
        report('information_schema', {'information_records_view', 'information_colleges_view',
               'public_information_records_view'} <= views,
               'Business query views required; run nannanews migrate explicitly')
    except (sqlite3.Error, ValueError):
        report("database", False, "Missing, outdated or invalid database; run nannanews migrate or restore a supported backup")
    from nannanews.publication import PACKAGES, typst_command
    try:
        result = subprocess.run([typst_command()[0], "--version"], capture_output=True, text=True, timeout=5)
        report("typst", result.returncode == 0, result.stdout.strip() if result.returncode == 0 else "Typst version probe failed")
    except (OSError, subprocess.TimeoutExpired):
        report("typst", False, "Install Typst or set NANNA_TYPST_BINARY")
    cache = runtime.settings.typst_package_cache
    if cache is None:
        cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "typst" / "packages"
    for name, version in PACKAGES:
        report(f"typst_package_{name}", (cache / "preview" / name / version / "typst.toml").is_file(),
               f"Require cached @preview/{name}:{version} (Docker preinstalls it)")
    font_command = [typst_command()[0], "fonts"]
    if runtime.settings.typst_font_dir:
        font_command += ["--font-path", str(runtime.settings.typst_font_dir)]
    try:
        result = subprocess.run(font_command, capture_output=True, text=True, timeout=5)
        font_names = result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        font_names = ""
    for family in ("Noto Sans CJK SC", "Noto Serif CJK SC", "DejaVu Sans Mono", "New Computer Modern", "Libertinus Serif"):
        report(f"font_{family}", family in font_names,
               "Require this font in Typst's font list; Docker includes CJK/DejaVu and Typst's bundled fonts")
    report("model_base_url", bool(runtime.settings.model_base_url), "Configured" if runtime.settings.model_base_url else "Missing NANNA_MODEL_BASE_URL; real extraction unavailable")
    report("model_api_key", bool(runtime.settings.model_api_key), "Configured" if runtime.settings.model_api_key else "Missing NANNA_MODEL_API_KEY; real extraction unavailable")
    report("model_name", bool(runtime.settings.model_name), runtime.settings.model_name)
    return {"ok": all(check["ok"] for check in checks), "network_probed": False, "checks": checks}


def _password(args):
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Password (minimum 12 characters): ")
        if password != getpass.getpass("Confirm password: "):
            raise ValueError("Passwords do not match")
    return password


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nannanews", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("init-admin", "add-user"):
        sub = commands.add_parser(command)
        sub.add_argument("username")
        sub.add_argument("--password-stdin", action="store_true", help="Read one password line from stdin instead of prompting")
        if command == "add-user":
            sub.add_argument("--role", choices=runtime.ROLES, default="member", help="Reviewers use editor; admin includes editor powers")
    commands.add_parser("doctor")
    commands.add_parser("migrate", help="Prepare private runtime and apply versioned local migrations; stop writers and back up first")
    sub = commands.add_parser("import-source-catalog", help="Convert reviewed workbook JSON to a public-only source catalog; no database or network")
    sub.add_argument("input", type=Path)
    sub.add_argument("output", type=Path)
    sub = commands.add_parser("backup", help="Stop file writers first; the SQLite snapshot itself is online-safe")
    sub.add_argument("output")
    sub = commands.add_parser("restore", help="Restore into a fresh directory; keys/config are not restored")
    sub.add_argument("backup")
    sub.add_argument("--target", required=True)
    sub = commands.add_parser("serve")
    sub.add_argument("--host", default=runtime.settings.host)
    sub.add_argument("--port", type=int, default=runtime.settings.port)
    sub = commands.add_parser('collect', help='Run a new-content scan or a separate bounded historical raw capture; never publish or generate PDF')
    sub.add_argument('--actor', default='system:daily')
    sub.add_argument('--mode', choices=('new', 'history'), default='new',
                     help='new (default): current discovery and extraction; history: bounded unprocessed historical raw only, no model backfill')
    commands.add_parser('collect-worker', help='Run the configured once-daily Beijing collection worker')
    sub = commands.add_parser('query', help='Read business information with the same filters as the website')
    sub.add_argument('--q', default='')
    sub.add_argument('--date-type', choices=('source', 'event', 'deadline', 'publication'), default='source')
    sub.add_argument('--date-from', default='')
    sub.add_argument('--date-to', default='')
    sub.add_argument('--college', action='append', default=[])
    sub.add_argument('--college-relation', choices=('any', 'publisher', 'associated', 'audience'), default='any')
    sub.add_argument('--source', action='append', default=[])
    sub.add_argument('--tag', action='append', default=[])
    sub.add_argument('--scope', choices=('private', 'public'), default='private')
    sub.add_argument('--page', type=int, default=1)
    sub.add_argument('--per-page', type=int, default=20)
    sub = commands.add_parser('parse-pdf', help='Parse a local bulletin PDF offline into a private evidence bundle; no import or model')
    sub.add_argument('pdf')
    sub.add_argument('--output', required=True, help='New private output directory')
    sub.add_argument('--parser-binary', default=None)
    sub = commands.add_parser('import-bulletins', help='Import historical HTML and local PDFs; configured AI reviews dates outside the write transaction')
    sub.add_argument('documents', nargs='*')
    sub.add_argument('--archive', help='Local nik-nul archive root with issue-index.json and manifest.json')
    sub.add_argument('--actor', default='system:bulletin-import')
    sub = commands.add_parser('bulletin-query', help='Query retained bulletin evidence without changing it')
    sub.add_argument('--id', type=int)
    sub.add_argument('--q', default='')
    sub.add_argument('--issue-from', default='')
    sub.add_argument('--issue-to', default='')
    sub.add_argument('--category', action='append', default=[])
    sub.add_argument('--college', action='append', default=[])
    sub.add_argument('--source', action='append', default=[])
    sub.add_argument('--date-type', choices=('event', 'deadline', 'listed', 'source'), default='event')
    sub.add_argument('--date-from', default='')
    sub.add_argument('--date-to', default='')
    sub.add_argument('--date-status', choices=('known', 'unknown'))
    sub.add_argument('--freshness', choices=('not_expired', 'expired', 'unknown'))
    sub.add_argument('--as-of')
    sub.add_argument('--page', type=int, default=1)
    sub.add_argument('--per-page', type=int, default=20)
    sub = commands.add_parser('bulletin-issues', help='Read issue identities and retained document evidence')
    sub.add_argument('--date-from', default='')
    sub.add_argument('--date-to', default='')
    sub = commands.add_parser('source-coverage', help='Read bounded original-article coverage against retained bulletin source links; not activity recall')
    sub.add_argument('--issue-from', type=int, help='Inclusive numeric issue number; no bounds defaults to latest issue')
    sub.add_argument('--issue-to', type=int, help='Inclusive numeric issue number')
    sub = commands.add_parser('export-static', help='Export only public bulletin fields and resources; no private editorial data or backend API')
    sub.add_argument('output', help='Static reader data directory')
    sub.add_argument('--replace', action='store_true', help='Replace only a previous marked static export')
    sub = commands.add_parser('prepare-bulletin-source', help='Bind an operator-reviewed source capture; no fetching or AI calls')
    sub.add_argument('--occurrence-id', required=True)
    sub.add_argument('--original-url', required=True)
    sub.add_argument('--corrected-url', required=True)
    sub.add_argument('--capture', required=True, help='Private captured source JSON')
    sub.add_argument('--reason', required=True, help='Public explanation for the correction')
    sub.add_argument('--actor', required=True, help='Private operator identity, not an AI reviewer')
    sub.add_argument('--output', required=True, help='New private directory for review.json')
    sub = commands.add_parser('repair-bulletin-sources', help='Append reviewed source corrections; dry-run unless --apply')
    sub.add_argument('--review', action='append', required=True, help='Prepared private source review JSON array; repeatable')
    sub.add_argument('--audit-dir', required=True, help='New private directory for complete plan and result')
    sub.add_argument('--apply', action='store_true')
    sub = commands.add_parser('repair-bulletin-dates', help='Audit derived dates over immutable public bulletin evidence; dry-run unless --apply')
    sub.add_argument('--review', action='append', default=[], help='Source-bound Luna review JSON decision array; repeatable')
    sub.add_argument('--audit-dir', help='New private directory for the complete plan and result')
    sub.add_argument('--apply', action='store_true', help='Append the validated revisions in one transaction')
    sub = commands.add_parser('review-bulletin-dates', help='Prepare resumable, private AI source reviews without applying database revisions')
    sub.add_argument('--output', required=True, help='Private output directory for reviews.jsonl')
    sub.add_argument('--occurrence-id', action='append', default=[], help='Public occurrence ID; repeatable')
    sub.add_argument('--selection', choices=('pending', 'unknown', 'all'), default='pending')
    sub.add_argument('--limit', type=int, help='Maximum new reviews in this invocation')
    sub = commands.add_parser('prepare-bulletin-full-review', help='Freeze all bulletin occurrences from three accepted current-bound receipts; no network or model')
    sub.add_argument('--output', required=True)
    sub.add_argument('--acceptance', required=True)
    sub.add_argument('--resume-from', help='Stopped previous private scope; preserve all paid outcomes and evidence')
    sub.add_argument('--recover-unpaid-sources', action='store_true', help='Reopen only previously unpaid source gaps in the new scope; never reset paid history')
    sub = commands.add_parser('review-bulletins-full', help='Run/resume the private serial frozen full-review worker; never apply dates')
    sub.add_argument('--output', required=True)
    sub = commands.add_parser('reuse-bulletin-full-reviews', help='Recover bound historical receipts from completed equivalents only; no network, model calls or MAIN writes')
    sub.add_argument('--output', required=True)
    sub = commands.add_parser('watch-bulletin-full-review', help='Read-only worker health monitor; no model configuration needed')
    sub.add_argument('--output', required=True)
    sub.add_argument('--interval', type=int, default=600)
    sub = commands.add_parser('merge-activities', help='Append reviewed cross-publication activity groups; dry-run unless --apply')
    sub.add_argument('--review', action='append', default=[], help='Prepared activity review JSON; repeatable')
    sub.add_argument('--audit-dir', help='New private directory for the complete plan and result')
    sub.add_argument('--apply', action='store_true', help='Append reviewed groups in one transaction')
    sub = commands.add_parser('activity-candidates', help='Export public-only same-title or explicitly selected activity candidates to a private artifact')
    sub.add_argument('--output', required=True, help='New private output directory')
    sub.add_argument('--item-id', action='append', default=[], help='Select a cross-title candidate by public item ID; repeat at least twice')
    args = parser.parse_args(argv)
    try:
        independent = {"migrate", "init-admin", "doctor", "backup", "restore", "parse-pdf", "import-source-catalog"}
        if args.command not in independent:
            schema.require_schema()
        if args.command in {"collect", "collect-worker", "import-bulletins"}:
            runtime.prepare_runtime()
        if args.command == "migrate":
            print(f"Database schema is current (version {schema.migrate()})")
        elif args.command == "import-source-catalog":
            from nannanews.source_catalog import import_catalog
            count = import_catalog(args.input, args.output)
            print(f"Imported {count} public accounts into {args.output}")
        elif args.command == "init-admin":
            create_user(args.username, _password(args), "admin", initial_admin=True)
            print("Admin initialized")
        elif args.command == "add-user":
            create_user(args.username, _password(args), args.role)
            print(f"Created {args.role} account")
        elif args.command == "doctor":
            result = doctor()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
        elif args.command == "backup":
            print(f"Backup created: {backup(args.output)}")
            print("SQLite is consistent. File writers must be stopped for a coherent file snapshot.", file=sys.stderr)
        elif args.command == "restore":
            print(f"Restored into: {restore(args.backup, args.target)}")
            print("Set NANNA_DATA_DIR to this target to use it. Session key and provider configuration are intentionally not restored.")
        elif args.command == 'collect':
            from nannanews import daily_acquisition
            result = daily_acquisition.run_once(actor=args.actor, mode=args.mode)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result.get('status') in {'failed', 'no_sources'}:
                return 1
        elif args.command == 'collect-worker':
            if not runtime.settings.collection_enabled:
                raise ValueError('Enable NANNA_COLLECTION_ENABLED before starting the daily worker')
            from nannanews import daily_acquisition
            daily_acquisition.run_worker()
        elif args.command == 'query':
            from nannanews import information
            filters = information.parse_filters({'q': args.q, 'date_type': args.date_type,
                'date_from': args.date_from, 'date_to': args.date_to,
                'college': args.college, 'college_relation': args.college_relation,
                'source': args.source, 'tag': args.tag})
            result = information.query_records(filters, scope=args.scope,
                page=max(1, args.page), per_page=min(100, max(1, args.per_page)))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == 'parse-pdf':
            from nannanews import pdf_adapter
            output = Path(args.output).expanduser().absolute()
            output.mkdir(parents=True, mode=0o700, exist_ok=False)
            parsed = pdf_adapter.parse_pdf(args.pdf, output,
                                           parser_binary=args.parser_binary or runtime.settings.pdf_parser_binary)
            document_path = output / 'document.json'
            with document_path.open('x', encoding='utf-8') as stream:
                json.dump(parsed, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            document_path.chmod(0o600)
            print(json.dumps({'output': str(document_path), 'issue_date': parsed['issue_date'],
                              'issue_number': parsed['issue_number'], 'pages': len(parsed['pages']),
                              'items': len(parsed['items']), 'assets': len(parsed['assets']),
                              'warnings': parsed['warnings']}, ensure_ascii=False, indent=2))
        elif args.command == 'import-bulletins':
            if not args.archive and not args.documents:
                raise ValueError('Provide --archive or at least one local document')
            from nannanews import bulletin_store
            bulletin_store.validate_model_configuration()
            results = []
            if args.archive:
                results.append(bulletin_store.import_archive(args.archive, actor=args.actor))
            for path in args.documents:
                results.append(bulletin_store.import_document(path, actor=args.actor))
            print(json.dumps({'results': results}, ensure_ascii=False, indent=2))
            if any(result.get('status') in {'failed', 'partial'} or result.get('failed', 0) for result in results):
                return 1
        elif args.command == 'bulletin-query':
            from nannanews import bulletin_store
            if args.id is not None:
                result = bulletin_store.get_item(args.id)
                if result is None:
                    raise ValueError('Bulletin item does not exist')
            else:
                filters = {'q': args.q, 'issue_date_from': args.issue_from, 'issue_date_to': args.issue_to,
                           'category': args.category, 'college': args.college, 'source': args.source,
                           'date_type': args.date_type, 'date_from': args.date_from, 'date_to': args.date_to,
                           'date_status': args.date_status, 'freshness': args.freshness, 'as_of': args.as_of}
                result = bulletin_store.query_items(filters, page=args.page, per_page=args.per_page)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == 'bulletin-issues':
            from nannanews import bulletin_store
            print(json.dumps(bulletin_store.list_issues({'issue_date_from': args.date_from,
                                                        'issue_date_to': args.date_to}), ensure_ascii=False, indent=2))
        elif args.command == 'source-coverage':
            from nannanews import source_coverage
            with closing(schema.open_database(readonly=True)) as connection:
                connection.execute('BEGIN')
                result = source_coverage.source_coverage(connection,
                    issue_from=args.issue_from, issue_to=args.issue_to)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == 'export-static':
            from nannanews import static_export
            print(json.dumps(static_export.export_static(args.output, replace=args.replace),
                             ensure_ascii=False, indent=2))
        elif args.command == 'prepare-bulletin-source':
            from nannanews import bulletin_source_repair
            print(json.dumps(bulletin_source_repair.prepare_source(occurrence_id=args.occurrence_id,
                original_url=args.original_url, corrected_url=args.corrected_url, capture_file=args.capture,
                reason=args.reason, actor=args.actor, output_dir=args.output), ensure_ascii=False, indent=2))
        elif args.command == 'repair-bulletin-sources':
            from nannanews import bulletin_source_repair
            print(json.dumps(bulletin_source_repair.repair_sources(review_files=args.review,
                apply=args.apply, audit_dir=args.audit_dir), ensure_ascii=False, indent=2))
        elif args.command == 'repair-bulletin-dates':
            from nannanews import bulletin_date_repair
            print(json.dumps(bulletin_date_repair.repair_dates(review_files=args.review,
                apply=args.apply, audit_dir=args.audit_dir), ensure_ascii=False, indent=2))
        elif args.command == 'review-bulletin-dates':
            from nannanews import bulletin_date_repair
            result = bulletin_date_repair.review_dates(output_dir=args.output,
                occurrence_ids=args.occurrence_id, selection=args.selection, limit=args.limit)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == 'prepare-bulletin-full-review':
            from nannanews import bulletin_full_review
            result = bulletin_full_review.prepare_full_review(args.output, args.acceptance,
                resume_from=args.resume_from, recover_unpaid_sources=args.recover_unpaid_sources)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == 'review-bulletins-full':
            from nannanews import bulletin_full_review
            result = bulletin_full_review.run_full_review(args.output)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == 'reuse-bulletin-full-reviews':
            from nannanews import bulletin_full_review
            result = bulletin_full_review.reuse_full_reviews(args.output)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == 'watch-bulletin-full-review':
            from nannanews import bulletin_full_review
            bulletin_full_review.watch_full_review(args.output, args.interval)
        elif args.command == 'merge-activities':
            from nannanews import bulletin_activities
            result = bulletin_activities.merge_activities(review_files=args.review,
                apply=args.apply, audit_dir=args.audit_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == 'activity-candidates':
            from nannanews import bulletin_activities
            database = Path(runtime.settings.database_path).resolve(strict=True)
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute('PRAGMA query_only=ON')
                candidates = bulletin_activities.activity_candidates(connection, item_ids=args.item_id or None)
            output = Path(args.output).expanduser().absolute()
            if output.exists() or output.is_symlink():
                raise ValueError('Activity candidate output directory already exists.')
            output.mkdir(mode=0o700, parents=True, exist_ok=False)
            output.chmod(0o700)
            candidate_file = output / 'candidates.json'
            with candidate_file.open('x', encoding='utf-8') as stream:
                candidate_file.chmod(0o600)
                json.dump(candidates, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            print(json.dumps({'output': str(candidate_file), 'groups': len(candidates['candidates']),
                              'snapshot_sha256': candidates['snapshot_sha256']}, ensure_ascii=False, indent=2))
        elif args.command == "serve":
            if not 1 <= args.port <= 65535:
                raise ValueError("Serve port must be between 1 and 65535")
            with closing(runtime.connect_db()) as connection:
                if not connection.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone():
                    raise ValueError("Run init-admin before serve")
            from waitress import serve
            from nannanews.web import create_app
            serve(create_app(), host=args.host, port=args.port, threads=4, expose_tracebacks=False)
    except (ValueError, RuntimeError, OSError, sqlite3.Error, zipfile.BadZipFile, EOFError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
