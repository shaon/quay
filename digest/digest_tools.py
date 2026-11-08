import hashlib
import os.path
import re

DIGEST_PATTERN = r"([A-Za-z0-9_+.-]+):([A-Fa-f0-9]+)"

# Client-visible digest algorithms Quay can verify, and the exact lowercase hex length of each.
# SHA-256 is always the internal (CAS) storage algorithm.
SHA256 = "sha256"
SHA384 = "sha384"
SHA512 = "sha512"
SUPPORTED_DIGEST_ALGORITHMS = (SHA256, SHA384, SHA512)
DIGEST_HEX_LENGTHS = {SHA256: 64, SHA384: 96, SHA512: 128}
_STRICT_DIGEST_REGEX = re.compile(r"(sha256|sha384|sha512):([a-f0-9]+)")

REPLACE_WITH_PATH = re.compile(r"[+.]")
REPLACE_DOUBLE_SLASHES = re.compile(r"/+")


class InvalidDigestException(RuntimeError):
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
    def parse_digest(digest):
        """
        Returns the digest parsed out to its components.
        """
        match = Digest.DIGEST_REGEX.match(digest)
        if match is None or match.end() != len(digest):
            raise InvalidDigestException("Not a valid digest: %s", digest)

        return Digest(match.group(1), match.group(2))

    @property
    def hash_alg(self):
        return self._hash_alg

    @property
    def hash_bytes(self):
        return self._hash_bytes


def parse_strict_digest(digest):
    """
    Parses a client-visible digest in strict, normalized form.

    The algorithm must be one of SUPPORTED_DIGEST_ALGORITHMS and the hex part must be lowercase and
    exactly as long as that algorithm's output. Raises InvalidDigestException otherwise.
    """
    if not isinstance(digest, str):
        raise InvalidDigestException("Not a valid digest: %s" % (digest,))

    match = _STRICT_DIGEST_REGEX.fullmatch(digest)
    if match is None:
        raise InvalidDigestException("Not a valid digest: %s" % digest)

    algorithm, hex_part = match.group(1), match.group(2)
    if len(hex_part) != DIGEST_HEX_LENGTHS[algorithm]:
        raise InvalidDigestException("Not a valid digest: %s" % digest)

    return Digest(algorithm, hex_part)


def normalize_digest(digest):
    """
    Returns the normalized string form of a strict digest, or raises InvalidDigestException.
    """
    return str(parse_strict_digest(digest))


def is_strict_digest(digest):
    """
    Returns True if the digest is in strict, normalized form.
    """
    try:
        parse_strict_digest(digest)
        return True
    except InvalidDigestException:
        return False


def digest_algorithm(digest):
    """
    Returns the algorithm prefix of a digest string (everything before the first colon), or None
    if there is no colon. Does not validate the digest.
    """
    if not isinstance(digest, str) or ":" not in digest:
        return None
    return digest.split(":", 1)[0]


def new_hasher(algorithm):
    """
    Returns a new hashlib object for a supported client-visible digest algorithm.
    """
    if algorithm not in SUPPORTED_DIGEST_ALGORITHMS:
        raise InvalidDigestException("Unsupported digest algorithm: %s" % algorithm)
    return hashlib.new(algorithm)


def digest_from_hasher(hash_obj):
    """
    Returns the digest string for a hashlib-compatible object, using its `name` as the algorithm.
    """
    return "{0}:{1}".format(hash_obj.name, hash_obj.hexdigest())


def compute_digest(content, algorithm=SHA256):
    """
    Returns the digest of the content bytes under the given algorithm.
    """
    assert isinstance(content, bytes)
    hasher = new_hasher(algorithm)
    hasher.update(content)
    return digest_from_hasher(hasher)


def compute_digest_from_generator(content_generator, algorithm=SHA256):
    """
    Reads all of the data from the iterator and returns its digest under the given algorithm.
    """
    hasher = new_hasher(algorithm)
    for chunk in content_generator:
        hasher.update(chunk)
    return digest_from_hasher(hasher)


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


def sha256_digest(content):
    """
    Returns a sha256 hash of the content bytes in digest form.
    """
    assert isinstance(content, bytes)

    def single_chunk_generator():
        yield content

    return sha256_digest_from_generator(single_chunk_generator())


def sha256_digest_from_generator(content_generator):
    """
    Reads all of the data from the iterator and creates a sha256 digest from the content.
    """
    digest = hashlib.sha256()
    for chunk in content_generator:
        digest.update(chunk)
    return "sha256:{0}".format(digest.hexdigest())


def sha256_digest_from_hashlib(sha256_hash_obj):
    return "sha256:{0}".format(sha256_hash_obj.hexdigest())


def digests_equal(lhs_digest_string, rhs_digest_string):
    """
    Parse and compare the two digests, returns True if the digests are equal, False otherwise.
    """
    return Digest.parse_digest(lhs_digest_string) == Digest.parse_digest(rhs_digest_string)
