import contextlib
import logging
import os
import subprocess

from test.fixtures import *
from test.registry.fixtures import *
from test.registry.liveserverfixture import *
from test.registry.protocol_fixtures import *

CONFORMANCE_TEST_PATH = "dist-spec/conformance/conformance.test"

# Optional comma-separated ALLOWED_HASH_ALGORITHMS for the run, for example
# CONFORMANCE_ALLOWED_HASH_ALGORITHMS=sha256,sha384,sha512 (PROJQUAY-10117). Unset keeps the
# default [sha256].
ALLOWED_HASH_ALGORITHMS_ENV = "CONFORMANCE_ALLOWED_HASH_ALGORITHMS"

logger = logging.getLogger(__name__)


def test_run_conformance(liveserver_session, app_reloader, registry_server_executor, liveserver):
    server_url = liveserver_session.base_url
    env = os.environ.copy()
    env["OCI_ROOT_URL"] = server_url
    env["OCI_NAMESPACE"] = "devtable/oci"
    env["OCI_USERNAME"] = "devtable"
    env["OCI_PASSWORD"] = "password"
    env["OCI_DEBUG"] = "true"

    env["OCI_TEST_PULL"] = "1"
    env["OCI_TEST_PUSH"] = "1"
    env["OCI_TEST_CONTENT_DISCOVERY"] = "1"
    env["OCI_TEST_CONTENT_MANAGEMENT"] = "1"

    assert server_url.startswith("http://")

    server_hostname = server_url[len("http://") :]
    assert server_hostname

    logger.info("Running server at %s", server_hostname)

    with ConfigChange(
        "SERVER_HOSTNAME", server_hostname, registry_server_executor.on(liveserver), liveserver
    ):
        with FeatureFlagValue(
            "GENERAL_OCI_SUPPORT",
            True,
            registry_server_executor.on(liveserver),
        ), _allowed_hash_algorithms(registry_server_executor, liveserver):
            assert subprocess.call(CONFORMANCE_TEST_PATH, env=env) == 0


def _allowed_hash_algorithms(registry_server_executor, liveserver):
    value = os.environ.get(ALLOWED_HASH_ALGORITHMS_ENV)
    if not value:
        return contextlib.nullcontext()

    algorithms = [a.strip() for a in value.split(",") if a.strip()]
    logger.info("Running conformance with ALLOWED_HASH_ALGORITHMS=%s", algorithms)
    return ConfigChange(
        "ALLOWED_HASH_ALGORITHMS", algorithms, registry_server_executor.on(liveserver), liveserver
    )
