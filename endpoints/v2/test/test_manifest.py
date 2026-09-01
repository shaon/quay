import hashlib
import json
import time
from unittest.mock import Mock, patch

import pytest
from flask import url_for
from playhouse.test_utils import count_queries

from app import app as realapp
from app import docker_v2_signing_key, instance_keys, storage
from auth.auth_context_type import ValidatedAuthContext
from data import model
from data.cache.impl import InMemoryDataModelCache
from data.cache.test.test_cache import TEST_CACHE_CONFIG
from data.database import (
    ImageStorage,
    ImageStorageLocation,
    Manifest,
    ManifestBlob,
    ManifestChild,
    RepositoryManifestDigest,
    Tag,
    db,
    db_transaction,
)
from data.model.oci.tag import set_tag_immutable
from data.model.storage import get_layer_path
from data.model.test.test_repo_mirroring import create_mirror_repo_robot
from data.registry_model import registry_model
from endpoints.test.shared import conduct_call, toggle_feature
from image.docker.schema1 import (
    DOCKER_SCHEMA1_MANIFEST_CONTENT_TYPE,
    DockerSchema1ManifestBuilder,
)
from image.docker.schema2 import (
    DOCKER_SCHEMA2_CONFIG_CONTENT_TYPE,
    DOCKER_SCHEMA2_LAYER_CONTENT_TYPE,
    DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
)
from image.docker.schema2.test.test_config import (
    CONFIG_BYTES,
    CONFIG_DIGEST,
    CONFIG_SIZE,
)
from image.oci import (
    OCI_IMAGE_CONFIG_CONTENT_TYPE,
    OCI_IMAGE_INDEX_CONTENT_TYPE,
    OCI_IMAGE_MANIFEST_CONTENT_TYPE,
    OCI_IMAGE_TAR_LAYER_CONTENT_TYPE,
)
from test.fixtures import *  # noqa: F401, F403
from util.security.registry_jwt import build_context_and_subject, generate_bearer_token


def _manifest_auth_headers(repository, actions=("pull", "push"), username="devtable"):
    user = model.user.get_user(username)
    context, subject = build_context_and_subject(ValidatedAuthContext(user=user))
    token = generate_bearer_token(
        realapp.config["SERVER_HOSTNAME"],
        subject,
        context,
        [{"type": "repository", "name": repository, "actions": list(actions)}],
        600,
        instance_keys,
    )
    return {"Authorization": "Bearer %s" % token}


def _store_registered_manifest_blob(repository_name, content, algorithm="sha512"):
    repository = model.repository.get_repository(*repository_name.split("/", 1))
    location = ImageStorageLocation.get(name="local_us")
    canonical_digest = "sha256:" + hashlib.sha256(content).hexdigest()
    external_digest = f"{algorithm}:" + hashlib.new(algorithm, content).hexdigest()
    blob = model.blob.store_blob_record_and_temp_link_in_repo(
        repository.id,
        canonical_digest,
        location,
        len(content),
        3600,
    )
    storage.put_content(["local_us"], get_layer_path(blob), content)
    model.oci.blob.register_repository_blob_digest(repository, blob, external_digest)
    return blob, external_digest


