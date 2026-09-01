from __future__ import annotations

import logging
import os
from collections import namedtuple
from typing import Literal, Optional, overload

from peewee import IntegrityError, fn

import features
from data.database import (
    ExternalNotificationEvent,
    IndexerVersion,
    IndexStatus,
    Manifest,
    ManifestBlob,
    ManifestChild,
    ManifestSecurityStatus,
    Repository,
    RepositoryManifestDigest,
    RepositoryNotification,
    Tag,
    db,
    db_transaction,
    get_epoch_timestamp_ms,
)
from data.model import BlobDoesNotExist, ManifestDigestConflictException, config
from data.model.blob import get_or_create_shared_blob, get_shared_blob
from data.model.oci.blob import get_repository_blob_by_digest
from data.model.oci.label import create_manifest_label
from data.model.oci.retriever import RepositoryContentRetriever
from data.model.oci.tag import (
    create_temporary_tag_if_necessary,
    filter_to_alive_tags,
    get_child_manifests,
)
from data.model.quota import QuotaOperation, update_quota
from image.docker.schema1 import ManifestException
from image.docker.schema2 import EMPTY_LAYER_BLOB_DIGEST, EMPTY_LAYER_BYTES
from image.docker.schema2.list import MalformedSchema2ManifestList
from image.shared.interfaces import ManifestInterface, ManifestListInterface
from util.validation import is_json

TEMP_TAG_EXPIRATION_SEC = 300  # 5 minutes
_UNRESOLVED_SUBJECT = object()


logger = logging.getLogger(__name__)


def is_manifest_present(manifest) -> bool:
    """
    Check if manifest content is available (not sparse).

    A manifest is considered "sparse" when it exists in the database but
    has empty manifest_bytes. This typically happens with pull-through proxy
    where child manifests of a manifest list may not be fetched until accessed.

    Args:
        manifest: A Manifest database row or object with manifest_bytes attribute.

    Returns:
        True if the manifest has content, False if it's sparse (empty/missing content).
    """
    manifest_bytes = manifest.manifest_bytes
    return manifest_bytes is not None and manifest_bytes != ""


CreatedManifest = namedtuple("CreatedManifest", ["manifest", "newly_created", "labels_to_apply"])


class CreateManifestException(Exception):
    """
    Exception raised when creating a manifest fails and explicit exception raising is requested.
    """


class ManifestBlobUnknownException(CreateManifestException):
    def __init__(self, digest):
        super().__init__("Unknown blob `%s`" % digest)
        self.digest = digest


class ManifestChildUnknownException(CreateManifestException):
    def __init__(self, digest):
        super().__init__("Unknown child manifest `%s`" % digest)
        self.digest = digest


class ManifestSubjectUnknownException(CreateManifestException):
    def __init__(self, digest):
        super().__init__("Unknown subject manifest `%s`" % digest)
        self.digest = digest


class ManifestDescriptorMismatchException(CreateManifestException):
    def __init__(self, digest, reason):
        super().__init__("Manifest descriptor `%s` has mismatched %s" % (digest, reason))
        self.digest = digest
        self.reason = reason


class _ManifestAlreadyExists(Exception):
    """
    Exception raised to break out of manifest creation due to the manifest already existing.
    """

    def __init__(self, internal_exception):
        self.internal_exception = internal_exception


def find_manifests_for_sec_notification(manifest_digest):
    """
    Finds all manifests matching the given digest that live in a repository with a registered
    notification event for security scan results.
    """

    return (
        Manifest.select(Manifest, Repository)
        .join(Repository)
        .join(RepositoryNotification)
        .where(
            Manifest.digest == manifest_digest,
            RepositoryNotification.event
            == ExternalNotificationEvent.get(name="vulnerability_found"),
        )
    )


