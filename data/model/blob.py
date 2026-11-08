import logging
from datetime import datetime, timedelta
from uuid import uuid4

from peewee import IntegrityError

from data.database import (
    BlobUpload,
    ImageStorage,
    ImageStorageLocation,
    ImageStoragePlacement,
    Namespace,
    Repository,
    RepositoryState,
    UploadedBlob,
    db,
    db_random_func,
)
from data.model import (
    BlobDoesNotExist,
    BlobStorageMissingException,
    InvalidBlobUpload,
    InvalidImageException,
    _basequery,
    db_transaction,
)
from data.model import storage as storage_model
from data.model.storage import (
    blob_lock_may_create,
    get_or_create_blob_with_lock,
    identity_clause,
    select_sha256_publication_row,
    with_blob_lock_or_fallback,
)

from util.locking import LockAcquireTimeout

logger = logging.getLogger(__name__)


def store_blob_record_and_temp_link(
    namespace,
    repo_name,
    blob_digest,
    location_obj,
    byte_count,
    link_expiration_s,
    uncompressed_byte_count=None,
):
    repo = _basequery.get_existing_repository(namespace, repo_name)
    assert repo

    return store_blob_record_and_temp_link_in_repo(
        repo.id, blob_digest, location_obj, byte_count, link_expiration_s, uncompressed_byte_count
    )


def _update_storage_sizes(storage, byte_count, uncompressed_byte_count):
    save_changes = False
    if storage.image_size is None:
        storage.image_size = byte_count
        save_changes = True

    if storage.uncompressed_size is None and uncompressed_byte_count is not None:
        storage.uncompressed_size = uncompressed_byte_count
        save_changes = True

    if save_changes:
        storage.save()


def _find_requested_row(blob_digest, requested_digest):
    return (
        ImageStorage.select()
        .where(
            ImageStorage.requested_content_checksum == requested_digest,
            ImageStorage.content_checksum == blob_digest,
        )
        .first()
    )


def _select_or_create_requested_row(
    blob_digest, requested_digest, byte_count, uncompressed_byte_count
):
    """
    Selects or creates the single row for (blob_digest, requested_digest). The partial unique
    index allows at most one; a publisher that loses a concurrent race reuses the winner's row.
    """
    storage = _find_requested_row(blob_digest, requested_digest)
    if storage is not None:
        _update_storage_sizes(storage, byte_count, uncompressed_byte_count)
        return storage

    if not blob_lock_may_create():
        raise LockAcquireTimeout(
            "BLOB_DELETE_%s is held by GC; not creating storage row for %s"
            % (blob_digest, requested_digest)
        )

    try:
        with db.atomic():
            return ImageStorage.create(
                content_checksum=blob_digest,
                requested_content_checksum=requested_digest,
                image_size=byte_count,
                uncompressed_size=uncompressed_byte_count,
            )
    except IntegrityError:
        logger.info("Lost publication race for %s; reusing the winning row", requested_digest)
        storage = _find_requested_row(blob_digest, requested_digest)
        if storage is None:
            raise
        return storage


def _publish_blob(
    repository_id,
    blob_digest,
    location_obj,
    byte_count,
    link_expiration_s,
    uncompressed_byte_count=None,
    requested_digest=None,
    blob_upload_id=None,
    recheck_storage=True,
    skip_lock=False,
):
    """
    Publication transaction (PROJQUAY-10117, "Publication and GC"). Runs while the caller holds
    the BLOB_DELETE_<blob_digest> lock (or has fallen back without it), and commits before the
    lock is released. The only object-storage I/O is the existence re-check in step 2.

    1. Select or create the storage row under the identity rule and selection order.
    2. If this is the first row for the SHA-256, re-check that the CAS object still exists.
    3. Get or create the placement.
    4. Insert UploadedBlob, linking this exact row.
    5. Delete the BlobUpload, if any.
    """
    location_id = getattr(location_obj, "id", location_obj)

    with db_transaction():
        # Content-checksum-only check: is any row (of any requested digest) holding this CAS key?
        first_row = not storage_model.checksum_has_storage_rows(blob_digest)

        if requested_digest is None:
            try:
                storage = select_sha256_publication_row(blob_digest, location_id)
                _update_storage_sizes(storage, byte_count, uncompressed_byte_count)
            except ImageStorage.DoesNotExist:
                storage = get_or_create_blob_with_lock(
                    digest=blob_digest,
                    image_size=byte_count,
                    uncompressed_size=uncompressed_byte_count,
                    skip_lock=skip_lock,
                )
        else:
            storage = _select_or_create_requested_row(
                blob_digest, requested_digest, byte_count, uncompressed_byte_count
            )

        if first_row and recheck_storage and storage.cas_path:
            store = storage_model.config.store if recheck_storage is True else recheck_storage
            location_name = storage_model.get_image_location_for_id(location_id).name
            layer_path = storage_model.get_layer_path(storage)
            if not store.exists({location_name}, layer_path):
                logger.warning(
                    "CAS object for %s disappeared at %s before publication", blob_digest, location_name
                )
                raise BlobStorageMissingException(
                    "CAS object for %s is missing at %s" % (blob_digest, location_name)
                )

        try:
            ImageStoragePlacement.get(storage=storage, location=location_id)
        except ImageStoragePlacement.DoesNotExist:
            try:
                with db.atomic():
                    ImageStoragePlacement.create(storage=storage, location=location_id)
            except IntegrityError as e:
                logger.warning(
                    "Another worker already created placement for blob %s, skipping creation: %s",
                    blob_digest,
                    e,
                )

        _temp_link_blob(repository_id, storage, link_expiration_s)

        if blob_upload_id is not None:
            BlobUpload.delete().where(BlobUpload.id == blob_upload_id).execute()

        return storage


