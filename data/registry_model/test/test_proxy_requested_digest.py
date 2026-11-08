"""
PROJQUAY-10117: proxy cache with SHA-384/512 descriptors.
"""

import hashlib
import json
from io import BytesIO
from test.fixtures import *
from unittest.mock import MagicMock, patch

import pytest

from app import app as application
from app import storage
from data.database import (
    ImageStorage,
    ImageStorageLocation,
    ImageStoragePlacement,
    ManifestBlob,
)
from data.model import ManifestDoesNotExist
from data.model.organization import create_organization
from data.model.proxy_cache import create_proxy_cache_config
from data.model.storage import garbage_collect_storage
from data.model.user import get_user
from data.registry_model.blobuploader import BlobUploadSettings, upload_blob
from data.registry_model.registry_proxy_model import ProxyModel
from proxy.fixtures import proxy_manifest_response  # noqa: F401

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
ALL = ["sha256", "sha384", "sha512"]


def _digest(algorithm, data):
    return "%s:%s" % (algorithm, hashlib.new(algorithm, data).hexdigest())


CONFIG = b'{"architecture":"amd64","os":"linux","rootfs":{"type":"layers","diff_ids":[]}}'
LAYER = b"proxied layer bytes"


def _manifest(algorithm):
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": _digest(algorithm, CONFIG),
                "size": len(CONFIG),
            },
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "digest": _digest(algorithm, LAYER),
                    "size": len(LAYER),
                }
            ],
        }
    )


ORG = "pqcproxy"


@pytest.fixture()
def proxy_org(initialized_db):
    user = get_user("devtable")
    create_organization(ORG, "pqcproxy@devtable.com", user).save()
    create_proxy_cache_config(org_name=ORG, upstream_registry="quay.io", expiration_s=3600)
    return user


@pytest.fixture()
def all_algorithms():
    with patch.dict(application.config, {"ALLOWED_HASH_ALGORITHMS": ALL}):
        yield


def _pull(user, repo, manifest_ref, manifest_json, proxy_manifest_response):
    with patch(
        "data.registry_model.registry_proxy_model.Proxy",
        MagicMock(
            return_value=proxy_manifest_response(manifest_ref, manifest_json, OCI_MANIFEST)
        ),
    ):
        model = ProxyModel(ORG, repo, user)
        repo_ref = model.lookup_repository(ORG, repo, manifest_ref=manifest_ref)
        manifest = model.lookup_manifest_by_digest(repo_ref, manifest_ref)
        return model, repo_ref, manifest


def _links(repo_ref):
    return list(
        ImageStorage.select().join(ManifestBlob).where(ManifestBlob.repository == repo_ref.id)
    )


def test_sha512_descriptors_create_per_repository_placeholders(
    proxy_org, all_algorithms, proxy_manifest_response
):
    manifest_json = _manifest("sha512")
    manifest_digest = _digest("sha512", manifest_json.encode())

    model_a, repo_a, manifest = _pull(
        proxy_org, "app/a", manifest_digest, manifest_json, proxy_manifest_response
    )
    assert manifest.digest == manifest_digest

    rows_a = _links(repo_a)
    assert {r.requested_content_checksum for r in rows_a} == {
        _digest("sha512", CONFIG),
        _digest("sha512", LAYER),
    }
    for row in rows_a:
        assert row.content_checksum is None
        assert not ImageStoragePlacement.select().where(ImageStoragePlacement.storage == row).exists()

    # Unfilled: HEAD returns the recorded size; GET streams from upstream (no local blob).
    layer_digest = _digest("sha512", LAYER)
    assert model_a.get_proxy_blob_size(ORG, "app/a", layer_digest) == len(LAYER)
    assert model_a.get_repo_blob_by_digest(repo_a, layer_digest) is None
    # Never served under its internal SHA-256.
    assert model_a.get_proxy_blob_size(ORG, "app/a", _digest("sha256", LAYER)) is None

    # Another repository gets its own placeholders.
    _, repo_b, _ = _pull(proxy_org, "app/b", manifest_digest, manifest_json, proxy_manifest_response)
    rows_b = _links(repo_b)
    assert {r.id for r in rows_a}.isdisjoint({r.id for r in rows_b})


