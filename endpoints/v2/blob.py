import logging
import re

from flask import Response
from flask import abort as flask_abort
from flask import redirect, request, url_for

import features
from app import app, get_app_url, model_cache, storage, usermanager
from auth.auth_context import get_authenticated_context, get_authenticated_user
from auth.permissions import (
    GlobalReadOnlySuperUserPermission,
    ModifyRepositoryPermission,
    ReadRepositoryPermission,
    SuperUserPermission,
)
from auth.registry_jwt_auth import process_registry_jwt_auth
from data import database
from data.model import BlobDigestConflictException, namespacequota
from data.registry_model import registry_model
from data.registry_model.blobuploader import (
    BlobDigestMismatchException,
    BlobRangeMismatchException,
    BlobTooLargeException,
    BlobUploadException,
    BlobUploadInvalidStateException,
    BlobUploadSettings,
    complete_when_uploaded,
    create_blob_upload,
    retrieve_blob_upload_manager,
)
from digest import digest_tools
from endpoints.api import log_unauthorized
from endpoints.decorators import (
    anon_allowed,
    anon_protect,
    check_pushes_disabled,
    check_readonly,
    disallow_for_account_recovery_mode,
    inject_registry_model,
    parse_repository_name,
)
from endpoints.metrics import image_pulled_bytes
from endpoints.v2 import (
    get_input_stream,
    is_mirror_repository,
    require_repo_read,
    require_repo_write,
    v2_bp,
    validate_mirror_digest_algorithm,
)
from endpoints.v2.errors import (
    BlobUnknown,
    BlobUploadInvalid,
    BlobUploadUnknown,
    DigestDisabled,
    DigestInvalid,
    DigestUnsupported,
    InvalidRequest,
    LayerTooLarge,
    NameUnknown,
    QuotaExceeded,
    Unsupported,
)
from util.cache import cache_control
from util.names import parse_namespace_repository
from util.request import get_request_ip

logger = logging.getLogger(__name__)

BASE_BLOB_ROUTE = '/<repopath:repository>/blobs/<regex("{0}"):digest>'
BLOB_DIGEST_ROUTE = BASE_BLOB_ROUTE.format(digest_tools.DIGEST_PATTERN)
RANGE_HEADER_REGEX = re.compile(r"^([0-9]+)-([0-9]+)$")
BLOB_CONTENT_TYPE = "application/octet-stream"


@v2_bp.route(BLOB_DIGEST_ROUTE, methods=["HEAD"])
@disallow_for_account_recovery_mode
@parse_repository_name()
@process_registry_jwt_auth(scopes=["pull"])
@require_repo_read(allow_for_superuser=True, allow_for_global_readonly_superuser=True)
@anon_allowed
@cache_control(max_age=31436000)
@inject_registry_model()
def check_blob_exists(namespace_name, repo_name, digest, registry_model):
    parsed_digest = _parse_digest(digest)
    _validate_digest_algorithm(parsed_digest.hash_alg)
    digest = str(parsed_digest)

    # Find the blob.
    blob = registry_model.get_cached_repo_blob(model_cache, namespace_name, repo_name, digest)
    if blob is None:
        raise BlobUnknown()

    # Build the response headers.
    headers = {
        "Docker-Content-Digest": digest,
        "Content-Length": blob.compressed_size,
        "Content-Type": BLOB_CONTENT_TYPE,
    }

    # If our storage supports range requests, let the client know.
    if storage.get_supports_resumable_downloads(blob.placements):
        headers["Accept-Ranges"] = "bytes"

    # Write the response to the client.
    return Response(headers=headers)


