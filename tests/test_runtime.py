"""Behavior tests; external compiler/process boundary is isolated explicitly."""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import zipfile

import pytest


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("NANNA_DATA_DIR", str(tmp_path / "live"))
    monkeypatch.delenv("NANNA_SECRET_KEY", raising=False)
    monkeypatch.setenv("NANNA_MODEL_BASE_URL", "")
    monkeypatch.setenv("NANNA_MODEL_API_KEY", "")
    from nannanews import runtime
    monkeypatch.setattr(runtime, "settings", runtime.load_settings())
    runtime.prepare_runtime()
    return runtime


def test_collection_configuration_bounds_discovery_not_article_model_count(state, monkeypatch):
    monkeypatch.setenv('NANNA_COLLECTION_PAGE_LIMIT', '3')
    settings = state.load_settings()
    assert settings.collection_page_limit == 3


@pytest.mark.parametrize('value', ['0', '-1', 'invalid'])
def test_collection_discovery_limit_still_requires_positive_configuration(state, monkeypatch, value):
    monkeypatch.setenv('NANNA_COLLECTION_PAGE_LIMIT', value)
    with pytest.raises(state.ConfigurationError, match='NANNA_COLLECTION_PAGE_LIMIT'):
        state.load_settings()


@pytest.mark.parametrize('value', ['0', '-1', '7', '1.5', 'invalid'])
def test_collection_fetch_concurrency_rejects_out_of_range_configuration(state, monkeypatch, value):
    monkeypatch.setenv('NANNA_COLLECTION_FETCH_WORKERS', value)
    with pytest.raises(state.ConfigurationError, match='NANNA_COLLECTION_FETCH_WORKERS'):
        state.load_settings()


def test_bootstrap_secret_persists_privately_and_paths_are_confined(state):
    second = state.load_settings()
    assert second.session_secret == state.settings.session_secret
    assert (state.settings.data_dir / "session.key").stat().st_mode & 0o777 == 0o600
    assert state.settings.data_dir.stat().st_mode & 0o777 == 0o700
    assert state.path("publications/issue.json") == state.settings.publication_dir / "issue.json"
    with pytest.raises(ValueError):
        state.path("../escape")
    with pytest.raises(ValueError):
        state.path("/absolute")


def test_existing_entries_and_users_migrate_without_column_reordering(state):
    with closing(sqlite3.connect(state.settings.database_path)) as db, db:
        db.execute("CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL)")
        db.execute("INSERT INTO users(username,password) VALUES('legacy','existing-hash')")
        db.execute("CREATE TABLE entries(" + ",".join(f'"{name}" {ddl}' for name, ddl in state.ENTRY_COLUMNS[:13]) + ")")
        db.execute("INSERT INTO entries(uploader,upload_time,title,status) VALUES('legacy','2026-10-01','Keep me','Approved')")
    from nannanews import schema
    schema.migrate()
    schema.migrate()
    with closing(state.connect_db()) as db:
        assert [row[1] for row in db.execute("PRAGMA table_info(entries)")] == [name for name, _ in state.ENTRY_COLUMNS]
        assert db.execute("SELECT title,status FROM entries").fetchone() == ("Keep me", "approved")
        assert db.execute("SELECT password,role FROM users").fetchone() == ("existing-hash", "member")
    assert state.get_user_role("legacy") == "member"
    assert state.get_user_role("unknown") is None


def test_accounts_have_no_default_password_or_implicit_privilege(state):
    from nannanews import cli as manage
    from werkzeug.security import check_password_hash
    from nannanews import schema
    schema.migrate()
    with pytest.raises(ValueError, match="Initialize an admin"):
        manage.create_user("reviewer", "a-long-user-password", "editor")
    manage.create_user("owner", "a-long-admin-password", "admin", initial_admin=True)
    manage.create_user("reviewer", "a-long-user-password", "editor")
    assert state.get_user_role("owner") == "admin"
    assert state.get_user_role("reviewer") == "editor"
    with closing(state.connect_db()) as db:
        hashed = db.execute("SELECT password FROM users WHERE username='owner'").fetchone()[0]
    assert hashed != "a-long-admin-password"
    assert check_password_hash(hashed, "a-long-admin-password")
    with pytest.raises(ValueError, match="already exists"):
        manage.create_user("other-admin", "a-long-other-password", "admin", initial_admin=True)
    with pytest.raises(ValueError, match="12 characters"):
        manage.create_user("weak", "short", "member")


