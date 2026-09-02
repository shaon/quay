import hashlib
import os
import tarfile
from contextlib import closing
from io import BytesIO
from unittest.mock import patch

import pytest

from app import app as application
from data.database import BlobUpload, ImageStorage, RepositoryBlobDigest, UploadedBlob
from data.registry_model.blobuploader import (
    BlobDigestMismatchException,
    BlobTooLargeException,
    BlobUploadException,
    BlobUploadInvalidStateException,
    BlobUploadSettings,
    create_blob_upload,
    retrieve_blob_upload_manager,
    upload_blob,
)
from data.registry_model.registry_oci_model import OCIModel
from digest.digest_tools import Digest
from storage.distributedstorage import DistributedStorage
from storage.fakestorage import FakeStorage
from test.fixtures import *


@pytest.fixture()
def registry_model(initialized_db):
    assert application.config["TESTING"]
    return OCIModel()


@pytest.mark.parametrize(
    "chunk_count",
    [
        0,
        1,
        2,
        10,
    ],
)
@pytest.mark.parametrize(
    "subchunk",
    [
        True,
        False,
    ],
)
def test_basic_upload_blob(chunk_count, subchunk, registry_model):
    repository_ref = registry_model.lookup_repository("devtable", "complex")
    storage = DistributedStorage({"local_us": FakeStorage(None)}, ["local_us"])
    settings = BlobUploadSettings("2M", 3600)
    app_config = {"TESTING": True}

    data = b""
    with upload_blob(repository_ref, storage, settings) as manager:
        assert manager
        assert manager.blob_upload_id

        for index in range(0, chunk_count):
            chunk_data = os.urandom(100)
            data += chunk_data

            if subchunk:
                manager.upload_chunk(app_config, BytesIO(chunk_data))
                manager.upload_chunk(app_config, BytesIO(chunk_data), (index * 100) + 50)
            else:
                manager.upload_chunk(app_config, BytesIO(chunk_data))

        blob = manager.commit_to_blob(app_config)

    # Check the blob.
    assert blob.compressed_size == len(data)
    assert blob.digest == "sha256:" + hashlib.sha256(data).hexdigest()

    # Ensure the blob exists in storage and has the expected data.
    assert storage.get_content(["local_us"], blob.storage_path) == data


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_prefetch_finalizes_bytes_without_repository_visibility(algorithm, registry_model):
    repository_ref = registry_model.lookup_repository("devtable", "complex")
    storage = DistributedStorage({"local_us": FakeStorage(None)}, ["local_us"])
    settings = BlobUploadSettings("2M", 3600)
    content = b"physically prefetched but not repository visible"
    expected_digest = Digest.parse_digest(
        f"{algorithm}:" + hashlib.new(algorithm, content).hexdigest(), strict=True
    )
    manager = create_blob_upload(
        repository_ref, storage, settings, requested_digest_algorithm=algorithm
    )
    manager.upload_chunk({"TESTING": True}, BytesIO(content))

    manager.prefetch_to_storage({"TESTING": True}, expected_digest)
    location, path = manager.prefetched_storage_location()

    assert storage.get_content([location], path) == content
    assert BlobUpload.get(uuid=manager.blob_upload_id)
    assert (
        not RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == repository_ref.id,
            RepositoryBlobDigest.digest == str(expected_digest),
        )
        .exists()
    )
    assert (
        not UploadedBlob.select()
        .join(ImageStorage)
        .where(
            UploadedBlob.repository == repository_ref.id,
            ImageStorage.content_checksum == "sha256:" + hashlib.sha256(content).hexdigest(),
        )
        .exists()
    )
    assert registry_model.get_repo_blob_by_digest(repository_ref, str(expected_digest)) is None

    blob = manager.commit_prefetched_blob(expected_digest)

    assert blob is not None
    assert registry_model.get_repo_blob_by_digest(repository_ref, str(expected_digest)) == blob
    assert not BlobUpload.select().where(BlobUpload.uuid == manager.blob_upload_id).exists()


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_alternative_upload_resume_register_and_lookup(algorithm, registry_model):
    repository_ref = registry_model.lookup_repository("devtable", "complex")
    storage = DistributedStorage({"local_us": FakeStorage(None)}, ["local_us"])
    settings = BlobUploadSettings("2M", 3600)
    app_config = {"TESTING": True}
    content = os.urandom(32)
    expected_digest = Digest.parse_digest(
        f"{algorithm}:" + hashlib.new(algorithm, content).hexdigest(), strict=True
    )

    manager = create_blob_upload(
        repository_ref, storage, settings, requested_digest_algorithm=algorithm
    )
    manager.upload_chunk(app_config, BytesIO(content[:16]))

    persisted = BlobUpload.get(uuid=manager.blob_upload_id)
    assert persisted.requested_digest_algorithm == algorithm
    assert persisted.requested_digest_state is not None

    manager = retrieve_blob_upload_manager(
        repository_ref, manager.blob_upload_id, storage, settings
    )
    manager.upload_chunk(app_config, BytesIO(content[16:]))
    blob = manager.commit_to_blob(app_config, expected_digest)

    assert blob.digest == "sha256:" + hashlib.sha256(content).hexdigest()
    assert storage.get_content(["local_us"], blob.storage_path) == content
    assert registry_model.get_repo_blob_by_digest(repository_ref, str(expected_digest)) == blob
    assert registry_model.get_repo_blob_by_digest(repository_ref, blob.digest) is None
    registration = RepositoryBlobDigest.get(
        repository=repository_ref.id, digest=str(expected_digest)
    )
    assert registration.image_storage.content_checksum == blob.digest


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_alternative_digest_mismatch_does_not_register_blob(algorithm, registry_model):
    repository_ref = registry_model.lookup_repository("devtable", "complex")
    storage = DistributedStorage({"local_us": FakeStorage(None)}, ["local_us"])
    settings = BlobUploadSettings("2M", 3600)
    app_config = {"TESTING": True}
    manager = create_blob_upload(
        repository_ref, storage, settings, requested_digest_algorithm=algorithm
    )
    manager.upload_chunk(app_config, BytesIO(b"actual content"))
    wrong_digest = Digest.parse_digest(
        f"{algorithm}:" + hashlib.new(algorithm, b"different content").hexdigest(),
        strict=True,
    )

    with pytest.raises(BlobDigestMismatchException):
        manager.commit_to_blob(app_config, wrong_digest)

    assert (
        not RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == repository_ref.id,
            RepositoryBlobDigest.digest == str(wrong_digest),
        )
        .exists()
    )
    manager.cancel_upload()


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_hintless_chunked_alternative_finalization_is_rejected(algorithm, registry_model):
    repository_ref = registry_model.lookup_repository("devtable", "complex")
    storage = DistributedStorage({"local_us": FakeStorage(None)}, ["local_us"])
    settings = BlobUploadSettings("2M", 3600)
    app_config = {"TESTING": True}
    manager = create_blob_upload(repository_ref, storage, settings)
    manager.upload_chunk(app_config, BytesIO(b"earlier chunk"))
    expected_digest = Digest.parse_digest(
        f"{algorithm}:" + hashlib.new(algorithm, b"earlier chunk").hexdigest(),
        strict=True,
    )

    with pytest.raises(BlobDigestMismatchException):
        manager.prepare_for_digest(expected_digest)

    assert (
        not RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == repository_ref.id,
            RepositoryBlobDigest.digest == str(expected_digest),
        )
        .exists()
    )
    manager.cancel_upload()


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_hintless_alternative_state_can_resume_without_storage_readback(algorithm, registry_model):
    repository_ref = registry_model.lookup_repository("devtable", "complex")
    storage = DistributedStorage({"local_us": FakeStorage(None)}, ["local_us"])
    settings = BlobUploadSettings("2M", 3600)
    app_config = {"TESTING": True}
    content = b"hintless alternative upload"
    manager = create_blob_upload(
        repository_ref,
        storage,
        settings,
        requested_digest_algorithm=algorithm,
        requested_digest_is_explicit=False,
    )
    manager.upload_chunk(app_config, BytesIO(content[:10]))

    manager = retrieve_blob_upload_manager(
        repository_ref, manager.blob_upload_id, storage, settings
    )
    assert manager.requested_digest_is_explicit() is False
    manager.upload_chunk(app_config, BytesIO(content[10:]))
    expected_digest = Digest.parse_digest(
        f"{algorithm}:" + hashlib.new(algorithm, content).hexdigest(), strict=True
    )

    with patch.object(storage, "stream_read", side_effect=AssertionError("storage readback")):
        blob = manager.commit_to_blob(app_config, expected_digest)

    assert blob.digest == "sha256:" + hashlib.sha256(content).hexdigest()
    assert registry_model.get_repo_blob_by_digest(repository_ref, str(expected_digest)) == blob


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_stale_explicit_state_is_rejected_after_older_worker_chunk(algorithm, registry_model):
    repository_ref = registry_model.lookup_repository("devtable", "complex")
    storage = DistributedStorage({"local_us": FakeStorage(None)}, ["local_us"])
    settings = BlobUploadSettings("2M", 3600)
    manager = create_blob_upload(
        repository_ref, storage, settings, requested_digest_algorithm=algorithm
    )
    manager.upload_chunk({"TESTING": True}, BytesIO(b"chunk"))
    BlobUpload.update(byte_count=manager.blob_upload.byte_count + 1).where(
        BlobUpload.uuid == manager.blob_upload_id
    ).execute()

    with pytest.raises(BlobUploadInvalidStateException):
        retrieve_blob_upload_manager(repository_ref, manager.blob_upload_id, storage, settings)

    manager.cancel_upload()


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_stale_hintless_state_degrades_to_legacy_sha256(algorithm, registry_model):
    repository_ref = registry_model.lookup_repository("devtable", "complex")
    storage = DistributedStorage({"local_us": FakeStorage(None)}, ["local_us"])
    settings = BlobUploadSettings("2M", 3600)
    manager = create_blob_upload(
        repository_ref,
        storage,
        settings,
        requested_digest_algorithm=algorithm,
        requested_digest_is_explicit=False,
    )
    manager.upload_chunk({"TESTING": True}, BytesIO(b"chunk"))
    BlobUpload.update(byte_count=manager.blob_upload.byte_count + 1).where(
        BlobUpload.uuid == manager.blob_upload_id
    ).execute()

    resumed = retrieve_blob_upload_manager(
        repository_ref, manager.blob_upload_id, storage, settings
    )

    assert resumed.blob_upload.requested_digest_algorithm is None
    assert resumed.requested_digest_hasher is None
    manager.cancel_upload()


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_explicit_alternative_session_can_finalize_as_authoritative_sha256(
    algorithm, registry_model
):
    repository_ref = registry_model.lookup_repository("devtable", "complex")
    storage = DistributedStorage({"local_us": FakeStorage(None)}, ["local_us"])
    settings = BlobUploadSettings("2M", 3600)
    content = b"final digest wins"
    manager = create_blob_upload(
        repository_ref, storage, settings, requested_digest_algorithm=algorithm
    )
    manager.upload_chunk({"TESTING": True}, BytesIO(content))
    expected_digest = Digest.parse_digest(
        "sha256:" + hashlib.sha256(content).hexdigest(), strict=True
    )

    blob = manager.commit_to_blob({"TESTING": True}, expected_digest)

    assert blob.digest == str(expected_digest)
    assert registry_model.get_repo_blob_by_digest(repository_ref, str(expected_digest)) == blob


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_corrupt_requested_digest_state_is_rejected(algorithm, registry_model):
    repository_ref = registry_model.lookup_repository("devtable", "complex")
    storage = DistributedStorage({"local_us": FakeStorage(None)}, ["local_us"])
    settings = BlobUploadSettings("2M", 3600)
    app_config = {"TESTING": True}
    registration_count = (
        RepositoryBlobDigest.select()
        .where(RepositoryBlobDigest.repository == repository_ref.id)
        .count()
    )
    manager = create_blob_upload(
        repository_ref, storage, settings, requested_digest_algorithm=algorithm
    )
    manager.upload_chunk(app_config, BytesIO(b"hello"))
    BlobUpload.update(requested_digest_state="corrupt").where(
        BlobUpload.uuid == manager.blob_upload_id
    ).execute()

    with pytest.raises(BlobUploadInvalidStateException):
        retrieve_blob_upload_manager(repository_ref, manager.blob_upload_id, storage, settings)

    assert (
        RepositoryBlobDigest.select()
        .where(RepositoryBlobDigest.repository == repository_ref.id)
        .count()
        == registration_count
    )
    manager.cancel_upload()


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_story16_cancel_removes_requested_digest_state_and_temporary_bytes(
    algorithm, registry_model
):
    repository_ref = registry_model.lookup_repository("devtable", "complex")
    storage = DistributedStorage({"local_us": FakeStorage(None)}, ["local_us"])
    settings = BlobUploadSettings("2M", 3600)
    manager = create_blob_upload(
        repository_ref, storage, settings, requested_digest_algorithm=algorithm
    )
    manager.upload_chunk({"TESTING": True}, BytesIO(b"abandoned alternative upload"))
    upload_id = manager.blob_upload_id

    persisted = BlobUpload.get(uuid=upload_id)
    assert persisted.requested_digest_algorithm == algorithm
    assert persisted.requested_digest_state is not None
    assert storage.exists(["local_us"], upload_id)

    manager.cancel_upload()

    assert not BlobUpload.select().where(BlobUpload.uuid == upload_id).exists()
    assert not storage.exists(["local_us"], upload_id)


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_story16_successful_resume_removes_hash_state_and_keeps_canonical_content(
    algorithm, registry_model
):
    repository_ref = registry_model.lookup_repository("devtable", "complex")
    storage = DistributedStorage({"local_us": FakeStorage(None)}, ["local_us"])
    settings = BlobUploadSettings("2M", 3600)
    content = b"successfully finalized alternative upload"
    requested_digest = Digest.parse_digest(
        f"{algorithm}:" + hashlib.new(algorithm, content).hexdigest(), strict=True
    )
    manager = create_blob_upload(
        repository_ref, storage, settings, requested_digest_algorithm=algorithm
    )
    manager.upload_chunk({"TESTING": True}, BytesIO(content[:17]))
    upload_id = manager.blob_upload_id

    manager = retrieve_blob_upload_manager(repository_ref, upload_id, storage, settings)
    manager.upload_chunk({"TESTING": True}, BytesIO(content[17:]))
    blob = manager.commit_to_blob({"TESTING": True}, requested_digest)

    assert not BlobUpload.select().where(BlobUpload.uuid == upload_id).exists()
    assert blob.digest == "sha256:" + hashlib.sha256(content).hexdigest()
    assert storage.get_content(["local_us"], blob.storage_path) == content
    assert registry_model.get_repo_blob_by_digest(repository_ref, str(requested_digest)) == blob