def lookup_manifest(
    repository_id,
    manifest_digest,
    allow_dead=False,
    allow_hidden=False,
    require_available=False,
    temp_tag_expiration_sec=TEMP_TAG_EXPIRATION_SEC,
):
    """
    Returns the repository-visible manifest for the specified digest or None if none.

    Registered identities are resolved first. Canonical SHA-256 remains available for historical
    manifests only while the repository/manifest pair has no explicit registrations.
    """
    if not require_available:
        return _lookup_manifest(
            repository_id, manifest_digest, allow_dead=allow_dead, allow_hidden=allow_hidden
        )

    with db_transaction():
        found = _lookup_manifest(
            repository_id, manifest_digest, allow_dead=allow_dead, allow_hidden=allow_hidden
        )
        if found is None:
            return None

        create_temporary_tag_if_necessary(found, temp_tag_expiration_sec)
        return found


def lookup_canonical_manifest(
    repository_id,
    canonical_digest,
    allow_dead=False,
    allow_hidden=False,
    require_available=False,
    temp_tag_expiration_sec=TEMP_TAG_EXPIRATION_SEC,
):
    """Looks up Quay's internal canonical manifest identity without exposing it externally."""
    query = Manifest.select().where(
        Manifest.repository == repository_id,
        Manifest.digest == canonical_digest,
    )
    found = _lookup_manifest_query(query, allow_dead=allow_dead, allow_hidden=allow_hidden)
    if found is None or not require_available:
        return found

    with db_transaction():
        create_temporary_tag_if_necessary(found, temp_tag_expiration_sec)
    return found


def _lookup_manifest(repository_id, manifest_digest, allow_dead=False, allow_hidden=False):
    matching_registration = RepositoryManifestDigest.alias()
    matching_registration_exists = fn.EXISTS(
        matching_registration.select(matching_registration.id).where(
            matching_registration.repository == repository_id,
            matching_registration.manifest == Manifest.id,
            matching_registration.digest == manifest_digest,
        )
    )
    identity_condition = matching_registration_exists

    if manifest_digest.startswith("sha256:"):
        any_registration = RepositoryManifestDigest.alias()
        any_registration_exists = fn.EXISTS(
            any_registration.select(any_registration.id).where(
                any_registration.repository == repository_id,
                any_registration.manifest == Manifest.id,
            )
        )
        identity_condition = matching_registration_exists | (
            (Manifest.digest == manifest_digest) & ~any_registration_exists
        )

    query = Manifest.select().where(
        Manifest.repository == repository_id,
        identity_condition,
    )
    return _lookup_manifest_query(query, allow_dead=allow_dead, allow_hidden=allow_hidden)


def _lookup_manifest_query(query, allow_dead=False, allow_hidden=False):
    if allow_dead:
        try:
            return query.get()
        except Manifest.DoesNotExist:
            return None

    # Preserve the one-query common case for a manifest directly referenced by an alive tag.
    try:
        return filter_to_alive_tags(query.join(Tag), allow_hidden=allow_hidden).get()
    except Manifest.DoesNotExist:
        pass

    try:
        found = query.get()
    except Manifest.DoesNotExist:
        return None
    return found if _manifest_is_available(found, allow_hidden=allow_hidden) else None


def _manifest_is_available(manifest, allow_hidden=False):
    """Returns whether a manifest is reachable from an alive tag through the complete graph."""
    repository_id = manifest.repository_id
    pending = {manifest.id}
    visited = set()

    while pending:
        current = pending - visited
        if not current:
            return False
        visited.update(current)

        tag_query = Tag.select(Tag.id).where(
            Tag.repository == repository_id,
            Tag.manifest.in_(current),
        )
        if filter_to_alive_tags(tag_query, allow_hidden=allow_hidden).exists():
            return True

        pending = {
            relationship.manifest_id
            for relationship in ManifestChild.select(ManifestChild.manifest).where(
                ManifestChild.repository == repository_id,
                ManifestChild.child_manifest.in_(current),
            )
        }

    return False


def has_repository_manifest_registration(repository_id, manifest):
    return (
        RepositoryManifestDigest.select()
        .where(
            RepositoryManifestDigest.repository == repository_id,
            RepositoryManifestDigest.manifest == manifest,
        )
        .exists()
    )