def test_existing_stored_requested_row_is_linked(
    proxy_org, all_algorithms, proxy_manifest_response
):
    stored = ImageStorage.create(
        content_checksum=_digest("sha256", LAYER),
        requested_content_checksum=_digest("sha512", LAYER),
        image_size=len(LAYER),
    )
    ImageStoragePlacement.create(storage=stored, location=ImageStorageLocation.get(name="local_us"))

    manifest_json = _manifest("sha512")
    _, repo_ref, _ = _pull(
        proxy_org,
        "app/linked",
        _digest("sha512", manifest_json.encode()),
        manifest_json,
        proxy_manifest_response,
    )
    layer_rows = [r for r in _links(repo_ref) if r.requested_content_checksum == _digest("sha512", LAYER)]
    assert [r.id for r in layer_rows] == [stored.id]


def test_disallowed_descriptors_are_not_cached(proxy_org, proxy_manifest_response):
    manifest_json = _manifest("sha512")
    with pytest.raises(ManifestDoesNotExist):
        _pull(
            proxy_org,
            "app/denied",
            _digest("sha256", manifest_json.encode()),
            manifest_json,
            proxy_manifest_response,
        )


def _fill(repo_ref, data, digest):
    settings = BlobUploadSettings("10M", 3600)
    with upload_blob(repo_ref, storage, settings, requested_digest_algorithm="sha512") as m:
        m.upload_chunk({"TESTING": True}, BytesIO(data))
        return m.commit_to_blob({"TESTING": True}, digest)


def test_fill_placeholder_sets_sha256(proxy_org, all_algorithms, proxy_manifest_response):
    manifest_json = _manifest("sha512")
    _, repo_ref, _ = _pull(
        proxy_org,
        "app/fill",
        _digest("sha512", manifest_json.encode()),
        manifest_json,
        proxy_manifest_response,
    )
    layer_digest = _digest("sha512", LAYER)
    placeholder = [r for r in _links(repo_ref) if r.requested_content_checksum == layer_digest][0]

    blob = _fill(repo_ref, LAYER, layer_digest)

    assert blob._db_id == placeholder.id
    filled = ImageStorage.get(id=placeholder.id)
    assert filled.content_checksum == _digest("sha256", LAYER)
    assert ImageStoragePlacement.select().where(ImageStoragePlacement.storage == filled).exists()


def test_fill_repoints_to_existing_row(proxy_org, all_algorithms, proxy_manifest_response):
    manifest_json = _manifest("sha512")
    _, repo_ref, _ = _pull(
        proxy_org,
        "app/repoint",
        _digest("sha512", manifest_json.encode()),
        manifest_json,
        proxy_manifest_response,
    )
    layer_digest = _digest("sha512", LAYER)
    placeholder = [r for r in _links(repo_ref) if r.requested_content_checksum == layer_digest][0]

    # Meanwhile, the same bytes were published under this requested digest elsewhere, without a
    # placement yet at the time of the manifest pull.
    existing = ImageStorage.create(
        content_checksum=_digest("sha256", LAYER),
        requested_content_checksum=layer_digest,
        image_size=len(LAYER),
    )

    blob = _fill(repo_ref, LAYER, layer_digest)

    assert blob._db_id == existing.id
    layer_links = [r.id for r in _links(repo_ref) if r.requested_content_checksum == layer_digest]
    assert layer_links == [existing.id]
    # The placeholder was never filled and is now unlinked, so GC deletes it.
    assert ImageStorage.get(id=placeholder.id).content_checksum is None
    assert garbage_collect_storage([placeholder.id]) == {placeholder.id}