def _sha512_single_manifest(
    repository_name,
    layer_bytes=b"alternative digest manifest layer",
    algorithm="sha512",
):
    config_bytes = json.dumps(
        {
            "architecture": "amd64",
            "config": {},
            "container_config": {},
            "created": "2026-01-01T00:00:00Z",
            "docker_version": "test",
            "history": [{"created": "2026-01-01T00:00:00Z", "created_by": "test"}],
            "os": "linux",
            "rootfs": {
                "type": "layers",
                "diff_ids": ["sha256:" + hashlib.sha256(layer_bytes).hexdigest()],
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    config_blob, config_digest = _store_registered_manifest_blob(
        repository_name, config_bytes, algorithm
    )
    layer_blob, layer_digest = _store_registered_manifest_blob(
        repository_name, layer_bytes, algorithm
    )
    manifest_bytes = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
            "config": {
                "mediaType": DOCKER_SCHEMA2_CONFIG_CONTENT_TYPE,
                "size": len(config_bytes),
                "digest": config_digest,
            },
            "layers": [
                {
                    "mediaType": DOCKER_SCHEMA2_LAYER_CONTENT_TYPE,
                    "size": len(layer_bytes),
                    "digest": layer_digest,
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "bytes": manifest_bytes,
        "canonical_digest": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
        "external_digest": f"{algorithm}:" + hashlib.new(algorithm, manifest_bytes).hexdigest(),
        "blob_ids": {config_blob.id, layer_blob.id},
        "referenced_blobs": [
            {
                "bytes": config_bytes,
                "canonical_digest": config_blob.content_checksum,
                "external_digest": config_digest,
            },
            {
                "bytes": layer_bytes,
                "canonical_digest": layer_blob.content_checksum,
                "external_digest": layer_digest,
            },
        ],
    }


def _manifest_index(
    children,
    media_type=OCI_IMAGE_INDEX_CONTENT_TYPE,
    subject=None,
    algorithm="sha512",
):
    manifest_dict = {
        "schemaVersion": 2,
        "mediaType": media_type,
        "manifests": [
            {
                "mediaType": child["media_type"],
                "size": len(child["bytes"]),
                "digest": child["descriptor_digest"],
                "platform": {"architecture": child["architecture"], "os": "linux"},
            }
            for child in children
        ],
    }
    if subject is not None:
        manifest_dict["subject"] = subject
    manifest_bytes = json.dumps(manifest_dict, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return {
        "bytes": manifest_bytes,
        "canonical_digest": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
        "external_digest": f"{algorithm}:" + hashlib.new(algorithm, manifest_bytes).hexdigest(),
        "media_type": media_type,
    }


def _oci_artifact(
    repository_name,
    subject,
    layer_bytes=b"artifact payload",
    algorithm="sha512",
):
    artifact = _sha512_single_manifest(repository_name, layer_bytes, algorithm)
    manifest_dict = json.loads(artifact["bytes"])
    manifest_dict["mediaType"] = OCI_IMAGE_MANIFEST_CONTENT_TYPE
    manifest_dict["artifactType"] = "application/vnd.example.signature"
    manifest_dict["config"]["mediaType"] = OCI_IMAGE_CONFIG_CONTENT_TYPE
    manifest_dict["layers"][0]["mediaType"] = OCI_IMAGE_TAR_LAYER_CONTENT_TYPE
    manifest_dict["subject"] = {
        "mediaType": subject["media_type"],
        "size": len(subject["bytes"]),
        "digest": subject["external_digest"],
    }
    manifest_bytes = json.dumps(manifest_dict, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    artifact.update(
        {
            "bytes": manifest_bytes,
            "canonical_digest": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
            "external_digest": f"{algorithm}:" + hashlib.new(algorithm, manifest_bytes).hexdigest(),
            "media_type": OCI_IMAGE_MANIFEST_CONTENT_TYPE,
        }
    )
    return artifact


def _put_manifest(client, repository, manifest_ref, manifest_info, expected_code=201):
    return conduct_call(
        client,
        "v2.write_manifest_by_digest",
        url_for,
        "PUT",
        {"repository": repository, "manifest_ref": manifest_ref},
        expected_code=expected_code,
        headers={
            **_manifest_auth_headers(repository),
            "Content-Type": manifest_info.get("media_type", DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE),
        },
        raw_body=manifest_info["bytes"],
    )


def _repository_publication_state(repository_name):
    repository = model.repository.get_repository(*repository_name.split("/", 1))
    return {
        "manifests": list(
            Manifest.select(
                Manifest.id,
                Manifest.digest,
                Manifest.subject,
                Manifest.artifact_type,
                Manifest.manifest_bytes,
            )
            .where(Manifest.repository == repository)
            .order_by(Manifest.id)
            .tuples()
        ),
        "registrations": list(
            RepositoryManifestDigest.select(
                RepositoryManifestDigest.id,
                RepositoryManifestDigest.manifest,
                RepositoryManifestDigest.digest,
            )
            .where(RepositoryManifestDigest.repository == repository)
            .order_by(RepositoryManifestDigest.id)
            .tuples()
        ),
        "tags": list(
            Tag.select(Tag.id, Tag.name, Tag.manifest, Tag.lifetime_end_ms)
            .where(Tag.repository == repository)
            .order_by(Tag.id)
            .tuples()
        ),
        "canonical_blobs": list(
            ImageStorage.select(
                ImageStorage.id,
                ImageStorage.content_checksum,
                ImageStorage.image_size,
                ImageStorage.uploading,
            )
            .order_by(ImageStorage.id)
            .tuples()
        ),
    }


def test_e2e_query_count_manifest_norewrite(client, app):
    repo_ref = registry_model.lookup_repository("devtable", "simple")
    tag = registry_model.get_repo_tag(repo_ref, "latest")
    manifest = registry_model.get_manifest_for_tag(tag)

    params = {
        "repository": "devtable/simple",
        "manifest_ref": manifest.digest,
    }

    user = model.user.get_user("devtable")
    access = [
        {
            "type": "repository",
            "name": "devtable/simple",
            "actions": ["pull", "push"],
        }
    ]

    context, subject = build_context_and_subject(ValidatedAuthContext(user=user))
    token = generate_bearer_token(
        realapp.config["SERVER_HOSTNAME"], subject, context, access, 600, instance_keys
    )

    headers = {
        "Authorization": "Bearer %s" % token,
    }

    # Conduct a call to prime the instance key and other caches.
    conduct_call(
        client,
        "v2.write_manifest_by_digest",
        url_for,
        "PUT",
        params,
        expected_code=201,
        headers=headers,
        raw_body=manifest.internal_manifest_bytes.as_encoded_str(),
    )

    timecode = time.time()

    def get_time():
        return timecode + 10

    with patch("time.time", get_time):
        # Necessary in order to have the tag updates not occur in the same second, which is the
        # granularity supported currently.
        with count_queries() as counter:
            conduct_call(
                client,
                "v2.write_manifest_by_digest",
                url_for,
                "PUT",
                params,
                expected_code=201,
                headers=headers,
                raw_body=manifest.internal_manifest_bytes.as_encoded_str(),
            )

        assert counter.count <= 27


INVALID_DOCKER_V2_MANIFEST = json.dumps(
    {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {
            "mediaType": "application/vnd.docker.container.image.v1+json",
            "size": CONFIG_SIZE,
            "digest": CONFIG_DIGEST,
        },
        "layers": [
            {
                "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                "size": 1234,
                "digest": "sha256:ec4b8955958665577945c89419d1af06b5f7636b4ac3da7f12184802ad867736",
            },
            {
                "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                "size": 32654,
                "digest": "sha256:e692418e4cbaf90ca69d05a66403747baa33ee08806650b51fab815ad7fc331f",
            },
            {
                "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                "size": -1,
                "digest": "sha256:3c3a4604a545cdc127456d94e421cd355bca5b528f4a9c1905b15da2eb4a4c6b",
            },
            {
                "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                "size": 73109,
                "digest": "sha256:ec4b8955958665577945c89419d1af06b5f7636b4ac3da7f12184802ad867736",
            },
        ],
    }
).encode("utf-8")


def test_push_malformed_manifest_docker_v2s2(client, app):
    repo_ref = registry_model.lookup_repository("devtable", "simple")

    params = {
        "repository": "devtable/simple",
        "manifest_ref": "sha256:" + hashlib.sha256(INVALID_DOCKER_V2_MANIFEST).hexdigest(),
    }

    user = model.user.get_user("devtable")
    access = [
        {
            "type": "repository",
            "name": "devtable/simple",
            "actions": ["pull", "push"],
        }
    ]

    context, subject = build_context_and_subject(ValidatedAuthContext(user=user))
    token = generate_bearer_token(
        realapp.config["SERVER_HOSTNAME"], subject, context, access, 600, instance_keys
    )

    headers = {
        "Authorization": "Bearer %s" % token,
    }

    # Conduct a call to prime the instance key and other caches.
    conduct_call(
        client,
        "v2.write_manifest_by_digest",
        url_for,
        "PUT",
        params,
        expected_code=400,
        headers=headers,
        raw_body=INVALID_DOCKER_V2_MANIFEST,
    )


INVALID_OCI_MANIFEST = json.dumps(
    {
        "schemaVersion": 2,
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "size": 7023,
            "digest": "sha256:b5b2b2c507a0944348e0303114d8d93aaaa081732b86451d9bce1f432a537bc7",
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "size": 32654,
                "digest": "sha256:9834876dcfb05cb167a5c24953eba58c4ac89b1adf57f28f2f9d09af107ee8f0",
            },
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "size": -1,
                "digest": "sha256:3c3a4604a545cdc127456d94e421cd355bca5b528f4a9c1905b15da2eb4a4c6b",
            },
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "size": 73109,
                "digest": "sha256:ec4b8955958665577945c89419d1af06b5f7636b4ac3da7f12184802ad867736",
            },
        ],
        "annotations": {"com.example.key1": "value1", "com.example.key2": "value2"},
    }
).encode("utf-8")


def test_push_malformed_manifest_oci_manifest(client, app):
    repo_ref = registry_model.lookup_repository("devtable", "simple")

    params = {
        "repository": "devtable/simple",
        "manifest_ref": "sha256:" + hashlib.sha256(INVALID_OCI_MANIFEST).hexdigest(),
    }

    user = model.user.get_user("devtable")
    access = [
        {
            "type": "repository",
            "name": "devtable/simple",
            "actions": ["pull", "push"],
        }
    ]

    context, subject = build_context_and_subject(ValidatedAuthContext(user=user))
    token = generate_bearer_token(
        realapp.config["SERVER_HOSTNAME"], subject, context, access, 600, instance_keys
    )

    headers = {
        "Authorization": "Bearer %s" % token,
    }

    # Conduct a call to prime the instance key and other caches.
    conduct_call(
        client,
        "v2.write_manifest_by_digest",
        url_for,
        "PUT",
        params,
        expected_code=400,
        headers=headers,
        raw_body=INVALID_OCI_MANIFEST,
    )


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_schema1_digest_push_remains_sha256_only(algorithm, client, app):
    repository = "devtable/simple"
    repository_row = model.repository.get_repository("devtable", "simple")
    relationship = ManifestBlob.select().where(ManifestBlob.repository == repository_row).get()
    schema1 = (
        DockerSchema1ManifestBuilder("devtable", "simple", "schema1-digest")
        .add_layer(
            relationship.blob.content_checksum,
            json.dumps({"id": "a" * 64}),
        )
        .build(docker_v2_signing_key)
    )
    body = schema1.bytes.as_encoded_str()
    alternative_digest = f"{algorithm}:" + hashlib.new(algorithm, body).hexdigest()
    headers = {
        **_manifest_auth_headers(repository),
        "Content-Type": DOCKER_SCHEMA1_MANIFEST_CONTENT_TYPE,
    }

    with patch.dict(
        realapp.config,
        {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
    ):
        rejected = conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": alternative_digest},
            expected_code=400,
            headers=headers.copy(),
            raw_body=body,
        )
        accepted = conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": schema1.digest},
            expected_code=201,
            headers=headers.copy(),
            raw_body=body,
        )

    error = rejected.get_json()["errors"][0]
    assert error["code"] == "UNSUPPORTED"
    assert error["detail"] == {"algorithm": algorithm, "reason": "unsupported"}
    assert accepted.headers["Docker-Content-Digest"] == schema1.digest
    assert (
        not RepositoryManifestDigest.select()
        .where(
            RepositoryManifestDigest.repository == repository_row,
            RepositoryManifestDigest.digest == alternative_digest,
        )
        .exists()
    )


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_alternative_single_manifest_push_pull_tag_and_repository_isolation(algorithm, client, app):
    repository = "devtable/simple"
    other_repository = "devtable/complex"
    manifest_info = _sha512_single_manifest(repository, algorithm=algorithm)
    headers = _manifest_auth_headers(repository)
    media_headers = {
        **headers,
        "Content-Type": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
        "Accept": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
    }
    test_cache = InMemoryDataModelCache(TEST_CACHE_CONFIG)

    with (
        patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]}),
        patch("endpoints.v2.manifest.model_cache", test_cache),
    ):
        conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": manifest_info["external_digest"]},
            expected_code=404,
            headers=headers.copy(),
        )

        for _ in range(2):
            response = conduct_call(
                client,
                "v2.write_manifest_by_digest",
                url_for,
                "PUT",
                {"repository": repository, "manifest_ref": manifest_info["external_digest"]},
                expected_code=201,
                headers=media_headers.copy(),
                raw_body=manifest_info["bytes"],
            )
            assert response.headers["Docker-Content-Digest"] == manifest_info["external_digest"]
            assert response.headers["Location"].endswith(
                "/manifests/" + manifest_info["external_digest"]
            )

        response = conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": manifest_info["external_digest"]},
            expected_code=200,
            headers=media_headers.copy(),
        )
        assert response.data == manifest_info["bytes"]
        assert response.headers["Docker-Content-Digest"] == manifest_info["external_digest"]
        assert response.headers["Content-Type"] == DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE

        response = conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "HEAD",
            {"repository": repository, "manifest_ref": manifest_info["external_digest"]},
            expected_code=200,
            headers=media_headers.copy(),
        )
        assert response.data == b""
        assert response.headers["Docker-Content-Digest"] == manifest_info["external_digest"]
        assert response.headers["Content-Type"] == DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE

        conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": manifest_info["canonical_digest"]},
            expected_code=404,
            headers=headers.copy(),
        )
        with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256"]}):
            disabled = conduct_call(
                client,
                "v2.fetch_manifest_by_digest",
                url_for,
                "GET",
                {"repository": repository, "manifest_ref": manifest_info["external_digest"]},
                expected_code=400,
                headers=headers.copy(),
            )
        assert disabled.get_json()["errors"][0]["detail"] == {
            "algorithm": algorithm,
            "reason": "disabled",
        }
        conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {
                "repository": other_repository,
                "manifest_ref": manifest_info["external_digest"],
            },
            expected_code=404,
            headers=_manifest_auth_headers(other_repository, actions=("pull",)),
        )

        response = conduct_call(
            client,
            "v2.write_manifest_by_tagname",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": f"{algorithm}-image"},
            expected_code=201,
            headers=media_headers.copy(),
            raw_body=manifest_info["bytes"],
        )
        assert response.headers["Docker-Content-Digest"] == manifest_info["canonical_digest"]

        response = conduct_call(
            client,
            "v2.fetch_manifest_by_tagname",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": f"{algorithm}-image"},
            expected_code=200,
            headers=media_headers.copy(),
        )
        assert response.data == manifest_info["bytes"]
        assert response.headers["Docker-Content-Digest"] == manifest_info["canonical_digest"]

        conduct_call(
            client,
            "v2.delete_manifest_by_digest",
            url_for,
            "DELETE",
            {"repository": repository, "manifest_ref": manifest_info["external_digest"]},
            expected_code=202,
            headers=headers.copy(),
        )

    repository_row = model.repository.get_repository("devtable", "simple")
    manifest = Manifest.get(
        repository=repository_row,
        digest=manifest_info["canonical_digest"],
    )
    assert {
        registration.digest
        for registration in RepositoryManifestDigest.select().where(
            RepositoryManifestDigest.repository == repository_row,
            RepositoryManifestDigest.manifest == manifest,
        )
    } == {manifest_info["external_digest"], manifest_info["canonical_digest"]}
    assert {
        relationship.blob_id
        for relationship in ManifestBlob.select().where(
            ManifestBlob.repository == repository_row,
            ManifestBlob.manifest == manifest,
        )
    } == manifest_info["blob_ids"]


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_story4_registered_graph_pull_contract(algorithm, client, app):
    repository = "devtable/simple"
    other_repository = "devtable/complex"
    tag_name = f"story4-{algorithm}"
    manifest_info = _sha512_single_manifest(repository, algorithm=algorithm)
    auth_headers = _manifest_auth_headers(repository)
    manifest_headers = {
        **auth_headers,
        "Content-Type": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
        "Accept": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
    }
    test_cache = InMemoryDataModelCache(TEST_CACHE_CONFIG)

    manifest_paths = [
        ("v2.fetch_manifest_by_digest", {"manifest_ref": manifest_info["external_digest"]}),
        ("v2.fetch_manifest_by_tagname", {"manifest_ref": tag_name}),
    ]
    blob_paths = [
        (
            "v2.download_blob",
            "v2.check_blob_exists",
            descriptor["external_digest"],
            descriptor["bytes"],
        )
        for descriptor in manifest_info["referenced_blobs"]
    ]

    with (
        patch.dict(
            realapp.config,
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
        ),
        patch("endpoints.v2.manifest.model_cache", test_cache),
        patch("endpoints.v2.blob.model_cache", test_cache),
    ):
        pushed = conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {
                "repository": repository,
                "manifest_ref": manifest_info["external_digest"],
                "tag": tag_name,
            },
            expected_code=201,
            headers=manifest_headers.copy(),
            raw_body=manifest_info["bytes"],
        )
        assert pushed.headers["Docker-Content-Digest"] == manifest_info["external_digest"]

        for endpoint, params in manifest_paths:
            for method in ("GET", "HEAD"):
                response = conduct_call(
                    client,
                    endpoint,
                    url_for,
                    method,
                    {"repository": repository, **params},
                    expected_code=200,
                    headers=manifest_headers.copy(),
                )
                assert response.headers["Docker-Content-Digest"] == manifest_info["external_digest"]
                assert response.headers["Content-Type"] == DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
                assert response.data == (manifest_info["bytes"] if method == "GET" else b"")
                if method == "GET":
                    assert (
                        f"{algorithm}:" + hashlib.new(algorithm, response.data).hexdigest()
                        == manifest_info["external_digest"]
                    )

        for get_endpoint, head_endpoint, digest, content in blob_paths:
            for method, endpoint in (("GET", get_endpoint), ("HEAD", head_endpoint)):
                response = conduct_call(
                    client,
                    endpoint,
                    url_for,
                    method,
                    {"repository": repository, "digest": digest},
                    expected_code=200,
                    headers=auth_headers.copy(),
                )
                assert response.headers["Docker-Content-Digest"] == digest
                assert response.headers["Content-Type"] == "application/octet-stream"
                assert response.data == (content if method == "GET" else b"")
                if method == "GET":
                    assert (
                        f"{algorithm}:" + hashlib.new(algorithm, response.data).hexdigest()
                        == digest
                    )

        hidden_canonical_paths = [
            (
                "v2.fetch_manifest_by_digest",
                {"manifest_ref": manifest_info["canonical_digest"]},
            ),
            *[
                ("v2.download_blob", {"digest": descriptor["canonical_digest"]})
                for descriptor in manifest_info["referenced_blobs"]
            ],
        ]
        for endpoint, params in hidden_canonical_paths:
            for method in ("GET", "HEAD"):
                conduct_call(
                    client,
                    endpoint,
                    url_for,
                    method,
                    {"repository": repository, **params},
                    expected_code=404,
                    headers=auth_headers.copy(),
                )

        for endpoint, params in [*manifest_paths, (blob_paths[0][0], {"digest": blob_paths[0][2]})]:
            normalized_params = params if isinstance(params, dict) else {"digest": params}
            for method in ("GET", "HEAD"):
                conduct_call(
                    client,
                    endpoint,
                    url_for,
                    method,
                    {"repository": repository, **normalized_params},
                    expected_code=401,
                    headers={"Accept": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE},
                )

        for method in ("GET", "HEAD"):
            conduct_call(
                client,
                "v2.fetch_manifest_by_digest",
                url_for,
                method,
                {
                    "repository": other_repository,
                    "manifest_ref": manifest_info["external_digest"],
                },
                expected_code=404,
                headers=_manifest_auth_headers(other_repository, actions=("pull",)),
            )
            conduct_call(
                client,
                "v2.download_blob" if method == "GET" else "v2.check_blob_exists",
                url_for,
                method,
                {"repository": other_repository, "digest": blob_paths[0][2]},
                expected_code=404,
                headers=_manifest_auth_headers(other_repository, actions=("pull",)),
            )

        with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256"]}):
            disabled_requests = [
                ("v2.fetch_manifest_by_digest", {"manifest_ref": manifest_info["external_digest"]}),
                ("v2.fetch_manifest_by_tagname", {"manifest_ref": tag_name}),
                ("v2.download_blob", {"digest": blob_paths[0][2]}),
            ]
            for endpoint, params in disabled_requests:
                disabled = conduct_call(
                    client,
                    endpoint,
                    url_for,
                    "GET",
                    {"repository": repository, **params},
                    expected_code=400,
                    headers=manifest_headers.copy(),
                )
                assert disabled.get_json()["errors"][0]["detail"] == {
                    "algorithm": algorithm,
                    "reason": "disabled",
                }
                conduct_call(
                    client,
                    endpoint,
                    url_for,
                    "HEAD",
                    {"repository": repository, **params},
                    expected_code=400,
                    headers=manifest_headers.copy(),
                )

        for endpoint, params in manifest_paths:
            conduct_call(
                client,
                endpoint,
                url_for,
                "HEAD",
                {"repository": repository, **params},
                expected_code=200,
                headers=manifest_headers.copy(),
            )
        conduct_call(
            client,
            "v2.check_blob_exists",
            url_for,
            "HEAD",
            {"repository": repository, "digest": blob_paths[0][2]},
            expected_code=200,
            headers=auth_headers.copy(),
        )

        for descriptor in manifest_info["referenced_blobs"]:
            response = conduct_call(
                client,
                "v2.start_blob_upload",
                url_for,
                "POST",
                {"repository": repository, "digest": descriptor["canonical_digest"]},
                expected_code=201,
                headers=auth_headers.copy(),
                raw_body=descriptor["bytes"],
            )
            assert response.headers["Docker-Content-Digest"] == descriptor["canonical_digest"]

        response = conduct_call(
            client,
            "v2.write_manifest_by_tagname",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": tag_name},
            expected_code=201,
            headers=manifest_headers.copy(),
            raw_body=manifest_info["bytes"],
        )
        assert response.headers["Docker-Content-Digest"] == manifest_info["canonical_digest"]

        canonical_paths = [
            (
                "v2.fetch_manifest_by_digest",
                "v2.fetch_manifest_by_digest",
                {"manifest_ref": manifest_info["canonical_digest"]},
            ),
            (
                "v2.fetch_manifest_by_tagname",
                "v2.fetch_manifest_by_tagname",
                {"manifest_ref": tag_name},
            ),
            *[
                (
                    "v2.download_blob",
                    "v2.check_blob_exists",
                    {"digest": descriptor["canonical_digest"]},
                )
                for descriptor in manifest_info["referenced_blobs"]
            ],
        ]
        for get_endpoint, head_endpoint, params in canonical_paths:
            expected_digest = params.get("manifest_ref", params.get("digest"))
            if get_endpoint == "v2.fetch_manifest_by_tagname":
                expected_digest = manifest_info["canonical_digest"]
            for method, endpoint in (("GET", get_endpoint), ("HEAD", head_endpoint)):
                response = conduct_call(
                    client,
                    endpoint,
                    url_for,
                    method,
                    {"repository": repository, **params},
                    expected_code=200,
                    headers=manifest_headers.copy(),
                )
                assert response.headers["Docker-Content-Digest"] == expected_digest


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_tag_push_pull_and_cleanup_obey_hard_allowlist(algorithm, client, app):
    repository = "devtable/simple"
    manifest_info = _sha512_single_manifest(repository, algorithm=algorithm)
    tag_names = [f"{algorithm}-primary", f"{algorithm}-secondary"]
    headers = _manifest_auth_headers(repository)
    media_headers = {
        **headers,
        "Content-Type": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
        "Accept": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
    }

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": [algorithm]}):
        rejected = conduct_call(
            client,
            "v2.write_manifest_by_tagname",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": tag_names[0]},
            expected_code=400,
            headers=media_headers.copy(),
            raw_body=manifest_info["bytes"],
        )
        assert rejected.get_json()["errors"][0]["detail"] == {
            "algorithm": "sha256",
            "reason": "disabled",
        }

        pushed = conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {
                "repository": repository,
                "manifest_ref": manifest_info["external_digest"],
                "tag": tag_names,
            },
            expected_code=201,
            headers=media_headers.copy(),
            raw_body=manifest_info["bytes"],
        )
        assert pushed.headers["Docker-Content-Digest"] == manifest_info["external_digest"]
        assert pushed.headers["Location"].endswith("/manifests/" + manifest_info["external_digest"])
        assert pushed.headers.getlist("OCI-Tag") == tag_names

        for tag_name in tag_names:
            pulled = conduct_call(
                client,
                "v2.fetch_manifest_by_tagname",
                url_for,
                "GET",
                {"repository": repository, "manifest_ref": tag_name},
                expected_code=200,
                headers=media_headers.copy(),
            )
            assert pulled.data == manifest_info["bytes"]
            assert pulled.headers["Docker-Content-Digest"] == manifest_info["external_digest"]

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256"]}):
        disabled = conduct_call(
            client,
            "v2.fetch_manifest_by_tagname",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": tag_names[0]},
            expected_code=400,
            headers=media_headers.copy(),
        )
        assert disabled.get_json()["errors"][0]["detail"] == {
            "algorithm": algorithm,
            "reason": "disabled",
        }

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": [algorithm]}):
        restored = conduct_call(
            client,
            "v2.fetch_manifest_by_tagname",
            url_for,
            "HEAD",
            {"repository": repository, "manifest_ref": tag_names[0]},
            expected_code=200,
            headers=media_headers.copy(),
        )
        assert restored.data == b""
        assert restored.headers["Docker-Content-Digest"] == manifest_info["external_digest"]

    # Cleanup is authorized independently of the read/write allowlist.
    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256"]}):
        conduct_call(
            client,
            "v2.delete_manifest_by_digest",
            url_for,
            "DELETE",
            {"repository": repository, "manifest_ref": manifest_info["external_digest"]},
            expected_code=202,
            headers=headers.copy(),
        )

    repository_ref = registry_model.lookup_repository("devtable", "simple")
    assert all(
        registry_model.get_repo_tag(repository_ref, tag_name) is None for tag_name in tag_names
    )


