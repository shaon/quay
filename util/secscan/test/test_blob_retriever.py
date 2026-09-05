from unittest.mock import patch
from urllib.parse import urlsplit

from app import app as application
from app import instance_keys, storage
from auth.registry_jwt_auth import identity_from_bearer_token
from data import model
from data.database import ImageStorage
from data.registry_model import registry_model
from endpoints.v2 import v2_bp
from test.fixtures import *
from util.secscan.blob import BlobURLRetriever


def test_generate_url(initialized_db):
    try:
        application.register_blueprint(v2_bp, url_prefix="/v2")
    except:
        # Already registered.
        pass

    repo_ref = registry_model.lookup_repository("devtable", "simple")
    tag = registry_model.get_repo_tag(repo_ref, "latest")
    manifest = tag.manifest
    blobs = registry_model.get_manifest_local_blobs(manifest, storage)

    retriever = BlobURLRetriever(storage, instance_keys, application)
    headers = retriever.headers_for_download(repo_ref, blobs[0])

    if headers:
        identity, _ = identity_from_bearer_token(headers["Authorization"][0])
        assert len(identity.provides) == 1

        provide = list(identity.provides)[0]
        assert provide.role == "read"
        assert provide.name == "simple"
        assert provide.namespace == "devtable"
        assert provide.type == "repository"

        assert retriever.url_for_download(repo_ref, blobs[0]).startswith(
            "http://localhost:5000/v2/devtable/simple/blobs/"
        )

    # Direct download url from storage
    else:
        assert retriever.url_for_download(repo_ref, blobs[0]) == storage.get_direct_download_url(
            storage.locations, blobs[0].storage_path
        )


def test_registered_digest_download_url_serves_canonical_blob(initialized_db, client):
    try:
        application.register_blueprint(v2_bp, url_prefix="/v2")
    except ValueError:
        # Already registered.
        pass

    repo_ref = registry_model.lookup_repository("devtable", "simple")
    tag = registry_model.get_repo_tag(repo_ref, "latest")
    blobs = registry_model.get_manifest_local_blobs(tag.manifest, storage)
    canonical_blob = blobs[0]
    registered_digest = "sha512:" + "f" * 128
    model.oci.blob.register_repository_blob_digest(
        repo_ref.id,
        ImageStorage.get_by_id(canonical_blob._db_id),
        registered_digest,
    )

    retriever = BlobURLRetriever(storage, instance_keys, application)
    with (
        patch.object(storage, "get_direct_download_url", return_value=None),
        patch.dict(
            application.config,
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]},
        ),
    ):
        url = retriever.url_for_download(
            repo_ref,
            canonical_blob,
            repository_digest=registered_digest,
        )
        headers = retriever.headers_for_download(repo_ref, canonical_blob)
        response = client.get(
            urlsplit(url).path,
            headers={"Authorization": headers["Authorization"][0]},
        )

    assert url.endswith("/blobs/" + registered_digest)
    assert response.status_code == 200
    assert response.headers["Docker-Content-Digest"] == registered_digest
