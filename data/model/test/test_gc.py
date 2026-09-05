import hashlib
import json
import logging
import random
import string
from contextlib import contextmanager
from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import call
from unittest.mock import patch as mock_patch

import pytest
from freezegun import freeze_time
from mock import patch
from playhouse.test_utils import assert_query_count

from app import docker_v2_signing_key, model_cache, storage
from data import database, model
from data.database import (
    BlobUpload,
    ExternalNotificationMethod,
    ImageStorage,
    ImageStorageLocation,
    ImageStoragePlacement,
    Label,
    Manifest,
    ManifestBlob,
    ManifestChild,
    ManifestLabel,
    ManifestPullStatistics,
    MediaType,
    QueueItem,
    Repository,
    RepositoryBlobDigest,
    RepositoryManifestDigest,
    RepositoryState,
    Tag,
    TagNotificationSuccess,
    TagPullStatistics,
    UploadedBlob,
)
from data.model import oci, pull_statistics
from data.model import storage as storage_model
from data.model.oci.test.test_oci_manifest import create_manifest_for_testing
from data.registry_model import registry_model
from data.registry_model.datatypes import RepositoryReference
from digest.digest_tools import sha256_digest
from endpoints.api.repositorynotification_models_pre_oci import pre_oci_model
from image.docker.schema1 import DockerSchema1ManifestBuilder
from image.oci.config import OCIConfig
from image.oci.index import OCIIndexBuilder
from image.oci.manifest import OCIManifestBuilder
from image.shared.schemas import parse_manifest_from_bytes
from test.fixtures import *
from test.helpers import check_transitive_modifications
from util.bytes import Bytes
from util.secscan.v4.api import APIRequestFailure

ADMIN_ACCESS_USER = "devtable"
PUBLIC_USER = "public"

REPO = "somerepo"


def _set_tag_expiration_policy(namespace, expiration_s):
    namespace_user = model.user.get_user(namespace)
    model.user.change_user_tag_expiration(namespace_user, expiration_s)


@pytest.fixture()
def default_tag_policy(initialized_db):
    _set_tag_expiration_policy(ADMIN_ACCESS_USER, 0)
    _set_tag_expiration_policy(PUBLIC_USER, 0)


def _delete_temp_links(repo):
    """Deletes any temp links to blobs."""
    UploadedBlob.delete().where(UploadedBlob.repository == repo).execute()


def _populate_blob(repo, content):
    assert isinstance(content, bytes)
    digest = sha256_digest(content)
    location = ImageStorageLocation.get(name="local_us")
    storage.put_content(["local_us"], storage.blob_path(digest), content)
    blob = model.blob.store_blob_record_and_temp_link_in_repo(
        repo, digest, location, len(content), 120
    )
    return blob, digest


def create_repository(namespace=ADMIN_ACCESS_USER, name=REPO, **kwargs):
    user = model.user.get_user(namespace)
    repo = model.repository.create_repository(namespace, name, user)

    # Populate the repository with the tags.
    for tag_name, image_ids in kwargs.items():
        move_tag(repo, tag_name, image_ids, expect_gc=False)

    return repo


def gc_now(repository):
    return model.gc.garbage_collect_repo(repository)


def delete_tag(repository, tag, perform_gc=True, expect_gc=True):
    repo_ref = RepositoryReference.for_repo_obj(repository)
    registry_model.delete_tag(model_cache, repo_ref, tag)
    if perform_gc:
        assert gc_now(repository) == expect_gc


def move_tag(repository, tag, image_ids, expect_gc=True):
    namespace = repository.namespace_user.username
    name = repository.name

    repo_ref = RepositoryReference.for_repo_obj(repository)
    builder = DockerSchema1ManifestBuilder(namespace, name, tag)
    builder = OCIManifestBuilder()

    def history_for_image(image):
        history = {
            "created": "2018-04-03T18:37:09.284840891Z",
            "created_by": (
                ("/bin/sh -c #(nop) ENTRYPOINT %s" % image.config["Entrypoint"])
                if image.config and image.config.get("Entrypoint")
                else "/bin/sh -c #(nop) %s" % image.id
            ),
        }

        if image.is_empty:
            history["empty_layer"] = True

        return history

    config = {
        "os": "linux",
        "architecture": "amd64",
        "rootfs": {"type": "layers", "diff_ids": []},
        "history": [history_for_image(image) for image in images],
    }

    config_json = json.dumps(config, ensure_ascii=options.ensure_ascii)
    oci_config = OCIConfig(Bytes.for_string_or_unicode(config_json))
    builder.set_config(oci_config)

    # NOTE: Building root to leaf.
    parent_id = None
    for image_id in image_ids:
        config = {
            "id": image_id,
            "config": {
                "Labels": {
                    "foo": "bar",
                    "meh": "grah",
                }
            },
        }

        if parent_id:
            config["parent"] = parent_id

        # Create a storage row for the layer blob.
        _, layer_blob_digest = _populate_blob(repository, image_id.encode("ascii"))

        builder.insert_layer(layer_blob_digest, json.dumps(config))

        parent_id = image_id

    # Store the manifest.
    manifest = builder.build(docker_v2_signing_key)
    registry_model.create_manifest_and_retarget_tag(
        repo_ref, manifest, tag, storage, raise_on_error=True
    )

    tag_ref = registry_model.get_repo_tag(repo_ref, tag)
    manifest_ref = registry_model.get_manifest_for_tag(tag_ref)

    if expect_gc:
        assert gc_now(repository) == expect_gc


def _get_dangling_storage_count():
    storage_ids = set([current.id for current in ImageStorage.select()])
    referenced_by_manifest = set([blob.blob_id for blob in ManifestBlob.select()])
    referenced_by_uploaded = set([upload.blob_id for upload in UploadedBlob.select()])
    return len(storage_ids - referenced_by_manifest - referenced_by_uploaded)


def _get_dangling_label_count():
    return len(_get_dangling_labels())


def _get_dangling_labels():
    label_ids = set([current.id for current in Label.select()])
    referenced_by_manifest = set([mlabel.label_id for mlabel in ManifestLabel.select()])
    return label_ids - referenced_by_manifest


def _get_dangling_manifest_count():
    manifest_ids = set([current.id for current in Manifest.select()])
    referenced_by_tag = set([tag.manifest_id for tag in Tag.select()])
    return len(manifest_ids - referenced_by_tag)


@contextmanager
def populate_storage_for_gc():
    """
    Populate FakeStorage with dummy data for each ImageStorage row.
    """
    preferred = storage.preferred_locations[0]
    for storage_row in ImageStorage.select():
        content = b"hello world"
        storage.put_content({preferred}, storage.blob_path(storage_row.content_checksum), content)
        assert storage.exists({preferred}, storage.blob_path(storage_row.content_checksum))

    yield


@contextmanager
def assert_gc_integrity(expect_storage_removed=True):
    """
    Specialized assertion for ensuring that GC cleans up all dangling storages and labels, invokes
    the callback for images removed and doesn't invoke the callback for images *not* removed.
    """

    # Add a callback for when images are removed.
    removed_image_storages = []
    remove_callback = model.config.register_image_cleanup_callback(removed_image_storages.extend)

    # Store existing storages. We won't verify these for existence because they
    # were likely created as test data.
    existing_digests = set()
    for storage_row in ImageStorage.select():
        if storage_row.cas_path:
            existing_digests.add(storage_row.content_checksum)

    # Store the number of dangling objects.
    existing_storage_count = _get_dangling_storage_count()
    existing_label_count = _get_dangling_label_count()
    existing_manifest_count = _get_dangling_manifest_count()

    # Yield to the GC test.
    with check_transitive_modifications():
        try:
            yield
        finally:
            remove_callback()

    # Ensure the number of dangling storages, manifests and labels has not changed.
    updated_storage_count = _get_dangling_storage_count()
    assert updated_storage_count == existing_storage_count

    updated_label_count = _get_dangling_label_count()
    assert updated_label_count == existing_label_count, _get_dangling_labels()

    updated_manifest_count = _get_dangling_manifest_count()
    assert updated_manifest_count == existing_manifest_count

    # Ensure all CAS storage is in the storage engine.
    preferred = storage.preferred_locations[0]
    for storage_row in ImageStorage.select():
        if storage_row.content_checksum in existing_digests:
            continue

        if storage_row.cas_path:
            storage.get_content({preferred}, storage.blob_path(storage_row.content_checksum))

    # Ensure all tags have valid manifests.
    for manifest in {t.manifest for t in Tag.select()}:
        # Ensure that the manifest's blobs all exist.
        found_blobs = {
            b.blob.content_checksum
            for b in ManifestBlob.select().where(ManifestBlob.manifest == manifest)
        }

        parsed = parse_manifest_from_bytes(
            Bytes.for_string_or_unicode(manifest.manifest_bytes), manifest.media_type.name
        )
        assert set(parsed.local_blob_digests) == found_blobs


def test_has_garbage(default_tag_policy, initialized_db):
    """
    Create a repo with a tag, delete the tag, and verify that garbage detection
    respects the time machine setting and that GC cleans it up.
    """
    # Set a very large time machine so no existing tags appear as garbage.
    (
        database.User.update(removed_tag_expiration_s=1000000000)
        .where(database.User.username == ADMIN_ACCESS_USER)
        .execute()
    )

    # Create a repository without any garbage.
    repo = model.repository.create_repository("devtable", "newrepo", None)
    manifest, built = create_manifest_for_testing(
        repo, differentiation_field="1", include_shared_blob=True
    )
    model.oci.tag.retarget_tag("latest", manifest)

    # Ensure that no repositories are returned by the has garbage check.
    assert model.oci.tag.find_repository_with_garbage(1000000000) is None

    # Delete a tag.
    delete_tag(repo, "latest", perform_gc=False)

    # There should still not be any repositories with garbage, due to time machine.
    assert model.oci.tag.find_repository_with_garbage(1000000000) is None

    # Change the time machine expiration on the namespace.
    (
        database.User.update(removed_tag_expiration_s=0)
        .where(database.User.username == ADMIN_ACCESS_USER)
        .execute()
    )

    # Now we should find a repository for GC.
    repository = model.oci.tag.find_repository_with_garbage(0)
    assert repository is not None

    # GC all repositories with garbage to reach a clean state.
    while repository is not None:
        gc_now(repository)
        repository = model.oci.tag.find_repository_with_garbage(0)

    # There should now be no repositories with garbage.
    assert model.oci.tag.find_repository_with_garbage(0) is None


def test_find_garbage_policy_functions(default_tag_policy, initialized_db):
    with assert_query_count(1):
        one_policy = model.repository.get_random_gc_policy()
        all_policies = model.repository._get_gc_expiration_policies()
        assert one_policy in all_policies


def test_one_tag(default_tag_policy, initialized_db):
    """
    Create a repository with a single tag, then remove that tag and verify that the repository is
    now empty.
    """
    repo1 = model.repository.create_repository("devtable", "newrepo", None)
    manifest1, built1 = create_manifest_for_testing(
        repo1, differentiation_field="1", include_shared_blob=True
    )
    model.oci.tag.retarget_tag("tag1", manifest1)

    with assert_gc_integrity(expect_storage_removed=True):
        delete_tag(repo1, "tag1", expect_gc=True)


def test_two_tags_unshared_manifests(default_tag_policy, initialized_db):
    """
    Repository has two tags with no shared manifest between them.
    """
    repo1 = model.repository.create_repository("devtable", "newrepo", None)
    manifest1, built1 = create_manifest_for_testing(
        repo1, differentiation_field="1", include_shared_blob=True
    )
    manifest2, built2 = create_manifest_for_testing(
        repo1, differentiation_field="1", include_shared_blob=False
    )

    model.oci.tag.retarget_tag("tag1", manifest1)
    model.oci.tag.retarget_tag("tag2", manifest2)

    with assert_gc_integrity(expect_storage_removed=True):
        delete_tag(repo1, "tag1", expect_gc=True)

    # Ensure the blobs for manifest2 still all exist.
    preferred = storage.preferred_locations[0]
    for blob_digest in built2.local_blob_digests:
        storage_row = ImageStorage.get(content_checksum=blob_digest)

        assert storage_row.cas_path
        storage.get_content({preferred}, storage.blob_path(storage_row.content_checksum))