@pytest.mark.parametrize("algorithm", ["sha256", "sha512"])
def test_digest_push_reports_only_committed_tags_without_duplicating_manifest(
    algorithm, client, app
):
    repository = "devtable/simple"
    manifest_info = _sha512_single_manifest(repository, algorithm=algorithm)
    tag_name = f"story3-{algorithm}"
    second_tag_name = f"story3-{algorithm}-second"
    conventional_tag_name = f"story3-{algorithm}-conventional"
    media_headers = {
        **_manifest_auth_headers(repository),
        "Content-Type": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
    }

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha512"]}):
        untagged = conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {
                "repository": repository,
                "manifest_ref": manifest_info["external_digest"],
            },
            expected_code=201,
            headers=media_headers.copy(),
            raw_body=manifest_info["bytes"],
        )
        assert untagged.headers["Docker-Content-Digest"] == manifest_info["external_digest"]
        assert untagged.headers["Location"].endswith(
            "/manifests/" + manifest_info["external_digest"]
        )
        assert untagged.headers.getlist("OCI-Tag") == []

        repository_row = model.repository.get_repository("devtable", "simple")
        manifest_query = Manifest.select().where(
            Manifest.repository == repository_row,
            Manifest.digest == manifest_info["canonical_digest"],
        )
        assert manifest_query.count() == 1
        manifest_id = manifest_query.get().id

        tagged = conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {
                "repository": repository,
                "manifest_ref": manifest_info["external_digest"],
                "tag": tag_name,
            },
            expected_code=201,
            headers=media_headers.copy(),
            raw_body=manifest_info["bytes"],
        )
        assert tagged.headers["Docker-Content-Digest"] == manifest_info["external_digest"]
        assert tagged.headers["Location"].endswith("/manifests/" + manifest_info["external_digest"])
        assert tagged.headers.getlist("OCI-Tag") == [tag_name]

        duplicated = conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {
                "repository": repository,
                "manifest_ref": manifest_info["external_digest"],
                "tag": [tag_name, second_tag_name, tag_name, second_tag_name],
            },
            expected_code=201,
            headers=media_headers.copy(),
            raw_body=manifest_info["bytes"],
        )
        assert duplicated.headers["Docker-Content-Digest"] == manifest_info["external_digest"]
        assert duplicated.headers["Location"].endswith(
            "/manifests/" + manifest_info["external_digest"]
        )
        assert duplicated.headers.getlist("OCI-Tag") == [tag_name, second_tag_name]

        conventional = conduct_call(
            client,
            "v2.write_manifest_by_tagname",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": conventional_tag_name},
            expected_code=201,
            headers=media_headers.copy(),
            raw_body=manifest_info["bytes"],
        )
        assert conventional.headers["Docker-Content-Digest"] == manifest_info["canonical_digest"]
        assert conventional.headers["Location"].endswith(
            "/manifests/" + manifest_info["canonical_digest"]
        )
        assert conventional.headers.getlist("OCI-Tag") == []

    assert manifest_query.count() == 1
    repository_ref = registry_model.lookup_repository("devtable", "simple")
    for committed_tag_name in (tag_name, second_tag_name, conventional_tag_name):
        assert (
            registry_model.get_repo_tag(repository_ref, committed_tag_name).manifest.id
            == manifest_id
        )


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_mirror_manifest_push_rejects_alternative_digest_without_persistence(
    algorithm, client, app
):
    repo_name = f"unsupported-{algorithm}"
    mirror, repository_row = create_mirror_repo_robot(["latest"], repo_name=repo_name)
    repository = f"mirror/{repo_name}"
    manifest_info = _sha512_single_manifest(repository, algorithm=algorithm)
    headers = {
        **_manifest_auth_headers(repository, username=mirror.internal_robot.username),
        "Content-Type": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
    }

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]}):
        response = conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {
                "repository": repository,
                "manifest_ref": manifest_info["external_digest"],
                "tag": "latest",
            },
            expected_code=400,
            headers=headers,
            raw_body=manifest_info["bytes"],
        )

    assert response.get_json()["errors"][0] == {
        "code": "UNSUPPORTED",
        "message": "digest algorithm is unsupported",
        "detail": {"algorithm": algorithm, "reason": "unsupported"},
    }
    assert (
        not RepositoryManifestDigest.select()
        .where(
            RepositoryManifestDigest.repository == repository_row,
            RepositoryManifestDigest.digest == manifest_info["external_digest"],
        )
        .exists()
    )
    assert (
        registry_model.get_repo_tag(registry_model.lookup_repository("mirror", repo_name), "latest")
        is None
    )


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_tag_pull_does_not_expose_disabled_canonical_identity(algorithm, client, app):
    repository = "devtable/simple"
    manifest_info = _sha512_single_manifest(repository, algorithm=algorithm)
    tag_name = f"canonical-only-{algorithm}"
    media_headers = {
        **_manifest_auth_headers(repository),
        "Content-Type": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
        "Accept": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
    }

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]}):
        conduct_call(
            client,
            "v2.write_manifest_by_tagname",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": tag_name},
            expected_code=201,
            headers=media_headers.copy(),
            raw_body=manifest_info["bytes"],
        )

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": [algorithm]}):
        disabled = conduct_call(
            client,
            "v2.fetch_manifest_by_tagname",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": tag_name},
            expected_code=400,
            headers=media_headers.copy(),
        )

    assert disabled.get_json()["errors"][0]["detail"] == {
        "algorithm": "sha256",
        "reason": "disabled",
    }


