import hashlib
import json
import unittest
from unittest.mock import MagicMock, patch

import pytest
from flask import url_for
from playhouse.test_utils import assert_query_count

from app import app as realapp
from app import instance_keys, storage
from auth.auth_context_type import ValidatedAuthContext
from data import model
from data.cache import InMemoryDataModelCache, NoopDataModelCache
from data.cache.test.test_cache import TEST_CACHE_CONFIG
from data.database import (
    BlobUpload,
    ImageStorage,
    ImageStorageLocation,
    ImageStoragePlacement,
    RepositoryBlobDigest,
)
from data.model.storage import get_layer_path
from data.model.test.test_repo_mirroring import create_mirror_repo_robot
from data.registry_model import registry_model
from data.registry_model.registry_proxy_model import ProxyModel
from digest import digest_tools
from digest.digest_tools import sha256_digest
from endpoints.test.shared import conduct_call
from image.docker.schema2 import DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
from image.docker.schema2.manifest import DockerSchema2ManifestBuilder
from proxy.fixtures import *  # noqa: F401, F403
from test.fixtures import *
from util.bytes import Bytes
from util.security.registry_jwt import build_context_and_subject, generate_bearer_token

HELLO_WORLD_DIGEST = "sha256:f54a58bc1aac5ea1a25d796ae155dc228b3f0e11d046ae276b39c4bf2f13d8c4"


def _blob_auth_headers(repository, source_repository=None, username="devtable"):
    user = model.user.get_user(username)
    context, subject = build_context_and_subject(ValidatedAuthContext(user=user))
    access = [
        {
            "type": "repository",
            "name": repository,
            "actions": ["pull", "push"],
        }
    ]
    if source_repository is not None:
        access.append(
            {
                "type": "repository",
                "name": source_repository,
                "actions": ["pull"],
            }
        )
    token = generate_bearer_token(
        realapp.config["SERVER_HOSTNAME"], subject, context, access, 600, instance_keys
    )
    return {"Authorization": "Bearer %s" % token}


def _store_registered_blob(repository_name, content, external_digest):
    repository = model.repository.get_repository(*repository_name.split("/", 1))
    location = ImageStorageLocation.get(name="local_us")
    canonical_digest = "sha256:" + hashlib.sha256(content).hexdigest()
    blob = model.blob.store_blob_record_and_temp_link_in_repo(
        repository.id,
        canonical_digest,
        location,
        len(content),
        3600,
    )
    storage.put_content(["local_us"], get_layer_path(blob), content)
    model.oci.blob.register_repository_blob_digest(repository, blob, external_digest)
    return repository, blob, canonical_digest


HELLO_WORLD_SCHEMA2_MANIFEST_JSON = r"""{
   "schemaVersion": 2,
   "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
   "config": {
      "mediaType": "application/vnd.docker.container.image.v1+json",
      "size": 1469,
      "digest": "sha256:feb5d9fea6a5e9606aa995e879d862b825965ba48de054caab5ef356dc6b3412"
   },
   "layers": [
      {
         "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
         "size": 2479,
         "digest": "sha256:2db29710123e3e53a794f2694094b9b4338aa9ee5c40b930cb8063a1be392c54"
      }
   ]
}"""


