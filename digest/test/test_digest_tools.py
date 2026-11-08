import pytest

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


# PROJQUAY-10117: strict parsing and algorithm-generic hashing.

import hashlib

from digest.digest_tools import (
    compute_digest,
    compute_digest_from_generator,
    digest_algorithm,
    digest_from_hasher,
    is_strict_digest,
    new_hasher,
    normalize_digest,
    parse_strict_digest,
    sha256_digest,
)

_SHA256_HEX = "a" * 64
_SHA384_HEX = "b" * 96
_SHA512_HEX = "c" * 128


@pytest.mark.parametrize(
    "digest",
    ["sha256:" + _SHA256_HEX, "sha384:" + _SHA384_HEX, "sha512:" + _SHA512_HEX],
)
def test_parse_strict_good(digest):
    parsed = parse_strict_digest(digest)
    assert str(parsed) == digest
    assert normalize_digest(digest) == digest
    assert is_strict_digest(digest)


@pytest.mark.parametrize(
    "digest",
    [
        "sha256:" + _SHA256_HEX.upper(),
        "SHA256:" + _SHA256_HEX,
        "sha512:" + _SHA512_HEX[:-1],
        "sha512:" + _SHA512_HEX + "c",
        "sha384:" + _SHA256_HEX,
        "sha256:" + _SHA512_HEX,
        "sha1:" + "a" * 40,
        "md5:abc",
        "sha512:" + "g" * 128,
        "sha512" + _SHA512_HEX,
        "sha512:",
        "",
        None,
        " sha256:" + _SHA256_HEX,
        "sha256:" + _SHA256_HEX + "\n",
    ],
)
def test_parse_strict_bad(digest):
    with pytest.raises(InvalidDigestException):
        parse_strict_digest(digest)
    assert not is_strict_digest(digest)


@pytest.mark.parametrize("algorithm", ["sha256", "sha384", "sha512"])
def test_compute_digest(algorithm):
    content = b"hello world"
    expected = "%s:%s" % (algorithm, hashlib.new(algorithm, content).hexdigest())
    assert compute_digest(content, algorithm) == expected
    assert compute_digest_from_generator(iter([b"hello", b" ", b"world"]), algorithm) == expected
    hasher = new_hasher(algorithm)
    hasher.update(content)
    assert digest_from_hasher(hasher) == expected
    assert is_strict_digest(expected)


def test_compute_digest_default_matches_sha256_digest():
    assert compute_digest(b"abc") == sha256_digest(b"abc")


@pytest.mark.parametrize("algorithm", ["md5", "sha1", "sha224", "SHA512"])
def test_new_hasher_unsupported(algorithm):
    with pytest.raises(InvalidDigestException):
        new_hasher(algorithm)


@pytest.mark.parametrize(
    "digest, expected",
    [("sha512:abc", "sha512"), ("tarsum.v1+sha256:1", "tarsum.v1+sha256"), ("nocolon", None)],
)
def test_digest_algorithm(digest, expected):
    assert digest_algorithm(digest) == expected
