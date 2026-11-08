from data.database import ImageStorage, ManifestBlob, UploadedBlob
from data.model import BlobDoesNotExist
from data.model.storage import (
    InvalidImageException,
    _is_placeholder,
    get_storage_by_uuid,
    identity_clause,
    repository_selection_order,
)


def get_repository_blob_by_digest(repository, blob_digest):
    """
    Find the content-addressable blob linked to the specified repository and returns it or None if
    none.

    Applies the identity rule: a SHA-384/512 digest matches requested_content_checksum and a
    SHA-256 digest matches only SHA-256 rows. Among this repository's linked matching rows
    (UploadedBlob or ManifestBlob), picks the lowest row ID with placeholders last.
    """
    storage = lookup_repository_blob_row(repository, blob_digest)
    return get_storage_by_uuid(storage.uuid) if storage is not None else None


def lookup_repository_blob_row(repository, blob_digest):
    """
    Returns the selected ImageStorage row (possibly a placeholder) linked to the repository whose
    identity is `blob_digest`, or None.
    """
    candidates = [
        candidate
        for candidate in (
            _lookup_blob_linked(repository, blob_digest, UploadedBlob),
            _lookup_blob_linked(repository, blob_digest, ManifestBlob),
        )
        if candidate is not None
    ]
    if not candidates:
        return None

    return min(candidates, key=lambda row: (_is_placeholder(row), row.id))


def _lookup_blob_linked(repository, blob_digest, link_model):
    try:
        return (
            ImageStorage.select(
                ImageStorage.id,
                ImageStorage.uuid,
                ImageStorage.content_checksum,
                ImageStorage.requested_content_checksum,
            )
            .join(link_model)
            .where(link_model.repository == repository, identity_clause(blob_digest))
            .order_by(*repository_selection_order())
            .limit(1)
            .get()
        )
    except ImageStorage.DoesNotExist:
        return None
