from peewee import IntegrityError

from data.database import (
    ImageStorage,
    ManifestBlob,
    RepositoryBlobDigest,
    UploadedBlob,
    db,
    db_disallow_replica_use,
    db_transaction,
)
from data.model import BlobDigestConflictException, BlobDoesNotExist
from data.model.storage import InvalidImageException, get_storage_by_uuid


def get_repository_blob_by_digest(repository, blob_digest):
    """
    Find a complete placed blob linked to the specified repository, or return None if none.

    A repository-local placeholder is intentionally not complete and raises InvalidImageException
    through get_storage_by_uuid. Call lookup_repository_blob_by_digest when the caller needs to
    inspect or promote placeholders.
    """
    storage = lookup_repository_blob_by_digest(repository, blob_digest)
    return get_storage_by_uuid(storage.uuid) if storage is not None else None


def lookup_repository_blob_by_digest(repository, blob_digest):
    """Resolve repository-scoped blob identity without requiring a storage placement."""
    storage = _lookup_blob_by_registered_digest(repository, blob_digest)
    if storage is not None and _repository_references_storage(repository, storage):
        return storage

    # Preserve lookup for SHA-256 repository/blob pairs created before digest registrations
    # existed. Materialize that historical identity on the primary database so later alternative
    # registrations cannot hide it. Once any registration exists for the pair, only exact
    # registered digests resolve.
    if blob_digest.startswith("sha256:"):
        with db_disallow_replica_use(), db_transaction():
            storage = _lookup_blob_by_registered_digest(repository, blob_digest)
            if storage is not None and _repository_references_storage(repository, storage):
                return storage

            storage = _lookup_blob_uploaded(repository, blob_digest)
            if storage is None:
                storage = _lookup_blob_in_repository(repository, blob_digest)
            if storage is not None and not has_repository_blob_registration(repository, storage):
                register_repository_blob_digest(repository, storage, blob_digest)
                return storage

    return None


def _lookup_blob_by_registered_digest(repository, blob_digest):
    try:
        return (
            ImageStorage.select()
            .join(RepositoryBlobDigest)
            .where(
                RepositoryBlobDigest.repository == repository,
                RepositoryBlobDigest.digest == blob_digest,
            )
            .get()
        )
    except ImageStorage.DoesNotExist:
        return None


def _repository_references_storage(repository, storage):
    recently_uploaded = UploadedBlob.select().where(
        UploadedBlob.repository == repository,
        UploadedBlob.blob == storage,
    )
    if recently_uploaded.exists():
        return True

    return (
        ManifestBlob.select()
        .where(
            ManifestBlob.repository == repository,
            ManifestBlob.blob == storage,
        )
        .exists()
    )


def has_repository_blob_registration(repository, storage):
    return (
        RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == repository,
            RepositoryBlobDigest.image_storage == storage,
        )
        .exists()
    )


def register_repository_blob_digest(repository, storage, blob_digest):
    if blob_digest.startswith("sha256:") and storage.content_checksum != blob_digest:
        raise BlobDigestConflictException(blob_digest)

    existing = RepositoryBlobDigest.get_or_none(
        RepositoryBlobDigest.repository == repository,
        RepositoryBlobDigest.digest == blob_digest,
    )
    if existing is not None:
        if existing.image_storage_id == storage.id:
            return existing
        raise BlobDigestConflictException(blob_digest)

    try:
        # Use atomic rather than the configured transaction factory so a nested call creates a
        # savepoint. A concurrent unique-index conflict must not roll back the caller's link work.
        with db.atomic():
            return RepositoryBlobDigest.create(
                repository=repository,
                image_storage=storage,
                digest=blob_digest,
            )
    except IntegrityError:
        existing = RepositoryBlobDigest.get_or_none(
            RepositoryBlobDigest.repository == repository,
            RepositoryBlobDigest.digest == blob_digest,
        )
        if existing is not None and existing.image_storage_id == storage.id:
            return existing
        raise BlobDigestConflictException(blob_digest)


def _lookup_blob_uploaded(repository, blob_digest):
    try:
        return (
            ImageStorage.select()
            .join(UploadedBlob)
            .where(
                UploadedBlob.repository == repository,
                ImageStorage.content_checksum == blob_digest,
            )
            .get()
        )
    except ImageStorage.DoesNotExist:
        return None


def _lookup_blob_in_repository(repository, blob_digest):
    try:
        return (
            ImageStorage.select()
            .join(ManifestBlob)
            .where(
                ManifestBlob.repository == repository,
                ImageStorage.content_checksum == blob_digest,
            )
            .get()
        )
    except ImageStorage.DoesNotExist:
        return None