@v2_bp.route(BLOB_DIGEST_ROUTE, methods=["GET"])
@disallow_for_account_recovery_mode
@parse_repository_name()
@process_registry_jwt_auth(scopes=["pull"])
@require_repo_read(allow_for_superuser=True, allow_for_global_readonly_superuser=True)
@anon_allowed
@cache_control(max_age=31536000)
@inject_registry_model()
def download_blob(namespace_name, repo_name, digest, registry_model):
    parsed_digest = _parse_digest(digest)
    _validate_digest_algorithm(parsed_digest.hash_alg)
    digest = str(parsed_digest)

    # Find the blob.
    blob = registry_model.get_cached_repo_blob(model_cache, namespace_name, repo_name, digest)
    if blob is None:
        raise BlobUnknown()

    # Build the response headers.
    headers = {"Docker-Content-Digest": digest}

    # If our storage supports range requests, let the client know.
    if storage.get_supports_resumable_downloads(blob.placements):
        headers["Accept-Ranges"] = "bytes"

    image_pulled_bytes.labels("v2").inc(blob.compressed_size)

    # Short-circuit by redirecting if the storage supports it.
    path = blob.storage_path
    logger.debug("Looking up the direct download URL for path: %s", path)

    # TODO (syahmed): the call below invokes a DB call to get the user
    # optimize so we don't have to go to the DB but read from the
    # token instead
    user = get_authenticated_user()
    username = user.username if user else None
    direct_download_url = storage.get_direct_download_url(
        blob.placements,
        path,
        get_request_ip(),
        username=username,
        namespace=namespace_name,
        repo_name=repo_name,
        cdn_specific=_is_cdn_specific(namespace_name),
    )
    if direct_download_url:
        logger.debug("Returning direct download URL")
        resp = redirect(direct_download_url)
        resp.headers.extend(headers)
        return resp

    # Close the database connection before we stream the download.
    logger.debug("Closing database connection before streaming layer data")
    headers.update(
        {
            "Content-Length": blob.compressed_size,
            "Content-Type": BLOB_CONTENT_TYPE,
        }
    )

    with database.CloseForLongOperation(app.config):
        # Stream the response to the client.
        return Response(
            storage.stream_read(blob.placements, path),
            headers=headers,
        )


def _is_cdn_specific(namespace):
    # Checks if blob belongs to namespace that should have cdn url returned
    logger.debug("Checking for namespace %s", namespace)
    namespaces = app.config.get("CDN_SPECIFIC_NAMESPACES")
    return namespace in namespaces


def _try_to_mount_blob(repository_ref, mount_blob_digest):
    """
    Attempts to mount a blob requested by the user from another repository.
    """

    logger.debug("Got mount request for blob `%s` into `%s`", mount_blob_digest, repository_ref)
    from_repo = request.args.get("from", None)
    if from_repo is None:
        # If we cannot mount the blob, fall back to the standard upload behavior,
        # since we don't support automatic mount origin discovery across all repos.
        return None

    # Ensure the user has access to the repository.
    logger.debug(
        "Got mount request for blob `%s` under repository `%s` into `%s`",
        mount_blob_digest,
        from_repo,
        repository_ref,
    )
    from_namespace, from_repo_name = parse_namespace_repository(
        from_repo, app.config["LIBRARY_NAMESPACE"], include_tag=False
    )

    from_repository_ref = registry_model.lookup_repository(from_namespace, from_repo_name)
    if from_repository_ref is None:
        logger.debug("Could not find from repo: `%s/%s`", from_namespace, from_repo_name)
        return None

    # First check permission.
    read_permission = ReadRepositoryPermission(from_namespace, from_repo_name).can()
    if not read_permission and features.SUPERUSERS_FULL_ACCESS:
        read_permission = SuperUserPermission().can()
    if not read_permission:
        read_permission = GlobalReadOnlySuperUserPermission().can()
    if not read_permission:
        # If no direct permission, check if the repository is public.
        if not from_repository_ref.is_public:
            logger.debug(
                "No permission to mount blob `%s` under repository `%s` into `%s`",
                mount_blob_digest,
                from_repo,
                repository_ref,
            )
            return None

    # Lookup if the mount blob's digest exists in the repository.
    mount_blob = registry_model.get_cached_repo_blob(
        model_cache, from_namespace, from_repo_name, mount_blob_digest
    )
    if mount_blob is None:
        logger.debug("Blob `%s` under repository `%s` not found", mount_blob_digest, from_repo)
        return None

    logger.debug(
        "Mounting blob `%s` under repository `%s` into `%s`",
        mount_blob_digest,
        from_repo,
        repository_ref,
    )

    # Mount the blob into the current repository and return that we've completed the operation.
    expiration_sec = app.config["PUSH_TEMP_TAG_EXPIRATION_SEC"]
    try:
        mounted = registry_model.mount_blob_into_repository(
            mount_blob,
            repository_ref,
            expiration_sec,
            mount_blob_digest,
        )
    except BlobDigestConflictException:
        raise DigestInvalid(
            message="digest is already registered to different content in destination repository",
            detail={"digest": mount_blob_digest, "reason": "conflict"},
        )
    if not mounted:
        logger.debug(
            "Could not mount blob `%s` under repository `%s` not found",
            mount_blob_digest,
            from_repo,
        )
        return

    # Return the response for the blob indicating that it was mounted, and including its content
    # digest.
    logger.debug(
        "Mounted blob `%s` under repository `%s` into `%s`",
        mount_blob_digest,
        from_repo,
        repository_ref,
    )

    namespace_name = repository_ref.namespace_name
    repo_name = repository_ref.name

    return Response(
        status=201,
        headers={
            "Docker-Content-Digest": mount_blob_digest,
            "Location": get_app_url()
            + url_for(
                "v2.download_blob",
                repository="%s/%s" % (namespace_name, repo_name),
                digest=mount_blob_digest,
            ),
        },
    )