class TestBlobPullThroughStorage:
    orgname = "cache"
    registry = "docker.io"
    image_name = "library/hello-world"
    repository = f"{orgname}/{image_name}"
    tag = "14"
    # digest for 'test'. matches the one used in proxy/fixtures.py
    digest = "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    config = None
    org = None
    manifest = None
    repo_ref = None
    blob = None

    @pytest.fixture(autouse=True)
    def setup(self, client, app, proxy_manifest_response):
        self.client = client

        self.user = model.user.get_user("devtable")
        context, subject = build_context_and_subject(ValidatedAuthContext(user=self.user))
        access = [
            {
                "type": "repository",
                "name": self.repository,
                "actions": ["pull"],
            }
        ]
        token = generate_bearer_token(
            realapp.config["SERVER_HOSTNAME"], subject, context, access, 600, instance_keys
        )
        self.headers = {
            "Authorization": f"Bearer {token}",
        }

        if self.org is None:
            self.org = model.organization.create_organization(
                self.orgname, "{self.orgname}@devtable.com", self.user
            )
            self.org.save()
            self.config = model.proxy_cache.create_proxy_cache_config(
                org_name=self.orgname,
                upstream_registry=self.registry,
                expiration_s=3600,
            )

        if self.repo_ref is None:
            r = model.repository.create_repository(self.orgname, self.image_name, self.user)
            assert r is not None
            self.repo_ref = registry_model.lookup_repository(self.orgname, self.image_name)
            assert self.repo_ref is not None

        def get_blob(layer):
            content = Bytes.for_string_or_unicode(layer).as_encoded_str()
            digest = str(sha256_digest(content))
            blob = model.blob.store_blob_record_and_temp_link(
                self.orgname,
                self.image_name,
                digest,
                ImageStorageLocation.get(name="local_us"),
                len(content),
                120,
            )
            storage.put_content(["local_us"], get_layer_path(blob), content)
            return blob, digest

        if self.manifest is None:
            layer1 = json.dumps(
                {
                    "config": {},
                    "rootfs": {"type": "layers", "diff_ids": []},
                    "history": [{}],
                }
            )
            _, config_digest = get_blob(layer1)
            layer2 = "test"
            _, blob_digest = get_blob(layer2)
            builder = DockerSchema2ManifestBuilder()
            builder.set_config_digest(config_digest, len(layer1.encode("utf-8")))
            builder.add_layer(blob_digest, len(layer2.encode("utf-8")))
            manifest = builder.build()
            created_manifest = model.oci.manifest.get_or_create_manifest(
                self.repo_ref.id, manifest, storage
            )
            self.manifest = created_manifest.manifest
            assert self.digest == blob_digest
            assert self.manifest is not None

        if self.blob is None:
            self.blob = ImageStorage.filter(ImageStorage.content_checksum == self.digest).get()

    def test_create_blob_placement_on_first_time_download(self, proxy_manifest_response):
        proxy_mock = proxy_manifest_response(
            self.tag, HELLO_WORLD_SCHEMA2_MANIFEST_JSON, DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
        )
        params = {
            "repository": self.repository,
            "digest": self.digest,
        }

        with patch(
            "data.registry_model.registry_proxy_model.Proxy", MagicMock(return_value=proxy_mock)
        ):
            with patch("endpoints.v2.blob.model_cache", NoopDataModelCache(TEST_CACHE_CONFIG)):
                conduct_call(
                    self.client,
                    "v2.download_blob",
                    url_for,
                    "GET",
                    params,
                    expected_code=200,
                    headers=self.headers,
                )
        placements = ImageStoragePlacement.filter(ImageStoragePlacement.storage == self.blob)
        assert placements.count() == 1

    def test_store_blob_on_first_time_download(self, proxy_manifest_response):
        proxy_mock = proxy_manifest_response(
            self.tag, HELLO_WORLD_SCHEMA2_MANIFEST_JSON, DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
        )
        params = {
            "repository": self.repository,
            "digest": self.digest,
        }

        with patch(
            "data.registry_model.registry_proxy_model.Proxy", MagicMock(return_value=proxy_mock)
        ):
            with patch("endpoints.v2.blob.model_cache", NoopDataModelCache(TEST_CACHE_CONFIG)):
                conduct_call(
                    self.client,
                    "v2.download_blob",
                    url_for,
                    "GET",
                    params,
                    expected_code=200,
                    headers=self.headers,
                )

        path = get_layer_path(self.blob)
        assert path is not None

        placements = ImageStoragePlacement.filter(ImageStoragePlacement.storage == self.blob)
        locations = [placements.get().location.name]
        assert storage.exists(locations, path), f"blob not found in storage at path {path}"

    @pytest.mark.parametrize("algorithm,length", [("sha384", 96), ("sha512", 128)])
    def test_alternative_digest_is_rejected_before_upstream_blob_request(
        self, algorithm, length
    ):
        digest = f"{algorithm}:" + "a" * length
        proxy_mock = MagicMock()

        with (
            patch.dict(
                realapp.config,
                {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
            ),
            patch(
                "data.registry_model.registry_proxy_model.Proxy",
                MagicMock(return_value=proxy_mock),
            ),
            patch(
                "endpoints.v2.blob.model_cache", NoopDataModelCache(TEST_CACHE_CONFIG)
            ),
        ):
            response = conduct_call(
                self.client,
                "v2.download_blob",
                url_for,
                "GET",
                {"repository": self.repository, "digest": digest},
                expected_code=400,
                headers=self.headers,
            )

        assert response.get_json()["errors"][0] == {
            "code": "UNSUPPORTED",
            "message": "digest algorithm is unsupported",
            "detail": {"algorithm": algorithm, "reason": "unsupported"},
        }
        proxy_mock.get_blob.assert_not_called()


@pytest.mark.e2e
class TestBlobPullThroughProxy(unittest.TestCase):
    org = "cache"
    registry = "docker.io"
    image_name = "library/postgres"
    repository = f"{org}/{image_name}"
    manifest_digest = "sha256:3039f467c7f92ee93a68dc88237495624c27921927b147d8b4e914f885c89d9f"
    config = None
    blob_digest = None
    repo_ref = None

    @pytest.fixture(autouse=True)
    def setup(self, client, app):
        self.client = client

        self.user = model.user.get_user("devtable")
        context, subject = build_context_and_subject(ValidatedAuthContext(user=self.user))
        access = [
            {
                "type": "repository",
                "name": self.repository,
                "actions": ["pull"],
            }
        ]
        token = generate_bearer_token(
            realapp.config["SERVER_HOSTNAME"], subject, context, access, 600, instance_keys
        )
        self.headers = {
            "Authorization": f"Bearer {token}",
        }

        try:
            model.organization.get(self.org)
        except Exception:
            org = model.organization.create_organization(self.org, "cache@devtable.com", self.user)
            org.save()

        if self.config is None:
            self.config = model.proxy_cache.create_proxy_cache_config(
                org_name=self.org,
                upstream_registry=self.registry,
                expiration_s=3600,
            )

        if self.repo_ref is None:
            r = model.repository.create_repository(self.org, self.image_name, self.user)
            assert r is not None
            self.repo_ref = registry_model.lookup_repository(self.org, self.image_name)
            assert self.repo_ref is not None

        if self.blob_digest is None:
            proxy_model = ProxyModel(self.org, self.image_name, self.user)
            manifest = proxy_model.lookup_manifest_by_digest(self.repo_ref, self.manifest_digest)
            self.blob_digest = manifest.get_parsed_manifest().blob_digests[0]

    def test_pull_from_dockerhub(self):
        params = {
            "repository": self.repository,
            "digest": self.blob_digest,
        }
        conduct_call(
            self.client,
            "v2.download_blob",
            url_for,
            "GET",
            params,
            expected_code=200,
            headers=self.headers,
        )

    def test_pull_from_dockerhub_404(self):
        digest = "sha256:" + hashlib.sha256(b"a").hexdigest()
        params = {
            "repository": self.repository,
            "digest": digest,
        }
        conduct_call(
            self.client,
            "v2.download_blob",
            url_for,
            "GET",
            params,
            expected_code=404,
            headers=self.headers,
        )

    def test_check_blob_exists_from_dockerhub(self):
        params = {
            "repository": self.repository,
            "digest": self.blob_digest,
        }
        conduct_call(
            self.client,
            "v2.check_blob_exists",
            url_for,
            "HEAD",
            params,
            expected_code=200,
            headers=self.headers,
        )

    def test_check_blob_exists_from_dockerhub_404(self):
        digest = "sha256:" + hashlib.sha256(b"a").hexdigest()
        params = {
            "repository": self.repository,
            "digest": digest,
        }
        conduct_call(
            self.client,
            "v2.check_blob_exists",
            url_for,
            "HEAD",
            params,
            expected_code=404,
            headers=self.headers,
        )


@pytest.mark.parametrize(
    "method, endpoint, expected_count",
    [
        ("GET", "download_blob", 1),
        ("HEAD", "check_blob_exists", 0),
    ],
)
@patch("endpoints.v2.blob.model_cache", InMemoryDataModelCache(TEST_CACHE_CONFIG))
def test_blob_caching(method, endpoint, expected_count, client, app):
    digest = "sha256:" + hashlib.sha256(b"a").hexdigest()
    location = ImageStorageLocation.get(name="local_us")
    model.blob.store_blob_record_and_temp_link("devtable", "simple", digest, location, 1, 10000000)

    params = {
        "repository": "devtable/simple",
        "digest": digest,
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

    # Run without caching to make sure the request works. This also preloads some of
    # our global model caches.
    conduct_call(
        client, "v2." + endpoint, url_for, method, params, expected_code=200, headers=headers
    )
    # First request should make a DB query to retrieve the blob.
    conduct_call(
        client, "v2." + endpoint, url_for, method, params, expected_code=200, headers=headers
    )

    # turn off pull-thru proxy cache. it adds an extra query to the pull operation
    # for checking whether the namespace is a cache org or not before retrieving
    # the blob.
    with patch("endpoints.decorators.features.PROXY_CACHE", False):
        # Subsequent requests should use the cached blob.
        # one query for the get_authenticated_user()
        with assert_query_count(expected_count):
            conduct_call(
                client,
                "v2." + endpoint,
                url_for,
                method,
                params,
                expected_code=200,
                headers=headers,
            )


@pytest.mark.parametrize(
    "mount_digest, source_repo, username, include_from_param, expected_code",
    [
        # Unknown blob.
        (
            "sha256:" + hashlib.sha256(b"unknown").hexdigest(),
            "devtable/simple",
            "devtable",
            True,
            202,
        ),
        (
            "sha256:" + hashlib.sha256(b"unknown").hexdigest(),
            "devtable/simple",
            "devtable",
            False,
            202,
        ),
        # Blob not in repo.
        ("sha256:" + hashlib.sha256(b"a").hexdigest(), "devtable/complex", "devtable", True, 202),
        ("sha256:" + hashlib.sha256(b"a").hexdigest(), "devtable/complex", "devtable", False, 202),
        # # Blob in repo.
        ("sha256:" + hashlib.sha256(b"b").hexdigest(), "devtable/complex", "devtable", True, 201),
        ("sha256:" + hashlib.sha256(b"b").hexdigest(), "devtable/complex", "devtable", False, 202),
        # # No access to repo.
        ("sha256:" + hashlib.sha256(b"b").hexdigest(), "devtable/complex", "public", True, 202),
        ("sha256:" + hashlib.sha256(b"b").hexdigest(), "devtable/complex", "public", False, 202),
        # # Public repo.
        ("sha256:" + hashlib.sha256(b"c").hexdigest(), "public/publicrepo", "devtable", True, 201),
        ("sha256:" + hashlib.sha256(b"c").hexdigest(), "public/publicrepo", "devtable", False, 202),
    ],
)
def test_blob_mounting(
    mount_digest, source_repo, username, include_from_param, expected_code, client, app
):
    location = ImageStorageLocation.get(name="local_us")

    # Store and link some blobs.
    digest = "sha256:" + hashlib.sha256(b"a").hexdigest()
    model.blob.store_blob_record_and_temp_link("devtable", "simple", digest, location, 1, 10000000)

    digest = "sha256:" + hashlib.sha256(b"b").hexdigest()
    model.blob.store_blob_record_and_temp_link("devtable", "complex", digest, location, 1, 10000000)

    digest = "sha256:" + hashlib.sha256(b"c").hexdigest()
    model.blob.store_blob_record_and_temp_link(
        "public", "publicrepo", digest, location, 1, 10000000
    )

    params = {
        "repository": "devtable/building",
        "mount": mount_digest,
    }
    if include_from_param:
        params["from"] = source_repo

    user = model.user.get_user(username)
    access = [
        {
            "type": "repository",
            "name": "devtable/building",
            "actions": ["pull", "push"],
        }
    ]

    if source_repo.find(username) == 0:
        access.append(
            {
                "type": "repository",
                "name": source_repo,
                "actions": ["pull"],
            }
        )

    context, subject = build_context_and_subject(ValidatedAuthContext(user=user))
    token = generate_bearer_token(
        realapp.config["SERVER_HOSTNAME"], subject, context, access, 600, instance_keys
    )

    headers = {
        "Authorization": "Bearer %s" % token,
    }

    conduct_call(
        client,
        "v2.start_blob_upload",
        url_for,
        "POST",
        params,
        expected_code=expected_code,
        headers=headers,
    )

    repository = model.repository.get_repository("devtable", "building")

    if expected_code == 201:
        # Ensure the blob now exists under the repo.
        assert model.oci.blob.get_repository_blob_by_digest(repository, mount_digest)
    else:
        assert model.oci.blob.get_repository_blob_by_digest(repository, mount_digest) is None


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_alternative_mount_registers_destination_without_exposing_canonical_digest(
    algorithm, client, app
):
    source = "devtable/complex"
    destination = "devtable/building"
    content = b"alternative mount content"
    alternative_digest = f"{algorithm}:" + hashlib.new(algorithm, content).hexdigest()
    _, _, canonical_digest = _store_registered_blob(source, content, alternative_digest)
    headers = _blob_auth_headers(destination, source)

    with (
        patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]}),
        patch("endpoints.v2.blob.model_cache", NoopDataModelCache(TEST_CACHE_CONFIG)),
    ):
        for _ in range(2):
            response = conduct_call(
                client,
                "v2.start_blob_upload",
                url_for,
                "POST",
                {"repository": destination, "mount": alternative_digest, "from": source},
                expected_code=201,
                headers=headers.copy(),
            )
            assert response.headers["Docker-Content-Digest"] == alternative_digest
            assert response.headers["Location"].endswith("/blobs/" + alternative_digest)
            assert "Docker-Upload-UUID" not in response.headers

        response = conduct_call(
            client,
            "v2.download_blob",
            url_for,
            "GET",
            {"repository": destination, "digest": alternative_digest},
            expected_code=200,
            headers=headers.copy(),
        )
        assert response.headers["Docker-Content-Digest"] == alternative_digest
        assert response.data == content

        conduct_call(
            client,
            "v2.download_blob",
            url_for,
            "GET",
            {"repository": destination, "digest": canonical_digest},
            expected_code=404,
            headers=headers.copy(),
        )

    destination_repository = model.repository.get_repository("devtable", "building")
    assert (
        RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == destination_repository,
            RepositoryBlobDigest.digest == alternative_digest,
        )
        .count()
        == 1
    )


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_alternative_mount_enforces_source_authorization_and_repository_boundary(
    algorithm, client, app
):
    registered_source = "devtable/complex"
    wrong_source = "devtable/simple"
    destination = "devtable/building"
    content = b"isolated mount"
    alternative_digest = f"{algorithm}:" + hashlib.new(algorithm, content).hexdigest()
    _store_registered_blob(registered_source, content, alternative_digest)
    destination_repository = model.repository.get_repository("devtable", "building")

    with (
        patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]}),
        patch("endpoints.v2.blob.model_cache", NoopDataModelCache(TEST_CACHE_CONFIG)),
    ):
        wrong_source_response = conduct_call(
            client,
            "v2.start_blob_upload",
            url_for,
            "POST",
            {
                "repository": destination,
                "mount": alternative_digest,
                "from": wrong_source,
            },
            expected_code=202,
            headers=_blob_auth_headers(destination, wrong_source),
        )
        unauthorized_response = conduct_call(
            client,
            "v2.start_blob_upload",
            url_for,
            "POST",
            {
                "repository": destination,
                "mount": alternative_digest,
                "from": registered_source,
            },
            expected_code=202,
            headers=_blob_auth_headers(destination),
        )

        for response in (wrong_source_response, unauthorized_response):
            upload_uuid = response.headers["Docker-Upload-UUID"]
            conduct_call(
                client,
                "v2.cancel_upload",
                url_for,
                "DELETE",
                {"repository": destination, "upload_uuid": upload_uuid},
                expected_code=204,
                headers=_blob_auth_headers(destination),
            )

    assert (
        RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == destination_repository,
            RepositoryBlobDigest.digest == alternative_digest,
        )
        .count()
        == 0
    )


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_alternative_mount_rejects_destination_registration_conflict(algorithm, client, app):
    source = "devtable/complex"
    destination = "devtable/building"
    alternative_digest = f"{algorithm}:" + hashlib.new(algorithm, b"same identity").hexdigest()
    _store_registered_blob(source, b"source content", alternative_digest)
    destination_repository, existing_storage, _ = _store_registered_blob(
        destination, b"different content", alternative_digest
    )

    with (
        patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]}),
        patch("endpoints.v2.blob.model_cache", NoopDataModelCache(TEST_CACHE_CONFIG)),
    ):
        response = conduct_call(
            client,
            "v2.start_blob_upload",
            url_for,
            "POST",
            {"repository": destination, "mount": alternative_digest, "from": source},
            expected_code=400,
            headers=_blob_auth_headers(destination, source),
        )

    error = response.get_json()["errors"][0]
    assert error == {
        "code": "DIGEST_INVALID",
        "message": "digest is already registered to different content in destination repository",
        "detail": {"digest": alternative_digest, "reason": "conflict"},
    }
    registration = RepositoryBlobDigest.get(
        repository=destination_repository, digest=alternative_digest
    )
    assert registration.image_storage_id == existing_storage.id


