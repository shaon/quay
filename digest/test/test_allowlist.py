import pytest

from digest.allowlist import (
    DigestCheck,
    check_digest,
    is_algorithm_allowed,
    is_stored_digest_allowed,
)

SHA256 = "sha256:" + "a" * 64
SHA384 = "sha384:" + "b" * 96
SHA512 = "sha512:" + "c" * 128

DEFAULT: dict = {}
ALL = {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384", "sha512"]}
ONLY_512 = {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha512"]}


@pytest.mark.parametrize(
    "digest, config, expected",
    [
        # SHA-256 requests keep today's handling, even when malformed.
        (SHA256, DEFAULT, DigestCheck.LEGACY),
        ("sha256:ABC", DEFAULT, DigestCheck.LEGACY),
        ("sha256:short", ALL, DigestCheck.LEGACY),
        # Other algorithms are rejected at the default allowlist.
        (SHA384, DEFAULT, DigestCheck.REJECTED),
        (SHA512, DEFAULT, DigestCheck.REJECTED),
        ("md5:abc", DEFAULT, DigestCheck.REJECTED),
        ("SHA256:" + "a" * 64, DEFAULT, DigestCheck.REJECTED),
        # Allowed when listed and strict.
        (SHA384, ALL, DigestCheck.ALLOWED),
        (SHA512, ALL, DigestCheck.ALLOWED),
        (SHA512, ONLY_512, DigestCheck.ALLOWED),
        (SHA384, ONLY_512, DigestCheck.REJECTED),
        # Malformed non-SHA-256 digests are rejected even when the algorithm is allowed.
        ("sha512:" + "C" * 128, ALL, DigestCheck.REJECTED),
        ("sha512:" + "c" * 127, ALL, DigestCheck.REJECTED),
        ("sha1:" + "a" * 40, ALL, DigestCheck.REJECTED),
    ],
)
def test_check_digest(digest, config, expected):
    assert check_digest(digest, config) == expected


@pytest.mark.parametrize(
    "algorithm, config, expected",
    [
        ("sha256", DEFAULT, True),
        ("sha512", DEFAULT, False),
        ("sha512", ALL, True),
        ("sha384", ONLY_512, False),
        ("md5", ALL, False),
        ("SHA512", ALL, False),
    ],
)
def test_is_algorithm_allowed(algorithm, config, expected):
    assert is_algorithm_allowed(algorithm, config) == expected


def test_is_stored_digest_allowed():
    assert is_stored_digest_allowed(SHA256, DEFAULT)
    assert not is_stored_digest_allowed(SHA512, DEFAULT)
    assert is_stored_digest_allowed(SHA512, ALL)
    assert not is_stored_digest_allowed(SHA384, ONLY_512)
