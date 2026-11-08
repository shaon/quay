import json
import logging

from digest import digest_tools
from image.shared import ManifestException
from image.shared.interfaces import ContentRetriever
from util.bytes import Bytes

logger = logging.getLogger(__name__)


class ContentRetrieverForTesting(ContentRetriever):
    def __init__(self, digests=None):
        self.digests = digests or {}

    def add_digest(self, digest, content):
        self.digests[digest] = content

    def get_manifest_bytes_with_digest(self, digest):
        return self.digests.get(digest)

    def get_blob_bytes_with_digest(self, digest):
        return self.digests.get(digest)

    @classmethod
    def for_config(cls, config_obj, digest, size, ensure_ascii=True):
        config_str = json.dumps(config_obj, ensure_ascii=ensure_ascii)
        padded_string = config_str + " " * (size - len(config_str))
        digests = {}
        digests[digest] = padded_string
        return ContentRetrieverForTesting(digests)


class _CustomEncoder(json.JSONEncoder):
    def encode(self, o):
        encoded = super(_CustomEncoder, self).encode(o)
        if isinstance(o, str):
            encoded = encoded.replace("<", "\\u003c")
            encoded = encoded.replace(">", "\\u003e")
            encoded = encoded.replace("&", "\\u0026")
        return encoded


def to_canonical_json(value, ensure_ascii=True, indent=None):
    """
    Returns the canonical JSON string form of the given value, as per the guidelines in
    https://github.com/docker/distribution/blob/master/docs/spec/json.md.

    `indent` is allowed only for the purposes of indenting for debugging.
    """
    return json.dumps(
        value,
        ensure_ascii=ensure_ascii,
        sort_keys=True,
        separators=(",", ":"),
        cls=_CustomEncoder,
        indent=indent,
    )


def is_sha256_only(parsed_manifest):
    """
    Returns True if a parsed manifest's own digest, every blob digest, and (for lists) every
    child manifest digest is SHA-256. Only such manifests may be converted to Docker schema 1 or
    served through the v1 protocol (PROJQUAY-10117).
    """
    digests = [parsed_manifest.digest]
    digests.extend(parsed_manifest.blob_digests or [])
    if parsed_manifest.is_manifest_list:
        for child in parsed_manifest.manifest_dict.get("manifests", []):
            if isinstance(child, dict) and "digest" in child:
                digests.append(child["digest"])
    return all(str(d).startswith("sha256:") for d in digests)


def manifest_digest_algorithm(digest):
    """
    Returns the algorithm to compute a manifest's digest with, given the digest it is addressed or
    stored by: sha384/sha512 when the digest names one, otherwise sha256 (PROJQUAY-10117).
    """
    algorithm = digest_tools.digest_algorithm(str(digest)) if digest else None
    if algorithm in (digest_tools.SHA384, digest_tools.SHA512):
        return algorithm
    return digest_tools.SHA256


class LazyManifestLoader(object):
    """Lazy loader for manifests referenced by another manifest list or index."""

    # Platform key names used in manifest data
    PLATFORM_KEY = "platform"
    ARCHITECTURE_KEY = "architecture"

    def __init__(
        self,
        manifest_data,
        content_retriever,
        supported_types,
        digest_key,
        size_key,
        media_type_key,
        app_config=None,
    ):
        self._manifest_data = manifest_data
        self._content_retriever = content_retriever
        self._loaded_manifest = None
        self._load_attempted = False
        self._digest_key = digest_key
        self._size_key = size_key
        self._media_type_key = media_type_key
        self._supported_types = supported_types
        self._app_config = app_config or {}

    @property
    def architecture(self):
        """Returns the architecture of this manifest from its platform data, or None if not set."""
        platform = self._manifest_data.get(self.PLATFORM_KEY)
        if platform:
            return platform.get(self.ARCHITECTURE_KEY)
        return None

    def _is_sparse_index_enabled(self):
        """Check if sparse index feature is enabled."""
        return self._app_config.get("FEATURE_SPARSE_INDEX", False)

    def _is_architecture_required(self):
        """Returns False when sparse index is enabled (all archs optional), True otherwise."""
        return not self._is_sparse_index_enabled()

    @property
    def manifest_obj(self):
        if self._load_attempted:
            return self._loaded_manifest

        self._loaded_manifest = self._load_manifest()
        self._load_attempted = True
        return self._loaded_manifest

    def _load_manifest(self):
        digest = self._manifest_data[self._digest_key]
        size = self._manifest_data[self._size_key]
        manifest_bytes = self._content_retriever.get_manifest_bytes_with_digest(digest)
        if manifest_bytes is None:
            if not self._is_architecture_required():
                logger.debug(
                    "Skipping manifest with digest `%s` for optional architecture `%s`",
                    digest,
                    self.architecture,
                )
                return None
            raise ManifestException("Could not find child manifest with digest `%s`" % digest)

        if len(manifest_bytes) != size:
            raise ManifestException(
                "Size of manifest does not match that retrieved: %s vs %s"
                % (len(manifest_bytes), size)
            )

        content_type = self._manifest_data[self._media_type_key]
        if content_type not in self._supported_types:
            raise ManifestException(
                "Unknown or unsupported manifest media type `%s` not found in %s"
                % (content_type, self._supported_types.keys())
            )

        return self._supported_types[content_type](
            Bytes.for_string_or_unicode(manifest_bytes),
            validate=False,
            digest_algorithm=manifest_digest_algorithm(digest),
        )