def test_legacy_null_requested_digest_state_resumes_sha256(registry_model):
    repository_ref = registry_model.lookup_repository("devtable", "complex")
    storage = DistributedStorage({"local_us": FakeStorage(None)}, ["local_us"])
    settings = BlobUploadSettings("2M", 3600)
    app_config = {"TESTING": True}
    content = b"legacy upload"
    manager = create_blob_upload(repository_ref, storage, settings)
    manager.upload_chunk(app_config, BytesIO(content[:6]))

    manager = retrieve_blob_upload_manager(
        repository_ref, manager.blob_upload_id, storage, settings
    )
    manager.upload_chunk(app_config, BytesIO(content[6:]))
    digest = Digest.parse_digest("sha256:" + hashlib.sha256(content).hexdigest(), strict=True)
    blob = manager.commit_to_blob(app_config, str(digest))

    assert blob.digest == str(digest)
    assert registry_model.get_repo_blob_by_digest(repository_ref, str(digest)) == blob


def test_cancel_upload(registry_model):
    repository_ref = registry_model.lookup_repository("devtable", "complex")
    storage = DistributedStorage({"local_us": FakeStorage(None)}, ["local_us"])
    settings = BlobUploadSettings("2M", 3600)
    app_config = {"TESTING": True}

    blob_upload_id = None
    with upload_blob(repository_ref, storage, settings) as manager:
        blob_upload_id = manager.blob_upload_id
        assert registry_model.lookup_blob_upload(repository_ref, blob_upload_id) is not None

        manager.upload_chunk(app_config, BytesIO(b"hello world"))

    # Since the blob was not comitted, the upload should be deleted.
    assert blob_upload_id
    assert registry_model.lookup_blob_upload(repository_ref, blob_upload_id) is None


