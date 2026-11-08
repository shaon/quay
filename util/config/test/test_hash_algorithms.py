import pytest
from jsonschema import Draft7Validator

from config import DefaultConfig, frontend_visible_config
from util.config.hash_algorithms import (
    InvalidHashAlgorithmConfig,
    allowed_hash_algorithms,
    default_manifest_hash_algorithm,
    validate_hash_algorithm_config,
)
from util.config.schema import CONFIG_SCHEMA


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"ALLOWED_HASH_ALGORITHMS": ["sha256"]},
        {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha512"]},
        {"ALLOWED_HASH_ALGORITHMS": ["sha512", "sha384", "sha256"]},
        {
            "ALLOWED_HASH_ALGORITHMS": ["sha256", "sha512"],
            "DEFAULT_MANIFEST_HASH_ALGORITHM": "sha512",
        },
    ],
)
def test_valid_config(config):
    validate_hash_algorithm_config(config)


@pytest.mark.parametrize(
    "config, message",
    [
        ({"ALLOWED_HASH_ALGORITHMS": []}, "non-empty"),
        ({"ALLOWED_HASH_ALGORITHMS": "sha256"}, "non-empty list"),
        ({"ALLOWED_HASH_ALGORITHMS": ["sha512"]}, "must include sha256"),
        ({"ALLOWED_HASH_ALGORITHMS": ["sha256", "md5"]}, "unsupported"),
        ({"ALLOWED_HASH_ALGORITHMS": ["sha256", "SHA512"]}, "unsupported"),
        ({"DEFAULT_MANIFEST_HASH_ALGORITHM": "sha512"}, "not listed"),
        (
            {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha384"], "DEFAULT_MANIFEST_HASH_ALGORITHM": "sha512"},
            "not listed",
        ),
        ({"DEFAULT_MANIFEST_HASH_ALGORITHM": "sha1"}, "not a supported"),
    ],
)
def test_invalid_config(config, message):
    with pytest.raises(InvalidHashAlgorithmConfig, match=message):
        validate_hash_algorithm_config(config)


def test_defaults():
    assert DefaultConfig.ALLOWED_HASH_ALGORITHMS == ["sha256"]
    assert DefaultConfig.DEFAULT_MANIFEST_HASH_ALGORITHM == "sha256"
    assert allowed_hash_algorithms({}) == frozenset(["sha256"])
    assert default_manifest_hash_algorithm({}) == "sha256"


@pytest.mark.parametrize(
    "value, valid",
    [
        (["sha256"], True),
        (["sha256", "sha384", "sha512"], True),
        (["sha512"], False),
        (["sha256", "md5"], False),
        ([], False),
        (["sha256", "sha256"], False),
    ],
)
def test_schema_allowed_hash_algorithms(value, valid):
    schema = CONFIG_SCHEMA["properties"]["ALLOWED_HASH_ALGORITHMS"]
    assert Draft7Validator(schema).is_valid(value) == valid


@pytest.mark.parametrize("value, valid", [("sha256", True), ("sha512", True), ("md5", False)])
def test_schema_default_manifest_hash_algorithm(value, valid):
    schema = CONFIG_SCHEMA["properties"]["DEFAULT_MANIFEST_HASH_ALGORITHM"]
    assert Draft7Validator(schema).is_valid(value) == valid


def test_allowed_hash_algorithms_frontend_visible():
    config = {"ALLOWED_HASH_ALGORITHMS": ["sha256", "sha512"], "DEFAULT_MANIFEST_HASH_ALGORITHM": "x"}
    visible = frontend_visible_config(config)
    assert visible["ALLOWED_HASH_ALGORITHMS"] == ["sha256", "sha512"]
    assert "DEFAULT_MANIFEST_HASH_ALGORITHM" not in visible


def test_none_means_default():
    config = {"ALLOWED_HASH_ALGORITHMS": None, "DEFAULT_MANIFEST_HASH_ALGORITHM": None}
    validate_hash_algorithm_config(config)
    assert allowed_hash_algorithms(config) == frozenset(["sha256"])
    assert default_manifest_hash_algorithm(config) == "sha256"
