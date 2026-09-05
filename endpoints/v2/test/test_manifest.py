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
    ImageStoragePlacement,
    Manifest,
    ManifestBlob,
    ManifestChild,
    QuotaNamespaceSize,
    QuotaRepositorySize,
    RepositoryBlobDigest,
    RepositoryManifestDigest,
    Tag,
    db,
    db_transaction,
)
from data.model.oci.tag import filter_to_alive_tags, set_tag_immutable
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
from image.shared.schemas import parse_manifest_from_bytes
from test.fixtures import *  # noqa: F401, F403
from util.bytes import Bytes
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


def _copy_auth_headers(source_repository, destination_repository, username="devtable"):
    user = model.user.get_user(username)
    context, subject = build_context_and_subject(ValidatedAuthContext(user=user))
    token = generate_bearer_token(
        realapp.config["SERVER_HOSTNAME"],
        subject,
        context,
        [
            {
                "type": "repository",
                "name": source_repository,
                "actions": ["pull"],
            },
            {
                "type": "repository",
                "name": destination_repository,
                "actions": ["pull", "push"],
            },
        ],
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
    config_algorithm=None,
    layer_algorithm=None,
):
    config_algorithm = config_algorithm or algorithm
    layer_algorithm = layer_algorithm or algorithm
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
        repository_name, config_bytes, config_algorithm
    )
    layer_blob, layer_digest = _store_registered_manifest_blob(
        repository_name, layer_bytes, layer_algorithm
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


def _put_manifest(
    client,
    repository,
    manifest_ref,
    manifest_info,
    expected_code=201,
    tag=None,
):
    params = {"repository": repository, "manifest_ref": manifest_ref}
    if tag is not None:
        params["tag"] = tag
    return conduct_call(
        client,
        "v2.write_manifest_by_digest",
        url_for,
        "PUT",
        params,
        expected_code=expected_code,
        headers={
            **_manifest_auth_headers(repository),
            "Content-Type": manifest_info.get("media_type", DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE),
        },
        raw_body=manifest_info["bytes"],
    )


def _register_manifest_alias(repository_name, canonical_digest, content, algorithm):
    repository = model.repository.get_repository(*repository_name.split("/", 1))
    manifest = Manifest.get(
        Manifest.repository == repository,
        Manifest.digest == canonical_digest,
    )
    alias = f"{algorithm}:" + hashlib.new(algorithm, content).hexdigest()
    model.oci.manifest.register_repository_manifest_digest(repository.id, manifest, alias)
    return alias


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
        "manifest_registrations": list(
            RepositoryManifestDigest.select(
                RepositoryManifestDigest.id,
                RepositoryManifestDigest.manifest,
                RepositoryManifestDigest.digest,
            )
            .where(RepositoryManifestDigest.repository == repository)
            .order_by(RepositoryManifestDigest.id)
            .tuples()
        ),
        "blob_registrations": list(
            RepositoryBlobDigest.select(
                RepositoryBlobDigest.id,
                RepositoryBlobDigest.image_storage,
                RepositoryBlobDigest.digest,
            )
            .where(RepositoryBlobDigest.repository == repository)
            .order_by(RepositoryBlobDigest.id)
            .tuples()
        ),
        "tags": list(
            Tag.select(Tag.id, Tag.name, Tag.manifest, Tag.lifetime_end_ms)
            .where(Tag.repository == repository)
            .order_by(Tag.id)
            .tuples()
        ),
        "manifest_blobs": list(
            ManifestBlob.select(
                ManifestBlob.id,
                ManifestBlob.manifest,
                ManifestBlob.blob,
            )
            .where(ManifestBlob.repository == repository)
            .order_by(ManifestBlob.id)
            .tuples()
        ),
        "manifest_children": list(
            ManifestChild.select(
                ManifestChild.id,
                ManifestChild.manifest,
                ManifestChild.child_manifest,
            )
            .where(ManifestChild.repository == repository)
            .order_by(ManifestChild.id)
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
        "repository_quota": list(
            QuotaRepositorySize.select().where(QuotaRepositorySize.repository == repository).dicts()
        ),
        "namespace_quota": list(
            QuotaNamespaceSize.select()
            .where(QuotaNamespaceSize.namespace_user == repository.namespace_user)
            .dicts()
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

        assert counter.count <= 28


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


def _store_story13_legacy_single_manifest(repository_name, tag_name, algorithm):
    manifest_info = _sha512_single_manifest(
        repository_name,
        layer_bytes=f"story13 legacy layer {algorithm}".encode("utf-8"),
        algorithm="sha256",
    )
    repository = model.repository.get_repository(*repository_name.split("/", 1))
    parsed = parse_manifest_from_bytes(
        Bytes.for_string_or_unicode(manifest_info["bytes"]),
        DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
    )
    created = model.oci.manifest.get_or_create_manifest(
        repository.id,
        parsed,
        storage,
        for_tagging=True,
        raise_on_error=True,
    )
    model.oci.tag.retarget_tag(tag_name, created.manifest, raise_on_error=True)

    RepositoryManifestDigest.delete().where(
        RepositoryManifestDigest.repository == repository,
        RepositoryManifestDigest.manifest == created.manifest,
    ).execute()
    RepositoryBlobDigest.delete().where(
        RepositoryBlobDigest.repository == repository,
        RepositoryBlobDigest.image_storage.in_(manifest_info["blob_ids"]),
    ).execute()
    return repository, created.manifest, manifest_info


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_story13_legacy_single_manifest_graph_compatibility(algorithm, client, app):
    repository = "devtable/simple"
    other_repository = "devtable/complex"
    tag_name = f"story13-legacy-{algorithm}"
    repository_row, manifest_row, manifest_info = _store_story13_legacy_single_manifest(
        repository,
        tag_name,
        algorithm,
    )
    other_repository_row = model.repository.get_repository("devtable", "complex")
    alternative_manifest_digest = (
        f"{algorithm}:" + hashlib.new(algorithm, manifest_info["bytes"]).hexdigest()
    )
    auth_headers = _manifest_auth_headers(repository, actions=("pull",))
    manifest_headers = {
        **auth_headers,
        "Accept": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
    }
    test_cache = InMemoryDataModelCache(TEST_CACHE_CONFIG)

    assert (
        not RepositoryManifestDigest.select()
        .where(
            RepositoryManifestDigest.repository == repository_row,
            RepositoryManifestDigest.manifest == manifest_row,
        )
        .exists()
    )
    assert (
        not RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == repository_row,
            RepositoryBlobDigest.image_storage.in_(manifest_info["blob_ids"]),
        )
        .exists()
    )

    unauthorized_requests = [
        (
            "v2.fetch_manifest_by_tagname",
            {"manifest_ref": tag_name},
        ),
        (
            "v2.fetch_manifest_by_digest",
            {"manifest_ref": manifest_info["canonical_digest"]},
        ),
        (
            "v2.download_blob",
            {"digest": manifest_info["referenced_blobs"][0]["canonical_digest"]},
        ),
    ]
    for endpoint, params in unauthorized_requests:
        conduct_call(
            client,
            endpoint,
            url_for,
            "GET",
            {"repository": repository, **params},
            expected_code=401,
            headers={"Accept": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE},
        )

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": [algorithm]}):
        disabled_requests = [
            (
                "v2.fetch_manifest_by_tagname",
                {"manifest_ref": tag_name},
            ),
            (
                "v2.fetch_manifest_by_digest",
                {"manifest_ref": manifest_info["canonical_digest"]},
            ),
            (
                "v2.download_blob",
                {"digest": manifest_info["referenced_blobs"][0]["canonical_digest"]},
            ),
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
                "algorithm": "sha256",
                "reason": "disabled",
            }

    assert (
        not RepositoryManifestDigest.select()
        .where(
            RepositoryManifestDigest.repository == repository_row,
            RepositoryManifestDigest.manifest == manifest_row,
        )
        .exists()
    )
    assert (
        not RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == repository_row,
            RepositoryBlobDigest.image_storage.in_(manifest_info["blob_ids"]),
        )
        .exists()
    )

    with (
        patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]}),
        patch("endpoints.v2.manifest.model_cache", test_cache),
        patch("endpoints.v2.blob.model_cache", test_cache),
    ):
        for _ in range(2):
            for method in ("GET", "HEAD"):
                tag_response = conduct_call(
                    client,
                    "v2.fetch_manifest_by_tagname",
                    url_for,
                    method,
                    {"repository": repository, "manifest_ref": tag_name},
                    expected_code=200,
                    headers=manifest_headers.copy(),
                )
                digest_response = conduct_call(
                    client,
                    "v2.fetch_manifest_by_digest",
                    url_for,
                    method,
                    {
                        "repository": repository,
                        "manifest_ref": manifest_info["canonical_digest"],
                    },
                    expected_code=200,
                    headers=manifest_headers.copy(),
                )
                for response in (tag_response, digest_response):
                    assert (
                        response.headers["Docker-Content-Digest"]
                        == manifest_info["canonical_digest"]
                    )
                    assert response.headers["Content-Type"] == DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
                    assert response.data == (manifest_info["bytes"] if method == "GET" else b"")

        manifest_registration = RepositoryManifestDigest.get(
            repository=repository_row,
            digest=manifest_info["canonical_digest"],
        )
        assert manifest_registration.manifest_id == manifest_row.id
        assert (
            RepositoryManifestDigest.select()
            .where(
                RepositoryManifestDigest.repository == repository_row,
                RepositoryManifestDigest.digest == manifest_info["canonical_digest"],
            )
            .count()
            == 1
        )

        for descriptor in manifest_info["referenced_blobs"]:
            for _ in range(2):
                for method, endpoint in (
                    ("GET", "v2.download_blob"),
                    ("HEAD", "v2.check_blob_exists"),
                ):
                    response = conduct_call(
                        client,
                        endpoint,
                        url_for,
                        method,
                        {"repository": repository, "digest": descriptor["canonical_digest"]},
                        expected_code=200,
                        headers=auth_headers.copy(),
                    )
                    assert (
                        response.headers["Docker-Content-Digest"] == descriptor["canonical_digest"]
                    )
                    assert response.data == (descriptor["bytes"] if method == "GET" else b"")
                    if method == "GET":
                        assert (
                            "sha256:" + hashlib.sha256(response.data).hexdigest()
                            == descriptor["canonical_digest"]
                        )

            blob_registration = RepositoryBlobDigest.get(
                repository=repository_row,
                digest=descriptor["canonical_digest"],
            )
            assert (
                blob_registration.image_storage.content_checksum == descriptor["canonical_digest"]
            )
            assert (
                RepositoryBlobDigest.select()
                .where(
                    RepositoryBlobDigest.repository == repository_row,
                    RepositoryBlobDigest.digest == descriptor["canonical_digest"],
                )
                .count()
                == 1
            )

        for method in ("GET", "HEAD"):
            conduct_call(
                client,
                "v2.fetch_manifest_by_digest",
                url_for,
                method,
                {
                    "repository": other_repository,
                    "manifest_ref": manifest_info["canonical_digest"],
                },
                expected_code=404,
                headers=_manifest_auth_headers(other_repository, actions=("pull",)),
            )
            for descriptor in manifest_info["referenced_blobs"]:
                conduct_call(
                    client,
                    "v2.download_blob" if method == "GET" else "v2.check_blob_exists",
                    url_for,
                    method,
                    {"repository": other_repository, "digest": descriptor["canonical_digest"]},
                    expected_code=404,
                    headers=_manifest_auth_headers(other_repository, actions=("pull",)),
                )

        assert (
            not RepositoryManifestDigest.select()
            .where(
                RepositoryManifestDigest.repository == other_repository_row,
                RepositoryManifestDigest.digest == manifest_info["canonical_digest"],
            )
            .exists()
        )
        assert (
            not RepositoryBlobDigest.select()
            .where(
                RepositoryBlobDigest.repository == other_repository_row,
                RepositoryBlobDigest.digest.in_(
                    [item["canonical_digest"] for item in manifest_info["referenced_blobs"]]
                ),
            )
            .exists()
        )

        model.oci.manifest.register_repository_manifest_digest(
            repository_row,
            manifest_row,
            alternative_manifest_digest,
        )
        alternative_blob_digests = []
        for descriptor in manifest_info["referenced_blobs"]:
            alternative_digest = (
                f"{algorithm}:" + hashlib.new(algorithm, descriptor["bytes"]).hexdigest()
            )
            alternative_blob_digests.append(alternative_digest)
            blob_row = ImageStorage.get(content_checksum=descriptor["canonical_digest"])
            model.oci.blob.register_repository_blob_digest(
                repository_row,
                blob_row,
                alternative_digest,
            )

        for digest in (manifest_info["canonical_digest"], alternative_manifest_digest):
            for method in ("GET", "HEAD"):
                response = conduct_call(
                    client,
                    "v2.fetch_manifest_by_digest",
                    url_for,
                    method,
                    {"repository": repository, "manifest_ref": digest},
                    expected_code=200,
                    headers=manifest_headers.copy(),
                )
                assert response.headers["Docker-Content-Digest"] == digest
                assert response.data == (manifest_info["bytes"] if method == "GET" else b"")

        with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": [algorithm]}):
            for method in ("GET", "HEAD"):
                conduct_call(
                    client,
                    "v2.fetch_manifest_by_digest",
                    url_for,
                    method,
                    {
                        "repository": repository,
                        "manifest_ref": manifest_info["canonical_digest"],
                    },
                    expected_code=400,
                    headers=manifest_headers.copy(),
                )
                alternative_response = conduct_call(
                    client,
                    "v2.fetch_manifest_by_digest",
                    url_for,
                    method,
                    {"repository": repository, "manifest_ref": alternative_manifest_digest},
                    expected_code=200,
                    headers=manifest_headers.copy(),
                )
                assert alternative_response.headers["Docker-Content-Digest"] == (
                    alternative_manifest_digest
                )
                tag_response = conduct_call(
                    client,
                    "v2.fetch_manifest_by_tagname",
                    url_for,
                    method,
                    {"repository": repository, "manifest_ref": tag_name},
                    expected_code=200,
                    headers=manifest_headers.copy(),
                )
                assert tag_response.headers["Docker-Content-Digest"] == alternative_manifest_digest

                for descriptor, alternative_digest in zip(
                    manifest_info["referenced_blobs"], alternative_blob_digests
                ):
                    conduct_call(
                        client,
                        "v2.download_blob" if method == "GET" else "v2.check_blob_exists",
                        url_for,
                        method,
                        {"repository": repository, "digest": descriptor["canonical_digest"]},
                        expected_code=400,
                        headers=auth_headers.copy(),
                    )
                    conduct_call(
                        client,
                        "v2.download_blob" if method == "GET" else "v2.check_blob_exists",
                        url_for,
                        method,
                        {"repository": repository, "digest": alternative_digest},
                        expected_code=200,
                        headers=auth_headers.copy(),
                    )

        restored_tag = conduct_call(
            client,
            "v2.fetch_manifest_by_tagname",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": tag_name},
            expected_code=200,
            headers=manifest_headers.copy(),
        )
        assert restored_tag.headers["Docker-Content-Digest"] == manifest_info["canonical_digest"]

        malformed = conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": "sha256:1234"},
            expected_code=400,
            headers=manifest_headers.copy(),
        )
        unknown = conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {
                "repository": repository,
                "manifest_ref": "sha256:" + hashlib.sha256(b"story13 unknown").hexdigest(),
            },
            expected_code=404,
            headers=manifest_headers.copy(),
        )
        assert malformed.get_json()["errors"][0]["detail"]["reason"] == "malformed"
        assert unknown.get_json()["errors"][0]["code"] == "MANIFEST_UNKNOWN"

    assert Manifest.get_by_id(manifest_row.id).digest == manifest_info["canonical_digest"]
    assert {
        registration.digest
        for registration in RepositoryManifestDigest.select().where(
            RepositoryManifestDigest.repository == repository_row,
            RepositoryManifestDigest.manifest == manifest_row,
        )
    } == {manifest_info["canonical_digest"], alternative_manifest_digest}
    assert {
        registration.digest
        for registration in RepositoryBlobDigest.select().where(
            RepositoryBlobDigest.repository == repository_row,
            RepositoryBlobDigest.image_storage.in_(manifest_info["blob_ids"]),
        )
    } == {
        *[item["canonical_digest"] for item in manifest_info["referenced_blobs"]],
        *alternative_blob_digests,
    }


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
@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.parametrize("algorithm_enabled", [False, True])
def test_story19_schema1_alternative_route_is_always_unsupported_without_mutation(
    algorithm, signed, algorithm_enabled, client, app
):
    repository = "devtable/simple"
    relationship = (
        ManifestBlob.select()
        .where(ManifestBlob.repository == model.repository.get_repository("devtable", "simple"))
        .get()
    )
    schema1 = (
        DockerSchema1ManifestBuilder("devtable", "simple", "story19-route")
        .add_layer(relationship.blob.content_checksum, json.dumps({"id": "b" * 64}))
        .build(docker_v2_signing_key if signed else None)
    )
    body = schema1.bytes.as_encoded_str()
    alternative_digest = f"{algorithm}:" + hashlib.new(algorithm, body).hexdigest()
    allowed_algorithms = ["sha256", algorithm] if algorithm_enabled else ["sha256"]
    before = _repository_publication_state(repository)
    test_cache = InMemoryDataModelCache(TEST_CACHE_CONFIG)

    with (
        patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": allowed_algorithms}),
        patch("endpoints.v2.manifest.model_cache", test_cache),
        patch("endpoints.v2.manifest.spawn_notification") as spawn_notification,
    ):
        response = conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": alternative_digest},
            expected_code=400,
            headers={
                **_manifest_auth_headers(repository),
                "Content-Type": schema1.media_type,
            },
            raw_body=body,
        )

    error = response.get_json()["errors"][0]
    assert error["code"] == "UNSUPPORTED"
    assert error["detail"] == {"algorithm": algorithm, "reason": "unsupported"}
    assert _repository_publication_state(repository) == before
    assert not test_cache.cache
    spawn_notification.assert_not_called()


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
@pytest.mark.parametrize("signed", [False, True])
def test_story19_schema1_alternative_blob_sum_is_unsupported_before_mutation(
    algorithm, signed, client, app
):
    repository = "devtable/simple"
    repository_row = model.repository.get_repository("devtable", "simple")
    relationship = ManifestBlob.select().where(ManifestBlob.repository == repository_row).get()
    alternative_blob_digest = f"{algorithm}:" + "c" * hashlib.new(algorithm).digest_size * 2
    model.oci.blob.register_repository_blob_digest(
        repository_row,
        relationship.blob,
        alternative_blob_digest,
    )
    schema1 = (
        DockerSchema1ManifestBuilder("devtable", "simple", "story19-blob-sum")
        .add_layer(alternative_blob_digest, json.dumps({"id": "d" * 64}))
        .build(docker_v2_signing_key if signed else None)
    )
    before = _repository_publication_state(repository)
    test_cache = InMemoryDataModelCache(TEST_CACHE_CONFIG)

    with (
        patch.dict(
            realapp.config,
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
        ),
        patch("endpoints.v2.manifest.model_cache", test_cache),
        patch("endpoints.v2.manifest.spawn_notification") as spawn_notification,
    ):
        response = conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": schema1.digest},
            expected_code=400,
            headers={
                **_manifest_auth_headers(repository),
                "Content-Type": schema1.media_type,
            },
            raw_body=schema1.bytes.as_encoded_str(),
        )

    error = response.get_json()["errors"][0]
    assert error["code"] == "UNSUPPORTED"
    assert error["detail"] == {"algorithm": algorithm, "reason": "unsupported"}
    assert _repository_publication_state(repository) == before
    assert not test_cache.cache
    spawn_notification.assert_not_called()


