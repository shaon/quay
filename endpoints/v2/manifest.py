import logging
import re
from functools import wraps

from flask import Response
from flask import abort as flask_abort
from flask import request, url_for

import features
from app import app, model_cache, pullmetrics, storage
from auth.registry_jwt_auth import process_registry_jwt_auth
from data.database import db_disallow_replica_use
from data.model import (
    ImmutableTagException,
    ManifestDoesNotExist,
    QuotaExceededException,
    RepositoryDoesNotExist,
    TagDoesNotExist,
    namespacequota,
)
from data.model.oci.manifest import CreateManifestException
from data.model.oci.tag import RetargetTagException
from data.registry_model import registry_model
from digest import digest_tools
from digest.allowlist import DigestCheck, check_digest, is_stored_digest_allowed
from endpoints.api import (
    log_unauthorized_delete,
    log_unauthorized_pull,
    log_unauthorized_push,
)
from endpoints.decorators import (
    anon_protect,
    check_pushes_disabled,
    check_readonly,
    disallow_for_account_recovery_mode,
    inject_registry_model,
    parse_repository_name,
)
from endpoints.metrics import image_pulls, image_pushes
from endpoints.v2 import require_repo_read, require_repo_write, v2_bp
from endpoints.v2.errors import (
    DigestInvalid,
    ManifestInvalid,
    ManifestUnknown,
    NameInvalid,
    NameUnknown,
    QuotaExceeded,
    TagExpired,
    TagImmutable,
    TagInvalid,
)
from image.docker.schema1 import (
    DOCKER_SCHEMA1_CONTENT_TYPES,
    DOCKER_SCHEMA1_MANIFEST_CONTENT_TYPE,
    DockerSchema1Manifest,
)
from image.docker.schema2 import DOCKER_SCHEMA2_CONTENT_TYPES
from image.oci import OCI_CONTENT_TYPES
from image.shared import ManifestException
from image.shared.schemas import parse_manifest_from_bytes
from image.shared.schemautil import manifest_digest_algorithm
from notifications import spawn_notification
from util.audit import track_and_log
from util.bytes import Bytes
from util.config.hash_algorithms import default_manifest_hash_algorithm
from util.names import VALID_TAG_PATTERN
from util.registry.replication import queue_replication_batch

logger = logging.getLogger(__name__)

BASE_MANIFEST_ROUTE = '/<repopath:repository>/manifests/<regex("{0}"):manifest_ref>'
MANIFEST_DIGEST_ROUTE = BASE_MANIFEST_ROUTE.format(digest_tools.DIGEST_PATTERN)
MANIFEST_TAGNAME_ROUTE = BASE_MANIFEST_ROUTE.format(VALID_TAG_PATTERN)

# Maximum number of `tag` query parameters on a manifest push by digest (PROJQUAY-10117).
MAX_TAGS_PER_MANIFEST_PUSH = 10
VALID_TAG_REGEX = re.compile(VALID_TAG_PATTERN)


