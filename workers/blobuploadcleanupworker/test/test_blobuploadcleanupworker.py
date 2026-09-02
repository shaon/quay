from contextlib import contextmanager
from datetime import datetime, timedelta

import boto3
from mock import Mock, patch

from app import app as realapp
from data import model as data_model
from data.database import BlobUpload as BlobUploadTable
from digest import digest_tools
from test.fixtures import *
from workers.blobuploadcleanupworker.blobuploadcleanupworker import (
    LOCK_TTL,
    MPU_DELETION_DATE_THRESHOLD,
    BlobUploadCleanupWorker,
)
from workers.blobuploadcleanupworker.models_pre_oci import pre_oci_model as model


@contextmanager
def _keep_test_database_connected(_):
    yield


def _run_upload_cleanup(storage_mock):
    with (
        patch(
            "workers.blobuploadcleanupworker.blobuploadcleanupworker.UseThenDisconnect",
            _keep_test_database_connected,
        ),
        patch("workers.blobuploadcleanupworker.blobuploadcleanupworker.storage", storage_mock),
    ):
        BlobUploadCleanupWorker()._cleanup_uploads()


def _create_upload(
    upload_uuid,
    repository_name="simple",
    requested_digest_algorithm=None,
    requested_digest_state=None,
    stale=True,
):
    repository = data_model.repository.get_repository("devtable", repository_name)
    upload = data_model.blob.initiate_upload_for_repo(
        repository,
        upload_uuid,
        "local_us",
        {"upload": upload_uuid},
        requested_digest_algorithm=requested_digest_algorithm,
        requested_digest_state=requested_digest_state,
    )
    if stale:
        upload.created = datetime.now() - timedelta(days=60)
        upload.save()
    return upload


def _requested_digest_state(algorithm):
    return digest_tools.serialize_resumable_hasher(
        algorithm,
        digest_tools.create_resumable_hasher(algorithm),
        bytes_hashed=0,
        origin=digest_tools.RESUMABLE_HASH_ORIGIN_HINT,
    )


def test_blobuploadcleanupworker(initialized_db):
    # Create a blob upload older than the threshold.
    blob_upload = model.create_stale_upload_for_testing()

    # Note: We need to override UseThenDisconnect to ensure to remains connected to the test DB.
    @contextmanager
    def noop(_):
        yield

    storage_mock = Mock()
    with patch("workers.blobuploadcleanupworker.blobuploadcleanupworker.UseThenDisconnect", noop):
        with patch("workers.blobuploadcleanupworker.blobuploadcleanupworker.storage", storage_mock):
            # Call cleanup and ensure it is canceled.
            worker = BlobUploadCleanupWorker()
            worker._cleanup_uploads()

            storage_mock.locations = ["default"]
            worker._try_clean_partial_uploads()

    storage_mock.clean_partial_uploads.assert_called_once()
    storage_mock.cancel_chunked_upload.assert_called_once()

    # Ensure the blob no longer exists.
    assert not model.blob_upload_exists(blob_upload.uuid)


def test_story16_expiration_removes_all_hash_state_variants_and_is_isolated(initialized_db):
    stale_uploads = [
        _create_upload("story16-legacy"),
        _create_upload("story16-sha256", requested_digest_algorithm="sha256"),
        _create_upload(
            "story16-sha384",
            requested_digest_algorithm="sha384",
            requested_digest_state=_requested_digest_state("sha384"),
        ),
        _create_upload(
            "story16-sha512-disabled",
            requested_digest_algorithm="sha512",
            requested_digest_state=_requested_digest_state("sha512"),
        ),
        _create_upload("story16-missing-state", requested_digest_algorithm="sha384"),
        _create_upload("story16-orphan-state", requested_digest_state="orphaned-state"),
        _create_upload(
            "story16-corrupt-state",
            requested_digest_algorithm="sha512",
            requested_digest_state="corrupt-state",
        ),
        _create_upload(
            "story16-unknown-algorithm",
            requested_digest_algorithm="sha999",
            requested_digest_state="unknown-state",
        ),
    ]
    fresh_upload = _create_upload(
        "story16-fresh-other-repository",
        repository_name="complex",
        requested_digest_algorithm="sha512",
        requested_digest_state=_requested_digest_state("sha512"),
        stale=False,
    )
    storage_mock = Mock()

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256"]}):
        _run_upload_cleanup(storage_mock)

    assert (
        not BlobUploadTable.select()
        .where(BlobUploadTable.id << [upload.id for upload in stale_uploads])
        .exists()
    )
    preserved = BlobUploadTable.get_by_id(fresh_upload.id)
    assert preserved.requested_digest_algorithm == "sha512"
    assert preserved.requested_digest_state == fresh_upload.requested_digest_state
    assert storage_mock.cancel_chunked_upload.call_count == len(stale_uploads)
    actual_cancellations = {
        (tuple(cancellation.args[0]), cancellation.args[1], cancellation.args[2]["upload"])
        for cancellation in storage_mock.cancel_chunked_upload.call_args_list
    }
    assert actual_cancellations == {
        (("local_us",), upload.uuid, upload.uuid) for upload in stale_uploads
    }


