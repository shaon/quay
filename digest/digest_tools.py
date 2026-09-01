import base64
import binascii
import hashlib
import json
import os.path
import platform
import re
import sys

DIGEST_PATTERN = r"([A-Za-z0-9_+.-]+):([A-Fa-f0-9]+)"
DIGEST_ALGORITHM_LENGTHS = {
    "sha256": 64,
    "sha384": 96,
    "sha512": 128,
}
REPLACE_WITH_PATH = re.compile(r"[+.]")
REPLACE_DOUBLE_SLASHES = re.compile(r"/+")
RESUMABLE_HASH_STATE_VERSION = 1
RESUMABLE_HASH_ORIGIN_HINT = "hint"
RESUMABLE_HASH_ORIGIN_HINTLESS = "hintless"
RESUMABLE_HASH_ORIGINS = {
    RESUMABLE_HASH_ORIGIN_HINT,
    RESUMABLE_HASH_ORIGIN_HINTLESS,
}


class InvalidDigestException(RuntimeError):
    pass


class UnsupportedDigestAlgorithmException(InvalidDigestException):
    pass


class MalformedDigestException(InvalidDigestException):
    pass


class Digest(object):
    DIGEST_REGEX = re.compile(DIGEST_PATTERN)

    def __init__(self, hash_alg, hash_bytes):
        self._hash_alg = hash_alg
        self._hash_bytes = hash_bytes

    def __str__(self):
        return "{0}:{1}".format(self._hash_alg, self._hash_bytes)

    def __eq__(self, rhs):
        return isinstance(rhs, Digest) and str(self) == str(rhs)

    def __hash__(self):
        return hash((self._hash_alg, self._hash_bytes))

    @staticmethod
    def parse_digest(digest, strict=False):
        """
        Returns the digest parsed out to its components.

        When strict is true, the digest must use a supported algorithm and its encoded value must
        be lowercase hexadecimal of the exact length defined for that algorithm.
        """
        if not isinstance(digest, str):
            raise MalformedDigestException("Not a valid digest: %s" % digest)

        match = Digest.DIGEST_REGEX.match(digest)
        if match is None or match.end() != len(digest):
            raise MalformedDigestException("Not a valid digest: %s" % digest)

        hash_alg = match.group(1)
        hash_bytes = match.group(2)
        if strict:
            expected_length = DIGEST_ALGORITHM_LENGTHS.get(hash_alg)
            if expected_length is None:
                raise UnsupportedDigestAlgorithmException(
                    "Unsupported digest algorithm: %s" % hash_alg
                )
            if hash_bytes != hash_bytes.lower() or len(hash_bytes) != expected_length:
                raise MalformedDigestException("Not a valid %s digest: %s" % (hash_alg, digest))

        return Digest(hash_alg, hash_bytes)

    @property
    def hash_alg(self):
        return self._hash_alg

    @property
    def hash_bytes(self):
        return self._hash_bytes


def content_path(digest):
    """
    Returns a relative path to the parsed digest.
    """
    parsed = Digest.parse_digest(digest)
    components = []

    # Generate a prefix which is always two characters, and which will be filled with leading zeros
    # if the input does not contain at least two characters. e.g. ABC -> AB, A -> 0A
    prefix = parsed.hash_bytes[0:2].zfill(2)
    pathish = REPLACE_WITH_PATH.sub("/", parsed.hash_alg)
    normalized = REPLACE_DOUBLE_SLASHES.sub("/", pathish).lstrip("/")
    components.extend([normalized, prefix, parsed.hash_bytes])
    return os.path.join(*components)


def digest_bytes(hash_alg, content):
    """Returns a digest of the exact content bytes using the specified algorithm."""
    assert isinstance(content, bytes)
    if hash_alg not in DIGEST_ALGORITHM_LENGTHS:
        raise UnsupportedDigestAlgorithmException("Unsupported digest algorithm: %s" % hash_alg)
    return "{0}:{1}".format(hash_alg, hashlib.new(hash_alg, content).hexdigest())


def sha256_digest(content):
    """
    Returns a sha256 hash of the content bytes in digest form.
    """
    return digest_bytes("sha256", content)


def sha256_digest_from_generator(content_generator):
    """
    Reads all of the data from the iterator and creates a sha256 digest from the content.
    """
    digest = hashlib.sha256()
    for chunk in content_generator:
        digest.update(chunk)
    return "sha256:{0}".format(digest.hexdigest())


def digest_from_hashlib(hash_alg, hash_obj):
    return "{0}:{1}".format(hash_alg, hash_obj.hexdigest())


