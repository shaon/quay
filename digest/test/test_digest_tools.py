import hashlib
import json

import pytest

from digest import digest_tools
from digest.digest_tools import Digest, InvalidDigestException, content_path


@pytest.mark.parametrize(
    "digest, output_args",
    [
        ("tarsum.v123123+sha1:123deadbeef", ("tarsum.v123123+sha1", "123deadbeef")),
        ("tarsum.v1+sha256:123123", ("tarsum.v1+sha256", "123123")),
        ("tarsum.v0+md5:abc", ("tarsum.v0+md5", "abc")),
        ("tarsum+sha1:abc", ("tarsum+sha1", "abc")),
        ("sha1:123deadbeef", ("sha1", "123deadbeef")),
        ("sha256:123123", ("sha256", "123123")),
        ("md5:abc", ("md5", "abc")),
    ],
)
def test_parse_good(digest, output_args):
    assert Digest.parse_digest(digest) == Digest(*output_args)
    assert str(Digest.parse_digest(digest)) == digest


@pytest.mark.parametrize(
    "bad_digest",
    [
        "tarsum.v+md5:abc:",
        "sha1:123deadbeefzxczxv",
        "sha256123123",
        "tarsum.v1+",
        "tarsum.v1123+sha1:",
        "sha256:👌",
    ],
)
def test_parse_fail(bad_digest):
    with pytest.raises(InvalidDigestException):
        Digest.parse_digest(bad_digest)


@pytest.mark.parametrize(
    "digest, path",
    [
        ("tarsum.v123123+sha1:123deadbeef", "tarsum/v123123/sha1/12/123deadbeef"),
        ("tarsum.v1+sha256:123123", "tarsum/v1/sha256/12/123123"),
        ("tarsum.v0+md5:abc", "tarsum/v0/md5/ab/abc"),
        ("sha1:123deadbeef", "sha1/12/123deadbeef"),
        ("sha256:123123", "sha256/12/123123"),
        ("md5:abc", "md5/ab/abc"),
        ("md5:1", "md5/01/1"),
        ("md5.....+++:1", "md5/01/1"),
        (".md5.:1", "md5/01/1"),
    ],
)
def test_paths(digest, path):
    assert content_path(digest) == path


@pytest.mark.parametrize(
    "algorithm",
    [
        "sha256",
        "sha384",
        "sha512",
    ],
)
def test_parse_strict_digest(algorithm):
    encoded = hashlib.new(algorithm, b"hello world").hexdigest()
    parsed = Digest.parse_digest(f"{algorithm}:{encoded}", strict=True)

    assert parsed.hash_alg == algorithm
    assert parsed.hash_bytes == encoded


@pytest.mark.parametrize(
    "bad_digest",
    [
        "sha256:1234",
        "sha384:1234",
        "sha512:1234",
        "sha256:" + "A" * 64,
        "sha384:" + "a" * 95,
        "sha384:" + "a" * 97,
        "sha384:" + "A" * 96,
        "sha512:" + "a" * 127,
        "sha999:" + "a" * 96,
        "SHA256:" + "a" * 64,
    ],
)
def test_parse_strict_digest_rejects_malformed_or_unsupported(bad_digest):
    with pytest.raises(InvalidDigestException):
        Digest.parse_digest(bad_digest, strict=True)


