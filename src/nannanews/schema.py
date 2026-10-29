"""Explicit, versioned migrations; ordinary connections only validate metadata.

Stop file writers and take a coherent backup before ``nannanews migrate``.
Migrations never invoke a model, touch the paid-attempt ledger, or downgrade data.
"""
from contextlib import closing
import os
import sqlite3

from nannanews import runtime

CURRENT_VERSION = 3


def open_database(*, readonly=False, create=False):
    """Open the configured main database without accidentally creating it."""
    path = runtime.settings.database_path
    if path.is_symlink():
        raise runtime.ConfigurationError("database.db must not be a symlink")
    if not create and not path.is_file():
        raise runtime.ConfigurationError("Database is uninitialized; run `nannanews migrate` then `nannanews init-admin USER`")
    mode = "rwc" if create else "ro" if readonly else "rw"
    connection = sqlite3.connect(path.absolute().as_uri() + "?mode=" + mode, uri=True, timeout=30)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _version(connection):
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nannanews_schema'").fetchone()
    if not exists:
        return 0
    rows = connection.execute("SELECT version FROM nannanews_schema WHERE id=1").fetchall()
    if len(rows) != 1 or type(rows[0][0]) is not int or rows[0][0] < 1:
        raise runtime.ConfigurationError("Invalid schema metadata; restore a supported backup before migration")
    return rows[0][0]


def require_schema(connection=None):
    """Read-only version guard, returning the supported version or actionable error."""
    if connection is None:
        with closing(open_database(readonly=True)) as database:
            return require_schema(database)
    version = _version(connection)
    if version < CURRENT_VERSION:
        raise runtime.ConfigurationError(
            f"Database schema {version} is outdated (requires {CURRENT_VERSION}); stop writers, back up, then run `nannanews migrate`")
    if version > CURRENT_VERSION:
        raise runtime.ConfigurationError(
            f"Database schema {version} is newer than supported {CURRENT_VERSION}; use compatible NannaNews code, never downgrade the database")
    return version


def execute_script(connection, script):
    """Execute DDL including trigger bodies without executescript's implicit commit."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("Incomplete migration SQL statement")


def _seed_catalog():
    source = runtime.CODE_DIR / "data" / "source_catalog.json"
    target = runtime.settings.data_dir / "source_catalog.json"
    if not source.is_file() or target.exists():
        return
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(descriptor, "wb") as destination:
        destination.write(source.read_bytes())


def migrate():
    """Prepare runtime and atomically migrate once; repeat calls skip all backfills."""
    runtime.prepare_runtime()
    from nannanews import acquisition, bulletin_store, daily_acquisition, information
    with closing(open_database(create=True)) as connection:
        runtime.settings.database_path.chmod(0o600)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            version = _version(connection)
            if version > CURRENT_VERSION:
                require_schema(connection)
            if version < 1:
                runtime._migrate_core(connection)
                acquisition._migrate_schema(connection)
                information._migrate_schema(connection)
                bulletin_store._migrate_schema(connection)
                daily_acquisition._migrate_schema(connection)
            if version < 2:
                from nannanews.reader_reviews import _migrate_schema
                _migrate_schema(connection)
            if version < 3:
                from nannanews.reader_reviews import _migrate_private_schema
                _migrate_private_schema(connection)
            if version < CURRENT_VERSION:
                connection.execute("CREATE TABLE IF NOT EXISTS nannanews_schema (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)")
                connection.execute("INSERT INTO nannanews_schema(id,version) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version", (CURRENT_VERSION,))
    _seed_catalog()
    return CURRENT_VERSION