def test_two_tags_shared_manifest(default_tag_policy, initialized_db):
    """
    Repository has two tags with shared manifest.

    Deleting the tag should not remove the shared manifest.
    """
    repo1 = model.repository.create_repository("devtable", "newrepo", None)
    manifest1, built1 = create_manifest_for_testing(
        repo1, differentiation_field="1", include_shared_blob=True
    )

    model.oci.tag.retarget_tag("tag1", manifest1)
    model.oci.tag.retarget_tag("tag2", manifest1)

    with assert_gc_integrity(expect_storage_removed=False):
        delete_tag(repo1, "latest", expect_gc=False)

    preferred = storage.preferred_locations[0]
    for blob_digest in built1.local_blob_digests:
        storage_row = ImageStorage.get(content_checksum=blob_digest)

        assert storage_row.cas_path
        storage.get_content({preferred}, storage.blob_path(storage_row.content_checksum))


def test_multiple_shared_manifest(default_tag_policy, initialized_db):
    """
    Repository has multiple tags with shared manifests.

    Selectively deleting the tags, and verifying at each step.
    """
    repo = model.repository.create_repository("devtable", "newrepo", None)
    manifest1, built1 = create_manifest_for_testing(
        repo, differentiation_field="1", include_shared_blob=True
    )
    manifest2, built2 = create_manifest_for_testing(
        repo, differentiation_field="2", include_shared_blob=True
    )
    manifest3, built3 = create_manifest_for_testing(
        repo, differentiation_field="3", include_shared_blob=False
    )

    assert set(built1.local_blob_digests).intersection(built2.local_blob_digests)
    assert built1.config.digest == built2.config.digest

    # Create tags pointing to the manifests.
    model.oci.tag.retarget_tag("tag1", manifest1)
    model.oci.tag.retarget_tag("tag2", manifest2)
    model.oci.tag.retarget_tag("tag3", manifest3)

    with assert_gc_integrity(expect_storage_removed=True):
        delete_tag(repo, "tag3", expect_gc=True)

    with assert_gc_integrity(expect_storage_removed=False):
        delete_tag(repo, "tag1", expect_gc=True)

    with assert_gc_integrity(expect_storage_removed=True):
        delete_tag(repo, "tag2", expect_gc=True)


def test_empty_gc(default_tag_policy, initialized_db):
    with assert_gc_integrity(expect_storage_removed=False):
        repo = model.repository.create_repository("devtable", "newrepo", None)
        manifest1, built1 = create_manifest_for_testing(
            repo, differentiation_field="1", include_shared_blob=True
        )
        assert not gc_now(repo)


def test_time_machine_no_gc(default_tag_policy, initialized_db):
    """
    Repository has two tags with shared manfiest.

    Deleting the tags should not remove any images
    """
    with assert_gc_integrity(expect_storage_removed=False):
        repo = model.repository.create_repository("devtable", "newrepo", None)
        manifest1, built1 = create_manifest_for_testing(
            repo, differentiation_field="1", include_shared_blob=True
        )
        model.oci.tag.retarget_tag("tag1", manifest1)
        model.oci.tag.retarget_tag("tag2", manifest1)

        _set_tag_expiration_policy(repo.namespace_user.username, 60 * 60 * 24)

        with assert_gc_integrity():
            delete_tag(repo, "tag1", expect_gc=False)

        with assert_gc_integrity():
            delete_tag(repo, "tag2", expect_gc=False)

        # Ensure the blobs for manifest1 still all exist.
        preferred = storage.preferred_locations[0]
        for blob_digest in built1.local_blob_digests:
            storage_row = ImageStorage.get(content_checksum=blob_digest)

            assert storage_row.cas_path
            storage.get_content({preferred}, storage.blob_path(storage_row.content_checksum))


def test_time_machine_gc(default_tag_policy, initialized_db):
    """
    Repository has two tags with shared images.

    Deleting the second tag should cause the images for the first deleted tag to gc.
    """
    now = datetime.utcnow()

    with assert_gc_integrity():
        with freeze_time(now):
            repo = model.repository.create_repository("devtable", "newrepo", None)
            manifest1, built1 = create_manifest_for_testing(
                repo, differentiation_field="1", include_shared_blob=True
            )
            model.oci.tag.retarget_tag("tag1", manifest1)

            _set_tag_expiration_policy(repo.namespace_user.username, 1)

            with assert_gc_integrity(expect_storage_removed=False):
                delete_tag(repo, "tag1", expect_gc=False)

            # Ensure the blobs for manifest1 still all exist.
            preferred = storage.preferred_locations[0]
            for blob_digest in built1.local_blob_digests:
                storage_row = ImageStorage.get(content_checksum=blob_digest)

                assert storage_row.cas_path
                storage.get_content({preferred}, storage.blob_path(storage_row.content_checksum))

        with freeze_time(now + timedelta(seconds=2)):
            with assert_gc_integrity(expect_storage_removed=True):
                delete_tag(repo, "tag1", expect_gc=True)


def test_gc_removes_manifest_digest_registrations(default_tag_policy, initialized_db):
    repo = model.repository.create_repository("devtable", "newrepo", None)
    manifest, _ = create_manifest_for_testing(
        repo, differentiation_field="registered", include_shared_blob=True
    )
    external_digest = "sha512:" + "a" * 128
    model.oci.manifest.register_repository_manifest_digest(repo.id, manifest, external_digest)
    Tag.delete().where(Tag.manifest == manifest).execute()

    context = model.gc._GarbageCollectorContext(repo)
    context.add_manifest_id(manifest.id)
    with (
        mock_patch("data.model.gc.features.SECURITY_SCANNER", True),
        mock_patch.dict(model.gc.config.app_config, {"SECURITY_SCANNER_V4_MANIFEST_CLEANUP": True}),
        mock_patch(
            "data.model.gc.secscan_model.garbage_collect_manifest_report", return_value=True
        ) as cleanup_report,
    ):
        model.gc._run_garbage_collection(context)
        assert cleanup_report.call_count == 0
        assert model.gc.garbage_collect_secscan_reports() == 2

    assert cleanup_report.call_args_list == [call(manifest.digest), call(external_digest)]
    assert (
        not RepositoryManifestDigest.select()
        .where(
            RepositoryManifestDigest.repository == repo,
            RepositoryManifestDigest.digest == external_digest,
        )
        .exists()
    )
    assert Manifest.get_or_none(Manifest.id == manifest.id) is None


def test_story17_manifest_gc_removes_registrations_and_canonical_scanner_report(
    default_tag_policy, initialized_db
):
    # create_manifest_for_testing uses the fixed devtable/newrepo blob fixture.
    repo = model.repository.create_repository("devtable", "newrepo", None)
    manifest, _ = create_manifest_for_testing(
        repo, differentiation_field="story17-manifest", include_shared_blob=True
    )
    manifest_bytes = (
        manifest.manifest_bytes
        if isinstance(manifest.manifest_bytes, bytes)
        else manifest.manifest_bytes.encode("utf-8")
    )
    registered_digests = [
        "sha512:" + hashlib.sha512(manifest_bytes).hexdigest(),
        "sha384:" + hashlib.sha384(manifest_bytes).hexdigest(),
        manifest.digest,
    ]
    for digest in registered_digests:
        model.oci.manifest.register_repository_manifest_digest(repo.id, manifest, digest)

    model.oci.tag.retarget_tag("story17-final", manifest)
    assert ManifestBlob.select().where(ManifestBlob.manifest == manifest).exists()
    model.oci.tag.delete_tags_for_manifest(manifest)

    with (
        mock_patch("data.model.gc.features.SECURITY_SCANNER", True),
        mock_patch("data.model.gc.features.QUOTA_MANAGEMENT", True),
        mock_patch.dict(
            model.gc.config.app_config,
            {
                "ALLOWED_HASH_ALGORITHMS": ["sha256"],
                "SECURITY_SCANNER_V4_MANIFEST_CLEANUP": True,
            },
        ),
        mock_patch("data.model.gc.update_quota") as update_quota,
        mock_patch(
            "data.model.gc.secscan_model.garbage_collect_manifest_report", return_value=True
        ) as cleanup_report,
    ):
        assert gc_now(repo)
        assert not gc_now(repo)
        assert cleanup_report.call_count == 0
        assert model.gc.garbage_collect_secscan_reports() == 2

    assert cleanup_report.call_args_list == [call(manifest.digest), call(registered_digests[0])]
    update_quota.assert_called_once()
    assert (
        not RepositoryManifestDigest.select()
        .where(
            RepositoryManifestDigest.repository == repo,
            RepositoryManifestDigest.manifest == manifest.id,
        )
        .exists()
    )
    assert Manifest.get_or_none(Manifest.id == manifest.id) is None
    assert not ManifestBlob.select().where(ManifestBlob.manifest == manifest.id).exists()
    assert (
        not ManifestChild.select()
        .where(
            (ManifestChild.manifest == manifest.id) | (ManifestChild.child_manifest == manifest.id)
        )
        .exists()
    )


def test_story17_manifest_gc_retries_after_database_failure(default_tag_policy, initialized_db):
    repo = model.repository.create_repository("devtable", "newrepo", None)
    manifest, _ = create_manifest_for_testing(
        repo, differentiation_field="story17-manifest-retry", include_shared_blob=True
    )
    registered_digest = "sha512:" + "b" * 128
    model.oci.manifest.register_repository_manifest_digest(repo.id, manifest, registered_digest)
    model.oci.tag.delete_tags_for_manifest(manifest)
    initial_quota_size = model.quota.get_repository_size(repo.id).size_bytes
    assert initial_quota_size > 0

    with (
        mock_patch("data.model.gc.features.SECURITY_SCANNER", True),
        mock_patch.dict(
            model.gc.config.app_config,
            {"SECURITY_SCANNER_V4_MANIFEST_CLEANUP": True},
        ),
        mock_patch("data.model.gc.db_transaction", database.db.atomic),
        mock_patch.object(
            Manifest,
            "delete_instance",
            autospec=True,
            side_effect=RuntimeError("injected database failure"),
        ),
        pytest.raises(RuntimeError, match="injected database failure"),
    ):
        gc_now(repo)

    assert Manifest.get_by_id(manifest.id)
    assert ManifestBlob.select().where(ManifestBlob.manifest == manifest.id).exists()
    assert (
        RepositoryManifestDigest.select()
        .where(
            RepositoryManifestDigest.repository == repo,
            RepositoryManifestDigest.manifest == manifest.id,
        )
        .exists()
    )
    assert model.quota.get_repository_size(repo.id).size_bytes == initial_quota_size
    assert not QueueItem.select().where(QueueItem.body == manifest.digest).exists()

    with (
        mock_patch("data.model.gc.features.SECURITY_SCANNER", True),
        mock_patch.dict(
            model.gc.config.app_config,
            {"SECURITY_SCANNER_V4_MANIFEST_CLEANUP": True},
        ),
    ):
        assert gc_now(repo)
    assert QueueItem.select().where(QueueItem.body == manifest.digest).exists()
    assert Manifest.get_or_none(Manifest.id == manifest.id) is None
    assert (
        not RepositoryManifestDigest.select()
        .where(
            RepositoryManifestDigest.repository == repo,
            RepositoryManifestDigest.manifest == manifest.id,
        )
        .exists()
    )
    assert model.quota.get_repository_size(repo.id).size_bytes == 0


def test_story17_blob_gc_retries_after_storage_placement_delete_failure(
    default_tag_policy, initialized_db
):
    repo = model.repository.create_repository("devtable", "story17-storage-retry", None)
    blob, digests = _create_story17_registered_blob(
        repo,
        b"story17-storage-retry",
        datetime.utcnow() - timedelta(seconds=1),
        ("sha512",),
    )
    digest = digests[0]
    blob_path = storage.blob_path(blob.content_checksum)

    with (
        mock_patch("data.model.gc.db_transaction", database.db.atomic),
        mock_patch("data.model.storage.db_transaction", database.db.atomic),
        mock_patch.object(
            ImageStoragePlacement,
            "delete",
            side_effect=RuntimeError("injected placement delete failure"),
        ),
        pytest.raises(RuntimeError, match="injected placement delete failure"),
    ):
        gc_now(repo)

    assert ImageStorage.get_by_id(blob.id)
    assert ImageStoragePlacement.select().where(ImageStoragePlacement.storage == blob).exists()
    assert RepositoryBlobDigest.get(repository=repo, digest=digest).image_storage_id == blob.id
    assert storage.exists({"local_us"}, blob_path)

    assert gc_now(repo)
    assert not gc_now(repo)
    assert ImageStorage.get_or_none(ImageStorage.id == blob.id) is None
    assert (
        not ImageStoragePlacement.select().where(ImageStoragePlacement.storage == blob.id).exists()
    )
    assert not RepositoryBlobDigest.select().where(RepositoryBlobDigest.digest == digest).exists()
    assert not storage.exists({"local_us"}, blob_path)


