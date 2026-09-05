import pytest

from app import namespace_gc_queue
from data import database, model
from data.database import (
    Manifest,
    ManifestBlob,
    RepositoryBlobDigest,
    RepositoryManifestDigest,
)
from test.fixtures import *
from workers.namespacegcworker import NamespaceGCWorker


def test_story15_gc_namespace(initialized_db):
    namespace = model.user.get_namespace_user("buynlarge")
    repository = model.repository.get_repository("buynlarge", "orgrepo")
    manifest = Manifest.select().where(Manifest.repository == repository).first()
    assert manifest is not None
    RepositoryManifestDigest.create(
        repository=repository,
        manifest=manifest,
        digest=f"sha512:{manifest.id:0128x}",
    )
    for link in ManifestBlob.select().where(ManifestBlob.repository == repository):
        model.oci.blob.register_repository_blob_digest(
            repository, link.blob, f"sha512:{link.blob.id:0128x}"
        )

    repository_id = repository.id
    marker_id = model.user.mark_namespace_for_deletion(namespace, [], namespace_gc_queue)

    assert not database.User.get(id=namespace).enabled
    assert (
        RepositoryManifestDigest.select()
        .where(RepositoryManifestDigest.repository == repository_id)
        .exists()
    )
    assert (
        RepositoryBlobDigest.select()
        .where(RepositoryBlobDigest.repository == repository_id)
        .exists()
    )

    worker = NamespaceGCWorker(None)
    worker._perform_gc({"marker_id": marker_id})

    assert model.user.get_namespace_user("buynlarge") is None
    assert (
        not RepositoryManifestDigest.select()
        .where(RepositoryManifestDigest.repository == repository_id)
        .exists()
    )
    assert (
        not RepositoryBlobDigest.select()
        .where(RepositoryBlobDigest.repository == repository_id)
        .exists()
    )