@v2_bp.route(MANIFEST_TAGNAME_ROUTE, methods=["GET"])
@disallow_for_account_recovery_mode
@parse_repository_name()
@process_registry_jwt_auth(scopes=["pull"])
@log_unauthorized_pull
@require_repo_read(allow_for_superuser=True, allow_for_global_readonly_superuser=True)
@anon_protect
@inject_registry_model()
def fetch_manifest_by_tagname(namespace_name, repo_name, manifest_ref, registry_model):
    try:
        repository_ref = registry_model.lookup_repository(
            namespace_name,
            repo_name,
            raise_on_error=True,
            manifest_ref=manifest_ref,
            model_cache=model_cache,
        )
    except RepositoryDoesNotExist as e:
        image_pulls.labels("v2", "tag", 404).inc()
        raise NameUnknown("repository not found")

    try:
        tag = registry_model.get_repo_tag(repository_ref, manifest_ref, raise_on_error=True)
    except TagDoesNotExist as e:
        if registry_model.has_expired_tag(repository_ref, manifest_ref):
            logger.debug(
                "Found expired tag %s for repository %s/%s", manifest_ref, namespace_name, repo_name
            )
            msg = (
                "Tag %s was deleted or has expired. To pull, revive via time machine" % manifest_ref
            )
            image_pulls.labels("v2", "tag", 404).inc()
            raise TagExpired(msg)

        image_pulls.labels("v2", "tag", 404).inc()
        raise ManifestUnknown(str(e))

    manifest = registry_model.get_manifest_for_tag(tag)
    if manifest is None:
        # Something went wrong.
        image_pulls.labels("v2", "tag", 400).inc()
        raise ManifestInvalid()

    # For tag requests the allowlist applies to the resolved manifest digest (PROJQUAY-10117).
    if not is_stored_digest_allowed(manifest.digest, app.config):
        image_pulls.labels("v2", "tag", 404).inc()
        raise ManifestUnknown()

    try:
        manifest_bytes, manifest_digest, manifest_media_type = _rewrite_schema_if_necessary(
            namespace_name, repo_name, manifest_ref, manifest, registry_model
        )
    except (ManifestException, ManifestDoesNotExist) as e:
        image_pulls.labels("v2", "tag", 404).inc()
        raise ManifestUnknown(str(e))

    if manifest_bytes is None or not is_stored_digest_allowed(manifest_digest, app.config):
        image_pulls.labels("v2", "tag", 404).inc()
        raise ManifestUnknown()

    track_and_log(
        "pull_repo",
        repository_ref,
        analytics_name="pull_repo_100x",
        analytics_sample=0.01,
        tag=manifest_ref,
        mediaType=manifest_media_type,
    )
    image_pulls.labels("v2", "tag", 200).inc()

    # Track pull metrics if feature is enabled
    if features.IMAGE_PULL_STATS:
        try:
            if pullmetrics:
                metrics = pullmetrics.get_event()
                metrics.track_tag_pull(repository_ref, manifest_ref, manifest_digest)
        except Exception as e:
            logger.warning(
                "Could not track tag pull metrics: %s. " "Pull statistics may not be recorded.",
                str(e),
            )

    return Response(
        manifest_bytes.as_unicode(),
        status=200,
        headers={
            "Content-Type": manifest_media_type,
            "Docker-Content-Digest": manifest_digest,
        },
    )


@v2_bp.route(MANIFEST_DIGEST_ROUTE, methods=["GET"])
@disallow_for_account_recovery_mode
@parse_repository_name()
@process_registry_jwt_auth(scopes=["pull"])
@log_unauthorized_pull
@require_repo_read(allow_for_superuser=True, allow_for_global_readonly_superuser=True)
@anon_protect
@inject_registry_model()
def fetch_manifest_by_digest(namespace_name, repo_name, manifest_ref, registry_model):
    try:
        repository_ref = registry_model.lookup_repository(
            namespace_name,
            repo_name,
            raise_on_error=True,
            manifest_ref=manifest_ref,
            model_cache=model_cache,
        )
    except RepositoryDoesNotExist as e:
        image_pulls.labels("v2", "manifest", 404).inc()
        raise NameUnknown("repository not found")

    # The allowlist applies to every response, including cached ones (PROJQUAY-10117).
    if check_digest(manifest_ref, app.config) == DigestCheck.REJECTED:
        image_pulls.labels("v2", "manifest", 404).inc()
        raise ManifestUnknown()

    try:
        manifest = registry_model.lookup_cached_manifest_by_digest(
            model_cache,
            repository_ref,
            manifest_ref,
            raise_on_error=True,
            allow_hidden=True,
        )
    except ManifestDoesNotExist as e:
        image_pulls.labels("v2", "manifest", 404).inc()
        raise ManifestUnknown(str(e))

    track_and_log(
        "pull_repo", repository_ref, manifest_digest=manifest_ref, mediaType=manifest.media_type
    )
    image_pulls.labels("v2", "manifest", 200).inc()

    # Track pull metrics if feature is enabled
    if features.IMAGE_PULL_STATS:
        try:
            if pullmetrics:
                metrics = pullmetrics.get_event()
                metrics.track_manifest_pull(repository_ref, manifest_ref)
        except Exception as e:
            logger.warning(
                "Could not track manifest pull metrics: %s. "
                "Pull statistics may not be recorded.",
                str(e),
            )

    return Response(
        manifest.internal_manifest_bytes.as_unicode(),
        status=200,
        headers={
            "Content-Type": manifest.media_type,
            "Docker-Content-Digest": manifest.digest,
        },
    )