@v2_bp.route("/<repopath:repository>/blobs/uploads/", methods=["POST"])
@disallow_for_account_recovery_mode
@parse_repository_name()
@process_registry_jwt_auth(scopes=["pull", "push"])
@log_unauthorized("push_repo_failed")
@require_repo_write(allow_for_superuser=True, disallow_for_restricted_users=True)
@anon_protect
@check_readonly
@check_pushes_disabled
def start_blob_upload(namespace_name, repo_name):

    repository_ref = registry_model.lookup_repository(namespace_name, repo_name)
    if repository_ref is None:
        raise NameUnknown("repository not found")

    if app.config.get("FEATURE_QUOTA_MANAGEMENT", False) and app.config.get(
        "FEATURE_VERIFY_QUOTA", True
    ):
        quota = namespacequota.verify_namespace_quota(repository_ref)
        if quota["severity_level"] in ("Warning", "Reject"):
            namespacequota.maybe_trigger_quota_notification(namespace_name, quota)
        if quota["severity_level"] == "Reject":
            namespacequota.notify_organization_admins(
                repository_ref, "quota_error", {"severity": "Reject"}
            )
            raise QuotaExceeded

    # Check for mounting of a blob from another repository.
    mount_blob_digest = request.args.get("mount", None)
    if mount_blob_digest is not None:
        parsed_mount_digest = _parse_digest(mount_blob_digest)
        _validate_digest_algorithm(parsed_mount_digest.hash_alg)
        validate_mirror_digest_algorithm(repository_ref, parsed_mount_digest.hash_alg)
        response = _try_to_mount_blob(repository_ref, str(parsed_mount_digest))
        if response is not None:
            return response

    # Check if the blob will be uploaded now or in followup calls. If the `digest` is given, then
    # the upload will occur as a monolithic chunk in this call. Otherwise, we return a redirect
    # for the client to upload the chunks as distinct operations.
    digest = request.args.get("digest", None)
    parsed_digest = _parse_digest(digest) if digest is not None else None
    requested_digest_algorithm = request.args.get("digest-algorithm", None)
    requested_digest_is_explicit = requested_digest_algorithm is not None
    if parsed_digest is not None:
        # The final digest is authoritative over an opening hint.
        _validate_digest_algorithm(parsed_digest.hash_alg)
        requested_digest_algorithm = parsed_digest.hash_alg
        requested_digest_is_explicit = True
    else:
        _validate_digest_algorithm(requested_digest_algorithm)

    validate_mirror_digest_algorithm(repository_ref, requested_digest_algorithm)
    if parsed_digest is None:
        if requested_digest_algorithm is None and not is_mirror_repository(repository_ref):
            alternatives = [
                algorithm
                for algorithm in app.config.get("ALLOWED_HASH_ALGORITHMS", ["sha256"])
                if algorithm != "sha256" and algorithm in digest_tools.DIGEST_ALGORITHM_LENGTHS
            ]
            if len(alternatives) == 1:
                # Track the configured alternative from byte zero. This lets a standard hintless
                # upload finalize with either SHA-256 or the alternative without storage readback.
                requested_digest_algorithm = alternatives[0]
                requested_digest_is_explicit = False

    # Begin the blob upload process only after rejecting malformed or disabled algorithms.
    blob_uploader = create_blob_upload(
        repository_ref,
        storage,
        _upload_settings(),
        requested_digest_algorithm=requested_digest_algorithm,
        requested_digest_is_explicit=requested_digest_is_explicit,
    )
    if blob_uploader is None:
        logger.debug("Could not create a blob upload for `%s/%s`", namespace_name, repo_name)
        raise InvalidRequest(message="Unable to start blob upload for unknown repository")

    if parsed_digest is None:
        # Short-circuit because the user will send the blob data in another request.
        return Response(
            status=202,
            headers={
                "Docker-Upload-UUID": blob_uploader.blob_upload_id,
                "Range": _render_range(0),
                "Location": get_app_url()
                + url_for(
                    "v2.upload_chunk",
                    repository="%s/%s" % (namespace_name, repo_name),
                    upload_uuid=blob_uploader.blob_upload_id,
                ),
            },
        )

    # Upload the data sent and commit it to a blob.
    with complete_when_uploaded(blob_uploader):
        _upload_chunk(blob_uploader, parsed_digest)

    # Write the response to the client.
    return Response(
        status=201,
        headers={
            "Docker-Content-Digest": str(parsed_digest),
            "Location": get_app_url()
            + url_for(
                "v2.download_blob",
                repository="%s/%s" % (namespace_name, repo_name),
                digest=str(parsed_digest),
            ),
        },
    )


