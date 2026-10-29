"""Private content-addressed storage with explicit original references."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import tempfile



_CHUNK = 1024 * 1024


def _digest_file(path: Path) -> tuple[str, os.stat_result]:
    """Hash a regular, stable, non-symlink file without loading it into memory."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("source is not a regular file")
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(_CHUNK), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
            before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size,
                                   after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("source changed while hashing")
    return digest.hexdigest(), after


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class BlobStore:
    def __init__(self, root: Path, db: sqlite3.Connection):
        self.root = Path(root).resolve()
        self.db = db

    def _local_valid(self, row) -> bool:
        if not row["local_path"]:
            return False
        try:
            digest, info = _digest_file(Path(row["local_path"]))
        except (OSError, ValueError):
            return False
        return digest == row["sha256"] and info.st_size == row["size"]

    def put_file(self, path: Path, *, mime: str, kind: str) -> str:
        source = Path(path).absolute()
        if source != source.resolve():
            raise ValueError("symlink sources are not supported")
        digest, info = _digest_file(source)
        row = self.db.execute("SELECT * FROM blobs WHERE sha256=?", (digest,)).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO blobs(sha256,size,mime,kind,local_path,owned_local) VALUES(?,?,?,?,?,0)",
                (digest, info.st_size, mime, kind, str(source)))
        elif row["size"] != info.st_size:
            raise ValueError("existing blob size disagrees with digest")
        elif not self._local_valid(row):
            self.db.execute("UPDATE blobs SET local_path=?,owned_local=0 WHERE sha256=?",
                            (str(source), digest))
        return digest

    def put_bytes(self, data: bytes, *, mime: str, kind: str) -> str:
        digest = hashlib.sha256(data).hexdigest()
        row = self.db.execute("SELECT * FROM blobs WHERE sha256=?", (digest,)).fetchone()
        if row is not None:
            if row["size"] != len(data):
                raise ValueError("existing blob size disagrees with digest")
            if self._local_valid(row):
                return digest
        directory = self.root / "blobs"
        if directory.is_symlink():
            raise ValueError("blob staging directory must not be a symlink")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        destination = directory / digest
        if destination.exists():
            actual, info = _digest_file(destination)
            if actual != digest or info.st_size != len(data):
                raise ValueError("existing staged object is corrupt")
        else:
            descriptor, temporary = tempfile.mkstemp(prefix=".stage-", dir=directory)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                # No replace: another writer must not lose its canonical object.
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    actual, info = _digest_file(destination)
                    if actual != digest or info.st_size != len(data):
                        raise ValueError("concurrent staged object is corrupt")
            finally:
                os.unlink(temporary)
            _sync_directory(directory)
        destination.chmod(0o600)
        self.db.execute(
            "INSERT INTO blobs(sha256,size,mime,kind,local_path,owned_local) VALUES(?,?,?,?,?,1) "
            "ON CONFLICT(sha256) DO UPDATE SET local_path=excluded.local_path,owned_local=1",
            (digest, len(data), mime, kind, str(destination)))
        return digest