def test_tag_pull_rejects_disabled_converted_representation(client, app):
    repository = "devtable/simple"
    manifest_info = _sha512_single_manifest(repository)
    tag_name = "sha512-conversion"
    headers = _manifest_auth_headers(repository)
    media_headers = {
        **headers,
        "Content-Type": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
        "Accept": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
    }

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha512"]}):
        conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {
                "repository": repository,
                "manifest_ref": manifest_info["external_digest"],
                "tag": tag_name,
            },
            expected_code=201,
            headers=media_headers.copy(),
            raw_body=manifest_info["bytes"],
        )

        with patch(
            "endpoints.v2.manifest._rewrite_schema_if_necessary",
            return_value=(
                Mock(),
                manifest_info["canonical_digest"],
                DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
            ),
        ):
            disabled = conduct_call(
                client,
                "v2.fetch_manifest_by_tagname",
                url_for,
                "GET",
                {"repository": repository, "manifest_ref": tag_name},
                expected_code=400,
                headers=headers.copy(),
            )

    assert disabled.get_json()["errors"][0]["detail"] == {
        "algorithm": "sha256",
        "reason": "disabled",
    }


def test_digest_push_rejects_invalid_tag_query_without_persisting_manifest(client, app):
    repository = "devtable/simple"
    manifest_info = _sha512_single_manifest(repository)
    headers = {
        **_manifest_auth_headers(repository),
        "Content-Type": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
    }

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha512"]}):
        response = conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {
                "repository": repository,
                "manifest_ref": manifest_info["external_digest"],
                "tag": "invalid/tag",
            },
            expected_code=400,
            headers=headers,
            raw_body=manifest_info["bytes"],
        )

    assert response.get_json()["errors"][0]["code"] == "TAG_INVALID"
    repository_row = model.repository.get_repository("devtable", "simple")
    assert (
        not RepositoryManifestDigest.select()
        .where(
            RepositoryManifestDigest.repository == repository_row,
            RepositoryManifestDigest.digest == manifest_info["external_digest"],
        )
        .exists()
    )


