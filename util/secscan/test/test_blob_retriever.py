from test.fixtures import *

from app import app as application
from app import instance_keys, storage
from auth.registry_jwt_auth import identity_from_bearer_token
from data.registry_model import registry_model
from endpoints.v2 import v2_bp
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


def test_layer_url_uses_published_digest(initialized_db):
    """PROJQUAY-10117: the registry layer URL given to Clair uses the digest published in the
    repository, not the internal SHA-256."""
    import hashlib
    from datetime import datetime, timedelta

    from data.database import (
        ImageStorage,
        ImageStorageLocation,
        ImageStoragePlacement,
        UploadedBlob,
    )
    from data.model import repository

    try:
        application.register_blueprint(v2_bp, url_prefix="/v2")
    except:
        pass

    content = b"clair layer bytes"
    sha256 = "sha256:" + hashlib.sha256(content).hexdigest()
    sha512 = "sha512:" + hashlib.sha512(content).hexdigest()
    storage.put_content(["local_us"], storage.blob_path(sha256), content)

    repo = repository.get_repository("devtable", "simple")
    row = ImageStorage.create(
        content_checksum=sha256,
        requested_content_checksum=sha512,
        image_size=len(content),
        uploading=False,
    )
    ImageStoragePlacement.create(storage=row, location=ImageStorageLocation.get(name="local_us"))
    UploadedBlob.create(
        repository=repo, blob=row, expires_at=datetime.utcnow() + timedelta(hours=1)
    )

    repo_ref = registry_model.lookup_repository("devtable", "simple")
    blob = registry_model.get_repo_blob_by_digest(repo_ref, sha512, include_placements=True)
    assert blob.digest == sha512

    retriever = BlobURLRetriever(storage, instance_keys, application)
    if retriever.headers_for_download(repo_ref, blob):
        url = retriever.url_for_download(repo_ref, blob)
        assert url.endswith("/v2/devtable/simple/blobs/%s" % sha512)
        assert sha256 not in url
    else:
        # Signed direct-download URLs point at the CAS path and are unaffected.
        assert retriever.url_for_download(repo_ref, blob) == storage.get_direct_download_url(
            storage.locations, blob.storage_path
        )
