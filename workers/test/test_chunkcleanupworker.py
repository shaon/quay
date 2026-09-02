from unittest.mock import Mock, patch

from data import model
from data.database import BlobUpload
from test.fixtures import *
from workers.chunkcleanupworker import ChunkCleanupWorker


def test_story16_chunk_cleanup_is_idempotent_and_does_not_terminate_upload(initialized_db):
    repository = model.repository.get_repository("devtable", "simple")
    upload = model.blob.initiate_upload_for_repo(
        repository,
        "story16-chunk-owner",
        "local_us",
        {"segments": ["queued-segment"]},
        requested_digest_algorithm="sha512",
        requested_digest_state="corrupt-state-is-not-deserialized",
    )
    storage_mock = Mock()
    storage_mock.exists.side_effect = [True, False]
    worker = ChunkCleanupWorker(Mock())
    job = {"location": "local_us", "path": "segments/queued-segment"}

    with patch("workers.chunkcleanupworker.storage", storage_mock):
        worker.process_queue_item(job)
        worker.process_queue_item(job)

    storage_mock.remove.assert_called_once_with(["local_us"], "segments/queued-segment")
    persisted = BlobUpload.get_by_id(upload.id)
    assert persisted.requested_digest_algorithm == "sha512"
    assert persisted.requested_digest_state == "corrupt-state-is-not-deserialized"