@pytest.mark.parametrize(
    "mount_digest,allowed_algorithms,expected_code,expected_reason",
    [
        ("sha512:1234", ["sha256", "sha512"], 400, "malformed"),
        ("sha999:" + "a" * 96, ["sha256", "sha384", "sha512"], 400, "unsupported"),
        ("sha512:" + "a" * 128, ["sha256"], 400, "disabled"),
        (
            "sha512:" + hashlib.sha512(b"unknown").hexdigest(),
            ["sha256", "sha512"],
            202,
            None,
        ),
    ],
)
def test_mount_digest_errors(
    mount_digest,
    allowed_algorithms,
    expected_code,
    expected_reason,
    client,
    app,
):
    destination = "devtable/building"
    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": allowed_algorithms}):
        response = conduct_call(
            client,
            "v2.start_blob_upload",
            url_for,
            "POST",
            {
                "repository": destination,
                "mount": mount_digest,
                "from": "devtable/complex",
            },
            expected_code=expected_code,
            headers=_blob_auth_headers(destination, "devtable/complex"),
        )

    if expected_reason is None:
        assert response.headers["Docker-Upload-UUID"]
        return
    error = response.get_json()["errors"][0]
    assert error["detail"]["reason"] == expected_reason
    assert "Docker-Upload-UUID" not in response.headers


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_alternative_chunked_blob_upload_resume_and_pull(algorithm, client, app):
    repository = "devtable/simple"
    headers = _blob_auth_headers(repository)
    content = b"hello world"
    requested_digest = f"{algorithm}:" + hashlib.new(algorithm, content).hexdigest()
    canonical_digest = "sha256:" + hashlib.sha256(content).hexdigest()

    with (
        patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]}),
        patch("endpoints.v2.blob.model_cache", NoopDataModelCache(TEST_CACHE_CONFIG)),
    ):
        response = conduct_call(
            client,
            "v2.start_blob_upload",
            url_for,
            "POST",
            {"repository": repository, "digest-algorithm": algorithm},
            expected_code=202,
            headers=headers.copy(),
        )
        upload_uuid = response.headers["Docker-Upload-UUID"]
        assert response.headers["Location"].endswith("/blobs/uploads/" + upload_uuid)

        response = conduct_call(
            client,
            "v2.upload_chunk",
            url_for,
            "PATCH",
            {"repository": repository, "upload_uuid": upload_uuid},
            raw_body=b"hello ",
            expected_code=202,
            headers=headers.copy(),
        )
        assert response.headers["Docker-Upload-UUID"] == upload_uuid
        assert response.headers["Location"].endswith("/blobs/uploads/" + upload_uuid)

        response = conduct_call(
            client,
            "v2.fetch_existing_upload",
            url_for,
            "GET",
            {"repository": repository, "upload_uuid": upload_uuid},
            expected_code=204,
            headers=headers.copy(),
        )
        assert response.headers["Docker-Upload-UUID"] == upload_uuid
        assert response.headers["Location"].endswith("/blobs/uploads/" + upload_uuid)

        response = conduct_call(
            client,
            "v2.monolithic_upload_or_last_chunk",
            url_for,
            "PUT",
            {
                "repository": repository,
                "upload_uuid": upload_uuid,
                "digest": requested_digest,
            },
            raw_body=b"world",
            expected_code=201,
            headers=headers.copy(),
        )
        assert response.headers["Docker-Content-Digest"] == requested_digest
        assert response.headers["Location"].endswith("/blobs/" + requested_digest)
        assert "Docker-Upload-UUID" not in response.headers

        response = conduct_call(
            client,
            "v2.download_blob",
            url_for,
            "GET",
            {"repository": repository, "digest": requested_digest},
            expected_code=200,
            headers=headers.copy(),
        )
        assert response.headers["Docker-Content-Digest"] == requested_digest
        assert response.data == content

        response = conduct_call(
            client,
            "v2.check_blob_exists",
            url_for,
            "HEAD",
            {"repository": repository, "digest": requested_digest},
            expected_code=200,
            headers=headers.copy(),
        )
        assert response.headers["Docker-Content-Digest"] == requested_digest

        conduct_call(
            client,
            "v2.download_blob",
            url_for,
            "GET",
            {"repository": repository, "digest": canonical_digest},
            expected_code=404,
            headers=headers.copy(),
        )
        conduct_call(
            client,
            "v2.download_blob",
            url_for,
            "GET",
            {"repository": "devtable/complex", "digest": requested_digest},
            expected_code=404,
            headers=_blob_auth_headers("devtable/complex"),
        )


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_alternative_monolithic_upload_infers_algorithm_without_hint(algorithm, client, app):
    repository = "devtable/simple"
    content = b"monolithic alternative digest"
    requested_digest = f"{algorithm}:" + hashlib.new(algorithm, content).hexdigest()

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]}):
        response = conduct_call(
            client,
            "v2.start_blob_upload",
            url_for,
            "POST",
            {"repository": repository, "digest": requested_digest},
            raw_body=content,
            expected_code=201,
            headers=_blob_auth_headers(repository),
        )

    assert response.headers["Docker-Content-Digest"] == requested_digest
    assert response.headers["Location"].endswith("/blobs/" + requested_digest)


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_mirror_blob_upload_rejects_alternative_digest_without_persistence(algorithm, client, app):
    repo_name = f"unsupported-blob-{algorithm}"
    mirror, repository_row = create_mirror_repo_robot(["latest"], repo_name=repo_name)
    repository = f"mirror/{repo_name}"
    content = b"unsupported mirror blob"
    requested_digest = f"{algorithm}:" + hashlib.new(algorithm, content).hexdigest()

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]}):
        response = conduct_call(
            client,
            "v2.start_blob_upload",
            url_for,
            "POST",
            {"repository": repository, "digest": requested_digest},
            raw_body=content,
            expected_code=400,
            headers=_blob_auth_headers(repository, username=mirror.internal_robot.username),
        )

    assert response.get_json()["errors"][0] == {
        "code": "UNSUPPORTED",
        "message": "digest algorithm is unsupported",
        "detail": {"algorithm": algorithm, "reason": "unsupported"},
    }
    assert not BlobUpload.select().where(BlobUpload.repository == repository_row).exists()
    assert (
        not RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == repository_row,
            RepositoryBlobDigest.digest == requested_digest,
        )
        .exists()
    )


