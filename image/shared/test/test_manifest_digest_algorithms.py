"""
PROJQUAY-10117: manifest digests follow the algorithm the manifest is addressed by.
"""

import hashlib
import json

import pytest

from image.docker.schema1 import (
    DOCKER_SCHEMA1_MANIFEST_CONTENT_TYPE,
    DockerSchema1Manifest,
)
from image.docker.schema2 import (
    DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
    DOCKER_SCHEMA2_MANIFESTLIST_CONTENT_TYPE,
)
from image.oci import OCI_IMAGE_INDEX_CONTENT_TYPE, OCI_IMAGE_MANIFEST_CONTENT_TYPE
from image.shared import ManifestException
from image.shared.schemas import parse_manifest_from_bytes
from image.shared.schemautil import (
    ContentRetrieverForTesting,
    is_sha256_only,
    manifest_digest_algorithm,
)
from util.bytes import Bytes


def _digest(algorithm, data):
    return "%s:%s" % (algorithm, hashlib.new(algorithm, data).hexdigest())


CONFIG = json.dumps(
    {
        "architecture": "amd64",
        "os": "linux",
        "rootfs": {"type": "layers", "diff_ids": []},
        "history": [{"created_by": "x"}],
    }
).encode()


def _image_manifest(media_type, config_type, layer_type, algorithm):
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": media_type,
            "config": {"mediaType": config_type, "digest": _digest(algorithm, CONFIG), "size": len(CONFIG)},
            "layers": [{"mediaType": layer_type, "digest": _digest(algorithm, b"layer"), "size": 5}],
        }
    ).encode()


def _oci_manifest(algorithm="sha256"):
    return _image_manifest(
        OCI_IMAGE_MANIFEST_CONTENT_TYPE,
        "application/vnd.oci.image.config.v1+json",
        "application/vnd.oci.image.layer.v1.tar+gzip",
        algorithm,
    )


def _docker_manifest(algorithm="sha256"):
    return _image_manifest(
        DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
        "application/vnd.docker.container.image.v1+json",
        "application/vnd.docker.image.rootfs.diff.tar.gzip",
        algorithm,
    )


def _list(media_type, child_type, child_bytes, child_algorithm):
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": media_type,
            "manifests": [
                {
                    "mediaType": child_type,
                    "digest": _digest(child_algorithm, child_bytes),
                    "size": len(child_bytes),
                    "platform": {"architecture": "amd64", "os": "linux"},
                }
            ],
        }
    ).encode()


def _all_kinds(blob_algorithm="sha256", child_algorithm="sha256"):
    oci = _oci_manifest(blob_algorithm)
    docker = _docker_manifest(blob_algorithm)
    return [
        (OCI_IMAGE_MANIFEST_CONTENT_TYPE, oci),
        (DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE, docker),
        (
            OCI_IMAGE_INDEX_CONTENT_TYPE,
            _list(OCI_IMAGE_INDEX_CONTENT_TYPE, OCI_IMAGE_MANIFEST_CONTENT_TYPE, oci, child_algorithm),
        ),
        (
            DOCKER_SCHEMA2_MANIFESTLIST_CONTENT_TYPE,
            _list(
                DOCKER_SCHEMA2_MANIFESTLIST_CONTENT_TYPE,
                DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
                docker,
                child_algorithm,
            ),
        ),
    ]


@pytest.mark.parametrize("algorithm", ["sha256", "sha384", "sha512"])
@pytest.mark.parametrize("media_type, manifest_bytes", _all_kinds())
def test_digest_follows_algorithm(algorithm, media_type, manifest_bytes):
    parsed = parse_manifest_from_bytes(
        Bytes.for_string_or_unicode(manifest_bytes), media_type, digest_algorithm=algorithm
    )
    assert parsed.digest == _digest(algorithm, manifest_bytes)


