"""Explicit native restic archives of a private foundation and its complete graph.

SQLite remains the catalog; restic is the only archive format. Imports and config
reads do not initialize anything. Failed operations never authorize source deletion.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import tempfile

from nannanews.data_foundation import _json, _no_symlinks, _open_existing


_CONFIG = "restic_archive:v1"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_CHUNK = 1024 * 1024
_TIMEOUT = 24 * 60 * 60
_PROTECTED = frozenset({"original", "originals", "source", "sources", "public",
                        "reader", "uploads", "credentials", ".git"})


def _now():
    return datetime.now(timezone.utc).isoformat()


def _path(value):
    path = Path(os.path.abspath(Path(value).expanduser()))
    _no_symlinks(path)
    return path


def _overlaps(left, right):
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _stream_file(source, *, output=None):
    """Copy/hash a stable regular file, rejecting symlinks and replacement races."""
    source = _path(source)
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Archive input must be a regular file.")
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(_CHUNK), b""):
            digest.update(chunk)
            if output is not None:
                output.write(chunk)
        after = os.fstat(stream.fileno())
        _no_symlinks(source)
        if _identity(before) != _identity(after) or _identity(after) != _identity(source.stat()):
            raise ValueError("Archive input changed while reading.")
    return digest.hexdigest(), after.st_size




@contextmanager
def _output(target, digest, size, *, sync_parent=True):
    """Pin the output directory and publish only verified, fsynced private bytes."""
    target = _path(target)
    parent = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".restic-output-" + secrets.token_hex(24)
    created = False
    try:
        descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        created = True
        with os.fdopen(descriptor, "w+b") as stream:
            yield stream
            stream.flush()
            stream.seek(0)
            actual = hashlib.sha256()
            count = 0
            for chunk in iter(lambda: stream.read(_CHUNK), b""):
                actual.update(chunk)
                count += len(chunk)
            if (actual.hexdigest(), count) != (digest, size):
                raise ValueError("Archived blob failed exact SHA-256/size verification.")
            os.fsync(stream.fileno())
            _no_symlinks(target)
            bound, current = os.fstat(parent), target.parent.stat()
            if (bound.st_dev, bound.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("Archive output directory changed before publication.")
            if _identity(os.fstat(stream.fileno())) != _identity(
                    os.stat(temporary, dir_fd=parent, follow_symlinks=False)):
                raise ValueError("Archive temporary file changed before publication.")
            os.link(temporary, target.name, src_dir_fd=parent, dst_dir_fd=parent,
                    follow_symlinks=False)
            if sync_parent:
                os.fsync(parent)
            _no_symlinks(target)
            current = target.parent.stat()
            if (bound.st_dev, bound.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("Archive output directory changed during publication.")
    finally:
        try:
            if created:
                os.unlink(temporary, dir_fd=parent)
        finally:
            os.close(parent)


def _copy_blob(source, destination, digest, size):
    """Copy stable input into an exclusively published exact-byte cache file."""
    with _output(destination, digest, size) as stream:
        if _stream_file(source, output=stream) != (digest, size):
            raise ValueError("Blob SHA-256 or size does not match its catalog.")


def _write_db(root):
    # mode=rw is deliberate: explicit archive operations cannot create/migrate DBs.
    with closing(_open_existing(root)):
        pass
    path = _path(root) / "foundation.sqlite3"
    db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


@contextmanager
def _locked(root):
    root = _path(root)
    with closing(_open_existing(root)):
        pass
    descriptor = os.open(root / ".restic-archive.lock",
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield root
    finally:
        os.close(descriptor)


def archive_config(root: Path) -> dict | None:
    """Return persisted native configuration, without creating files or directories."""
    root = _path(root)
    if not (root / "foundation.sqlite3").exists():
        return None
    with closing(_open_existing(root)) as db:
        return _read_archive_config(db)


def _read_archive_config(db):
    row = db.execute("SELECT value FROM metadata WHERE key=?", (_CONFIG,)).fetchone()
    if row is None:
        return None
    config = json.loads(row[0])
    if (not isinstance(config, dict) or config.get("version") != 1
            or not _SHA.fullmatch(str(config.get("dataset_id", "")))
            or not _SHA.fullmatch(str(config.get("repository_id", "")))
            or config.get("tag") != "nannanews-foundation:" + config["dataset_id"]
            or not all(isinstance(config.get(key), str) and config[key]
                       for key in ("repository", "password_file", "restic_binary", "repository_id"))):
        raise ValueError("Invalid native archive configuration.")
    return config




def _require_config(root):
    config = archive_config(root)
    if config is None:
        raise ValueError("Native archive has not been explicitly configured.")
    _validate_locations(root, config['repository'], Path(config['password_file']))
    _password(Path(config["password_file"]), create=False)
    return config


def _remote(repository):
    if not repository.startswith("rclone:"):
        return False
    if not repository.startswith("rclone:nju:"):
        raise ValueError("Only the dedicated native rclone:nju archive namespace is allowed.")
    parts = repository[len("rclone:nju:"):].split("/")
    current = len(parts) >= 3 and parts[:2] == ["NannaNews", "activity-snapshots"]
    if (not current or any(part in {"", ".", ".."} for part in parts)
            or any(character.isspace() or ord(character) < 32 for character in repository)
            or "\\" in repository or ":" in repository[len("rclone:nju:"):]):
        raise ValueError("Invalid dedicated native remote repository.")
    return True


def _validate_locations(root, repository, password):
    root, password = _path(root), _path(password)
    local = None if _remote(repository) else _path(repository)
    if local is not None and (":" in repository or repository.startswith("-")):
        raise ValueError("Repository must be a local path or dedicated native rclone URI.")
    candidates = [password] + ([local] if local is not None else [])
    project = Path(__file__).resolve().parents[2]
    protected = [root, project]
    with closing(_open_existing(root)) as db:
        protected.extend(Path(row[0]).expanduser().resolve() for row in db.execute("SELECT path FROM originals") if row[0])
        protected.extend(Path(row[0]).expanduser().resolve() for row in db.execute(
            "SELECT local_path FROM blobs WHERE owned_local=0 AND local_path IS NOT NULL"))
    for candidate in candidates:
        if (any(_overlaps(candidate, location) for location in protected)
                or any(part.lower() in _PROTECTED for part in candidate.parts)):
            raise ValueError("Archive repository/key must be outside foundation, application and source data.")
    if local is not None and _overlaps(local, password):
        raise ValueError("Password file must be outside the native repository.")
    if local is not None and local.exists():
        info = local.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Native local repository must be a private owned directory.")
    return local


def _password(path, *, create):
    path = _path(path)
    if create and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write((secrets.token_urlsafe(48) + "\n").encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directory(path.parent)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size == 0):
            raise ValueError("Archive password file must be private, owned, nonempty and not linked.")
    finally:
        os.close(descriptor)


def _command(config, *arguments):
    command = [config["restic_binary"], "--repo", config["repository"],
               "--password-file", config["password_file"], "--no-cache"]
    if _remote(config['repository']):
        command.extend(["-o", "rclone.connections=1", "-o",
                        "rclone.args=serve restic --stdio --b2-hard-delete --disable-http2 --transfers 1 --checkers 1 --multi-thread-streams 0"])
    return command + list(arguments)


def _environment():
    # Do not let ambient restic password commands, repository overrides or hooks run.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("RESTIC_")}
    environment["GOMAXPROCS"] = "1"
    return environment


def _run(config, *arguments, cwd=None, allow_missing=False):
    # stdout/stderr never inherit the operator's terminal; raw errors may contain
    # private source names or backend credentials, so report only command/status.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        try:
            result = subprocess.run(_command(config, *arguments), cwd=cwd,
                                    env=_environment(), stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=error,
                                    timeout=config.get("operation_timeout", _TIMEOUT), check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("Native restic operation could not complete.") from None
        if result.returncode != 0:
            if allow_missing and result.returncode == 10:
                return None
            raise RuntimeError(f"Native restic {arguments[0]} failed (exit {result.returncode}).")
        output.seek(0)
        # Commands called here return metadata, not blob contents; backup progress
        # is parsed line by line rather than loaded along with file data.
        if arguments[0] == "backup":
            summary = None
            for line in output:
                event = json.loads(line)
                if event.get("message_type") == "error":
                    raise RuntimeError("Native restic backup reported an input error.")
                if event.get("message_type") == "summary":
                    summary = event
            if not summary or not _SHA.fullmatch(str(summary.get("snapshot_id", ""))):
                raise RuntimeError("Native restic did not publish a complete snapshot.")
            return summary
        if arguments[0] in {"cat", "snapshots"}:
            return json.load(output)
        return {"ok": True}


def _repository(config):
    identity = _run(config, "cat", "config")["id"]
    if identity != config["repository_id"]:
        raise ValueError("Native repository identity changed.")


def _save_config(root, config, *, proof=None):
    if proof is not None:
        config.setdefault("proofs", {})[proof["snapshot_id"]] = proof
    with closing(_write_db(root)) as db, db:
        db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)", (_CONFIG, _json(config)))


def configure_archive(root, *, repository: str | Path, password_file: Path,
                      restic_binary: str = "restic") -> dict:
    """Explicitly create/open a native repository; persist paths, never key bytes."""
    with _locked(root) as root:
        value = str(repository)
        if not value.startswith("rclone:"):
            value = str(_path(repository))
        password = _path(password_file)
        local = _validate_locations(root, value, password)
        if not isinstance(restic_binary, str) or not restic_binary or restic_binary.startswith("-"):
            raise ValueError("Invalid restic binary.")
        previous = archive_config(root)
        if previous and (previous["repository"] != value or previous["password_file"] != str(password)):
            raise ValueError("Refusing to replace an existing archive repository or key binding.")
        _password(password, create=True)
        dataset = previous['dataset_id'] if previous else hashlib.sha256(str(root).encode('utf-8')).hexdigest()
        config = dict(previous or {}, version=1, repository=value, password_file=str(password),
                      restic_binary=restic_binary, dataset_id=dataset,
                      tag="nannanews-foundation:" + dataset)
        # Existing local directories with no config may be initialized only if
        # empty. Other failures (including a wrong key) are never treated as init.
        new_local = local is not None and not (local / "config").exists()
        if new_local:
            if local.exists() and any(local.iterdir()):
                raise ValueError("Refusing to initialize a nonempty non-repository directory.")
            local.mkdir(parents=True, exist_ok=True, mode=0o700)
            local.chmod(0o700)
            _run(config, "init", "--json")
        else:
            existing = _run(config, "cat", "config", allow_missing=local is None)
            if existing is None:
                _run(config, "init", "--json")
        identity = _run(config, "cat", "config")["id"]
        if previous and previous["repository_id"] != identity:
            raise ValueError("Native repository identity changed.")
        config["repository_id"] = identity
        _save_config(root, config)
        return dict(config, ok=True)




def _blobs(catalog):
    with closing(_open_existing(catalog.parent)) as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM blobs ORDER BY sha256")]
    for row in rows:
        if (not _SHA.fullmatch(row["sha256"]) or not isinstance(row["size"], int)
                or row["size"] < 0):
            raise ValueError("Invalid blob identity in catalog.")
    return rows


def _seal(source, destination):
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(destination)) as target:
        source.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")
    with destination.open("rb") as stream:
        os.fsync(stream.fileno())


def _proof_for(root, config, snapshot_id=None):
    snapshot_id = snapshot_id or config.get("last_snapshot_id")
    if not isinstance(snapshot_id, str) or not _SHA.fullmatch(snapshot_id):
        raise ValueError("No explicit native backup snapshot is available.")
    proof = config.get("proofs", {}).get(snapshot_id)
    if not isinstance(proof, dict):
        raise ValueError("Snapshot has no recorded native backup for this dataset.")
    if (proof.get("snapshot_id") != snapshot_id or proof.get("repository_id") != config["repository_id"]
            or proof.get("dataset_id") != config["dataset_id"] or not proof.get("ok")):
        raise ValueError("Snapshot proof does not belong to the configured dataset/repository.")
    return proof


def _snapshot(config, snapshot_id):
    _repository(config)
    snapshots = _run(config, "snapshots", "--json", "--tag", config["tag"], snapshot_id)
    if (len(snapshots) != 1 or snapshots[0].get("id") != snapshot_id
            or config["tag"] not in snapshots[0].get("tags", [])):
        raise ValueError("Requested snapshot is not an exact snapshot of this dataset.")


def _check(config, *, full=False):
    _run(config, "check", *(["--read-data"] if full else []))


def _dump_to_stream(config, snapshot_id, path, stream):
    with tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(_command(config, "dump", snapshot_id, path),
                                    env=_environment(), stdin=subprocess.DEVNULL, stdout=stream,
                                    stderr=errors, timeout=_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("Native restic dump could not complete.") from None
        if result.returncode:
            raise RuntimeError(f"Native restic dump failed (exit {result.returncode}).")


def _catalog_path(proof):
    if proof.get("layout") == "source-paths":
        path = proof.get("catalog_path")
        if not isinstance(path, str) or not path.startswith("/") or ".." in Path(path).parts:
            raise ValueError("Invalid archived catalog path.")
        return path
    return "/payload/foundation.sqlite3"


def _blob_path(proof, row):
    if proof.get("layout") != "source-paths":
        return "/payload/blobs/" + row["sha256"]
    path = row["local_path"]
    if not isinstance(path, str) or not path.startswith("/") or ".." in Path(path).parts:
        raise ValueError("Invalid archived blob source path.")
    return path


def _fetch_catalog(config, proof, directory):
    path = directory / "foundation.sqlite3"
    with path.open("xb") as stream:
        _dump_to_stream(config, proof["snapshot_id"], _catalog_path(proof), stream)
    path.chmod(0o600)
    if proof.get("catalog_sha256") and _stream_file(path)[0] != proof["catalog_sha256"]:
        raise ValueError("Restored catalog differs from the sealed archived catalog.")
    with closing(_open_existing(directory)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Archived catalog failed SQLite integrity_check.")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Archived catalog has broken foreign-key associations.")
    return path




def _archive_locked(root, config):
    _repository(config)
    with tempfile.TemporaryDirectory(prefix=".restic-backup-", dir=root) as temporary, closing(_write_db(root)) as source:
        work = Path(temporary)
        version = source.execute("PRAGMA data_version").fetchone()[0]
        catalog = work / "foundation.sqlite3"
        _seal(source, catalog)
        rows = _blobs(catalog)
        paths = {str(catalog)}
        identities = {}
        for row in rows:
            if not row["local_path"]:
                raise ValueError("Current catalog has missing local blob bytes; restore them explicitly before backup.")
            path = _path(row["local_path"])
            if str(path) != row["local_path"]:
                raise ValueError("Blob source path must be absolute and normalized before backup.")
            try:
                info = path.stat()
            except FileNotFoundError:
                raise ValueError("Current catalog has missing local blob bytes; restore them explicitly before backup.") from None
            if not stat.S_ISREG(info.st_mode) or info.st_size != row["size"]:
                raise ValueError("Current catalog has invalid local blob bytes.")
            paths.add(str(path))
            identities[path] = _identity(info)
        selection = work / "files"
        with selection.open("xb") as stream:
            for path in sorted(paths):
                stream.write(os.fsencode(path) + b"\0")
        arguments = ["backup", "--json", "--tag", config["tag"], "--host",
                     "nannanews-" + config["dataset_id"]]
        if config.get("last_snapshot_id"):
            _snapshot(config, config["last_snapshot_id"])
            arguments.extend(["--parent", config["last_snapshot_id"]])
        arguments.extend(["--files-from-raw", str(selection)])
        metrics = _run(config, *arguments)
        for path, identity in identities.items():
            _no_symlinks(path)
            if _identity(path.stat()) != identity:
                raise ValueError("Blob changed during archive; snapshot not published.")
        source.execute("BEGIN IMMEDIATE")
        if source.execute("PRAGMA data_version").fetchone()[0] != version:
            raise ValueError("Live catalog changed during archive; snapshot not published.")
        snapshot_id = metrics["snapshot_id"]
        proof = {"ok": True, "snapshot_id": snapshot_id, "repository": config["repository"],
                 "repository_id": config["repository_id"], "dataset_id": config["dataset_id"],
                 "layout": "source-paths", "catalog_path": str(catalog),
                 "catalog_sha256": _stream_file(catalog)[0], "blob_count": len(rows),
                 "blob_bytes": sum(row["size"] for row in rows), "native_metrics": metrics,
                 "content_hashes_verified": False}
        config.update(last_snapshot_id=snapshot_id, last_backup=proof)
        config.setdefault("proofs", {})[snapshot_id] = proof
        source.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)", (_CONFIG, _json(config)))
        source.commit()
        return proof


def archive_foundation(root) -> dict:
    """Back up a SQLite snapshot and its registered local files using native restic."""
    with _locked(root) as root:
        return _archive_locked(root, _require_config(root))




def _verify_snapshot(config, proof, *, full=False):
    _snapshot(config, proof["snapshot_id"])
    _check(config, full=full)
    return dict(proof, verified_at=_now(), level='data' if full else 'structure',
                verification={"native_check_read_data": full})


def verify_archive(root, *, full=False) -> dict:
    """Check repository structure; full=True also reads all repository data."""
    with _locked(root) as root:
        config = _require_config(root)
        return _verify_snapshot(config, _proof_for(root, config), full=full)


def relocate_archive(root, *, repository: str | Path) -> dict:
    """Rebind an already copied repository after checking its identity and snapshots."""
    with _locked(root) as root:
        config = _require_config(root)
        previous = config["repository"]
        value = str(repository)
        if not value.startswith("rclone:"):
            value = str(_path(repository))
        _validate_locations(root, value, Path(config["password_file"]))
        proof = _proof_for(root, config)
        _repository(config)
        target = dict(config, repository=value)
        _repository(target)
        source_snapshots = _run(config, "snapshots", "--json")
        target_snapshots = _run(target, "snapshots", "--json")
        if sorted(source_snapshots, key=lambda row: row["id"]) != sorted(target_snapshots, key=lambda row: row["id"]):
            raise ValueError("Copied repository must preserve every snapshot, dataset and tag, including unproven history.")
        proofs = {}
        for snapshot_id in config["proofs"]:
            recorded = _proof_for(root, config, snapshot_id)
            _snapshot(target, snapshot_id)
            proofs[snapshot_id] = dict(recorded, repository=value)
        verified = dict(_verify_snapshot(target, proof, full=True), repository=value)
        target.update(proofs=proofs, last_verified=verified)
        result = dict(verified, relocated=True, previous_repository=previous)
        _save_config(root, target, proof=verified)
        return result




def _fresh_target(root, config, target, *, blob=False):
    target = _path(target)
    if target.exists():
        raise FileExistsError("Refusing to overwrite an existing archive output target.")
    protected = [Path(__file__).resolve().parents[2], _path(config["password_file"])]
    if not blob:
        protected.append(_path(root))
    elif target == _path(root) / "foundation.sqlite3":
        raise ValueError("Blob read cannot replace the foundation catalog.")
    if not _remote(config["repository"]):
        protected.append(_path(config["repository"]))
    with closing(_open_existing(root)) as db:
        protected.extend(Path(row[0]).expanduser().resolve() for row in db.execute("SELECT path FROM originals") if row[0])
        protected.extend(Path(row[0]).expanduser().resolve() for row in db.execute(
            "SELECT local_path FROM blobs WHERE owned_local=0 AND local_path IS NOT NULL"))
    if any(_overlaps(target, path) for path in protected):
        raise ValueError("Archive output must be outside current foundation, repository and original sources.")
    if any(part.lower() in _PROTECTED for part in target.parts):
        raise ValueError("Archive output cannot be placed in application/public/source directories.")
    if not target.parent.is_dir():
        raise ValueError("Archive output parent must already exist.")
    return target


def restore_foundation(root, *, target: Path, snapshot_id: str | None = None) -> dict:
    """Restore a chosen proven dataset snapshot into an exclusive fresh foundation."""
    with _locked(root) as root:
        config = _require_config(root)
        proof = _proof_for(root, config, snapshot_id)
        target = _fresh_target(root, config, target)
        _snapshot(config, proof["snapshot_id"])
        target.mkdir(mode=0o700)
        try:
            with tempfile.TemporaryDirectory(prefix=".native-restore-", dir=target) as temporary:
                work = Path(temporary)
                catalog = _fetch_catalog(config, proof, work)
                rows = _blobs(catalog)
                restored = work / "restored"
                _run(config, "restore", proof["snapshot_id"], "--target", str(restored), "--verify")
                blobs = target / "blobs"
                blobs.mkdir(mode=0o700)
                for row in rows:
                    source = restored / _blob_path(proof, row).lstrip("/")
                    _copy_blob(source, blobs / row["sha256"], row["sha256"], row["size"])
                os.rename(catalog, target / "foundation.sqlite3")
            with closing(_write_db(target)) as db, db:
                for row in db.execute("SELECT sha256 FROM blobs").fetchall():
                    db.execute("UPDATE blobs SET local_path=?,owned_local=1 WHERE sha256=?",
                               (str(target / "blobs" / row[0]), row[0]))
                restored_config = dict(config, last_snapshot_id=proof["snapshot_id"], last_verified=proof)
                db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)", (_CONFIG, _json(restored_config)))
            _sync_directory(target)
            return dict(proof, target=str(target), restored_blob_count=len(rows),
                        local_paths_rebound=True)
        except BaseException:
            shutil.rmtree(target)
            raise


def read_archived_blob(root, sha256: str, *, target: Path) -> dict:
    """Stream a native dump; publish exact catalog bytes exclusively after fsync."""
    if not isinstance(sha256, str) or not _SHA.fullmatch(sha256):
        raise ValueError("Invalid blob SHA-256.")
    with _locked(root) as root:
        config = _require_config(root)
        proof = _proof_for(root, config)
        target = _fresh_target(root, config, target, blob=True)
        _snapshot(config, proof["snapshot_id"])
        with tempfile.TemporaryDirectory(prefix=".restic-catalog-", dir=root) as temporary:
            catalog = _fetch_catalog(config, proof, Path(temporary))
            with closing(_open_existing(catalog.parent)) as db:
                raw = db.execute("SELECT * FROM blobs WHERE sha256=?", (sha256,)).fetchone()
            if raw is None:
                raise ValueError("Blob is not covered by the selected native snapshot.")
            row = dict(raw)
            size = row["size"]
            if not isinstance(size, int) or size < 0:
                raise ValueError("Invalid archived blob size.")
            with _output(target, sha256, size) as stream:
                _dump_to_stream(config, proof["snapshot_id"], _blob_path(proof, row), stream)
        return {"ok": True, "snapshot_id": proof["snapshot_id"], "sha256": sha256,
                "size": size, "target": str(target), "repository_id": config["repository_id"]}