def test_blob_upload_returns_precise_digest_errors_and_rejects_disabled_patch(client, app):
    repository = "devtable/simple"
    headers = _blob_auth_headers(repository)
    content = b"content"
    requested_digest = "sha512:" + hashlib.sha512(content).hexdigest()

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha512"]}):
        response = conduct_call(
            client,
            "v2.start_blob_upload",
            url_for,
            "POST",
            {"repository": repository, "digest-algorithm": "sha512"},
            expected_code=202,
            headers=headers.copy(),
        )
    upload_uuid = response.headers["Docker-Upload-UUID"]

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256"]}):
        disabled = conduct_call(
            client,
            "v2.start_blob_upload",
            url_for,
            "POST",
            {"repository": repository, "digest-algorithm": "sha512"},
            expected_code=400,
            headers=headers.copy(),
        )
        disabled_sha384 = conduct_call(
            client,
            "v2.start_blob_upload",
            url_for,
            "POST",
            {"repository": repository, "digest-algorithm": "sha384"},
            expected_code=400,
            headers=headers.copy(),
        )
        malformed = conduct_call(
            client,
            "v2.start_blob_upload",
            url_for,
            "POST",
            {"repository": repository, "digest": "sha256:1234"},
            raw_body=content,
            expected_code=400,
            headers=headers.copy(),
        )
        unsupported = conduct_call(
            client,
            "v2.start_blob_upload",
            url_for,
            "POST",
            {"repository": repository, "digest-algorithm": "sha999"},
            expected_code=400,
            headers=headers.copy(),
        )
        disabled_patch = conduct_call(
            client,
            "v2.upload_chunk",
            url_for,
            "PATCH",
            {"repository": repository, "upload_uuid": upload_uuid},
            raw_body=content,
            expected_code=400,
            headers=headers.copy(),
        )
        disabled_final = conduct_call(
            client,
            "v2.monolithic_upload_or_last_chunk",
            url_for,
            "PUT",
            {
                "repository": repository,
                "upload_uuid": upload_uuid,
                "digest": requested_digest,
            },
            raw_body=content,
            expected_code=400,
            headers=headers.copy(),
        )

    for response in (disabled, disabled_patch, disabled_final):
        error = response.get_json()["errors"][0]
        assert error["code"] == "UNSUPPORTED"
        assert error["detail"] == {"algorithm": "sha512", "reason": "disabled"}
    assert disabled_sha384.get_json()["errors"][0]["detail"] == {
        "algorithm": "sha384",
        "reason": "disabled",
    }
    assert malformed.get_json()["errors"][0]["code"] == "DIGEST_INVALID"
    assert malformed.get_json()["errors"][0]["detail"]["reason"] == "malformed"
    assert unsupported.get_json()["errors"][0]["detail"] == {
        "algorithm": "sha999",
        "reason": "unsupported",
    }
    assert BlobUpload.get(uuid=upload_uuid).byte_count == 0

    conduct_call(
        client,
        "v2.cancel_upload",
        url_for,
        "DELETE",
        {"repository": repository, "upload_uuid": upload_uuid},
        expected_code=204,
        headers=headers.copy(),
    )


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_hintless_upload_survives_mixed_configuration_without_storage_readback(
    algorithm, client, app
):
    repository = "devtable/simple"
    headers = _blob_auth_headers(repository)
    content = b"hintless rolling upload"
    requested_digest = f"{algorithm}:" + hashlib.new(algorithm, content).hexdigest()

    with (
        patch.object(storage, "stream_read", side_effect=AssertionError("storage readback")),
        patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]}),
    ):
        response = conduct_call(
            client,
            "v2.start_blob_upload",
            url_for,
            "POST",
            {"repository": repository},
            expected_code=202,
            headers=headers.copy(),
        )
        upload_uuid = response.headers["Docker-Upload-UUID"]
        upload = BlobUpload.get(uuid=upload_uuid)
        assert upload.requested_digest_algorithm == algorithm
        assert (
            digest_tools.resumable_hasher_state_origin(algorithm, upload.requested_digest_state)
            == digest_tools.RESUMABLE_HASH_ORIGIN_HINTLESS
        )
        conduct_call(
            client,
            "v2.upload_chunk",
            url_for,
            "PATCH",
            {"repository": repository, "upload_uuid": upload_uuid},
            raw_body=content[:10],
            expected_code=202,
            headers=headers.copy(),
        )

    with (
        patch.object(storage, "stream_read", side_effect=AssertionError("storage readback")),
        patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256"]}),
    ):
        response = conduct_call(
            client,
            "v2.upload_chunk",
            url_for,
            "PATCH",
            {"repository": repository, "upload_uuid": upload_uuid},
            raw_body=content[10:],
            expected_code=202,
            headers=headers.copy(),
        )
        assert response.headers["Docker-Upload-UUID"] == upload_uuid

    with (
        patch.object(storage, "stream_read", side_effect=AssertionError("storage readback")),
        patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]}),
    ):
        response = conduct_call(
            client,
            "v2.monolithic_upload_or_last_chunk",
            url_for,
            "PUT",
            {
                "repository": repository,
                "upload_uuid": upload_uuid,
                "digest": requested_digest,
            },
            expected_code=201,
            headers=headers.copy(),
        )

    assert response.headers["Docker-Content-Digest"] == requested_digest
    assert response.headers["Location"].endswith("/blobs/" + requested_digest)


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_hintless_chunked_upload_with_multiple_alternatives_requires_a_hint(algorithm, client, app):
    repository = "devtable/simple"
    headers = _blob_auth_headers(repository)
    content = b"ambiguous hintless chunked upload"
    requested_digest = f"{algorithm}:" + hashlib.new(algorithm, content).hexdigest()

    with patch.dict(
        realapp.config,
        {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
    ):
        response = conduct_call(
            client,
            "v2.start_blob_upload",
            url_for,
            "POST",
            {"repository": repository},
            expected_code=202,
            headers=headers.copy(),
        )
        upload_uuid = response.headers["Docker-Upload-UUID"]
        upload = BlobUpload.get(uuid=upload_uuid)
        assert upload.requested_digest_algorithm is None
        assert upload.requested_digest_state is None

        conduct_call(
            client,
            "v2.upload_chunk",
            url_for,
            "PATCH",
            {"repository": repository, "upload_uuid": upload_uuid},
            raw_body=content,
            expected_code=202,
            headers=headers.copy(),
        )
        rejected = conduct_call(
            client,
            "v2.monolithic_upload_or_last_chunk",
            url_for,
            "PUT",
            {
                "repository": repository,
                "upload_uuid": upload_uuid,
                "digest": requested_digest,
            },
            expected_code=400,
            headers=headers.copy(),
        )

    error = rejected.get_json()["errors"][0]
    assert error["code"] == "DIGEST_INVALID"
    assert error["detail"]["reason"] == (
        "Final digest algorithm was not recorded before earlier chunks"
    )
    assert not BlobUpload.select().where(BlobUpload.uuid == upload_uuid).exists()
    repository_row = model.repository.get_repository("devtable", "simple")
    assert (
        not RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == repository_row,
            RepositoryBlobDigest.digest == requested_digest,
        )
        .exists()
    )


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_final_sha256_is_authoritative_when_session_algorithm_is_disabled(algorithm, client, app):
    repository = "devtable/simple"
    headers = _blob_auth_headers(repository)
    content = b"canonical final digest"
    canonical_digest = "sha256:" + hashlib.sha256(content).hexdigest()

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]}):
        response = conduct_call(
            client,
            "v2.start_blob_upload",
            url_for,
            "POST",
            {"repository": repository, "digest-algorithm": algorithm},
            expected_code=202,
            headers=headers.copy(),
        )
    upload_uuid = response.headers["Docker-Upload-UUID"]

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256"]}):
        response = conduct_call(
            client,
            "v2.monolithic_upload_or_last_chunk",
            url_for,
            "PUT",
            {
                "repository": repository,
                "upload_uuid": upload_uuid,
                "digest": canonical_digest,
            },
            raw_body=content,
            expected_code=201,
            headers=headers.copy(),
        )

    assert response.headers["Docker-Content-Digest"] == canonical_digest
    assert response.headers["Location"].endswith("/blobs/" + canonical_digest)


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_mismatched_final_digest_returns_digest_invalid(algorithm, client, app):
    repository = "devtable/simple"
    content = b"actual content"
    wrong_digest = f"{algorithm}:" + hashlib.new(algorithm, b"different content").hexdigest()

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]}):
        response = conduct_call(
            client,
            "v2.start_blob_upload",
            url_for,
            "POST",
            {"repository": repository, "digest": wrong_digest},
            raw_body=content,
            expected_code=400,
            headers=_blob_auth_headers(repository),
        )

    error = response.get_json()["errors"][0]
    assert error["code"] == "DIGEST_INVALID"
    assert error["detail"]["reason"] == "mismatch"
    repository_row = model.repository.get_repository("devtable", "simple")
    assert (
        RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == repository_row,
            RepositoryBlobDigest.digest == wrong_digest,
        )
        .count()
        == 0
    )


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_repeated_alternative_monolithic_upload_deduplicates(algorithm, client, app):
    repository = "devtable/simple"
    content = b"deduplicated alternative upload"
    requested_digest = f"{algorithm}:" + hashlib.new(algorithm, content).hexdigest()
    canonical_digest = "sha256:" + hashlib.sha256(content).hexdigest()

    with patch.dict(realapp.config, {"ALLOWED_HASH_ALGORITHMS": ["sha256", algorithm]}):
        for _ in range(2):
            response = conduct_call(
                client,
                "v2.start_blob_upload",
                url_for,
                "POST",
                {"repository": repository, "digest": requested_digest},
                raw_body=content,
                expected_code=201,
                headers=_blob_auth_headers(repository),
            )
            assert response.headers["Docker-Content-Digest"] == requested_digest
            assert response.headers["Location"].endswith("/blobs/" + requested_digest)
            assert "Docker-Upload-UUID" not in response.headers

    repository_row = model.repository.get_repository("devtable", "simple")
    assert (
        ImageStorage.select().where(ImageStorage.content_checksum == canonical_digest).count() == 1
    )
    assert (
        RepositoryBlobDigest.select()
        .where(
            RepositoryBlobDigest.repository == repository_row,
            RepositoryBlobDigest.digest == requested_digest,
        )
        .count()
        == 1
    )


