from flask import Response, request

import features
from app import app, model_cache
from auth.registry_jwt_auth import process_registry_jwt_auth
from data.model import ManifestDoesNotExist, RepositoryDoesNotExist
from data.registry_model import registry_model
from digest import digest_tools
from endpoints.decorators import (
    anon_protect,
    disallow_for_account_recovery_mode,
    inject_registry_model,
    parse_repository_name,
    route_show_if,
)
from endpoints.v2 import require_repo_read, v2_bp
from endpoints.v2.errors import (
    DigestDisabled,
    DigestInvalid,
    DigestUnsupported,
    ManifestInvalid,
    NameUnknown,
)
from image.oci.index import OCIIndexBuilder

BASE_REFERRERS_ROUTE = '/<repopath:repository>/referrers/<regex("{0}"):manifest_ref>'
MANIFEST_REFERRERS_ROUTE = BASE_REFERRERS_ROUTE.format(digest_tools.DIGEST_PATTERN)


@v2_bp.route(MANIFEST_REFERRERS_ROUTE, methods=["GET"])
@route_show_if(features.REFERRERS_API)
@disallow_for_account_recovery_mode
@parse_repository_name()
@process_registry_jwt_auth(scopes=["pull"])
@require_repo_read(allow_for_superuser=True, allow_for_global_readonly_superuser=True)
@anon_protect
@inject_registry_model()
def list_manifest_referrers(namespace_name, repo_name, manifest_ref, registry_model):
    manifest_ref = _validate_manifest_reference(manifest_ref)
    try:
        repository_ref = registry_model.lookup_repository(
            namespace_name, repo_name, raise_on_error=True, manifest_ref=manifest_ref
        )
    except RepositoryDoesNotExist as e:
        raise NameUnknown("repository not found")

    try:
        manifest = registry_model.lookup_manifest_by_digest(
            repository_ref, manifest_ref, raise_on_error=True, allow_hidden=True
        )
    except ManifestDoesNotExist as e:
        raise ManifestInvalid(str(e))

    artifact_type = request.args.get("artifactType", None)

    referrers = registry_model.lookup_cached_referrers_for_manifest(
        model_cache,
        repository_ref,
        manifest,
        artifact_type,
        allowed_algorithms=app.config.get("ALLOWED_HASH_ALGORITHMS", ["sha256"]),
    )
    index = _build_referrers_index_for_manifests(referrers)
    headers = {"Content-Type": index.media_type}
    if artifact_type is not None:
        headers["OCI-Filters-Applied"] = "artifactType"

    return Response(index.bytes.as_unicode(), status=200, headers=headers)


def _build_referrers_index_for_manifests(referrers):
    index_builder = OCIIndexBuilder()

    for referrer in referrers:
        parsed_referrer = referrer.get_parsed_manifest()
        index_builder.add_manifest_digest(
            referrer.digest,
            len(referrer.internal_manifest_bytes.as_encoded_str()),
            referrer.media_type,
            None,
            None,
            parsed_referrer.artifact_type,
            getattr(parsed_referrer, "annotations", None),
        )

    index = index_builder.build()
    return index


def _validate_manifest_reference(manifest_ref):
    try:
        parsed = digest_tools.Digest.parse_digest(manifest_ref, strict=True)
    except digest_tools.UnsupportedDigestAlgorithmException:
        raise DigestUnsupported(manifest_ref.split(":", 1)[0])
    except digest_tools.InvalidDigestException as exc:
        raise DigestInvalid(
            message="provided manifest digest is malformed",
            detail={
                "digest": manifest_ref,
                "reason": "malformed",
                "description": str(exc),
            },
        )

    if parsed.hash_alg not in app.config.get("ALLOWED_HASH_ALGORITHMS", ["sha256"]):
        raise DigestDisabled(parsed.hash_alg)
    return str(parsed)
