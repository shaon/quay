"""
PROJQUAY-10117 Proposal 1: blob upload paths A, B and C, publication, and stored-bytes
verification for SHA-256, SHA-384 and SHA-512 identities.
"""

import hashlib
import os
from io import BytesIO
from test.fixtures import *
from unittest.mock import patch

import pytest

import data.model.storage as storage_model
from data.database import BlobUpload, ImageStorage, ImageStoragePlacement, UploadedBlob, db
from data.model import BlobStorageMissingException
from data.registry_model.blobuploader import (
    BlobDigestMismatchException,
    BlobIntegrityException,
    BlobUploadSettings,
    RequestedDigestMismatchException,
    create_blob_upload,
    retrieve_blob_upload_manager,
    upload_blob,
)
from data.registry_model.registry_oci_model import OCIModel
from digest import digest_tools
from storage.distributedstorage import DistributedStorage
from storage.fakestorage import FakeStorage
from util.locking import LockAcquireTimeout
from util.metrics.prometheus import blob_integrity_failures

ALGORITHMS = ["sha256", "sha384", "sha512"]
APP_CONFIG = {"TESTING": True}
SETTINGS = BlobUploadSettings("10M", 3600)


def _digest(algorithm, data):
    return "%s:%s" % (algorithm, hashlib.new(algorithm, data).hexdigest())


@pytest.fixture()
def registry_model(initialized_db):
    return OCIModel()


@pytest.fixture()
def repo_ref(registry_model):
    return registry_model.lookup_repository("devtable", "complex")


@pytest.fixture()
def storage():
    return DistributedStorage({"local_us": FakeStorage("local")}, ["local_us"])


@pytest.fixture()
def real_transactions():
    """
    Unit tests normally replace db_transaction with a no-op. Use real transactions for the
    publication code so rollback and lock ordering are observable.
    """
    with patch("data.model.blob.db_transaction", lambda: db.atomic()):
        yield


def _rows_for(sha256):
    return list(ImageStorage.select().where(ImageStorage.content_checksum == sha256))


def _assert_published(blob, data, algorithm, repo_ref, registry_model):
    sha256 = _digest("sha256", data)
    expected = _digest(algorithm, data)
    assert blob.digest == expected
    row = ImageStorage.get(id=blob._db_id)
    assert row.content_checksum == sha256
    if algorithm == "sha256":
        assert row.requested_content_checksum is None
    else:
        assert row.requested_content_checksum == expected
        # Never served under its internal SHA-256 in this repository.
        found = registry_model.get_repo_blob_by_digest(repo_ref, sha256)
        assert found is None or found._db_id != row.id
    assert registry_model.get_repo_blob_by_digest(repo_ref, expected)._db_id == row.id


def _chunks(data, size=7):
    return [data[i : i + size] for i in range(0, len(data), size)]


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_path_a_monolithic(algorithm, repo_ref, storage, registry_model):
    data = os.urandom(300)
    hint = algorithm if algorithm != "sha256" else None
    with upload_blob(repo_ref, storage, SETTINGS, requested_digest_algorithm=hint) as manager:
        manager.upload_chunk(APP_CONFIG, BytesIO(data))
        blob = manager.commit_to_blob(APP_CONFIG, _digest(algorithm, data))

    _assert_published(blob, data, algorithm, repo_ref, registry_model)
    assert storage.get_content(["local_us"], blob.storage_path) == data
    assert BlobUpload.select().where(BlobUpload.uuid == manager.blob_upload_id).count() == 0


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_path_b_hinted_chunked_across_requests(algorithm, repo_ref, storage, registry_model):
    data = os.urandom(200)
    manager = create_blob_upload(repo_ref, storage, SETTINGS, requested_digest_algorithm=algorithm)
    upload_id = manager.blob_upload_id

    record = BlobUpload.get(uuid=upload_id)
    if algorithm == "sha256":
        assert record.requested_digest_algorithm is None
        assert record.requested_digest_state is None
    else:
        assert record.requested_digest_algorithm == algorithm
        assert record.requested_digest_state.name == algorithm

    offset = 0
    for chunk in _chunks(data, 50):
        # Each chunk arrives in a separate request: reload the manager from the database.
        manager = retrieve_blob_upload_manager(repo_ref, upload_id, storage, SETTINGS)
        manager.upload_chunk(APP_CONFIG, BytesIO(chunk), offset, len(chunk))
        offset += len(chunk)

    manager = retrieve_blob_upload_manager(repo_ref, upload_id, storage, SETTINGS)
    if algorithm != "sha256":
        saved = manager.blob_upload.requested_digest_state
        assert saved.hexdigest() == hashlib.new(algorithm, data).hexdigest()

    with patch.object(storage, "stream_read", wraps=storage.stream_read) as reread:
        blob = manager.commit_to_blob(APP_CONFIG, _digest(algorithm, data))
        # The hinted state covered the bytes: no reread.
        assert reread.call_count == 0

    _assert_published(blob, data, algorithm, repo_ref, registry_model)


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_path_c_unhinted_chunked(algorithm, repo_ref, storage, registry_model):
    data = os.urandom(150)
    with upload_blob(repo_ref, storage, SETTINGS) as manager:
        for chunk in _chunks(data, 40):
            manager.upload_chunk(APP_CONFIG, BytesIO(chunk))

        with patch.object(storage, "stream_read", wraps=storage.stream_read) as reread:
            blob = manager.commit_to_blob(APP_CONFIG, _digest(algorithm, data))
            assert reread.call_count == (0 if algorithm == "sha256" else 1)

    _assert_published(blob, data, algorithm, repo_ref, registry_model)