@v2_bp.route("/<repopath:repository>/blobs/uploads/<upload_uuid>", methods=["GET"])
@disallow_for_account_recovery_mode
@parse_repository_name()
@process_registry_jwt_auth(scopes=["pull"])
@require_repo_write(allow_for_superuser=True, disallow_for_restricted_users=True)
@anon_protect
def fetch_existing_upload(namespace_name, repo_name, upload_uuid):
    repository_ref = registry_model.lookup_repository(namespace_name, repo_name)
    if repository_ref is None:
        raise NameUnknown("repository not found")

    uploader = _retrieve_blob_upload_manager(
        repository_ref, upload_uuid, restore_requested_digest=False
    )
    if uploader is None:
        raise BlobUploadUnknown()

    return Response(
        status=204,
        headers={
            "Docker-Upload-UUID": upload_uuid,
            "Location": _current_request_url(),
            "Range": _render_range(
                uploader.blob_upload.byte_count + 1
            ),  # byte ranges are exclusive
        },
    )


@v2_bp.route("/<repopath:repository>/blobs/uploads/<upload_uuid>", methods=["PATCH"])
@disallow_for_account_recovery_mode
@parse_repository_name()
@process_registry_jwt_auth(scopes=["pull", "push"])
@require_repo_write(allow_for_superuser=True, disallow_for_restricted_users=True)
@anon_protect
@check_readonly
@check_pushes_disabled
def upload_chunk(namespace_name, repo_name, upload_uuid):
    repository_ref = registry_model.lookup_repository(namespace_name, repo_name)
    if repository_ref is None:
        raise NameUnknown("repository not found")

    uploader = _retrieve_blob_upload_manager(repository_ref, upload_uuid)
    if uploader is None:
        raise BlobUploadUnknown()

    if app.config.get("FEATURE_QUOTA_MANAGEMENT", False) and app.config.get(
        "FEATURE_VERIFY_QUOTA", True
    ):
        quota = namespacequota.verify_namespace_quota_during_upload(repository_ref)
        if quota["severity_level"] in ("Warning", "Reject"):
            namespacequota.maybe_trigger_quota_notification(namespace_name, quota)
        if quota["severity_level"] == "Reject":
            namespacequota.notify_organization_admins(
                repository_ref, "quota_error", {"severity": "Reject"}
            )
            uploader.cancel_upload()
            raise QuotaExceeded

    # Upload the chunk for the blob.
    _upload_chunk(uploader)

    # Write the response to the client.
    return Response(
        status=202,
        headers={
            "Location": _current_request_url(),
            "Range": _render_range(uploader.blob_upload.byte_count, with_bytes_prefix=False),
            "Docker-Upload-UUID": upload_uuid,
        },
    )