def register_repository_manifest_digest(repository_id, manifest, manifest_digest):
    repository_pk = getattr(repository_id, "id", repository_id)
    if manifest.repository_id != repository_pk:
        raise ManifestDigestConflictException(manifest_digest)
    if manifest_digest.startswith("sha256:") and manifest.digest != manifest_digest:
        raise ManifestDigestConflictException(manifest_digest)

    existing = RepositoryManifestDigest.get_or_none(
        RepositoryManifestDigest.repository == repository_id,
        RepositoryManifestDigest.digest == manifest_digest,
    )
    if existing is not None:
        if existing.manifest_id == manifest.id:
            return existing
        raise ManifestDigestConflictException(manifest_digest)

    try:
        # A nested atomic block is a real savepoint. A concurrent idempotent uniqueness conflict
        # must not roll back graph or tag work in the caller's transaction.
        with db.atomic():
            return RepositoryManifestDigest.create(
                repository=repository_id,
                manifest=manifest,
                digest=manifest_digest,
            )
    except IntegrityError:
        existing = RepositoryManifestDigest.get_or_none(
            RepositoryManifestDigest.repository == repository_id,
            RepositoryManifestDigest.digest == manifest_digest,
        )
        if existing is not None and existing.manifest_id == manifest.id:
            return existing
        raise ManifestDigestConflictException(manifest_digest)


def get_repository_manifest_digests(repository_id, manifest):
    manifest_id = getattr(manifest, "id", manifest)
    registrations = list(
        RepositoryManifestDigest.select()
        .where(
            RepositoryManifestDigest.repository == repository_id,
            RepositoryManifestDigest.manifest == manifest_id,
        )
        .order_by(RepositoryManifestDigest.id)
    )
    if not registrations:
        return [manifest.digest]
    return [registration.digest for registration in registrations]


def get_repository_manifest_digest(repository_id, manifest, allowed_algorithms=None):
    """Returns a deterministic enabled repository-visible identity.

    When ``allowed_algorithms`` is omitted, this preserves the existing selection behavior. When
    supplied, registrations using disabled algorithms are excluded. Canonical SHA-256 remains
    preferred only when it is both repository-visible and enabled.
    """
    digests = get_repository_manifest_digests(repository_id, manifest)
    if allowed_algorithms is not None:
        allowed = set(allowed_algorithms)
        digests = [digest for digest in digests if digest.partition(":")[0] in allowed]
    if not digests:
        return None
    if manifest.digest in digests:
        return manifest.digest
    return digests[0]


def get_repository_manifest_scanner_digest(repository_id, manifest):
    """Returns the stable repository-visible identity used as the external scanner report key."""
    manifest_row = manifest if hasattr(manifest, "digest") else Manifest.get_by_id(manifest)
    registrations = get_repository_manifest_digests(repository_id, manifest_row)
    return registrations[0] if registrations else manifest_row.digest


def resolve_repository_manifest_descriptor(
    repository_id,
    digest,
    size=None,
    media_type=None,
    unknown_exception=ManifestChildUnknownException,
):
    manifest = lookup_manifest(repository_id, digest, allow_dead=True, allow_hidden=True)
    if manifest is None:
        raise unknown_exception(digest)

    if size is not None and len(manifest.manifest_bytes.encode("utf-8")) != size:
        raise ManifestDescriptorMismatchException(digest, "size")
    if media_type is not None and manifest.media_type.name != media_type:
        raise ManifestDescriptorMismatchException(digest, "media type")
    return manifest


def _register_requested_manifest_digest(
    repository_id,
    manifest,
    requested_digest,
    preserve_legacy_canonical=False,
):
    if requested_digest is None:
        return

    if preserve_legacy_canonical and requested_digest != manifest.digest:
        register_repository_manifest_digest(repository_id, manifest, manifest.digest)
    register_repository_manifest_digest(repository_id, manifest, requested_digest)


def lookup_manifest_referrers(repository_id, manifest_digest, artifact_type=None):
    subject_manifest = lookup_manifest(
        repository_id,
        manifest_digest,
        allow_dead=True,
        allow_hidden=True,
    )
    canonical_subject_digest = (
        subject_manifest.digest if subject_manifest is not None else manifest_digest
    )
    query = (
        Manifest.select()
        .where(Manifest.repository == repository_id)
        .where(Manifest.subject == canonical_subject_digest)
    )
    if artifact_type is not None:
        query = query.where(Manifest.artifact_type == artifact_type)

    return query