@pytest.mark.parametrize("algorithm", ["sha256", "sha384", "sha512"])
def test_digest_bytes_uses_exact_content_and_algorithm(algorithm):
    content = b'{"whitespace": true}\n'

    assert digest_tools.digest_bytes(algorithm, content) == (
        f"{algorithm}:" + hashlib.new(algorithm, content).hexdigest()
    )


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_resumable_hasher_state_roundtrip_without_pickle(algorithm):
    hasher = digest_tools.create_resumable_hasher(algorithm)
    hasher.update(b"hello ")
    serialized = digest_tools.serialize_resumable_hasher(algorithm, hasher, bytes_hashed=6)

    restored = digest_tools.restore_resumable_hasher(algorithm, serialized, expected_byte_count=6)
    restored.update(b"world")

    assert restored.hexdigest() == hashlib.new(algorithm, b"hello world").hexdigest()


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_resumable_hasher_state_rejects_corruption(algorithm):
    hasher = digest_tools.create_resumable_hasher(algorithm)
    envelope = json.loads(digest_tools.serialize_resumable_hasher(algorithm, hasher))
    envelope["state"] = "bm90IGEgdmFsaWQgc3RhdGU="

    with pytest.raises(InvalidDigestException, match=f"Invalid persisted {algorithm} digest state"):
        digest_tools.restore_resumable_hasher(algorithm, json.dumps(envelope))


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_resumable_hasher_state_rejects_incompatible_architecture(algorithm, monkeypatch):
    hasher = digest_tools.create_resumable_hasher(algorithm)
    serialized = digest_tools.serialize_resumable_hasher(algorithm, hasher)
    monkeypatch.setattr(digest_tools.platform, "machine", lambda: "different-architecture")

    with pytest.raises(InvalidDigestException, match=f"Invalid persisted {algorithm} digest state"):
        digest_tools.restore_resumable_hasher(algorithm, serialized)


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_resumable_hasher_state_rejects_stale_byte_count(algorithm):
    hasher = digest_tools.create_resumable_hasher(algorithm)
    hasher.update(b"first chunk")
    serialized = digest_tools.serialize_resumable_hasher(algorithm, hasher, bytes_hashed=11)

    with pytest.raises(InvalidDigestException, match=f"Invalid persisted {algorithm} digest state"):
        digest_tools.restore_resumable_hasher(algorithm, serialized, expected_byte_count=12)


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_resumable_hasher_state_rejects_wrong_algorithm(algorithm):
    other_algorithm = "sha512" if algorithm == "sha384" else "sha384"
    hasher = digest_tools.create_resumable_hasher(algorithm)
    serialized = digest_tools.serialize_resumable_hasher(algorithm, hasher)

    with pytest.raises(
        InvalidDigestException, match=f"Invalid persisted {other_algorithm} digest state"
    ):
        digest_tools.restore_resumable_hasher(other_algorithm, serialized)


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_resumable_hasher_state_rejects_version_mismatch(algorithm):
    hasher = digest_tools.create_resumable_hasher(algorithm)
    envelope = json.loads(digest_tools.serialize_resumable_hasher(algorithm, hasher))
    envelope["format"] = digest_tools.RESUMABLE_HASH_STATE_VERSION + 1

    with pytest.raises(InvalidDigestException, match=f"Invalid persisted {algorithm} digest state"):
        digest_tools.restore_resumable_hasher(algorithm, json.dumps(envelope))


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_resumable_hasher_state_records_hintless_origin(algorithm):
    hasher = digest_tools.create_resumable_hasher(algorithm)
    serialized = digest_tools.serialize_resumable_hasher(
        algorithm,
        hasher,
        origin=digest_tools.RESUMABLE_HASH_ORIGIN_HINTLESS,
    )

    assert (
        digest_tools.resumable_hasher_state_origin(algorithm, serialized)
        == digest_tools.RESUMABLE_HASH_ORIGIN_HINTLESS
    )


def test_sha384_digest_comparison():
    digest = digest_tools.digest_bytes("sha384", b"same content")

    assert digest_tools.digests_equal(digest, Digest.parse_digest(digest, strict=True))
    assert not digest_tools.digests_equal(
        digest,
        digest_tools.digest_bytes("sha384", b"different content"),
    )


def test_strict_digest_errors_distinguish_unsupported_and_malformed():
    with pytest.raises(digest_tools.UnsupportedDigestAlgorithmException):
        Digest.parse_digest("sha999:" + "a" * 96, strict=True)

    with pytest.raises(digest_tools.MalformedDigestException):
        Digest.parse_digest("sha384:1234", strict=True)