@v2_bp.route("/<repopath:repository>/blobs/uploads/<upload_uuid>", methods=["PUT"])
@disallow_for_account_recovery_mode
@parse_repository_name()
@process_registry_jwt_auth(scopes=["pull", "push"])
@require_repo_write(allow_for_superuser=True, disallow_for_restricted_users=True)
@anon_protect
@check_readonly
def monolithic_upload_or_last_chunk(namespace_name, repo_name, upload_uuid):

    # Ensure the digest is present before proceeding.
    digest = request.args.get("digest", None)
    if digest is None:
        raise BlobUploadInvalid(detail={"reason": "Missing digest arg on monolithic upload"})
    parsed_digest = _parse_digest(digest)
    _validate_digest_algorithm(parsed_digest.hash_alg)

    # Find the upload.
    repository_ref = registry_model.lookup_repository(namespace_name, repo_name)
    if repository_ref is None:
        raise NameUnknown("repository not found")
    validate_mirror_digest_algorithm(repository_ref, parsed_digest.hash_alg)

    uploader = _retrieve_blob_upload_manager(
        repository_ref,
        upload_uuid,
        final_digest_algorithm=parsed_digest.hash_alg,
    )
    if uploader is None:
        raise BlobUploadUnknown()

    if app.config.get("FEATURE_QUOTA_MANAGEMENT", False) and app.config.get(
        "FEATURE_VERIFY_QUOTA", True
    ):
        quota = namespacequota.verify_namespace_quota_during_upload(repository_ref)
        if quota["severity_level"] in ("Warning", "Reject"):
            namespacequota.maybe_trigger_quota_notification(namespace_name, quota)
        if quota["severity_level"] == "Reject":
            namespacequota.notify_organization_admins(
                repository_ref, "quota_error", {"severity": "Reject"}
            )
            uploader.cancel_upload()
            raise QuotaExceeded

    # Upload the chunk for the blob and commit it once complete.
    with complete_when_uploaded(uploader):
        _upload_chunk(uploader, parsed_digest)

    # Write the response to the client.
    return Response(
        status=201,
        headers={
            "Docker-Content-Digest": str(parsed_digest),
            "Location": get_app_url()
            + url_for(
                "v2.download_blob",
                repository="%s/%s" % (namespace_name, repo_name),
                digest=str(parsed_digest),
            ),
        },
    )


@v2_bp.route("/<repopath:repository>/blobs/uploads/<upload_uuid>", methods=["DELETE"])
@disallow_for_account_recovery_mode
@parse_repository_name()
@process_registry_jwt_auth(scopes=["pull", "push"])
@require_repo_write(allow_for_superuser=True, disallow_for_restricted_users=True)
@anon_protect
@check_readonly
@check_pushes_disabled
def cancel_upload(namespace_name, repo_name, upload_uuid):
    repository_ref = registry_model.lookup_repository(namespace_name, repo_name)
    if repository_ref is None:
        raise NameUnknown("repository not found")

    uploader = _retrieve_blob_upload_manager(
        repository_ref, upload_uuid, restore_requested_digest=False
    )
    if uploader is None:
        raise BlobUploadUnknown()

    uploader.cancel_upload()
    return Response(status=204)


@v2_bp.route("/<repopath:repository>/blobs/<digest>", methods=["DELETE"])
@disallow_for_account_recovery_mode
@parse_repository_name()
@process_registry_jwt_auth(scopes=["pull", "push"])
@require_repo_write(allow_for_superuser=True, disallow_for_restricted_users=True)
@anon_protect
@check_readonly
@check_pushes_disabled
def delete_digest(namespace_name, repo_name, digest):
    # We do not support deleting arbitrary digests, as they break repo images.
    raise Unsupported()


def _render_range(num_uploaded_bytes, with_bytes_prefix=True):
    """
    Returns a string formatted to be used in the Range header.
    """
    return "{0}0-{1}".format("bytes=" if with_bytes_prefix else "", num_uploaded_bytes - 1)


def _current_request_url():
    return "{0}{1}{2}".format(get_app_url(), request.script_root, request.path)


def _abort_range_not_satisfiable(valid_end, upload_uuid):
    """
    Writes a failure response for scenarios where the registry cannot function with the provided
    range.

    TODO: Unify this with the V2RegistryException class.
    """
    flask_abort(
        Response(
            status=416,
            headers={
                "Location": _current_request_url(),
                "Range": "0-{0}".format(valid_end),
                "Docker-Upload-UUID": upload_uuid,
            },
        )
    )


def _start_offset_and_length(range_header):
    """
    Returns a tuple of the start offset and the length.

    If the range header doesn't exist, defaults to (0, -1). If parsing fails, returns (None, None).
    """
    start_offset, length = 0, -1
    if range_header is not None:
        # Parse the header.
        found = RANGE_HEADER_REGEX.match(range_header)
        if found is None:
            return (None, None)

        # NOTE: Offsets here are *inclusive*.
        start_offset = int(found.group(1))
        end_offset = int(found.group(2))
        length = end_offset - start_offset + 1
        if length < 0:
            return None, None

    return start_offset, length


