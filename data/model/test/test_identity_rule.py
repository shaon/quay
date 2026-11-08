"""
PROJQUAY-10117 Proposal 1: identity rule and selection order for blob lookups.
"""

import hashlib
import os
from datetime import datetime, timedelta

import pytest

from data.database import (
    ImageStorage,
    ImageStorageLocation,
    ImageStoragePlacement,
    ManifestBlob,
    UploadedBlob,
)
from data.model import repository
from data.model.blob import (
    get_or_create_shared_blob,
    get_shared_blob,
    temp_link_blob,
    temp_link_blob_storage,
)
from data.model.oci.blob import get_repository_blob_by_digest, lookup_repository_blob_row
from data.model.storage import (
    EMPTY_LAYER_BLOB_DIGEST,
    identity_clause,
    lookup_repo_storages_by_content_checksum,
    select_sha256_publication_row,
    storage_identity,
)
from data.registry_model import registry_model
from data.registry_model.datatypes import Blob
from test.fixtures import *


def _digests(content):
    return (
        "sha256:" + hashlib.sha256(content).hexdigest(),
        "sha384:" + hashlib.sha384(content).hexdigest(),
        "sha512:" + hashlib.sha512(content).hexdigest(),
    )


def _location(name="local_us"):
    return ImageStorageLocation.get(name=name)


def _row(content_checksum, requested=None, placed_at=None, cas_path=True, size=10):
    row = ImageStorage.create(
        content_checksum=content_checksum,
        requested_content_checksum=requested,
        image_size=size,
        cas_path=cas_path,
        uploading=False,
    )
    if placed_at is not None:
        ImageStoragePlacement.create(storage=row, location=placed_at)
    return row


def _upload_link(repo, row):
    UploadedBlob.create(
        repository=repo, blob=row, expires_at=datetime.utcnow() + timedelta(hours=1)
    )


@pytest.fixture()
def repos(initialized_db):
    repo_a = repository.create_repository("devtable", "identity_a", None)
    repo_b = repository.create_repository("devtable", "identity_b", None)
    return repo_a, repo_b


@pytest.fixture()
def content():
    return _digests(os.urandom(64))


def test_identity_clause(repos, content):
    sha256, sha384, sha512 = content
    s256 = _row(sha256, placed_at=_location())
    s512 = _row(sha256, sha512, placed_at=_location())
    s384 = _row(sha256, sha384, placed_at=_location())

    assert {r.id for r in ImageStorage.select().where(identity_clause(sha256))} == {s256.id}
    assert {r.id for r in ImageStorage.select().where(identity_clause(sha512))} == {s512.id}
    assert {r.id for r in ImageStorage.select().where(identity_clause(sha384))} == {s384.id}

    assert storage_identity(s256) == sha256
    assert storage_identity(s512) == sha512


def test_sha512_row_never_satisfies_sha256_request(repos, content):
    repo_a, _ = repos
    sha256, _, sha512 = content
    row = _row(sha256, sha512, placed_at=_location())
    _upload_link(repo_a, row)

    assert get_repository_blob_by_digest(repo_a, sha256) is None
    assert get_repository_blob_by_digest(repo_a, sha512).id == row.id

    found = lookup_repo_storages_by_content_checksum(repo_a, [sha256, sha512], with_uploads=True)
    assert [(storage_identity(r), r.id) for r in found] == [(sha512, row.id)]

    blob = registry_model.get_repo_blob_by_digest(registry_model.lookup_repository("devtable", "identity_a"), sha512)
    assert blob.digest == sha512
    assert registry_model.get_repo_blob_by_digest(
        registry_model.lookup_repository("devtable", "identity_a"), sha256
    ) is None


def test_sha256_row_does_not_satisfy_sha512_request(repos, content):
    repo_a, _ = repos
    sha256, _, sha512 = content
    row = _row(sha256, placed_at=_location())
    _upload_link(repo_a, row)

    assert get_repository_blob_by_digest(repo_a, sha512) is None
    assert get_repository_blob_by_digest(repo_a, sha256).id == row.id


def test_requested_digest_is_repository_scoped(repos, content):
    repo_a, repo_b = repos
    sha256, _, sha512 = content
    row = _row(sha256, sha512, placed_at=_location())
    _upload_link(repo_a, row)

    assert get_repository_blob_by_digest(repo_b, sha512) is None
    assert get_repository_blob_by_digest(repo_b, sha256) is None


def test_repository_selection_lowest_id_placeholders_last(repos, content):
    repo_a, _ = repos
    sha256, _, sha512 = content

    # Placeholder (no placement) with the lowest ID, then two placed rows.
    placeholder = _row(sha256)
    first_placed = _row(sha256, placed_at=_location())
    second_placed = _row(sha256, placed_at=_location())
    _upload_link(repo_a, second_placed)
    _upload_link(repo_a, placeholder)
    ManifestBlob.create(repository=repo_a, blob=first_placed, manifest=_manifest(repo_a))

    selected = lookup_repository_blob_row(repo_a, sha256)
    assert selected.id == first_placed.id
    assert get_repository_blob_by_digest(repo_a, sha256).id == first_placed.id

    found = lookup_repo_storages_by_content_checksum(repo_a, [sha256], with_uploads=True)
    assert [r.id for r in found] == [first_placed.id]

    # ManifestBlob-only lookup (layer listing) applies the same order.
    found = lookup_repo_storages_by_content_checksum(repo_a, [sha256])
    assert [r.id for r in found] == [first_placed.id]