@pytest.mark.parametrize("media_type, manifest_bytes", _all_kinds())
def test_default_is_sha256(media_type, manifest_bytes):
    parsed = parse_manifest_from_bytes(Bytes.for_string_or_unicode(manifest_bytes), media_type)
    assert parsed.digest == _digest("sha256", manifest_bytes)


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_sparse_list_digest_follows_algorithm(algorithm):
    _, index = _all_kinds()[2]
    parsed = parse_manifest_from_bytes(
        Bytes.for_string_or_unicode(index),
        OCI_IMAGE_INDEX_CONTENT_TYPE,
        sparse_manifest_support=True,
        digest_algorithm=algorithm,
    )
    assert parsed.digest == _digest(algorithm, index)


@pytest.mark.parametrize("algorithm", ["sha384", "sha512", "md5"])
def test_schema1_is_sha256_only(algorithm):
    with pytest.raises(ManifestException):
        parse_manifest_from_bytes(
            Bytes.for_string_or_unicode(b"{}"),
            DOCKER_SCHEMA1_MANIFEST_CONTENT_TYPE,
            digest_algorithm=algorithm,
        )
    if algorithm != "md5":
        with pytest.raises(ManifestException):
            DockerSchema1Manifest(Bytes.for_string_or_unicode(b"{}"), digest_algorithm=algorithm)


@pytest.mark.parametrize(
    "digest, expected",
    [
        ("sha256:" + "a" * 64, "sha256"),
        ("sha384:" + "a" * 96, "sha384"),
        ("sha512:" + "a" * 128, "sha512"),
        ("md5:abc", "sha256"),
        (None, "sha256"),
    ],
)
def test_manifest_digest_algorithm(digest, expected):
    assert manifest_digest_algorithm(digest) == expected


@pytest.mark.parametrize("child_algorithm", ["sha384", "sha512"])
@pytest.mark.parametrize("kind", [2, 3])
def test_list_children_load_with_their_algorithm(child_algorithm, kind):
    media_type, list_bytes = _all_kinds(child_algorithm=child_algorithm)[kind]
    child_media_type, child_bytes = _all_kinds()[kind - 2]
    retriever = ContentRetrieverForTesting()
    retriever.add_digest(_digest(child_algorithm, child_bytes), child_bytes)

    parsed = parse_manifest_from_bytes(Bytes.for_string_or_unicode(list_bytes), media_type)
    children = parsed.manifests(retriever)
    assert children[0].manifest_obj.digest == _digest(child_algorithm, child_bytes)


@pytest.mark.parametrize("blob_algorithm", ["sha384", "sha512"])
@pytest.mark.parametrize("kind", [0, 1])
def test_no_schema1_for_non_sha256_blobs(blob_algorithm, kind):
    media_type, manifest_bytes = _all_kinds(blob_algorithm=blob_algorithm)[kind]
    parsed = parse_manifest_from_bytes(Bytes.for_string_or_unicode(manifest_bytes), media_type)
    assert not is_sha256_only(parsed)
    assert parsed.get_schema1_manifest("ns", "repo", "tag", ContentRetrieverForTesting()) is None


@pytest.mark.parametrize("kind", [0, 1])
def test_no_schema1_for_non_sha256_manifest_digest(kind):
    media_type, manifest_bytes = _all_kinds()[kind]
    parsed = parse_manifest_from_bytes(
        Bytes.for_string_or_unicode(manifest_bytes), media_type, digest_algorithm="sha512"
    )
    assert not is_sha256_only(parsed)
    assert parsed.get_schema1_manifest("ns", "repo", "tag", ContentRetrieverForTesting()) is None


@pytest.mark.parametrize("child_algorithm", ["sha384", "sha512"])
@pytest.mark.parametrize("kind", [2, 3])
def test_no_schema1_for_lists_with_non_sha256_children(child_algorithm, kind):
    media_type, list_bytes = _all_kinds(child_algorithm=child_algorithm)[kind]
    parsed = parse_manifest_from_bytes(Bytes.for_string_or_unicode(list_bytes), media_type)
    assert not is_sha256_only(parsed)
    assert parsed.get_schema1_manifest("ns", "repo", "tag", ContentRetrieverForTesting()) is None


@pytest.mark.parametrize("media_type, manifest_bytes", _all_kinds())
def test_sha256_only(media_type, manifest_bytes):
    parsed = parse_manifest_from_bytes(Bytes.for_string_or_unicode(manifest_bytes), media_type)
    assert is_sha256_only(parsed)