@overload
def create_manifest(
    repository_id: int,
    manifest: ManifestInterface | ManifestListInterface,
    raise_on_error: Literal[True] = ...,
    canonical_subject_digest=...,
) -> Manifest: ...


@overload
def create_manifest(
    repository_id: int,
    manifest: ManifestInterface | ManifestListInterface,
    raise_on_error: Literal[False],
    canonical_subject_digest=...,
) -> Optional[Manifest]: ...


def create_manifest(
    repository_id: int,
    manifest: ManifestInterface | ManifestListInterface,
    raise_on_error: bool = True,
    canonical_subject_digest=_UNRESOLVED_SUBJECT,
) -> Optional[Manifest]:
    """
    Creates a manifest in the database.
    Does not handle sub manifests in a manifest list/index.
    Raises a _ManifestAlreadyExists exception if the manifest has already been created.
    """
    media_type = Manifest.media_type.get_id(manifest.media_type)
    if canonical_subject_digest is _UNRESOLVED_SUBJECT:
        subject = manifest.subject
        if isinstance(subject, dict):
            canonical_subject_digest = subject.get("digest")
        else:
            canonical_subject_digest = subject.digest if subject else None
    created_manifest = None
    try:
        created_manifest = Manifest.create(
            repository=repository_id,
            digest=manifest.digest,
            media_type=media_type,
            manifest_bytes=manifest.bytes.as_encoded_str(),  # TODO(kleesc): Remove once fully on JSONB only
            config_media_type=manifest.config_media_type,
            layers_compressed_size=manifest.layers_compressed_size,
            subject_backfilled=True,  # TODO(kleesc): Remove once backfill is done
            subject=canonical_subject_digest,
            artifact_type_backfilled=True,  # TODO(kleesc): Remove once backfill is done
            artifact_type=(
                manifest.artifact_type if manifest.artifact_type else None
            ),  # TODO(kleesc): Remove once fully on JSONB only
        )
    except IntegrityError as e:
        # NOTE: An IntegrityError means (barring a bug) that the manifest was created by
        # another caller while we were attempting to create it. Since we need to return
        # the manifest, we raise a specialized exception here to break out of the
        # transaction so we can retrieve it.
        if raise_on_error:
            raise _ManifestAlreadyExists(e)

    return created_manifest


def connect_manifests(manifests: list[Manifest], parent: Manifest, repository_id: int):
    """
    Connects manifests to a manifest list.
    Raises a _ManifestAlreadyExists if any of the manifest children already exist.
    """

    # proxy code will send us a raw list of children, if we have multiple identical entries in the list,
    # the insert will fail with a unique constraint violation.
    # we need to make sure that duplicates are removed from the list

    children = []
    deduped_list = set()
    for manifest in manifests:
        if manifest.id not in deduped_list:
            deduped_list.add(manifest.id)
            children.append(
                dict(manifest=parent, child_manifest=manifest, repository=repository_id)
            )
    if not children:
        return

    try:
        ManifestChild.insert_many(children).execute()
    except IntegrityError as e:
        raise _ManifestAlreadyExists(e)


def is_child_manifest(manifest_id: int):
    return ManifestChild.select().where(ManifestChild.child_manifest == manifest_id).exists()


def connect_blobs(manifest: ManifestInterface, blob_ids: set[int], repository_id: int):
    manifest_blobs = [
        dict(manifest=manifest, repository=repository_id, blob=blob_id) for blob_id in blob_ids
    ]
    try:
        ManifestBlob.insert_many(manifest_blobs).execute()
    except IntegrityError as e:
        raise _ManifestAlreadyExists(e)