def publish_blob(
    repository_id,
    blob_digest,
    location_obj,
    byte_count,
    link_expiration_s,
    uncompressed_byte_count=None,
    requested_digest=None,
    blob_upload_id=None,
    recheck_storage=True,
):
    """
    Records a verified blob and temporarily links it to the repository.

    `blob_digest` is the internal SHA-256 (CAS key). `requested_digest` is the verified
    client-visible sha384/sha512 digest, or None for a SHA-256 identity. `recheck_storage` is the
    storage engine used for the step-2 existence re-check (True for the configured store), or
    False to skip it. The BLOB_DELETE lock,
    keyed by `blob_digest`, is held until the publication transaction commits. If GC still holds
    it after the acquire timeout and a storage row must be created, LockAcquireTimeout is raised
    (503). If the CAS object vanished before a first row was created, BlobStorageMissingException
    is raised (503).
    """
    assert blob_digest
    assert byte_count is not None
    assert requested_digest is None or storage_model.is_requested_identity(requested_digest)

    return with_blob_lock_or_fallback(
        blob_digest,
        _publish_blob,
        repository_id=repository_id,
        blob_digest=blob_digest,
        location_obj=location_obj,
        byte_count=byte_count,
        link_expiration_s=link_expiration_s,
        uncompressed_byte_count=uncompressed_byte_count,
        requested_digest=requested_digest,
        blob_upload_id=blob_upload_id,
        recheck_storage=recheck_storage,
    )


def store_blob_record_and_temp_link_in_repo(
    repository_id,
    blob_digest,
    location_obj,
    byte_count,
    link_expiration_s,
    uncompressed_byte_count=None,
):
    """
    Store a record of the blob and temporarily link it to the specified repository, as a SHA-256
    identity. Does not re-check storage; callers that finalize uploads use publish_blob.
    """
    return publish_blob(
        repository_id,
        blob_digest,
        location_obj,
        byte_count,
        link_expiration_s,
        uncompressed_byte_count=uncompressed_byte_count,
        recheck_storage=False,
    )


def temp_link_blob(repository_id, blob_digest, link_expiration_s):
    """
    Temporarily links to the blob record from the given repository.

    The record is resolved globally under the identity rule (lowest row ID). Prefer
    temp_link_blob_storage, which links an exact, already-resolved row.

    If the blob record is not found, return None.
    """
    assert blob_digest

    with db_transaction():
        storage = (
            ImageStorage.select()
            .where(identity_clause(blob_digest))
            .order_by(ImageStorage.id)
            .first()
        )
        if storage is None:
            return None

        _temp_link_blob(repository_id, storage, link_expiration_s)
        return storage


def temp_link_blob_storage(repository_id, storage_id, link_expiration_s):
    """
    Temporarily links the exact ImageStorage row `storage_id` to the given repository.

    Returns the storage row, or None if it does not exist or could not be linked.
    """
    with db_transaction():
        try:
            storage = ImageStorage.get(id=storage_id)
        except ImageStorage.DoesNotExist:
            return None

        if _temp_link_blob(repository_id, storage, link_expiration_s) is None:
            return None
        return storage