def test_story16_expiration_retries_storage_failure_before_discarding_state(initialized_db):
    state = _requested_digest_state("sha512")
    upload = _create_upload(
        "story16-retry",
        requested_digest_algorithm="sha512",
        requested_digest_state=state,
    )
    independent_upload = _create_upload(
        "story16-retry-independent",
        repository_name="complex",
        requested_digest_algorithm="sha384",
        requested_digest_state=_requested_digest_state("sha384"),
    )
    storage_mock = Mock()

    def fail_one_upload(_locations, upload_uuid, _metadata):
        if upload_uuid == upload.uuid:
            raise OSError("temporary storage failure")

    storage_mock.cancel_chunked_upload.side_effect = fail_one_upload
    _run_upload_cleanup(storage_mock)

    persisted = BlobUploadTable.get_by_id(upload.id)
    assert persisted.requested_digest_algorithm == "sha512"
    assert persisted.requested_digest_state == state
    assert not BlobUploadTable.select().where(BlobUploadTable.id == independent_upload.id).exists()

    storage_mock.cancel_chunked_upload.side_effect = None
    _run_upload_cleanup(storage_mock)

    assert not BlobUploadTable.select().where(BlobUploadTable.id == upload.id).exists()
    assert storage_mock.cancel_chunked_upload.call_count == 3


def test_story16_expiration_treats_missing_storage_as_idempotent_success(initialized_db):
    upload = _create_upload(
        "story16-storage-missing",
        requested_digest_algorithm="sha512",
        requested_digest_state="corrupt-state",
    )
    storage_mock = Mock()
    storage_mock.cancel_chunked_upload.side_effect = FileNotFoundError("already removed")

    _run_upload_cleanup(storage_mock)
    _run_upload_cleanup(storage_mock)

    assert not BlobUploadTable.select().where(BlobUploadTable.id == upload.id).exists()
    storage_mock.cancel_chunked_upload.assert_called_once_with(
        ["local_us"], upload.uuid, {"upload": upload.uuid}
    )


def test_story16_expiration_tolerates_upload_disappearance_and_repeated_cleanup(initialized_db):
    upload = _create_upload(
        "story16-disappears",
        requested_digest_algorithm="sha384",
        requested_digest_state="corrupt-state",
    )
    storage_mock = Mock()

    def remove_upload_during_storage_cleanup(*_args):
        BlobUploadTable.delete().where(BlobUploadTable.id == upload.id).execute()

    storage_mock.cancel_chunked_upload.side_effect = remove_upload_during_storage_cleanup

    _run_upload_cleanup(storage_mock)
    _run_upload_cleanup(storage_mock)

    assert not BlobUploadTable.select().where(BlobUploadTable.id == upload.id).exists()
    storage_mock.cancel_chunked_upload.assert_called_once_with(
        ["local_us"], upload.uuid, {"upload": upload.uuid}
    )


def test_blobuploadcleanupworker_calls_mpu_cleanup(initialized_db):
    """
    Asserts that the MPU cleanup function is called from the worker.
    """
    storage_mock = Mock()
    storage_mock.preferred_locations = ["default"]

    # verify that the deletion threshold is always 1 day
    assert MPU_DELETION_DATE_THRESHOLD == timedelta(days=1)

    # we'll mock the deleted count
    storage_mock.clean_orphaned_multipart_uploads.return_value = 5

    with patch("workers.blobuploadcleanupworker.blobuploadcleanupworker.GlobalLock"):
        with patch("workers.blobuploadcleanupworker.blobuploadcleanupworker.storage", storage_mock):

            # call cleanup and ensure it's cancelled
            worker = BlobUploadCleanupWorker()
            worker._try_clean_stale_multipart_uploads()

        storage_mock.clean_orphaned_multipart_uploads.assert_called_once_with(
            ["default"], MPU_DELETION_DATE_THRESHOLD
        )


