import logging
import time
from collections import namedtuple
from contextlib import contextmanager

import bitmath
from prometheus_client import Counter, Histogram

from data import model
from data.database import CloseForLongOperation
from data.model import BlobStorageMissingException
from data.registry_model import registry_model
from digest import digest_tools
from util.locking import GlobalLock, LockNotAcquiredException
from util.metrics.prometheus import blob_integrity_failures
from util.registry.filelike import StreamSlice, wrap_with_handler
from util.registry.gzipstream import calculate_size_handler

logger = logging.getLogger(__name__)


chunk_upload_duration = Histogram(
    "quay_chunk_upload_duration_seconds",
    "number of seconds for a chunk to be uploaded to the registry",
    labelnames=["region"],
)
pushed_bytes_total = Counter(
    "quay_registry_image_pushed_bytes_total", "number of bytes pushed to the registry"
)


BLOB_CONTENT_TYPE = "application/octet-stream"


class BlobUploadException(Exception):
    """
    Base for all exceptions raised when uploading blobs.
    """


class BlobRangeMismatchException(BlobUploadException):
    """
    Exception raised if the range to be uploaded does not match.
    """


class BlobDigestMismatchException(BlobUploadException):
    """
    Exception raised if the digest requested does not match that of the contents uploaded.
    """


class RequestedDigestMismatchException(BlobUploadException):
    """
    Raised if the uploaded bytes do not match a requested SHA-384/512 digest (400 DIGEST_INVALID).
    SHA-256 mismatches keep raising BlobDigestMismatchException (400 BLOB_UPLOAD_INVALID).
    """


class BlobIntegrityException(BlobUploadException):
    """
    Raised if a reread's SHA-256 differs from the saved upload state, or stored-bytes verification
    fails before reusing an existing CAS object (500 UNKNOWN plus an alert). Nothing is published.
    """


class BlobTooLargeException(BlobUploadException):
    """
    Exception raised if the data uploaded exceeds the maximum_blob_size.
    """

    def __init__(self, uploaded, max_allowed):
        super(BlobTooLargeException, self).__init__()
        self.uploaded = uploaded
        self.max_allowed = max_allowed


BlobUploadSettings = namedtuple(
    "BlobUploadSettings",
    ["maximum_blob_size", "committed_blob_expiration"],
)


def create_blob_upload(
    repository_ref,
    storage,
    settings,
    extra_blob_stream_handlers=None,
    requested_digest_algorithm=None,
):
    """
    Creates a new blob upload in the specified repository and returns a manager for interacting with
    that upload.

    If `requested_digest_algorithm` is sha384 or sha512 (a `digest-algorithm` hint, or the
    algorithm of a monolithic upload's digest), the upload keeps a resumable hash state for it.

    Returns None if a new blob upload could not be started.
    """
    location_name = storage.preferred_locations[0]
    new_upload_uuid, upload_metadata = storage.initiate_chunked_upload(location_name)
    blob_upload = registry_model.create_blob_upload(
        repository_ref,
        new_upload_uuid,
        location_name,
        upload_metadata,
        requested_digest_algorithm=requested_digest_algorithm,
    )
    if blob_upload is None:
        return None

    return _BlobUploadManager(
        repository_ref, blob_upload, settings, storage, extra_blob_stream_handlers
    )


def retrieve_blob_upload_manager(repository_ref, blob_upload_id, storage, settings):
    """
    Retrieves the manager for an in-progress blob upload with the specified ID under the given
    repository or None if none.
    """
    blob_upload = registry_model.lookup_blob_upload(repository_ref, blob_upload_id)
    if blob_upload is None:
        return None

    return _BlobUploadManager(repository_ref, blob_upload, settings, storage)


@contextmanager
def complete_when_uploaded(blob_upload):
    """
    Wraps the given blob upload in a context manager that completes the upload when the context
    closes.
    """
    try:
        yield blob_upload
    except Exception as ex:
        logger.exception("Exception when uploading blob `%s`", blob_upload.blob_upload_id)
        raise ex
    finally:
        # Cancel the upload if something went wrong or it was not commit to a blob.
        if blob_upload.committed_blob is None:
            blob_upload.cancel_upload()


