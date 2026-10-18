"""Explicit lifecycle boundaries, using only temporary local state and SQLite."""
from contextlib import closing
import os
import sqlite3
import subprocess
import sys

import pytest


@pytest.fixture
def unprepared(tmp_path, monkeypatch):
    from nannanews import runtime
    monkeypatch.setenv("NANNA_DATA_DIR", str(tmp_path / "unprepared"))
    monkeypatch.delenv("NANNA_SECRET_KEY", raising=False)
    monkeypatch.setattr(runtime, "settings", runtime.load_settings())
    return runtime


def _process(runtime, *arguments):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(runtime.CODE_DIR.parent)
    return subprocess.run([sys.executable, *arguments], env=environment,
                          capture_output=True, text=True, timeout=30)


def test_imports_and_configuration_do_not_prepare_state(unprepared):
    result = _process(unprepared, "-c", "from nannanews import runtime, cli, schema, acquisition, information, bulletin_store, daily_acquisition; runtime.load_settings()")
    assert result.returncode == 0, result.stderr
    assert not unprepared.settings.data_dir.exists()
    assert unprepared.settings.session_secret == ""


@pytest.mark.parametrize("command", [[], ["migrate"], ["init-admin"], ["serve"],
    ["collect-worker"], ["bulletin-query"], ["import-bulletins"], ["import-source-catalog"]])
def test_cli_help_never_prepares_state(unprepared, command):
    result = _process(unprepared, "-m", "nannanews", *command, "--help")
    assert result.returncode == 0, result.stderr
    assert not unprepared.settings.data_dir.exists()


def test_configuration_ignores_retired_mail_and_sync_settings(unprepared, monkeypatch):
    for name in ("EMAIL_ENABLED", "SMTP_PORT", "LECTURE_SYNC_ENABLED", "LECTURE_SYNC_URL"):
        monkeypatch.setenv("NANNA_" + name, "invalid-retired-value")
    settings = unprepared.load_settings()
    assert not hasattr(settings, "email_enabled")
    assert not hasattr(settings, "lecture_sync_enabled")
    assert not settings.data_dir.exists()


@pytest.mark.parametrize("configured", [None, "", " \t "])
def test_default_state_lives_outside_installed_package(unprepared, tmp_path, monkeypatch, configured):
    if configured is None:
        monkeypatch.delenv("NANNA_DATA_DIR")
    else:
        monkeypatch.setenv("NANNA_DATA_DIR", configured)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    settings = unprepared.load_settings()
    assert settings.data_dir == tmp_path / "data" / "nannanews"
    assert not settings.data_dir.exists()


def test_missing_database_is_rejected_without_creating_anything(unprepared):
    from nannanews import schema, daily_acquisition
    for operation in (schema.require_schema, unprepared.connect_db,
                      daily_acquisition.get_status, daily_acquisition.list_items):
        with pytest.raises(unprepared.ConfigurationError, match="nannanews migrate"):
            operation()
    result = _process(unprepared, "-m", "nannanews", "query")
    assert result.returncode == 2
    assert "nannanews migrate" in result.stderr
    assert not unprepared.settings.data_dir.exists()


def test_prepare_is_explicit_and_does_not_create_a_database(unprepared):
    first = unprepared.prepare_runtime()
    assert not first.database_path.exists()
    assert first.session_secret
    assert unprepared.load_settings().session_secret == first.session_secret
    assert unprepared.prepare_runtime().session_secret == first.session_secret
    assert (first.data_dir / "session.key").stat().st_mode & 0o777 == 0o600
    assert first.data_dir.stat().st_mode & 0o777 == 0o700


def test_migrate_command_bootstraps_all_domains_but_no_admin(unprepared, capsys):
    from nannanews import cli, schema
    assert cli.main(["migrate"]) == 0
    assert "version 1" in capsys.readouterr().out
    assert schema.require_schema() == schema.CURRENT_VERSION
    with closing(unprepared.connect_db()) as db:
        assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 0
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"entries", "acquisitions", "information_records", "bulletin_occurrences", "daily_runs"} <= tables