def test_manifest_with_tags(default_tag_policy, initialized_db):
    """
    A repository with two tags pointing to a manifest.

    Deleting and GCing one of the tag should not result in the storage and its CAS data being removed.
    """
    repo = model.repository.create_repository("devtable", "newrepo", None)
    manifest1, built1 = create_manifest_for_testing(
        repo, differentiation_field="1", include_shared_blob=True
    )

    # Create tags pointing to the manifests.
    model.oci.tag.retarget_tag("tag1", manifest1)
    model.oci.tag.retarget_tag("tag2", manifest1)

    with assert_gc_integrity(expect_storage_removed=False):
        # Delete tag2.
        model.oci.tag.delete_tag(repo, "tag2")
        assert gc_now(repo)

    # Ensure the blobs for manifest1 still all exist.
    preferred = storage.preferred_locations[0]
    for blob_digest in built1.local_blob_digests:
        storage_row = ImageStorage.get(content_checksum=blob_digest)

        assert storage_row.cas_path
        storage.get_content({preferred}, storage.blob_path(storage_row.content_checksum))


def test_manifest_v2_shared_config_and_blobs(app, default_tag_policy):
    """
    Test that GCing a tag that refers to a V2 manifest with the same config and some shared blobs as
    another manifest ensures that the config blob and shared blob are NOT GCed.
    """
    repo = model.repository.create_repository("devtable", "newrepo", None)
    manifest1, built1 = create_manifest_for_testing(
        repo, differentiation_field="1", include_shared_blob=True
    )
    manifest2, built2 = create_manifest_for_testing(
        repo, differentiation_field="2", include_shared_blob=True
    )

    assert set(built1.local_blob_digests).intersection(built2.local_blob_digests)
    assert built1.config.digest == built2.config.digest

    # Create tags pointing to the manifests.
    model.oci.tag.retarget_tag("tag1", manifest1)
    model.oci.tag.retarget_tag("tag2", manifest2)

    with assert_gc_integrity(expect_storage_removed=True):
        # Delete tag2.
        model.oci.tag.delete_tag(repo, "tag2")
        assert gc_now(repo)

    # Ensure the blobs for manifest1 still all exist.
    preferred = storage.preferred_locations[0]
    for blob_digest in built1.local_blob_digests:
        storage_row = ImageStorage.get(content_checksum=blob_digest)

        assert storage_row.cas_path
        storage.get_content({preferred}, storage.blob_path(storage_row.content_checksum))


def test_garbage_collect_storage(default_tag_policy, initialized_db):
    with populate_storage_for_gc():
        preferred = storage.preferred_locations[0]

        # Get a random sample of storages
        uploadedblobs = list(UploadedBlob.select())
        random_uploadedblobs = random.sample(
            uploadedblobs, random.randrange(1, len(uploadedblobs) + 1)
        )
        model.storage.garbage_collect_storage([b.blob.id for b in random_uploadedblobs])
        # Ensure that the blobs' storage weren't removed, since we didn't GC anything
        for uploadedblob in random_uploadedblobs:
            assert storage.exists(
                {preferred}, storage.blob_path(uploadedblob.blob.content_checksum)
            )


def _create_story17_registered_blob(repository, content, expires_at, algorithms):
    location = ImageStorageLocation.get(name="local_us")
    canonical_digest = sha256_digest(content)
    blob = ImageStorage.create(
        content_checksum=canonical_digest,
        image_size=len(content),
        uncompressed_size=len(content),
        uploading=False,
        cas_path=True,
    )
    ImageStoragePlacement.create(storage=blob, location=location)
    storage.put_content([location.name], storage.blob_path(canonical_digest), content)
    UploadedBlob.create(repository=repository, blob=blob, expires_at=expires_at)

    digests = []
    for algorithm in algorithms:
        digest = f"{algorithm}:{hashlib.new(algorithm, content).hexdigest()}"
        model.oci.blob.register_repository_blob_digest(repository, blob, digest)
        digests.append(digest)
    return blob, digests


def test_story17_blob_gc_removes_disabled_aliases_and_canonical_storage(
    default_tag_policy, initialized_db
):
    repo = model.repository.create_repository("devtable", "story17-blob", None)
    blob, registered_digests = _create_story17_registered_blob(
        repo,
        b"story17 registered blob",
        datetime.utcnow() - timedelta(seconds=1),
        ("sha256", "sha384", "sha512"),
    )
    blob_id = blob.id
    preferred = storage.preferred_locations[0]

    assert [
        registration.digest
        for registration in RepositoryBlobDigest.select()
        .where(RepositoryBlobDigest.repository == repo)
        .order_by(RepositoryBlobDigest.id)
    ] == registered_digests

    with mock_patch.dict(model.gc.config.app_config, {"ALLOWED_HASH_ALGORITHMS": ["sha256"]}):
        assert gc_now(repo)
        assert not gc_now(repo)

    assert (
        not RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == repo,
            RepositoryBlobDigest.image_storage == blob_id,
        )
        .exists()
    )
    assert ImageStorage.get_or_none(ImageStorage.id == blob_id) is None
    assert (
        not ImageStoragePlacement.select().where(ImageStoragePlacement.storage == blob_id).exists()
    )
    assert not storage.exists({preferred}, storage.blob_path(blob.content_checksum))


def test_story17_shared_blob_registration_is_repository_scoped(default_tag_policy, initialized_db):
    first = model.repository.create_repository("devtable", "story17-shared-first", None)
    second = model.repository.create_repository("devtable", "story17-shared-second", None)
    blob, first_digests = _create_story17_registered_blob(
        first,
        b"story17 shared alternative-only blob",
        datetime.utcnow() - timedelta(seconds=1),
        ("sha384", "sha512"),
    )
    second_digests = []
    content = b"story17 shared alternative-only blob"
    for algorithm in ("sha384", "sha512"):
        digest = f"{algorithm}:{hashlib.new(algorithm, content).hexdigest()}"
        model.oci.blob.register_repository_blob_digest(second, blob, digest)
        second_digests.append(digest)
    UploadedBlob.create(
        repository=second,
        blob=blob,
        expires_at=datetime.utcnow() - timedelta(seconds=1),
    )
    blob_id = blob.id

    assert not any(digest.startswith("sha256:") for digest in first_digests + second_digests)
    assert gc_now(first)
    assert (
        not RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == first,
            RepositoryBlobDigest.image_storage == blob_id,
        )
        .exists()
    )
    assert [
        registration.digest
        for registration in RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == second,
            RepositoryBlobDigest.image_storage == blob_id,
        )
        .order_by(RepositoryBlobDigest.id)
    ] == second_digests
    assert ImageStorage.get_by_id(blob_id).content_checksum == blob.content_checksum

    assert gc_now(second)
    assert (
        not RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == second,
            RepositoryBlobDigest.image_storage == blob_id,
        )
        .exists()
    )
    assert ImageStorage.get_or_none(ImageStorage.id == blob_id) is None


def test_story17_mount_only_registration_waits_for_upload_expiration(
    default_tag_policy, initialized_db
):
    repo = model.repository.create_repository("devtable", "story17-mount", None)
    blob, digests = _create_story17_registered_blob(
        repo,
        b"story17 recent mount",
        datetime.utcnow() + timedelta(days=1),
        ("sha512",),
    )

    assert not gc_now(repo)
    assert (
        RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == repo,
            RepositoryBlobDigest.digest == digests[0],
        )
        .exists()
    )
    assert ImageStorage.get_by_id(blob.id)

    UploadedBlob.update(expires_at=datetime.utcnow() - timedelta(seconds=1)).where(
        UploadedBlob.repository == repo,
        UploadedBlob.blob == blob,
    ).execute()
    assert gc_now(repo)
    assert (
        not RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == repo,
            RepositoryBlobDigest.image_storage == blob.id,
        )
        .exists()
    )
    assert ImageStorage.get_or_none(ImageStorage.id == blob.id) is None


def test_story17_live_manifest_link_preserves_registration(default_tag_policy, initialized_db):
    repo = model.repository.create_repository("devtable", "story17-live-link", None)
    blob, digests = _create_story17_registered_blob(
        repo,
        b"story17 live manifest link",
        datetime.utcnow() - timedelta(seconds=1),
        ("sha512",),
    )
    media_type = MediaType.get(name="application/vnd.oci.image.manifest.v1+json")
    manifest = Manifest.create(
        repository=repo,
        digest="sha256:" + "c" * 64,
        media_type=media_type,
        manifest_bytes=b"{}",
    )
    ManifestBlob.create(repository=repo, manifest=manifest, blob=blob)

    assert gc_now(repo)
    assert (
        RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == repo,
            RepositoryBlobDigest.digest == digests[0],
        )
        .exists()
    )
    assert ImageStorage.get_by_id(blob.id)

    ManifestBlob.delete().where(ManifestBlob.manifest == manifest).execute()
    manifest.delete_instance()
    assert gc_now(repo)
    assert (
        not RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == repo,
            RepositoryBlobDigest.image_storage == blob.id,
        )
        .exists()
    )
    assert ImageStorage.get_or_none(ImageStorage.id == blob.id) is None


def test_story17_legacy_blob_with_and_without_registration(default_tag_policy, initialized_db):
    repo = model.repository.create_repository("devtable", "story17-legacy", None)
    unregistered, _ = _create_story17_registered_blob(
        repo,
        b"story17 legacy without registration",
        datetime.utcnow() - timedelta(seconds=1),
        (),
    )
    registered, registered_digests = _create_story17_registered_blob(
        repo,
        b"story17 legacy with canonical registration",
        datetime.utcnow() - timedelta(seconds=1),
        ("sha256",),
    )

    assert gc_now(repo)
    assert ImageStorage.get_or_none(ImageStorage.id == unregistered.id) is None
    assert ImageStorage.get_or_none(ImageStorage.id == registered.id) is None
    assert (
        not RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == repo,
            RepositoryBlobDigest.digest == registered_digests[0],
        )
        .exists()
    )


def test_story17_blob_gc_retries_after_transaction_failure(default_tag_policy, initialized_db):
    repo = model.repository.create_repository("devtable", "story17-retry", None)
    blob, _ = _create_story17_registered_blob(
        repo,
        b"story17 retry blob",
        datetime.utcnow() - timedelta(seconds=1),
        ("sha512",),
    )

    with (
        mock_patch("data.model.storage.db_transaction", database.db.atomic),
        mock_patch("data.model.storage._is_storage_orphaned", side_effect=RuntimeError("injected")),
        pytest.raises(RuntimeError, match="injected"),
    ):
        gc_now(repo)

    assert (
        RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == repo,
            RepositoryBlobDigest.image_storage == blob.id,
        )
        .exists()
    )
    assert ImageStorage.get_by_id(blob.id)

    assert gc_now(repo)
    assert (
        not RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == repo,
            RepositoryBlobDigest.image_storage == blob.id,
        )
        .exists()
    )
    assert ImageStorage.get_or_none(ImageStorage.id == blob.id) is None


def test_story17_gc_finder_discovers_mount_only_expiration(default_tag_policy, initialized_db):
    Tag.update(lifetime_end_ms=None).execute()
    UploadedBlob.delete().execute()
    repo = model.repository.create_repository("devtable", "story17-finder", None)
    _create_story17_registered_blob(
        repo,
        b"story17 finder blob",
        datetime.utcnow() - timedelta(seconds=1),
        ("sha512",),
    )

    assert model.oci.tag.find_repository_with_garbage(0).id == repo.id


def _story17_create_manifest(repository, suffix):
    media_type = MediaType.get(name="application/vnd.oci.image.manifest.v1+json")
    return Manifest.create(
        repository=repository,
        digest=str(sha256_digest(suffix.encode("utf-8"))),
        media_type=media_type,
        manifest_bytes=b"{}",
    )