@contextmanager
def upload_blob(
    repository_ref,
    storage,
    settings,
    extra_blob_stream_handlers=None,
    requested_digest_algorithm=None,
):
    """
    Starts a new blob upload in the specified repository and yields a manager for interacting with
    that upload.

    When the context manager completes, the blob upload is deleted, whether committed to a blob or
    not. Yields None if a blob upload could not be started.
    """
    assert repository_ref is not None

    created = create_blob_upload(
        repository_ref,
        storage,
        settings,
        extra_blob_stream_handlers,
        requested_digest_algorithm=requested_digest_algorithm,
    )
    if not created:
        yield None
        return

    try:
        yield created
    except Exception as ex:
        logger.exception("Exception when uploading blob `%s`", created.blob_upload_id)
        raise ex
    finally:
        # Cancel the upload if something went wrong or it was not commit to a blob.
        if created.committed_blob is None:
            created.cancel_upload()


class _BlobUploadManager(object):
    """
    Defines a helper class for easily interacting with blob uploads in progress, including handling
    of database and storage calls.
    """

    def __init__(
        self, repository_ref, blob_upload, settings, storage, extra_blob_stream_handlers=None
    ):
        assert repository_ref is not None
        assert blob_upload is not None

        self.repository_ref = repository_ref
        self.blob_upload = blob_upload
        self.settings = settings
        self.storage = storage
        self.extra_blob_stream_handlers = extra_blob_stream_handlers
        self.committed_blob = None
        # Set once the chunked upload has been completed into (or discarded in favor of) the CAS
        # object; the chunked upload no longer exists after that.
        self._storage_finalized = False

    @property
    def blob_upload_id(self):
        """
        Returns the unique ID for the blob upload.
        """
        return self.blob_upload.upload_id

    def upload_chunk(self, app_config, input_fp, start_offset=0, length=-1):
        """
        Uploads a chunk of data found in the given input file-like interface. start_offset and
        length are optional and should match a range header if any was given.

        Returns the total number of bytes uploaded after this upload has completed. Raises a
        BlobUploadException if the upload failed.
        """
        assert start_offset is not None
        assert length is not None

        if start_offset > 0 and start_offset > self.blob_upload.byte_count:
            logger.error("start_offset provided greater than blob_upload.byte_count")
            raise BlobRangeMismatchException()

        # Ensure that we won't go over the allowed maximum size for blobs.
        max_blob_size = bitmath.parse_string_unsafe(self.settings.maximum_blob_size)
        uploaded = bitmath.Byte(length + start_offset)
        if length > -1 and uploaded > max_blob_size:
            raise BlobTooLargeException(uploaded=uploaded.bytes, max_allowed=max_blob_size.bytes)

        location_set = {self.blob_upload.location_name}
        upload_error = None
        with CloseForLongOperation(app_config):
            if start_offset > 0 and start_offset < self.blob_upload.byte_count:
                # Skip the bytes which were received on a previous push, which are already stored and
                # included in the sha calculation
                overlap_size = self.blob_upload.byte_count - start_offset
                input_fp = StreamSlice(input_fp, overlap_size)

                # Update our upload bounds to reflect the skipped portion of the overlap
                start_offset = self.blob_upload.byte_count
                length = max(length - overlap_size, 0)

            # We use this to escape early in case we have already processed all of the bytes the user
            # wants to upload.
            if length == 0:
                return self.blob_upload.byte_count

            input_fp = wrap_with_handler(input_fp, self.blob_upload.sha_state.update)

            # Path B: keep the hinted algorithm's resumable state covering exactly the bytes
            # received. If the saved state is unusable, stop maintaining it; the final PUT then
            # rereads the upload (Path C).
            requested_hasher = self._requested_hasher()
            if requested_hasher is not None:
                input_fp = wrap_with_handler(input_fp, requested_hasher.update)

            if self.extra_blob_stream_handlers:
                for handler in self.extra_blob_stream_handlers:
                    input_fp = wrap_with_handler(input_fp, handler)

            # If this is the first chunk and we're starting at the 0 offset, add a handler to gunzip the
            # stream so we can determine the uncompressed size. We'll throw out this data if another chunk
            # comes in, but in the common case the docker client only sends one chunk.
            size_info = None
            if start_offset == 0 and self.blob_upload.chunk_count == 0:
                size_info, fn = calculate_size_handler()
                input_fp = wrap_with_handler(input_fp, fn)

            start_time = time.time()
            length_written, new_metadata, upload_error = self.storage.stream_upload_chunk(
                location_set,
                self.blob_upload.upload_id,
                start_offset,
                length,
                input_fp,
                self.blob_upload.storage_metadata,
                content_type=BLOB_CONTENT_TYPE,
            )

            if upload_error is not None:
                logger.error("storage.stream_upload_chunk returned error %s", upload_error)
                raise BlobUploadException(upload_error)

            # Update the chunk upload time and push bytes metrics.
            chunk_upload_duration.labels(list(location_set)[0]).observe(time.time() - start_time)
            pushed_bytes_total.inc(length_written)
            logger.debug(
                f"Uploaded {length_written} bytes to blob {self.blob_upload.upload_id} "
                f"took {time.time() - start_time} seconds"
            )

        # Ensure we have not gone beyond the max layer size.
        new_blob_bytes = self.blob_upload.byte_count + length_written
        new_blob_size = bitmath.Byte(new_blob_bytes)
        if new_blob_size > max_blob_size:
            raise BlobTooLargeException(uploaded=new_blob_size, max_allowed=max_blob_size.bytes)

        # If we determined an uncompressed size and this is the first chunk, add it to the blob.
        # Otherwise, we clear the size from the blob as it was uploaded in multiple chunks.
        uncompressed_byte_count = self.blob_upload.uncompressed_byte_count
        if size_info is not None and self.blob_upload.chunk_count == 0 and size_info.is_valid:
            uncompressed_byte_count = size_info.uncompressed_size
        elif length_written > 0:
            # Otherwise, if we wrote some bytes and the above conditions were not met, then we don't
            # know the uncompressed size.
            uncompressed_byte_count = None

        self.blob_upload = registry_model.update_blob_upload(
            self.blob_upload,
            uncompressed_byte_count,
            new_metadata,
            new_blob_bytes,
            self.blob_upload.chunk_count + 1,
            self.blob_upload.sha_state,
            requested_digest_state=requested_hasher,
        )
        if self.blob_upload is None:
            raise BlobUploadException("Could not complete upload of chunk")

        return new_blob_bytes

    def cancel_upload(self):
        """
        Cancels the blob upload, deleting any data uploaded and removing the upload itself.
        """
        if self.blob_upload is None:
            return

        # Tell storage to cancel the chunked upload, deleting its contents, unless it was already
        # completed into (or discarded in favor of) the CAS object.
        if not self._storage_finalized:
            self.storage.cancel_chunked_upload(
                {self.blob_upload.location_name},
                self.blob_upload.upload_id,
                self.blob_upload.storage_metadata,
            )

        # Remove the blob upload record itself.
        registry_model.delete_blob_upload(self.blob_upload)

    def _requested_hasher(self):
        """
        Returns the hinted algorithm's restored hash state if it is usable, else None. State is
        usable only if it restored cleanly and its `name` equals requested_digest_algorithm.
        """
        algorithm = self.blob_upload.requested_digest_algorithm
        state = self.blob_upload.requested_digest_state
        if not algorithm or state is None:
            return None
        if getattr(state, "name", None) != algorithm:
            logger.warning(
                "Discarding hash state for upload %s: algorithm %s does not match %s",
                self.blob_upload.upload_id,
                getattr(state, "name", None),
                algorithm,
            )
            return None
        return state

    def commit_to_blob(self, app_config, expected_digest=None):
        """
        Commits the blob upload to a blob under the repository. The resulting blob will be marked to
        not be GCed for some period of time (as configured by `committed_blob_expiration`).

        If expected_digest is specified, the content digest of the data uploaded for the blob is
        compared to that given. A SHA-256 (or legacy) digest is checked against the saved SHA-256
        state and raises BlobDigestMismatchException on mismatch. A sha384/sha512 digest is the
        requested client-visible identity (PROJQUAY-10117): it is checked against the hinted
        state when that state is usable (Paths A and B), or by rereading the finalized bytes once
        (Path C), and raises RequestedDigestMismatchException on mismatch.

        Raises BlobIntegrityException (500) if stored or reread bytes fail verification,
        BlobStorageMissingException (503) if the CAS object vanished, and LockAcquireTimeout (503)
        if GC holds the blob's deletion lock and a storage row must be created.
        """
        expected_digest = str(expected_digest) if expected_digest is not None else None
        algorithm = digest_tools.digest_algorithm(expected_digest) if expected_digest else None
        requested_digest = (
            expected_digest if algorithm in (digest_tools.SHA384, digest_tools.SHA512) else None
        )

        computed_digest_str = digest_tools.sha256_digest_from_hashlib(self.blob_upload.sha_state)
        location_name = self.blob_upload.location_name
        final_blob_location = digest_tools.content_path(computed_digest_str)

        if requested_digest is None:
            if expected_digest is not None:
                self._validate_digest(expected_digest)

            storage_already_existed = self._finalize_blob_storage(app_config)
            if storage_already_existed:
                self._verify_stored_bytes(app_config, computed_digest_str, final_blob_location)
        else:
            requested_hasher = (
                self._requested_hasher()
                if self.blob_upload.requested_digest_algorithm == algorithm
                else None
            )

            if requested_hasher is not None:
                # Paths A and B: verify against the state covering exactly the bytes received.
                self._check_requested_digest(
                    digest_tools.digest_from_hasher(requested_hasher), requested_digest
                )
                storage_already_existed = self._finalize_blob_storage(app_config)
                if storage_already_existed:
                    self._verify_stored_bytes(app_config, computed_digest_str, final_blob_location)
            else:
                # Path C: finalize, then reread the CAS object once (approved deviation: the
                # storage engines cannot read an in-progress chunked upload).
                storage_already_existed = self._finalize_blob_storage(app_config)
                self._reread_and_verify(
                    app_config,
                    computed_digest_str,
                    final_blob_location,
                    algorithm,
                    requested_digest,
                    created_object=not storage_already_existed,
                )

        # Publish: storage row, placement, repository link, and BlobUpload deletion, in one
        # transaction committed under the BLOB_DELETE lock.
        blob = registry_model.commit_blob_upload(
            self.blob_upload,
            computed_digest_str,
            self.settings.committed_blob_expiration,
            requested_digest=requested_digest,
            recheck_storage=self.storage,
        )
        if blob is None:
            return None

        self.committed_blob = blob
        return blob

    def _check_requested_digest(self, computed, requested_digest):
        if computed != requested_digest:
            logger.error(
                "Digest mismatch for upload %s: Expected digest %s, found digest %s",
                self.blob_upload.upload_id,
                requested_digest,
                computed,
            )
            raise RequestedDigestMismatchException()

    def _integrity_failure(self, reason, message, *args):
        blob_integrity_failures.labels(reason).inc()
        logger.error("ALERT blob integrity failure (%s): " + message, reason, *args)
        raise BlobIntegrityException(reason)

    def _missing_object(self, app_config, computed_digest_str, final_blob_location):
        """
        The CAS object is missing. If no row has its SHA-256, GC deleted it: 503, client retries.
        Otherwise rows point at missing bytes: 500 plus an alert.
        """
        if not model.storage.checksum_has_storage_rows(computed_digest_str):
            raise BlobStorageMissingException(
                "CAS object %s is missing at %s" % (final_blob_location, self.blob_upload.location_name)
            )
        self._integrity_failure(
            "missing_object",
            "CAS object %s for %s is missing at %s but storage rows reference it",
            final_blob_location,
            computed_digest_str,
            self.blob_upload.location_name,
        )

    def _verify_stored_bytes(self, app_config, computed_digest_str, final_blob_location):
        """
        Tiered stored-bytes verification before reusing a CAS object that existed before this
        upload. Always: the object exists and its size equals the verified upload size. Only when
        no CAS row with this SHA-256 has a placement at this location: read the object once and
        recompute SHA-256. Existing records, placements and the stored object are never changed.
        """
        verify_stored_blob(
            self.storage,
            self.blob_upload.location_name,
            final_blob_location,
            computed_digest_str,
            self.blob_upload.byte_count,
            app_config,
            on_missing=lambda: self._missing_object(
                app_config, computed_digest_str, final_blob_location
            ),
            on_failure=self._integrity_failure,
        )

    def _reread_and_verify(
        self,
        app_config,
        computed_digest_str,
        final_blob_location,
        algorithm,
        requested_digest,
        created_object,
    ):
        """
        Path C: reread the finalized CAS object once, checking SHA-256 against the saved upload
        state and the requested digest against the client's value.
        """
        location_set = {self.blob_upload.location_name}
        sha256_hasher = digest_tools.new_hasher(digest_tools.SHA256)
        requested_hasher = digest_tools.new_hasher(algorithm)
        byte_count = 0

        with CloseForLongOperation(app_config):
            if not self.storage.exists(location_set, final_blob_location):
                missing = True
            else:
                missing = False
                for chunk in self.storage.stream_read(location_set, final_blob_location):
                    sha256_hasher.update(chunk)
                    requested_hasher.update(chunk)
                    byte_count += len(chunk)

        if missing:
            self._missing_object(app_config, computed_digest_str, final_blob_location)

        reread_sha256 = digest_tools.digest_from_hasher(sha256_hasher)
        if reread_sha256 != computed_digest_str or byte_count != self.blob_upload.byte_count:
            if created_object:
                self._remove_unreferenced_object(computed_digest_str, final_blob_location)
            self._integrity_failure(
                "reread_mismatch",
                "reread of upload %s at %s found %s (%s bytes), saved state is %s (%s bytes)",
                self.blob_upload.upload_id,
                final_blob_location,
                reread_sha256,
                byte_count,
                computed_digest_str,
                self.blob_upload.byte_count,
            )

        self._check_requested_digest(
            digest_tools.digest_from_hasher(requested_hasher), requested_digest
        )

    def _remove_unreferenced_object(self, computed_digest_str, final_blob_location):
        """
        Removes a CAS object this upload created whose bytes do not match its key, under the
        BLOB_DELETE lock and only if no storage row references its SHA-256.
        """
        location_set = {self.blob_upload.location_name}

        def _remove():
            if not model.storage.checksum_has_storage_rows(computed_digest_str):
                self.storage.remove(location_set, final_blob_location)

        try:
            if GlobalLock.lock_factory is None:
                _remove()
                return
            with GlobalLock(f"BLOB_DELETE_{computed_digest_str}", lock_ttl=30, blocking_timeout=3):
                _remove()
        except LockNotAcquiredException:
            logger.warning(
                "Could not acquire lock to remove mismatched object %s", final_blob_location
            )

    def _validate_digest(self, expected_digest):
        """
        Verifies that the digest's SHA matches that of the uploaded data.
        """
        try:
            computed_digest = digest_tools.sha256_digest_from_hashlib(self.blob_upload.sha_state)
            if not digest_tools.digests_equal(computed_digest, expected_digest):
                logger.error(
                    "Digest mismatch for upload %s: Expected digest %s, found digest %s",
                    self.blob_upload.upload_id,
                    expected_digest,
                    computed_digest,
                )
                raise BlobDigestMismatchException()
        except digest_tools.InvalidDigestException:
            raise BlobDigestMismatchException()

    def _finalize_blob_storage(self, app_config):
        """
        When an upload is successful, this ends the uploading process from the storage's
        perspective.

        Returns True if the blob already existed.
        """
        computed_digest = digest_tools.sha256_digest_from_hashlib(self.blob_upload.sha_state)
        final_blob_location = digest_tools.content_path(computed_digest)

        # Close the database connection before we perform this operation, as it can take a while
        # and we shouldn't hold the connection during that time.
        with CloseForLongOperation(app_config):
            # Move the storage into place, or if this was a re-upload, cancel it
            already_existed = self.storage.exists(
                {self.blob_upload.location_name}, final_blob_location
            )
            if already_existed:
                # It already existed, clean up our upload which served as proof that the
                # uploader had the blob.
                self.storage.cancel_chunked_upload(
                    {self.blob_upload.location_name},
                    self.blob_upload.upload_id,
                    self.blob_upload.storage_metadata,
                )
            else:
                # We were the first ones to upload this image (at least to this location)
                # Let's copy it into place
                start_time = time.time()
                self.storage.complete_chunked_upload(
                    {self.blob_upload.location_name},
                    self.blob_upload.upload_id,
                    final_blob_location,
                    self.blob_upload.storage_metadata,
                )
                logger.debug(
                    f"Completed chunked upload for blob "
                    f"{self.blob_upload.upload_id} with digest {computed_digest} "
                    f"took {time.time() - start_time} seconds"
                )

        self._storage_finalized = True
        return already_existed