def _publish_story19_schema1(client, tag_name):
    repository = "devtable/simple"
    repository_row = model.repository.get_repository("devtable", "simple")
    relationship = ManifestBlob.select().where(ManifestBlob.repository == repository_row).get()
    schema1 = (
        DockerSchema1ManifestBuilder("devtable", "simple", tag_name)
        .add_layer(relationship.blob.content_checksum, json.dumps({"id": "e" * 64}))
        .build(docker_v2_signing_key)
    )
    conduct_call(
        client,
        "v2.write_manifest_by_digest",
        url_for,
        "PUT",
        {"repository": repository, "manifest_ref": schema1.digest},
        expected_code=201,
        headers={
            **_manifest_auth_headers(repository),
            "Content-Type": schema1.media_type,
        },
        raw_body=schema1.bytes.as_encoded_str(),
    )
    manifest = Manifest.get(repository=repository_row, digest=schema1.digest)
    return repository, repository_row, manifest, schema1


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_story19_schema1_stale_alias_cannot_bypass_reads_tags_or_hard_disable(
    algorithm, client, app
):
    repository, repository_row, manifest, schema1 = _publish_story19_schema1(
        client, f"story19-read-{algorithm}"
    )
    RepositoryManifestDigest.delete().where(
        RepositoryManifestDigest.repository == repository_row,
        RepositoryManifestDigest.manifest == manifest,
    ).execute()
    alternative_digest = (
        f"{algorithm}:" + hashlib.new(algorithm, schema1.bytes.as_encoded_str()).hexdigest()
    )
    RepositoryManifestDigest.create(
        repository=repository_row,
        manifest=manifest,
        digest=alternative_digest,
    )
    cache = InMemoryDataModelCache(TEST_CACHE_CONFIG)
    pull_headers = _manifest_auth_headers(repository, actions=("pull",))

    with (
        patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]}),
        patch("endpoints.v2.manifest.model_cache", cache),
    ):
        repository_ref = registry_model.lookup_repository("devtable", "simple")
        registry_model.lookup_cached_manifest_by_digest(
            cache,
            repository_ref,
            alternative_digest,
            allow_hidden=True,
            raise_on_error=True,
        )
        for _ in range(2):
            for method in ("GET", "HEAD"):
                alias_response = conduct_call(
                    client,
                    "v2.fetch_manifest_by_digest",
                    url_for,
                    method,
                    {"repository": repository, "manifest_ref": alternative_digest},
                    expected_code=400,
                    headers=pull_headers.copy(),
                )
                if method == "GET":
                    assert alias_response.get_json()["errors"][0]["detail"] == {
                        "algorithm": algorithm,
                        "reason": "unsupported",
                    }
                else:
                    assert alias_response.data == b""
                canonical_response = conduct_call(
                    client,
                    "v2.fetch_manifest_by_digest",
                    url_for,
                    method,
                    {"repository": repository, "manifest_ref": schema1.digest},
                    expected_code=200,
                    headers=pull_headers.copy(),
                )
                assert canonical_response.headers["Docker-Content-Digest"] == schema1.digest
                assert canonical_response.data == (
                    schema1.bytes.as_encoded_str() if method == "GET" else b""
                )

        tag_response = conduct_call(
            client,
            "v2.fetch_manifest_by_tagname",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": schema1.tag},
            expected_code=200,
            headers=pull_headers.copy(),
        )
        assert tag_response.headers["Docker-Content-Digest"] == schema1.digest

        conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {"repository": "devtable/complex", "manifest_ref": alternative_digest},
            expected_code=404,
            headers=_manifest_auth_headers("devtable/complex", actions=("pull",)),
        )
        conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": alternative_digest},
            expected_code=401,
        )

        with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256"]}):
            disabled_alias = conduct_call(
                client,
                "v2.fetch_manifest_by_digest",
                url_for,
                "GET",
                {"repository": repository, "manifest_ref": alternative_digest},
                expected_code=400,
                headers=pull_headers.copy(),
            )
            assert disabled_alias.get_json()["errors"][0]["detail"] == {
                "algorithm": algorithm,
                "reason": "unsupported",
            }
        with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": [algorithm]}):
            for manifest_ref in (schema1.digest, alternative_digest):
                response = conduct_call(
                    client,
                    "v2.fetch_manifest_by_digest",
                    url_for,
                    "GET",
                    {"repository": repository, "manifest_ref": manifest_ref},
                    expected_code=400,
                    headers=pull_headers.copy(),
                )
                expected_algorithm = "sha256" if manifest_ref == schema1.digest else algorithm
                expected_reason = "disabled" if manifest_ref == schema1.digest else "unsupported"
                assert response.get_json()["errors"][0]["detail"] == {
                    "algorithm": expected_algorithm,
                    "reason": expected_reason,
                }
            tag_disabled = conduct_call(
                client,
                "v2.fetch_manifest_by_tagname",
                url_for,
                "GET",
                {"repository": repository, "manifest_ref": schema1.tag},
                expected_code=400,
                headers=pull_headers.copy(),
            )
            assert tag_disabled.get_json()["errors"][0]["detail"] == {
                "algorithm": "sha256",
                "reason": "disabled",
            }

        recovered = conduct_call(
            client,
            "v2.fetch_manifest_by_tagname",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": schema1.tag},
            expected_code=200,
            headers=pull_headers.copy(),
        )
        assert recovered.headers["Docker-Content-Digest"] == schema1.digest

    assert RepositoryManifestDigest.get(
        repository=repository_row,
        manifest=manifest,
        digest=schema1.digest,
    )
    assert RepositoryManifestDigest.get(
        repository=repository_row,
        manifest=manifest,
        digest=alternative_digest,
    )


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_story19_schema1_alias_delete_is_unsupported_but_canonical_delete_ignores_allowlist(
    algorithm, client, app
):
    repository, repository_row, manifest, schema1 = _publish_story19_schema1(
        client, f"story19-delete-{algorithm}"
    )
    alternative_digest = f"{algorithm}:" + "f" * hashlib.new(algorithm).digest_size * 2
    RepositoryManifestDigest.create(
        repository=repository_row,
        manifest=manifest,
        digest=alternative_digest,
    )
    before = _repository_publication_state(repository)

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": [algorithm]}):
        rejected = conduct_call(
            client,
            "v2.delete_manifest_by_digest",
            url_for,
            "DELETE",
            {"repository": repository, "manifest_ref": alternative_digest},
            expected_code=400,
            headers=_manifest_auth_headers(repository),
        )
        assert rejected.get_json()["errors"][0]["detail"] == {
            "algorithm": algorithm,
            "reason": "unsupported",
        }
        assert _repository_publication_state(repository) == before

        conduct_call(
            client,
            "v2.delete_manifest_by_digest",
            url_for,
            "DELETE",
            {"repository": repository, "manifest_ref": schema1.digest},
            expected_code=202,
            headers=_manifest_auth_headers(repository),
        )

    assert not filter_to_alive_tags(
        Tag.select().where(Tag.repository == repository_row, Tag.manifest == manifest),
        allow_hidden=True,
    ).exists()
    assert RepositoryManifestDigest.get(
        repository=repository_row,
        manifest=manifest,
        digest=alternative_digest,
    )