def _story17_candidate_ids(policy, source_start_ids=(0, 0, 0, 0)):
    return {
        row.repository_id
        for row in model.oci.tag._find_repository_garbage_candidates(
            policy,
            source_start_ids=source_start_ids,
        )
    }


def _story17_reset_garbage_sources():
    Tag.update(lifetime_end_ms=None).execute()
    UploadedBlob.delete().execute()
    RepositoryBlobDigest.delete().execute()
    RepositoryManifestDigest.delete().execute()


def test_story17_gc_finder_is_bounded_and_source_driven(default_tag_policy, initialized_db):
    query = model.oci.tag._find_repository_garbage_candidates(
        0,
        source_start_ids=(11, 12, 13, 14),
    )
    sql, parameters = query.sql()
    normalized = " ".join(sql.upper().split())

    assert normalized.count("UNION ALL") == 3
    assert normalized.count("LIMIT") >= 5
    assert normalized.count("ORDER BY") >= 4
    assert normalized.count('"ID" <') >= 4
    assert 'FROM (SELECT "T' in normalized
    assert 'FROM "TAG"' in normalized
    assert 'FROM "UPLOADEDBLOB"' in normalized
    assert 'FROM "REPOSITORYBLOBDIGEST"' in normalized
    assert 'FROM "REPOSITORYMANIFESTDIGEST"' in normalized
    assert len(parameters) <= 40


def test_story17_gc_finder_covers_all_sources_and_bounds_result_count(
    default_tag_policy, initialized_db
):
    _story17_reset_garbage_sources()
    expiration = datetime.utcnow() - timedelta(seconds=1)

    tag_repo = model.repository.create_repository("devtable", "story17-source-tag", None)
    tag_manifest = _story17_create_manifest(tag_repo, "story17-source-tag")
    model.oci.tag.retarget_tag("expired", tag_manifest)
    Tag.update(lifetime_end_ms=0).where(Tag.repository == tag_repo).execute()
    RepositoryManifestDigest.delete().where(
        RepositoryManifestDigest.repository == tag_repo
    ).execute()

    upload_repo = model.repository.create_repository("devtable", "story17-source-upload", None)
    _create_story17_registered_blob(
        upload_repo,
        b"story17 source upload",
        expiration,
        (),
    )

    blob_repo = model.repository.create_repository("devtable", "story17-source-blob", None)
    blob, _ = _create_story17_registered_blob(
        blob_repo,
        b"story17 source blob registration",
        expiration,
        ("sha512",),
    )
    UploadedBlob.delete().where(UploadedBlob.blob == blob).execute()

    manifest_repo = model.repository.create_repository("devtable", "story17-source-manifest", None)
    manifest = _story17_create_manifest(manifest_repo, "story17-source-manifest")
    model.oci.manifest.register_repository_manifest_digest(
        manifest_repo.id,
        manifest,
        "sha512:" + "d" * 128,
    )

    expected = {tag_repo.id, upload_repo.id, blob_repo.id, manifest_repo.id}
    assert expected <= _story17_candidate_ids(0)

    with mock_patch.object(model.oci.tag, "GC_CANDIDATE_COUNT", 2):
        assert len(_story17_candidate_ids(0)) <= 2


def test_story17_gc_finder_returns_no_candidate_for_live_source_rows(
    default_tag_policy, initialized_db
):
    _story17_reset_garbage_sources()
    repo = model.repository.create_repository("devtable", "story17-no-garbage", None)
    blob, _ = _create_story17_registered_blob(
        repo,
        b"story17 live source rows",
        datetime.utcnow() + timedelta(days=1),
        ("sha512",),
    )
    manifest = _story17_create_manifest(repo, "story17-live-source-manifest")
    ManifestBlob.create(repository=repo, manifest=manifest, blob=blob)
    model.oci.manifest.register_repository_manifest_digest(
        repo.id,
        manifest,
        "sha512:" + "f" * 128,
    )
    model.oci.tag.retarget_tag("live", manifest)

    assert repo.id not in _story17_candidate_ids(0)


def test_story17_gc_finder_applies_policy_and_repository_filters(
    default_tag_policy, initialized_db
):
    _story17_reset_garbage_sources()
    database.User.update(removed_tag_expiration_s=60).where(
        database.User.username == "public"
    ).execute()

    wrong_policy_repo = model.repository.create_repository(
        "public", "story17-policy-mismatch", None
    )
    wrong_policy_manifest = _story17_create_manifest(wrong_policy_repo, "story17-policy-mismatch")
    model.oci.tag.retarget_tag("expired", wrong_policy_manifest)
    Tag.update(lifetime_end_ms=0).where(Tag.repository == wrong_policy_repo).execute()
    RepositoryManifestDigest.delete().where(
        RepositoryManifestDigest.repository == wrong_policy_repo
    ).execute()
    policy_sixty = _story17_candidate_ids(60)

    immutable_repo = model.repository.create_repository("devtable", "story17-immutable", None)
    immutable_manifest = _story17_create_manifest(immutable_repo, "story17-immutable")
    model.oci.tag.retarget_tag("expired", immutable_manifest)
    Tag.update(lifetime_end_ms=0, immutable=True).where(Tag.repository == immutable_repo).execute()
    RepositoryManifestDigest.delete().where(
        RepositoryManifestDigest.repository == immutable_repo
    ).execute()

    disabled_repo = model.repository.create_repository("public", "story17-disabled", None)
    _create_story17_registered_blob(
        disabled_repo,
        b"story17 disabled namespace",
        datetime.utcnow() - timedelta(seconds=1),
        (),
    )
    database.User.update(enabled=False).where(database.User.username == "public").execute()

    marked_repo = model.repository.create_repository("devtable", "story17-marked", None)
    _create_story17_registered_blob(
        marked_repo,
        b"story17 marked repository",
        datetime.utcnow() - timedelta(seconds=1),
        (),
    )
    Repository.update(state=RepositoryState.MARKED_FOR_DELETION).where(
        Repository.id == marked_repo.id
    ).execute()

    with (
        mock_patch.object(model.oci.tag.features, "IMMUTABLE_TAGS", True),
        mock_patch.dict(
            model.oci.tag.config.app_config,
            {"FEATURE_IMMUTABLE_TAGS_CAN_EXPIRE": False},
        ),
    ):
        policy_zero = _story17_candidate_ids(0)

    assert wrong_policy_repo.id not in policy_zero
    assert wrong_policy_repo.id in policy_sixty
    assert immutable_repo.id not in policy_zero
    assert disabled_repo.id not in policy_zero
    assert marked_repo.id not in policy_zero


def _story17_secscan_queue_items():
    return QueueItem.select().where(
        QueueItem.queue_name.startswith(model.gc.SECURITY_SCANNER_GC_QUEUE_NAME + "/")
    )


def test_story17_blob_gc_rechecks_reachability_after_candidate_discovery(
    default_tag_policy, initialized_db
):
    repo = model.repository.create_repository("devtable", "story17-reachability-race", None)
    blob, digests = _create_story17_registered_blob(
        repo,
        b"story17 reachability race",
        datetime.utcnow() - timedelta(seconds=1),
        ("sha512",),
    )
    manifest = _story17_create_manifest(repo, "story17-reachability-owner")
    garbage_collect_storage = storage_model.garbage_collect_storage

    def add_reachability_before_storage_recheck(*args, **kwargs):
        ManifestBlob.get_or_create(repository=repo, manifest=manifest, blob=blob)
        return garbage_collect_storage(*args, **kwargs)

    with mock_patch(
        "data.model.gc.storage.garbage_collect_storage",
        side_effect=add_reachability_before_storage_recheck,
    ):
        assert gc_now(repo)

    assert RepositoryBlobDigest.get(repository=repo, digest=digests[0]).image_storage_id == blob.id
    assert ImageStorage.get_by_id(blob.id)
    assert ManifestBlob.get(repository=repo, manifest=manifest, blob=blob)


def test_story17_canonical_scanner_failure_is_durably_retried(default_tag_policy, initialized_db):
    repo = model.repository.create_repository("devtable", "story17-scanner-retry", None)
    manifest = _story17_create_manifest(repo, "story17-scanner-retry")
    manifest_bytes = (
        manifest.manifest_bytes
        if isinstance(manifest.manifest_bytes, bytes)
        else manifest.manifest_bytes.encode("utf-8")
    )
    registered_digests = [
        "sha512:" + hashlib.sha512(manifest_bytes).hexdigest(),
        "sha384:" + hashlib.sha384(manifest_bytes).hexdigest(),
        manifest.digest,
    ]
    for digest in registered_digests:
        model.oci.manifest.register_repository_manifest_digest(repo.id, manifest, digest)
    model.oci.tag.retarget_tag("story17-scanner-retry", manifest)
    model.oci.tag.delete_tags_for_manifest(manifest)

    with (
        mock_patch("data.model.gc.features.SECURITY_SCANNER", True),
        mock_patch.dict(
            model.gc.config.app_config,
            {"SECURITY_SCANNER_V4_MANIFEST_CLEANUP": True},
        ),
        mock_patch(
            "data.model.gc.secscan_model.garbage_collect_manifest_report",
            side_effect=APIRequestFailure(),
        ) as cleanup_report,
    ):
        assert gc_now(repo)
        assert cleanup_report.call_count == 0
        assert [item.body for item in _story17_secscan_queue_items().order_by(QueueItem.id)] == [
            manifest.digest,
            registered_digests[0],
        ]

        assert model.gc.garbage_collect_secscan_reports() == 2
        assert cleanup_report.call_args_list == [
            call(manifest.digest),
            call(registered_digests[0]),
        ]
        assert _story17_secscan_queue_items().count() == 2

        QueueItem.update(available_after=datetime.utcnow() - timedelta(seconds=1)).where(
            QueueItem.id << _story17_secscan_queue_items().select(QueueItem.id)
        ).execute()
        cleanup_report.reset_mock()
        cleanup_report.side_effect = None
        cleanup_report.return_value = True

        assert model.gc.garbage_collect_secscan_reports() == 2
        assert cleanup_report.call_args_list == [
            call(manifest.digest),
            call(registered_digests[0]),
        ]
        assert not _story17_secscan_queue_items().exists()
        assert model.gc.garbage_collect_secscan_reports() == 0


def test_story17_scanner_cleanup_preserves_cross_repository_report_and_legacy_fallback(
    default_tag_policy, initialized_db
):
    first = model.repository.create_repository("devtable", "story17-scanner-first", None)
    second = model.repository.create_repository("devtable", "story17-scanner-second", None)
    first_manifest = _story17_create_manifest(first, "story17-scanner-shared")
    second_manifest = _story17_create_manifest(second, "story17-scanner-shared")
    shared_digest = "sha512:" + "e" * 128
    model.oci.manifest.register_repository_manifest_digest(first.id, first_manifest, shared_digest)
    model.oci.manifest.register_repository_manifest_digest(
        second.id, second_manifest, shared_digest
    )
    model.oci.tag.delete_tags_for_manifest(first_manifest)

    legacy = model.repository.create_repository("devtable", "story17-scanner-legacy", None)
    legacy_manifest = _story17_create_manifest(legacy, "story17-scanner-legacy")
    RepositoryManifestDigest.delete().where(
        RepositoryManifestDigest.repository == legacy,
        RepositoryManifestDigest.manifest == legacy_manifest,
    ).execute()
    model.oci.tag.retarget_tag("story17-scanner-legacy", legacy_manifest)
    model.oci.tag.delete_tags_for_manifest(legacy_manifest)

    with (
        mock_patch("data.model.gc.features.SECURITY_SCANNER", True),
        mock_patch.dict(
            model.gc.config.app_config,
            {"SECURITY_SCANNER_V4_MANIFEST_CLEANUP": True},
        ),
        mock_patch(
            "data.model.gc.secscan_model.garbage_collect_manifest_report",
            return_value=True,
        ) as cleanup_report,
    ):
        assert gc_now(first)
        assert gc_now(legacy)
        assert model.gc.garbage_collect_secscan_reports() == 3

        cleanup_report.assert_called_once_with(legacy_manifest.digest)
        assert RepositoryManifestDigest.get(
            repository=second,
            manifest=second_manifest,
            digest=shared_digest,
        )

        model.oci.tag.delete_tags_for_manifest(second_manifest)
        assert gc_now(second)
        cleanup_report.reset_mock()
        assert model.gc.garbage_collect_secscan_reports() == 2

    assert cleanup_report.call_args_list == [
        call(second_manifest.digest),
        call(shared_digest),
    ]
    assert not _story17_secscan_queue_items().exists()


