import base64

import pytest
from mock import patch

from app import app as application
from data import model
from data.database import Manifest, RepositoryManifestDigest
from data.registry_model import registry_model
from endpoints.api.secscan import RepositoryManifestSecurity
from endpoints.api.test.shared import conduct_api_call
from endpoints.test.shared import gen_basic_auth
from image.docker.schema1 import DOCKER_SCHEMA1_CONTENT_TYPES
from initdb import create_schema2_or_oci_manifest_for_testing
from test.fixtures import *


def _create_story22_manifest(repository_name):
    repository = model.repository.create_repository("devtable", repository_name, None)
    tags = {}
    create_schema2_or_oci_manifest_for_testing(
        repository,
        (1, [], ["story22"]),
        tags,
    )
    repository_ref = registry_model.lookup_repository("devtable", repository_name)
    return repository_ref, tags["story22"]


def _get_story22_schema1_manifest():
    repository_ref = registry_model.lookup_repository("devtable", "simple")
    manifest = registry_model.list_all_active_repository_tags(repository_ref)[0].manifest
    manifest_row = Manifest.get_by_id(manifest.id)
    assert manifest_row.media_type.name in DOCKER_SCHEMA1_CONTENT_TYPES
    return repository_ref, manifest_row


@pytest.mark.parametrize(
    "endpoint, anonymous_allowed, auth_headers, expected_code",
    [
        pytest.param(RepositoryManifestSecurity, True, gen_basic_auth("devtable", "password"), 200),
        pytest.param(
            RepositoryManifestSecurity, False, gen_basic_auth("devtable", "password"), 200
        ),
        pytest.param(RepositoryManifestSecurity, True, None, 401),
        pytest.param(RepositoryManifestSecurity, False, None, 401),
    ],
)
def test_get_security_info_with_pull_secret(
    endpoint, anonymous_allowed, auth_headers, expected_code, client
):
    with patch("features.ANONYMOUS_ACCESS", anonymous_allowed):
        repository_ref = registry_model.lookup_repository("devtable", "simple")
        tag = registry_model.get_repo_tag(repository_ref, "latest")
        manifest = registry_model.get_manifest_for_tag(tag)

        params = {
            "repository": "devtable/simple",
            "manifestref": manifest.digest,
        }

        headers = {}
        if auth_headers is not None:
            headers["Authorization"] = auth_headers

        conduct_api_call(
            client, endpoint, "GET", params, None, headers=headers, expected_code=expected_code
        )


@pytest.mark.parametrize(("algorithm", "encoded_length"), [("sha384", 96), ("sha512", 128)])
def test_get_security_info_through_registered_digest_identity(algorithm, encoded_length, client):
    repository_ref, manifest = _create_story22_manifest("story22-report")
    manifest_row = Manifest.get_by_id(manifest.id)
    registered_digest = algorithm + ":" + "a" * encoded_length
    model.oci.manifest.register_repository_manifest_digest(
        repository_ref.id, manifest_row, registered_digest
    )

    params = {"repository": "devtable/story22-report", "manifestref": registered_digest}
    headers = {"Authorization": gen_basic_auth("devtable", "password")}
    with (
        patch.dict(
            application.config,
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
        ),
        patch(
            "endpoints.api.secscan._security_info",
            return_value={"status": "scanned", "data": None},
        ) as security_info,
    ):
        conduct_api_call(
            client,
            RepositoryManifestSecurity,
            "GET",
            params,
            None,
            headers=headers,
            expected_code=200,
        )

    resolved_manifest = security_info.call_args.args[0]
    assert resolved_manifest.id == manifest.id
    assert resolved_manifest.digest == registered_digest


@pytest.mark.parametrize(("algorithm", "encoded_length"), [("sha384", 96), ("sha512", 128)])
def test_get_security_info_hard_disables_registered_digest_identity(
    algorithm, encoded_length, client
):
    repository_ref, manifest = _create_story22_manifest("story22-disabled")
    manifest_row = Manifest.get_by_id(manifest.id)
    registered_digest = algorithm + ":" + "b" * encoded_length
    registration = model.oci.manifest.register_repository_manifest_digest(
        repository_ref.id, manifest_row, registered_digest
    )

    params = {"repository": "devtable/story22-disabled", "manifestref": registered_digest}
    headers = {"Authorization": gen_basic_auth("devtable", "password")}
    with (
        patch.dict(application.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256"]}),
        patch("endpoints.api.secscan._security_info") as security_info,
    ):
        conduct_api_call(
            client,
            RepositoryManifestSecurity,
            "GET",
            params,
            None,
            headers=headers,
            expected_code=404,
        )

    security_info.assert_not_called()
    assert RepositoryManifestDigest.get_by_id(registration.id).digest == registered_digest

    with (
        patch.dict(
            application.config,
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]},
        ),
        patch(
            "endpoints.api.secscan._security_info",
            return_value={"status": "scanned", "data": None},
        ) as security_info,
    ):
        conduct_api_call(
            client,
            RepositoryManifestSecurity,
            "GET",
            params,
            None,
            headers=headers,
            expected_code=200,
        )
    security_info.assert_called_once()