def get_or_create_manifest(
    repository_id,
    manifest_interface_instance,
    storage,
    temp_tag_expiration_sec=TEMP_TAG_EXPIRATION_SEC,
    for_tagging=False,
    raise_on_error=False,
    retriever=None,
    requested_digest=None,
    prevalidated=False,
    prevalidated_labels=None,
    prevalidated_child_labels=None,
):
    """
    Returns a CreatedManifest for the manifest in the specified repository with the matching digest
    (if it already exists) or, if not yet created, creates and returns the manifest.

    Returns None if there was an error creating the manifest, unless raise_on_error is specified,
    in which case a CreateManifestException exception will be raised instead to provide more
    context to the error.

    Note that *all* blobs referenced by the manifest must exist already in the repository or this
    method will fail with a None.
    """
    canonical_digest = manifest_interface_instance.digest
    legacy_visible = lookup_canonical_manifest(
        repository_id,
        canonical_digest,
        allow_hidden=True,
    )
    preserve_legacy_canonical = (
        requested_digest is not None
        and requested_digest != canonical_digest
        and legacy_visible is not None
        and not has_repository_manifest_registration(repository_id, legacy_visible)
    )

    existing = lookup_canonical_manifest(
        repository_id,
        canonical_digest,
        allow_dead=True,
        require_available=True,
        temp_tag_expiration_sec=temp_tag_expiration_sec,
    )
    if (
        existing is not None
        and not prevalidated
        and not manifest_interface_instance.is_manifest_list
        and manifest_interface_instance.subject is None
    ):
        with db_transaction():
            _register_requested_manifest_digest(
                repository_id,
                existing,
                requested_digest,
                preserve_legacy_canonical=preserve_legacy_canonical,
            )
        return CreatedManifest(manifest=existing, newly_created=False, labels_to_apply=None)

    return _create_manifest(
        repository_id,
        manifest_interface_instance,
        storage,
        temp_tag_expiration_sec,
        for_tagging=for_tagging,
        raise_on_error=raise_on_error,
        retriever=retriever,
        requested_digest=requested_digest,
        preserve_legacy_canonical=preserve_legacy_canonical,
        prevalidated=prevalidated,
        prevalidated_labels=prevalidated_labels,
        prevalidated_child_labels=prevalidated_child_labels,
    )


