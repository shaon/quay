import base64
import io
import json
import logging
import pickle
import string
from random import SystemRandom

import bcrypt
from peewee import CharField, SmallIntegerField, TextField

from data.text import prefix_search
from util.bytes import Bytes

logger = logging.getLogger(__name__)


def random_string(length=16):
    random = SystemRandom()
    return "".join([random.choice(string.ascii_uppercase + string.digits) for _ in range(length)])


class _SafeUnpickler(pickle.Unpickler):
    """
    Restricted unpickler that only allows deserializing resumablesha256.sha256 objects.
    Prevents arbitrary code execution via crafted pickle payloads (CVE-2026-32590).
    """

    def find_class(self, module, name):
        if module == "resumablesha256" and name == "sha256":
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"Forbidden class: {module}.{name}")


class _ResumableSHAField(TextField):
    """
    Base Class used to store the state of an in-progress hash in the database. This is particularly
    useful for working with large byte streams and allows the hashing to be paused and resumed
    as needed.
    """

    def _create_sha(self):
        raise NotImplementedError

    def db_value(self, value):
        """
        Serialize the Hasher's state for storage in the database as plain-text.
        """
        if value is None:
            return None

        serialized_state = base64.b64encode(pickle.dumps(value)).decode("ascii")
        return serialized_state

    def python_value(self, value):
        """
        Restore the Hasher from its state stored in the database.
        """
        if value is None:
            return None

        data = base64.b64decode(value.encode("ascii"))
        hasher = _SafeUnpickler(io.BytesIO(data)).load()
        return hasher


class ResumableSHA256Field(_ResumableSHAField):
    pass


class ResumableSHA1Field(_ResumableSHAField):
    pass


class _RequestedDigestUnpickler(pickle.Unpickler):
    """
    Restricted unpickler for BlobUpload.requested_digest_state (PROJQUAY-10117). Only allows the
    resumable SHA-384 and SHA-512 hash classes from `resumablehash`. Deliberately separate from
    _SafeUnpickler so the `sha_state` allowlist stays exactly `resumablesha256.sha256`
    (CVE-2026-32590).
    """

    ALLOWED = frozenset({("resumablehash", "sha384"), ("resumablehash", "sha512")})

    def find_class(self, module, name):
        if (module, name) in self.ALLOWED:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"Forbidden class: {module}.{name}")


# resumablehash objects cannot be pickled with protocols 0 or 1.
REQUESTED_DIGEST_PICKLE_PROTOCOL = 4


class ResumableRequestedDigestField(TextField):
    """
    Stores the resumable hash state for the hinted (sha384/sha512) digest algorithm of a blob
    upload.

    Restoring never raises: state that cannot be loaded (forbidden class, corrupt or truncated
    bytes, state rejected by resumablehash's own validation, or state saved on a different CPU
    architecture) is returned as None. Callers must also check that the restored object's `name`
    equals BlobUpload.requested_digest_algorithm, and treat any mismatch or None as invalid state,
    falling back to rereading the upload.
    """

    def db_value(self, value):
        if value is None:
            return None
        data = pickle.dumps(value, protocol=REQUESTED_DIGEST_PICKLE_PROTOCOL)
        return base64.b64encode(data).decode("ascii")

    def python_value(self, value):
        if value is None:
            return None
        try:
            data = base64.b64decode(value.encode("ascii"), validate=True)
            hasher = _RequestedDigestUnpickler(io.BytesIO(data)).load()
        except Exception as ex:
            logger.warning("Discarding invalid requested digest hash state: %s", ex)
            return None

        hasher_type = type(hasher)
        if (hasher_type.__module__, hasher_type.__name__) not in _RequestedDigestUnpickler.ALLOWED:
            logger.warning("Discarding requested digest hash state of type %s", hasher_type)
            return None
        return hasher


class JSONField(TextField):
    def db_value(self, value):
        return json.dumps(value)

    def python_value(self, value):
        if value is None or value == "":
            return {}
        if isinstance(value, (dict, list)):
            return value
        return json.loads(value)


class Base64BinaryField(TextField):
    def db_value(self, value):
        if value is None:
            return None
        return base64.b64encode(value).decode("ascii")

    def python_value(self, value):
        if value is None:
            return None
        return base64.b64decode(value.encode("ascii"))


class DecryptedValue(object):
    """
    Wrapper around an already decrypted value to be placed into an encrypted field.
    """

    def __init__(self, decrypted_value):
        assert decrypted_value is not None
        self.value = decrypted_value

    def decrypt(self):
        return self.value

    def matches(self, unencrypted_value):
        """
        Returns whether the value of this field matches the unencrypted_value.
        """
        return self.decrypt() == unencrypted_value


class LazyEncryptedValue(object):
    """
    Wrapper around an encrypted value in an encrypted field.

    Will decrypt lazily.
    """

    def __init__(self, encrypted_value, field):
        self.encrypted_value = encrypted_value
        self._field = field

    def decrypt(self, encrypter=None):
        """
        Decrypts the value.
        """
        encrypter = encrypter or self._field.model._meta.encrypter
        return encrypter.decrypt_value(self.encrypted_value)

    def matches(self, unencrypted_value):
        """
        Returns whether the value of this field matches the unencrypted_value.
        """
        return self.decrypt() == unencrypted_value

    def __eq__(self, _):
        raise Exception("Disallowed operation; use `matches`")

    def __mod__(self, _):
        raise Exception("Disallowed operation; use `matches`")

    def __pow__(self, _):
        raise Exception("Disallowed operation; use `matches`")

    def __contains__(self, _):
        raise Exception("Disallowed operation; use `matches`")

    def contains(self, _):
        raise Exception("Disallowed operation; use `matches`")

    def startswith(self, _):
        raise Exception("Disallowed operation; use `matches`")

    def endswith(self, _):
        raise Exception("Disallowed operation; use `matches`")


