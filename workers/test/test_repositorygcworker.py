from app import repository_gc_queue
from data import model
from data.database import (
    Manifest,
    ManifestBlob,
    Repository,
    RepositoryBlobDigest,
    RepositoryManifestDigest,
)
from data.model.oci.test.test_oci_manifest import create_manifest_for_testing
from image.docker.schema1 import DOCKER_SCHEMA1_MANIFEST_CONTENT_TYPE
from test.fixtures import *
from workers.repositorygcworker import RepositoryGCWorker


def test_story19_gc_repository_removes_stale_schema1_alternative_registration(initialized_db):
    repository = model.repository.create_repository("devtable", "newrepo", None)
    manifest, _ = create_manifest_for_testing(
        repository, differentiation_field="story19-schema1-alias-gc"
    )
    manifest.media_type = Manifest.media_type.get_id(DOCKER_SCHEMA1_MANIFEST_CONTENT_TYPE)
    manifest.save()
    RepositoryManifestDigest.create(
        repository=repository,
        manifest=manifest,
        digest="sha512:" + "1" * 128,
    )

    repository_id = repository.id
    marker_id = model.repository.mark_repository_for_deletion(
        "devtable", "newrepo", repository_gc_queue
    )
    RepositoryGCWorker(None)._perform_gc({"marker_id": marker_id})

    assert Repository.get_or_none(Repository.id == repository_id) is None
    assert (
        not RepositoryManifestDigest.select()
        .where(RepositoryManifestDigest.repository == repository_id)
        .exists()
    )


def test_story15_gc_repository_removes_digest_registrations(initialized_db):
    repository = model.repository.create_repository("devtable", "newrepo", None)
    manifest, _ = create_manifest_for_testing(
        repository, differentiation_field="story15-repository-worker", include_shared_blob=True
    )
    model.oci.manifest.register_repository_manifest_digest(
        repository.id, manifest, f"sha512:{manifest.id:0128x}"
    )
    for link in ManifestBlob.select().where(ManifestBlob.repository == repository):
        model.oci.blob.register_repository_blob_digest(
            repository, link.blob, f"sha512:{link.blob.id:0128x}"
        )

    repository_id = repository.id
    marker_id = model.repository.mark_repository_for_deletion(
        "devtable", "newrepo", repository_gc_queue
    )

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

    worker = RepositoryGCWorker(None)
    worker._perform_gc({"marker_id": marker_id})

    assert Repository.get_or_none(Repository.id == repository_id) is None
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