def test_current_migration_and_storage_access_skip_all_historical_backfills(unprepared, monkeypatch):
    from nannanews import schema, acquisition, information, bulletin_store, daily_acquisition
    from nannanews.bulletin_historical_attempts import AttemptStore, AttemptBlocked
    import hashlib
    schema.migrate()
    with closing(unprepared.connect_db()) as db, db:
        db.execute("INSERT INTO entries(uploader,upload_time,title,status) VALUES('owner','2026-10-01','Protected','described')")
        db.execute("INSERT INTO source_documents(source_url,digest,document_json,title,content_text,fetched_at,captured_by,model_status) VALUES('https://example.org/one','one','{}','One','Public','2026-10-01','owner','extracting')")
        document_id = db.execute("SELECT id FROM source_documents").fetchone()[0]
        db.execute("INSERT INTO source_document_model_attempts(document_id,actor,started_at,model_status) VALUES(?,'owner','2026-10-01','extracting')", (document_id,))
        db.execute("INSERT INTO daily_links(source_id,url,state,eligible,first_seen,last_seen,document_id) VALUES('source','https://example.org/one','model_uncertain',0,'2026-10-01','2026-10-01',?)", (document_id,))
    ledger = AttemptStore(unprepared.settings.database_path)
    request = '{"request":"paid-once"}'
    digest = hashlib.sha256(request.encode()).hexdigest()
    ledger.reserve(digest, request)
    ledger_bytes = ledger.path.read_bytes()
    database_bytes = unprepared.settings.database_path.read_bytes()
    def unexpected_backfill(*args):
        raise AssertionError("Current schema must never run a historical migration")
    for module, name in ((unprepared, "_migrate_core"), (acquisition, "_migrate_schema"),
                         (information, "_migrate_schema"), (bulletin_store, "_migrate_schema"),
                         (daily_acquisition, "_migrate_schema"), (information, "_backfill")):
        monkeypatch.setattr(module, name, unexpected_backfill)
    assert schema.migrate() == schema.CURRENT_VERSION
    schema.require_schema()
    information.list_sources()
    daily_acquisition.get_status()
    daily_acquisition.list_items()
    assert unprepared.settings.database_path.read_bytes() == database_bytes
    assert ledger.path.read_bytes() == ledger_bytes
    with pytest.raises(AttemptBlocked):
        ledger.reserve(digest, request)
    with closing(unprepared.connect_db()) as db:
        assert db.execute("SELECT status FROM entries").fetchone()[0] == "described"
        assert db.execute("SELECT model_status FROM source_documents").fetchone()[0] == "extracting"
        assert db.execute("SELECT model_status FROM source_document_model_attempts").fetchone()[0] == "extracting"
        assert db.execute("SELECT state FROM daily_links").fetchone()[0] == "model_uncertain"


def test_failed_late_migration_rolls_back_every_domain_and_version(unprepared, monkeypatch):
    from nannanews import schema, daily_acquisition
    unprepared.prepare_runtime()
    with closing(sqlite3.connect(unprepared.settings.database_path)) as db, db:
        db.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, password TEXT)")
        db.execute("INSERT INTO users VALUES(7,'owner','original-hash')")
    def fail_after_ddl(db):
        schema.execute_script(db, "CREATE TABLE migration_partial(id INTEGER);\n")
        raise RuntimeError("deliberate migration interruption")
    monkeypatch.setattr(daily_acquisition, "_migrate_schema", fail_after_ddl)
    with pytest.raises(RuntimeError, match="interruption"):
        schema.migrate()
    with closing(sqlite3.connect(unprepared.settings.database_path)) as db:
        assert [row[1] for row in db.execute("PRAGMA table_info(users)")] == ["id", "username", "password"]
        assert db.execute("SELECT * FROM users").fetchone() == (7, "owner", "original-hash")
        assert {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"users"}
    with pytest.raises(unprepared.ConfigurationError, match="migrate"):
        schema.require_schema()


@pytest.mark.parametrize("version", ["legacy", "future"])
def test_schema_guards_reject_old_and_new_databases_without_writes(unprepared, version):
    from nannanews import schema
    schema.migrate()
    with closing(sqlite3.connect(unprepared.settings.database_path)) as db, db:
        if version == "legacy":
            db.execute("DROP TABLE nannanews_schema")
        else:
            db.execute("UPDATE nannanews_schema SET version=?", (schema.CURRENT_VERSION + 1,))
    before = unprepared.settings.database_path.read_bytes()
    with pytest.raises(unprepared.ConfigurationError, match="migrate" if version == "legacy" else "newer"):
        schema.require_schema()
    with pytest.raises(unprepared.ConfigurationError):
        unprepared.connect_db()
    if version == "future":
        with pytest.raises(unprepared.ConfigurationError, match="newer"):
            schema.migrate()
    assert unprepared.settings.database_path.read_bytes() == before


def test_catalog_cli_is_independent_of_runtime_database(unprepared, tmp_path, capsys):
    from nannanews import cli
    reviewed = tmp_path / "reviewed.json"
    reviewed.write_text("[]", encoding="utf-8")
    output = tmp_path / "public.json"
    assert cli.main(["import-source-catalog", str(reviewed), str(output)]) == 0
    assert output.is_file()
    assert "0 public accounts" in capsys.readouterr().out
    assert not unprepared.settings.data_dir.exists()


def test_serve_uses_real_factory_without_running_migrations(unprepared, monkeypatch):
    from nannanews import cli, schema
    import waitress
    cli.create_user("owner", "a-long-private-password", "admin", initial_admin=True)
    def unexpected_migration():
        raise AssertionError("Serve must validate, not migrate")
    monkeypatch.setattr(schema, "migrate", unexpected_migration)
    observed = []
    monkeypatch.setattr(waitress, "serve", lambda app, **options: observed.append((app, options)))
    assert cli.main(["serve", "--host", "127.0.0.1", "--port", "8088"]) == 0
    assert len(observed) == 1
    app, options = observed[0]
    assert app.secret_key == unprepared.settings.session_secret
    assert options == {"host": "127.0.0.1", "port": 8088, "threads": 4, "expose_tracebacks": False}
