import base64
import hashlib
import pickle

import pytest
import resumablehash
import resumablesha256

from data.fields import (
    REQUESTED_DIGEST_PICKLE_PROTOCOL,
    ResumableRequestedDigestField,
    ResumableSHA256Field,
)


class _Evil:
    def __reduce__(self):
        return (print, ("pwned",))


def _encode(obj, protocol=REQUESTED_DIGEST_PICKLE_PROTOCOL):
    return base64.b64encode(pickle.dumps(obj, protocol=protocol)).decode("ascii")


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_roundtrip(algorithm):
    field = ResumableRequestedDigestField()
    hasher = resumablehash.new(algorithm)
    hasher.update(b"hello ")

    restored = field.python_value(field.db_value(hasher))
    assert restored.name == algorithm
    restored.update(b"world")
    assert restored.hexdigest() == hashlib.new(algorithm, b"hello world").hexdigest()


def test_db_value_uses_protocol_2_or_higher():
    assert REQUESTED_DIGEST_PICKLE_PROTOCOL >= 2
    field = ResumableRequestedDigestField()
    raw = base64.b64decode(field.db_value(resumablehash.sha512()))
    assert raw[0] == 0x80 and raw[1] >= 2


def test_none():
    field = ResumableRequestedDigestField()
    assert field.db_value(None) is None
    assert field.python_value(None) is None


@pytest.mark.parametrize(
    "payload",
    [
        _encode(resumablesha256.sha256()),  # wrong class (the sha_state type)
        _encode(_Evil()),  # arbitrary callable
        _encode({"a": 1}),  # plain data is still not a hasher
        "not base64!!",
        base64.b64encode(b"\x80\x04garbage").decode("ascii"),
    ],
)
def test_rejected_payloads_return_none(payload):
    field = ResumableRequestedDigestField()
    assert field.python_value(payload) is None


def test_corrupt_state_returns_none():
    field = ResumableRequestedDigestField()
    hasher = resumablehash.sha512()
    hasher.update(b"abc")
    state = hasher.__getstate__()
    if isinstance(state, (bytes, bytearray)):
        corrupt = bytearray(state)
        corrupt[0] ^= 0xFF
        payload = base64.b64encode(
            pickle.dumps(_StateCarrier(type(hasher), bytes(corrupt)), protocol=4)
        ).decode("ascii")
        assert field.python_value(payload) is None
    else:
        pytest.skip("resumablehash state is not a byte string")


class _StateCarrier:
    """Pickles as `cls` with the given state, to simulate a corrupt stored state."""

    def __init__(self, cls, state):
        self._cls = cls
        self._state = state

    def __reduce__(self):
        return (self._cls, (), self._state)


def test_sha_state_allowlist_not_widened():
    field = ResumableSHA256Field()
    with pytest.raises(pickle.UnpicklingError):
        field.python_value(_encode(resumablehash.sha512()))
