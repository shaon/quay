import hashlib
from test.fixtures import *

import pytest

from data import database, model
from storage.basestorage import StoragePaths
from storage.distributedstorage import DistributedStorage
from storage.fakestorage import FakeStorage
from workers.storagereplication import (
    JobException,
    StorageReplicationWorker,
    WorkerSleepException,
    WorkerUnhealthyException,
)


@pytest.fixture()
def storage_user(app):
    user = model.user.get_user("devtable")
    database.UserRegion.create(
        user=user, location=database.ImageStorageLocation.get(name="local_us")
    )
    database.UserRegion.create(
        user=user, location=database.ImageStorageLocation.get(name="local_eu")
    )
    return user


@pytest.fixture()
def storage_paths():
    return StoragePaths()


@pytest.fixture()
def replication_worker():
    return StorageReplicationWorker(None)


@pytest.fixture()
def storage():
    return DistributedStorage(
        {"local_us": FakeStorage("local"), "local_eu": FakeStorage("local")}, ["local_us"]
    )


def test_storage_replication_v1(storage_user, storage_paths, replication_worker, storage, app):
    # Add a storage entry with a V1 path.
    v1_storage = model.storage.create_v1_storage("local_us")
    content_path = storage_paths.v1_image_layer_path(v1_storage.uuid)
    storage.put_content(["local_us"], content_path, b"some content")

    # Call replicate on it and verify it replicates.
    replication_worker.replicate_storage(storage_user, v1_storage.uuid, storage)

    # Ensure that the data was replicated to the other "region".
    assert storage.get_content(["local_eu"], content_path) == b"some content"

    locations = model.storage.get_storage_locations(v1_storage.uuid)
    assert len(locations) == 2


def test_storage_replication_cas(storage_user, storage_paths, replication_worker, storage, app):
    # Add a storage entry with a CAS path.
    content_checksum = "sha256:" + hashlib.sha256(b"some content").hexdigest()
    cas_storage = database.ImageStorage.create(cas_path=True, content_checksum=content_checksum)

    location = database.ImageStorageLocation.get(name="local_us")
    database.ImageStoragePlacement.create(storage=cas_storage, location=location)

    content_path = storage_paths.blob_path(cas_storage.content_checksum)
    storage.put_content(["local_us"], content_path, b"some content")

    # Call replicate on it and verify it replicates.
    replication_worker.replicate_storage(storage_user, cas_storage.uuid, storage)

    # Ensure that the data was replicated to the other "region".
    assert storage.get_content(["local_eu"], content_path) == b"some content"

    locations = model.storage.get_storage_locations(cas_storage.uuid)
    assert len(locations) == 2


def test_storage_replication_missing_base(
    storage_user, storage_paths, replication_worker, storage, app
):
    # Add a storage entry with a CAS path.
    content_checksum = "sha256:" + hashlib.sha256(b"some content").hexdigest()
    cas_storage = database.ImageStorage.create(cas_path=True, content_checksum=content_checksum)

    location = database.ImageStorageLocation.get(name="local_us")
    database.ImageStoragePlacement.create(storage=cas_storage, location=location)

    # Attempt to replicate storage. This should fail because the layer is missing from the base
    # storage.
    with pytest.raises(JobException):
        replication_worker.replicate_storage(
            storage_user, cas_storage.uuid, storage, backoff_check=False
        )

    # Ensure the storage location count remains 1. This is technically inaccurate, but that's okay
    # as we still require at least one location per storage.
    locations = model.storage.get_storage_locations(cas_storage.uuid)
    assert len(locations) == 1


def test_storage_replication_copy_error(
    storage_user, storage_paths, replication_worker, storage, app
):
    # Add a storage entry with a CAS path.
    content_checksum = "sha256:" + hashlib.sha256(b"some content").hexdigest()
    cas_storage = database.ImageStorage.create(cas_path=True, content_checksum=content_checksum)

    location = database.ImageStorageLocation.get(name="local_us")
    database.ImageStoragePlacement.create(storage=cas_storage, location=location)

    content_path = storage_paths.blob_path(cas_storage.content_checksum)
    storage.put_content(["local_us"], content_path, b"some content")

    # Tell storage to break copying.
    storage.put_content(["local_us"], "break_copying", b"true")

    # Attempt to replicate storage. This should fail because the write fails.
    with pytest.raises(WorkerSleepException):
        replication_worker.replicate_storage(
            storage_user, cas_storage.uuid, storage, backoff_check=False
        )

    # Ensure the storage location count remains 1.
    locations = model.storage.get_storage_locations(cas_storage.uuid)
    assert len(locations) == 1