@pytest.mark.parametrize(
    "hint, final",
    [("sha512", "sha384"), ("sha384", "sha512"), ("sha512", "sha256"), ("sha384", "sha256")],
)
def test_final_digest_overrides_hint(hint, final, repo_ref, storage, registry_model):
    data = os.urandom(120)
    with upload_blob(repo_ref, storage, SETTINGS, requested_digest_algorithm=hint) as manager:
        manager.upload_chunk(APP_CONFIG, BytesIO(data))
        with patch.object(storage, "stream_read", wraps=storage.stream_read) as reread:
            blob = manager.commit_to_blob(APP_CONFIG, _digest(final, data))
            # A SHA-256 final digest uses the saved SHA-256 state; another algorithm rereads.
            assert reread.call_count == (0 if final == "sha256" else 1)

    _assert_published(blob, data, final, repo_ref, registry_model)


@pytest.mark.parametrize("corruption", ["garbage", "wrong_algorithm", "null"])
def test_invalid_hinted_state_falls_back_to_reread(corruption, repo_ref, storage, registry_model):
    import resumablehash

    data = os.urandom(100)
    manager = create_blob_upload(repo_ref, storage, SETTINGS, requested_digest_algorithm="sha512")
    upload_id = manager.blob_upload_id
    manager.upload_chunk(APP_CONFIG, BytesIO(data[:50]), 0, 50)

    if corruption == "garbage":
        db.execute_sql(
            "UPDATE blobupload SET requested_digest_state = 'bm90IGEgcGlja2xl' WHERE uuid = ?",
            (upload_id,),
        )
    elif corruption == "wrong_algorithm":
        record = BlobUpload.get(uuid=upload_id)
        other = resumablehash.new("sha384")
        other.update(data[:50])
        record.requested_digest_state = other
        record.save()
    else:
        BlobUpload.update(requested_digest_state=None).where(BlobUpload.uuid == upload_id).execute()

    manager = retrieve_blob_upload_manager(repo_ref, upload_id, storage, SETTINGS)
    manager.upload_chunk(APP_CONFIG, BytesIO(data[50:]), 50, 50)
    # The unusable state is no longer maintained.
    assert BlobUpload.get(uuid=upload_id).requested_digest_state is None

    manager = retrieve_blob_upload_manager(repo_ref, upload_id, storage, SETTINGS)
    with patch.object(storage, "stream_read", wraps=storage.stream_read) as reread:
        blob = manager.commit_to_blob(APP_CONFIG, _digest("sha512", data))
        assert reread.call_count == 1

    _assert_published(blob, data, "sha512", repo_ref, registry_model)


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
@pytest.mark.parametrize("hinted", [True, False])
def test_requested_digest_mismatch(algorithm, hinted, repo_ref, storage):
    data = os.urandom(64)
    wrong = _digest(algorithm, data + b"x")
    hint = algorithm if hinted else None
    with pytest.raises(RequestedDigestMismatchException):
        with upload_blob(repo_ref, storage, SETTINGS, requested_digest_algorithm=hint) as manager:
            manager.upload_chunk(APP_CONFIG, BytesIO(data))
            manager.commit_to_blob(APP_CONFIG, wrong)

    assert not ImageStorage.select().where(ImageStorage.requested_content_checksum == wrong).exists()
    assert BlobUpload.select().where(BlobUpload.uuid == manager.blob_upload_id).count() == 0