def test_blob_lookup_digest_errors_are_precise(client, app):
    repository = "devtable/simple"
    headers = _blob_auth_headers(repository)
    unknown_digest = "sha256:" + hashlib.sha256(b"unknown lookup").hexdigest()

    unknown = conduct_call(
        client,
        "v2.download_blob",
        url_for,
        "GET",
        {"repository": repository, "digest": unknown_digest},
        expected_code=404,
        headers=headers.copy(),
    )
    malformed = conduct_call(
        client,
        "v2.download_blob",
        url_for,
        "GET",
        {"repository": repository, "digest": "sha256:1234"},
        expected_code=400,
        headers=headers.copy(),
    )
    malformed_sha384 = conduct_call(
        client,
        "v2.download_blob",
        url_for,
        "GET",
        {"repository": repository, "digest": "sha384:" + "a" * 95},
        expected_code=400,
        headers=headers.copy(),
    )
    disabled_sha384 = conduct_call(
        client,
        "v2.download_blob",
        url_for,
        "GET",
        {"repository": repository, "digest": "sha384:" + "a" * 96},
        expected_code=400,
        headers=headers.copy(),
    )
    unsupported = conduct_call(
        client,
        "v2.download_blob",
        url_for,
        "GET",
        {"repository": repository, "digest": "sha999:" + "a" * 96},
        expected_code=400,
        headers=headers.copy(),
    )

    assert unknown.get_json()["errors"][0]["code"] == "BLOB_UNKNOWN"
    assert malformed.get_json()["errors"][0]["detail"]["reason"] == "malformed"
    assert malformed_sha384.get_json()["errors"][0]["detail"]["reason"] == "malformed"
    assert disabled_sha384.get_json()["errors"][0]["detail"]["reason"] == "disabled"
    assert unsupported.get_json()["errors"][0]["detail"]["reason"] == "unsupported"


def test_blob_upload_offset(client, app):
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

    # Create a blob upload request.
    params = {
        "repository": "devtable/simple",
    }
    response = conduct_call(
        client, "v2.start_blob_upload", url_for, "POST", params, expected_code=202, headers=headers
    )

    upload_uuid = response.headers["Docker-Upload-UUID"]

    # Attempt to start an upload past index zero.
    params = {
        "repository": "devtable/simple",
        "upload_uuid": upload_uuid,
    }

    headers = {
        "Authorization": "Bearer %s" % token,
        "Content-Range": "13-50",
    }

    conduct_call(
        client,
        "v2.upload_chunk",
        url_for,
        "PATCH",
        params,
        expected_code=416,
        headers=headers,
        body="something",
    )


def test_blob_upload_when_pushes_disabled(client, app):
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

    # Create a blob upload request.
    params = {
        "repository": "devtable/simple",
    }

    # Disable pushes before conducting the call
    realapp.config["DISABLE_PUSHES"] = True
    conduct_call(
        client,
        "v2.start_blob_upload",
        url_for,
        "POST",
        params,
        expected_code=405,
        headers=headers,
    )

    # re-enable pushes, since the setting persists across all tests
    realapp.config["DISABLE_PUSHES"] = False
