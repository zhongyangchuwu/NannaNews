"""Private content-addressed blob storage behavior."""
import pytest

from nannanews.data_foundation import connect_foundation
from nannanews.data_foundation_blobs import BlobStore



@pytest.fixture
def store(tmp_path):
    root = tmp_path / "foundation"
    db = connect_foundation(root)
    try:
        yield BlobStore(root, db)
    finally:
        db.close()




def test_bytes_deduplicate_private_files_without_eviction_quota(store):
    first = store.put_bytes(b"1234", mime="text/plain", kind="answer")
    assert store.put_bytes(b"1234", mime="text/plain", kind="image") == first
    second = store.put_bytes(b"5678", mime="text/plain", kind="answer")
    assert first != second
    assert store.db.execute("SELECT count(*) FROM blobs").fetchone()[0] == 2
    objects = list((store.root / "blobs").iterdir())
    assert {path.read_bytes() for path in objects} == {b"1234", b"5678"}
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in objects)
    assert (store.root / "blobs").stat().st_mode & 0o777 == 0o700


def test_file_references_original_without_copy_or_permission_change(store, tmp_path):
    original = tmp_path / "original.html"
    original.write_bytes(b"Original publication")
    original.chmod(0o640)
    digest = store.put_file(original, mime="text/html", kind="original")
    assert store.put_bytes(b"Original publication", mime="text/html", kind="original") == digest
    row = store.db.execute("SELECT * FROM blobs").fetchone()
    assert row["local_path"] == str(original) and row["owned_local"] == 0
    assert not (store.root / "blobs").exists()
    assert original.stat().st_mode & 0o777 == 0o640


def test_corrupt_staged_object_is_not_silently_reused(store):
    digest = store.put_bytes(b"answer", mime="text/plain", kind="answer")
    (store.root / "blobs" / digest).write_bytes(b"broken")
    with pytest.raises(ValueError, match="corrupt"):
        store.put_bytes(b"answer", mime="text/plain", kind="answer")