def test_sha256_mismatch_keeps_todays_exception(repo_ref, storage):
    data = os.urandom(64)
    with pytest.raises(BlobDigestMismatchException):
        with upload_blob(repo_ref, storage, SETTINGS, requested_digest_algorithm="sha512") as m:
            m.upload_chunk(APP_CONFIG, BytesIO(data))
            m.commit_to_blob(APP_CONFIG, _digest("sha256", data + b"x"))


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_reuses_existing_cas_object_with_placement_without_reading(
    algorithm, repo_ref, storage, registry_model
):
    data = os.urandom(90)
    # First push stores the bytes under a SHA-256 identity.
    with upload_blob(repo_ref, storage, SETTINGS) as manager:
        manager.upload_chunk(APP_CONFIG, BytesIO(data))
        first = manager.commit_to_blob(APP_CONFIG, _digest("sha256", data))

    hint = algorithm if algorithm != "sha256" else None
    with upload_blob(repo_ref, storage, SETTINGS, requested_digest_algorithm=hint) as manager:
        manager.upload_chunk(APP_CONFIG, BytesIO(data))
        with patch.object(storage, "stream_read", wraps=storage.stream_read) as reread:
            blob = manager.commit_to_blob(APP_CONFIG, _digest(algorithm, data))
            assert reread.call_count == 0

    _assert_published(blob, data, algorithm, repo_ref, registry_model)
    if algorithm == "sha256":
        assert blob._db_id == first._db_id
    else:
        assert blob._db_id != first._db_id
    # Both rows share one CAS object.
    assert len({r.id for r in _rows_for(_digest("sha256", data))}) == (
        1 if algorithm == "sha256" else 2
    )


def _store_orphan(storage, data, content=None):
    sha256 = _digest("sha256", data)
    path = storage_model.config.store.blob_path(sha256)
    storage.put_content(["local_us"], path, content if content is not None else data)
    return path


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_orphan_cas_object_is_read_and_verified(algorithm, repo_ref, storage, registry_model):
    data = os.urandom(80)
    _store_orphan(storage, data)

    hint = algorithm if algorithm != "sha256" else None
    with upload_blob(repo_ref, storage, SETTINGS, requested_digest_algorithm=hint) as manager:
        manager.upload_chunk(APP_CONFIG, BytesIO(data))
        with patch.object(storage, "stream_read", wraps=storage.stream_read) as reread:
            blob = manager.commit_to_blob(APP_CONFIG, _digest(algorithm, data))
            assert reread.call_count == 1

    _assert_published(blob, data, algorithm, repo_ref, registry_model)


@pytest.mark.parametrize("algorithm", ALGORITHMS)
@pytest.mark.parametrize("damage", ["size", "bytes"])
def test_corrupt_existing_cas_object_fails_with_alert(algorithm, damage, repo_ref, storage):
    data = os.urandom(80)
    if damage == "size":
        bad = data + b"extra"
    else:
        bad = bytes([data[0] ^ 0xFF]) + data[1:]
    path = _store_orphan(storage, data, bad)

    before = blob_integrity_failures.labels(
        "size_mismatch" if damage == "size" else "checksum_mismatch"
    )._value.get()
    hint = algorithm if algorithm != "sha256" else None
    with pytest.raises(BlobIntegrityException):
        with upload_blob(repo_ref, storage, SETTINGS, requested_digest_algorithm=hint) as m:
            m.upload_chunk(APP_CONFIG, BytesIO(data))
            m.commit_to_blob(APP_CONFIG, _digest(algorithm, data))

    after = blob_integrity_failures.labels(
        "size_mismatch" if damage == "size" else "checksum_mismatch"
    )._value.get()
    assert after == before + 1
    # Nothing published; the stored object is left untouched.
    assert _rows_for(_digest("sha256", data)) == []
    assert storage.get_content(["local_us"], path) == bad


def test_corrupt_object_with_placed_row_only_checks_size(repo_ref, storage, registry_model):
    # A same-size corrupt object with a placed CAS row is not reread (tiered verification).
    data = os.urandom(80)
    with upload_blob(repo_ref, storage, SETTINGS) as manager:
        manager.upload_chunk(APP_CONFIG, BytesIO(data))
        manager.commit_to_blob(APP_CONFIG, _digest("sha256", data))

    with upload_blob(repo_ref, storage, SETTINGS, requested_digest_algorithm="sha512") as m:
        m.upload_chunk(APP_CONFIG, BytesIO(data))
        with patch.object(storage, "get_size", return_value=len(data) + 1):
            with pytest.raises(BlobIntegrityException):
                m.commit_to_blob(APP_CONFIG, _digest("sha512", data))