def test_mpu_cleanup_exits_if_no_preferred_storage_location_is_found(initialized_db):
    """
    Checks that the MPU cleanup is not called if preferred storage engine is not set.
    """
    storage_mock = Mock()
    storage_mock.preferred_locations = []

    with patch("workers.blobuploadcleanupworker.blobuploadcleanupworker.GlobalLock"):
        with patch("workers.blobuploadcleanupworker.blobuploadcleanupworker.storage", storage_mock):
            worker = BlobUploadCleanupWorker()
            worker._try_clean_stale_multipart_uploads()

    storage_mock.clean_orphaned_multipart_uploads.assert_not_called()


def test_partial_blob_cleanup_exits_if_no_preferred_storage_location_is_found(initialized_db):
    """
    Checks that the partial blob cleanup is not called if preferred storage engine is not set.
    """
    storage_mock = Mock()
    storage_mock.preferred_locations = []

    with patch("workers.blobuploadcleanupworker.blobuploadcleanupworker.GlobalLock"):
        with patch("workers.blobuploadcleanupworker.blobuploadcleanupworker.storage", storage_mock):
            worker = BlobUploadCleanupWorker()
            worker._try_clean_partial_uploads()

    storage_mock.clean_partial_uploads.assert_not_called()


def test_verify_operation_is_not_registered_if_feature_flag_is_disabled(initialized_db):
    """
    Verifies that the job is not scheduled unless the feature flag is set.
    """
    with patch.dict(realapp.config, {"FEATURE_ENABLE_STALE_MPU_CLEANUP": False}):
        with patch.object(BlobUploadCleanupWorker, "add_operation") as mock_add:
            BlobUploadCleanupWorker()

        registered = [c.args[0].__name__ for c in mock_add.call_args_list]
        assert "_try_clean_stale_multipart_uploads" not in registered


def test_verify_operation_is_registered_if_feature_flag_is_enabled(initialized_db):
    """
    Asserts that the job operation is scheduled if the feature flag is set.
    """
    with patch.dict(realapp.config, {"FEATURE_ENABLE_STALE_MPU_CLEANUP": True}):
        with patch.object(BlobUploadCleanupWorker, "add_operation") as mock_add:
            BlobUploadCleanupWorker()

        registered = [c.args[0].__name__ for c in mock_add.call_args_list]
        assert "_try_clean_stale_multipart_uploads" in registered


def test_verify_that_worker_acquires_a_global_lock_with_proper_values(initialized_db):
    """
    Verifies that a GlobalLock is acquired if the worker is called and that proper values
    were sent.
    """
    from util.locking import GlobalLock

    captured = {}

    class _FakeLock:
        def __init__(self, name, expire=None, auto_renewal=False):
            self._name = name
            self._expire = expire
            self._auto_renewal = auto_renewal
            captured.update(name=name, expire=expire, auto_renewal=auto_renewal)

        def acquire(self):
            return True

        def release(self):
            pass

    storage_mock = Mock()

    storage_mock.preferred_locations = ["default"]
    storage_mock.clean_orphaned_multipart_uploads.return_value = 0

    with patch.object(GlobalLock, "lock_factory", staticmethod(_FakeLock)):
        with patch("workers.blobuploadcleanupworker.blobuploadcleanupworker.storage", storage_mock):
            worker = BlobUploadCleanupWorker()
            worker._try_clean_stale_multipart_uploads()

        # verify that the lock is initialized with proper values
        assert captured["name"] == "STALE_MPU_CLEANUP"
        assert captured["expire"] == LOCK_TTL
        assert captured["auto_renewal"] is False

        storage_mock.clean_orphaned_multipart_uploads.assert_called_once_with(
            ["default"], MPU_DELETION_DATE_THRESHOLD
        )


def test_verify_that_multipart_cleanup_does_not_run_if_lock_cannot_be_acquired(initialized_db):
    """
    Verifies that cleanup of orphaned MPUs is not called if GlobalLock cannot be acquired.
    """
    from util.locking import GlobalLock, LockNotAcquiredException

    class _FakeLock:
        def __init__(self, name, expire=None, auto_renewal=False):
            self._name = name

        def acquire(self):
            return False

        def release(self):
            pass

    storage_mock = Mock()
    storage_mock.preferred_locations = ["default"]

    with patch.object(GlobalLock, "lock_factory", staticmethod(_FakeLock)):
        with patch("workers.blobuploadcleanupworker.blobuploadcleanupworker.storage", storage_mock):
            worker = BlobUploadCleanupWorker()
            worker._try_clean_stale_multipart_uploads()

        storage_mock.clean_orphaned_multipart_uploads.assert_not_called()
