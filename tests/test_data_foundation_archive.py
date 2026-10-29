"""Data-loss/security contracts exercised with real temporary native repositories."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3

import pytest

import nannanews.data_foundation_archive as archive_module
from nannanews.data_foundation import connect_foundation
from nannanews.data_foundation_archive import (
    archive_config, archive_foundation, configure_archive,
    read_archived_blob, relocate_archive, restore_foundation, verify_archive,
)


def foundation(tmp_path, name="foundation"):
    root = tmp_path / name
    content = b"Retained activity text and paid-review evidence.\n"
    digest = hashlib.sha256(content).hexdigest()
    original = tmp_path / (name + "-original.txt")
    original.write_bytes(b"User-owned input: never remove or rewrite.\n")
    original_digest = hashlib.sha256(original.read_bytes()).hexdigest()
    with closing(connect_foundation(root)) as db, db:
        (root / "blobs").mkdir(mode=0o700)
        blob = root / "blobs" / digest
        blob.write_bytes(content)
        blob.chmod(0o600)
        db.execute("INSERT INTO blobs(sha256,size,mime,kind,local_path,owned_local,remote_key) "
                   "VALUES(?,?,?,?,?,1,?)", (digest, len(content), "text/plain", "snapshot_text",
                                            str(blob), "nju:historical/inert-provenance"))
        db.execute("INSERT INTO activities(activity_id,title,summary,review_status,record_version,input_sha256) "
                   "VALUES('activity','A real activity','Its summary','reviewed','version-1',?)", (digest,))
        db.execute("INSERT INTO snapshots(snapshot_id,url,title,text_blob_sha,status,manifest_json) "
                   "VALUES('capture','https://example.edu/activity','Evidence',?,'complete','{}')", (digest,))
        db.execute("INSERT INTO snapshot_blobs VALUES('capture',?,'text',0)", (digest,))
        db.execute("INSERT INTO activity_snapshots VALUES('activity','capture','source-1')")
        db.execute("INSERT INTO reviews(review_id,activity_id,status,answer_blob_sha,fields_json,error_json) "
                   "VALUES('review','activity','reviewed',?,'{}','[]')", (digest,))
        db.execute("INSERT INTO review_snapshots VALUES('review','capture')")
        db.execute("INSERT INTO attempts(request_key,state,model,source_ledger) "
                   "VALUES('already-paid','succeeded','fixture-model','paid-ledger')")
        db.execute("INSERT INTO attempt_origins VALUES('already-paid','paid-ledger','request-1','succeeded')")
        db.execute("INSERT INTO documents(document_id,sha256,format,parse_status) VALUES('document',?,'txt','parsed')",
                   (original_digest,))
        info = original.stat()
        db.execute("INSERT INTO originals(path,document_id,sha256,size,mtime_ns) VALUES(?,'document',?,?,?)",
                   (str(original), original_digest, info.st_size, info.st_mtime_ns))
        db.execute("INSERT INTO ingestions(source_path,sha256,size,mtime_ns,status,record_count,completed_at) "
                   "VALUES(?,?,?,?,'completed',1,'2026-10-27T00:00:00Z')",
                   (str(original), original_digest, info.st_size, info.st_mtime_ns))
        db.execute("INSERT INTO ingestion_outputs VALUES(?,'review','review')", (str(original),))
    return root, digest, original


@pytest.fixture
def native(tmp_path):
    binary = shutil.which("restic")
    if binary is None:
        pytest.skip("Native restic executable is required for actual archive/restore behavior.")
    root, digest, original = foundation(tmp_path)
    config = configure_archive(root, repository=tmp_path / "native-repository",
                               password_file=tmp_path / "archive.key", restic_binary=binary)
    return root, digest, original, config


def test_config_read_does_not_create_missing_root(tmp_path):
    root = tmp_path / "not-prepared"
    assert archive_config(root) is None
    assert not root.exists()


def test_configuration_refuses_unsafe_or_symlink_secret_without_mutating_it(tmp_path):
    root, _, original = foundation(tmp_path)
    secret = tmp_path / "insecure.key"
    secret.write_text("existing-private-value", encoding="utf-8")
    secret.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        configure_archive(root, repository=tmp_path / "repo", password_file=secret)
    assert secret.read_text(encoding="utf-8") == "existing-private-value"
    assert archive_config(root) is None
    link = tmp_path / "linked.key"
    link.symlink_to(secret)
    with pytest.raises(ValueError, match="symlink"):
        configure_archive(root, repository=tmp_path / "repo", password_file=link)
    with pytest.raises(ValueError, match="outside"):
        configure_archive(root, repository=root / "recursive-repo", password_file=tmp_path / "safe.key")
    assert original.exists()
    assert not (root / "recursive-repo").exists()


def test_native_restore_preserves_graph_paid_history_originals_and_rebinds_files(native, tmp_path):
    root, digest, original, config = native
    original_bytes = original.read_bytes()
    proof = archive_foundation(root)
    assert proof["ok"] and proof["blob_count"] == 1
    assert proof["repository_id"] == config["repository_id"]
    assert verify_archive(root)["ok"]
    restored = tmp_path / "restored"
    result = restore_foundation(root, target=restored)
    assert result["snapshot_id"] == proof["snapshot_id"]
    with closing(sqlite3.connect(restored / "foundation.sqlite3")) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT * FROM activity_snapshots").fetchall() == [("activity", "capture", "source-1")]
        assert db.execute("SELECT * FROM review_snapshots").fetchall() == [("review", "capture")]
        assert db.execute("SELECT * FROM attempt_origins").fetchall() == [
            ("already-paid", "paid-ledger", "request-1", "succeeded")]
        path, owned, remote = db.execute("SELECT local_path,owned_local,remote_key FROM blobs").fetchone()
        assert path == str(restored / "blobs" / digest) and owned == 1
        assert remote == "nju:historical/inert-provenance"
        assert db.execute("SELECT path FROM originals").fetchone()[0] == str(original)
    assert Path(path).read_bytes() == (root / "blobs" / digest).read_bytes()
    assert verify_archive(restored)["ok"]
    assert original.read_bytes() == original_bytes
    with pytest.raises(FileExistsError):
        restore_foundation(root, target=restored)
    assert Path(path).exists()


def test_old_version_restore_preserves_original_history(native, tmp_path):
    root, _, original, _ = native
    first = archive_foundation(root)
    with closing(sqlite3.connect(root / "foundation.sqlite3")) as db, db:
        db.execute("INSERT INTO attempts(request_key,state,model) VALUES('new-paid-attempt','reserved','fixture-model')")
    second = archive_foundation(root)
    assert second["snapshot_id"] != first["snapshot_id"]
    restored = tmp_path / "old-version"
    restore_foundation(root, target=restored, snapshot_id=first["snapshot_id"])
    with closing(sqlite3.connect(restored / "foundation.sqlite3")) as db:
        assert db.execute("SELECT count(*) FROM attempts WHERE request_key='new-paid-attempt'").fetchone()[0] == 0
    assert verify_archive(restored)["snapshot_id"] == first["snapshot_id"]
    assert archive_config(restored)["last_snapshot_id"] == first["snapshot_id"]
    assert original.exists()



def test_native_get_and_missing_local_backup_refusal(native, tmp_path):
    root, digest, original, _ = native
    content = (root / "blobs" / digest).read_bytes()
    proof = archive_foundation(root)
    (root / "blobs" / digest).unlink()  # Disposable generated fixture, not an original.
    directory = root / ".archive-readback"
    directory.mkdir(mode=0o700)
    output = directory / digest
    got = read_archived_blob(root, digest, target=output)
    assert got["snapshot_id"] == proof["snapshot_id"]
    assert output.read_bytes() == content
    with pytest.raises(FileExistsError):
        read_archived_blob(root, digest, target=output)
    assert output.read_bytes() == content
    symlink = tmp_path / "redirect"
    symlink.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        read_archived_blob(root, digest, target=symlink / "unsafe")
    assert not (directory / "unsafe").exists()
    with pytest.raises(ValueError, match="missing local blob"):
        archive_foundation(root)
    assert original.exists()




def test_shared_native_repository_never_selects_another_dataset_latest(native, tmp_path):
    root, _, _, config = native
    first = archive_foundation(root)
    other, _, _ = foundation(tmp_path, "other-foundation")
    configure_archive(other, repository=config["repository"], password_file=Path(config["password_file"]),
                      restic_binary=config["restic_binary"])
    second = archive_foundation(other)
    assert first["dataset_id"] != second["dataset_id"]
    restored = restore_foundation(root, target=tmp_path / "selected-dataset")
    assert restored["snapshot_id"] == first["snapshot_id"]
    with pytest.raises(ValueError):
        restore_foundation(root, target=tmp_path / "wrong-dataset", snapshot_id=second["snapshot_id"])
    assert not (tmp_path / "wrong-dataset").exists()


def test_native_repository_corruption_cannot_refresh_proof_or_delete_original(native):
    root, _, original, config = native
    proof = archive_foundation(root)
    packs = [path for path in (Path(config["repository"]) / "data").glob("*/*") if path.is_file()]
    assert packs, "A real native backup must contain encrypted restic packs."
    packs[0].chmod(0o600)  # Native packs are immutable; damage only our private fixture.
    with packs[0].open("r+b") as stream:
        first = stream.read(1)
        stream.seek(0)
        stream.write(bytes([first[0] ^ 0xFF]))
        stream.flush()
        os.fsync(stream.fileno())
    with pytest.raises(RuntimeError, match="Native restic"):
        verify_archive(root, full=True)
    assert archive_config(root)["last_snapshot_id"] == proof["snapshot_id"]
    assert original.exists()


def test_relocation_preserves_native_identity_history_key_and_source(native, tmp_path):
    root, digest, original, config = native
    first = archive_foundation(root)
    with closing(sqlite3.connect(root / "foundation.sqlite3")) as db, db:
        db.execute("UPDATE activities SET title='Second archived title',record_version='version-2'")
    latest = archive_foundation(root)
    with closing(sqlite3.connect(root / "foundation.sqlite3")) as db, db:
        db.execute("UPDATE ingestions SET status='cleaned',cleaned_at='2026-10-27T01:00:00Z'")
        db.execute("INSERT INTO cleanup_log VALUES(?,?,?,'2026-10-27T01:00:00Z','{}')",
                   (str(original), hashlib.sha256(original.read_bytes()).hexdigest(), original.stat().st_size))
    previous = archive_config(root)
    source = Path(config["repository"])
    source_bytes = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    key_bytes = Path(config["password_file"]).read_bytes()
    original_bytes = original.read_bytes()
    target = tmp_path / "copied-native-repository"
    shutil.copytree(source, target)
    with pytest.raises(ValueError, match="Refusing to replace"):
        configure_archive(root, repository=target, password_file=Path(config["password_file"]),
                          restic_binary=config["restic_binary"])
    assert archive_config(root) == previous

    result = relocate_archive(root, repository=target)

    assert result["relocated"] is True and result["ok"] is True
    assert result["previous_repository"] == str(source)
    assert result["repository"] == str(target)
    for identity in ("snapshot_id", "repository_id", "dataset_id"):
        assert result[identity] == latest[identity]
    persisted = archive_config(root)
    assert persisted["repository"] == str(target)
    assert persisted["password_file"] == config["password_file"]
    assert persisted["tag"] == config["tag"]
    assert persisted["last_snapshot_id"] == latest["snapshot_id"]
    assert persisted["last_verified"] == persisted["proofs"][latest["snapshot_id"]]
    assert persisted["last_verified"]["verified_at"] == result["verified_at"]
    assert persisted["proofs"][first["snapshot_id"]] == dict(first, repository=str(target))
    assert set(persisted["proofs"]) == set(previous["proofs"])
    assert verify_archive(root)["repository"] == str(target)
    restored = tmp_path / "relocated-historical-restore"
    restore_foundation(root, target=restored, snapshot_id=first["snapshot_id"])
    with closing(sqlite3.connect(restored / "foundation.sqlite3")) as db:
        assert db.execute("SELECT title FROM activities").fetchone()[0] == "A real activity"
    output = root / ".archive-readback" / digest
    output.parent.mkdir(mode=0o700)
    assert read_archived_blob(root, digest, target=output)["snapshot_id"] == latest["snapshot_id"]
    assert output.read_bytes() == (root / "blobs" / digest).read_bytes()
    assert {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()} == source_bytes
    assert Path(config["password_file"]).read_bytes() == key_bytes
    assert original.read_bytes() == original_bytes


def test_relocation_rejects_unrelated_native_repository_without_rebinding(native, tmp_path):
    root, _, original, config = native
    archive_foundation(root)
    other, _, _ = foundation(tmp_path, "unrelated-foundation")
    target = tmp_path / "unrelated-native-repository"
    unrelated = configure_archive(other, repository=target, password_file=Path(config["password_file"]),
                                  restic_binary=config["restic_binary"])
    assert unrelated["repository_id"] != config["repository_id"]
    previous = archive_config(root)
    with pytest.raises(ValueError, match="repository identity changed"):
        relocate_archive(root, repository=target)
    assert archive_config(root) == previous
    assert verify_archive(root)["ok"]
    assert original.exists()


def test_relocation_rejects_corrupt_clone_without_publishing_partial_config(native, tmp_path):
    root, _, original, config = native
    archive_foundation(root)
    previous = archive_config(root)
    target = tmp_path / "corrupt-clone"
    shutil.copytree(config["repository"], target)
    pack = next(path for path in (target / "data").glob("*/*") if path.is_file())
    pack.chmod(0o600)
    with pack.open("r+b") as stream:
        first = stream.read(1)
        stream.seek(0)
        stream.write(bytes([first[0] ^ 0xFF]))
        stream.flush()
        os.fsync(stream.fileno())
    with pytest.raises(RuntimeError, match="Native restic check failed"):
        relocate_archive(root, repository=target)
    assert archive_config(root) == previous
    assert verify_archive(root)["ok"]
    assert original.exists()


def test_relocation_requires_every_recorded_historical_snapshot(native, tmp_path):
    root, _, _, config = native
    first = archive_foundation(root)
    with closing(sqlite3.connect(root / "foundation.sqlite3")) as db, db:
        db.execute("UPDATE activities SET title='Latest title',record_version='version-2'")
    latest = archive_foundation(root)
    previous = archive_config(root)
    target = tmp_path / "incomplete-history-clone"
    shutil.copytree(config["repository"], target)
    (target / "snapshots" / first["snapshot_id"]).unlink()
    assert (target / "snapshots" / latest["snapshot_id"]).exists()
    with pytest.raises((ValueError, RuntimeError)):
        relocate_archive(root, repository=target)
    assert archive_config(root) == previous


def test_relocation_requires_exact_dataset_tag_on_recorded_snapshot(native, tmp_path):
    root, _, _, config = native
    own = archive_foundation(root)
    other, _, _ = foundation(tmp_path, "other-dataset-foundation")
    configure_archive(other, repository=config["repository"], password_file=Path(config["password_file"]),
                      restic_binary=config["restic_binary"])
    foreign = archive_foundation(other)
    # A recorded ID alone must never authorize a foreign dataset snapshot.
    with closing(sqlite3.connect(root / "foundation.sqlite3")) as db, db:
        value = json.loads(db.execute("SELECT value FROM metadata WHERE key='restic_archive:v1'").fetchone()[0])
        value["proofs"][foreign["snapshot_id"]] = dict(own, snapshot_id=foreign["snapshot_id"])
        db.execute("UPDATE metadata SET value=? WHERE key='restic_archive:v1'", (json.dumps(value),))
    previous = archive_config(root)
    target = tmp_path / "shared-clone"
    shutil.copytree(config["repository"], target)
    with pytest.raises(ValueError, match="exact snapshot of this dataset"):
        relocate_archive(root, repository=target)
    assert archive_config(root) == previous



def test_relocation_config_commit_failure_leaves_old_binding_and_proofs(native, tmp_path):
    root, _, _, config = native
    with closing(sqlite3.connect(root / "foundation.sqlite3")) as db, db:
        db.execute("""CREATE TRIGGER reject_relocation BEFORE INSERT ON metadata
                      WHEN NEW.key='restic_archive:v1' AND instr(NEW.value,'relocation-rejected-target') > 0
                      BEGIN SELECT RAISE(ABORT,'relocation config write rejected'); END""")
    archive_foundation(root)
    previous = archive_config(root)
    target = tmp_path / "relocation-rejected-target"
    shutil.copytree(config["repository"], target)
    with pytest.raises(sqlite3.IntegrityError, match="relocation config write rejected"):
        relocate_archive(root, repository=target)
    assert archive_config(root) == previous
    assert verify_archive(root)["ok"]


@pytest.mark.parametrize("location", ["missing", "empty", "foundation", "source", "public", "symlink", "remote"])
def test_relocation_never_initializes_or_accepts_unsafe_target(native, tmp_path, location):
    root, _, original, config = native
    archive_foundation(root)
    previous = archive_config(root)
    target = tmp_path / "new-repository"
    if location == "empty":
        target.mkdir(mode=0o700)
    elif location == "foundation":
        target = root / "unsafe-repository"
    elif location == "source":
        target = original
    elif location == "public":
        target = tmp_path / "public" / "unsafe-repository"
    elif location == "symlink":
        target.symlink_to(config["repository"], target_is_directory=True)
    elif location == "remote":
        target = "rclone:other:Private/NannaNews/activity-snapshots/not-authorized"
    with pytest.raises((ValueError, RuntimeError)):
        relocate_archive(root, repository=target)
    assert archive_config(root) == previous
    if location in {"missing", "foundation", "public"}:
        assert not target.exists()
    elif location == "empty":
        assert list(target.iterdir()) == []
    assert original.exists()


@pytest.mark.parametrize("secret", ["missing", "public"])
def test_relocation_requires_existing_private_key_and_never_recreates_it(native, tmp_path, secret):
    root, _, _, config = native
    archive_foundation(root)
    previous = archive_config(root)
    target = tmp_path / "key-validation-clone"
    shutil.copytree(config["repository"], target)
    key = Path(config["password_file"])
    original = key.read_bytes()
    if secret == "missing":
        key.unlink()
    else:
        key.chmod(0o644)
    with pytest.raises((OSError, ValueError)):
        relocate_archive(root, repository=target)
    assert archive_config(root) == previous
    if secret == "missing":
        assert not key.exists()
    else:
        assert key.read_bytes() == original and key.stat().st_mode & 0o777 == 0o644


def test_relocation_requires_published_native_snapshot(native, tmp_path):
    root, _, _, config = native
    target = tmp_path / "empty-history-clone"
    shutil.copytree(config["repository"], target)
    previous = archive_config(root)
    with pytest.raises(ValueError):
        relocate_archive(root, repository=target)
    assert archive_config(root) == previous




def test_relocation_preserves_unproven_and_other_dataset_snapshots(native, tmp_path):
    from nannanews import data_foundation_archive as archive
    root, _, _, config = native
    archive_foundation(root)
    stage = tmp_path / "retained-history"
    stage.mkdir(mode=0o700)
    (stage / "history.txt").write_text("Unknown provenance: preserve, never silently authorize", encoding="utf-8")
    unknown = archive._run(config, "backup", "--json", "--tag", "legacy-unproven-history", "history.txt", cwd=stage)["snapshot_id"]
    target = tmp_path / "missing-unproven-clone"
    shutil.copytree(config["repository"], target)
    (target / "snapshots" / unknown).unlink()
    previous = archive_config(root)
    with pytest.raises(ValueError, match="every snapshot"):
        relocate_archive(root, repository=target)
    assert archive_config(root) == previous


def test_backup_and_verification_do_not_restore_implicitly(native, monkeypatch):
    root, _, original, _ = native
    calls = []
    run = archive_module._run

    def observe(config, *args, **kwargs):
        calls.append(args)
        return run(config, *args, **kwargs)

    monkeypatch.setattr(archive_module, "_run", observe)
    archive_foundation(root)
    assert not any(args[0] in {"check", "restore", "dump"} for args in calls)
    calls.clear()
    verify_archive(root)
    assert ("check",) in calls
    assert not any(args[0] in {"restore", "dump"} for args in calls)
    calls.clear()
    verify_archive(root, full=True)
    assert ("check", "--read-data") in calls
    assert not any(args[0] in {"restore", "dump"} for args in calls)
    assert original.exists()


def test_historical_payload_snapshot_still_restores(native, tmp_path):
    root, digest, original, config = native
    work = tmp_path / "old-stage"
    payload = work / "payload"
    (payload / "blobs").mkdir(parents=True)
    with closing(sqlite3.connect(root / "foundation.sqlite3")) as source, closing(sqlite3.connect(payload / "foundation.sqlite3")) as target:
        source.backup(target)
    shutil.copyfile(root / "blobs" / digest, payload / "blobs" / digest)
    (payload / "directory.sqlite3").write_bytes(b"obsolete derived data")
    metrics = archive_module._run(config, "backup", "--json", "--tag", config["tag"], "payload", cwd=work)
    snapshot_id = metrics["snapshot_id"]
    proof = {"ok": True, "snapshot_id": snapshot_id, "repository_id": config["repository_id"],
             "dataset_id": config["dataset_id"], "repository": config["repository"],
             "catalog_sha256": hashlib.sha256((payload / "foundation.sqlite3").read_bytes()).hexdigest()}
    config.update(last_snapshot_id=snapshot_id, last_verified=proof, proofs={snapshot_id: proof})
    archive_module._save_config(root, config)
    restored = tmp_path / "old-restored"
    restore_foundation(root, target=restored)
    assert (restored / "blobs" / digest).read_bytes() == (root / "blobs" / digest).read_bytes()
    assert original.exists()


def test_registered_external_original_is_backed_up_without_mutation(native, tmp_path):
    root, _, original, _ = native
    data = original.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    with closing(sqlite3.connect(root / "foundation.sqlite3")) as db, db:
        db.execute("INSERT INTO blobs(sha256,size,mime,kind,local_path,owned_local) VALUES(?,?,?,?,?,0)",
                   (digest, len(data), "text/plain", "original", str(original)))
    archive_foundation(root)
    restored = tmp_path / "external-restored"
    restore_foundation(root, target=restored)
    assert (restored / "blobs" / digest).read_bytes() == data
    assert original.read_bytes() == data
