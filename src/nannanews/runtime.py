"""Private, relocatable runtime state shared by the app and management CLI."""
from contextlib import closing
from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import secrets
import tempfile
import re
from urllib.parse import urlsplit

CODE_DIR = Path(__file__).resolve().parent
ROLES = ("admin", "editor", "member")
ENTRY_COLUMNS = (
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("uploader", "TEXT NOT NULL"), ("upload_time", "TIMESTAMP NOT NULL"),
    ("title", "TEXT NOT NULL"), ("link", "TEXT"), ("description", "TEXT"),
    ("describer", "TEXT"), ("reviewer", "TEXT"), ("due_time", "TIMESTAMP"),
    ("status", "TEXT"), ("type", "TEXT"), ("locked_by", "TEXT"),
    ("lock_time", "TIMESTAMP"), ("use_image", "INTEGER DEFAULT 0"),
    ("publish_date", "TIMESTAMP DEFAULT NULL"), ("tag", "TEXT"),
    ("short_title", "TEXT"), ("lecture_data", "TEXT"),
    ("priority", "INTEGER DEFAULT 0"),
)


class ConfigurationError(ValueError):
    """Invalid operator-supplied configuration, without exposing secret values."""


def _env(name, default=""):
    return os.environ.get("NANNA_" + name, default).strip()


def _bool(name, default=False):
    value = _env(name, "true" if default else "false").lower()
    if value not in ("true", "false", "1", "0", "yes", "no"):
        raise ConfigurationError(f"NANNA_{name} must be true or false")
    return value in ("true", "1", "yes")