def test_story15_purge_repository_removes_digest_registrations(default_tag_policy, initialized_db):
    repository = model.repository.create_repository("devtable", "newrepo", None)
    survivor = model.repository.create_repository("devtable", "story15-survivor", None)
    manifest, _ = create_manifest_for_testing(
        repository, differentiation_field="story15-delete", include_shared_blob=True
    )
    repository_blobs = {
        link.blob for link in ManifestBlob.select().where(ManifestBlob.repository == repository)
    }
    shared_blob = next(iter(repository_blobs))

    model.oci.manifest.register_repository_manifest_digest(
        repository.id, manifest, f"sha512:{manifest.id:0128x}"
    )
    for blob_row in repository_blobs:
        model.oci.blob.register_repository_blob_digest(
            repository, blob_row, f"sha512:{blob_row.id:0128x}"
        )
    survivor_registration = model.oci.blob.register_repository_blob_digest(
        survivor, shared_blob, f"sha512:{shared_blob.id:0128x}"
    )
    UploadedBlob.create(
        repository=survivor,
        blob=shared_blob,
        expires_at=datetime.utcnow() + timedelta(days=1),
    )

    repository_id = repository.id
    shared_blob_id = shared_blob.id

    assert model.gc.purge_repository(repository, force=True)

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
    assert RepositoryBlobDigest.get_by_id(survivor_registration.id).repository_id == survivor.id
    assert ImageStorage.get_by_id(shared_blob_id).content_checksum.startswith("sha256:")


def test_story16_repository_purge_discards_upload_metadata_without_storage_cancellation(
    default_tag_policy, initialized_db
):
    repository = model.repository.create_repository("devtable", "story16-purge-upload", None)
    upload_uuid, metadata = storage.initiate_chunked_upload(["local_us"])
    content = b"repository purge leaves temporary upload storage"
    written, metadata, error = storage.stream_upload_chunk(
        ["local_us"], upload_uuid, 0, len(content), BytesIO(content), metadata
    )
    assert error is None
    assert written == len(content)
    upload = model.blob.initiate_upload_for_repo(
        repository,
        upload_uuid,
        "local_us",
        metadata,
        requested_digest_algorithm="sha512",
        requested_digest_state="persisted-requested-state",
    )

    try:
        assert storage.exists(["local_us"], upload_uuid)
        with mock_patch.object(
            storage, "cancel_chunked_upload", wraps=storage.cancel_chunked_upload
        ) as cancel:
            assert model.gc.purge_repository(repository, force=True)

        cancel.assert_not_called()
        assert BlobUpload.get_or_none(BlobUpload.id == upload.id) is None
        assert storage.exists(["local_us"], upload_uuid)
    finally:
        storage.cancel_chunked_upload(["local_us"], upload_uuid, metadata)


def test_purge_repository_storage_blob(default_tag_policy, initialized_db):
    with populate_storage_for_gc():
        expected_blobs_removed_from_storage = set()
        preferred = storage.preferred_locations[0]

        # Check that existing uploadedblobs has an object in storage
        for repo in database.Repository.select().order_by(database.Repository.id):
            for uploadedblob in UploadedBlob.select().where(UploadedBlob.repository == repo):
                assert storage.exists(
                    {preferred}, storage.blob_path(uploadedblob.blob.content_checksum)
                )

        # Remove eveyrhing
        for repo in database.Repository.select():  # .order_by(database.Repository.id):
            for uploadedblob in UploadedBlob.select().where(UploadedBlob.repository == repo):
                # Check if only this repository is referencing the uploadedblob
                # If so, the blob should be removed from storage
                has_depedent_manifestblob = (
                    ManifestBlob.select()
                    .where(
                        ManifestBlob.blob == uploadedblob.blob,
                        ManifestBlob.repository != repo,
                    )
                    .count()
                )
                has_dependent_uploadedblobs = (
                    UploadedBlob.select()
                    .where(
                        UploadedBlob.blob == uploadedblob.blob,
                        UploadedBlob.repository != repo,
                    )
                    .count()
                )

                if not has_depedent_manifestblob and not has_dependent_uploadedblobs:
                    expected_blobs_removed_from_storage.add(uploadedblob.blob)

            assert model.gc.purge_repository(repo, force=True)

        for removed_blob_from_storage in expected_blobs_removed_from_storage:
            assert not storage.exists(
                {preferred}, storage.blob_path(removed_blob_from_storage.content_checksum)
            )


def test_delete_manifests_with_subject(initialized_db):
    def generate_random_data_for_layer():
        charset = string.ascii_uppercase + string.ascii_lowercase + string.digits
        return "".join(random.choice(charset) for _ in range(random.randrange(1, 20)))

    repository = create_repository("devtable", "newrepo")

    config1 = {
        "os": "linux",
        "architecture": "amd64",
        "rootfs": {"type": "layers", "diff_ids": []},
        "history": [],
    }
    config1_json = json.dumps(config1)
    _, config1_digest = _populate_blob(repository, config1_json.encode("ascii"))

    # Add a blob of random data.
    random_data1 = generate_random_data_for_layer()
    _, random_digest1 = _populate_blob(repository, random_data1.encode("ascii"))

    oci_builder1 = OCIManifestBuilder()
    oci_builder1.set_config_digest(config1_digest, len(config1_json.encode("utf-8")))
    oci_builder1.add_layer(random_digest1, len(random_data1.encode("utf-8")))
    oci_manifest1 = oci_builder1.build()

    # Manifest 2
    # Add a blob containing the config.
    config2 = {
        "os": "linux",
        "architecture": "amd64",
        "rootfs": {"type": "layers", "diff_ids": []},
        "history": [],
    }
    config2_json = json.dumps(config2)
    _, config2_digest = _populate_blob(repository, config2_json.encode("ascii"))

    # Add a blob of random data.
    random_data2 = generate_random_data_for_layer()
    _, random_digest2 = _populate_blob(repository, random_data1.encode("ascii"))

    oci_builder2 = OCIManifestBuilder()
    oci_builder2.set_config_digest(config2_digest, len(config2_json.encode("utf-8")))
    oci_builder2.add_layer(random_digest2, len(random_data2.encode("utf-8")))
    oci_builder2.set_subject(
        oci_manifest1.digest, len(oci_manifest1.bytes.as_encoded_str()), oci_manifest1.media_type
    )
    oci_manifest2 = oci_builder2.build()

    manifest1_created = model.oci.manifest.get_or_create_manifest(
        repository, oci_manifest1, storage
    )
    assert manifest1_created

    # Delete temp tags for GC check
    Tag.delete().where(Tag.manifest == manifest1_created.manifest.id).execute()

    # Subject does not have referrers yet
    assert not model.gc._check_manifest_used(manifest1_created.manifest.id)

    manifest2_created = model.oci.manifest.get_or_create_manifest(
        repository, oci_manifest2, storage
    )
    assert manifest2_created

    # Check that the "temp" tag won't expire for the referrer
    tag2 = Tag.select().where(Tag.manifest == manifest2_created.manifest.id).get()
    assert tag2.lifetime_end_ms is None

    assert model.gc._check_manifest_used(manifest1_created.manifest.id)

    # The referrer should also be considered in use even without a tag,
    # otherwise GC would delete a valid manifest referrer.
    # These are kept alive with a "non-temporary" hidden tag.
    # In order to clean these up, they need to be manually deleted for now.
    assert model.gc._check_manifest_used(manifest2_created.manifest.id)

    # Make sure we're able to delete the untagged manifests when deleting the repository
    # i.e There should be no manifests leftover
    assert model.gc.purge_repository(repository, force=True)


def test_check_manifest_used_scoped_to_repository(initialized_db):
    """
    A referrer in a different repository sharing the same digest should not
    prevent garbage collection of a manifest.
    """

    def generate_random_data_for_layer():
        charset = string.ascii_uppercase + string.ascii_lowercase + string.digits
        return "".join(random.choice(charset) for _ in range(random.randrange(1, 20)))

    repo1 = create_repository("devtable", "repo_scoped_1")
    repo2 = create_repository("devtable", "repo_scoped_2")

    # Create a manifest in repo1
    config1_json = json.dumps(
        {
            "os": "linux",
            "architecture": "amd64",
            "rootfs": {"type": "layers", "diff_ids": []},
            "history": [],
        }
    )
    _, config1_digest = _populate_blob(repo1, config1_json.encode("ascii"))
    random_data1 = generate_random_data_for_layer()
    _, random_digest1 = _populate_blob(repo1, random_data1.encode("ascii"))

    oci_builder1 = OCIManifestBuilder()
    oci_builder1.set_config_digest(config1_digest, len(config1_json.encode("utf-8")))
    oci_builder1.add_layer(random_digest1, len(random_data1.encode("utf-8")))
    oci_manifest1 = oci_builder1.build()

    manifest1_created = model.oci.manifest.get_or_create_manifest(repo1, oci_manifest1, storage)
    assert manifest1_created

    # Remove tags so GC check relies only on referrer logic
    Tag.delete().where(Tag.manifest == manifest1_created.manifest.id).execute()

    # Create a referrer in repo2 that points to the same digest as manifest1
    config2_json = json.dumps(
        {
            "os": "linux",
            "architecture": "amd64",
            "rootfs": {"type": "layers", "diff_ids": []},
            "history": [],
        }
    )
    _, config2_digest = _populate_blob(repo2, config2_json.encode("ascii"))
    random_data2 = generate_random_data_for_layer()
    _, random_digest2 = _populate_blob(repo2, random_data2.encode("ascii"))

    oci_builder2 = OCIManifestBuilder()
    oci_builder2.set_config_digest(config2_digest, len(config2_json.encode("utf-8")))
    oci_builder2.add_layer(random_digest2, len(random_data2.encode("utf-8")))
    oci_builder2.set_subject(
        oci_manifest1.digest, len(oci_manifest1.bytes.as_encoded_str()), oci_manifest1.media_type
    )
    oci_manifest2 = oci_builder2.build()

    # New writes reject cross-repository subjects. Insert a legacy/corrupt row directly so this
    # test continues to verify that GC scopes defensive subject checks to the repository.
    manifest2_created = model.oci.manifest.create_manifest(
        repo2,
        oci_manifest2,
        canonical_subject_digest=oci_manifest1.digest,
    )
    assert manifest2_created

    # Cross-repo referrer should not prevent GC of manifest1 in repo1
    assert not model.gc._check_manifest_used(manifest1_created.manifest.id)


def test_tag_cleanup_with_autoprune_policy(default_tag_policy, initialized_db):
    repo1 = model.repository.create_repository("devtable", "newrepo", None)
    slack = ExternalNotificationMethod.get(ExternalNotificationMethod.name == "slack")
    notification = pre_oci_model.create_repo_notification(
        namespace_name="devtable",
        repository_name="newrepo",
        event_name="repo_image_expiry",
        method_name=slack.name,
        method_config={"url": "http://example.com"},
        event_config={"days": 5},
        title="Image(s) will expire in 5 days",
    )
    notification = model.notification.get_repo_notification(notification.uuid)
    manifest1, built1 = create_manifest_for_testing(
        repo1, differentiation_field="1", include_shared_blob=True
    )
    model.oci.tag.retarget_tag("tag1", manifest1)
    tag = Tag.select().where(Tag.name == "tag1", Tag.manifest == manifest1.id).get()

    TagNotificationSuccess.create(notification=notification.id, tag=tag.id, method=slack.id)

    with assert_gc_integrity(expect_storage_removed=True):
        delete_tag(repo1, "tag1", expect_gc=True)

    tag_notification_count = (
        TagNotificationSuccess.select().where(TagNotificationSuccess.tag == tag.id).count()
    )
    assert tag_notification_count == 0