def test_backup_restore_isolates_data_and_omits_secrets(state, tmp_path):
    from nannanews import cli as manage
    manage.create_user("owner", "a-long-admin-password", "admin", initial_admin=True)
    with closing(state.connect_db()) as db, db:
        db.execute("INSERT INTO entries(uploader,upload_time,title,status) VALUES('owner','2026-10-01','Snapshot title','approved')")
    (state.settings.upload_dir / "public-image.png").write_bytes(b"image")
    (state.settings.archive_dir / "2026-10-01.pdf").write_bytes(b"%PDF-snapshot")
    (state.settings.publication_dir / "latest.json").write_text('{"data":{}}')
    (state.settings.data_dir / ".env").write_text("MODEL_API_KEY=private-key")
    output = manage.backup(tmp_path / "snapshot.zip")
    with zipfile.ZipFile(output) as archive:
        assert "session.key" not in archive.namelist()
        assert ".env" not in archive.namelist()
    assert output.stat().st_mode & 0o777 == 0o600
    with closing(state.connect_db()) as db, db:
        db.execute("UPDATE entries SET title='Live title'")
    restored = manage.restore(output, tmp_path / "recovered")
    assert (restored / "uploads/public-image.png").read_bytes() == b"image"
    assert (restored / "archives/2026-10-01.pdf").read_bytes() == b"%PDF-snapshot"
    assert not (restored / "session.key").exists()
    with closing(sqlite3.connect(restored / "database.db")) as db:
        assert db.execute("SELECT title FROM entries").fetchone()[0] == "Snapshot title"
    with closing(state.connect_db()) as db:
        assert db.execute("SELECT title FROM entries").fetchone()[0] == "Live title"
    with pytest.raises(ValueError, match="must not exist"):
        manage.restore(output, restored)
    with pytest.raises(ValueError):
        manage.restore(output, state.settings.data_dir)
    with pytest.raises(ValueError, match="already exists"):
        manage.backup(output)


def test_historical_attempts_use_coherent_backup_and_preserve_newer_local_reservations(state, tmp_path):
    from nannanews import cli as manage
    from nannanews.bulletin_historical_attempts import AttemptBlocked, AttemptStore, ledger_path
    manage.create_user('owner', 'a-long-admin-password', 'admin', initial_admin=True)
    store = AttemptStore(state.settings.database_path)
    first = '{"activity":"first"}'
    second = '{"activity":"second"}'
    first_key = hashlib.sha256(first.encode()).hexdigest()
    second_key = hashlib.sha256(second.encode()).hexdigest()
    store.reserve(first_key, first)
    # Keep a WAL connection open to prove the archive uses SQLite's snapshot API.
    with closing(sqlite3.connect(store.path)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        output = manage.backup(tmp_path / 'before-finish.zip')
        store.finish(first_key, decision={'status': 'unresolved'}, raw_output={'private_reply': 'retained'})
        store.reserve(second_key, second)
        recovered = manage.restore(output, tmp_path / 'recovered-with-prevention')
    with zipfile.ZipFile(output) as archive:
        assert ledger_path(state.settings.database_path).name in archive.namelist()
        assert not any(name.endswith(('-wal', '-shm')) for name in archive.namelist())
    restored = AttemptStore(recovered / 'database.db')
    assert restored.path.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(restored.path)) as db:
        rows = db.execute('SELECT request_sha256,state,raw_output_json FROM historical_attempts ORDER BY request_sha256').fetchall()
    assert {key: status for key, status, _ in rows} == {first_key: 'completed', second_key: 'reserved'}
    assert json.loads(next(value for key, _, value in rows if key == first_key)) == {'private_reply': 'retained'}
    for key, value in ((first_key, first), (second_key, second)):
        with pytest.raises(AttemptBlocked):
            restored.reserve(key, value)
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute('SELECT COUNT(*) FROM historical_attempts').fetchone()[0] == 2