def test_too_large(registry_model):
    repository_ref = registry_model.lookup_repository("devtable", "complex")
    storage = DistributedStorage({"local_us": FakeStorage(None)}, ["local_us"])
    settings = BlobUploadSettings("1K", 3600)
    app_config = {"TESTING": True}

    with upload_blob(repository_ref, storage, settings) as manager:
        with pytest.raises(BlobTooLargeException):
            manager.upload_chunk(app_config, BytesIO(os.urandom(1024 * 1024 * 2)))


def test_extra_blob_stream_handlers(registry_model):
    handler1_result = []
    handler2_result = []

    def handler1(bytes_data):
        handler1_result.append(bytes_data)

    def handler2(bytes_data):
        handler2_result.append(bytes_data)

    repository_ref = registry_model.lookup_repository("devtable", "complex")
    storage = DistributedStorage({"local_us": FakeStorage(None)}, ["local_us"])
    settings = BlobUploadSettings("1K", 3600)
    app_config = {"TESTING": True}

    with upload_blob(
        repository_ref, storage, settings, extra_blob_stream_handlers=[handler1, handler2]
    ) as manager:
        manager.upload_chunk(app_config, BytesIO(b"hello "))
        manager.upload_chunk(app_config, BytesIO(b"world"))

    assert b"".join(handler1_result) == b"hello world"
    assert b"".join(handler2_result) == b"hello world"


def valid_tar_gz(contents):
    assert isinstance(contents, bytes)
    with closing(BytesIO()) as layer_data:
        with closing(tarfile.open(fileobj=layer_data, mode="w|gz")) as tar_file:
            tar_file_info = tarfile.TarInfo(name="somefile")
            tar_file_info.type = tarfile.REGTYPE
            tar_file_info.size = len(contents)
            tar_file_info.mtime = 1
            tar_file.addfile(tar_file_info, BytesIO(contents))

        layer_bytes = layer_data.getvalue()
    return layer_bytes


def test_uncompressed_size(registry_model):
    repository_ref = registry_model.lookup_repository("devtable", "complex")
    storage = DistributedStorage({"local_us": FakeStorage(None)}, ["local_us"])
    settings = BlobUploadSettings("1K", 3600)
    app_config = {"TESTING": True}

    with upload_blob(repository_ref, storage, settings) as manager:
        manager.upload_chunk(app_config, BytesIO(valid_tar_gz(b"hello world")))

        blob = manager.commit_to_blob(app_config)

    assert blob.compressed_size is not None
    assert blob.uncompressed_size is not None