def verify_stored_blob(
    storage,
    location_name,
    blob_path,
    content_checksum,
    expected_size,
    app_config,
    on_missing,
    on_failure,
):
    """
    Tiered stored-bytes verification of an existing CAS object (PROJQUAY-10117).

    - Always: the object exists and its size equals `expected_size`.
    - Only when no CAS row with `content_checksum` has a placement at `location_name`: read the
      object once and recompute SHA-256.

    Calls `on_missing()` if the object does not exist and `on_failure(reason, message, *args)`
    for any other failure; both are expected to raise.
    """
    location_set = {location_name}
    with CloseForLongOperation(app_config):
        actual_size = storage.get_size(location_set, blob_path)

    if actual_size is None:
        on_missing()
        return

    if expected_size is not None and actual_size != expected_size:
        on_failure(
            "size_mismatch",
            "stored object %s at %s has %s bytes, expected %s",
            blob_path,
            location_name,
            actual_size,
            expected_size,
        )
        return

    location = model.storage.get_image_location_for_name(location_name)
    if model.storage.checksum_has_cas_placement(content_checksum, location.id):
        return

    hasher = digest_tools.new_hasher(digest_tools.SHA256)
    with CloseForLongOperation(app_config):
        for chunk in storage.stream_read(location_set, blob_path):
            hasher.update(chunk)

    recomputed = digest_tools.digest_from_hasher(hasher)
    if recomputed != content_checksum:
        on_failure(
            "checksum_mismatch",
            "stored object %s at %s hashes to %s, expected %s",
            blob_path,
            location_name,
            recomputed,
            content_checksum,
        )