def _number(name, default, conversion=int):
    try:
        value = conversion(_env(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"NANNA_{name} must be a positive number") from exc
    if value <= 0 or value == float("inf") or value != value:
        raise ConfigurationError(f"NANNA_{name} must be a finite positive number")
    return value


def _url(name, default=""):
    value = _env(name, default)
    parsed = urlsplit(value)
    if value and (parsed.scheme not in ("http", "https") or not parsed.netloc
                  or parsed.username or parsed.password or parsed.fragment):
        raise ConfigurationError(f"NANNA_{name} must be an HTTP(S) URL without credentials or fragment")
    return value.rstrip("/")


def _private_directory(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)


def _session_secret(data_dir, *, create=False):
    configured = _env("SECRET_KEY")
    if configured:
        if len(configured) < 32:
            raise ConfigurationError("NANNA_SECRET_KEY must contain at least 32 characters")
        return configured
    secret_file = data_dir / "session.key"
    if secret_file.is_symlink():
        raise ConfigurationError("session.key must not be a symlink")
    if not secret_file.exists():
        if not create:
            return ""
        descriptor, candidate = tempfile.mkstemp(prefix=".session-", dir=data_dir)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(secrets.token_urlsafe(48) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(candidate, secret_file)
            except FileExistsError:
                pass
        finally:
            Path(candidate).unlink(missing_ok=True)
    if secret_file.is_symlink():
        raise ConfigurationError("session.key must not be a symlink")
    if create:
        secret_file.chmod(0o600)
    value = secret_file.read_text(encoding="ascii").strip()
    if len(value) < 32:
        raise ConfigurationError("session.key is invalid; restore a valid secret or configure NANNA_SECRET_KEY")
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    upload_dir: Path
    publication_dir: Path
    archive_dir: Path
    lecture_archive_dir: Path
    session_secret: str = field(repr=False)
    site_name: str = "公共消息"
    public_url: str = "http://127.0.0.1:8080"
    qq_group_url: str = ""
    qq_group_label: str = "订阅"
    typst_binary: str = "typst"
    typst_package_cache: Path | None = None
    typst_font_dir: Path | None = None
    pdf_parser_binary: str = 'lit'
    host: str = "127.0.0.1"
    port: int = 8080
    session_cookie_secure: bool = False
    model_base_url: str = ""
    model_api_key: str = field(default="", repr=False)
    model_name: str = 'gpt-6-luna'
    model_timeout: float = 90.0
    model_api_style: str = 'responses'
    collection_enabled: bool = False
    collection_time: str = '04:00'
    collection_page_limit: int = 10
    collection_fetch_workers: int = 1


def load_settings():
    default = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "nannanews"
    data_dir = Path(os.path.abspath(Path(_env("DATA_DIR") or str(default)).expanduser()))
    directories = {name: data_dir / dirname for name, dirname in (
        ("upload_dir", "uploads"), ("publication_dir", "publications"),
        ("archive_dir", "archives"), ("lecture_archive_dir", "lecture_archives"),
    )}
    port = _number("PORT", 8080)
    if port > 65535:
        raise ConfigurationError("TCP ports must be between 1 and 65535")
    style = _env('MODEL_API_STYLE', 'responses')
    if style not in ("chat_completions", "responses"):
        raise ConfigurationError("NANNA_MODEL_API_STYLE must be chat_completions or responses")
    collection_time = _env('COLLECTION_TIME', '04:00')
    if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', collection_time):
        raise ConfigurationError('NANNA_COLLECTION_TIME must be HH:MM Beijing time')
    collection_fetch_workers = _number('COLLECTION_FETCH_WORKERS', 1)
    if collection_fetch_workers > 6:
        raise ConfigurationError('NANNA_COLLECTION_FETCH_WORKERS must be between 1 and 6')
    def optional_path(name):
        value = _env(name)
        if not value:
            return None
        result = Path(value).expanduser()
        return (result if result.is_absolute() else CODE_DIR / result).resolve()
    return Settings(
        data_dir=data_dir, database_path=data_dir / "database.db", **directories,
        session_secret=_session_secret(data_dir),
        site_name=_env("SITE_NAME", "公共消息"),
        public_url=_url("PUBLIC_URL", "http://127.0.0.1:8080"),
        qq_group_url=_url("QQ_GROUP_URL"), qq_group_label=_env("QQ_GROUP_LABEL", "订阅"),
        typst_binary=_env("TYPST_BINARY", "typst"),
        typst_package_cache=optional_path("TYPST_PACKAGE_CACHE"),
        typst_font_dir=optional_path("TYPST_FONT_DIR"),
        pdf_parser_binary=_env('PDF_PARSER_BINARY', 'lit'),
        host=_env("HOST", "127.0.0.1"), port=port,
        session_cookie_secure=_bool("SESSION_COOKIE_SECURE"),
        model_base_url=_url("MODEL_BASE_URL"), model_api_key=_env("MODEL_API_KEY"),
        model_name=_env('MODEL_NAME', 'gpt-6-luna'),
        model_timeout=_number("MODEL_TIMEOUT", 90, float), model_api_style=style,
        collection_enabled=_bool('COLLECTION_ENABLED'), collection_time=collection_time,
        collection_page_limit=_number('COLLECTION_PAGE_LIMIT', 10),
        collection_fetch_workers=collection_fetch_workers,
    )


settings = load_settings()


def prepare_runtime():
    """Explicitly prepare private directories and the persistent session key."""
    global settings
    for directory in (settings.data_dir, settings.upload_dir, settings.publication_dir,
                      settings.archive_dir, settings.lecture_archive_dir):
        if directory.is_symlink():
            raise ConfigurationError("Runtime directories must not be symlinks")
        _private_directory(directory)
    secret = _session_secret(settings.data_dir, create=True)
    settings = replace(settings, session_secret=secret)
    return settings


def path(relative):
    """Resolve a data-relative path; reject traversal and absolute paths."""
    relative = Path(relative)
    if relative.is_absolute():
        raise ValueError("Expected a relative runtime path")
    result = (settings.data_dir / relative).resolve()
    if not result.is_relative_to(settings.data_dir):
        raise ValueError("Runtime path escapes data directory")
    return result


def connect_db():
    """Connect only to a current database; callers own transactions and close."""
    from nannanews import schema
    connection = schema.open_database()
    try:
        schema.require_schema(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def _migrate_core(connection):
    """Append only missing historical columns; preserve rows and positional APIs."""
    connection.execute("CREATE TABLE IF NOT EXISTS users ("
                       "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, "
                       "role TEXT NOT NULL DEFAULT 'member' "
                       "CHECK(role IN ('admin', 'editor', 'member')))")
    user_columns = [row[1] for row in connection.execute("PRAGMA table_info(users)")]
    if user_columns[:3] != ["id", "username", "password"]:
        raise ConfigurationError("Existing users schema is incompatible; no migration performed")
    if "role" not in user_columns:
        if len(user_columns) != 3:
            raise ConfigurationError("Cannot append role immediately after password; migrate custom schema explicitly")
        connection.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member' "
                           "CHECK(role IN ('admin', 'editor', 'member'))")
    elif user_columns[3] != "role":
        raise ConfigurationError("users.role must follow password")
    connection.execute("CREATE TABLE IF NOT EXISTS entries (" +
                       ", ".join(f'"{name}" {ddl}' for name, ddl in ENTRY_COLUMNS) + ")")
    columns = [row[1] for row in connection.execute("PRAGMA table_info(entries)")]
    expected = [name for name, _ in ENTRY_COLUMNS]
    if columns[:19] != expected[:len(columns[:19])]:
        raise ConfigurationError("Existing entries column order is incompatible; no migration performed")
    for name, ddl in ENTRY_COLUMNS[len(columns):]:
        connection.execute(f'ALTER TABLE entries ADD COLUMN "{name}" {ddl}')
    connection.execute("UPDATE entries SET status='approved' WHERE lower(trim(status))='approved'")
    connection.execute("CREATE INDEX IF NOT EXISTS entries_publication ON entries(status, publish_date)")



def get_user_role(username):
    with closing(connect_db()) as connection:
        row = connection.execute("SELECT role FROM users WHERE username=?", (username,)).fetchone()
    return row[0] if row and row[0] in ROLES else None
