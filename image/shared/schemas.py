from image.docker.schema1 import DOCKER_SCHEMA1_CONTENT_TYPES, DockerSchema1Manifest
from image.docker.schema2 import (
    DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
    DOCKER_SCHEMA2_MANIFESTLIST_CONTENT_TYPE,
)
from image.docker.schema2.list import DockerSchema2ManifestList
from image.docker.schema2.manifest import DockerSchema2Manifest
from image.oci import OCI_IMAGE_INDEX_CONTENT_TYPE, OCI_IMAGE_MANIFEST_CONTENT_TYPE
from image.oci.index import OCIIndex
from image.oci.manifest import OCIManifest
from image.shared import ManifestException
from digest import digest_tools
from image.shared.types import SparseManifestList
from util.bytes import Bytes

MANIFEST_LIST_TYPES = [DOCKER_SCHEMA2_MANIFESTLIST_CONTENT_TYPE, OCI_IMAGE_INDEX_CONTENT_TYPE]


def is_manifest_list_type(content_type):
    """Returns True if the given content type refers to a manifest list of some kind."""
    return content_type in MANIFEST_LIST_TYPES


def parse_manifest_from_bytes(
    manifest_bytes,
    media_type,
    validate=True,
    sparse_manifest_support=False,
    digest_algorithm=digest_tools.SHA256,
):
    """
    Parses and returns a manifest from the given bytes, for the given media type.

    `digest_algorithm` is the algorithm the manifest's `digest` is computed with (PROJQUAY-10117):
    the algorithm of the address for a push by digest, DEFAULT_MANIFEST_HASH_ALGORITHM for a push
    by tag, or the stored digest's algorithm when re-parsing. Docker schema 1 manifests are
    SHA-256 only.

    Raises a ManifestException if the parse fails for some reason.
    """
    assert isinstance(manifest_bytes, Bytes)
    if digest_algorithm not in digest_tools.SUPPORTED_DIGEST_ALGORITHMS:
        raise ManifestException("Unsupported manifest digest algorithm `%s`" % digest_algorithm)

    if is_manifest_list_type(media_type) and sparse_manifest_support:
        return SparseManifestList(manifest_bytes, media_type, digest_algorithm=digest_algorithm)

    if media_type == DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE:
        return DockerSchema2Manifest(manifest_bytes, digest_algorithm=digest_algorithm)

    if media_type == DOCKER_SCHEMA2_MANIFESTLIST_CONTENT_TYPE:
        return DockerSchema2ManifestList(manifest_bytes, digest_algorithm=digest_algorithm)

    if media_type == OCI_IMAGE_MANIFEST_CONTENT_TYPE:
        return OCIManifest(manifest_bytes, digest_algorithm=digest_algorithm)

    if media_type == OCI_IMAGE_INDEX_CONTENT_TYPE:
        return OCIIndex(manifest_bytes, digest_algorithm=digest_algorithm)

    if media_type in DOCKER_SCHEMA1_CONTENT_TYPES:
        if digest_algorithm != digest_tools.SHA256:
            raise ManifestException("Docker schema 1 manifests only support sha256 digests")
        return DockerSchema1Manifest(manifest_bytes, validate=validate)

    raise ManifestException("Unknown or unsupported manifest media type `%s`" % media_type)