def _rewrite_schema_if_necessary(
    namespace_name, repo_name, tag_name, manifest, registry_model=registry_model
):
    # As per the Docker protocol, if the manifest is not schema version 1 and the manifest's
    # media type is not in the Accept header, we return a schema 1 version of the manifest for
    # the amd64+linux platform, if any, or None if none.
    # See: https://docs.docker.com/registry/spec/manifest-v2-2
    mimetypes = [mimetype for mimetype, _ in request.accept_mimetypes]
    if manifest.media_type in mimetypes:
        return manifest.internal_manifest_bytes, manifest.digest, manifest.media_type

    # Short-circuit check: If the mimetypes is empty or just `application/json`, verify we have
    # a schema 1 manifest and return it.
    if not mimetypes or mimetypes == ["application/json"]:
        if manifest.media_type in DOCKER_SCHEMA1_CONTENT_TYPES:
            return manifest.internal_manifest_bytes, manifest.digest, manifest.media_type

    logger.debug(
        "Manifest `%s` not compatible against %s; checking for conversion",
        manifest.digest,
        request.accept_mimetypes,
    )
    converted = registry_model.convert_manifest(
        manifest, namespace_name, repo_name, tag_name, mimetypes, storage
    )
    if converted is not None:
        return converted.bytes, converted.digest, converted.media_type

    # For back-compat, we always default to schema 1 if the manifest could not be converted.
    schema1 = registry_model.get_schema1_parsed_manifest(
        manifest, namespace_name, repo_name, tag_name, storage, raise_on_error=True
    )
    return schema1.bytes, schema1.digest, schema1.media_type