def test_path_c_reread_sha256_mismatch_fails_and_removes_created_object(repo_ref, storage):
    data = os.urandom(70)
    before = blob_integrity_failures.labels("reread_mismatch")._value.get()

    with pytest.raises(BlobIntegrityException):
        with upload_blob(repo_ref, storage, SETTINGS) as manager:
            manager.upload_chunk(APP_CONFIG, BytesIO(data))
            with patch.object(storage, "stream_read", return_value=iter([b"different bytes"])):
                manager.commit_to_blob(APP_CONFIG, _digest("sha512", data))

    assert blob_integrity_failures.labels("reread_mismatch")._value.get() == before + 1
    sha256 = _digest("sha256", data)
    assert _rows_for(sha256) == []
    assert not storage.exists(["local_us"], storage_model.config.store.blob_path(sha256))


def test_path_c_object_missing_without_rows_is_unavailable(repo_ref, storage):
    data = os.urandom(70)
    with pytest.raises(BlobStorageMissingException):
        with upload_blob(repo_ref, storage, SETTINGS) as manager:
            manager.upload_chunk(APP_CONFIG, BytesIO(data))
            real_exists = storage.exists
            calls = {"n": 0}

            def exists(locations, path):
                # The first call is finalization (not there yet); then GC "deletes" it.
                calls["n"] += 1
                return real_exists(locations, path) if calls["n"] == 1 else False

            with patch.object(storage, "exists", side_effect=exists):
                manager.commit_to_blob(APP_CONFIG, _digest("sha512", data))


def test_missing_object_with_rows_is_integrity_failure(repo_ref, storage, registry_model):
    data = os.urandom(70)
    with upload_blob(repo_ref, storage, SETTINGS) as manager:
        manager.upload_chunk(APP_CONFIG, BytesIO(data))
        manager.commit_to_blob(APP_CONFIG, _digest("sha256", data))

    with pytest.raises(BlobIntegrityException):
        with upload_blob(repo_ref, storage, SETTINGS, requested_digest_algorithm="sha512") as manager:
            manager.upload_chunk(APP_CONFIG, BytesIO(data))
            with patch.object(storage, "get_size", return_value=None):
                manager.commit_to_blob(APP_CONFIG, _digest("sha512", data))


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_first_row_recheck_unavailable_when_cas_object_vanishes(
    algorithm, repo_ref, storage, real_transactions
):
    data = os.urandom(70)
    sha256 = _digest("sha256", data)
    real_exists = storage.exists
    calls = {"n": 0}

    def exists(locations, path):
        calls["n"] += 1
        # Finalization and any reread see the object; the step-2 re-check (inside the
        # publication transaction) does not.
        if db.in_transaction():
            return False
        return real_exists(locations, path)

    hint = algorithm if algorithm != "sha256" else None
    with pytest.raises(BlobStorageMissingException):
        with upload_blob(repo_ref, storage, SETTINGS, requested_digest_algorithm=hint) as m:
            m.upload_chunk(APP_CONFIG, BytesIO(data))
            with patch.object(storage, "exists", side_effect=exists):
                m.commit_to_blob(APP_CONFIG, _digest(algorithm, data))

    # The publication transaction rolled back.
    assert _rows_for(sha256) == []
    assert not UploadedBlob.select().join(ImageStorage).where(
        ImageStorage.content_checksum == sha256
    ).exists()


