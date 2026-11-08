"""
PROJQUAY-10117: `?tag=` assigns every tag in one transaction; a failure commits nothing.
"""

import hashlib
import json
from test.fixtures import *
from unittest.mock import patch

import pytest

from app import storage
from data.database import Manifest, Tag, db
from data.model import ImmutableTagException
from data.model.oci import tag as oci_tag
from data.registry_model.registry_oci_model import OCIModel
from image.shared.schemas import parse_manifest_from_bytes
from util.bytes import Bytes


@pytest.fixture()
def registry_model(initialized_db):
    return OCIModel()


def _artifact_manifest(label):
    empty = b"{}"
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "artifactType": "application/vnd.test." + label,
            "config": {
                "mediaType": "application/vnd.oci.empty.v1+json",
                "digest": "sha256:" + hashlib.sha256(empty).hexdigest(),
                "size": 2,
            },
            "layers": [],
        }
    ).encode()


def _parsed(manifest_bytes, algorithm):
    return parse_manifest_from_bytes(
        Bytes.for_string_or_unicode(manifest_bytes),
        "application/vnd.oci.image.manifest.v1+json",
        digest_algorithm=algorithm,
    )


@pytest.fixture()
def repo_ref(registry_model):
    from data.model.blob import store_blob_record_and_temp_link_in_repo
    from data.model.storage import get_image_location_for_name

    ref = registry_model.lookup_repository("devtable", "simple")
    empty = b"{}"
    digest = "sha256:" + hashlib.sha256(empty).hexdigest()
    storage.put_content(["local_us"], storage.blob_path(digest), empty)
    store_blob_record_and_temp_link_in_repo(
        ref._db_id, digest, get_image_location_for_name("local_us").id, 2, 3600
    )
    return ref


@pytest.mark.parametrize("algorithm", ["sha256", "sha384", "sha512"])
def test_assigns_all_tags(algorithm, registry_model, repo_ref):
    parsed = _parsed(_artifact_manifest("ok-" + algorithm), algorithm)
    manifest, tags = registry_model.create_manifest_and_retarget_tags(
        repo_ref, parsed, ["one", "two"], storage, raise_on_error=True
    )
    assert manifest.digest == parsed.digest
    assert [t.name for t in tags] == ["one", "two"]
    for name in ("one", "two"):
        assert registry_model.get_repo_tag(repo_ref, name).manifest_digest == parsed.digest


def test_failure_on_a_later_tag_rolls_back_everything(registry_model, repo_ref):
    parsed = _parsed(_artifact_manifest("rollback"), "sha512")
    real_retarget = oci_tag.retarget_tag
    calls = {"n": 0}

    def failing_retarget(tag_name, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ImmutableTagException(tag_name, "overwrite", repo_ref._db_id)
        return real_retarget(tag_name, *args, **kwargs)

    with patch(
        "data.registry_model.registry_oci_model.db_transaction", lambda: db.atomic()
    ), patch.object(oci_tag, "retarget_tag", side_effect=failing_retarget):
        with pytest.raises(ImmutableTagException):
            registry_model.create_manifest_and_retarget_tags(
                repo_ref, parsed, ["first", "second"], storage, raise_on_error=True
            )

    assert not Manifest.select().where(Manifest.digest == parsed.digest).exists()
    assert registry_model.get_repo_tag(repo_ref, "first") is None
    assert registry_model.get_repo_tag(repo_ref, "second") is None


def test_immutable_tag_precheck_creates_nothing(registry_model, repo_ref):
    import features

    existing = _parsed(_artifact_manifest("existing"), "sha256")
    registry_model.create_manifest_and_retarget_tags(
        repo_ref, existing, ["locked"], storage, raise_on_error=True
    )
    oci_tag.set_tag_immutable(repo_ref._db_id, "locked", True)

    parsed = _parsed(_artifact_manifest("new"), "sha512")
    with patch.object(features, "IMMUTABLE_TAGS", True):
        with pytest.raises(ImmutableTagException):
            registry_model.create_manifest_and_retarget_tags(
                repo_ref, parsed, ["fresh", "locked"], storage, raise_on_error=True
            )

    assert not Manifest.select().where(Manifest.digest == parsed.digest).exists()
    assert registry_model.get_repo_tag(repo_ref, "fresh") is None
    assert registry_model.get_repo_tag(repo_ref, "locked").manifest_digest == existing.digest