def test_digest_push_with_multiple_tags_rolls_back_atomically(client, app):
    repository = "devtable/simple"
    manifest_info = _sha512_single_manifest(repository, b"atomic multiple tags")
    new_tag = "new-before-immutable"
    repository_ref = registry_model.lookup_repository("devtable", "simple")
    original_latest = registry_model.get_repo_tag(repository_ref, "latest")
    original_manifest_id = original_latest.manifest.id
    headers = {
        **_manifest_auth_headers(repository),
        "Content-Type": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
    }

    with (
        toggle_feature("IMMUTABLE_TAGS", True),
        patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha512"]}),
    ):
        set_tag_immutable(repository_ref.id, "latest", True)
        response = conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {
                "repository": repository,
                "manifest_ref": manifest_info["external_digest"],
                "tag": [new_tag, "latest"],
            },
            expected_code=409,
            headers=headers,
            raw_body=manifest_info["bytes"],
        )

    assert response.get_json()["errors"][0]["code"] == "TAG_IMMUTABLE"
    assert registry_model.get_repo_tag(repository_ref, new_tag) is None
    assert registry_model.get_repo_tag(repository_ref, "latest").manifest.id == original_manifest_id
    assert (
        not RepositoryManifestDigest.select()
        .where(
            RepositoryManifestDigest.repository == repository_ref.id,
            RepositoryManifestDigest.digest == manifest_info["external_digest"],
        )
        .exists()
    )


def test_manifest_cache_is_invalidated_for_alternative_digest_after_tag_retarget(client, app):
    repository = "devtable/simple"
    first = _sha512_single_manifest(repository, b"first cache layer")
    second = _sha512_single_manifest(repository, b"second cache layer")
    headers = {
        **_manifest_auth_headers(repository),
        "Content-Type": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
        "Accept": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
    }
    test_cache = InMemoryDataModelCache(TEST_CACHE_CONFIG)

    with (
        patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha512"]}),
        patch("endpoints.v2.manifest.model_cache", test_cache),
    ):
        conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": first["external_digest"]},
            expected_code=201,
            headers=headers.copy(),
            raw_body=first["bytes"],
        )
        conduct_call(
            client,
            "v2.write_manifest_by_tagname",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": "cache-tag"},
            expected_code=201,
            headers=headers.copy(),
            raw_body=first["bytes"],
        )

        repository_row = model.repository.get_repository("devtable", "simple")
        first_manifest = Manifest.get(
            repository=repository_row,
            digest=first["canonical_digest"],
        )
        Tag.update(lifetime_end_ms=0).where(
            Tag.manifest == first_manifest,
            Tag.hidden,
        ).execute()

        conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": first["external_digest"]},
            expected_code=200,
            headers=headers.copy(),
        )
        conduct_call(
            client,
            "v2.write_manifest_by_tagname",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": "cache-tag"},
            expected_code=201,
            headers=headers.copy(),
            raw_body=second["bytes"],
        )
        conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": first["external_digest"]},
            expected_code=404,
            headers=headers.copy(),
        )


def test_nested_index_availability_and_descendant_cache_invalidation(client, app):
    repository = "devtable/simple"
    leaf = _sha512_single_manifest(repository, b"nested leaf")
    leaf["media_type"] = DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
    inner = _manifest_index(
        [
            {
                "bytes": leaf["bytes"],
                "descriptor_digest": leaf["external_digest"],
                "media_type": leaf["media_type"],
                "architecture": "amd64",
            }
        ]
    )
    outer = _manifest_index(
        [
            {
                "bytes": inner["bytes"],
                "descriptor_digest": inner["external_digest"],
                "media_type": OCI_IMAGE_INDEX_CONTENT_TYPE,
                "architecture": "multi",
            }
        ]
    )
    replacement = _sha512_single_manifest(repository, b"replacement root")
    headers = _manifest_auth_headers(repository)
    test_cache = InMemoryDataModelCache(TEST_CACHE_CONFIG)

    with (
        patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha512"]}),
        patch("endpoints.v2.manifest.model_cache", test_cache),
    ):
        _put_manifest(client, repository, leaf["external_digest"], leaf)
        _put_manifest(client, repository, inner["external_digest"], inner)
        _put_manifest(client, repository, outer["external_digest"], outer)
        conduct_call(
            client,
            "v2.write_manifest_by_tagname",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": "nested-root"},
            expected_code=201,
            headers={**headers, "Content-Type": OCI_IMAGE_INDEX_CONTENT_TYPE},
            raw_body=outer["bytes"],
        )

        repository_row = model.repository.get_repository("devtable", "simple")
        graph_digests = {
            leaf["canonical_digest"],
            inner["canonical_digest"],
            outer["canonical_digest"],
        }
        Tag.update(lifetime_end_ms=0).where(
            Tag.hidden,
            Tag.manifest.in_(
                Manifest.select(Manifest.id).where(
                    Manifest.repository == repository_row,
                    Manifest.digest.in_(graph_digests),
                )
            ),
        ).execute()

        conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": leaf["external_digest"]},
            expected_code=200,
            headers={**headers, "Accept": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE},
        )
        conduct_call(
            client,
            "v2.write_manifest_by_tagname",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": "nested-root"},
            expected_code=201,
            headers={**headers, "Content-Type": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE},
            raw_body=replacement["bytes"],
        )
        conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": leaf["external_digest"]},
            expected_code=404,
            headers=headers.copy(),
        )


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_alternative_manifest_rejects_cross_repository_descriptors_without_registration(
    algorithm, client, app
):
    manifest_info = _sha512_single_manifest("devtable/complex", algorithm=algorithm)
    repository = "devtable/simple"

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]}):
        response = conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": manifest_info["external_digest"]},
            expected_code=400,
            headers={
                **_manifest_auth_headers(repository),
                "Content-Type": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
            },
            raw_body=manifest_info["bytes"],
        )

    error = response.get_json()["errors"][0]
    assert error["code"] == "MANIFEST_BLOB_UNKNOWN"
    assert error["detail"]["digest"].startswith(f"{algorithm}:")
    repository_row = model.repository.get_repository("devtable", "simple")
    assert (
        not RepositoryManifestDigest.select()
        .where(
            RepositoryManifestDigest.repository == repository_row,
            RepositoryManifestDigest.digest == manifest_info["external_digest"],
        )
        .exists()
    )
    assert (
        not Manifest.select()
        .where(
            Manifest.repository == repository_row,
            Manifest.digest == manifest_info["canonical_digest"],
        )
        .exists()
    )


@pytest.mark.parametrize(
    "manifest_ref,allowed_algorithms,expected_reason",
    [
        ("sha512:1234", ["sha256", "sha512"], "malformed"),
        ("sha384:" + "a" * 95, ["sha256", "sha384", "sha512"], "malformed"),
        ("sha999:" + "a" * 96, ["sha256", "sha384", "sha512"], "unsupported"),
        ("sha384:" + "a" * 96, ["sha256", "sha512"], "disabled"),
        ("sha512:" + "a" * 128, ["sha256"], "disabled"),
    ],
)
def test_manifest_reference_digest_errors(
    manifest_ref,
    allowed_algorithms,
    expected_reason,
    client,
    app,
):
    repository = "devtable/simple"
    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": allowed_algorithms}):
        response = conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": manifest_ref},
            expected_code=400,
            headers={
                **_manifest_auth_headers(repository),
                "Content-Type": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
            },
            raw_body=b"{}",
        )

    error = response.get_json()["errors"][0]
    assert error["detail"]["reason"] == expected_reason


@pytest.mark.parametrize(
    "manifest_ref,allowed_algorithms,expected_code,expected_error,expected_reason",
    [
        ("sha384:" + "a" * 95, ["sha256", "sha384"], 400, "DIGEST_INVALID", "malformed"),
        ("sha999:" + "a" * 96, ["sha256", "sha384", "sha512"], 400, "UNSUPPORTED", "unsupported"),
        ("sha384:" + "a" * 96, ["sha256", "sha512"], 400, "UNSUPPORTED", "disabled"),
        ("sha512:" + "0" * 128, ["sha256", "sha512"], 404, "MANIFEST_UNKNOWN", None),
    ],
)
def test_manifest_pull_digest_errors_are_precise_for_get_and_head(
    manifest_ref,
    allowed_algorithms,
    expected_code,
    expected_error,
    expected_reason,
    client,
    app,
):
    repository = "devtable/simple"
    headers = _manifest_auth_headers(repository, actions=("pull",))

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": allowed_algorithms}):
        response = conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": manifest_ref},
            expected_code=expected_code,
            headers=headers.copy(),
        )
        conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "HEAD",
            {"repository": repository, "manifest_ref": manifest_ref},
            expected_code=expected_code,
            headers=headers.copy(),
        )

    error = response.get_json()["errors"][0]
    assert error["code"] == expected_error
    if expected_reason is not None:
        assert error["detail"]["reason"] == expected_reason