def _create_manifest(
    repository_id,
    manifest_interface_instance,
    storage,
    temp_tag_expiration_sec=TEMP_TAG_EXPIRATION_SEC,
    for_tagging=False,
    raise_on_error=False,
    retriever=None,
    requested_digest=None,
    preserve_legacy_canonical=False,
    prevalidated=False,
    prevalidated_labels=None,
    prevalidated_child_labels=None,
):
    retriever = retriever or RepositoryContentRetriever.for_repository(repository_id, storage)
    canonical_subject = resolve_manifest_subject(repository_id, manifest_interface_instance)

    # Resolve every local descriptor through this repository before parsing config content or
    # writing graph rows. This produces precise unknown-blob failures and enforces isolation.
    blob_map = _build_blob_map(
        repository_id,
        manifest_interface_instance,
        retriever,
        storage,
        raise_on_error,
        require_empty_layer=False,
    )
    if blob_map is None:
        return None

    resolved_children = _resolve_manifest_children(repository_id, manifest_interface_instance)

    if not prevalidated:
        # Validate the manifest. Coordinated proxy ingestion performs this phase before its final
        # transaction and passes prevalidated=True to prevent storage reads under that transaction.
        try:
            manifest_interface_instance.validate(retriever)
        except (ManifestException, MalformedSchema2ManifestList, BlobDoesNotExist, IOError) as ex:
            logger.exception("Could not validate manifest `%s`", manifest_interface_instance.digest)
            if raise_on_error:
                raise CreateManifestException(str(ex))

            return None

    # Load, parse and get/create child manifests for ordinary pushes. A prevalidated coordinated
    # graph has already fetched and validated every child, so use the repository-scoped descriptor
    # rows resolved above without reading child content again.
    child_manifest_rows = {}
    child_manifest_label_dicts = []
    if prevalidated:
        child_manifest_rows = {child.id: child for child in resolved_children}
        child_manifest_label_dicts = prevalidated_child_labels or []
    else:
        child_manifest_refs = manifest_interface_instance.child_manifests(retriever)
        if child_manifest_refs is not None:
            for child_manifest_ref in child_manifest_refs:
                # Load and parse the child manifest.
                try:
                    child_manifest = child_manifest_ref.manifest_obj
                except (
                    ManifestException,
                    MalformedSchema2ManifestList,
                    BlobDoesNotExist,
                    IOError,
                ) as ex:
                    logger.exception(
                        "Could not load manifest list for manifest `%s`",
                        manifest_interface_instance.digest,
                    )
                    if raise_on_error:
                        raise CreateManifestException(str(ex))

                    return None

                # Skip manifests that were not loaded (e.g., due to sparse index configuration).
                if child_manifest is None:
                    continue

                # Retrieve its labels.
                labels = child_manifest.get_manifest_labels(retriever)
                if labels is None and not isinstance(child_manifest, ManifestListInterface):
                    if raise_on_error:
                        raise CreateManifestException("Unable to retrieve manifest labels")

                    logger.exception("Could not load manifest labels for child manifest")
                    return None

                # Get/create the child manifest in the database.
                child_manifest_info = get_or_create_manifest(
                    repository_id, child_manifest, storage, raise_on_error=raise_on_error
                )
                if child_manifest_info is None:
                    if raise_on_error:
                        raise CreateManifestException("Unable to retrieve child manifest")

                    logger.error("Could not get/create child manifest")
                    return None

                child_manifest_rows[child_manifest_info.manifest.digest] = (
                    child_manifest_info.manifest
                )
                child_manifest_label_dicts.append(labels or {})

    # Create the manifest and its blobs.
    storage_ids = {storage.id for storage in list(blob_map.values())}

    # Get the storage sizes
    blob_sizes = {}
    if features.QUOTA_MANAGEMENT:
        for storage in list(blob_map.values()):
            blob_sizes[storage.id] = storage.image_size

    # Check for the manifest, in case it was created since we checked earlier.
    try:
        manifest = Manifest.get(repository=repository_id, digest=manifest_interface_instance.digest)
        with db_transaction():
            _ensure_existing_manifest_graph(
                repository_id,
                manifest,
                storage_ids,
                child_manifest_rows.values(),
                canonical_subject,
            )
            create_temporary_tag_if_necessary(manifest, temp_tag_expiration_sec)
            _register_requested_manifest_digest(
                repository_id,
                manifest,
                requested_digest,
                preserve_legacy_canonical=preserve_legacy_canonical,
            )
        return CreatedManifest(manifest=manifest, newly_created=False, labels_to_apply=None)
    except Manifest.DoesNotExist:
        pass

    try:
        with db_transaction():
            manifest = create_manifest(
                repository_id,
                manifest_interface_instance,
                canonical_subject_digest=(canonical_subject.digest if canonical_subject else None),
            )

            if storage_ids:
                connect_blobs(manifest, storage_ids, repository_id)

            # Add blob sizes if quota management is enabled
            update_quota(repository_id, manifest.id, blob_sizes, QuotaOperation.ADD)

            if child_manifest_rows:
                connect_manifests(child_manifest_rows.values(), manifest, repository_id)

            ManifestSecurityStatus.create(
                manifest=manifest.id,
                repository=repository_id,
                index_status=IndexStatus.PENDING,
                indexer_hash="",
                indexer_version=IndexerVersion.V4,
                error_json={},
                metadata_json={},
            )

            # If this manifest is being created not for immediate tagging, add a temporary tag to the
            # manifest to ensure it isn't being GCed. If the manifest *is* for tagging, then since we're
            # creating a new one here, it cannot be GCed (since it isn't referenced by anything yet), so
            # its safe to elide the temp tag operation. If we ever change GC code to collect *all* manifests
            # in a repository for GC, then we will have to reevaluate this optimization at that time.
            if not for_tagging:
                create_temporary_tag_if_necessary(
                    manifest,
                    temp_tag_expiration_sec,
                    skip_expiration=manifest_interface_instance.subject is not None,
                )

            # The external identity becomes visible only after validation and all graph rows have
            # been persisted successfully in this transaction.
            _register_requested_manifest_digest(repository_id, manifest, requested_digest)

        # Define the labels for the manifest (if any). Coordinated ingestion supplies labels that
        # were read and parsed before its final transaction.
        # TODO: Once the old data model is gone, turn this into a batch operation and make the label
        # application to the manifest occur under the transaction.
        labels = (
            prevalidated_labels
            if prevalidated
            else manifest_interface_instance.get_manifest_labels(retriever)
        )
        if labels:
            for key, value in labels.items():
                # NOTE: There can technically be empty label keys via Dockerfile's. We ignore any
                # such `labels`, as they don't really mean anything.
                if not key:
                    continue

                media_type = "application/json" if is_json(value) else "text/plain"
                create_manifest_label(manifest, key, value, "manifest", media_type)

        # Return the dictionary of labels to apply (i.e. those labels that cause an action to be taken
        # on the manifest or its resulting tags). We only return those labels either defined on
        # the manifest or shared amongst all the child manifests. We intersect amongst all child manifests
        # to ensure that any action performed is defined in all manifests.
        labels_to_apply = labels or {}
        if child_manifest_label_dicts:
            labels_to_apply = child_manifest_label_dicts[0].items()
            for child_manifest_label_dict in child_manifest_label_dicts[1:]:
                # Intersect the key+values of the labels to ensure we get the exact same result
                # for all the child manifests.
                labels_to_apply = labels_to_apply & child_manifest_label_dict.items()

            labels_to_apply = dict(labels_to_apply)

        return CreatedManifest(
            manifest=manifest, newly_created=True, labels_to_apply=labels_to_apply
        )
    except _ManifestAlreadyExists as mae:
        try:
            manifest = Manifest.get(
                repository=repository_id, digest=manifest_interface_instance.digest
            )
        except Manifest.DoesNotExist:
            # NOTE: If we've reached this point, then somehow we had an IntegrityError without it
            # being due to a duplicate manifest. We therefore log the error.
            logger.error(
                "Got integrity error when trying to create manifest: %s", mae.internal_exception
            )
            if raise_on_error:
                raise CreateManifestException(
                    "Attempt to create an invalid manifest. Please report this issue."
                )

            return None

        with db_transaction():
            _ensure_existing_manifest_graph(
                repository_id,
                manifest,
                storage_ids,
                child_manifest_rows.values(),
                canonical_subject,
            )
            _register_requested_manifest_digest(
                repository_id,
                manifest,
                requested_digest,
                preserve_legacy_canonical=preserve_legacy_canonical,
            )
        return CreatedManifest(manifest=manifest, newly_created=False, labels_to_apply=None)