def test_corrupt_historical_ledger_restore_never_creates_target(state, tmp_path):
    from nannanews import cli as manage
    manage.create_user('owner', 'a-long-admin-password', 'admin', initial_admin=True)
    original = manage.backup(tmp_path / 'ordinary.zip')
    damaged = tmp_path / 'damaged-ledger.zip'
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(damaged, 'w') as destination:
        for member in source.infolist():
            destination.writestr(member, source.read(member.filename))
        destination.writestr(manage.HISTORICAL_LEDGER_NAME, b'not a SQLite ledger')
    target = tmp_path / 'never-created'
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        manage.restore(damaged, target)
    assert not target.exists()


def test_restore_rejects_unsafe_paths_before_creating_target(state, tmp_path):
    from nannanews import cli as manage
    malicious = tmp_path / "malicious.zip"
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr("manifest.json", '{"format":"nanna-backup","version":1}')
        archive.writestr("database.db", "not a database")
        archive.writestr("../escape", "unsafe")
    target = tmp_path / "target"
    with pytest.raises(ValueError, match="unsafe path"):
        manage.restore(malicious, target)
    assert not target.exists()
    assert not (tmp_path / "escape").exists()


def test_corrupt_restore_never_creates_target(state, tmp_path):
    from nannanews import cli as manage
    corrupt = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(corrupt, "w") as archive:
        archive.writestr("manifest.json", '{"format":"nanna-backup","version":1}')
        archive.writestr("database.db", "not a database")
    target = tmp_path / "target"
    with pytest.raises(sqlite3.DatabaseError):
        manage.restore(corrupt, target)
    assert not target.exists()


def _issue(state, image=None):
    data = {"date": "2026-10-01", "no": 1, "columns": 4}
    data.update({name: [] for name in ("other", "lecture", "college", "club", "career", "reference")})
    if image:
        data["other"] = [{"title": "Public event", "image": image, "description": [], "id": 1, "priority": 0}]
    issue = {"data": data, "due": {name: [] for name in ("other", "lecture", "college", "club", "career")}}
    source = state.settings.publication_dir / "input.json"
    source.write_text(json.dumps(issue), encoding="utf-8")
    return source


@pytest.mark.parametrize("template", ["news_template.typ", "lecture_template.typ"])
def test_render_isolates_inputs_and_atomically_publishes_success(state, monkeypatch, template):
    from nannanews import publication
    (state.settings.upload_dir / "public.png").write_bytes(b"public-image")
    (state.settings.upload_dir / "private.png").write_bytes(b"must-not-copy")
    monkeypatch.setattr(state, "settings", replace(state.settings, site_name="Owned publisher", public_url="https://news.example"))
    source = _issue(state, "public.png")
    destination = state.settings.publication_dir / "result.pdf"
    destination.write_bytes(b"previous-pdf")
    observed = []
    current_directory = Path.cwd()
    def compiler(command, **kwargs):
        workspace = Path(kwargs["cwd"])
        observed.append(workspace)
        assert Path.cwd() == current_directory
        assert (workspace / template).read_text() == (state.CODE_DIR / template).read_text()
        assert (workspace / "static/uploads/public.png").read_bytes() == b"public-image"
        assert not (workspace / "static/uploads/private.png").exists()
        data = json.loads((workspace / "issue.json").read_text())["data"]
        assert data["site_name"] == "Owned publisher"
        assert data["public_url"] == "https://news.example"
        assert destination.read_bytes() == b"previous-pdf"
        Path(command[-1]).write_bytes(b"%PDF-rendered")
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(publication.subprocess, "run", compiler)
    result = publication.compile_pdf(source, template, destination)
    assert result.returncode == 0
    assert destination.read_bytes() == b"%PDF-rendered"
    assert destination.stat().st_mode & 0o777 == 0o600
    assert all(not workspace.exists() for workspace in observed)