def _temp_link_blob(repository_id, storage, link_expiration_s):
    """Note: Should *always* be called by a parent under a transaction."""
    try:
        repository = Repository.get(id=repository_id)
    except Repository.DoesNotExist:
        return None

    if repository.state == RepositoryState.MARKED_FOR_DELETION:
        return None

    return UploadedBlob.create(
        repository=repository_id,
        blob=storage,
        expires_at=datetime.utcnow() + timedelta(seconds=link_expiration_s),
    )


def lookup_expired_uploaded_blobs(repository):
    """Looks up all expired uploaded blobs in a repository."""
    return UploadedBlob.select().where(
        UploadedBlob.repository == repository, UploadedBlob.expires_at <= datetime.utcnow()
    )


def get_stale_blob_upload(stale_timespan):
    """
    Returns a blob upload which was created before the stale timespan.
    """
    stale_threshold = datetime.now() - stale_timespan

    try:
        candidates = (
            BlobUpload.select(BlobUpload, ImageStorageLocation)
            .join(ImageStorageLocation)
            .where(BlobUpload.created <= stale_threshold)
        )

        return candidates.get()
    except BlobUpload.DoesNotExist:
        return None


def get_blob_upload_by_uuid(upload_uuid):
    """
    Loads the upload with the given UUID, if any.
    """
    try:
        return (
            BlobUpload.select(BlobUpload, ImageStorageLocation)
            .join(ImageStorageLocation)
            .where(BlobUpload.uuid == upload_uuid)
            .get()
        )
    except BlobUpload.DoesNotExist:
        return None


def initiate_upload(
    namespace, repo_name, uuid, location_name, storage_metadata, requested_digest_algorithm=None
):
    """
    Initiates a blob upload for the repository with the given namespace and name, in a specific
    location.
    """
    repo = _basequery.get_existing_repository(namespace, repo_name)
    return initiate_upload_for_repo(
        repo,
        uuid,
        location_name,
        storage_metadata,
        requested_digest_algorithm=requested_digest_algorithm,
    )


# Algorithms for which a blob upload keeps a resumable hash state besides SHA-256.
RESUMABLE_REQUESTED_DIGEST_ALGORITHMS = ("sha384", "sha512")


def initiate_upload_for_repo(
    repo, uuid, location_name, storage_metadata, requested_digest_algorithm=None
):
    """
    Initiates a blob upload for a specific repository object, in a specific location.

    If `requested_digest_algorithm` is sha384 or sha512, the upload also keeps a resumable hash
    state for that algorithm. A sha256 (or absent) hint stores NULL.
    """
    location = storage_model.get_image_location_for_name(location_name)
    requested_state = None
    if requested_digest_algorithm in RESUMABLE_REQUESTED_DIGEST_ALGORITHMS:
        import resumablehash

        requested_state = resumablehash.new(requested_digest_algorithm)
    else:
        requested_digest_algorithm = None

    return BlobUpload.create(
        repository=repo,
        location=location.id,
        uuid=uuid,
        storage_metadata=storage_metadata,
        requested_digest_algorithm=requested_digest_algorithm,
        requested_digest_state=requested_state,
    )


def get_shared_blob(digest):
    """
    Returns the ImageStorage blob with the given digest or, if not present, returns None.

    This method is *only* to be used for shared blobs that are globally accessible, such as the
    special empty gzipped tar layer that Docker no longer pushes to us. Only SHA-256 rows match.
    """
    assert digest
    return _get_shared_sha256_row(digest)


def _get_shared_sha256_row(digest):
    return (
        ImageStorage.select()
        .where(identity_clause(digest))
        .order_by(ImageStorage.id)
        .first()
    )


def get_or_create_shared_blob(digest, byte_data, storage):
    """
    Returns the ImageStorage blob with the given digest or, if not present, adds a row and writes
    the given byte data to the storage engine.

    This method is *only* to be used for shared blobs that are globally accessible, such as the
    special empty gzipped tar layer that Docker no longer pushes to us.
    """
    assert digest
    assert byte_data is not None and isinstance(byte_data, bytes)
    assert storage

    existing = _get_shared_sha256_row(digest)
    if existing is not None:
        return existing
    else:
        preferred = storage.preferred_locations[0]
        location_obj = ImageStorageLocation.get(name=preferred)

        record = get_or_create_blob_with_lock(digest=digest, image_size=len(byte_data))

        try:
            storage.put_content([preferred], storage_model.get_layer_path(record), byte_data)
            ImageStoragePlacement.create(storage=record, location=location_obj)
        except IntegrityError as e:
            logger.warning("Exception when trying to write special layer %s: %s", digest, e)

        return record