def _parse_digest(digest):
    try:
        return digest_tools.Digest.parse_digest(digest, strict=True)
    except digest_tools.UnsupportedDigestAlgorithmException:
        algorithm = digest.split(":", 1)[0] if isinstance(digest, str) else str(digest)
        raise DigestUnsupported(algorithm)
    except digest_tools.InvalidDigestException as exc:
        raise DigestInvalid(
            message="provided digest is malformed",
            detail={
                "digest": digest,
                "reason": "malformed",
                "description": str(exc),
            },
        )


def _validate_digest_algorithm(algorithm):
    if algorithm is None:
        return
    if algorithm not in digest_tools.DIGEST_ALGORITHM_LENGTHS:
        raise DigestUnsupported(algorithm)
    if algorithm not in app.config.get("ALLOWED_HASH_ALGORITHMS", ["sha256"]):
        raise DigestDisabled(algorithm)


def _retrieve_blob_upload_manager(
    repository_ref,
    upload_uuid,
    restore_requested_digest=True,
    final_digest_algorithm=None,
):
    try:
        uploader = retrieve_blob_upload_manager(
            repository_ref,
            upload_uuid,
            storage,
            _upload_settings(),
            restore_requested_digest=False,
        )
        if uploader is None or not restore_requested_digest:
            return uploader

        stored_algorithm = uploader.blob_upload.requested_digest_algorithm
        if stored_algorithm not in (None, "sha256"):
            is_explicit = uploader.requested_digest_is_explicit()
            if stored_algorithm not in digest_tools.DIGEST_ALGORITHM_LENGTHS:
                if is_explicit:
                    raise DigestUnsupported(stored_algorithm)
                uploader.discard_implicit_requested_digest()
                return uploader
            if (
                is_explicit
                and stored_algorithm not in app.config.get("ALLOWED_HASH_ALGORITHMS", ["sha256"])
                and final_digest_algorithm != "sha256"
            ):
                raise DigestDisabled(stored_algorithm)

        uploader.restore_requested_digest()
        return uploader
    except BlobUploadInvalidStateException as exc:
        raise BlobUploadInvalid(detail={"reason": str(exc)})


def _upload_settings():
    """
    Returns the settings for instantiating a blob upload manager.
    """
    expiration_sec = app.config["PUSH_TEMP_TAG_EXPIRATION_SEC"]
    settings = BlobUploadSettings(
        maximum_blob_size=app.config["MAXIMUM_LAYER_SIZE"],
        committed_blob_expiration=expiration_sec,
    )
    return settings


def _upload_chunk(blob_uploader, commit_digest=None):
    """
    Performs uploading of a chunk of data in the current request's stream, via the blob uploader
    given.

    If commit_digest is specified, the upload is committed to a blob once the stream's data has been
    read and stored.
    """
    start_offset, length = _start_offset_and_length(request.headers.get("content-range"))
    if None in {start_offset, length}:
        raise InvalidRequest(message="Invalid range header")

    input_fp = get_input_stream(request)

    try:
        if commit_digest is not None:
            _validate_digest_algorithm(commit_digest.hash_alg)
            blob_uploader.prepare_for_digest(commit_digest)

        # Upload the data received.
        blob_uploader.upload_chunk(app.config, input_fp, start_offset, length)

        if commit_digest is not None:
            # Commit the upload to a blob.
            committed = blob_uploader.commit_to_blob(app.config, commit_digest)
            if committed is None:
                raise BlobUploadException("Could not commit blob upload")
            return committed
    except BlobTooLargeException as ble:
        raise LayerTooLarge(uploaded=ble.uploaded, max_allowed=ble.max_allowed)
    except BlobRangeMismatchException:
        logger.exception("Exception when uploading blob to %s", blob_uploader.blob_upload_id)
        _abort_range_not_satisfiable(
            blob_uploader.blob_upload.byte_count, blob_uploader.blob_upload_id
        )
    except BlobDigestMismatchException as exc:
        logger.exception("Digest mismatch for upload %s", blob_uploader.blob_upload_id)
        raise DigestInvalid(detail={"reason": str(exc) or "mismatch"})
    except BlobUploadException:
        logger.exception("Exception when uploading blob to %s", blob_uploader.blob_upload_id)
        raise BlobUploadInvalid()