def sha256_digest_from_hashlib(sha256_hash_obj):
    return digest_from_hashlib("sha256", sha256_hash_obj)


def create_resumable_hasher(hash_alg):
    if hash_alg not in DIGEST_ALGORITHM_LENGTHS:
        raise UnsupportedDigestAlgorithmException("Unsupported digest algorithm: %s" % hash_alg)

    import resumablehash

    return resumablehash.new(hash_alg)


def serialize_resumable_hasher(
    hash_alg,
    hasher,
    bytes_hashed=0,
    origin=RESUMABLE_HASH_ORIGIN_HINT,
):
    if getattr(hasher, "name", None) != hash_alg:
        raise InvalidDigestException("Hasher algorithm does not match %s" % hash_alg)
    if type(bytes_hashed) is not int or bytes_hashed < 0:
        raise InvalidDigestException("Hasher byte count must be a nonnegative integer")
    if origin not in RESUMABLE_HASH_ORIGINS:
        raise InvalidDigestException("Unknown resumable hash origin: %s" % origin)

    state = hasher.__getstate__()
    envelope = {
        "algorithm": hash_alg,
        "architecture": platform.machine(),
        "byteorder": sys.byteorder,
        "bytes_hashed": bytes_hashed,
        "format": RESUMABLE_HASH_STATE_VERSION,
        "origin": origin,
        "state": base64.b64encode(state).decode("ascii"),
    }
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"))


def _parse_resumable_hasher_envelope(hash_alg, serialized_state):
    try:
        envelope = json.loads(serialized_state)
        if not isinstance(envelope, dict):
            raise ValueError("state envelope must be an object")
        if envelope.get("format") != RESUMABLE_HASH_STATE_VERSION:
            raise ValueError("unsupported state envelope version")
        if envelope.get("algorithm") != hash_alg:
            raise ValueError("state algorithm does not match upload")
        if envelope.get("architecture") != platform.machine():
            raise ValueError("state architecture does not match this worker")
        if envelope.get("byteorder") != sys.byteorder:
            raise ValueError("state byte order does not match this worker")
        if type(envelope.get("bytes_hashed")) is not int or envelope["bytes_hashed"] < 0:
            raise ValueError("state byte count is invalid")
        if envelope.get("origin") not in RESUMABLE_HASH_ORIGINS:
            raise ValueError("state origin is invalid")
        return envelope
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidDigestException("Invalid persisted %s digest state" % hash_alg) from exc


def resumable_hasher_state_origin(hash_alg, serialized_state):
    try:
        envelope = json.loads(serialized_state)
        if not isinstance(envelope, dict):
            raise ValueError("state envelope must be an object")
        if envelope.get("format") != RESUMABLE_HASH_STATE_VERSION:
            raise ValueError("unsupported state envelope version")
        if envelope.get("algorithm") != hash_alg:
            raise ValueError("state algorithm does not match upload")
        if envelope.get("origin") not in RESUMABLE_HASH_ORIGINS:
            raise ValueError("state origin is invalid")
        return envelope["origin"]
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidDigestException("Invalid persisted %s digest state" % hash_alg) from exc


def restore_resumable_hasher(hash_alg, serialized_state, expected_byte_count=None):
    try:
        envelope = _parse_resumable_hasher_envelope(hash_alg, serialized_state)
        if expected_byte_count is not None and envelope["bytes_hashed"] != expected_byte_count:
            raise ValueError("state byte count does not match upload")

        encoded_state = envelope.get("state")
        if not isinstance(encoded_state, str):
            raise ValueError("state must be base64 text")
        state = base64.b64decode(encoded_state.encode("ascii"), validate=True)

        hasher = create_resumable_hasher(hash_alg)
        hasher.__setstate__(state)
        return hasher
    except InvalidDigestException:
        raise
    except (binascii.Error, ImportError, TypeError, ValueError) as exc:
        raise InvalidDigestException("Invalid persisted %s digest state" % hash_alg) from exc


def digests_equal(lhs_digest_string, rhs_digest_string):
    """
    Parse and compare the two digests, returns True if the digests are equal, False otherwise.
    """
    lhs_digest = (
        lhs_digest_string
        if isinstance(lhs_digest_string, Digest)
        else Digest.parse_digest(lhs_digest_string)
    )
    rhs_digest = (
        rhs_digest_string
        if isinstance(rhs_digest_string, Digest)
        else Digest.parse_digest(rhs_digest_string)
    )
    return lhs_digest == rhs_digest
