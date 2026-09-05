from unittest.mock import patch

from test.fixtures import *
from workers.gc.gcworker import GarbageCollectionWorker


def test_gc(initialized_db):
    worker = GarbageCollectionWorker()
    worker._garbage_collection_repos(skip_lock_for_testing=True)


def test_gc_processes_scanner_cleanup_without_repository_candidate(initialized_db):
    worker = GarbageCollectionWorker()

    with (
        patch("workers.gc.gcworker.garbage_collect_secscan_reports") as cleanup_reports,
        patch("workers.gc.gcworker.get_random_gc_policy", return_value=None),
    ):
        worker._garbage_collection_repos(skip_lock_for_testing=True)

    cleanup_reports.assert_called_once_with()
