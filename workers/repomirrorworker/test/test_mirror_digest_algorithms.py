"""
PROJQUAY-10117: repository mirroring supports SHA-256 images only. A tag that uses another digest
algorithm fails to sync, under today's per-tag failure rules, with a reason saying so.
"""

from unittest.mock import patch

import mock
import pytest

from data.database import RepoMirrorConfig, RepoMirrorStatus
from data.model.test.test_repo_mirroring import create_mirror_repo_robot
from test.fixtures import *
from util.repomirror.skopeomirror import SkopeoResults
from workers.repomirrorworker import (
    MIRROR_UNSUPPORTED_DIGEST_REASON,
    _map_failure_to_reason,
    is_unsupported_digest_algorithm_error,
    perform_mirror,
)
from workers.repomirrorworker.test.mirror_test_utils import disable_existing_mirrors


@pytest.mark.parametrize(
    "message, expected",
    [
        ("unsupported digest algorithm", True),
        ('manifest digest "sha512:abcd": invalid', True),
        ("digest algorithm sha384 is not supported for mirroring", True),
        ("Error reading manifest: unauthorized", False),
        ("sha256:abcd not found", False),
    ],
)
def test_detects_unsupported_digest_algorithm(message, expected):
    assert is_unsupported_digest_algorithm_error(message) == expected
    if expected:
        assert _map_failure_to_reason(message) == MIRROR_UNSUPPORTED_DIGEST_REASON


def test_other_failure_reasons_unchanged():
    assert _map_failure_to_reason("401 unauthorized") == "auth_failed"
    assert _map_failure_to_reason("manifest unknown: 404") == "not_found"


@disable_existing_mirrors
@mock.patch("util.repomirror.skopeomirror.SkopeoMirror.run_skopeo")
def test_non_sha256_tag_fails_with_reason(run_skopeo_mock, initialized_db, app):
    mirror, repo = create_mirror_repo_robot(["latest"], external_registry_config={"verify_tls": False})

    def skopeo(args, proxy, timeout=300):
        if args[1] == "list-tags":
            return SkopeoResults(True, [], '{"Tags": ["latest"]}', "")
        if args[1] == "copy":
            return SkopeoResults(
                False,
                [],
                "",
                'reading manifest latest: digest "sha512:%s": unsupported digest algorithm'
                % ("a" * 128),
            )
        return SkopeoResults(True, [], "", "")

    run_skopeo_mock.side_effect = skopeo

    logged = []

    def capture(mirror, log_kind, verb, message, tag=None, tags=None, stdout=None, stderr=None):
        logged.append((log_kind, message, tag))

    with patch("workers.repomirrorworker.emit_log", side_effect=capture), patch(
        "workers.repomirrorworker.repo_mirror_sync_failures_total"
    ) as failures:
        from util.repomirror.skopeomirror import SkopeoMirror

        perform_mirror(SkopeoMirror(), RepoMirrorConfig.get_by_id(mirror.id))

    tag_failures = [entry for entry in logged if entry[0] == "repo_mirror_sync_tag_failed"]
    assert len(tag_failures) == 1
    assert tag_failures[0][2] == "latest"
    assert "not supported for mirroring" in tag_failures[0][1]

    assert RepoMirrorConfig.get_by_id(mirror.id).sync_status == RepoMirrorStatus.FAIL
    reasons = [c.kwargs.get("reason") for c in failures.labels.call_args_list]
    assert MIRROR_UNSUPPORTED_DIGEST_REASON in reasons