class _RecordingLock:
    events: list = []
    lock_factory = object()

    def __init__(self, name, lock_ttl=600, auto_renewal=False, blocking_timeout=None):
        self.name = name

    def __enter__(self):
        _RecordingLock.events.append(("acquire", self.name, db.in_transaction()))
        return self

    def __exit__(self, *args):
        _RecordingLock.events.append(("release", self.name, db.in_transaction()))


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_lock_held_until_publication_commits(
    algorithm, repo_ref, storage, registry_model, real_transactions
):
    import data.model.blob as blob_model

    data = os.urandom(60)
    sha256 = _digest("sha256", data)
    _RecordingLock.events = []
    real_link = blob_model._temp_link_blob

    def recording_link(*args, **kwargs):
        _RecordingLock.events.append(("link", None, db.in_transaction()))
        return real_link(*args, **kwargs)

    hint = algorithm if algorithm != "sha256" else None
    with patch.object(storage_model, "GlobalLock", _RecordingLock), patch.object(
        blob_model, "_temp_link_blob", side_effect=recording_link
    ):
        with upload_blob(repo_ref, storage, SETTINGS, requested_digest_algorithm=hint) as m:
            m.upload_chunk(APP_CONFIG, BytesIO(data))
            m.commit_to_blob(APP_CONFIG, _digest(algorithm, data))

    events = [
        e for e in _RecordingLock.events if e[1] in (None, "BLOB_DELETE_%s" % sha256)
    ]
    # Acquire, then link inside the open publication transaction, then release only after the
    # transaction committed.
    assert [e[0] for e in events] == ["acquire", "link", "release"]
    assert events[0][2] is False
    assert events[1][2] is True
    assert events[2][2] is False
    assert UploadedBlob.select().join(ImageStorage).where(
        ImageStorage.content_checksum == sha256
    ).exists()


class _GCHeldLock:
    lock_factory = object()

    def __init__(self, name, lock_ttl=600, auto_renewal=False, blocking_timeout=None):
        pass

    def __enter__(self):
        raise LockAcquireTimeout(holder="gcworker:1:test")

    def __exit__(self, *args):
        return False


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_gc_held_lock_blocks_row_creation(algorithm, repo_ref, storage):
    data = os.urandom(60)
    hint = algorithm if algorithm != "sha256" else None
    with patch.object(storage_model, "GlobalLock", _GCHeldLock):
        with pytest.raises(LockAcquireTimeout):
            with upload_blob(repo_ref, storage, SETTINGS, requested_digest_algorithm=hint) as m:
                m.upload_chunk(APP_CONFIG, BytesIO(data))
                m.commit_to_blob(APP_CONFIG, _digest(algorithm, data))

    assert _rows_for(_digest("sha256", data)) == []


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_gc_held_lock_allows_reusing_existing_row(algorithm, repo_ref, storage, registry_model):
    data = os.urandom(60)
    hint = algorithm if algorithm != "sha256" else None
    with upload_blob(repo_ref, storage, SETTINGS, requested_digest_algorithm=hint) as m:
        m.upload_chunk(APP_CONFIG, BytesIO(data))
        first = m.commit_to_blob(APP_CONFIG, _digest(algorithm, data))

    with patch.object(storage_model, "GlobalLock", _GCHeldLock):
        with upload_blob(repo_ref, storage, SETTINGS, requested_digest_algorithm=hint) as m:
            m.upload_chunk(APP_CONFIG, BytesIO(data))
            second = m.commit_to_blob(APP_CONFIG, _digest(algorithm, data))

    assert second._db_id == first._db_id


def test_concurrent_requested_publication_reuses_winner(
    repo_ref, storage, registry_model, real_transactions
):
    import data.model.blob as blob_model

    data = os.urandom(60)
    sha256 = _digest("sha256", data)
    sha512 = _digest("sha512", data)
    real_find = blob_model._find_requested_row
    calls = {"n": 0}
    winner = {}

    def racing_find(blob_digest, requested_digest):
        calls["n"] += 1
        if calls["n"] == 1:
            # Another publisher commits the same pair between our lookup and our insert.
            winner["row"] = ImageStorage.create(
                content_checksum=blob_digest, requested_content_checksum=requested_digest
            )
            return None
        return real_find(blob_digest, requested_digest)

    with upload_blob(repo_ref, storage, SETTINGS, requested_digest_algorithm="sha512") as m:
        m.upload_chunk(APP_CONFIG, BytesIO(data))
        with patch.object(blob_model, "_find_requested_row", side_effect=racing_find):
            blob = m.commit_to_blob(APP_CONFIG, sha512)

    assert blob._db_id == winner["row"].id
    rows = _rows_for(sha256)
    assert [r.requested_content_checksum for r in rows] == [sha512]


def test_cancel_after_finalize_does_not_touch_storage(repo_ref, storage):
    data = os.urandom(60)
    with pytest.raises(RequestedDigestMismatchException):
        with upload_blob(repo_ref, storage, SETTINGS) as manager:
            manager.upload_chunk(APP_CONFIG, BytesIO(data))
            with patch.object(storage, "cancel_chunked_upload") as cancel:
                try:
                    manager.commit_to_blob(APP_CONFIG, _digest("sha512", data + b"!"))
                finally:
                    manager.cancel_upload()
                    assert cancel.call_count == 0