@pytest.mark.parametrize("outcome", ["failure", "timeout", "missing-artifact"])
def test_render_failure_preserves_prior_output_and_cleans_workspace(state, monkeypatch, outcome):
    from nannanews import publication
    source = _issue(state)
    destination = state.settings.publication_dir / "result.pdf"
    destination.write_bytes(b"previous-pdf")
    observed = []
    def compiler(command, **kwargs):
        observed.append(Path(kwargs["cwd"]))
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 1 if outcome == "failure" else 0, "", "real compiler failure")
    monkeypatch.setattr(publication.subprocess, "run", compiler)
    if outcome == "failure":
        assert publication.compile_pdf(source, "news_template.typ", destination).returncode == 1
    else:
        with pytest.raises(subprocess.TimeoutExpired if outcome == "timeout" else RuntimeError):
            publication.compile_pdf(source, "news_template.typ", destination)
    assert destination.read_bytes() == b"previous-pdf"
    assert all(not workspace.exists() for workspace in observed)


def test_renderer_rejects_image_traversal_and_nonprivate_output(state):
    from nannanews import publication
    source = _issue(state, "../session.key")
    with pytest.raises(ValueError, match="image escapes"):
        publication.compile_pdf(source, "news_template.typ", state.settings.publication_dir / "result.pdf")
    with pytest.raises(ValueError, match="inside NANNA_DATA_DIR"):
        publication.compile_pdf(source, "news_template.typ", state.settings.data_dir.parent / "result.pdf")


def test_incompatible_migration_rolls_back_user_schema_changes(state):
    with closing(sqlite3.connect(state.settings.database_path)) as db, db:
        db.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, password TEXT)")
        db.execute("CREATE TABLE entries(id INTEGER PRIMARY KEY, title TEXT, uploader TEXT)")
        db.execute("INSERT INTO users VALUES(1,'legacy','hash')")
    with pytest.raises(state.ConfigurationError, match="column order"):
        from nannanews import schema
        schema.migrate()
    with closing(sqlite3.connect(state.settings.database_path)) as db:
        assert [row[1] for row in db.execute("PRAGMA table_info(users)")] == ["id", "username", "password"]
        assert db.execute("SELECT * FROM users").fetchone() == (1, "legacy", "hash")


def test_doctor_reports_missing_model_without_exposing_secrets(state, monkeypatch):
    from nannanews import cli as manage
    monkeypatch.setattr(state, "settings", replace(state.settings, model_api_key="never-print-this-key"))
    result = manage.doctor()
    checks = {check["name"]: check for check in result["checks"]}
    assert result["network_probed"] is False
    assert result["ok"] is False
    assert checks["model_base_url"]["ok"] is False
    assert "NANNA_MODEL_BASE_URL" in checks["model_base_url"]["detail"]
    assert checks["model_api_key"]["ok"] is True
    assert "never-print-this-key" not in json.dumps(result)


def _wire_archive(directory, marker, *, response=True):
    from nannanews.model_audit import ModelAudit
    audit = ModelAudit(directory, {'model': 'test-model', 'instructions': 'Public source audit',
        'input': [{'role': 'user', 'content': marker}]}, request_limit=4096, response_limit=8192,
        context={'fixture': marker})
    if response:
        audit.append(json.dumps({'id': marker, 'status': 'completed'}).encode())
        audit.preserve_response(complete=True)
        audit.save('response_received', http_status=200)
    identity = audit.identity
    audit.close()
    return identity


def test_model_wire_archives_survive_backup_and_relocated_restore(state, tmp_path):
    from nannanews import cli as manage
    from nannanews.bulletin_historical_attempts import AttemptStore
    manage.create_user('owner', 'a-long-admin-password', 'admin', initial_admin=True)
    roots = (state.settings.data_dir / 'model-audit', AttemptStore(state.settings.database_path).audit_directory)
    identities = [_wire_archive(root, str(index)) for index, root in enumerate(roots)]
    original = {identity['receipt']: Path(identity['receipt']).with_name('response.bin').read_bytes()
                for identity in identities}
    backup = manage.backup(tmp_path / 'wire-evidence.zip')
    restored = manage.restore(backup, tmp_path / 'wire-restored')
    mapping = json.loads((restored / manage.MODEL_AUDIT_PATHS_NAME).read_text())['roots']
    for identity in identities:
        source = Path(identity['receipt'])
        root = next(row for row in mapping if source.is_relative_to(row['original_root']))
        receipt = Path(root['restored_root']) / source.relative_to(root['original_root'])
        assert receipt.with_name('response.bin').read_bytes() == original[identity['receipt']]
        request = receipt.with_name('request.json')
        assert hashlib.sha256(request.read_bytes()).hexdigest() == identity['request_sha256']
        assert receipt.stat().st_mode & 0o777 == 0o600
        assert receipt.parent.stat().st_mode & 0o777 == 0o700