def test_gc_cleans_up_pull_statistics(default_tag_policy, initialized_db):
    """
    Verify that GC removes tag pull statistics when tags are deleted.
    """
    repo = model.repository.create_repository("devtable", "newrepo", None)
    manifest, built = create_manifest_for_testing(
        repo, differentiation_field="1", include_shared_blob=True
    )

    # Create two tags pointing to the same manifest
    model.oci.tag.retarget_tag("tag1", manifest)
    model.oci.tag.retarget_tag("tag2", manifest)

    # Create pull statistics for both tags
    pull_statistics.bulk_upsert_tag_statistics(
        [
            {
                "repository_id": repo.id,
                "tag_name": "tag1",
                "pull_count": 10,
                "last_pull_timestamp": datetime.utcnow(),
                "manifest_digest": manifest.digest,
            },
            {
                "repository_id": repo.id,
                "tag_name": "tag2",
                "pull_count": 5,
                "last_pull_timestamp": datetime.utcnow(),
                "manifest_digest": manifest.digest,
            },
        ]
    )

    # Create pull statistics for the manifest
    pull_statistics.bulk_upsert_manifest_statistics(
        [
            {
                "repository_id": repo.id,
                "manifest_digest": manifest.digest,
                "pull_count": 15,
                "last_pull_timestamp": datetime.utcnow(),
            }
        ]
    )

    # Verify stats exist for both tags
    assert (
        TagPullStatistics.select()
        .where(TagPullStatistics.repository == repo, TagPullStatistics.tag_name == "tag1")
        .count()
        == 1
    )
    assert (
        TagPullStatistics.select()
        .where(TagPullStatistics.repository == repo, TagPullStatistics.tag_name == "tag2")
        .count()
        == 1
    )

    # Delete tag1 and run GC
    with assert_gc_integrity(expect_storage_removed=False):
        delete_tag(repo, "tag1", expect_gc=True)

    # Verify tag1 stats are cleaned up
    assert (
        TagPullStatistics.select()
        .where(TagPullStatistics.repository == repo, TagPullStatistics.tag_name == "tag1")
        .count()
        == 0
    )

    # Verify tag2 stats still exist
    assert (
        TagPullStatistics.select()
        .where(TagPullStatistics.repository == repo, TagPullStatistics.tag_name == "tag2")
        .count()
        == 1
    )

    # Manifest stats should still exist since manifest is still referenced by tag2
    assert (
        ManifestPullStatistics.select()
        .where(
            ManifestPullStatistics.repository == repo,
            ManifestPullStatistics.manifest_digest == manifest.digest,
        )
        .count()
        == 1
    )

    # Delete tag2 and run GC
    with assert_gc_integrity(expect_storage_removed=True):
        delete_tag(repo, "tag2", expect_gc=True)

    # Verify all tag pull statistics are cleaned up
    assert TagPullStatistics.select().where(TagPullStatistics.repository == repo).count() == 0

    # Check if manifest was GC'd
    manifest_exists = Manifest.select().where(Manifest.id == manifest.id).count() > 0
    manifest_stats_count = (
        ManifestPullStatistics.select().where(ManifestPullStatistics.repository == repo).count()
    )

    # If manifest was GC'd, its stats should be gone too
    if not manifest_exists:
        assert manifest_stats_count == 0, "Manifest was GC'd, so its stats should be cleaned up"


def test_gc_pull_statistics_with_shared_manifest(default_tag_policy, initialized_db):
    """
    Verify that tag pull statistics are deleted when tags are removed,
    and manifest pull statistics persist while the manifest is still referenced.
    """
    repo = model.repository.create_repository("devtable", "newrepo", None)
    manifest, built = create_manifest_for_testing(
        repo, differentiation_field="1", include_shared_blob=True
    )

    # Create two tags pointing to the same manifest
    model.oci.tag.retarget_tag("tag1", manifest)
    model.oci.tag.retarget_tag("tag2", manifest)

    # Create pull statistics for both tags
    pull_statistics.bulk_upsert_tag_statistics(
        [
            {
                "repository_id": repo.id,
                "tag_name": "tag1",
                "pull_count": 10,
                "last_pull_timestamp": datetime.utcnow(),
                "manifest_digest": manifest.digest,
            },
            {
                "repository_id": repo.id,
                "tag_name": "tag2",
                "pull_count": 5,
                "last_pull_timestamp": datetime.utcnow(),
                "manifest_digest": manifest.digest,
            },
        ]
    )

    # Create pull statistics for the manifest
    pull_statistics.bulk_upsert_manifest_statistics(
        [
            {
                "repository_id": repo.id,
                "manifest_digest": manifest.digest,
                "pull_count": 15,
                "last_pull_timestamp": datetime.utcnow(),
            }
        ]
    )

    # Verify stats exist
    assert TagPullStatistics.select().where(TagPullStatistics.repository == repo).count() == 2
    assert (
        ManifestPullStatistics.select().where(ManifestPullStatistics.repository == repo).count()
        == 1
    )

    # Delete tag1 and run GC
    with assert_gc_integrity(expect_storage_removed=False):
        delete_tag(repo, "tag1", expect_gc=True)

    # Verify tag1 stats are cleaned up but tag2 stats remain
    assert (
        TagPullStatistics.select()
        .where(TagPullStatistics.repository == repo, TagPullStatistics.tag_name == "tag1")
        .count()
        == 0
    )
    assert (
        TagPullStatistics.select()
        .where(TagPullStatistics.repository == repo, TagPullStatistics.tag_name == "tag2")
        .count()
        == 1
    )

    # Verify manifest stats still exist (manifest still referenced by tag2)
    assert (
        ManifestPullStatistics.select()
        .where(
            ManifestPullStatistics.repository == repo,
            ManifestPullStatistics.manifest_digest == manifest.digest,
        )
        .count()
        == 1
    )

    # Delete tag2 and run GC
    with assert_gc_integrity(expect_storage_removed=True):
        delete_tag(repo, "tag2", expect_gc=True)

    # Verify all tag pull statistics are cleaned up
    assert TagPullStatistics.select().where(TagPullStatistics.repository == repo).count() == 0


def test_gc_removes_manifest_stats_when_unreferenced(default_tag_policy, initialized_db):
    """
    Verify that manifest pull statistics are only deleted when the manifest is no longer
    referenced by any tags and gets garbage collected.

    This test uses the same manifest for both tags to explicitly test that manifest stats
    persist while at least one tag references the manifest, and are only removed when
    the last tag is deleted and the manifest is GC'd.
    """
    repo = model.repository.create_repository("devtable", "newrepo", None)

    # Create a single manifest
    manifest, built = create_manifest_for_testing(
        repo, differentiation_field="1", include_shared_blob=True
    )

    # Create two tags pointing to the SAME manifest
    model.oci.tag.retarget_tag("tag1", manifest)
    model.oci.tag.retarget_tag("tag2", manifest)

    # Create pull statistics for both tags
    pull_statistics.bulk_upsert_tag_statistics(
        [
            {
                "repository_id": repo.id,
                "tag_name": "tag1",
                "pull_count": 10,
                "last_pull_timestamp": datetime.utcnow(),
                "manifest_digest": manifest.digest,
            },
            {
                "repository_id": repo.id,
                "tag_name": "tag2",
                "pull_count": 5,
                "last_pull_timestamp": datetime.utcnow(),
                "manifest_digest": manifest.digest,
            },
        ]
    )

    # Create pull statistics for the manifest
    pull_statistics.bulk_upsert_manifest_statistics(
        [
            {
                "repository_id": repo.id,
                "manifest_digest": manifest.digest,
                "pull_count": 15,
                "last_pull_timestamp": datetime.utcnow(),
            }
        ]
    )

    # Verify stats exist
    assert TagPullStatistics.select().where(TagPullStatistics.repository == repo).count() == 2
    assert (
        ManifestPullStatistics.select()
        .where(
            ManifestPullStatistics.repository == repo,
            ManifestPullStatistics.manifest_digest == manifest.digest,
        )
        .count()
        == 1
    )

    # Delete tag1 and run GC - manifest should NOT be GC'd since tag2 still references it
    with assert_gc_integrity(expect_storage_removed=False):
        delete_tag(repo, "tag1", expect_gc=True)

    # Verify tag1 stats are cleaned up
    assert (
        TagPullStatistics.select()
        .where(TagPullStatistics.repository == repo, TagPullStatistics.tag_name == "tag1")
        .count()
        == 0
    )

    # Verify tag2 stats still exist
    assert (
        TagPullStatistics.select()
        .where(TagPullStatistics.repository == repo, TagPullStatistics.tag_name == "tag2")
        .count()
        == 1
    )

    # CRITICAL CHECK: Manifest stats should STILL exist because tag2 still references the manifest
    assert (
        ManifestPullStatistics.select()
        .where(
            ManifestPullStatistics.repository == repo,
            ManifestPullStatistics.manifest_digest == manifest.digest,
        )
        .count()
        == 1
    ), "Manifest stats should persist while tag2 still references the manifest"

    # Verify the manifest itself still exists
    assert Manifest.select().where(Manifest.id == manifest.id).count() == 1

    # Delete tag2 (the LAST tag) and run GC - now the manifest should be GC'd
    with assert_gc_integrity(expect_storage_removed=True):
        delete_tag(repo, "tag2", expect_gc=True)

    # Verify all tag stats are cleaned up
    assert TagPullStatistics.select().where(TagPullStatistics.repository == repo).count() == 0

    # Check if manifest was GC'd
    manifest_exists = Manifest.select().where(Manifest.id == manifest.id).count() > 0

    if not manifest_exists:
        # If manifest was GC'd, its pull statistics should also be deleted
        assert (
            ManifestPullStatistics.select()
            .where(
                ManifestPullStatistics.repository == repo,
                ManifestPullStatistics.manifest_digest == manifest.digest,
            )
            .count()
            == 0
        ), "Manifest was GC'd but its pull statistics were not cleaned up"


def test_gc_skips_immutable_tags(default_tag_policy, initialized_db):
    """
    Verify that GC does not delete immutable tags even if they have lifetime_end_ms set,
    when FEATURE_IMMUTABLE_TAGS_CAN_EXPIRE is False.
    """
    repo = model.repository.create_repository("devtable", "newrepo", None)
    manifest, built = create_manifest_for_testing(
        repo, differentiation_field="1", include_shared_blob=True
    )

    # Create a tag with immutable=True and an expiration in the past
    now_ms = database.get_epoch_timestamp_ms()
    tag = Tag.create(
        name="immutable-tag",
        repository=repo.id,
        manifest=manifest,
        lifetime_start_ms=now_ms - 100000,
        lifetime_end_ms=now_ms - 50000,  # expired
        hidden=False,
        immutable=True,
        reversion=False,
        tag_kind=Tag.tag_kind.get_id("tag"),
    )

    # Set time machine to 0 so expired tags are immediately eligible for GC
    _set_tag_expiration_policy(repo.namespace_user.username, 0)

    with mock_patch("features.IMMUTABLE_TAGS", True):
        with mock_patch.dict(
            "data.model.oci.tag.config.app_config", {"FEATURE_IMMUTABLE_TAGS_CAN_EXPIRE": False}
        ):
            # GC should skip the immutable tag
            gc_now(repo)

    # Tag should still exist
    assert Tag.select().where(Tag.id == tag.id).count() == 1


def test_gc_collects_immutable_tags_when_can_expire(default_tag_policy, initialized_db):
    """
    Verify that GC collects expired immutable tags when FEATURE_IMMUTABLE_TAGS_CAN_EXPIRE is True.
    """
    repo = model.repository.create_repository("devtable", "newrepo", None)
    manifest, built = create_manifest_for_testing(
        repo, differentiation_field="1", include_shared_blob=True
    )

    # Create a tag with immutable=True and an expiration in the past
    now_ms = database.get_epoch_timestamp_ms()
    tag = Tag.create(
        name="immutable-expiring-tag",
        repository=repo.id,
        manifest=manifest,
        lifetime_start_ms=now_ms - 100000,
        lifetime_end_ms=now_ms - 50000,  # expired
        hidden=False,
        immutable=True,
        reversion=False,
        tag_kind=Tag.tag_kind.get_id("tag"),
    )

    # Set time machine to 0 so expired tags are immediately eligible for GC
    _set_tag_expiration_policy(repo.namespace_user.username, 0)

    with mock_patch("features.IMMUTABLE_TAGS", True):
        with mock_patch.dict(
            "data.model.oci.tag.config.app_config", {"FEATURE_IMMUTABLE_TAGS_CAN_EXPIRE": True}
        ):
            gc_now(repo)

    # Tag should be deleted
    assert Tag.select().where(Tag.id == tag.id).count() == 0


