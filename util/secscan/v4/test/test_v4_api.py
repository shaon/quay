from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import app  # noqa: F401
from data.registry_model.datatypes import Manifest, ManifestLayer
from image.docker.schema2 import DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
from util.secscan.v4.api import (
    Action,
    APIRequestFailure,
    ClairSecurityScannerAPI,
    Non200ResponseException,
    actions,
    is_valid_response,
)

good_index_report = {
    "manifest_hash": "sha256:b05ac1eeec8635442fa5d3e55d6ef4ad287b9c66055a552c2fd309c334563b0a",
    "state": "IndexError",
    "packages": {},
    "distributions": {},
    "repository": {},
    "environments": {},
    "success": False,
    "err": "failed to scan all layer contents: scanner: dpkg error: opening layer failed: claircore: Layer not fetched",
}
bad_index_report = {
    key: good_index_report[key] for key in good_index_report if key not in ["state"]
}
good_vuln_report = {
    "manifest_hash": "sha256:b05ac1eeec8635442fa5d3e55d6ef4ad287b9c66055a552c2fd309c334563b0a",
    "packages": {},
    "distributions": {},
    "environments": {},
    "vulnerabilities": {},
    "package_vulnerabilities": {},
}
bad_vuln_report = {
    key: good_vuln_report[key] for key in good_vuln_report if key not in ["vulnerabilities"]
}


@pytest.mark.parametrize(
    "action, resp, expected, exception",
    [
        (None, None, False, AttributeError),
        (actions["IndexState"](), {}, False, None),
        (actions["IndexState"](), {"state": "abc"}, True, None),
        (actions["Index"](None), {}, False, None),
        (actions["Index"](None), good_index_report, True, None),
        (actions["GetIndexReport"](good_index_report["manifest_hash"]), {}, False, None),
        (
            actions["GetIndexReport"](good_index_report["manifest_hash"]),
            bad_index_report,
            False,
            None,
        ),
        (
            actions["GetIndexReport"](good_index_report["manifest_hash"]),
            good_index_report,
            True,
            None,
        ),
        (actions["GetVulnerabilityReport"](good_vuln_report["manifest_hash"]), {}, False, None),
        (
            actions["GetVulnerabilityReport"](good_vuln_report["manifest_hash"]),
            bad_vuln_report,
            False,
            None,
        ),
        (
            actions["GetVulnerabilityReport"](good_vuln_report["manifest_hash"]),
            good_vuln_report,
            True,
            None,
        ),
        (
            actions["GetNotification"]("5e4b387e-88d3-4364-86fd-063447a6fad2", None),
            None,
            False,
            None,
        ),
        (
            actions["GetNotification"]("5e4b387e-88d3-4364-86fd-063447a6fad2", None),
            {"page": {}, "notifications": []},
            True,
            None,
        ),
        (
            actions["DeleteNotification"]("5e4b387e-88d3-4364-86fd-063447a6fad2"),
            None,
            True,
            None,
        ),
        (
            actions["DeleteIndexReport"]("sha256:abc123"),
            None,
            True,
            None,
        ),
    ],
)
def test_is_valid_response(action, resp, expected, exception):
    class TestResponse(object):
        def json(self):
            return resp

    try:
        result = is_valid_response(action, TestResponse())
        assert result == expected
    except Exception as e:
        assert exception is not None and isinstance(e, exception)


def test_index_uses_canonical_storage_digests_for_locally_stored_content():
    canonical_manifest_digest = "sha256:" + "a" * 64
    registered_layer_digest = "sha512:" + "b" * 128
    canonical_layer_digest = "sha256:" + "c" * 64
    remote_layer_digest = "sha384:" + "d" * 96
    repository = Mock()
    manifest = Manifest(
        db_id=1,
        digest=canonical_manifest_digest,
        internal_manifest_bytes=None,
        media_type=DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
        config_media_type=None,
        _layers_compressed_size=123,
        inputs={"repository": repository},
    )
    canonical_blob = Mock(digest=canonical_layer_digest)
    layer = ManifestLayer(
        SimpleNamespace(
            blob_digest=registered_layer_digest,
            compressed_size=123,
            is_remote=False,
            urls=[],
        ),
        canonical_blob,
    )
    remote_layer = ManifestLayer(
        SimpleNamespace(
            blob_digest=remote_layer_digest,
            compressed_size=456,
            is_remote=True,
            urls=["https://layers.test/remote"],
        ),
        None,
    )

    api = object.__new__(ClairSecurityScannerAPI)
    api.max_layer_size = 0
    api._blob_url_retriever = Mock()
    api._blob_url_retriever.url_for_download.return_value = "https://quay.test/blob"
    api._blob_url_retriever.headers_for_download.return_value = {}
    response = Mock(headers={"etag": '"indexer-state"'})
    response.json.return_value = {"state": "IndexFinished"}
    api._perform = Mock(return_value=response)

    api.index(manifest, [layer, remote_layer])

    action = api._perform.call_args.args[0]
    request_body = action.payload[2]
    assert request_body["hash"] == canonical_manifest_digest
    assert request_body["layers"][0]["hash"] == canonical_layer_digest
    assert request_body["layers"][0]["uri"] == "https://quay.test/blob"
    assert request_body["layers"][1]["hash"] == remote_layer_digest
    api._blob_url_retriever.url_for_download.assert_called_once_with(
        repository,
        canonical_blob,
        repository_digest=registered_layer_digest,
    )


def test_delete_index_report_is_idempotent_when_report_is_missing():
    api = object.__new__(ClairSecurityScannerAPI)
    response = Mock(status_code=404)
    api._perform = Mock(side_effect=Non200ResponseException(response))

    assert api.delete("sha512:" + "a" * 128) is None


def test_delete_index_report_preserves_other_api_failures():
    api = object.__new__(ClairSecurityScannerAPI)
    response = Mock(status_code=503)
    api._perform = Mock(side_effect=Non200ResponseException(response))

    with pytest.raises(APIRequestFailure):
        api.delete("sha512:" + "a" * 128)
