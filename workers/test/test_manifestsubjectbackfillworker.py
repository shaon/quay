from unittest.mock import patch

import pytest

from data import database, model
from data.model.oci.manifest import CreateManifestException
from test.fixtures import *
from workers.manifestsubjectbackfillworker import ManifestSubjectBackfillWorker


def test_basic(initialized_db):
    worker = ManifestSubjectBackfillWorker()

    # By default new manifests are already backfilled (i.e subject. if any, are parsed at creation)
    assert not worker._backfill_manifest_subject()

    # Set manifests to be backfilled some manifests
    database.Manifest.update(subject_backfilled=False).execute()

    assert worker._backfill_manifest_subject()

    for manifest_row in database.Manifest.select():
        assert manifest_row.subject_backfilled is True

        if manifest_row.subject is not None:
            database.Manifest.select().where(
                database.Manifest.repository == manifest_row.repository,
                database.Manifest.digest == manifest_row.subject,
            ).get()

    assert not worker._backfill_manifest_subject()


def test_unresolved_subject_remains_pending_for_retry(initialized_db):
    worker = ManifestSubjectBackfillWorker()
    database.Manifest.update(subject_backfilled=True).execute()
    manifest = database.Manifest.select().first()
    database.Manifest.update(
        subject="sha512:" + "a" * 128,
        subject_backfilled=False,
    ).where(database.Manifest.id == manifest.id).execute()

    with patch(
        "workers.manifestsubjectbackfillworker.resolve_manifest_subject",
        side_effect=CreateManifestException("subject is not registered in this repository"),
    ):
        assert worker._backfill_manifest_subject()

    manifest = database.Manifest.get_by_id(manifest.id)
    assert manifest.subject_backfilled is False
    assert manifest.subject is None