def test_gc_purge_oci_tag_guard_skips_immutable(default_tag_policy, initialized_db):
    """
    Verify the defense-in-depth guard in _purge_oci_tag directly skips immutable tags
    during GC, but allows them during repository deletion (allow_non_expired=True).
    """
    from data.model.gc import _GarbageCollectorContext, _purge_oci_tag

    repo = model.repository.create_repository("devtable", "newrepo", None)
    manifest, _ = create_manifest_for_testing(
        repo, differentiation_field="1", include_shared_blob=True
    )

    now_ms = database.get_epoch_timestamp_ms()
    tag = Tag.create(
        name="guarded-tag",
        repository=repo.id,
        manifest=manifest,
        lifetime_start_ms=now_ms - 100000,
        lifetime_end_ms=now_ms - 50000,
        hidden=False,
        immutable=True,
        reversion=False,
        tag_kind=Tag.tag_kind.get_id("tag"),
    )

    context = _GarbageCollectorContext(repo)

    with mock_patch("features.IMMUTABLE_TAGS", True):
        with mock_patch.dict(
            "data.model.gc.config.app_config", {"FEATURE_IMMUTABLE_TAGS_CAN_EXPIRE": False}
        ):
            # GC path (allow_non_expired=False) should skip the tag
            result = _purge_oci_tag(tag, context, allow_non_expired=False)
            assert result is False
            assert Tag.select().where(Tag.id == tag.id).count() == 1

            # Repository deletion path (allow_non_expired=True) should proceed
            result = _purge_oci_tag(tag, context, allow_non_expired=True)
            assert result is not False


def _get_digest(content_bytes):
    """
    Helper function that creates blobs with proper digests
    """
    return sha256_digest(content_bytes)


def test_gc_skips_placeholder_blobs(default_tag_policy, initialized_db):
    """
    Tests that GC skips placeholder blobs from being garbage collected. Deleting these could cause the database
    to reference blobs that have been removed from backing storage, causing pull failures.
    """
    repo = model.repository.create_repository("devtable", "placeholder_test", None)
    blob_content = random.randbytes(1024)
    blob_digest = _get_digest(blob_content)

    placeholder_blob = ImageStorage.create(
        content_checksum=blob_digest,
        image_size=len(blob_content),
        uncompressed_size=len(blob_content),
    )

    assert (
        not ImageStoragePlacement.select()
        .where(ImageStoragePlacement.storage == placeholder_blob)
        .exists()
    )

    orphaned_ids = storage_model.garbage_collect_storage([placeholder_blob.id])
    assert placeholder_blob.id not in orphaned_ids
    assert ImageStorage.select().where(ImageStorage.id == placeholder_blob.id).exists()


def test_gc_collects_orphaned_blobs_with_placement(default_tag_policy, initialized_db):
    """
    Tests that GC DOES delete orphaned blobs that have placement set.
    """
    location = ImageStorageLocation.get(name="local_us")

    blob_content = random.randbytes(1024)
    blob_digest = _get_digest(blob_content)

    # We cannot use _populate_blob here because it would create an entry in UploadedBlob table which would
    # protect it from GCing because of the _is_storage_orphaned check, so we have to create it manually.
    normal_blob = ImageStorage.create(
        content_checksum=blob_digest,
        image_size=len(blob_content),
        uncompressed_size=len(blob_content),
    )
    ImageStoragePlacement.create(storage=normal_blob, location=location)

    storage.put_content(
        ["local_us"],
        storage.blob_path(normal_blob.content_checksum),
        blob_content,
    )

    assert (
        ImageStoragePlacement.select().where(ImageStoragePlacement.storage == normal_blob).exists()
    )

    # This blob is orphaned because no ManifestBlob or UploadedBlob entries exist so it should be picked
    # by GC.
    orphaned_ids = storage_model.garbage_collect_storage([normal_blob.id])
    assert normal_blob.id in orphaned_ids

    assert not ImageStorage.select().where(ImageStorage.id == normal_blob.id).exists()


def test_gc_storage_logs_namespace_and_repo(default_tag_policy, initialized_db):
    """
    When namespace and repo_name are provided, garbage_collect_storage emits an
    info-level log line containing both values for each removed blob.
    """
    location = ImageStorageLocation.get(name="local_us")

    blob_content = random.randbytes(1024)
    blob_digest = _get_digest(blob_content)

    orphan = ImageStorage.create(
        content_checksum=blob_digest,
        image_size=len(blob_content),
        uncompressed_size=len(blob_content),
    )
    ImageStoragePlacement.create(storage=orphan, location=location)
    storage.put_content(["local_us"], storage.blob_path(blob_digest), blob_content)

    with mock_patch("data.model.storage.logger") as mock_logger:
        mock_logger.debug = logging.getLogger().debug
        mock_logger.warning = logging.getLogger().warning

        orphaned_ids = storage_model.garbage_collect_storage(
            [orphan.id], namespace="testns", repo_name="testrepo"
        )

    assert orphan.id in orphaned_ids
    mock_logger.info.assert_called()
    log_msg = mock_logger.info.call_args[0][0] % tuple(mock_logger.info.call_args[0][1:])
    assert "testns" in log_msg
    assert "testrepo" in log_msg
    assert blob_digest in log_msg


def test_gc_storage_no_log_without_namespace(default_tag_policy, initialized_db):
    """
    When namespace is not provided, garbage_collect_storage does not emit
    the audit log line.
    """
    location = ImageStorageLocation.get(name="local_us")

    blob_content = random.randbytes(1024)
    blob_digest = _get_digest(blob_content)

    orphan = ImageStorage.create(
        content_checksum=blob_digest,
        image_size=len(blob_content),
        uncompressed_size=len(blob_content),
    )
    ImageStoragePlacement.create(storage=orphan, location=location)
    storage.put_content(["local_us"], storage.blob_path(blob_digest), blob_content)

    with mock_patch("data.model.storage.logger") as mock_logger:
        mock_logger.debug = logging.getLogger().debug
        mock_logger.warning = logging.getLogger().warning

        orphaned_ids = storage_model.garbage_collect_storage([orphan.id])

    assert orphan.id in orphaned_ids
    mock_logger.info.assert_not_called()


def test_gc_storage_returns_version_id_from_store(default_tag_policy, initialized_db):
    """
    garbage_collect_storage captures the return value of config.store.remove
    and includes a delete_marker in the log when the storage backend returns
    a version id.
    """
    location = ImageStorageLocation.get(name="local_us")

    blob_content = random.randbytes(1024)
    blob_digest = _get_digest(blob_content)

    orphan = ImageStorage.create(
        content_checksum=blob_digest,
        image_size=len(blob_content),
        uncompressed_size=len(blob_content),
    )
    ImageStoragePlacement.create(storage=orphan, location=location)
    storage.put_content(["local_us"], storage.blob_path(blob_digest), blob_content)

    fake_version_id = "v-abc123"
    original_remove = storage.remove

    def patched_remove(locations, path):
        original_remove(locations, path)
        return fake_version_id

    import logging

    with mock_patch.object(storage, "remove", side_effect=patched_remove):
        with mock_patch("data.model.storage.logger") as mock_logger:
            mock_logger.debug = logging.getLogger().debug
            mock_logger.warning = logging.getLogger().warning

            storage_model.garbage_collect_storage(
                [orphan.id], namespace="testns", repo_name="testrepo"
            )

    mock_logger.info.assert_called()
    log_msg = mock_logger.info.call_args[0][0] % tuple(mock_logger.info.call_args[0][1:])
    assert fake_version_id in log_msg
    assert "delete_marker" in log_msg


def test_gc_storage_omits_delete_marker_when_none(default_tag_policy, initialized_db):
    """
    When the storage backend returns None (non-versioned bucket), the log
    line should not include a delete_marker fragment.
    """
    location = ImageStorageLocation.get(name="local_us")

    blob_content = random.randbytes(1024)
    blob_digest = _get_digest(blob_content)

    orphan = ImageStorage.create(
        content_checksum=blob_digest,
        image_size=len(blob_content),
        uncompressed_size=len(blob_content),
    )
    ImageStoragePlacement.create(storage=orphan, location=location)
    storage.put_content(["local_us"], storage.blob_path(blob_digest), blob_content)

    with mock_patch("data.model.storage.logger") as mock_logger:
        mock_logger.debug = logging.getLogger().debug
        mock_logger.warning = logging.getLogger().warning

        storage_model.garbage_collect_storage([orphan.id], namespace="testns", repo_name="testrepo")

    mock_logger.info.assert_called()
    log_msg = mock_logger.info.call_args[0][0] % tuple(mock_logger.info.call_args[0][1:])
    assert "delete_marker" not in log_msg


def test_gc_proxy_cache_expired_temp_tag(default_tag_policy, initialized_db):
    """
    Test the proxy cache race condition scenario:
    1. Proxy cache creates manifest with placeholder blobs
    2. Temp tag gets created
    3. Tag expires and manifest is GCed
    4. Placeholder blobs should NOT be deleted
    """
    repo = model.repository.create_repository("devtable", "proxy_test", None)
    media_type, _ = MediaType.get_or_create(name="application/vnd.oci.image.manifest.v1+json")
    manifest_bytes = (
        '{"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json"}'
    )
    manifest = Manifest.create(
        repository=repo,
        digest=_get_digest(manifest_bytes.encode("utf-8")),
        manifest_bytes=manifest_bytes,
        media_type=media_type,
    )

    # Placeholder blobs
    blob_list = []
    for i in range(0, 3):
        blob_content = random.randbytes(1024)
        placeholder_blob = ImageStorage.create(
            content_checksum=_get_digest(blob_content),
            image_size=len(blob_content),
            uncompressed_size=len(blob_content),
        )
        ManifestBlob.create(manifest=manifest, blob=placeholder_blob, repository=repo)
        blob_list.append(placeholder_blob)

    # Create a temporary tag that's already expired
    past_time = datetime.utcnow() - timedelta(hours=1)
    with freeze_time(past_time):
        now_ms = oci.tag.get_epoch_timestamp_ms()
        temp_tag = Tag.create(
            name=f"$temp-{manifest.digest}",
            repository=repo,
            manifest=manifest,
            lifetime_start_ms=now_ms,
            lifetime_end_ms=now_ms + 300000,  # 5 minutes from frozen time
            hidden=True,
            reversion=False,
            tag_kind=Tag.tag_kind.get_id("tag"),
        )

    # Run GC and check if entries in tables exist
    model.gc.garbage_collect_repo(repo)
    assert not Manifest.select().where(Manifest.id == manifest.id).exists()
    assert not ManifestBlob.select().where(ManifestBlob.manifest == manifest.id).exists()
    # Placeholder blobs should still exist
    for blob in blob_list:
        assert ImageStorage.select().where(ImageStorage.id == blob.id).exists()


def test_is_storage_orphaned_returns_false_for_placeholders(default_tag_policy, initialized_db):
    """
    Tests whether _is_storage_orphaned returns a negative result (not orphaned) for each placeholder blob
    even if these do not have a ManifestBlob in place.
    """
    blob_content = random.randbytes(1024)
    placeholder_blob = ImageStorage.create(
        content_checksum=_get_digest(blob_content),
        image_size=len(blob_content),
        uncompressed_size=len(blob_content),
    )

    # Blob has no ManifestBlob, UploadedBlob or placement entry but should not be considered orphaned
    assert not storage_model._is_storage_orphaned(placeholder_blob.id)

    # Add placement, should be considered orphaned
    location = ImageStorageLocation.get(name="local_us")
    ImageStoragePlacement.create(storage=placeholder_blob, location=location)
    assert storage_model._is_storage_orphaned(placeholder_blob.id)


