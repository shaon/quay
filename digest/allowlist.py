"""
The single allowlist check for client-visible digests, shared by `/v2` and `/api/v1`.

The Error codes table in the PROJQUAY-10117 design applies only to requests that name an algorithm
other than SHA-256: those must be strict (supported algorithm, lowercase hex of the exact length)
and allowed by ALLOWED_HASH_ALGORITHMS. Requests naming `sha256` keep today's handling, so
`check_digest` reports them as LEGACY and the caller proceeds as before.
"""

from enum import Enum

from digest import digest_tools
from util.config.hash_algorithms import allowed_hash_algorithms


class DigestCheck(Enum):
    # The digest names `sha256` (exact, lowercase prefix). Callers keep today's handling.
    LEGACY = "legacy"
    # Strict, supported and allowed non-SHA-256 digest.
    ALLOWED = "allowed"
    # Unsupported, disallowed, or malformed non-SHA-256 digest.
    REJECTED = "rejected"


def is_sha256_request(digest_str):
    return digest_tools.digest_algorithm(digest_str) == digest_tools.SHA256


def check_digest(digest_str, config):
    """
    Classifies a client-supplied digest string against the allowlist in `config`.
    """
    if is_sha256_request(digest_str):
        return DigestCheck.LEGACY

    try:
        parsed = digest_tools.parse_strict_digest(digest_str)
    except digest_tools.InvalidDigestException:
        return DigestCheck.REJECTED

    if parsed.hash_alg not in allowed_hash_algorithms(config):
        return DigestCheck.REJECTED

    return DigestCheck.ALLOWED


def is_digest_rejected(digest_str, config):
    return check_digest(digest_str, config) == DigestCheck.REJECTED


def is_algorithm_allowed(algorithm, config):
    """
    Returns True if `algorithm` is supported and allowed. Used for the `digest-algorithm` hint.
    """
    return (
        algorithm in digest_tools.SUPPORTED_DIGEST_ALGORITHMS
        and algorithm in allowed_hash_algorithms(config)
    )


def manifest_referenced_digests(parsed_manifest):
    """
    Returns the digests a parsed manifest references: blobs (layers and config), child manifests
    of a list, and the subject, if any.
    """
    referenced = [str(d) for d in (parsed_manifest.blob_digests or [])]
    if parsed_manifest.is_manifest_list:
        for child in parsed_manifest.manifest_dict.get("manifests", []):
            if isinstance(child, dict) and "digest" in child:
                referenced.append(str(child["digest"]))

    subject = getattr(parsed_manifest, "subject", None)
    if subject is not None:
        referenced.append(str(subject.digest))
    return referenced


def first_rejected_manifest_reference(parsed_manifest, config):
    """
    Returns the first digest referenced by the manifest (blob, child manifest or subject) that is
    an unsupported, disallowed or malformed non-SHA-256 digest, or None.
    """
    for digest_str in manifest_referenced_digests(parsed_manifest):
        if is_digest_rejected(digest_str, config):
            return digest_str
    return None


def is_stored_digest_allowed(digest_str, config):
    """
    Returns True if a digest recorded in the database (always normalized) may be served under the
    current allowlist. SHA-256 is always allowed.
    """
    algorithm = digest_tools.digest_algorithm(digest_str)
    if algorithm == digest_tools.SHA256:
        return True
    return algorithm in allowed_hash_algorithms(config)