def test_restore_preserves_newer_paid_reservation_and_its_original_wire_request(state, tmp_path):
    from nannanews import cli as manage
    from nannanews.bulletin_historical_attempts import AttemptBlocked, AttemptStore
    manage.create_user('owner', 'a-long-admin-password', 'admin', initial_admin=True)
    backup = manage.backup(tmp_path / 'before-new-paid-request.zip')
    store = AttemptStore(state.settings.database_path)
    identity = _wire_archive(store.audit_directory, 'new request with uncertain outcome', response=False)
    request = Path(identity['receipt']).with_name('request.json').read_text()
    store.reserve(identity['request_sha256'], request)
    restored = manage.restore(backup, tmp_path / 'preserved-wire-request')
    recovered = AttemptStore(restored / 'database.db')
    with pytest.raises(AttemptBlocked):
        recovered.reserve(identity['request_sha256'], request)
    mapping = json.loads((restored / manage.MODEL_AUDIT_PATHS_NAME).read_text())['roots']
    root = next(row for row in mapping if row['original_root'] == str(store.audit_directory))
    receipt = Path(root['restored_root']) / Path(identity['receipt']).relative_to(store.audit_directory)
    assert receipt.with_name('request.json').read_text() == request
    with closing(sqlite3.connect(recovered.path)) as db:
        assert db.execute('SELECT state FROM historical_attempts').fetchone()[0] == 'reserved'


def test_conflicting_model_audit_restore_does_not_replace_either_original(state, tmp_path):
    from nannanews import cli as manage
    manage.create_user('owner', 'a-long-admin-password', 'admin', initial_admin=True)
    identity = _wire_archive(state.settings.data_dir / 'model-audit', 'original request')
    backup = manage.backup(tmp_path / 'original-wire.zip')
    original_backup = hashlib.sha256(backup.read_bytes()).hexdigest()
    request = Path(identity['receipt']).with_name('request.json')
    request.write_bytes(b'conflicting current evidence')
    target = tmp_path / 'conflicting-wire-restore'
    with pytest.raises(ValueError, match='Conflicting model audit'):
        manage.restore(backup, target)
    assert not target.exists()
    assert request.read_bytes() == b'conflicting current evidence'
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == original_backup