@pytest.mark.parametrize(
    "descriptor_digest,allowed_algorithms,expected_reason",
    [
        ("sha999:" + "a" * 96, ["sha256", "sha384", "sha512"], "unsupported"),
        ("sha384:" + "a" * 96, ["sha256", "sha512"], "disabled"),
        ("sha512:" + "a" * 128, ["sha256"], "disabled"),
    ],
)
def test_manifest_descriptor_digest_errors(
    descriptor_digest,
    allowed_algorithms,
    expected_reason,
    client,
    app,
):
    repository = "devtable/simple"
    manifest_info = _sha512_single_manifest(repository)
    manifest_dict = json.loads(manifest_info["bytes"])
    manifest_dict["config"]["digest"] = descriptor_digest
    manifest_bytes = json.dumps(manifest_dict, separators=(",", ":")).encode("utf-8")
    manifest_ref = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": allowed_algorithms}):
        response = conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": manifest_ref},
            expected_code=400,
            headers={
                **_manifest_auth_headers(repository),
                "Content-Type": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
            },
            raw_body=manifest_bytes,
        )

    error = response.get_json()["errors"][0]
    assert error["code"] == "UNSUPPORTED"
    assert error["detail"]["reason"] == expected_reason


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_manifest_registration_conflict_rolls_back_new_graph(algorithm, client, app):
    repository = "devtable/simple"
    repository_row = model.repository.get_repository("devtable", "simple")
    existing_tag = registry_model.get_repo_tag(
        registry_model.lookup_repository("devtable", "simple"), "latest"
    )
    existing_manifest = Manifest.get_by_id(existing_tag.manifest.id)
    manifest_info = _sha512_single_manifest(repository, b"conflicting manifest graph", algorithm)
    RepositoryManifestDigest.create(
        repository=repository_row,
        manifest=existing_manifest,
        digest=manifest_info["external_digest"],
    )

    previous_transaction_factory = db_transaction.obj
    db_transaction.initialize(lambda: db.transaction())
    try:
        with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]}):
            response = conduct_call(
                client,
                "v2.write_manifest_by_digest",
                url_for,
                "PUT",
                {"repository": repository, "manifest_ref": manifest_info["external_digest"]},
                expected_code=400,
                headers={
                    **_manifest_auth_headers(repository),
                    "Content-Type": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
                },
                raw_body=manifest_info["bytes"],
            )
    finally:
        db_transaction.initialize(previous_transaction_factory)

    error = response.get_json()["errors"][0]
    assert error["code"] == "DIGEST_INVALID"
    assert error["detail"]["reason"] == "conflict"
    registration = RepositoryManifestDigest.get(
        repository=repository_row,
        digest=manifest_info["external_digest"],
    )
    assert registration.manifest_id == existing_manifest.id
    assert (
        not Manifest.select()
        .where(
            Manifest.repository == repository_row,
            Manifest.digest == manifest_info["canonical_digest"],
        )
        .exists()
    )


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_manifest_exact_byte_mismatch_and_malformed_descriptor_errors(algorithm, client, app):
    repository = "devtable/simple"
    manifest_info = _sha512_single_manifest(repository, algorithm=algorithm)
    headers = {
        **_manifest_auth_headers(repository),
        "Content-Type": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
    }

    malformed_manifest = json.loads(manifest_info["bytes"])
    malformed_manifest["config"]["digest"] = f"{algorithm}:1234"
    malformed_bytes = json.dumps(malformed_manifest, separators=(",", ":")).encode("utf-8")
    malformed_ref = f"{algorithm}:" + hashlib.new(algorithm, malformed_bytes).hexdigest()

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]}):
        mismatch = conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {
                "repository": repository,
                "manifest_ref": f"{algorithm}:" + "0" * (hashlib.new(algorithm).digest_size * 2),
            },
            expected_code=400,
            headers=headers.copy(),
            raw_body=manifest_info["bytes"],
        )
        malformed = conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": malformed_ref},
            expected_code=400,
            headers=headers.copy(),
            raw_body=malformed_bytes,
        )

    mismatch_error = mismatch.get_json()["errors"][0]
    assert mismatch_error["code"] == "DIGEST_INVALID"
    assert mismatch_error["detail"]["reason"] == "mismatch"
    malformed_error = malformed.get_json()["errors"][0]
    assert malformed_error["code"] == "DIGEST_INVALID"
    assert malformed_error["detail"]["reason"] == "malformed"


@pytest.mark.parametrize(
    "parent_media_type",
    [
        OCI_IMAGE_INDEX_CONTENT_TYPE,
        "application/vnd.docker.distribution.manifest.list.v2+json",
    ],
)
def test_sha384_manifest_list_lifecycle_with_mixed_child_identities(parent_media_type, client, app):
    repository = "devtable/simple"
    sha384_child = _sha512_single_manifest(repository, b"sha384 child", algorithm="sha384")
    sha512_child = _sha512_single_manifest(repository, b"sha512 child", algorithm="sha512")
    canonical_child = _sha512_single_manifest(repository, b"canonical child")
    for child in (sha384_child, sha512_child, canonical_child):
        child["media_type"] = DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
    headers = _manifest_auth_headers(repository)
    test_cache = InMemoryDataModelCache(TEST_CACHE_CONFIG)

    with (
        patch.dict(
            realapp.config,
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
        ),
        patch("endpoints.v2.manifest.model_cache", test_cache),
    ):
        _put_manifest(client, repository, sha384_child["external_digest"], sha384_child)
        _put_manifest(client, repository, sha512_child["external_digest"], sha512_child)
        conduct_call(
            client,
            "v2.write_manifest_by_tagname",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": "canonical-child"},
            expected_code=201,
            headers={
                **headers,
                "Content-Type": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
            },
            raw_body=canonical_child["bytes"],
        )
        parent = _manifest_index(
            [
                {
                    **sha384_child,
                    "descriptor_digest": sha384_child["external_digest"],
                    "architecture": "amd64",
                },
                {
                    **sha512_child,
                    "descriptor_digest": sha512_child["external_digest"],
                    "architecture": "arm64",
                },
                {
                    **canonical_child,
                    "descriptor_digest": canonical_child["canonical_digest"],
                    "architecture": "ppc64le",
                },
            ],
            media_type=parent_media_type,
            algorithm="sha384",
        )

        for _ in range(2):
            response = _put_manifest(client, repository, parent["external_digest"], parent)
            assert response.headers["Docker-Content-Digest"] == parent["external_digest"]
            assert response.headers["Location"].endswith("/manifests/" + parent["external_digest"])

        for method in ("GET", "HEAD"):
            response = conduct_call(
                client,
                "v2.fetch_manifest_by_digest",
                url_for,
                method,
                {"repository": repository, "manifest_ref": parent["external_digest"]},
                expected_code=200,
                headers={**headers, "Accept": parent_media_type},
            )
            assert response.headers["Docker-Content-Digest"] == parent["external_digest"]
            assert response.headers["Content-Type"] == parent_media_type
            assert response.data == (parent["bytes"] if method == "GET" else b"")

        conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": parent["canonical_digest"]},
            expected_code=404,
            headers=headers.copy(),
        )
        conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {
                "repository": "devtable/complex",
                "manifest_ref": parent["external_digest"],
            },
            expected_code=404,
            headers=_manifest_auth_headers("devtable/complex", actions=("pull",)),
        )

        response = conduct_call(
            client,
            "v2.write_manifest_by_tagname",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": "multi-platform"},
            expected_code=201,
            headers={**headers, "Content-Type": parent_media_type},
            raw_body=parent["bytes"],
        )
        assert response.headers["Docker-Content-Digest"] == parent["canonical_digest"]

        parent_row = Manifest.get(digest=parent["canonical_digest"])
        Tag.update(lifetime_end_ms=int(time.time() * 1000)).where(
            Tag.manifest == parent_row, Tag.hidden == True
        ).execute()
        conduct_call(
            client,
            "v2.delete_manifest_by_digest",
            url_for,
            "DELETE",
            {"repository": repository, "manifest_ref": parent["external_digest"]},
            expected_code=202,
            headers=headers.copy(),
        )
        conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": parent["external_digest"]},
            expected_code=404,
            headers=headers.copy(),
        )

    repository_row = model.repository.get_repository("devtable", "simple")
    parent_row = Manifest.get(
        repository=repository_row,
        digest=parent["canonical_digest"],
    )
    child_ids = {
        relationship.child_manifest_id
        for relationship in ManifestChild.select().where(
            ManifestChild.repository == repository_row,
            ManifestChild.manifest == parent_row,
        )
    }
    assert child_ids == {
        Manifest.get(repository=repository_row, digest=sha384_child["canonical_digest"]).id,
        Manifest.get(repository=repository_row, digest=sha512_child["canonical_digest"]).id,
        Manifest.get(repository=repository_row, digest=canonical_child["canonical_digest"]).id,
    }
    assert {
        registration.digest
        for registration in RepositoryManifestDigest.select().where(
            RepositoryManifestDigest.repository == repository_row,
            RepositoryManifestDigest.manifest == parent_row,
        )
    } == {parent["canonical_digest"], parent["external_digest"]}