def test_storage_replication_copy_didnot_copy(
    storage_user, storage_paths, replication_worker, storage, app
):
    # Add a storage entry with a CAS path.
    content_checksum = "sha256:" + hashlib.sha256(b"some content").hexdigest()
    cas_storage = database.ImageStorage.create(cas_path=True, content_checksum=content_checksum)

    location = database.ImageStorageLocation.get(name="local_us")
    database.ImageStoragePlacement.create(storage=cas_storage, location=location)

    content_path = storage_paths.blob_path(cas_storage.content_checksum)
    storage.put_content(["local_us"], content_path, b"some content")

    # Tell storage to fake copying (i.e. not actually copy the data).
    storage.put_content(["local_us"], "fake_copying", b"true")

    # Attempt to replicate storage. This should fail because the copy doesn't actually do the copy.
    with pytest.raises(JobException):
        replication_worker.replicate_storage(
            storage_user, cas_storage.uuid, storage, backoff_check=False
        )

    # Ensure the storage location count remains 1.
    locations = model.storage.get_storage_locations(cas_storage.uuid)
    assert len(locations) == 1


# PROJQUAY-10117: stored-bytes verification at the destination before copying.


def _requested_row(content, location_name="local_us", image_size=None):
    sha256 = "sha256:" + hashlib.sha256(content).hexdigest()
    sha512 = "sha512:" + hashlib.sha512(content).hexdigest()
    row = database.ImageStorage.create(
        cas_path=True,
        content_checksum=sha256,
        requested_content_checksum=sha512,
        image_size=len(content) if image_size is None else image_size,
    )
    database.ImageStoragePlacement.create(
        storage=row, location=database.ImageStorageLocation.get(name=location_name)
    )
    return row


def test_replication_reuses_verified_object_without_copy(
    storage_user, storage_paths, replication_worker, storage, app
):
    content = b"geo content shared by two rows"
    row = _requested_row(content)
    path = storage_paths.blob_path(row.content_checksum)
    storage.put_content(["local_us"], path, content)
    # Another row's replication already stored the same CAS object in the EU region.
    storage.put_content(["local_eu"], path, content)

    copies = []
    real_copy = storage.copy_between
    storage.copy_between = lambda *args: copies.append(args) or real_copy(*args)

    replication_worker.replicate_storage(storage_user, row.uuid, storage)

    assert copies == []
    assert set(model.storage.get_storage_locations(row.uuid)) == {"local_us", "local_eu"}


@pytest.mark.parametrize("damage", ["size", "bytes"])
def test_replication_copies_when_existing_object_fails_verification(
    damage, storage_user, storage_paths, replication_worker, storage, app
):
    content = b"geo content that must be copied"
    row = _requested_row(content)
    path = storage_paths.blob_path(row.content_checksum)
    storage.put_content(["local_us"], path, content)
    bad = content + b"!" if damage == "size" else bytes([content[0] ^ 1]) + content[1:]
    storage.put_content(["local_eu"], path, bad)

    copies = []
    real_copy = storage.copy_between
    storage.copy_between = lambda *args: copies.append(args) or real_copy(*args)

    replication_worker.replicate_storage(storage_user, row.uuid, storage)

    assert len(copies) == 1
    assert storage.get_content(["local_eu"], path) == content
    assert set(model.storage.get_storage_locations(row.uuid)) == {"local_us", "local_eu"}


def test_replication_skips_read_when_cas_row_already_placed(
    storage_user, storage_paths, replication_worker, storage, app
):
    content = b"geo content placed for the sha256 row"
    row = _requested_row(content)
    path = storage_paths.blob_path(row.content_checksum)
    storage.put_content(["local_us"], path, content)
    storage.put_content(["local_eu"], path, content)

    # The SHA-256 row for the same bytes is already placed in the EU region.
    sha256_row = database.ImageStorage.create(
        cas_path=True, content_checksum=row.content_checksum, image_size=len(content)
    )
    database.ImageStoragePlacement.create(
        storage=sha256_row, location=database.ImageStorageLocation.get(name="local_eu")
    )

    reads = []
    real_read = storage.stream_read
    storage.stream_read = lambda *args, **kw: reads.append(args) or real_read(*args, **kw)

    replication_worker.replicate_storage(storage_user, row.uuid, storage)

    assert reads == []
    assert set(model.storage.get_storage_locations(row.uuid)) == {"local_us", "local_eu"}