def _add_encryption(field_class, requires_length_check=True):
    """
    Adds support for encryption and decryption to the given field class.
    """

    class indexed_class(field_class):
        def __init__(self, default_token_length=None, *args, **kwargs):
            def _generate_default():
                return DecryptedValue(random_string(default_token_length))

            if default_token_length is not None:
                kwargs["default"] = _generate_default

            field_class.__init__(self, *args, **kwargs)
            assert not self.index

        def db_value(self, value):
            if value is None:
                return None

            if isinstance(value, LazyEncryptedValue):
                return value.encrypted_value

            if isinstance(value, DecryptedValue):
                value = value.value

            meta = self.model._meta
            return meta.encrypter.encrypt_value(
                value, self.max_length if requires_length_check else None
            )

        def python_value(self, value):
            if value is None:
                return None

            return LazyEncryptedValue(value, self)

        def __hash__(self):
            return field_class.__hash__(self)

        def __eq__(self, _):
            raise Exception("Disallowed operation; use `matches`")

        def __mod__(self, _):
            raise Exception("Disallowed operation; use `matches`")

        def __pow__(self, _):
            raise Exception("Disallowed operation; use `matches`")

        def __contains__(self, _):
            raise Exception("Disallowed operation; use `matches`")

        def contains(self, _):
            raise Exception("Disallowed operation; use `matches`")

        def startswith(self, _):
            raise Exception("Disallowed operation; use `matches`")

        def endswith(self, _):
            raise Exception("Disallowed operation; use `matches`")

    return indexed_class


EncryptedCharField = _add_encryption(CharField)
EncryptedTextField = _add_encryption(TextField, requires_length_check=False)


class EnumField(SmallIntegerField):
    def __init__(self, enum_type, *args, **kwargs):
        kwargs.pop("index", None)

        super(EnumField, self).__init__(index=True, *args, **kwargs)
        self.enum_type = enum_type

    def db_value(self, value):
        """
        Convert the python value for storage in the database.
        """
        return int(value.value)

    def python_value(self, value):
        """
        Convert the database value to a pythonic value.
        """
        return self.enum_type(value) if value is not None else None

    def clone_base(self, **kwargs):
        return super(EnumField, self).clone_base(enum_type=self.enum_type, **kwargs)


def _add_fulltext(field_class):
    """
    Adds support for full text indexing and lookup to the given field class.
    """

    class indexed_class(field_class):
        # Marker used by SQLAlchemy translation layer to add the proper index for full text searching.
        __fulltext__ = True

        def __init__(self, match_function, *args, **kwargs):
            field_class.__init__(self, *args, **kwargs)
            self.match_function = match_function

        def match(self, query):
            return self.match_function(self, query)

        def match_prefix(self, query):
            return prefix_search(self, query)

        def __mod__(self, _):
            raise Exception("Unsafe operation: Use `match` or `match_prefix`")

        def __pow__(self, _):
            raise Exception("Unsafe operation: Use `match` or `match_prefix`")

        def __contains__(self, _):
            raise Exception("Unsafe operation: Use `match` or `match_prefix`")

        def contains(self, _):
            raise Exception("Unsafe operation: Use `match` or `match_prefix`")

        def startswith(self, _):
            raise Exception("Unsafe operation: Use `match` or `match_prefix`")

        def endswith(self, _):
            raise Exception("Unsafe operation: Use `match` or `match_prefix`")

    return indexed_class


FullIndexedCharField = _add_fulltext(CharField)
FullIndexedTextField = _add_fulltext(TextField)


class Credential(object):
    """
    Credential represents a hashed credential.
    """

    def __init__(self, hashed):
        self.hashed = hashed

    def matches(self, value):
        """
        Returns true if this credential matches the unhashed value given.
        """
        return bcrypt.hashpw(value.encode("utf-8"), self.hashed) == self.hashed

    @classmethod
    def from_string(cls, string_value):
        """
        Returns a Credential object from an unhashed string value.
        """
        return Credential(bcrypt.hashpw(string_value.encode("utf-8"), bcrypt.gensalt()))

    @classmethod
    def generate(cls, length=20):
        """
        Generates a new credential and returns it, along with its unhashed form.
        """
        token = random_string(length)
        return Credential.from_string(token), token


class CredentialField(CharField):
    """
    A character field that stores crytographically hashed credentials that should never be available
    to the user in plaintext after initial creation.

    This field automatically provides verification.
    """

    def __init__(self, *args, **kwargs):
        CharField.__init__(self, *args, **kwargs)
        assert "default" not in kwargs
        assert not self.index

    def db_value(self, value):
        if value is None:
            return None

        if isinstance(value, str):
            raise Exception(
                "A string cannot be given to a CredentialField; please wrap in a Credential"
            )

        return Bytes.for_string_or_unicode(value.hashed).as_unicode()

    def python_value(self, value):
        if value is None:
            return None

        return Credential(Bytes.for_string_or_unicode(value).as_encoded_str())