def test_manifest_list_rejects_cross_repository_and_unknown_child(client, app):
    source_child = _sha512_single_manifest("devtable/complex", b"isolated list child")
    source_child["media_type"] = DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
    parent = _manifest_index(
        [
            {
                **source_child,
                "descriptor_digest": source_child["external_digest"],
                "architecture": "amd64",
            }
        ]
    )

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha512"]}):
        _put_manifest(
            client,
            "devtable/complex",
            source_child["external_digest"],
            source_child,
        )
        response = _put_manifest(
            client,
            "devtable/simple",
            parent["external_digest"],
            parent,
            expected_code=400,
        )

    error = response.get_json()["errors"][0]
    assert error["code"] == "MANIFEST_BLOB_UNKNOWN"
    assert error["detail"] == {
        "digest": source_child["external_digest"],
        "descriptor": "child",
    }
    repository = model.repository.get_repository("devtable", "simple")
    assert (
        not Manifest.select()
        .where(
            Manifest.repository == repository,
            Manifest.digest == parent["canonical_digest"],
        )
        .exists()
    )
    assert (
        not RepositoryManifestDigest.select()
        .where(
            RepositoryManifestDigest.repository == repository,
            RepositoryManifestDigest.digest == parent["external_digest"],
        )
        .exists()
    )


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_alternative_referrer_subject_query_is_unsupported_when_enabled(algorithm, client, app):
    repository = "devtable/simple"
    digest = f"{algorithm}:" + "a" * hashlib.new(algorithm).digest_size * 2

    with (
        patch.dict(
            realapp.config,
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
        ),
        toggle_feature("REFERRERS_API", True),
    ):
        response = conduct_call(
            client,
            "v2.list_manifest_referrers",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": digest},
            expected_code=400,
            headers=_manifest_auth_headers(repository, actions=("pull",)),
        )

    assert response.get_json()["errors"][0] == {
        "code": "UNSUPPORTED",
        "message": "digest algorithm is unsupported",
        "detail": {"algorithm": algorithm, "reason": "unsupported"},
    }