def test_gc_placeholder_blob_eventually_collected(default_tag_policy, initialized_db):
    """
    Tests if placeholder blobs are eventually collected once they get get a placement and their manifest
    is deleted.
    This ensures that placeholder blobs are not leaked indefinitely.
    """
    repo = model.repository.create_repository("devtable", "placeholder_cleanup", None)
    media_type, _ = MediaType.get_or_create(name="application/vnd.oci.image.manifest.v1+json")

    # Create placeholder blobs
    media_type, _ = MediaType.get_or_create(name="application/vnd.oci.image.manifest.v1+json")
    manifest_bytes = (
        '{"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json"}'
    )
    manifest = Manifest.create(
        repository=repo,
        digest=_get_digest(manifest_bytes.encode("utf-8")),
        manifest_bytes=manifest_bytes,
        media_type=media_type,
    )

    blob_list = []
    for i in range(0, 3):
        blob_content = random.randbytes(1024)
        placeholder_blob = ImageStorage.create(
            content_checksum=_get_digest(blob_content),
            image_size=len(blob_content),
            uncompressed_size=len(blob_content),
        )
        ManifestBlob.create(manifest=manifest, blob=placeholder_blob, repository=repo)

        # Download worker adds placement
        location = ImageStorageLocation.get(name="local_us")
        ImageStoragePlacement.create(storage=placeholder_blob, location=location)
        storage.put_content(
            ["local_us"],
            storage.blob_path(placeholder_blob.content_checksum),
            blob_content,
        )
        blob_list.append(placeholder_blob)

    # Delete manifest simulating manifest GC
    ManifestBlob.delete().where(ManifestBlob.manifest == manifest.id).execute()
    manifest.delete_instance()

    # Blob has placement but not references, should be GCed
    orphaned_ids = storage_model.garbage_collect_storage([blob.id for blob in blob_list])
    for blob in blob_list:
        assert blob.id in orphaned_ids
        assert not ImageStorage.select().where(ImageStorage.id == blob.id).exists()


def test_get_or_create_blob_with_lock_gets_existing(default_tag_policy, initialized_db):
    """
    Tests that get_or_create_blob_with_lock returns an existing blob without creating a duplicate.
    """
    blob_content = random.randbytes(1024)
    blob_digest = _get_digest(blob_content)

    # create initial blob
    existing_blob = ImageStorage.create(
        content_checksum=blob_digest,
        image_size=len(blob_content),
        uncompressed_size=len(blob_content),
    )

    # call the function
    result_blob = storage_model.get_or_create_blob_with_lock(
        digest=blob_digest,
        image_size=999,
    )

    assert result_blob.id == existing_blob.id
    assert result_blob.image_size == len(blob_content)

    assert ImageStorage.select().where(ImageStorage.content_checksum == blob_digest).count() == 1


def test_get_or_create_blob_with_lock_creates_new(default_tag_policy, initialized_db):
    """
    Verifies that the function creates a new blob if the blob doesn't exist.
    """
    blob_content = random.randbytes(1024)
    blob_digest = _get_digest(blob_content)

    # Verify blob doesn't exist in storage
    assert not ImageStorage.select().where(ImageStorage.content_checksum == blob_digest).exists()

    # create blob
    result_blob = storage_model.get_or_create_blob_with_lock(
        digest=blob_digest,
        image_size=len(blob_content),
        uncompressed_size=len(blob_content),
    )

    assert result_blob.content_checksum == blob_digest
    assert result_blob.image_size == len(blob_content)

    # verify that it was created in the database
    assert ImageStorage.select().where(ImageStorage.content_checksum == blob_digest).exists()


def test_get_or_create_blob_with_lock_fallback_gets_existing(default_tag_policy, initialized_db):
    """
    Verifies that when the lock cannot be acquired, the function falls back to
    getting an existing blob after a short delay.
    """
    blob_content = random.randbytes(1024)
    blob_digest = _get_digest(blob_content)

    # Create the blob first
    existing_blob = ImageStorage.create(
        content_checksum=blob_digest,
        image_size=len(blob_content),
    )

    # Patch GlobalLock to always raise LockNotAcquiredException
    from util.locking import LockNotAcquiredException

    class FailingLock:
        lock_factory = object()

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            raise LockNotAcquiredException()

        def __exit__(self, *args):
            pass

    original_lock = storage_model.GlobalLock
    storage_model.GlobalLock = FailingLock
    try:
        result_blob = storage_model.get_or_create_blob_with_lock(
            digest=blob_digest,
            image_size=999,
        )
        assert result_blob.id == existing_blob.id
        assert (
            ImageStorage.select().where(ImageStorage.content_checksum == blob_digest).count() == 1
        )
    finally:
        storage_model.GlobalLock = original_lock


def test_get_or_create_blob_with_lock_fallback_creates_new(default_tag_policy, initialized_db):
    """
    Verifies that when the lock cannot be acquired and the blob doesn't exist,
    the function falls back to creating the blob without the lock.
    """
    blob_content = random.randbytes(1024)
    blob_digest = _get_digest(blob_content)

    assert not ImageStorage.select().where(ImageStorage.content_checksum == blob_digest).exists()

    from util.locking import LockNotAcquiredException

    class FailingLock:
        lock_factory = object()

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            raise LockNotAcquiredException()

        def __exit__(self, *args):
            pass

    original_lock = storage_model.GlobalLock
    storage_model.GlobalLock = FailingLock
    try:
        result_blob = storage_model.get_or_create_blob_with_lock(
            digest=blob_digest,
            image_size=len(blob_content),
        )
        assert result_blob.content_checksum == blob_digest
        assert ImageStorage.select().where(ImageStorage.content_checksum == blob_digest).exists()
    finally:
        storage_model.GlobalLock = original_lock


def test_gc_manifest_list_with_children(default_tag_policy, initialized_db):
    """
    Tests that purging of a repository with a manifest list correctly removes all
    manifests and their children.
    """
    repo = model.repository.create_repository("devtable", "newrepo", None)

    # create three child manifests
    _, build1 = create_manifest_for_testing(repo, differentiation_field="amd64")
    _, build2 = create_manifest_for_testing(repo, differentiation_field="ppc64le")
    _, build3 = create_manifest_for_testing(repo, differentiation_field="arm64")

    # create index
    index_builder = OCIIndexBuilder()
    index_builder.add_manifest(build1, architecture="amd64", os="Linux")
    index_builder.add_manifest(build2, architecture="ppc64le", os="Linux")
    index_builder.add_manifest(build3, architecture="arm64", os="Linux")
    manifest_list = index_builder.build()

    created = model.oci.manifest.get_or_create_manifest(repo, manifest_list, storage)
    assert created

    # tag the manifest list
    model.oci.tag.retarget_tag("latest", created.manifest)

    # verify manifest child rows exist
    assert ManifestChild.select().where(ManifestChild.repository == repo).count() == 3

    # purge repo
    assert model.gc.purge_repository(repo, force=True)

    # verify eveything's cleaned up
    assert Manifest.select().where(Manifest.repository == repo).count() == 0
    assert ManifestChild.select().where(ManifestChild.repository == repo).count() == 0
    assert ManifestBlob.select().where(ManifestBlob.repository == repo).count() == 0


def test_gc_manifest_with_labels(default_tag_policy, initialized_db):
    """
    Tests that purging of a repository correctly GCs all labels associated with the manifests.
    """
    repo = model.repository.create_repository("devtable", "newrepo", None)
    assert repo
    manifest, _ = create_manifest_for_testing(repo, differentiation_field="labeled")

    # add labels to the manifest
    model.oci.label.create_manifest_label(
        manifest.id, "maintainer", "devtable@devtable.com", "manifest"
    )
    model.oci.label.create_manifest_label(manifest.id, "tester", "test@devtable.com", "manifest")
    model.oci.label.create_manifest_label(manifest.id, "version", "0.0.1", "manifest")

    assert ManifestLabel.select().where(ManifestLabel.repository == repo).count() == 3
    pre_gc_label_count = Label.select().count()

    # Purge repo
    assert model.gc.purge_repository(repo, force=True)

    assert Manifest.select().where(Manifest.repository == repo).count() == 0
    assert ManifestLabel.select().where(ManifestLabel.repository == repo).count() == 0
    assert Label.select().count() < pre_gc_label_count


def test_purge_manifest_list_no_infinite_loop(default_tag_policy, initialized_db):
    """
    Tests that purging a repository with manifest lists does not end in an infinite loop when
    child manifests have lower ID than their parent.
    """
    repo = model.repository.create_repository("devtable", "newrepo", None)
    assert repo

    built_manifests = []
    child_manifests = []

    # create a massive manifest list that contains 15 child image
    # Quay's batching process uses up to 10 manifests in a batch
    test_architectures = [
        "amd64",
        "386",
        "arm64",
        "arm",
        "ppc64le",
        "ppc64",
        "s390x",
        "mips64le",
        "mips64",
        "mipsle",
        "mips",
        "riscv64",
        "loong64",
        "sparc64",
        "wasm",
    ]
    for arch in test_architectures:
        child, build = create_manifest_for_testing(repo, differentiation_field=arch)
        built_manifests.append(build)
        child_manifests.append(child)

    index_builder = OCIIndexBuilder()
    for i, build in enumerate(built_manifests):
        index_builder.add_manifest(build, architecture=test_architectures[i], os="Linux")

    manifest_list = index_builder.build()

    created = model.oci.manifest.get_or_create_manifest(repo, manifest_list, storage)
    assert created
    model.oci.tag.retarget_tag("latest", created.manifest)

    # check if children have lower IDs than the parent
    for child in child_manifests:
        assert child.id < created.manifest.id

    assert model.gc.purge_repository(repo, force=True)
    assert Manifest.select().where(Manifest.repository == repo).count() == 0


def test_gc_shared_label_survives_partial_gc(default_tag_policy, initialized_db):
    """
    Tests that labels shared across multiple images survive the GC process and are not
    deleted during garbage collection.
    """
    repo = model.repository.create_repository("devtable", "newrepo", None)

    manifest1, _ = create_manifest_for_testing(repo, differentiation_field="shared1")
    manifest2, _ = create_manifest_for_testing(repo, differentiation_field="shared2")

    model.oci.tag.retarget_tag("tag1", manifest1)
    model.oci.tag.retarget_tag("tag2", manifest2)

    # add same label to both manifests
    label = Label.create(
        key="shared-key",
        value="shared-value",
        source_type=Label.source_type.get_id("manifest"),
        media_type=Label.media_type.get_id("text/plain"),
    )
    ManifestLabel.create(manifest=manifest1.id, label=label, repository=repo)
    ManifestLabel.create(manifest=manifest2.id, label=label, repository=repo)

    # delete tag1, GC manifest1
    now = datetime.utcnow()
    with freeze_time(now + timedelta(minutes=5)):
        delete_tag(repo, "tag1", expect_gc=True)

    # verify that label survived
    assert Label.select().where(Label.id == label.id).exists()
    assert not ManifestLabel.select().where(ManifestLabel.manifest == manifest1.id).exists()
    assert ManifestLabel.select().where(ManifestLabel.manifest == manifest2.id).exists()


def test_gc_manifest_list_partial_gc_child_in_use(default_tag_policy, initialized_db):
    """
    Tests that child images still in use by other manifests are not deleted when one of the
    parent's manifest list gets GCed.
    """
    repo = model.repository.create_repository("devtable", "newrepo", None)

    # create child manifests
    child_shared, build_shared = create_manifest_for_testing(repo, differentiation_field="shared")
    child_only1, build_only1 = create_manifest_for_testing(repo, differentiation_field="only1")
    child_only2, build_only2 = create_manifest_for_testing(repo, differentiation_field="only2")

    # create two different manifest lists that share the child image
    index_builder1 = OCIIndexBuilder()
    index_builder1.add_manifest(build_shared, architecture="amd64", os="linux")
    index_builder1.add_manifest(build_only1, architecture="arm64", os="linux")
    list1 = index_builder1.build()

    index_builder2 = OCIIndexBuilder()
    index_builder2.add_manifest(build_shared, architecture="amd64", os="linux")
    index_builder2.add_manifest(build_only2, architecture="s390x", os="linux")
    list2 = index_builder2.build()

    created1 = model.oci.manifest.get_or_create_manifest(repo, list1, storage)
    created2 = model.oci.manifest.get_or_create_manifest(repo, list2, storage)
    assert created1
    assert created2

    model.oci.tag.retarget_tag("tag1", created1.manifest)
    model.oci.tag.retarget_tag("tag2", created2.manifest)

    # delete tag 1, GC manifest 1
    now = datetime.utcnow()
    with freeze_time(now + timedelta(minutes=5)):
        delete_tag(repo, "tag1", perform_gc=True, expect_gc=True)

    # shared child must survive
    assert Manifest.select().where(Manifest.id == child_shared.id).exists()

    # child_only1 should be deleted
    assert not Manifest.select().where(Manifest.id == child_only1.id).exists()

    # child_only2 must survive
    assert Manifest.select().where(Manifest.id == child_only2.id).exists()