def _as_oci_manifest(manifest_info, algorithm="sha256"):
    manifest_dict = json.loads(manifest_info["bytes"])
    manifest_dict["mediaType"] = OCI_IMAGE_MANIFEST_CONTENT_TYPE
    manifest_dict["config"]["mediaType"] = OCI_IMAGE_CONFIG_CONTENT_TYPE
    manifest_dict["layers"][0]["mediaType"] = "application/vnd.oci.image.layer.v1.tar+gzip"
    manifest_bytes = json.dumps(manifest_dict, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    manifest_info.update(
        bytes=manifest_bytes,
        canonical_digest="sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
        external_digest=f"{algorithm}:" + hashlib.new(algorithm, manifest_bytes).hexdigest(),
        media_type=OCI_IMAGE_MANIFEST_CONTENT_TYPE,
    )
    return manifest_info


@pytest.mark.parametrize(
    "media_type",
    [DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE, OCI_IMAGE_MANIFEST_CONTENT_TYPE],
)
@pytest.mark.parametrize("layer_algorithm", ["sha384", "sha512"])
def test_story19_single_manifest_conversion_rejects_alternative_blob_sum_without_mutation(
    media_type, layer_algorithm, client, app
):
    repository = "devtable/simple"
    tag_name = f"story19-convert-{media_type.split('.')[-2]}-{layer_algorithm}"
    manifest_info = _sha512_single_manifest(
        repository,
        f"story19 {media_type} {layer_algorithm}".encode(),
        algorithm="sha256",
        config_algorithm="sha256",
        layer_algorithm=layer_algorithm,
    )
    if media_type == OCI_IMAGE_MANIFEST_CONTENT_TYPE:
        _as_oci_manifest(manifest_info)

    with patch.dict(
        realapp.config,
        {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
    ):
        _put_manifest(
            client,
            repository,
            manifest_info["external_digest"],
            manifest_info,
            tag=tag_name,
        )
        before = _repository_publication_state(repository)
        response = conduct_call(
            client,
            "v2.fetch_manifest_by_tagname",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": tag_name},
            expected_code=400,
            headers={
                **_manifest_auth_headers(repository, actions=("pull",)),
                "Accept": DOCKER_SCHEMA1_MANIFEST_CONTENT_TYPE,
            },
        )

    assert response.get_json()["errors"][0]["detail"] == {
        "algorithm": layer_algorithm,
        "reason": "unsupported",
    }
    assert _repository_publication_state(repository) == before


@pytest.mark.parametrize(
    "parent_media_type",
    [
        "application/vnd.docker.distribution.manifest.list.v2+json",
        OCI_IMAGE_INDEX_CONTENT_TYPE,
    ],
)
def test_story19_index_conversion_rejects_alternative_child_layer(parent_media_type, client, app):
    repository = "devtable/simple"
    child = _sha512_single_manifest(
        repository,
        b"story19 alternative index child",
        algorithm="sha256",
        config_algorithm="sha256",
        layer_algorithm="sha512",
    )
    child.update(
        media_type=DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
        descriptor_digest=child["external_digest"],
        architecture="amd64",
    )
    parent = _manifest_index(children=[child], media_type=parent_media_type, algorithm="sha256")
    tag_name = f"story19-index-{parent_media_type.split('.')[-2]}"

    with patch.dict(
        realapp.config,
        {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
    ):
        _put_manifest(client, repository, child["external_digest"], child)
        _put_manifest(client, repository, parent["external_digest"], parent, tag=tag_name)
        before = _repository_publication_state(repository)
        response = conduct_call(
            client,
            "v2.fetch_manifest_by_tagname",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": tag_name},
            expected_code=400,
            headers={
                **_manifest_auth_headers(repository, actions=("pull",)),
                "Accept": DOCKER_SCHEMA1_MANIFEST_CONTENT_TYPE,
            },
        )

    assert response.get_json()["errors"][0]["detail"] == {
        "algorithm": "sha512",
        "reason": "unsupported",
    }
    assert _repository_publication_state(repository) == before


@pytest.mark.parametrize(
    "source_media_type",
    [DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE, OCI_IMAGE_MANIFEST_CONTENT_TYPE],
)
def test_story19_all_sha256_conversion_preserves_existing_representation(
    source_media_type, client, app
):
    repository = "devtable/simple"
    tag_name = f"story19-sha256-convert-{source_media_type.split('.')[-2]}"
    manifest_info = _sha512_single_manifest(
        repository,
        f"story19 all sha256 {source_media_type}".encode(),
        algorithm="sha256",
    )
    if source_media_type == OCI_IMAGE_MANIFEST_CONTENT_TYPE:
        _as_oci_manifest(manifest_info)

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256"]}):
        _put_manifest(
            client,
            repository,
            manifest_info["external_digest"],
            manifest_info,
            tag=tag_name,
        )
        repository_ref = registry_model.lookup_repository("devtable", "simple")
        tag = registry_model.get_repo_tag(repository_ref, tag_name)
        source = registry_model.get_manifest_for_tag(tag)
        expected = registry_model.convert_manifest(
            source,
            "devtable",
            "simple",
            tag_name,
            {DOCKER_SCHEMA1_MANIFEST_CONTENT_TYPE},
            storage,
        )
        response = conduct_call(
            client,
            "v2.fetch_manifest_by_tagname",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": tag_name},
            expected_code=200,
            headers={
                **_manifest_auth_headers(repository, actions=("pull",)),
                "Accept": DOCKER_SCHEMA1_MANIFEST_CONTENT_TYPE,
            },
        )

    assert response.data == expected.bytes.as_encoded_str()
    assert response.headers["Content-Type"] == expected.media_type
    assert response.headers["Docker-Content-Digest"] == expected.digest
    assert all(str(layer.digest).startswith("sha256:") for layer in expected.layers)


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_story19_manifest_list_rejects_alternative_schema1_child_descriptor(algorithm, client, app):
    repository, repository_row, child, schema1 = _publish_story19_schema1(
        client, f"story19-list-child-{algorithm}"
    )
    alternative_digest = (
        f"{algorithm}:" + hashlib.new(algorithm, schema1.bytes.as_encoded_str()).hexdigest()
    )
    RepositoryManifestDigest.create(
        repository=repository_row,
        manifest=child,
        digest=alternative_digest,
    )
    parent_bytes = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json",
            "manifests": [
                {
                    "mediaType": DOCKER_SCHEMA1_MANIFEST_CONTENT_TYPE,
                    "size": len(schema1.bytes.as_encoded_str()),
                    "digest": alternative_digest,
                    "platform": {"architecture": "amd64", "os": "linux"},
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    parent = {
        "bytes": parent_bytes,
        "canonical_digest": "sha256:" + hashlib.sha256(parent_bytes).hexdigest(),
        "external_digest": "sha256:" + hashlib.sha256(parent_bytes).hexdigest(),
        "media_type": "application/vnd.docker.distribution.manifest.list.v2+json",
    }
    before = _repository_publication_state(repository)

    with patch.dict(
        realapp.config,
        {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
    ):
        response = _put_manifest(
            client,
            repository,
            parent["external_digest"],
            parent,
            expected_code=400,
            tag=f"story19-list-{algorithm}",
        )

    assert response.get_json()["errors"][0]["detail"] == {
        "algorithm": algorithm,
        "reason": "unsupported",
    }
    assert _repository_publication_state(repository) == before


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_story19_schema1_retarget_resigns_without_inheriting_alternative_alias(
    algorithm, client, app
):
    repository, repository_row, source_row, schema1 = _publish_story19_schema1(
        client, f"story19-retarget-source-{algorithm}"
    )
    alternative_digest = (
        f"{algorithm}:" + hashlib.new(algorithm, schema1.bytes.as_encoded_str()).hexdigest()
    )
    with pytest.raises(model.ManifestDigestConflictException):
        model.oci.manifest.register_repository_manifest_digest(
            repository_row.id, source_row, alternative_digest
        )
    RepositoryManifestDigest.create(
        repository=repository_row,
        manifest=source_row,
        digest=alternative_digest,
    )
    repository_ref = registry_model.lookup_repository("devtable", "simple")
    source_tag = registry_model.get_repo_tag(repository_ref, schema1.tag)
    source = registry_model.get_manifest_for_tag(source_tag)
    new_tag_name = f"story19-retargeted-{algorithm}"

    rewritten_tag = registry_model.retarget_tag(
        repository_ref,
        new_tag_name,
        source,
        storage,
        docker_v2_signing_key,
    )
    rewritten = rewritten_tag.manifest
    parsed = rewritten.get_parsed_manifest()

    assert rewritten._db_id != source._db_id
    assert parsed.is_signed
    assert parsed.tag == new_tag_name
    assert parsed.namespace == "devtable"
    assert parsed.repo_name == "simple"
    assert rewritten.digest.startswith("sha256:")
    assert (
        not RepositoryManifestDigest.select()
        .where(
            RepositoryManifestDigest.repository == repository_row,
            RepositoryManifestDigest.manifest == rewritten._db_id,
            RepositoryManifestDigest.digest == alternative_digest,
        )
        .exists()
    )

    with patch.dict(
        realapp.config,
        {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
    ):
        response = conduct_call(
            client,
            "v2.fetch_manifest_by_tagname",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": new_tag_name},
            expected_code=200,
            headers=_manifest_auth_headers(repository, actions=("pull",)),
        )

    assert response.data == rewritten.internal_manifest_bytes.as_encoded_str()
    assert response.headers["Docker-Content-Digest"] == rewritten.digest
    assert model.oci.manifest.get_repository_manifest_digests(
        repository_row.id, Manifest.get(id=rewritten._db_id)
    ) == [rewritten.digest]
    assert RepositoryManifestDigest.get(
        repository=repository_row,
        manifest=source_row,
        digest=alternative_digest,
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


@pytest.mark.parametrize("root_algorithm", ["sha256", "sha384", "sha512"])
@pytest.mark.parametrize(
    "parent_media_type",
    [
        OCI_IMAGE_INDEX_CONTENT_TYPE,
        "application/vnd.docker.distribution.manifest.list.v2+json",
    ],
)
def test_story6_mixed_digest_multiarchitecture_contract(
    root_algorithm, parent_media_type, client, app
):
    repository = "devtable/simple"
    other_repository = "devtable/complex"
    tag_name = f"story6-{root_algorithm}-{parent_media_type.split('.')[-2].replace('+json', '')}"
    child_specs = [
        ("sha256", "sha384", "sha512", "amd64"),
        ("sha384", "sha512", "sha256", "arm64"),
        ("sha512", "sha256", "sha384", "ppc64le"),
    ]
    children = []
    for manifest_algorithm, config_algorithm, layer_algorithm, architecture in child_specs:
        child = _sha512_single_manifest(
            repository,
            f"{parent_media_type}-{root_algorithm}-{manifest_algorithm}".encode("utf-8"),
            algorithm=manifest_algorithm,
            config_algorithm=config_algorithm,
            layer_algorithm=layer_algorithm,
        )
        child.update(
            media_type=DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
            descriptor_digest=child["external_digest"],
            architecture=architecture,
        )
        children.append(child)

    parent = _manifest_index(
        children,
        media_type=parent_media_type,
        algorithm=root_algorithm,
    )
    auth_headers = _manifest_auth_headers(repository)
    parent_headers = {
        **auth_headers,
        "Content-Type": parent_media_type,
        "Accept": parent_media_type,
    }
    child_headers = {
        **auth_headers,
        "Content-Type": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
        "Accept": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
    }
    test_cache = InMemoryDataModelCache(TEST_CACHE_CONFIG)

    with (
        patch.dict(
            realapp.config,
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
        ),
        patch("endpoints.v2.manifest.model_cache", test_cache),
        patch("endpoints.v2.blob.model_cache", test_cache),
    ):
        for child in children:
            _put_manifest(client, repository, child["external_digest"], child)

        for _ in range(2):
            response = _put_manifest(
                client,
                repository,
                parent["external_digest"],
                parent,
                tag=tag_name,
            )
            assert response.headers["Docker-Content-Digest"] == parent["external_digest"]
            assert response.headers["Location"].endswith("/manifests/" + parent["external_digest"])
            assert response.headers.getlist("OCI-Tag") == [tag_name]

        parent_paths = [
            ("v2.fetch_manifest_by_digest", parent["external_digest"]),
            ("v2.fetch_manifest_by_tagname", tag_name),
        ]
        for endpoint, manifest_ref in parent_paths:
            for method in ("GET", "HEAD"):
                response = conduct_call(
                    client,
                    endpoint,
                    url_for,
                    method,
                    {"repository": repository, "manifest_ref": manifest_ref},
                    expected_code=200,
                    headers=parent_headers.copy(),
                )
                assert response.headers["Docker-Content-Digest"] == parent["external_digest"]
                assert response.headers["Content-Type"] == parent_media_type
                assert response.data == (parent["bytes"] if method == "GET" else b"")
                if method == "GET":
                    assert (
                        f"{root_algorithm}:"
                        + hashlib.new(root_algorithm, response.data).hexdigest()
                        == parent["external_digest"]
                    )

        for child in children:
            child_algorithm = child["external_digest"].partition(":")[0]
            for method in ("GET", "HEAD"):
                response = conduct_call(
                    client,
                    "v2.fetch_manifest_by_digest",
                    url_for,
                    method,
                    {"repository": repository, "manifest_ref": child["external_digest"]},
                    expected_code=200,
                    headers=child_headers.copy(),
                )
                assert response.headers["Docker-Content-Digest"] == child["external_digest"]
                assert response.data == (child["bytes"] if method == "GET" else b"")
                if method == "GET":
                    assert (
                        f"{child_algorithm}:"
                        + hashlib.new(child_algorithm, response.data).hexdigest()
                        == child["external_digest"]
                    )

            for blob in child["referenced_blobs"]:
                blob_algorithm = blob["external_digest"].partition(":")[0]
                for method, endpoint in (
                    ("GET", "v2.download_blob"),
                    ("HEAD", "v2.check_blob_exists"),
                ):
                    response = conduct_call(
                        client,
                        endpoint,
                        url_for,
                        method,
                        {"repository": repository, "digest": blob["external_digest"]},
                        expected_code=200,
                        headers=auth_headers.copy(),
                    )
                    assert response.headers["Docker-Content-Digest"] == blob["external_digest"]
                    assert response.data == (blob["bytes"] if method == "GET" else b"")
                    if method == "GET":
                        assert (
                            f"{blob_algorithm}:"
                            + hashlib.new(blob_algorithm, response.data).hexdigest()
                            == blob["external_digest"]
                        )

        hidden_manifest_identities = [
            item["canonical_digest"]
            for item in [parent, *children]
            if item["canonical_digest"] != item["external_digest"]
        ]
        for hidden_digest in hidden_manifest_identities:
            for method in ("GET", "HEAD"):
                conduct_call(
                    client,
                    "v2.fetch_manifest_by_digest",
                    url_for,
                    method,
                    {"repository": repository, "manifest_ref": hidden_digest},
                    expected_code=404,
                    headers=auth_headers.copy(),
                )

        hidden_blobs = [
            blob
            for child in children
            for blob in child["referenced_blobs"]
            if blob["canonical_digest"] != blob["external_digest"]
        ]
        for blob in hidden_blobs:
            for method, endpoint in (
                ("GET", "v2.download_blob"),
                ("HEAD", "v2.check_blob_exists"),
            ):
                conduct_call(
                    client,
                    endpoint,
                    url_for,
                    method,
                    {"repository": repository, "digest": blob["canonical_digest"]},
                    expected_code=404,
                    headers=auth_headers.copy(),
                )

        isolated_paths = [
            ("v2.fetch_manifest_by_digest", {"manifest_ref": parent["external_digest"]}),
            ("v2.fetch_manifest_by_digest", {"manifest_ref": children[1]["external_digest"]}),
            ("v2.download_blob", {"digest": children[2]["referenced_blobs"][0]["external_digest"]}),
        ]
        for endpoint, params in isolated_paths:
            for method in ("GET", "HEAD"):
                conduct_call(
                    client,
                    endpoint,
                    url_for,
                    method,
                    {"repository": other_repository, **params},
                    expected_code=404,
                    headers=_manifest_auth_headers(other_repository, actions=("pull",)),
                )

        for endpoint, params in isolated_paths:
            for method in ("GET", "HEAD"):
                conduct_call(
                    client,
                    endpoint,
                    url_for,
                    method,
                    {"repository": repository, **params},
                    expected_code=401,
                    headers={"Accept": parent_media_type},
                )

        conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {
                "repository": repository,
                "manifest_ref": parent["external_digest"],
                "tag": "unauthorized-story6",
            },
            expected_code=401,
            headers={
                **_manifest_auth_headers(repository, actions=("pull",)),
                "Content-Type": parent_media_type,
            },
            raw_body=parent["bytes"],
        )

        with patch.dict(
            realapp.config,
            {
                "ALLOWED_HASH_ALGORITHMS": [
                    algorithm
                    for algorithm in ("sha256", "sha384", "sha512")
                    if algorithm != root_algorithm
                ]
            },
        ):
            disabled = conduct_call(
                client,
                "v2.fetch_manifest_by_digest",
                url_for,
                "GET",
                {"repository": repository, "manifest_ref": parent["external_digest"]},
                expected_code=400,
                headers=auth_headers.copy(),
            )
            assert disabled.get_json()["errors"][0]["detail"] == {
                "algorithm": root_algorithm,
                "reason": "disabled",
            }
            conduct_call(
                client,
                "v2.fetch_manifest_by_digest",
                url_for,
                "HEAD",
                {"repository": repository, "manifest_ref": parent["external_digest"]},
                expected_code=400,
                headers=auth_headers.copy(),
            )

        conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": parent["external_digest"]},
            expected_code=200,
            headers=parent_headers.copy(),
        )

    repository_row = model.repository.get_repository("devtable", "simple")
    parent_row = Manifest.get(repository=repository_row, digest=parent["canonical_digest"])
    child_rows = {
        child["canonical_digest"]: Manifest.get(
            repository=repository_row,
            digest=child["canonical_digest"],
        )
        for child in children
    }
    assert {
        relationship.child_manifest_id
        for relationship in ManifestChild.select().where(
            ManifestChild.repository == repository_row,
            ManifestChild.manifest == parent_row,
        )
    } == {child.id for child in child_rows.values()}
    assert (
        Tag.get(repository=repository_row, name=tag_name, lifetime_end_ms=None).manifest
        == parent_row
    )
    assert {
        registration.digest
        for registration in RepositoryManifestDigest.select().where(
            RepositoryManifestDigest.repository == repository_row,
            RepositoryManifestDigest.manifest == parent_row,
        )
    } == {parent["external_digest"]}

    for child in children:
        child_row = child_rows[child["canonical_digest"]]
        assert {
            registration.digest
            for registration in RepositoryManifestDigest.select().where(
                RepositoryManifestDigest.repository == repository_row,
                RepositoryManifestDigest.manifest == child_row,
            )
        } == {child["external_digest"]}
        assert {
            relationship.blob_id
            for relationship in ManifestBlob.select().where(
                ManifestBlob.repository == repository_row,
                ManifestBlob.manifest == child_row,
            )
        } == child["blob_ids"]
        for blob in child["referenced_blobs"]:
            blob_row = ImageStorage.get(content_checksum=blob["canonical_digest"])
            assert {
                registration.digest
                for registration in RepositoryBlobDigest.select().where(
                    RepositoryBlobDigest.repository == repository_row,
                    RepositoryBlobDigest.image_storage == blob_row,
                )
            } == {blob["external_digest"]}
            assert (
                ImageStorage.select()
                .where(ImageStorage.content_checksum == blob["canonical_digest"])
                .count()
                == 1
            )
            assert (
                ImageStoragePlacement.select()
                .where(ImageStoragePlacement.storage == blob_row)
                .count()
                == 1
            )


def test_story10_same_quay_copy_preserves_selected_mixed_digest_identities(client, app):
    source = "devtable/complex"
    destination = "devtable/building"
    unrelated_repository = "devtable/simple"
    source_tag = "story10-source"
    destination_tag = "story10-copy"
    children = []
    for manifest_algorithm, config_algorithm, layer_algorithm, architecture in [
        ("sha384", "sha512", "sha256", "amd64"),
        ("sha512", "sha384", "sha512", "arm64"),
    ]:
        child = _sha512_single_manifest(
            source,
            f"story10-{architecture}-layer".encode("utf-8"),
            algorithm=manifest_algorithm,
            config_algorithm=config_algorithm,
            layer_algorithm=layer_algorithm,
        )
        child.update(
            media_type=DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
            descriptor_digest=child["external_digest"],
            architecture=architecture,
        )
        children.append(child)
    parent = _manifest_index(children, algorithm="sha512")
    source_headers = _manifest_auth_headers(source)
    destination_headers = _manifest_auth_headers(destination)
    copy_headers = _copy_auth_headers(source, destination)

    def unrelated_digest(content, selected_digest):
        algorithm = "sha384" if not selected_digest.startswith("sha384:") else "sha512"
        return f"{algorithm}:" + hashlib.new(algorithm, content).hexdigest()

    with patch.dict(
        realapp.config,
        {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
    ):
        for child in children:
            _put_manifest(client, source, child["external_digest"], child)
        _put_manifest(
            client,
            source,
            parent["external_digest"],
            parent,
            tag=source_tag,
        )

        source_repository = model.repository.get_repository("devtable", "complex")
        source_unrelated_digests = []
        for manifest_info in [parent, *children]:
            manifest_row = Manifest.get(
                repository=source_repository,
                digest=manifest_info["canonical_digest"],
            )
            alias = unrelated_digest(manifest_info["bytes"], manifest_info["external_digest"])
            model.oci.manifest.register_repository_manifest_digest(
                source_repository.id,
                manifest_row,
                alias,
            )
            source_unrelated_digests.append(alias)
        for child in children:
            for blob in child["referenced_blobs"]:
                blob_row = ImageStorage.get(content_checksum=blob["canonical_digest"])
                alias = unrelated_digest(blob["bytes"], blob["external_digest"])
                model.oci.blob.register_repository_blob_digest(
                    source_repository,
                    blob_row,
                    alias,
                )
                source_unrelated_digests.append(alias)

        source_root = conduct_call(
            client,
            "v2.fetch_manifest_by_tagname",
            url_for,
            "GET",
            {"repository": source, "manifest_ref": source_tag},
            expected_code=200,
            headers={
                **source_headers,
                "Accept": OCI_IMAGE_INDEX_CONTENT_TYPE,
            },
        )
        assert source_root.data == parent["bytes"]
        assert source_root.headers["Docker-Content-Digest"] == parent["external_digest"]

        source_child_responses = []
        for descriptor in json.loads(source_root.data)["manifests"]:
            response = conduct_call(
                client,
                "v2.fetch_manifest_by_digest",
                url_for,
                "GET",
                {"repository": source, "manifest_ref": descriptor["digest"]},
                expected_code=200,
                headers={
                    **source_headers,
                    "Accept": descriptor["mediaType"],
                },
            )
            assert len(response.data) == descriptor["size"]
            assert response.headers["Docker-Content-Digest"] == descriptor["digest"]
            source_child_responses.append((descriptor, response))

        image_storage_count = ImageStorage.select().count()
        placement_count = ImageStoragePlacement.select().count()
        expected_blob_digests = set()
        for _ in range(2):
            for child_descriptor, child_response in source_child_responses:
                child_document = json.loads(child_response.data)
                for blob_descriptor in [
                    child_document["config"],
                    *child_document["layers"],
                ]:
                    expected_blob_digests.add(blob_descriptor["digest"])
                    mounted = conduct_call(
                        client,
                        "v2.start_blob_upload",
                        url_for,
                        "POST",
                        {
                            "repository": destination,
                            "mount": blob_descriptor["digest"],
                            "from": source,
                        },
                        expected_code=201,
                        headers=copy_headers.copy(),
                    )
                    assert mounted.headers["Docker-Content-Digest"] == blob_descriptor["digest"]
                    assert mounted.headers["Location"].endswith(
                        "/blobs/" + blob_descriptor["digest"]
                    )
                    assert "Docker-Upload-UUID" not in mounted.headers

                copied_child = _put_manifest(
                    client,
                    destination,
                    child_descriptor["digest"],
                    {
                        "bytes": child_response.data,
                        "media_type": child_descriptor["mediaType"],
                    },
                )
                assert copied_child.headers["Docker-Content-Digest"] == child_descriptor["digest"]

            copied_root = _put_manifest(
                client,
                destination,
                source_root.headers["Docker-Content-Digest"],
                {
                    "bytes": source_root.data,
                    "media_type": source_root.headers["Content-Type"],
                },
                tag=destination_tag,
            )
            assert copied_root.headers["Docker-Content-Digest"] == parent["external_digest"]
            assert copied_root.headers.getlist("OCI-Tag") == [destination_tag]

        assert ImageStorage.select().count() == image_storage_count
        assert ImageStoragePlacement.select().count() == placement_count

        destination_repository = model.repository.get_repository("devtable", "building")
        copied_manifests = {}
        for manifest_info in [parent, *children]:
            manifest_row = Manifest.get(
                repository=destination_repository,
                digest=manifest_info["canonical_digest"],
            )
            copied_manifests[manifest_info["canonical_digest"]] = manifest_row
            assert {
                registration.digest
                for registration in RepositoryManifestDigest.select().where(
                    RepositoryManifestDigest.repository == destination_repository,
                    RepositoryManifestDigest.manifest == manifest_row,
                )
            } == {manifest_info["external_digest"]}

        copied_parent = copied_manifests[parent["canonical_digest"]]
        assert {
            relationship.child_manifest_id
            for relationship in ManifestChild.select().where(
                ManifestChild.repository == destination_repository,
                ManifestChild.manifest == copied_parent,
            )
        } == {copied_manifests[child["canonical_digest"]].id for child in children}
        assert (
            Tag.get(
                repository=destination_repository,
                name=destination_tag,
                lifetime_end_ms=None,
            ).manifest
            == copied_parent
        )

        for child in children:
            child_row = copied_manifests[child["canonical_digest"]]
            assert {
                relationship.blob_id
                for relationship in ManifestBlob.select().where(
                    ManifestBlob.repository == destination_repository,
                    ManifestBlob.manifest == child_row,
                )
            } == child["blob_ids"]
            for blob in child["referenced_blobs"]:
                blob_row = ImageStorage.get(content_checksum=blob["canonical_digest"])
                assert {
                    registration.digest
                    for registration in RepositoryBlobDigest.select().where(
                        RepositoryBlobDigest.repository == destination_repository,
                        RepositoryBlobDigest.image_storage == blob_row,
                    )
                } == {blob["external_digest"]}

        assert {
            registration.digest
            for registration in RepositoryBlobDigest.select().where(
                RepositoryBlobDigest.repository == destination_repository,
            )
        } == expected_blob_digests
        for alias in source_unrelated_digests:
            assert (
                not RepositoryManifestDigest.select()
                .where(
                    RepositoryManifestDigest.repository == destination_repository,
                    RepositoryManifestDigest.digest == alias,
                )
                .exists()
            )
            assert (
                not RepositoryBlobDigest.select()
                .where(
                    RepositoryBlobDigest.repository == destination_repository,
                    RepositoryBlobDigest.digest == alias,
                )
                .exists()
            )

        for endpoint, manifest_ref in [
            ("v2.fetch_manifest_by_tagname", destination_tag),
            ("v2.fetch_manifest_by_digest", parent["external_digest"]),
        ]:
            copied = conduct_call(
                client,
                endpoint,
                url_for,
                "GET",
                {"repository": destination, "manifest_ref": manifest_ref},
                expected_code=200,
                headers={
                    **destination_headers,
                    "Accept": OCI_IMAGE_INDEX_CONTENT_TYPE,
                },
            )
            assert copied.data == parent["bytes"]
            assert copied.headers["Docker-Content-Digest"] == parent["external_digest"]

        for child_descriptor, child_response in source_child_responses:
            copied_child = conduct_call(
                client,
                "v2.fetch_manifest_by_digest",
                url_for,
                "GET",
                {
                    "repository": destination,
                    "manifest_ref": child_descriptor["digest"],
                },
                expected_code=200,
                headers=destination_headers.copy(),
            )
            assert copied_child.data == child_response.data
            assert copied_child.headers["Docker-Content-Digest"] == child_descriptor["digest"]
            for blob_descriptor in [
                json.loads(child_response.data)["config"],
                *json.loads(child_response.data)["layers"],
            ]:
                copied_blob = conduct_call(
                    client,
                    "v2.download_blob",
                    url_for,
                    "GET",
                    {
                        "repository": destination,
                        "digest": blob_descriptor["digest"],
                    },
                    expected_code=200,
                    headers=destination_headers.copy(),
                )
                algorithm = blob_descriptor["digest"].partition(":")[0]
                assert (
                    f"{algorithm}:" + hashlib.new(algorithm, copied_blob.data).hexdigest()
                    == blob_descriptor["digest"]
                )

        hidden_canonical_digests = [
            manifest_info["canonical_digest"]
            for manifest_info in [parent, *children]
            if manifest_info["canonical_digest"] != manifest_info["external_digest"]
        ]
        for canonical_digest in hidden_canonical_digests:
            conduct_call(
                client,
                "v2.fetch_manifest_by_digest",
                url_for,
                "GET",
                {"repository": destination, "manifest_ref": canonical_digest},
                expected_code=404,
                headers=destination_headers.copy(),
            )
        for child in children:
            for blob in child["referenced_blobs"]:
                if blob["canonical_digest"] == blob["external_digest"]:
                    continue
                conduct_call(
                    client,
                    "v2.download_blob",
                    url_for,
                    "GET",
                    {"repository": destination, "digest": blob["canonical_digest"]},
                    expected_code=404,
                    headers=destination_headers.copy(),
                )

        conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {
                "repository": unrelated_repository,
                "manifest_ref": parent["external_digest"],
            },
            expected_code=404,
            headers=_manifest_auth_headers(unrelated_repository, actions=("pull",)),
        )

        with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256"]}):
            disabled = conduct_call(
                client,
                "v2.fetch_manifest_by_tagname",
                url_for,
                "GET",
                {"repository": destination, "manifest_ref": destination_tag},
                expected_code=400,
                headers=destination_headers.copy(),
            )
            assert disabled.get_json()["errors"][0]["detail"] == {
                "algorithm": "sha512",
                "reason": "disabled",
            }

        restored = conduct_call(
            client,
            "v2.fetch_manifest_by_tagname",
            url_for,
            "GET",
            {"repository": destination, "manifest_ref": destination_tag},
            expected_code=200,
            headers={
                **destination_headers,
                "Accept": OCI_IMAGE_INDEX_CONTENT_TYPE,
            },
        )
        assert restored.headers["Docker-Content-Digest"] == parent["external_digest"]


@pytest.mark.parametrize(
    "parent_media_type",
    [
        OCI_IMAGE_INDEX_CONTENT_TYPE,
        "application/vnd.docker.distribution.manifest.list.v2+json",
    ],
)
@pytest.mark.parametrize(
    "mismatch_field,expected_field",
    [("size", "size"), ("mediaType", "media type")],
)
def test_story6_parent_descriptor_mismatch_rolls_back_publication(
    parent_media_type, mismatch_field, expected_field, client, app
):
    repository = "devtable/simple"
    tag_name = f"story6-mismatch-{mismatch_field}-{parent_media_type.split('.')[-2]}"
    child = _sha512_single_manifest(
        repository,
        b"story6 descriptor mismatch child",
        algorithm="sha384",
        config_algorithm="sha512",
        layer_algorithm="sha256",
    )
    child.update(
        media_type=DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
        descriptor_digest=child["external_digest"],
        architecture="amd64",
    )
    parent = _manifest_index([child], media_type=parent_media_type, algorithm="sha512")
    parent_dict = json.loads(parent["bytes"])
    if mismatch_field == "size":
        parent_dict["manifests"][0]["size"] += 1
    else:
        parent_dict["manifests"][0]["mediaType"] = (
            OCI_IMAGE_MANIFEST_CONTENT_TYPE
            if parent_media_type == OCI_IMAGE_INDEX_CONTENT_TYPE
            else DOCKER_SCHEMA1_MANIFEST_CONTENT_TYPE
        )
    parent["bytes"] = json.dumps(parent_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
    parent["canonical_digest"] = "sha256:" + hashlib.sha256(parent["bytes"]).hexdigest()
    parent["external_digest"] = "sha512:" + hashlib.sha512(parent["bytes"]).hexdigest()

    with patch.dict(
        realapp.config,
        {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
    ):
        _put_manifest(client, repository, child["external_digest"], child)
        response = _put_manifest(
            client,
            repository,
            parent["external_digest"],
            parent,
            expected_code=400,
            tag=tag_name,
        )

    error = response.get_json()["errors"][0]
    assert error["code"] == "MANIFEST_INVALID"
    assert error["detail"] == {
        "digest": child["external_digest"],
        "reason": "descriptor_mismatch",
        "field": expected_field,
    }
    repository_row = model.repository.get_repository("devtable", "simple")
    assert (
        not Manifest.select()
        .where(
            Manifest.repository == repository_row,
            Manifest.digest == parent["canonical_digest"],
        )
        .exists()
    )
    assert (
        not RepositoryManifestDigest.select()
        .where(
            RepositoryManifestDigest.repository == repository_row,
            RepositoryManifestDigest.digest == parent["external_digest"],
        )
        .exists()
    )
    assert (
        not Tag.select()
        .where(
            Tag.repository == repository_row,
            Tag.name == tag_name,
            Tag.lifetime_end_ms.is_null(True),
        )
        .exists()
    )


def test_story6_parent_registration_conflict_rolls_back_graph_and_tag(client, app):
    repository = "devtable/simple"
    tag_name = "story6-parent-conflict"
    child = _sha512_single_manifest(
        repository,
        b"story6 parent conflict child",
        algorithm="sha384",
        config_algorithm="sha512",
        layer_algorithm="sha256",
    )
    child.update(
        media_type=DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
        descriptor_digest=child["external_digest"],
        architecture="amd64",
    )
    parent = _manifest_index([child], algorithm="sha512")
    repository_row = model.repository.get_repository("devtable", "simple")
    existing_tag = registry_model.get_repo_tag(
        registry_model.lookup_repository("devtable", "simple"), "latest"
    )
    existing_manifest = Manifest.get_by_id(existing_tag.manifest.id)

    with patch.dict(
        realapp.config,
        {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
    ):
        _put_manifest(client, repository, child["external_digest"], child)
        RepositoryManifestDigest.create(
            repository=repository_row,
            manifest=existing_manifest,
            digest=parent["external_digest"],
        )
        previous_transaction_factory = db_transaction.obj
        db_transaction.initialize(lambda: db.transaction())
        try:
            response = _put_manifest(
                client,
                repository,
                parent["external_digest"],
                parent,
                expected_code=400,
                tag=tag_name,
            )
        finally:
            db_transaction.initialize(previous_transaction_factory)

    error = response.get_json()["errors"][0]
    assert error["code"] == "DIGEST_INVALID"
    assert error["detail"]["reason"] == "conflict"
    assert (
        RepositoryManifestDigest.get(
            repository=repository_row,
            digest=parent["external_digest"],
        ).manifest
        == existing_manifest
    )
    assert (
        not Manifest.select()
        .where(
            Manifest.repository == repository_row,
            Manifest.digest == parent["canonical_digest"],
        )
        .exists()
    )
    assert (
        not Tag.select()
        .where(
            Tag.repository == repository_row,
            Tag.name == tag_name,
            Tag.lifetime_end_ms.is_null(True),
        )
        .exists()
    )


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
            tag="story6-unknown-child",
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
    assert (
        not Tag.select()
        .where(
            Tag.repository == repository,
            Tag.name == "story6-unknown-child",
            Tag.lifetime_end_ms.is_null(True),
        )
        .exists()
    )


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_alternative_referrer_subject_query_is_disabled_when_not_allowed(algorithm, client, app):
    repository = "devtable/simple"
    digest = f"{algorithm}:" + "a" * hashlib.new(algorithm).digest_size * 2

    with (
        patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256"]}),
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
        "message": "digest algorithm is disabled by registry configuration",
        "detail": {"algorithm": algorithm, "reason": "disabled"},
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


@pytest.mark.parametrize(
    "digest,allowed_algorithms,expected_reason",
    [
        ("sha384:" + "a" * 95, ["sha256", "sha384"], "malformed"),
        ("sha999:" + "a" * 96, ["sha256", "sha384", "sha512"], "unsupported"),
        ("sha512:" + "a" * 128, ["sha256", "sha384"], "disabled"),
    ],
)
def test_story8_referrer_digest_rejection_precedes_repository_lookup(
    digest, allowed_algorithms, expected_reason, client, app
):
    repository = "devtable/simple"
    with (
        patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": allowed_algorithms}),
        patch.object(registry_model, "lookup_repository") as lookup_repository,
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

    lookup_repository.assert_not_called()
    assert response.get_json()["errors"][0]["detail"]["reason"] == expected_reason


@pytest.mark.parametrize(
    "artifact_algorithm,subject_algorithm",
    [("sha256", "sha384"), ("sha384", "sha512"), ("sha512", "sha256")],
)
def test_story7_registered_artifact_push_pull_contract(
    artifact_algorithm, subject_algorithm, client, app
):
    repository = "devtable/simple"
    other_repository = "devtable/complex"
    tag_name = f"story7-{artifact_algorithm}-{subject_algorithm}"
    subject = _sha512_single_manifest(
        repository,
        b"story7 artifact subject",
        algorithm=subject_algorithm,
    )
    subject["media_type"] = DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
    artifact = _oci_artifact(repository, subject, algorithm=artifact_algorithm)
    auth_headers = _manifest_auth_headers(repository)
    artifact_headers = {
        **auth_headers,
        "Content-Type": OCI_IMAGE_MANIFEST_CONTENT_TYPE,
        "Accept": OCI_IMAGE_MANIFEST_CONTENT_TYPE,
    }
    test_cache = InMemoryDataModelCache(TEST_CACHE_CONFIG)

    with (
        patch.dict(
            realapp.config,
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
        ),
        patch("endpoints.v2.manifest.model_cache", test_cache),
    ):
        _put_manifest(client, repository, subject["external_digest"], subject)

        conduct_call(
            client,
            "v2.write_manifest_by_digest",
            url_for,
            "PUT",
            {
                "repository": repository,
                "manifest_ref": artifact["external_digest"],
                "tag": "unauthorized-story7",
            },
            expected_code=401,
            headers={
                **_manifest_auth_headers(repository, actions=("pull",)),
                "Content-Type": OCI_IMAGE_MANIFEST_CONTENT_TYPE,
            },
            raw_body=artifact["bytes"],
        )

        for _ in range(2):
            pushed = _put_manifest(
                client,
                repository,
                artifact["external_digest"],
                artifact,
                tag=tag_name,
            )
            assert pushed.headers["Docker-Content-Digest"] == artifact["external_digest"]
            assert pushed.headers["Location"].endswith("/manifests/" + artifact["external_digest"])
            assert pushed.headers.getlist("OCI-Tag") == [tag_name]

        for endpoint, manifest_ref in (
            ("v2.fetch_manifest_by_digest", artifact["external_digest"]),
            ("v2.fetch_manifest_by_tagname", tag_name),
        ):
            for method in ("GET", "HEAD"):
                pulled = conduct_call(
                    client,
                    endpoint,
                    url_for,
                    method,
                    {"repository": repository, "manifest_ref": manifest_ref},
                    expected_code=200,
                    headers=artifact_headers.copy(),
                )
                assert pulled.headers["Docker-Content-Digest"] == artifact["external_digest"]
                assert pulled.headers["Content-Type"] == OCI_IMAGE_MANIFEST_CONTENT_TYPE
                assert pulled.data == (artifact["bytes"] if method == "GET" else b"")
                if method == "GET":
                    assert (
                        f"{artifact_algorithm}:"
                        + hashlib.new(artifact_algorithm, pulled.data).hexdigest()
                        == artifact["external_digest"]
                    )

        for method in ("GET", "HEAD"):
            conduct_call(
                client,
                "v2.fetch_manifest_by_digest",
                url_for,
                method,
                {"repository": repository, "manifest_ref": artifact["external_digest"]},
                expected_code=401,
                headers={"Accept": OCI_IMAGE_MANIFEST_CONTENT_TYPE},
            )
            conduct_call(
                client,
                "v2.fetch_manifest_by_digest",
                url_for,
                method,
                {"repository": other_repository, "manifest_ref": artifact["external_digest"]},
                expected_code=404,
                headers=_manifest_auth_headers(other_repository, actions=("pull",)),
            )

        if artifact["canonical_digest"] != artifact["external_digest"]:
            for method in ("GET", "HEAD"):
                conduct_call(
                    client,
                    "v2.fetch_manifest_by_digest",
                    url_for,
                    method,
                    {"repository": repository, "manifest_ref": artifact["canonical_digest"]},
                    expected_code=404,
                    headers=auth_headers.copy(),
                )

        if subject["canonical_digest"] != subject["external_digest"]:
            conduct_call(
                client,
                "v2.fetch_manifest_by_digest",
                url_for,
                "GET",
                {"repository": repository, "manifest_ref": subject["canonical_digest"]},
                expected_code=404,
                headers=auth_headers.copy(),
            )

        with patch.dict(
            realapp.config,
            {
                "ALLOWED_HASH_ALGORITHMS": [
                    algorithm
                    for algorithm in ("sha256", "sha384", "sha512")
                    if algorithm != artifact_algorithm
                ]
            },
        ):
            disabled = conduct_call(
                client,
                "v2.fetch_manifest_by_tagname",
                url_for,
                "GET",
                {"repository": repository, "manifest_ref": tag_name},
                expected_code=400,
                headers=artifact_headers.copy(),
            )
            assert disabled.get_json()["errors"][0]["detail"] == {
                "algorithm": artifact_algorithm,
                "reason": "disabled",
            }

        conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": artifact["external_digest"]},
            expected_code=200,
            headers=artifact_headers.copy(),
        )

    repository_row = model.repository.get_repository("devtable", "simple")
    subject_row = Manifest.get(
        repository=repository_row,
        digest=subject["canonical_digest"],
    )
    artifact_row = Manifest.get(
        repository=repository_row,
        digest=artifact["canonical_digest"],
    )
    assert artifact_row.subject == subject_row.digest
    assert artifact_row.artifact_type == "application/vnd.example.signature"
    assert artifact_row.manifest_bytes.encode("utf-8") == artifact["bytes"]
    assert {
        registration.digest
        for registration in RepositoryManifestDigest.select().where(
            RepositoryManifestDigest.repository == repository_row,
            RepositoryManifestDigest.manifest == artifact_row,
        )
    } == {artifact["external_digest"]}
    assert {
        relationship.blob_id
        for relationship in ManifestBlob.select().where(
            ManifestBlob.repository == repository_row,
            ManifestBlob.manifest == artifact_row,
        )
    } == artifact["blob_ids"]
    assert (
        Tag.get(repository=repository_row, name=tag_name, lifetime_end_ms=None).manifest
        == artifact_row
    )


@pytest.mark.parametrize("subject_algorithm", ["sha384", "sha512"])
def test_story7_tag_addressed_artifact_accepts_registered_subject(subject_algorithm, client, app):
    repository = "devtable/simple"
    tag_name = f"story7-tag-{subject_algorithm}"
    subject = _sha512_single_manifest(
        repository,
        b"story7 tag-addressed subject",
        algorithm=subject_algorithm,
    )
    subject["media_type"] = DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
    artifact = _oci_artifact(repository, subject, algorithm="sha256")
    headers = {
        **_manifest_auth_headers(repository),
        "Content-Type": OCI_IMAGE_MANIFEST_CONTENT_TYPE,
        "Accept": OCI_IMAGE_MANIFEST_CONTENT_TYPE,
    }

    with patch.dict(
        realapp.config,
        {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
    ):
        _put_manifest(client, repository, subject["external_digest"], subject)
        pushed = conduct_call(
            client,
            "v2.write_manifest_by_tagname",
            url_for,
            "PUT",
            {"repository": repository, "manifest_ref": tag_name},
            expected_code=201,
            headers=headers.copy(),
            raw_body=artifact["bytes"],
        )
        pulled = conduct_call(
            client,
            "v2.fetch_manifest_by_tagname",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": tag_name},
            expected_code=200,
            headers=headers.copy(),
        )

    assert pushed.headers["Docker-Content-Digest"] == artifact["canonical_digest"]
    assert pulled.headers["Docker-Content-Digest"] == artifact["canonical_digest"]
    assert pulled.data == artifact["bytes"]
    repository_row = model.repository.get_repository("devtable", "simple")
    artifact_row = Manifest.get(repository=repository_row, digest=artifact["canonical_digest"])
    assert artifact_row.subject == subject["canonical_digest"]


@pytest.mark.parametrize(
    "failure,expected_code,expected_field",
    [
        ("unknown", "MANIFEST_BLOB_UNKNOWN", None),
        ("cross-repository", "MANIFEST_BLOB_UNKNOWN", None),
        ("size", "MANIFEST_INVALID", "size"),
        ("media-type", "MANIFEST_INVALID", "media type"),
    ],
)
def test_story7_subject_descriptor_validation_prevents_artifact_publication(
    failure, expected_code, expected_field, client, app
):
    repository = "devtable/simple"
    subject_repository = "devtable/complex" if failure == "cross-repository" else repository
    subject = _sha512_single_manifest(
        subject_repository,
        f"story7 {failure} subject".encode("utf-8"),
        algorithm="sha384",
    )
    subject["media_type"] = DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
    artifact = _oci_artifact(repository, subject, algorithm="sha512")

    with patch.dict(
        realapp.config,
        {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
    ):
        if failure != "unknown":
            _put_manifest(
                client,
                subject_repository,
                subject["external_digest"],
                subject,
            )

        artifact_dict = json.loads(artifact["bytes"])
        if failure == "unknown":
            artifact_dict["subject"]["digest"] = "sha384:" + "0" * 96
        elif failure == "size":
            artifact_dict["subject"]["size"] += 1
        elif failure == "media-type":
            artifact_dict["subject"]["mediaType"] = OCI_IMAGE_INDEX_CONTENT_TYPE

        artifact["bytes"] = json.dumps(
            artifact_dict,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        artifact["canonical_digest"] = "sha256:" + hashlib.sha256(artifact["bytes"]).hexdigest()
        artifact["external_digest"] = "sha512:" + hashlib.sha512(artifact["bytes"]).hexdigest()
        before = _repository_publication_state(repository)
        response = _put_manifest(
            client,
            repository,
            artifact["external_digest"],
            artifact,
            expected_code=400,
            tag=f"story7-invalid-{failure}",
        )

    error = response.get_json()["errors"][0]
    assert error["code"] == expected_code
    if expected_field is None:
        assert error["detail"] == {
            "digest": artifact_dict["subject"]["digest"],
            "descriptor": "subject",
        }
    else:
        assert error["detail"] == {
            "digest": artifact_dict["subject"]["digest"],
            "reason": "descriptor_mismatch",
            "field": expected_field,
        }
    assert _repository_publication_state(repository) == before


@pytest.mark.parametrize(
    "subject_algorithm,artifact_algorithm,publication",
    [
        ("sha256", "sha384", "digest"),
        ("sha384", "sha512", "digest"),
        ("sha512", "sha256", "digest"),
        ("sha512", "sha384", "tag"),
    ],
)
def test_story8_discovers_registered_referrers(
    subject_algorithm, artifact_algorithm, publication, client, app
):
    repository = "devtable/simple"
    subject = _sha512_single_manifest(
        repository,
        f"story8 {subject_algorithm} subject".encode(),
        algorithm=subject_algorithm,
    )
    subject["media_type"] = DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
    artifact = _oci_artifact(
        repository,
        subject,
        layer_bytes=f"story8 {artifact_algorithm} {publication} artifact".encode(),
        algorithm=artifact_algorithm,
    )
    pull_headers = _manifest_auth_headers(repository, actions=("pull",))
    test_cache = InMemoryDataModelCache(TEST_CACHE_CONFIG)

    with (
        patch.dict(
            realapp.config,
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
        ),
        patch("endpoints.v2.manifest.model_cache", test_cache),
        patch("endpoints.v2.referrers.model_cache", test_cache),
        toggle_feature("REFERRERS_API", True),
    ):
        _put_manifest(client, repository, subject["external_digest"], subject)
        if publication == "digest":
            _put_manifest(client, repository, artifact["external_digest"], artifact)
            expected_artifact_digest = artifact["external_digest"]
        else:
            conduct_call(
                client,
                "v2.write_manifest_by_tagname",
                url_for,
                "PUT",
                {"repository": repository, "manifest_ref": "story8-artifact"},
                expected_code=201,
                headers={
                    **_manifest_auth_headers(repository),
                    "Content-Type": OCI_IMAGE_MANIFEST_CONTENT_TYPE,
                },
                raw_body=artifact["bytes"],
            )
            expected_artifact_digest = artifact["canonical_digest"]

        response = conduct_call(
            client,
            "v2.list_manifest_referrers",
            url_for,
            "GET",
            {
                "repository": repository,
                "manifest_ref": subject["external_digest"],
                "artifactType": "application/vnd.example.signature",
            },
            headers=pull_headers.copy(),
        )
        pulled = conduct_call(
            client,
            "v2.fetch_manifest_by_digest",
            url_for,
            "GET",
            {
                "repository": repository,
                "manifest_ref": expected_artifact_digest,
            },
            headers={
                **pull_headers,
                "Accept": OCI_IMAGE_MANIFEST_CONTENT_TYPE,
            },
        )

    assert response.headers["OCI-Filters-Applied"] == "artifactType"
    descriptors = response.get_json()["manifests"]
    assert descriptors == [
        {
            "artifactType": "application/vnd.example.signature",
            "digest": expected_artifact_digest,
            "mediaType": OCI_IMAGE_MANIFEST_CONTENT_TYPE,
            "size": len(artifact["bytes"]),
        }
    ]
    assert pulled.data == artifact["bytes"]
    assert pulled.headers["Docker-Content-Digest"] == expected_artifact_digest


@pytest.mark.parametrize("publication", ["tag-route", "digest-tag-parameter"])
def test_story8_sha512_fallback_tag_route(publication, client, app):
    repository = "devtable/simple"
    subject = _sha512_single_manifest(
        repository,
        f"story8 sha512 fallback subject {publication}".encode(),
        algorithm="sha512",
    )
    subject["media_type"] = DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
    artifact = _oci_artifact(
        repository,
        subject,
        layer_bytes=f"story8 sha512 fallback artifact {publication}".encode(),
        algorithm="sha512",
    )
    fallback_index = _manifest_index(
        [
            {
                "media_type": OCI_IMAGE_MANIFEST_CONTENT_TYPE,
                "bytes": artifact["bytes"],
                "descriptor_digest": artifact["external_digest"],
                "architecture": "amd64",
            }
        ],
        algorithm="sha256",
    )
    fallback_tag = subject["external_digest"].replace(":", "-", 1)

    with patch.dict(
        realapp.config,
        {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
    ):
        _put_manifest(client, repository, subject["external_digest"], subject)
        _put_manifest(client, repository, artifact["external_digest"], artifact)
        if publication == "tag-route":
            conduct_call(
                client,
                "v2.write_manifest_by_tagname",
                url_for,
                "PUT",
                {"repository": repository, "manifest_ref": fallback_tag},
                expected_code=201,
                headers={
                    **_manifest_auth_headers(repository),
                    "Content-Type": OCI_IMAGE_INDEX_CONTENT_TYPE,
                },
                raw_body=fallback_index["bytes"],
            )
        else:
            _put_manifest(
                client,
                repository,
                fallback_index["external_digest"],
                fallback_index,
                tag=fallback_tag,
            )
        pulled = conduct_call(
            client,
            "v2.fetch_manifest_by_tagname",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": fallback_tag},
            headers={
                **_manifest_auth_headers(repository, actions=("pull",)),
                "Accept": OCI_IMAGE_INDEX_CONTENT_TYPE,
            },
        )

    with patch.dict(
        realapp.config,
        {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384"]},
    ):
        disabled = conduct_call(
            client,
            "v2.fetch_manifest_by_tagname",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": fallback_tag},
            expected_code=400,
            headers=_manifest_auth_headers(repository, actions=("pull",)),
        )

    assert len(fallback_tag) == 135
    assert pulled.data == fallback_index["bytes"]
    assert disabled.get_json()["errors"][0]["detail"] == {
        "algorithm": "sha512",
        "reason": "disabled",
    }


def test_story8_aliases_cache_hard_disable_and_repository_isolation(client, app):
    repository = "devtable/simple"
    other_repository = "devtable/complex"
    subject = _sha512_single_manifest(
        repository,
        b"story8 multi-alias subject",
        algorithm="sha512",
    )
    subject["media_type"] = DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
    artifact = _oci_artifact(
        repository,
        subject,
        layer_bytes=b"story8 multi-alias artifact",
        algorithm="sha512",
    )
    headers = _manifest_auth_headers(repository, actions=("pull",))
    other_headers = _manifest_auth_headers(other_repository, actions=("pull",))
    test_cache = InMemoryDataModelCache(TEST_CACHE_CONFIG)

    with (
        patch("endpoints.v2.manifest.model_cache", test_cache),
        patch("endpoints.v2.referrers.model_cache", test_cache),
        toggle_feature("REFERRERS_API", True),
    ):
        with patch.dict(
            realapp.config,
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
        ):
            _put_manifest(client, repository, subject["external_digest"], subject)
            _put_manifest(client, repository, artifact["external_digest"], artifact)
            subject_sha384 = _register_manifest_alias(
                repository,
                subject["canonical_digest"],
                subject["bytes"],
                "sha384",
            )
            artifact_sha384 = _register_manifest_alias(
                repository,
                artifact["canonical_digest"],
                artifact["bytes"],
                "sha384",
            )

            for subject_digest in (subject["external_digest"], subject_sha384):
                response = conduct_call(
                    client,
                    "v2.list_manifest_referrers",
                    url_for,
                    "GET",
                    {"repository": repository, "manifest_ref": subject_digest},
                    headers=headers.copy(),
                )
                assert response.get_json()["manifests"][0]["digest"] == artifact["external_digest"]

            for rejected_repository, rejected_digest, rejected_headers in (
                (repository, subject["canonical_digest"], headers),
                (other_repository, subject_sha384, other_headers),
                (repository, "sha384:" + "0" * 96, headers),
            ):
                rejected = conduct_call(
                    client,
                    "v2.list_manifest_referrers",
                    url_for,
                    "GET",
                    {
                        "repository": rejected_repository,
                        "manifest_ref": rejected_digest,
                    },
                    expected_code=400,
                    headers=rejected_headers.copy(),
                )
                assert rejected.get_json()["errors"][0]["code"] == "MANIFEST_INVALID"

            conduct_call(
                client,
                "v2.list_manifest_referrers",
                url_for,
                "GET",
                {"repository": repository, "manifest_ref": subject_sha384},
                expected_code=401,
            )

        with patch.dict(
            realapp.config,
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384"]},
        ):
            enabled_alias = conduct_call(
                client,
                "v2.list_manifest_referrers",
                url_for,
                "GET",
                {"repository": repository, "manifest_ref": subject_sha384},
                headers=headers.copy(),
            )
            disabled_alias = conduct_call(
                client,
                "v2.list_manifest_referrers",
                url_for,
                "GET",
                {
                    "repository": repository,
                    "manifest_ref": subject["external_digest"],
                },
                expected_code=400,
                headers=headers.copy(),
            )

    assert enabled_alias.get_json()["manifests"][0]["digest"] == artifact_sha384
    assert disabled_alias.get_json()["errors"][0] == {
        "code": "UNSUPPORTED",
        "message": "digest algorithm is disabled by registry configuration",
        "detail": {"algorithm": "sha512", "reason": "disabled"},
    }


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


@pytest.mark.parametrize(
    "digest,expected_code,expected_reason",
    [
        ("sha384:" + "a" * 95, "DIGEST_INVALID", "malformed"),
        ("sha512:" + "A" * 128, "DIGEST_INVALID", "malformed"),
        ("sha999:" + "a" * 96, "UNSUPPORTED", "unsupported"),
    ],
)
def test_story14_manifest_delete_strictly_parses_registered_identity(
    digest, expected_code, expected_reason, client, app
):
    response = conduct_call(
        client,
        "v2.delete_manifest_by_digest",
        url_for,
        "DELETE",
        {"repository": "devtable/simple", "manifest_ref": digest},
        expected_code=400,
        headers=_manifest_auth_headers("devtable/simple"),
    )

    error = response.get_json()["errors"][0]
    assert error["code"] == expected_code
    assert error["detail"]["reason"] == expected_reason


def test_story14_delete_registered_manifest_aliases_isolated_and_hard_disable_safe(client, app):
    repository = "devtable/simple"
    other_repository = "devtable/complex"
    manifest_info = _sha512_single_manifest(
        repository,
        b"story14 registered manifest aliases",
        algorithm="sha512",
    )
    tags = ["story14-primary", "story14-secondary"]
    cache = InMemoryDataModelCache(TEST_CACHE_CONFIG)

    with (
        patch.dict(
            realapp.config,
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
        ),
        patch("endpoints.v2.manifest.model_cache", cache),
    ):
        _put_manifest(
            client,
            repository,
            manifest_info["external_digest"],
            manifest_info,
            tag=tags,
        )
        sha384_alias = _register_manifest_alias(
            repository,
            manifest_info["canonical_digest"],
            manifest_info["bytes"],
            "sha384",
        )
        repository_row = model.repository.get_repository("devtable", "simple")
        manifest_row = Manifest.get(
            repository=repository_row,
            digest=manifest_info["canonical_digest"],
        )
        model.oci.manifest.register_repository_manifest_digest(
            repository_row.id,
            manifest_row,
            manifest_info["canonical_digest"],
        )
        aliases = {
            manifest_info["canonical_digest"],
            sha384_alias,
            manifest_info["external_digest"],
        }

        for alias in aliases:
            conduct_call(
                client,
                "v2.fetch_manifest_by_digest",
                url_for,
                "GET",
                {"repository": repository, "manifest_ref": alias},
                expected_code=200,
                headers=_manifest_auth_headers(repository, actions=("pull",)),
            )

        conduct_call(
            client,
            "v2.delete_manifest_by_digest",
            url_for,
            "DELETE",
            {
                "repository": other_repository,
                "manifest_ref": manifest_info["external_digest"],
            },
            expected_code=404,
            headers=_manifest_auth_headers(other_repository),
        )
        conduct_call(
            client,
            "v2.delete_manifest_by_digest",
            url_for,
            "DELETE",
            {"repository": repository, "manifest_ref": manifest_info["external_digest"]},
            expected_code=401,
            headers=_manifest_auth_headers(repository, actions=("pull",)),
        )
        conduct_call(
            client,
            "v2.delete_manifest_by_digest",
            url_for,
            "DELETE",
            {"repository": repository, "manifest_ref": manifest_info["external_digest"]},
            expected_code=401,
        )

    with (
        patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256"]}),
        patch("endpoints.v2.manifest.model_cache", cache),
    ):
        conduct_call(
            client,
            "v2.delete_manifest_by_digest",
            url_for,
            "DELETE",
            {"repository": repository, "manifest_ref": manifest_info["external_digest"]},
            expected_code=202,
            headers=_manifest_auth_headers(repository),
        )

    with (
        patch.dict(
            realapp.config,
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
        ),
        patch("endpoints.v2.manifest.model_cache", cache),
    ):
        for alias in aliases:
            for method in ("GET", "HEAD"):
                conduct_call(
                    client,
                    "v2.fetch_manifest_by_digest",
                    url_for,
                    method,
                    {"repository": repository, "manifest_ref": alias},
                    expected_code=404,
                    headers=_manifest_auth_headers(repository, actions=("pull",)),
                )
        conduct_call(
            client,
            "v2.delete_manifest_by_digest",
            url_for,
            "DELETE",
            {"repository": repository, "manifest_ref": manifest_info["external_digest"]},
            expected_code=404,
            headers=_manifest_auth_headers(repository),
        )

    assert all(
        not filter_to_alive_tags(
            Tag.select().where(
                Tag.repository == repository_row,
                Tag.name == tag,
            ),
            allow_hidden=True,
        ).exists()
        for tag in tags
    )
    assert {
        registration.digest
        for registration in RepositoryManifestDigest.select().where(
            RepositoryManifestDigest.repository == repository_row,
            RepositoryManifestDigest.manifest == manifest_row,
        )
    } == aliases
    assert {
        relationship.blob_id
        for relationship in ManifestBlob.select().where(
            ManifestBlob.repository == repository_row,
            ManifestBlob.manifest == manifest_row,
        )
    } == manifest_info["blob_ids"]


def test_story14_deletes_untagged_registered_artifact_and_refreshes_referrers(client, app):
    repository = "devtable/simple"
    subject = _sha512_single_manifest(
        repository,
        b"story14 native referrer subject",
        algorithm="sha384",
    )
    subject["media_type"] = DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
    artifact = _oci_artifact(
        repository,
        subject,
        layer_bytes=b"story14 untagged native artifact",
        algorithm="sha512",
    )
    cache = InMemoryDataModelCache(TEST_CACHE_CONFIG)
    pull_headers = _manifest_auth_headers(repository, actions=("pull",))

    with (
        patch.dict(
            realapp.config,
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
        ),
        patch("endpoints.v2.manifest.model_cache", cache),
        patch("endpoints.v2.referrers.model_cache", cache),
        toggle_feature("REFERRERS_API", True),
    ):
        _put_manifest(
            client,
            repository,
            subject["external_digest"],
            subject,
            tag="story14-native-subject",
        )
        _put_manifest(client, repository, artifact["external_digest"], artifact)
        artifact_sha384 = _register_manifest_alias(
            repository,
            artifact["canonical_digest"],
            artifact["bytes"],
            "sha384",
        )

        for params in (
            {},
            {"artifactType": "application/vnd.example.signature"},
        ):
            response = conduct_call(
                client,
                "v2.list_manifest_referrers",
                url_for,
                "GET",
                {
                    "repository": repository,
                    "manifest_ref": subject["external_digest"],
                    **params,
                },
                headers=pull_headers.copy(),
            )
            assert [descriptor["digest"] for descriptor in response.get_json()["manifests"]] == [
                artifact["external_digest"]
            ]

        with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384"]}):
            conduct_call(
                client,
                "v2.delete_manifest_by_digest",
                url_for,
                "DELETE",
                {"repository": repository, "manifest_ref": artifact["external_digest"]},
                expected_code=202,
                headers=_manifest_auth_headers(repository),
            )

        for params in (
            {},
            {"artifactType": "application/vnd.example.signature"},
        ):
            response = conduct_call(
                client,
                "v2.list_manifest_referrers",
                url_for,
                "GET",
                {
                    "repository": repository,
                    "manifest_ref": subject["external_digest"],
                    **params,
                },
                headers=pull_headers.copy(),
            )
            assert response.get_json()["manifests"] == []

        for alias in (artifact["external_digest"], artifact_sha384):
            conduct_call(
                client,
                "v2.fetch_manifest_by_digest",
                url_for,
                "GET",
                {"repository": repository, "manifest_ref": alias},
                expected_code=404,
                headers=pull_headers.copy(),
            )

    repository_row = model.repository.get_repository("devtable", "simple")
    artifact_row = Manifest.get(
        repository=repository_row,
        digest=artifact["canonical_digest"],
    )
    assert not filter_to_alive_tags(
        Tag.select().where(Tag.manifest == artifact_row), allow_hidden=True
    ).exists()
    assert {
        registration.digest
        for registration in RepositoryManifestDigest.select().where(
            RepositoryManifestDigest.repository == repository_row,
            RepositoryManifestDigest.manifest == artifact_row,
        )
    } == {artifact["external_digest"], artifact_sha384}


def test_story14_fallback_index_delete_refreshes_primed_referrers_cache(client, app):
    repository = "devtable/simple"
    subject = _sha512_single_manifest(
        repository,
        b"story14 fallback referrer subject",
        algorithm="sha512",
    )
    subject["media_type"] = DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
    fallback_artifact = _sha512_single_manifest(
        repository,
        b"story14 fallback-only artifact",
        algorithm="sha384",
    )
    fallback_index = _manifest_index(
        [
            {
                "media_type": DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
                "bytes": fallback_artifact["bytes"],
                "descriptor_digest": fallback_artifact["external_digest"],
                "architecture": "amd64",
            }
        ],
        algorithm="sha512",
    )
    fallback_tag = subject["external_digest"].replace(":", "-", 1)
    cache = InMemoryDataModelCache(TEST_CACHE_CONFIG)
    pull_headers = _manifest_auth_headers(repository, actions=("pull",))

    with (
        patch.dict(
            realapp.config,
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
        ),
        patch("endpoints.v2.manifest.model_cache", cache),
        patch("endpoints.v2.referrers.model_cache", cache),
        toggle_feature("REFERRERS_API", True),
    ):
        _put_manifest(
            client,
            repository,
            subject["external_digest"],
            subject,
            tag="story14-fallback-subject",
        )
        _put_manifest(
            client,
            repository,
            fallback_artifact["external_digest"],
            fallback_artifact,
        )
        _put_manifest(
            client,
            repository,
            fallback_index["external_digest"],
            fallback_index,
            tag=fallback_tag,
        )

        primed = conduct_call(
            client,
            "v2.list_manifest_referrers",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": subject["external_digest"]},
            headers=pull_headers.copy(),
        )
        assert [descriptor["digest"] for descriptor in primed.get_json()["manifests"]] == [
            fallback_artifact["external_digest"]
        ]

        conduct_call(
            client,
            "v2.delete_manifest_by_digest",
            url_for,
            "DELETE",
            {"repository": repository, "manifest_ref": fallback_index["external_digest"]},
            expected_code=202,
            headers=_manifest_auth_headers(repository),
        )

        refreshed = conduct_call(
            client,
            "v2.list_manifest_referrers",
            url_for,
            "GET",
            {"repository": repository, "manifest_ref": subject["external_digest"]},
            headers=pull_headers.copy(),
        )
        assert refreshed.get_json()["manifests"] == []


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