@pytest.mark.parametrize(
    "digest,expected_code,expected_reason",
    [
        ("sha384:" + "a" * 95, "DIGEST_INVALID", "malformed"),
        ("sha512:" + "A" * 128, "DIGEST_INVALID", "malformed"),
        ("sha999:" + "a" * 96, "UNSUPPORTED", "unsupported"),
    ],
)
def test_referrer_subject_query_parses_strictly_before_capability_check(
    digest, expected_code, expected_reason, client, app
):
    repository = "devtable/simple"
    with (
        patch.dict(
            realapp.config,
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
        ),
        toggle_feature("REFERRERS_API", True),
    ):
        response = conduct_call(
            client,
            "v2.list_manifest_referrers",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": digest},
            expected_code=400,
            headers=_manifest_auth_headers(repository, actions=("pull",)),
        )

    error = response.get_json()["errors"][0]
    assert error["code"] == expected_code
    assert error["detail"]["reason"] == expected_reason


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_alternative_artifact_identity_is_rejected_without_publication(algorithm, client, app):
    repository = "devtable/simple"
    subject = _sha512_single_manifest(repository, b"sha256 artifact subject", algorithm="sha256")
    subject["media_type"] = DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
    artifact = _oci_artifact(repository, subject, algorithm=algorithm)

    with patch.dict(
        realapp.config,
        {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
    ):
        _put_manifest(client, repository, subject["canonical_digest"], subject)
        before = _repository_publication_state(repository)
        with (
            patch("endpoints.v2.manifest.track_and_log") as track_push,
            patch("endpoints.v2.manifest.spawn_notification") as notify_push,
        ):
            response = _put_manifest(
                client,
                repository,
                artifact["external_digest"],
                artifact,
                expected_code=400,
            )

    assert response.get_json()["errors"][0] == {
        "code": "UNSUPPORTED",
        "message": "digest algorithm is unsupported",
        "detail": {"algorithm": algorithm, "reason": "unsupported"},
    }
    assert _repository_publication_state(repository) == before
    track_push.assert_not_called()
    notify_push.assert_not_called()


@pytest.mark.parametrize("addressing", ["digest", "tag"])
@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_alternative_subject_identity_is_rejected_without_publication(
    algorithm, addressing, client, app
):
    repository = "devtable/simple"
    subject = _sha512_single_manifest(repository, b"alternative artifact subject", algorithm)
    subject["media_type"] = DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
    artifact = _oci_artifact(repository, subject, algorithm="sha256")
    headers = {
        **_manifest_auth_headers(repository),
        "Content-Type": OCI_IMAGE_MANIFEST_CONTENT_TYPE,
    }

    with patch.dict(
        realapp.config,
        {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
    ):
        _put_manifest(client, repository, subject["external_digest"], subject)
        before = _repository_publication_state(repository)
        with (
            patch("endpoints.v2.manifest.track_and_log") as track_push,
            patch("endpoints.v2.manifest.spawn_notification") as notify_push,
        ):
            if addressing == "digest":
                response = _put_manifest(
                    client,
                    repository,
                    artifact["canonical_digest"],
                    artifact,
                    expected_code=400,
                )
            else:
                response = conduct_call(
                    client,
                    "v2.write_manifest_by_tagname",
                    url_for,
                    "PUT",
                    {"repository": repository, "manifest_ref": "blocked-artifact"},
                    expected_code=400,
                    headers=headers,
                    raw_body=artifact["bytes"],
                )

    assert response.get_json()["errors"][0] == {
        "code": "UNSUPPORTED",
        "message": "digest algorithm is unsupported",
        "detail": {"algorithm": algorithm, "reason": "unsupported"},
    }
    assert _repository_publication_state(repository) == before
    track_push.assert_not_called()
    notify_push.assert_not_called()


def test_sha256_referrer_query_and_artifact_type_filter_continue_working(client, app):
    repository = "devtable/simple"
    subject = _sha512_single_manifest(repository, b"sha256 referrer subject", algorithm="sha256")
    subject["media_type"] = DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
    artifact = _oci_artifact(repository, subject, algorithm="sha256")
    headers = _manifest_auth_headers(repository, actions=("pull",))
    test_cache = InMemoryDataModelCache(TEST_CACHE_CONFIG)

    with (
        patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256"]}),
        patch("endpoints.v2.manifest.model_cache", test_cache),
        patch("endpoints.v2.referrers.model_cache", test_cache),
        toggle_feature("REFERRERS_API", True),
    ):
        _put_manifest(client, repository, subject["canonical_digest"], subject)
        _put_manifest(client, repository, artifact["canonical_digest"], artifact)
        matching = conduct_call(
            client,
            "v2.list_manifest_referrers",
            url_for,
            "GET",
            {
                "repository": repository,
                "manifest_ref": subject["canonical_digest"],
                "artifactType": "application/vnd.example.signature",
            },
            headers=headers.copy(),
        )
        missing = conduct_call(
            client,
            "v2.list_manifest_referrers",
            url_for,
            "GET",
            {
                "repository": repository,
                "manifest_ref": subject["canonical_digest"],
                "artifactType": "application/vnd.example.other",
            },
            headers=headers.copy(),
        )

    assert matching.headers["OCI-Filters-Applied"] == "artifactType"
    assert matching.get_json()["manifests"][0]["digest"] == artifact["canonical_digest"]
    assert matching.get_json()["manifests"][0]["artifactType"] == (
        "application/vnd.example.signature"
    )
    assert missing.get_json()["manifests"] == []


def test_fetch_manifest_by_digest_tracks_pull_metrics(client, app):
    """
    Test that fetching a manifest by digest calls the pull metrics tracking.

    This test verifies that PROJQUAY-9877 is fixed: "Last Pulled" and "Pull Count"
    should update when image is pulled by digest, not just by tag.
    """
    repo_ref = registry_model.lookup_repository("devtable", "simple")
    tag = registry_model.get_repo_tag(repo_ref, "latest")
    manifest = registry_model.get_manifest_for_tag(tag)

    params = {
        "repository": "devtable/simple",
        "manifest_ref": manifest.digest,
    }

    user = model.user.get_user("devtable")
    access = [
        {
            "type": "repository",
            "name": "devtable/simple",
            "actions": ["pull"],
        }
    ]

    context, subject = build_context_and_subject(ValidatedAuthContext(user=user))
    token = generate_bearer_token(
        realapp.config["SERVER_HOSTNAME"], subject, context, access, 600, instance_keys
    )

    headers = {
        "Authorization": "Bearer %s" % token,
    }

    # Mock the pullmetrics module to verify track_manifest_pull is called
    with patch("endpoints.v2.manifest.pullmetrics") as mock_pullmetrics:
        # Setup mock
        mock_event = Mock()
        mock_pullmetrics.get_event.return_value = mock_event

        # Fetch manifest by digest
        conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            params,
            expected_code=200,
            headers=headers,
        )

        # Verify that get_event was called and track_manifest_pull was invoked
        mock_pullmetrics.get_event.assert_called_once()
        mock_event.track_manifest_pull.assert_called_once()

        # Verify the call arguments
        call_args = mock_event.track_manifest_pull.call_args
        # First positional arg is repository_ref, second is manifest_digest
        assert call_args[0][1] == manifest.digest


def test_fetch_manifest_by_tagname_tracks_pull_metrics(client, app):
    """
    Test that fetching a manifest by tag name calls the pull metrics tracking.

    This is a companion test to ensure tag-based pulls are also tracked correctly.
    """
    params = {
        "repository": "devtable/simple",
        "manifest_ref": "latest",
    }

    user = model.user.get_user("devtable")
    access = [
        {
            "type": "repository",
            "name": "devtable/simple",
            "actions": ["pull"],
        }
    ]

    context, subject = build_context_and_subject(ValidatedAuthContext(user=user))
    token = generate_bearer_token(
        realapp.config["SERVER_HOSTNAME"], subject, context, access, 600, instance_keys
    )

    headers = {
        "Authorization": "Bearer %s" % token,
    }

    # Mock the pullmetrics module to verify track_tag_pull is called
    with patch("endpoints.v2.manifest.pullmetrics") as mock_pullmetrics:
        # Setup mock
        mock_event = Mock()
        mock_pullmetrics.get_event.return_value = mock_event

        # Fetch manifest by tag name
        conduct_call(
            client,
            "v2.fetch_manifest_by_tagname",
            url_for,
            "GET",
            params,
            expected_code=200,
            headers=headers,
        )

        # Verify that get_event was called and track_tag_pull was invoked
        mock_pullmetrics.get_event.assert_called_once()
        mock_event.track_tag_pull.assert_called_once()

        # Verify the call arguments
        call_args = mock_event.track_tag_pull.call_args
        # Args: repository_ref, tag_name, manifest_digest
        assert call_args[0][1] == "latest"  # tag_name


def test_delete_manifest_by_tag_immutable_returns_409(client, app):
    """Test that DELETE on an immutable tag returns 409 with TAG_IMMUTABLE error."""
    with toggle_feature("IMMUTABLE_TAGS", True):
        repo_ref = registry_model.lookup_repository("devtable", "simple")

        # Make the tag immutable
        set_tag_immutable(repo_ref.id, "latest", True)

        params = {
            "repository": "devtable/simple",
            "manifest_ref": "latest",
        }

        user = model.user.get_user("devtable")
        access = [
            {
                "type": "repository",
                "name": "devtable/simple",
                "actions": ["pull", "push"],
            }
        ]

        context, subject = build_context_and_subject(ValidatedAuthContext(user=user))
        token = generate_bearer_token(
            realapp.config["SERVER_HOSTNAME"], subject, context, access, 600, instance_keys
        )

        headers = {
            "Authorization": "Bearer %s" % token,
        }

        rv = conduct_call(
            client,
            "v2.delete_manifest_by_tag",
            url_for,
            "DELETE",
            params,
            expected_code=409,
            headers=headers,
        )

        # Verify TAG_IMMUTABLE error in response
        response_data = json.loads(rv.data)
        assert "errors" in response_data
        assert response_data["errors"][0]["code"] == "TAG_IMMUTABLE"


def test_delete_manifest_by_digest_immutable_returns_409(client, app):
    """Test that DELETE manifest by digest returns 409 when any tag is immutable."""
    with toggle_feature("IMMUTABLE_TAGS", True):
        repo_ref = registry_model.lookup_repository("devtable", "simple")
        tag = registry_model.get_repo_tag(repo_ref, "latest")
        manifest = registry_model.get_manifest_for_tag(tag)

        # Make the tag immutable
        set_tag_immutable(repo_ref.id, "latest", True)

        params = {
            "repository": "devtable/simple",
            "manifest_ref": manifest.digest,
        }

        user = model.user.get_user("devtable")
        access = [
            {
                "type": "repository",
                "name": "devtable/simple",
                "actions": ["pull", "push"],
            }
        ]

        context, subject = build_context_and_subject(ValidatedAuthContext(user=user))
        token = generate_bearer_token(
            realapp.config["SERVER_HOSTNAME"], subject, context, access, 600, instance_keys
        )

        headers = {
            "Authorization": "Bearer %s" % token,
        }

        rv = conduct_call(
            client,
            "v2.delete_manifest_by_digest",
            url_for,
            "DELETE",
            params,
            expected_code=409,
            headers=headers,
        )

        # Verify TAG_IMMUTABLE error in response
        response_data = json.loads(rv.data)
        assert "errors" in response_data
        assert len(response_data["errors"]) == 1

        error = response_data["errors"][0]
        assert error["code"] == "TAG_IMMUTABLE"
        assert "immutable" in error["message"].lower()
        assert "detail" in error
        assert "latest" in error["detail"]["message"]


def test_write_manifest_by_tagname_immutable_returns_409(client, app):
    """Test that PUT manifest on an immutable tag returns 409 with TAG_IMMUTABLE error."""
    with toggle_feature("IMMUTABLE_TAGS", True):
        repo_ref = registry_model.lookup_repository("devtable", "simple")
        tag = registry_model.get_repo_tag(repo_ref, "latest")
        manifest = registry_model.get_manifest_for_tag(tag)

        # Make the tag immutable
        set_tag_immutable(repo_ref.id, "latest", True)

        params = {
            "repository": "devtable/simple",
            "manifest_ref": "latest",
        }

        user = model.user.get_user("devtable")
        access = [
            {
                "type": "repository",
                "name": "devtable/simple",
                "actions": ["pull", "push"],
            }
        ]

        context, subject = build_context_and_subject(ValidatedAuthContext(user=user))
        token = generate_bearer_token(
            realapp.config["SERVER_HOSTNAME"], subject, context, access, 600, instance_keys
        )

        headers = {
            "Authorization": "Bearer %s" % token,
        }

        rv = conduct_call(
            client,
            "v2.write_manifest_by_tagname",
            url_for,
            "PUT",
            params,
            expected_code=409,
            headers=headers,
            raw_body=manifest.internal_manifest_bytes.as_encoded_str(),
        )

        # Verify TAG_IMMUTABLE error in response
        response_data = json.loads(rv.data)
        assert "errors" in response_data
        assert len(response_data["errors"]) == 1

        error = response_data["errors"][0]
        assert error["code"] == "TAG_IMMUTABLE"
        assert "immutable" in error["message"].lower()
        assert "detail" in error
        assert "latest" in error["detail"]["message"]


def test_tag_deletion_doesnt_return_stale_results(client, app):
    """
    Tests that deletion of a tag and subsequent pull does not return stale results for the tag
    from cache.
    """
    test_cache = InMemoryDataModelCache(TEST_CACHE_CONFIG)

    with (
        patch("endpoints.v2.tag.model_cache", test_cache),
        patch("endpoints.v2.manifest.model_cache", test_cache),
    ):
        repo_ref = registry_model.lookup_repository("devtable", "simple")
        assert repo_ref

        params_get_tags = {
            "repository": "devtable/simple",
        }

        params_delete_tag = {
            "repository": "devtable/simple",
            "manifest_ref": "latest",
        }

        # user info and access parameters
        user = model.user.get_user("devtable")
        access = [
            {
                "type": "repository",
                "name": "devtable/simple",
                "actions": ["pull", "push"],
            }
        ]

        # build context and auth
        context, subject = build_context_and_subject(ValidatedAuthContext(user=user))
        token = generate_bearer_token(
            realapp.config["SERVER_HOSTNAME"], subject, context, access, 600, instance_keys
        )

        headers = {
            "Authorization": "Bearer %s" % token,
        }

        # conduct GET tags/list to populate cache
        rv = conduct_call(
            client,
            "v2.list_all_tags",
            url_for,
            "GET",
            params_get_tags,
            expected_code=200,
            headers=headers,
        )

        # load response
        response_data = json.loads(rv.data)
        assert "latest" in response_data["tags"]

        # call delete method on the tag
        rv = conduct_call(
            client,
            "v2.delete_manifest_by_tag",
            url_for,
            "DELETE",
            params_delete_tag,
            expected_code=202,
            headers=headers,
        )

        # request another listing of tags, the tag "latest" should not be present
        rv = conduct_call(
            client,
            "v2.list_all_tags",
            url_for,
            "GET",
            params_get_tags,
            expected_code=200,
            headers=headers,
        )

        # load response
        response_data = json.loads(rv.data)
        assert "latest" not in response_data["tags"]


def test_manifest_deletion_doesnt_return_stale_results(client, app):
    """
    Tests that tag listing does not show tags associated with the deleted manifest when
    caching is enabled.
    """
    test_cache = InMemoryDataModelCache(TEST_CACHE_CONFIG)

    with (
        patch("endpoints.v2.tag.model_cache", test_cache),
        patch("endpoints.v2.manifest.model_cache", test_cache),
    ):
        repo_ref = registry_model.lookup_repository("devtable", "simple")
        assert repo_ref
        tag = registry_model.get_repo_tag(repo_ref, "latest")
        assert tag

        params_get_tags = {
            "repository": "devtable/simple",
        }

        # look up manifest digest for the "latest" tag in the repository
        manifest = registry_model.get_manifest_for_tag(tag)

        params_delete_manifest = {
            "repository": "devtable/simple",
            "manifest_ref": manifest.digest,
        }

        # user info and access parameters
        user = model.user.get_user("devtable")
        access = [
            {
                "type": "repository",
                "name": "devtable/simple",
                "actions": ["pull", "push"],
            }
        ]

        # build context and auth
        context, subject = build_context_and_subject(ValidatedAuthContext(user=user))
        token = generate_bearer_token(
            realapp.config["SERVER_HOSTNAME"], subject, context, access, 600, instance_keys
        )

        headers = {
            "Authorization": "Bearer %s" % token,
        }

        # conduct GET tags/list to populate cache
        rv = conduct_call(
            client,
            "v2.list_all_tags",
            url_for,
            "GET",
            params_get_tags,
            expected_code=200,
            headers=headers,
        )

        # load response
        response_data = json.loads(rv.data)
        assert "latest" in response_data["tags"]

        # call delete method on the tag
        rv = conduct_call(
            client,
            "v2.delete_manifest_by_tag",
            url_for,
            "DELETE",
            params_delete_manifest,
            expected_code=202,
            headers=headers,
        )

        # request another listing of tags, the tag "latest" should not be present
        rv = conduct_call(
            client,
            "v2.list_all_tags",
            url_for,
            "GET",
            params_get_tags,
            expected_code=200,
            headers=headers,
        )

        # load response
        response_data = json.loads(rv.data)
        assert "latest" not in response_data["tags"]