def _reject_manifest2_schema2(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        namespace_name = kwargs["namespace_name"]
        if (
            request.content_type
            and request.content_type != "application/json"
            and request.content_type
            not in DOCKER_SCHEMA1_CONTENT_TYPES | DOCKER_SCHEMA2_CONTENT_TYPES | OCI_CONTENT_TYPES
        ):
            raise ManifestInvalid(
                detail={"message": "manifest schema version not supported"}, http_status_code=415
            )

        if namespace_name not in app.config.get("V22_NAMESPACE_BLACKLIST", []):
            if request.content_type in OCI_CONTENT_TYPES:
                if (
                    namespace_name not in app.config.get("OCI_NAMESPACE_WHITELIST", [])
                    and not features.GENERAL_OCI_SUPPORT
                ):
                    raise ManifestInvalid(
                        detail={"message": "manifest schema version not supported"},
                        http_status_code=415,
                    )

            return func(*args, **kwargs)

        if (
            _doesnt_accept_schema_v1()
            or request.content_type in DOCKER_SCHEMA2_CONTENT_TYPES | OCI_CONTENT_TYPES
        ):
            raise ManifestInvalid(
                detail={"message": "manifest schema version not supported"}, http_status_code=415
            )
        return func(*args, **kwargs)

    return wrapped


def _doesnt_accept_schema_v1():
    # If the client doesn't specify anything, still give them Schema v1.
    return (
        len(request.accept_mimetypes) != 0
        and DOCKER_SCHEMA1_MANIFEST_CONTENT_TYPE not in request.accept_mimetypes
    )


@v2_bp.route(MANIFEST_TAGNAME_ROUTE, methods=["PUT"])
@disallow_for_account_recovery_mode
@parse_repository_name()
@_reject_manifest2_schema2
@process_registry_jwt_auth(scopes=["pull", "push"])
@log_unauthorized_push
@require_repo_write(allow_for_superuser=True, disallow_for_restricted_users=True)
@anon_protect
@check_readonly
@check_pushes_disabled
def write_manifest_by_tagname(namespace_name, repo_name, manifest_ref):
    # A push by tag hashes the manifest with DEFAULT_MANIFEST_HASH_ALGORITHM; schema 1 stays
    # SHA-256.
    parsed = _parse_manifest(
        request.content_type,
        request.data,
        digest_algorithm=default_manifest_hash_algorithm(app.config),
        schema1_uses_sha256=True,
    )
    _check_manifest_references(parsed)

    return _write_manifest_and_log(namespace_name, repo_name, manifest_ref, parsed)


def _enqueue_blobs_for_replication(manifest, storage, namespace_name):
    blobs = registry_model.get_manifest_local_blobs(manifest, storage)
    if blobs is None:
        logger.error("Could not lookup blobs for manifest `%s`", manifest.digest)
    else:
        with queue_replication_batch(namespace_name) as queue_storage_replication:
            for blob_digest in blobs:
                queue_storage_replication(blob_digest)


@v2_bp.route(MANIFEST_DIGEST_ROUTE, methods=["PUT"])
@disallow_for_account_recovery_mode
@parse_repository_name()
@_reject_manifest2_schema2
@process_registry_jwt_auth(scopes=["pull", "push"])
@log_unauthorized_push
@require_repo_write(allow_for_superuser=True, disallow_for_restricted_users=True)
@anon_protect
@check_readonly
@check_pushes_disabled
def write_manifest_by_digest(namespace_name, repo_name, manifest_ref):
    if check_digest(manifest_ref, app.config) == DigestCheck.REJECTED:
        image_pushes.labels("v2", 400, "").inc()
        raise DigestInvalid(detail={"digest": manifest_ref})

    tag_names = _requested_tags()

    # A push by digest hashes the manifest with the algorithm named in the address.
    parsed = _parse_manifest(
        request.content_type,
        request.data,
        digest_algorithm=manifest_digest_algorithm(manifest_ref),
    )
    if parsed.digest != manifest_ref:
        image_pushes.labels("v2", 400, "").inc()
        raise ManifestInvalid(detail={"message": "manifest digest mismatch"})

    _check_manifest_references(parsed)

    if tag_names:
        return _write_manifest_with_tags_and_log(namespace_name, repo_name, tag_names, parsed)

    if parsed.schema_version != 2:
        return _write_manifest_and_log(namespace_name, repo_name, parsed.tag, parsed)

    # If the manifest is schema version 2, then this cannot be a normal tag-based push, as the
    # manifest does not contain the tag and this call was not given a tag name.
    # Instead, we write the manifest with a temporary tag, as it is being pushed
    # as part of a call for a manifest list.
    repository_ref = registry_model.lookup_repository(
        namespace_name, repo_name, model_cache=model_cache
    )
    if repository_ref is None:
        image_pushes.labels("v2", 404, "").inc()
        raise NameUnknown("repository not found")

    expiration_sec = app.config["PUSH_TEMP_TAG_EXPIRATION_SEC"]
    manifest = registry_model.create_manifest_with_temp_tag(
        repository_ref,
        parsed,
        expiration_sec,
        storage,
        model_cache=model_cache,
    )

    if manifest is None:
        image_pushes.labels("v2", 400, "").inc()
        raise ManifestInvalid()

    image_pushes.labels("v2", 201, manifest.media_type).inc()

    # Queue all blob manifests for replication.
    if features.STORAGE_REPLICATION:
        _enqueue_blobs_for_replication(manifest, storage, namespace_name)

    return Response(
        "OK",
        status=201,
        headers={
            "Docker-Content-Digest": manifest.digest,
            "Location": url_for(
                "v2.fetch_manifest_by_digest",
                repository="%s/%s" % (namespace_name, repo_name),
                manifest_ref=manifest.digest,
            ),
        },
    )


def _requested_tags():
    """
    Returns the `tag` query parameters of a manifest push by digest, validated: more than
    MAX_TAGS_PER_MANIFEST_PUSH returns 414, an invalid tag name returns 400 TAG_INVALID.
    """
    tag_names = request.args.getlist("tag")
    if len(tag_names) > MAX_TAGS_PER_MANIFEST_PUSH:
        image_pushes.labels("v2", 414, "").inc()
        flask_abort(
            Response(
                "Request-URI Too Long: at most %s tag parameters are supported"
                % MAX_TAGS_PER_MANIFEST_PUSH,
                status=414,
            )
        )

    for tag_name in tag_names:
        if not VALID_TAG_REGEX.fullmatch(tag_name):
            image_pushes.labels("v2", 400, "").inc()
            raise TagInvalid(detail={"message": "invalid tag name `%s`" % tag_name})

    # Keep the first occurrence of each tag, in request order.
    return list(dict.fromkeys(tag_names))


def _check_manifest_references(parsed):
    """
    Rejects a manifest that references a blob or child manifest by an unsupported, disallowed or
    malformed non-SHA-256 digest, or whose `subject` does (400 MANIFEST_INVALID). An allowed
    subject that does not exist yet is accepted.
    """
    referenced = []
    try:
        referenced.extend(str(d) for d in (parsed.blob_digests or []))
    except ManifestException as me:
        raise ManifestInvalid(detail={"message": str(me)})

    if parsed.is_manifest_list:
        for child in parsed.manifest_dict.get("manifests", []):
            if isinstance(child, dict) and "digest" in child:
                referenced.append(str(child["digest"]))

    for digest in referenced:
        if check_digest(digest, app.config) == DigestCheck.REJECTED:
            image_pushes.labels("v2", 400, "").inc()
            raise ManifestInvalid(
                detail={"message": "manifest references a disallowed digest `%s`" % digest}
            )

    subject = getattr(parsed, "subject", None)
    if subject is not None and check_digest(str(subject.digest), app.config) == (
        DigestCheck.REJECTED
    ):
        image_pushes.labels("v2", 400, "").inc()
        raise ManifestInvalid(
            detail={"message": "manifest subject has a disallowed digest `%s`" % subject.digest}
        )


def _parse_manifest(
    content_type,
    request_data,
    digest_algorithm=digest_tools.SHA256,
    schema1_uses_sha256=False,
):
    content_type = content_type or DOCKER_SCHEMA1_MANIFEST_CONTENT_TYPE
    if content_type == "application/json":
        # For back-compat.
        content_type = DOCKER_SCHEMA1_MANIFEST_CONTENT_TYPE

    if schema1_uses_sha256 and content_type in DOCKER_SCHEMA1_CONTENT_TYPES:
        digest_algorithm = digest_tools.SHA256

    try:
        return parse_manifest_from_bytes(
            Bytes.for_string_or_unicode(request_data),
            content_type,
            digest_algorithm=digest_algorithm,
        )
    except ManifestException as me:
        logger.exception("failed to parse manifest when writing by tagname")
        raise ManifestInvalid(detail={"message": "failed to parse manifest: %s" % me})


@v2_bp.route(MANIFEST_DIGEST_ROUTE, methods=["DELETE"])
@disallow_for_account_recovery_mode
@parse_repository_name()
@process_registry_jwt_auth(scopes=["pull", "push"])
@log_unauthorized_delete
@require_repo_write(allow_for_superuser=True, disallow_for_restricted_users=True)
@anon_protect
@check_readonly
@check_pushes_disabled
def delete_manifest_by_digest(namespace_name, repo_name, manifest_ref):
    """
    Delete the manifest specified by the digest.

    Note: there is no equivalent method for deleting by tag name because it is
    forbidden by the spec.
    """
    if check_digest(manifest_ref, app.config) == DigestCheck.REJECTED:
        raise ManifestUnknown()

    with db_disallow_replica_use():
        repository_ref = registry_model.lookup_repository(
            namespace_name, repo_name, model_cache=model_cache
        )
        if repository_ref is None:
            raise NameUnknown("repository not found")

        manifest = registry_model.lookup_manifest_by_digest(repository_ref, manifest_ref)
        if manifest is None:
            raise ManifestUnknown()

        try:
            tags = registry_model.delete_tags_for_manifest(model_cache, manifest)
        except ImmutableTagException as ite:
            raise TagImmutable(detail={"message": f"tag '{ite.tag_name}' is immutable"}) from ite
        if not tags:
            raise ManifestUnknown()

        for tag in tags:
            track_and_log("delete_tag", repository_ref, tag=tag.name, digest=manifest_ref)

        return Response(status=202)


@v2_bp.route(MANIFEST_TAGNAME_ROUTE, methods=["DELETE"])
@disallow_for_account_recovery_mode
@parse_repository_name()
@process_registry_jwt_auth(scopes=["pull", "push"])
@log_unauthorized_delete
@require_repo_write(allow_for_superuser=True, disallow_for_restricted_users=True)
@anon_protect
@check_readonly
@check_pushes_disabled
def delete_manifest_by_tag(namespace_name, repo_name, manifest_ref):
    """
    Deletes the manifest specified by the tag.
    """
    with db_disallow_replica_use():
        repository_ref = registry_model.lookup_repository(
            namespace_name, repo_name, model_cache=model_cache
        )
        if repository_ref is None:
            raise NameUnknown("repository not found")

        tag = registry_model.get_repo_tag(repository_ref, manifest_ref)
        if tag is None:
            raise ManifestUnknown()

        try:
            deleted_tag = registry_model.delete_tag(model_cache, repository_ref, manifest_ref)
        except ImmutableTagException as ite:
            raise TagImmutable(detail={"message": f"tag '{ite.tag_name}' is immutable"}) from ite

        if not deleted_tag:
            raise ManifestUnknown()

        track_and_log("delete_tag", repository_ref, tag=deleted_tag.name, digest=manifest_ref)
        return Response(status=202)


def _write_manifest_and_log(namespace_name, repo_name, tag_name, manifest_impl):
    _validate_schema1_manifest(namespace_name, repo_name, manifest_impl)
    with db_disallow_replica_use():
        repository_ref, manifest, tag = _write_manifest(
            namespace_name, repo_name, tag_name, manifest_impl
        )

        # Queue all blob manifests for replication
        if features.STORAGE_REPLICATION:
            _enqueue_blobs_for_replication(manifest, storage, namespace_name)

        track_and_log("push_repo", repository_ref, tag=tag_name, mediaType=manifest.media_type)
        spawn_notification(repository_ref, "repo_push", {"updated_tags": [tag_name]})
        image_pushes.labels("v2", 201, manifest.media_type).inc()

        return Response(
            "OK",
            status=201,
            headers={
                "Docker-Content-Digest": manifest.digest,
                "Location": url_for(
                    "v2.fetch_manifest_by_digest",
                    repository="%s/%s" % (namespace_name, repo_name),
                    manifest_ref=manifest.digest,
                ),
            },
        )


def _write_manifest_with_tags_and_log(namespace_name, repo_name, tag_names, manifest_impl):
    """
    Push by digest with `?tag=` (PROJQUAY-10117): creates or reuses the manifest and assigns every
    requested tag in one transaction. Each accepted tag is reported in an `OCI-Tag` header.
    """
    _validate_schema1_manifest(namespace_name, repo_name, manifest_impl)
    with db_disallow_replica_use():
        repository_ref = registry_model.lookup_repository(
            namespace_name, repo_name, model_cache=model_cache
        )
        if repository_ref is None:
            raise NameUnknown("repository not found")

        try:
            manifest, tags = registry_model.create_manifest_and_retarget_tags(
                repository_ref,
                manifest_impl,
                tag_names,
                storage,
                raise_on_error=True,
                verify_quota=app.config.get("FEATURE_QUOTA_MANAGEMENT", False)
                and app.config.get("FEATURE_VERIFY_QUOTA", True),
                model_cache=model_cache,
            )
        except CreateManifestException as cme:
            raise ManifestInvalid(detail={"message": str(cme)})
        except RetargetTagException as rte:
            raise ManifestInvalid(detail={"message": str(rte)})
        except ImmutableTagException as ite:
            raise TagImmutable(detail={"message": f"tag '{ite.tag_name}' is immutable"}) from ite
        except QuotaExceededException:
            raise QuotaExceeded()

        if manifest is None:
            raise ManifestInvalid()

        if features.STORAGE_REPLICATION:
            _enqueue_blobs_for_replication(manifest, storage, namespace_name)

        for tag_name in tag_names:
            track_and_log(
                "push_repo", repository_ref, tag=tag_name, mediaType=manifest.media_type
            )
        spawn_notification(repository_ref, "repo_push", {"updated_tags": list(tag_names)})
        image_pushes.labels("v2", 201, manifest.media_type).inc()

        headers = [
            ("Docker-Content-Digest", manifest.digest),
            (
                "Location",
                url_for(
                    "v2.fetch_manifest_by_digest",
                    repository="%s/%s" % (namespace_name, repo_name),
                    manifest_ref=manifest.digest,
                ),
            ),
        ]
        headers.extend(("OCI-Tag", tag_name) for tag_name in tag_names)
        return Response("OK", status=201, headers=headers)


def _write_manifest(
    namespace_name, repo_name, tag_name, manifest_impl, registry_model=registry_model
):
    # Ensure that the repository exists.
    repository_ref = registry_model.lookup_repository(
        namespace_name, repo_name, model_cache=model_cache
    )
    if repository_ref is None:
        raise NameUnknown("repository not found")

    # Create the manifest(s) and retarget the tag to point to it.
    try:
        manifest, tag = registry_model.create_manifest_and_retarget_tag(
            repository_ref,
            manifest_impl,
            tag_name,
            storage,
            raise_on_error=True,
            verify_quota=app.config.get("FEATURE_QUOTA_MANAGEMENT", False)
            and app.config.get("FEATURE_VERIFY_QUOTA", True),
            model_cache=model_cache,
        )
    except CreateManifestException as cme:
        raise ManifestInvalid(detail={"message": str(cme)})
    except RetargetTagException as rte:
        raise ManifestInvalid(detail={"message": str(rte)})
    except ImmutableTagException as ite:
        raise TagImmutable(detail={"message": f"tag '{ite.tag_name}' is immutable"}) from ite
    except QuotaExceededException as qee:
        raise QuotaExceeded()

    if manifest is None:
        raise ManifestInvalid()

    return repository_ref, manifest, tag


def _validate_schema1_manifest(namespace: str, repo: str, manifest: DockerSchema1Manifest):
    # NOTE: These extra checks are needed for schema version 1 because the manifests
    # contain the repo namespace, name and tag name.
    if manifest.schema_version != 1:
        return

    if (
        manifest.namespace == ""
        and features.LIBRARY_SUPPORT
        and namespace == app.config["LIBRARY_NAMESPACE"]
    ):
        pass
    elif manifest.namespace != namespace:
        raise NameInvalid(
            message="namespace name does not match manifest",
            detail={
                "message": "namespace name `%s` does not match `%s` in manifest"
                % (namespace, manifest.namespace),
            },
        )

    if manifest.repo_name != repo:
        raise NameInvalid(
            message="repository name does not match manifest",
            detail={
                "message": "repository name `%s` does not match `%s` in manifest"
                % (repo, manifest.repo_name),
            },
        )

    try:
        if not manifest.layers:
            raise ManifestInvalid(detail={"message": "manifest does not reference any layers"})
    except ManifestException as me:
        raise ManifestInvalid(detail={"message": str(me)})