def test_repository_selection_placeholder_only(repos, content):
    repo_a, _ = repos
    _, _, sha512 = content
    placeholder = _row(None, sha512)
    _upload_link(repo_a, placeholder)

    assert lookup_repository_blob_row(repo_a, sha512).id == placeholder.id
    found = lookup_repo_storages_by_content_checksum(repo_a, [sha512], with_uploads=True)
    assert [r.id for r in found] == [placeholder.id]


def test_sha256_publication_selection(repos, content):
    sha256, _, sha512 = content
    us = _location("local_us")
    eu = _location("local_eu")

    _row(sha256, sha512, placed_at=us)  # requested rows are never SHA-256 candidates
    _row(sha256, cas_path=False, placed_at=us)  # outside CAS: never reused
    lowest = _row(sha256, placed_at=eu)
    placed_here = _row(sha256, placed_at=us)

    assert select_sha256_publication_row(sha256, us).id == placed_here.id
    assert select_sha256_publication_row(sha256, us.id).id == placed_here.id
    assert select_sha256_publication_row(sha256).id == lowest.id

    other = _digests(b"nothing here")[0]
    with pytest.raises(ImageStorage.DoesNotExist):
        select_sha256_publication_row(other, us)


def test_shared_empty_layer_is_sha256_only(initialized_db):
    # A requested-digest row sharing the empty layer's checksum must never be returned.
    fake_requested = "sha512:" + "e" * 128
    ImageStorage.delete().where(ImageStorage.content_checksum == EMPTY_LAYER_BLOB_DIGEST).execute()
    _row(EMPTY_LAYER_BLOB_DIGEST, fake_requested, placed_at=_location())
    assert get_shared_blob(EMPTY_LAYER_BLOB_DIGEST) is None

    sha256_row = _row(EMPTY_LAYER_BLOB_DIGEST, placed_at=_location())
    assert get_shared_blob(EMPTY_LAYER_BLOB_DIGEST).id == sha256_row.id


def test_get_or_create_shared_blob_ignores_requested_rows(initialized_db):
    from app import storage

    ImageStorage.delete().where(ImageStorage.content_checksum == EMPTY_LAYER_BLOB_DIGEST).execute()
    requested = _row(EMPTY_LAYER_BLOB_DIGEST, "sha512:" + "e" * 128, placed_at=_location())

    from image.docker.schema2 import EMPTY_LAYER_BYTES

    created = get_or_create_shared_blob(EMPTY_LAYER_BLOB_DIGEST, EMPTY_LAYER_BYTES, storage)
    assert created.id != requested.id
    assert created.requested_content_checksum is None


def test_temp_link_blob_storage_links_exact_row(repos, content):
    repo_a, repo_b = repos
    sha256, _, sha512 = content
    sha256_row = _row(sha256, placed_at=_location())
    sha512_row = _row(sha256, sha512, placed_at=_location())
    _upload_link(repo_a, sha512_row)

    linked = temp_link_blob_storage(repo_b.id, sha512_row.id, 60)
    assert linked.id == sha512_row.id
    assert get_repository_blob_by_digest(repo_b, sha512).id == sha512_row.id
    # Mounting a SHA-512 blob does not make the SHA-256 identity available.
    assert get_repository_blob_by_digest(repo_b, sha256) is None

    assert temp_link_blob_storage(repo_b.id, 999999999, 60) is None

    # The legacy digest-based helper applies the identity rule too.
    assert temp_link_blob(repo_b.id, sha256, 60).id == sha256_row.id


def test_mount_links_exact_source_row(repos, content):
    repo_a, repo_b = repos
    sha256, _, sha512 = content
    _row(sha256, placed_at=_location())  # a SHA-256 row with the same bytes must not be linked
    sha512_row = _row(sha256, sha512, placed_at=_location())
    _upload_link(repo_a, sha512_row)

    source_ref = registry_model.lookup_repository("devtable", "identity_a")
    target_ref = registry_model.lookup_repository("devtable", "identity_b")
    blob = registry_model.get_repo_blob_by_digest(source_ref, sha512)
    assert blob.digest == sha512

    assert registry_model.mount_blob_into_repository(blob, target_ref, 60)
    linked_ids = {u.blob_id for u in UploadedBlob.select().where(UploadedBlob.repository == repo_b)}
    assert linked_ids == {sha512_row.id}
    assert registry_model.get_repo_blob_by_digest(target_ref, sha256) is None


def test_blob_datatype_reports_identity(initialized_db, content):
    sha256, _, sha512 = content
    row = _row(sha256, sha512, placed_at=_location())
    assert Blob.for_image_storage(row, storage_path="x").digest == sha512
    row256 = _row(sha256, placed_at=_location())
    assert Blob.for_image_storage(row256, storage_path="x").digest == sha256


def _manifest(repo):
    from data.database import MediaType, Manifest

    return Manifest.create(
        repository=repo,
        digest="sha256:" + hashlib.sha256(os.urandom(8)).hexdigest(),
        media_type=MediaType.get(name="application/vnd.oci.image.manifest.v1+json"),
        manifest_bytes="{}",
    )


def test_identity_clause_accepts_digest_objects(repos, content):
    from digest.digest_tools import Digest

    repo_a, _ = repos
    sha256, _, sha512 = content
    row = _row(sha256, placed_at=_location())
    _upload_link(repo_a, row)
    assert get_repository_blob_by_digest(repo_a, Digest.parse_digest(sha256)).id == row.id