def _resolve_manifest_children(repository_id, manifest_interface_instance):
    if not manifest_interface_instance.is_manifest_list:
        return []

    resolved = []
    for descriptor in manifest_interface_instance.manifest_dict.get("manifests", []):
        resolved.append(
            resolve_repository_manifest_descriptor(
                repository_id,
                descriptor.get("digest"),
                size=descriptor.get("size"),
                media_type=descriptor.get("mediaType"),
                unknown_exception=ManifestChildUnknownException,
            )
        )
    return resolved


def resolve_manifest_subject(repository_id, manifest_interface_instance):
    subject = manifest_interface_instance.subject
    if subject is None:
        return None
    if isinstance(subject, dict):
        digest = subject.get("digest")
        size = subject.get("size")
        media_type = subject.get("mediaType")
    else:
        digest = subject.digest
        size = subject.size
        media_type = subject.mediatype
    return resolve_repository_manifest_descriptor(
        repository_id,
        digest,
        size=size,
        media_type=media_type,
        unknown_exception=ManifestSubjectUnknownException,
    )


def _ensure_existing_manifest_graph(
    repository_id,
    manifest,
    storage_ids,
    child_manifests,
    canonical_subject,
):
    existing_blob_ids = {
        relationship.blob_id
        for relationship in ManifestBlob.select(ManifestBlob.blob).where(
            ManifestBlob.repository == repository_id,
            ManifestBlob.manifest == manifest,
        )
    }
    missing_blob_ids = set(storage_ids) - existing_blob_ids
    if missing_blob_ids:
        connect_blobs(manifest, missing_blob_ids, repository_id)

    existing_child_ids = {
        relationship.child_manifest_id
        for relationship in ManifestChild.select(ManifestChild.child_manifest).where(
            ManifestChild.repository == repository_id,
            ManifestChild.manifest == manifest,
        )
    }
    missing_children = [child for child in child_manifests if child.id not in existing_child_ids]
    if missing_children:
        connect_manifests(missing_children, manifest, repository_id)

    canonical_subject_digest = canonical_subject.digest if canonical_subject else None
    if manifest.subject != canonical_subject_digest:
        Manifest.update(subject=canonical_subject_digest, subject_backfilled=True).where(
            Manifest.id == manifest.id,
            Manifest.repository == repository_id,
        ).execute()
        manifest.subject = canonical_subject_digest