def test_get_security_info_does_not_resolve_identity_from_another_repository(client):
    repository_ref, manifest = _create_story22_manifest("story22-isolation")
    registered_digest = "sha512:" + "c" * 128
    model.oci.manifest.register_repository_manifest_digest(
        repository_ref.id,
        Manifest.get_by_id(manifest.id),
        registered_digest,
    )

    params = {"repository": "public/publicrepo", "manifestref": registered_digest}
    headers = {"Authorization": gen_basic_auth("devtable", "password")}
    with (
        patch.dict(
            application.config,
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha512"]},
        ),
        patch("endpoints.api.secscan._security_info") as security_info,
    ):
        conduct_api_call(
            client,
            RepositoryManifestSecurity,
            "GET",
            params,
            None,
            headers=headers,
            expected_code=404,
        )
    security_info.assert_not_called()


@pytest.mark.parametrize(
    "case",
    [
        "malformed",
        "unknown_algorithm",
        "enabled_unregistered",
        "disabled_canonical",
    ],
)
def test_get_security_info_rejects_inaccessible_digest_identities(case, client):
    _, manifest = _create_story22_manifest("story22-negative")
    if case == "malformed":
        manifestref = "sha256:abc"
        allowed_algorithms = ["sha256", "sha384", "sha512"]
    elif case == "unknown_algorithm":
        manifestref = "sha999:" + "d" * 64
        allowed_algorithms = ["sha256", "sha384", "sha512"]
    elif case == "enabled_unregistered":
        manifestref = "sha384:" + "e" * 96
        allowed_algorithms = ["sha256", "sha384", "sha512"]
    else:
        manifestref = Manifest.get_by_id(manifest.id).digest
        allowed_algorithms = ["sha384", "sha512"]

    params = {"repository": "devtable/story22-negative", "manifestref": manifestref}
    headers = {"Authorization": gen_basic_auth("devtable", "password")}
    with (
        patch.dict(application.config, {"ALLOWED_HASH_ALGORITHMS": allowed_algorithms}),
        patch("endpoints.api.secscan._security_info") as security_info,
    ):
        response = conduct_api_call(
            client,
            RepositoryManifestSecurity,
            "GET",
            params,
            None,
            headers=headers,
            expected_code=404,
        )

    assert response.json["error_type"] == "not_found"
    assert response.json["detail"] == "Not Found"
    security_info.assert_not_called()


@pytest.mark.parametrize(
    "stale_digest",
    [
        "sha384:" + "6" * 96,
        "sha512:" + "7" * 128,
        "sha256:" + "8" * 64,
    ],
)
def test_get_security_info_rejects_stale_schema1_registration(stale_digest, client):
    repository_ref, manifest_row = _get_story22_schema1_manifest()
    assert stale_digest != manifest_row.digest
    RepositoryManifestDigest.create(
        repository=repository_ref.id,
        manifest=manifest_row,
        digest=stale_digest,
    )
    headers = {"Authorization": gen_basic_auth("devtable", "password")}

    with (
        patch.dict(
            application.config,
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
        ),
        patch(
            "endpoints.api.secscan._security_info",
            return_value={"status": "scanned", "data": None},
        ) as security_info,
    ):
        response = conduct_api_call(
            client,
            RepositoryManifestSecurity,
            "GET",
            {"repository": "devtable/simple", "manifestref": stale_digest},
            None,
            headers=headers,
            expected_code=404,
        )
        conduct_api_call(
            client,
            RepositoryManifestSecurity,
            "GET",
            {"repository": "devtable/simple", "manifestref": manifest_row.digest},
            None,
            headers=headers,
            expected_code=200,
        )

    assert response.json["error_type"] == "not_found"
    assert response.json["detail"] == "Not Found"
    security_info.assert_called_once()
    resolved_manifest = security_info.call_args.args[0]
    assert resolved_manifest.id == manifest_row.id
    assert resolved_manifest.digest == manifest_row.digest


@pytest.mark.parametrize(
    ("auth_headers", "expected_code"),
    [
        (None, 401),
        ({"Authorization": gen_basic_auth("freshuser", "password")}, 403),
    ],
)
@pytest.mark.parametrize("identity_case", ["malformed", "disabled", "schema1_alias"])
def test_security_endpoint_authorizes_before_digest_validation(
    auth_headers, expected_code, identity_case, client
):
    repository_ref, manifest_row = _get_story22_schema1_manifest()
    stale_digest = "sha512:" + "9" * 128
    RepositoryManifestDigest.create(
        repository=repository_ref.id,
        manifest=manifest_row,
        digest=stale_digest,
    )
    if identity_case == "malformed":
        manifestref = "sha256:abc"
        allowed_algorithms = ["sha256", "sha512"]
    elif identity_case == "disabled":
        manifestref = manifest_row.digest
        allowed_algorithms = ["sha512"]
    else:
        manifestref = stale_digest
        allowed_algorithms = ["sha256", "sha512"]

    with (
        patch.dict(application.config, {"ALLOWED_HASH_ALGORITHMS": allowed_algorithms}),
        patch("endpoints.api.secscan._require_enabled_manifest_digest") as validate_digest,
        patch("endpoints.api.secscan.registry_model.lookup_manifest_by_digest") as lookup_manifest,
    ):
        conduct_api_call(
            client,
            RepositoryManifestSecurity,
            "GET",
            {"repository": "devtable/simple", "manifestref": manifestref},
            None,
            headers=auth_headers or {},
            expected_code=expected_code,
        )

    validate_digest.assert_not_called()
    lookup_manifest.assert_not_called()
