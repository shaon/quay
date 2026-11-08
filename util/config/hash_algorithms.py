"""
Validation and accessors for the configurable client-visible digest algorithms (PROJQUAY-10117).

`ALLOWED_HASH_ALGORITHMS` lists the client-visible digest algorithms Quay accepts for reads and
writes. `sha256` is always allowed. `DEFAULT_MANIFEST_HASH_ALGORITHM` is the algorithm used for
tag-only manifest pushes; it must be supported and allowed. Internal blob storage stays SHA-256.
"""

from digest.digest_tools import SHA256, SUPPORTED_DIGEST_ALGORITHMS

DEFAULT_ALLOWED_HASH_ALGORITHMS = [SHA256]
DEFAULT_MANIFEST_HASH_ALGORITHM = SHA256


class InvalidHashAlgorithmConfig(Exception):
    pass


def validate_hash_algorithm_config(config):
    """
    Raises InvalidHashAlgorithmConfig if ALLOWED_HASH_ALGORITHMS or
    DEFAULT_MANIFEST_HASH_ALGORITHM is invalid.
    """
    allowed = config.get("ALLOWED_HASH_ALGORITHMS", DEFAULT_ALLOWED_HASH_ALGORITHMS)
    if not isinstance(allowed, (list, tuple)) or not allowed:
        raise InvalidHashAlgorithmConfig("ALLOWED_HASH_ALGORITHMS must be a non-empty list")

    for algorithm in allowed:
        if algorithm not in SUPPORTED_DIGEST_ALGORITHMS:
            raise InvalidHashAlgorithmConfig(
                "ALLOWED_HASH_ALGORITHMS contains unsupported algorithm %r; supported values are %s"
                % (algorithm, ", ".join(SUPPORTED_DIGEST_ALGORITHMS))
            )

    if SHA256 not in allowed:
        raise InvalidHashAlgorithmConfig(
            "ALLOWED_HASH_ALGORITHMS must include sha256; it cannot be disabled"
        )

    default = config.get("DEFAULT_MANIFEST_HASH_ALGORITHM", DEFAULT_MANIFEST_HASH_ALGORITHM)
    if default not in SUPPORTED_DIGEST_ALGORITHMS:
        raise InvalidHashAlgorithmConfig(
            "DEFAULT_MANIFEST_HASH_ALGORITHM %r is not a supported algorithm" % (default,)
        )
    if default not in allowed:
        raise InvalidHashAlgorithmConfig(
            "DEFAULT_MANIFEST_HASH_ALGORITHM %r is not listed in ALLOWED_HASH_ALGORITHMS"
            % (default,)
        )


def allowed_hash_algorithms(config):
    """
    Returns the frozenset of allowed client-visible digest algorithms.
    """
    return frozenset(config.get("ALLOWED_HASH_ALGORITHMS", DEFAULT_ALLOWED_HASH_ALGORITHMS))


def default_manifest_hash_algorithm(config):
    return config.get("DEFAULT_MANIFEST_HASH_ALGORITHM", DEFAULT_MANIFEST_HASH_ALGORITHM)