def _build_blob_map(
    repository_id,
    manifest_interface_instance,
    retriever,
    storage,
    raise_on_error=False,
    require_empty_layer=True,
):
    """Builds a map containing the digest of each blob referenced by the given manifest,
    to its associated Blob row in the database. This method also verifies that the blob
    is accessible under the given repository. Returns None on error (unless raise_on_error
    is specified). If require_empty_layer is set to True, the method will check if the manifest
    references the special shared empty layer blob and, if so, add it to the map. Otherwise,
    the empty layer blob is only returned if it was *explicitly* referenced in the manifest.
    This is necessary because Docker V2_2/OCI manifests can implicitly reference an empty blob
    layer for image layers that only change metadata.
    """

    # Ensure all the blobs in the manifest exist.
    digests = set(manifest_interface_instance.local_blob_digests)
    blob_map = {}

    # If the special empty layer is required, simply load it directly. This is much faster
    # than trying to load it on a per repository basis, and that is unnecessary anyway since
    # this layer is predefined.
    if EMPTY_LAYER_BLOB_DIGEST in digests:
        digests.remove(EMPTY_LAYER_BLOB_DIGEST)
        blob_map[EMPTY_LAYER_BLOB_DIGEST] = get_shared_blob(EMPTY_LAYER_BLOB_DIGEST)
        if not blob_map[EMPTY_LAYER_BLOB_DIGEST]:
            if raise_on_error:
                raise CreateManifestException("Unable to retrieve specialized empty blob")

            logger.warning("Could not find the special empty blob in storage")
            return None

    for digest_str in digests:
        blob = get_repository_blob_by_digest(repository_id, digest_str)
        if blob is not None:
            blob_map[digest_str] = blob
            continue

        logger.warning(
            "Unknown blob `%s` under manifest `%s` for repository `%s`",
            digest_str,
            manifest_interface_instance.digest,
            repository_id,
        )
        if raise_on_error:
            raise ManifestBlobUnknownException(digest_str)
        return None

    # Special check: If the empty layer blob is needed for this manifest, add it to the
    # blob map. This is necessary because Docker decided to elide sending of this special
    # empty layer in schema version 2, but we need to have it referenced for schema version 1.
    if require_empty_layer and EMPTY_LAYER_BLOB_DIGEST not in blob_map:
        try:
            requires_empty_layer = manifest_interface_instance.get_requires_empty_layer_blob(
                retriever
            )
        except ManifestException as ex:
            if raise_on_error:
                raise CreateManifestException(str(ex))

            return None

        if requires_empty_layer is None:
            if raise_on_error:
                raise CreateManifestException("Could not load configuration blob")

            return None

        if requires_empty_layer:
            shared_blob = get_or_create_shared_blob(
                EMPTY_LAYER_BLOB_DIGEST, EMPTY_LAYER_BYTES, storage
            )
            assert shared_blob.content_checksum == EMPTY_LAYER_BLOB_DIGEST
            blob_map[EMPTY_LAYER_BLOB_DIGEST] = shared_blob

    return blob_map