@pytest.fixture
def native_restore_boundary(state, tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from nannanews import acquisition, daily_acquisition as daily, schema

    root = 'https://college.nju.edu.cn/notices.htm'
    url = 'https://college.nju.edu.cn/info/1001/1.htm'
    now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
    definition = [{'id': 'restore-source', 'name': '测试学院通知', 'url': root, 'kind': 'html',
                   'enabled': True, 'identity_url': root, 'expected_identity': '南京大学测试学院',
                   'article_pattern': r'^/info/1001/[0-9]+\.htm$'}]
    boundary = SimpleNamespace(posts=[], failure=None, url=url, now=now)
    monkeypatch.setenv('NANNA_MODEL_BASE_URL', 'https://model.example/v1')
    monkeypatch.setenv('NANNA_MODEL_API_KEY', 'test-only-key')
    monkeypatch.setenv('NANNA_MODEL_API_STYLE', 'responses')
    monkeypatch.setenv('NANNA_MODEL_NAME', 'restore-test-model')
    clock = daily._now
    monkeypatch.setattr(daily, '_now', lambda value=None: clock(value if value is not None else now))

    def activate(directory, *, fresh=False):
        monkeypatch.setenv('NANNA_DATA_DIR', str(directory))
        monkeypatch.setattr(state, 'settings', state.load_settings())
        state.prepare_runtime()
        if fresh:
            schema.migrate()
            (directory / 'automation_sources.json').write_text(json.dumps(definition), encoding='utf-8')
            (directory / 'source_catalog.json').write_text('[]', encoding='utf-8')
            daily.initialize_sources()

    def fetch(request_url, **kwargs):
        if request_url == root:
            text = ('<html><title>南京大学测试学院通知公告</title><body>'
                    '<a href="/info/1001/1.htm">公开通知</a></body></html>')
        elif request_url == url:
            text = ('<html><head><title>公开通知</title><meta name="pubdate" content="2026-10-14"></head>'
                    '<body><article><h1>公开通知</h1><p>这是一篇公开的校园活动通知，保留原始来源证据，不推测未说明的日期和学院关联。'
                    '本次活动面向全校师生开放，具体安排以学院公开发布的正文和后续通知为准。'
                    '参与者应自行阅读报名要求，并在参加前核对最新的场地、时间与交通信息。'
                    '本文仅用于本地恢复防重回归，不连接校园网站或任何付费模型端点。</p></article></body></html>')
        else:
            raise AssertionError(f'Unexpected public fetch: {request_url}')
        return text.encode(), 'text/html; charset=utf-8', request_url

    def model(payload, *, audit=None):
        boundary.posts.append(payload)
        if boundary.failure is not None:
            raise boundary.failure
        source = json.loads(payload['input'][0]['content'][0]['text'].split('\n', 1)[1])
        output = {'items': [{'title': source['title'], 'description': source['text'], 'tag': '校级活动'}]}
        return {'status': 'completed', 'error': None, 'incomplete_details': None,
                'output': [{'type': 'message', 'role': 'assistant', 'status': 'completed',
                            'content': [{'type': 'output_text', 'text': json.dumps(output, ensure_ascii=False)}]}]}

    monkeypatch.setattr(acquisition, 'fetch_public_resource', fetch)
    monkeypatch.setattr(acquisition, '_model_post', model)
    boundary.activate = activate
    activate(state.settings.data_dir, fresh=True)
    return boundary


@pytest.mark.parametrize('location', ['existing', 'absent', 'id_collision'])
@pytest.mark.parametrize('outcome', ['completed', 'failed', 'uncertain'])
@pytest.mark.parametrize('paid_path', ['collection', 'source'])
def test_restore_blocks_newer_native_paid_attempts_by_source_content_identity(
        state, tmp_path, native_restore_boundary, location, outcome, paid_path):
    from nannanews import acquisition, cli, daily_acquisition as daily, information

    boundary = native_restore_boundary
    captured = acquisition.fetch_document(boundary.url)
    if location == 'existing':
        information.save_document(captured, 'restore-source', 'owner')
    elif location == 'id_collision':
        unrelated = dict(captured, canonical_url='https://college.nju.edu.cn/info/1001/999.htm',
                         text='备份分支上不同的原文，不得继承另一个原文的付费状态。')
        unrelated_id = information.save_document(unrelated, None, 'owner')['id']
        with closing(state.connect_db()) as db, db:
            backup_run = db.execute("INSERT INTO daily_runs(actor,started_at,status,mode) VALUES('backup-branch','2026-10-01','complete','new')").lastrowid
            backup_entry = db.execute("INSERT INTO entries(uploader,upload_time,title,status) VALUES('owner','2026-10-01','Unrelated backup entry','approved')").lastrowid
    backup = cli.backup(tmp_path / 'before-native-payment.zip')
    if location == 'id_collision':
        # A different live lineage reuses the same numeric document/run IDs.
        # The archive's unrelated source and run must remain untouched.
        boundary.activate(tmp_path / 'current-lineage', fresh=True)

    if outcome == 'failed':
        boundary.failure = acquisition.AcquisitionError('Provider rejected the request', 'model_http_error', 502)
    elif outcome == 'uncertain':
        boundary.failure = KeyboardInterrupt()
    if paid_path == 'collection':
        if outcome == 'uncertain':
            with pytest.raises(KeyboardInterrupt):
                daily.run_once(now=boundary.now)
        else:
            daily.run_once(now=boundary.now)
    else:
        saved = information.save_document(captured, 'restore-source', 'owner')
        if outcome == 'completed':
            information.enrich_document(saved['id'], 'owner')
        else:
            with pytest.raises(KeyboardInterrupt if outcome == 'uncertain' else acquisition.AcquisitionError):
                information.enrich_document(saved['id'], 'owner')
    assert len(boundary.posts) == 1
    audit_root = state.settings.data_dir / 'model-audit'
    original_requests = {path.relative_to(audit_root): path.read_bytes() for path in audit_root.rglob('request.json')}
    assert len(original_requests) == 1
    with closing(state.connect_db()) as db:
        original_document = db.execute('SELECT id,digest,document_json,model_status FROM source_documents WHERE source_url=?', (boundary.url,)).fetchone()
        original_attempt = db.execute('''SELECT actor,started_at,finished_at,model_status,model_error,model_name,output_json,response_json
            FROM source_document_model_attempts WHERE document_id=?''', (original_document[0],)).fetchone()
    if location == 'id_collision':
        assert original_document[0] == unrelated_id

    restored = cli.restore(backup, tmp_path / 'native-restored')
    for path, request in original_requests.items():
        assert (restored / 'model-audit' / path).read_bytes() == request
    with closing(sqlite3.connect(restored / 'database.db')) as db:
        restored_document = db.execute('SELECT id,digest,document_json,model_status FROM source_documents WHERE source_url=?', (boundary.url,)).fetchone()
        assert restored_document[1:] == original_document[1:]
        assert db.execute('''SELECT actor,started_at,finished_at,model_status,model_error,model_name,output_json,response_json
            FROM source_document_model_attempts WHERE document_id=?''', (restored_document[0],)).fetchone() == original_attempt
        if location == 'id_collision':
            assert restored_document[0] != unrelated_id
            assert db.execute('SELECT model_status FROM source_documents WHERE id=?', (unrelated_id,)).fetchone()[0] == 'not_extracted'
            assert db.execute('SELECT actor FROM daily_runs WHERE id=?', (backup_run,)).fetchone()[0] == 'backup-branch'
            assert db.execute('SELECT title,status FROM entries WHERE id=?', (backup_entry,)).fetchone() == ('Unrelated backup entry', 'approved')
        assert db.execute('SELECT COUNT(*) FROM acquisitions').fetchone()[0] == 0
        if paid_path == 'collection':
            run_id, status = db.execute('SELECT run_id,status FROM daily_model_attempts WHERE document_id=?', (restored_document[0],)).fetchone()
            assert status == {'completed': 'done', 'failed': 'failed', 'uncertain': 'uncertain'}[outcome]
            if location == 'id_collision':
                assert run_id != backup_run

    boundary.activate(restored)
    boundary.failure = None
    # Capture again through the real canonical store: absent-in-backup documents
    # must resolve to their preserved prevention identity, not become unpaid rows.
    revisited = information.save_document(acquisition.fetch_document(boundary.url), 'restore-source', 'owner')
    assert revisited['id'] == restored_document[0] and not revisited['created']
    if outcome == 'uncertain':
        with pytest.raises(acquisition.AcquisitionError, match='不会重复调用模型'):
            information.enrich_document(revisited['id'], 'owner')
    elif outcome == 'completed':
        assert information.enrich_document(revisited['id'], 'owner')['model_status'] == 'completed'
    daily.run_once(now=boundary.now)
    assert len(boundary.posts) == 1
    with closing(state.connect_db()) as db:
        assert db.execute('SELECT state FROM daily_links WHERE url=?', (boundary.url,)).fetchone()[0] == {
            'completed': 'done', 'failed': 'model_failed', 'uncertain': 'model_uncertain'}[outcome]
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts WHERE document_id=?', (revisited['id'],)).fetchone()[0] == 1


def test_conflicting_native_outcomes_refuse_restore_without_creating_target(
        state, tmp_path, native_restore_boundary):
    from nannanews import acquisition, cli, information

    boundary = native_restore_boundary
    saved = information.save_document(acquisition.fetch_document(boundary.url), 'restore-source', 'owner')
    information.enrich_document(saved['id'], 'owner')
    backup = cli.backup(tmp_path / 'completed-native-request.zip')
    with closing(state.connect_db()) as db, db:
        db.execute("UPDATE source_document_model_attempts SET output_json=?", ('{"conflicting":"private result"}',))
    target = tmp_path / 'conflicting-native-restore'
    with pytest.raises(ValueError, match='Conflicting native model attempt outcomes'):
        cli.restore(backup, target)
    assert not target.exists()
    with closing(state.connect_db()) as db:
        assert db.execute('SELECT output_json FROM source_document_model_attempts').fetchone()[0] == '{"conflicting":"private result"}'
    assert len(boundary.posts) == 1
