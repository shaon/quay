# pylint: disable=W0401, W0621, W0613, W0614, R0913
import binascii
import hashlib
import tarfile
import time
from io import BytesIO

from werkzeug.datastructures import Accept

from app import app as original_app
from app import instance_keys
from data.registry_model import registry_model
from image.docker.schema1 import DOCKER_SCHEMA1_MANIFEST_CONTENT_TYPE
from image.docker.schema2 import DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE
from image.docker.schema2.list import DockerSchema2ManifestListBuilder
from image.docker.schema2.manifest import DockerSchema2ManifestBuilder
from image.oci.index import OCIIndexBuilder
from test.fixtures import *
from test.registry.fixtures import *
from test.registry.liveserverfixture import *
from test.registry.protocol_fixtures import *
from test.registry.protocols import (
    Failures,
    Image,
    ProtocolOptions,
    layer_bytes_for_contents,
)
from util.security.registry_jwt import decode_bearer_header
from util.timedeltastring import convert_to_timedelta


def test_basic_push_pull(pusher, puller, basic_images, liveserver_session, app_reloader):
    """Test: Basic push and pull of an image to a new repository."""
    credentials = ("devtable", "password")

    # Push a new repository.
    pusher.push(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )

    # Pull the repository to verify.
    puller.pull(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )


def test_empty_layer(pusher, puller, images_with_empty_layer, liveserver_session, app_reloader):
    """Test: Push and pull of an image with an empty layer to a new repository."""
    credentials = ("devtable", "password")

    # Push a new repository.
    pusher.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        images_with_empty_layer,
        credentials=credentials,
    )

    # Pull the repository to verify.
    puller.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        images_with_empty_layer,
        credentials=credentials,
    )


def test_empty_layer_push_again(
    pusher, puller, images_with_empty_layer, liveserver_session, app_reloader
):
    """Test: Push and pull of an image with an empty layer to a new repository and then push it
    again."""
    credentials = ("devtable", "password")

    # Push a new repository.
    pusher.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        images_with_empty_layer,
        credentials=credentials,
    )

    # Pull the repository to verify.
    puller.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        images_with_empty_layer,
        credentials=credentials,
    )

    # Push to the repository again, to ensure everything is skipped properly.
    options = ProtocolOptions()
    options.skip_head_checks = True
    pusher.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        images_with_empty_layer,
        credentials=credentials,
        options=options,
    )


def test_multi_layer_images_push_pull(
    pusher, puller, multi_layer_images, liveserver_session, app_reloader
):
    """Test: Basic push and pull of a multi-layered image to a new repository."""
    credentials = ("devtable", "password")

    # Push a new repository.
    pusher.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        multi_layer_images,
        credentials=credentials,
    )

    # Pull the repository to verify.
    puller.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        multi_layer_images,
        credentials=credentials,
    )


def test_overwrite_tag(
    pusher, puller, basic_images, different_images, liveserver_session, app_reloader
):
    """Test: Basic push and pull of an image to a new repository, followed by a push to the same
    tag with different images."""
    credentials = ("devtable", "password")

    # Push a new repository.
    pusher.push(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )

    # Pull the repository to verify.
    puller.pull(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )

    # Push a new repository.
    pusher.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        different_images,
        credentials=credentials,
    )

    # Pull the repository to verify.
    puller.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        different_images,
        credentials=credentials,
    )


def test_basic_push_pull_by_manifest(
    manifest_protocol, basic_images, liveserver_session, app_reloader
):
    """Test: Basic push and pull-by-manifest of an image to a new repository."""
    credentials = ("devtable", "password")

    # Push a new repository.
    result = manifest_protocol.push(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )

    # Pull the repository by digests to verify.
    options = ProtocolOptions()
    options.require_matching_manifest_type = True

    digests = [str(manifest.digest) for manifest in list(result.manifests.values())]
    manifest_protocol.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        digests,
        basic_images,
        credentials=credentials,
        options=options,
    )


def test_basic_push_by_manifest_digest(
    manifest_protocol, basic_images, liveserver_session, app_reloader
):
    """Test: Basic push-by-manifest and pull-by-manifest of an image to a new repository."""
    credentials = ("devtable", "password")

    # Push a new repository.
    options = ProtocolOptions()
    options.push_by_manifest_digest = True

    result = manifest_protocol.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        basic_images,
        credentials=credentials,
        options=options,
    )

    expected_failure = None

    # Pull the repository by digests to verify.
    digests = [str(manifest.digest) for manifest in list(result.manifests.values())]
    manifest_protocol.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        digests,
        basic_images,
        credentials=credentials,
        expected_failure=expected_failure,
    )


def test_manifest_down_conversion(
    manifest_protocol, v21_protocol, basic_images, liveserver_session, app_reloader
):
    """Test: Push using a new protocol and ensure down-conversion."""
    credentials = ("devtable", "password")

    # Push a new repository.
    result = manifest_protocol.push(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )

    # Pull the repository with down conversion.
    options = ProtocolOptions()

    v21_protocol.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        basic_images,
        credentials=credentials,
        options=options,
    )


def test_empty_manifest(v22_protocol, liveserver_session, app_reloader):
    credentials = ("devtable", "password")

    # Push an empty manifest.
    v22_protocol.push(
        liveserver_session,
        "devtable",
        "simple",
        "latest",
        [],
        credentials=credentials,
    )

    # Pull the empty manifest.
    v22_protocol.pull(
        liveserver_session, "devtable", "simple", "latest", [], credentials=credentials
    )


def test_push_invalid_credentials(pusher, basic_images, liveserver_session, app_reloader):
    """Test: Ensure we get auth errors when trying to push with invalid credentials."""
    invalid_credentials = ("devtable", "notcorrectpassword")

    pusher.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        basic_images,
        credentials=invalid_credentials,
        expected_failure=Failures.UNAUTHENTICATED,
    )


def test_pull_invalid_credentials(puller, basic_images, liveserver_session, app_reloader):
    """Test: Ensure we get auth errors when trying to pull with invalid credentials."""
    invalid_credentials = ("devtable", "notcorrectpassword")

    puller.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        basic_images,
        credentials=invalid_credentials,
        expected_failure=Failures.UNAUTHENTICATED,
    )


def test_push_pull_formerly_bad_repo_name(
    pusher, puller, basic_images, liveserver_session, app_reloader
):
    """Test: Basic push and pull of an image to a new repository with a name that formerly
    failed."""
    credentials = ("devtable", "password")

    # Push a new repository.
    pusher.push(
        liveserver_session, "devtable", "foo.bar", "latest", basic_images, credentials=credentials
    )

    # Pull the repository to verify.
    puller.pull(
        liveserver_session, "devtable", "foo.bar", "latest", basic_images, credentials=credentials
    )


def test_middle_layer_different_sha(v2_protocol, v1_protocol, liveserver_session, app_reloader):
    """Test: Pushing of a 3-layer image with the *same* V1 ID's, but the middle layer having
    different bytes, must result in new IDs being generated for the leaf layer, as
    they point to different "images".
    """
    credentials = ("devtable", "password")
    first_images = [
        Image(id="baseimage", parent_id=None, size=None, bytes=layer_bytes_for_contents(b"base")),
        Image(
            id="middleimage",
            parent_id="baseimage",
            size=None,
            bytes=layer_bytes_for_contents(b"middle"),
        ),
        Image(
            id="leafimage",
            parent_id="middleimage",
            size=None,
            bytes=layer_bytes_for_contents(b"leaf"),
        ),
    ]

    # First push and pull the images, to ensure we have the basics setup and working.
    v2_protocol.push(
        liveserver_session, "devtable", "newrepo", "latest", first_images, credentials=credentials
    )
    first_pull_result = v2_protocol.pull(
        liveserver_session, "devtable", "newrepo", "latest", first_images, credentials=credentials
    )
    first_manifest = first_pull_result.manifests["latest"]
    assert set([image.id for image in first_images]) == set(first_manifest.image_ids)
    assert first_pull_result.image_ids["latest"] == "leafimage"

    # Next, create an image list with the middle image's *bytes* changed.
    second_images = list(first_images)
    second_images[1] = Image(
        id="middleimage",
        parent_id="baseimage",
        size=None,
        bytes=layer_bytes_for_contents(b"different middle bytes"),
    )

    # Push and pull the image, ensuring that the produced ID for the middle and leaf layers
    # are synthesized.
    options = ProtocolOptions()
    options.skip_head_checks = True

    v2_protocol.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        second_images,
        credentials=credentials,
        options=options,
    )
    second_pull_result = v1_protocol.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        second_images,
        credentials=credentials,
        options=options,
    )

    assert second_pull_result.image_ids["latest"] != "leafimage"


def add_robot(api_caller, _):
    api_caller.conduct_auth("devtable", "password")
    resp = api_caller.get("/api/v1/organization/buynlarge/robots/ownerbot")
    return ("buynlarge+ownerbot", resp.json()["token"])


def add_token(_, executor):
    return ("$token", executor.add_token().text)


# TODO: Test logging to elastic search log model.
@pytest.mark.parametrize(
    "credentials, namespace, expected_performer, is_public_namespace",
    [
        (("devtable", "password"), "devtable", "devtable", False),
        (("devtable", "password"), "buynlarge", "devtable", False),
        (("devtable", "password"), "sellnsmall", "devtable", True),
        (add_robot, "buynlarge", "buynlarge+ownerbot", False),
        (("$oauthtoken", "%s%s" % ("b" * 40, "c" * 40)), "devtable", "devtable", False),
        (("$app", "%s%s" % ("a" * 60, "b" * 60)), "devtable", "devtable", False),
        (add_token, "devtable", None, False),
    ],
)
@pytest.mark.parametrize(
    "disable_pull_logs",
    [
        False,
        True,
    ],
)
def test_push_pull_logging(
    credentials,
    namespace,
    expected_performer,
    disable_pull_logs,
    is_public_namespace,
    pusher,
    puller,
    basic_images,
    liveserver_session,
    liveserver,
    api_caller,
    app_reloader,
    registry_server_executor,
):
    """Test: Basic push and pull, ensuring that logs are added for each operation."""

    with FeatureFlagValue(
        "DISABLE_PULL_LOGS_FOR_FREE_NAMESPACES",
        disable_pull_logs,
        registry_server_executor.on(liveserver),
    ):
        # Create the repository before the test push.
        start_images = [
            Image(
                id="startimage",
                parent_id=None,
                size=None,
                bytes=layer_bytes_for_contents(b"start image"),
            )
        ]
        pusher.push(
            liveserver_session,
            namespace,
            "newrepo",
            "latest",
            start_images,
            credentials=("devtable", "password"),
        )

        # Retrieve the credentials to use. This must be done after the repo is created, because
        # some credentials creation code adds the new entity to the repository.
        if not isinstance(credentials, tuple):
            credentials = credentials(api_caller, registry_server_executor.on(liveserver))

        # Push to the repository with the specified credentials.
        pusher.push(
            liveserver_session,
            namespace,
            "newrepo",
            "anothertag",
            basic_images,
            credentials=credentials,
        )

        # Check the logs for the push.
        api_caller.conduct_auth("devtable", "password")

        result = api_caller.get("/api/v1/repository/%s/newrepo/logs" % namespace)
        logs = result.json()["logs"]
        assert len(logs) == 2
        assert logs[0]["kind"] == "push_repo"
        assert logs[0]["metadata"]["namespace"] == namespace
        assert logs[0]["metadata"]["repo"] == "newrepo"

        if expected_performer is not None:
            assert logs[0]["performer"]["name"] == expected_performer

        # Pull the repository to verify.
        puller.pull(
            liveserver_session,
            namespace,
            "newrepo",
            "anothertag",
            basic_images,
            credentials=credentials,
        )

        # Check the logs for the pull if applicable.
        result = api_caller.get("/api/v1/repository/%s/newrepo/logs" % namespace)
        logs = result.json()["logs"]

        if not disable_pull_logs or not is_public_namespace:
            assert len(logs) == 3
            assert logs[0]["kind"] == "pull_repo"
            assert logs[0]["metadata"]["namespace"] == namespace
            assert logs[0]["metadata"]["repo"] == "newrepo"

            if expected_performer is not None:
                assert logs[0]["performer"]["name"] == expected_performer
        else:
            assert len(logs) == 2
            assert logs[0]["kind"] == "push_repo"


def test_pull_publicrepo_anonymous(
    pusher, puller, basic_images, liveserver_session, app_reloader, api_caller, liveserver
):
    """Test: Pull a public repository anonymously."""
    # Add a new repository under the public user, so we have a repository to pull.
    pusher.push(
        liveserver_session,
        "public",
        "newrepo",
        "latest",
        basic_images,
        credentials=("public", "password"),
    )

    # First try to pull the (currently private) repo anonymously, which should fail (since it is
    # private)
    puller.pull(
        liveserver_session,
        "public",
        "newrepo",
        "latest",
        basic_images,
        expected_failure=Failures.UNAUTHORIZED,
    )

    # Using a non-public user should also fail.
    puller.pull(
        liveserver_session,
        "public",
        "newrepo",
        "latest",
        basic_images,
        credentials=("reader", "password"),
        expected_failure=Failures.UNAUTHORIZED,
    )

    # Make the repository public.
    api_caller.conduct_auth("public", "password")
    api_caller.change_repo_visibility("public", "newrepo", "public")

    # Pull the repository anonymously, which should succeed because the repository is public.
    puller.pull(liveserver_session, "public", "newrepo", "latest", basic_images)


def test_pull_publicrepo_no_anonymous_access(
    pusher,
    puller,
    basic_images,
    liveserver_session,
    app_reloader,
    api_caller,
    liveserver,
    registry_server_executor,
):
    """Test: Attempts to pull a public repository anonymously, with the feature flag disabled."""
    # Add a new repository under the public user, so we have a repository to pull.
    pusher.push(
        liveserver_session,
        "public",
        "newrepo",
        "latest",
        basic_images,
        credentials=("public", "password"),
    )

    # First try to pull the (currently private) repo anonymously, which should fail (since it is
    # private)
    puller.pull(
        liveserver_session,
        "public",
        "newrepo",
        "latest",
        basic_images,
        expected_failure=Failures.UNAUTHORIZED,
    )

    # Using a non-public user should also fail.
    puller.pull(
        liveserver_session,
        "public",
        "newrepo",
        "latest",
        basic_images,
        credentials=("reader", "password"),
        expected_failure=Failures.UNAUTHORIZED,
    )

    # Make the repository public.
    api_caller.conduct_auth("public", "password")
    api_caller.change_repo_visibility("public", "newrepo", "public")

    with FeatureFlagValue("ANONYMOUS_ACCESS", False, registry_server_executor.on(liveserver)):
        # Attempt again to pull the (now public) repo anonymously, which should fail since
        # the feature flag for anonymous access is turned off.
        options = ProtocolOptions()
        options.attempt_pull_without_token = True

        puller.pull(
            liveserver_session,
            "public",
            "newrepo",
            "latest",
            basic_images,
            expected_failure=Failures.ANONYMOUS_NOT_ALLOWED,
            options=options,
        )

        # Using a non-public user should now succeed.
        puller.pull(
            liveserver_session,
            "public",
            "newrepo",
            "latest",
            basic_images,
            credentials=("devtable", "password"),
        )


def test_basic_organization_flow(pusher, puller, basic_images, liveserver_session, app_reloader):
    """Test: Basic push and pull of an image to a new repository by members of an organization."""
    # Add a new repository under the organization via the creator user.
    pusher.push(
        liveserver_session,
        "buynlarge",
        "newrepo",
        "latest",
        basic_images,
        credentials=("creator", "password"),
    )

    # Ensure that the creator can pull it.
    puller.pull(
        liveserver_session,
        "buynlarge",
        "newrepo",
        "latest",
        basic_images,
        credentials=("creator", "password"),
    )

    # Ensure that the admin can pull it.
    puller.pull(
        liveserver_session,
        "buynlarge",
        "newrepo",
        "latest",
        basic_images,
        credentials=("devtable", "password"),
    )

    # Ensure that the reader *cannot* pull it.
    puller.pull(
        liveserver_session,
        "buynlarge",
        "newrepo",
        "latest",
        basic_images,
        credentials=("reader", "password"),
        expected_failure=Failures.UNAUTHORIZED,
    )


def test_library_support(pusher, puller, basic_images, liveserver_session, app_reloader):
    """Test: Pushing and pulling from the implicit library namespace."""
    credentials = ("devtable", "password")

    # Push a new repository.
    pusher.push(liveserver_session, "", "newrepo", "latest", basic_images, credentials=credentials)

    # Pull the repository to verify.
    puller.pull(liveserver_session, "", "newrepo", "latest", basic_images, credentials=credentials)

    # Pull the repository from the library namespace to verify.
    puller.pull(
        liveserver_session, "library", "newrepo", "latest", basic_images, credentials=credentials
    )


def test_library_namespace_with_support_disabled(
    pusher,
    puller,
    basic_images,
    liveserver_session,
    app_reloader,
    liveserver,
    registry_server_executor,
):
    """Test: Pushing and pulling from the explicit library namespace, even when the
    implicit one is disabled.
    """
    credentials = ("devtable", "password")

    with FeatureFlagValue("LIBRARY_SUPPORT", False, registry_server_executor.on(liveserver)):
        # Push a new repository.
        pusher.push(
            liveserver_session,
            "library",
            "newrepo",
            "latest",
            basic_images,
            credentials=credentials,
        )

        # Pull the repository from the library namespace to verify.
        puller.pull(
            liveserver_session,
            "library",
            "newrepo",
            "latest",
            basic_images,
            credentials=credentials,
        )


def test_push_library_with_support_disabled(
    pusher, basic_images, liveserver_session, app_reloader, liveserver, registry_server_executor
):
    """Test: Pushing to the implicit library namespace, when disabled,
    should fail.
    """
    credentials = ("devtable", "password")

    with FeatureFlagValue("LIBRARY_SUPPORT", False, registry_server_executor.on(liveserver)):
        # Attempt to push a new repository.
        pusher.push(
            liveserver_session,
            "",
            "newrepo",
            "latest",
            basic_images,
            credentials=credentials,
            expected_failure=Failures.DISALLOWED_LIBRARY_NAMESPACE,
        )


def test_pull_library_with_support_disabled(
    puller, basic_images, liveserver_session, app_reloader, liveserver, registry_server_executor
):
    """Test: Pushing to the implicit library namespace, when disabled,
    should fail.
    """
    credentials = ("devtable", "password")

    with FeatureFlagValue("LIBRARY_SUPPORT", False, registry_server_executor.on(liveserver)):
        # Attempt to pull the repository from the library namespace.
        puller.pull(
            liveserver_session,
            "",
            "newrepo",
            "latest",
            basic_images,
            credentials=credentials,
            expected_failure=Failures.DISALLOWED_LIBRARY_NAMESPACE,
        )


def test_image_replication(
    pusher,
    puller,
    basic_images,
    liveserver_session,
    app_reloader,
    liveserver,
    registry_server_executor,
):
    """Test: Ensure that entries are created for replication of the images pushed."""
    credentials = ("devtable", "password")

    with FeatureFlagValue("STORAGE_REPLICATION", True, registry_server_executor.on(liveserver)):
        pusher.push(
            liveserver_session,
            "devtable",
            "newrepo",
            "latest",
            basic_images,
            credentials=credentials,
        )

        result = puller.pull(
            liveserver_session,
            "devtable",
            "newrepo",
            "latest",
            basic_images,
            credentials=credentials,
        )

        # Ensure that entries were created for each layer.
        r = registry_server_executor.on(liveserver).verify_replication_for(
            "devtable", "newrepo", "latest"
        )
        assert r.text == "OK"


def test_image_replication_empty_layers(
    pusher,
    puller,
    images_with_empty_layer,
    liveserver_session,
    app_reloader,
    liveserver,
    registry_server_executor,
):
    """Test: Ensure that entries are created for replication of the images pushed."""
    credentials = ("devtable", "password")

    with FeatureFlagValue("STORAGE_REPLICATION", True, registry_server_executor.on(liveserver)):
        pusher.push(
            liveserver_session,
            "devtable",
            "newrepo",
            "latest",
            images_with_empty_layer,
            credentials=credentials,
        )

        result = puller.pull(
            liveserver_session,
            "devtable",
            "newrepo",
            "latest",
            images_with_empty_layer,
            credentials=credentials,
        )

        # Ensure that entries were created for each layer.
        r = registry_server_executor.on(liveserver).verify_replication_for(
            "devtable", "newrepo", "latest"
        )
        assert r.text == "OK"


@pytest.mark.parametrize(
    "repo_name, expected_failure",
    [
        ("something", None),
        pytest.param("x" * 255, None, id="Valid long name"),
        pytest.param("x" * 256, Failures.INVALID_REPOSITORY, id="Name too long"),
    ],
)
def test_push_reponame(
    repo_name, expected_failure, pusher, puller, basic_images, liveserver_session, app_reloader
):
    """Test: Attempt to add a repository with various names."""
    credentials = ("devtable", "password")

    pusher.push(
        liveserver_session,
        "devtable",
        repo_name,
        "latest",
        basic_images,
        credentials=credentials,
        expected_failure=expected_failure,
    )

    if expected_failure is None:
        puller.pull(
            liveserver_session,
            "devtable",
            repo_name,
            "latest",
            basic_images,
            credentials=credentials,
        )


@pytest.mark.parametrize(
    "repo_name, extended_repo_names, expected_failure",
    [
        ("something", False, None),
        ("something", True, None),
        ("some/slash", False, Failures.SLASH_REPOSITORY),
        ("some/slash", True, Failures.SLASH_REPOSITORY),
        ("some/more/slash", False, Failures.SLASH_REPOSITORY),
        ("some/more/slash", True, Failures.SLASH_REPOSITORY),
        pytest.param("x" * 255, False, None, id="Valid long name"),
        pytest.param("x" * 255, True, None, id="Valid long name"),
        pytest.param("x" * 256, False, Failures.INVALID_REPOSITORY, id="Name too long"),
        pytest.param("x" * 256, True, Failures.INVALID_REPOSITORY, id="Name too long"),
    ],
)
def test_v1_push_extended_reponame(
    repo_name,
    extended_repo_names,
    expected_failure,
    v1_pusher,
    v1_puller,
    basic_images,
    liveserver_session,
    app_reloader,
    liveserver,
    registry_server_executor,
):
    """Test: Attempt to add a repository with various names."""
    credentials = ("devtable", "password")

    with FeatureFlagValue(
        "EXTENDED_REPOSITORY_NAMES",
        extended_repo_names,
        registry_server_executor.on(liveserver),
    ):
        v1_pusher.push(
            liveserver_session,
            "devtable",
            repo_name,
            "latest",
            basic_images,
            credentials=credentials,
            expected_failure=expected_failure,
        )

        if expected_failure is None:
            v1_puller.pull(
                liveserver_session,
                "devtable",
                repo_name,
                "latest",
                basic_images,
                credentials=credentials,
            )


@pytest.mark.parametrize(
    "repo_name, extended_repo_names, expected_failure",
    [
        ("something", False, None),
        ("something", True, None),
        ("some/slash", False, Failures.SLASH_REPOSITORY),
        ("some/slash", True, None),
        ("some/more/slash", False, Failures.SLASH_REPOSITORY),
        ("some/more/slash", True, None),
        ("foo/build/bar", True, Failures.INVALID_REPOSITORY),
        ("build/foo", True, None),
        ("foo/build/bar/extra", True, None),
        pytest.param("x" * 255, False, None, id="Valid long name"),
        pytest.param("x" * 255, True, None, id="Valid long name"),
        pytest.param("x" * 256, False, Failures.INVALID_REPOSITORY, id="Name too long"),
        pytest.param("x" * 256, True, Failures.INVALID_REPOSITORY, id="Name too long"),
    ],
)
def test_v2_push_reponame(
    repo_name,
    extended_repo_names,
    expected_failure,
    v2_pusher,
    v2_puller,
    basic_images,
    liveserver_session,
    app_reloader,
    liveserver,
    registry_server_executor,
):
    """Test: Attempt to add a repository with various names."""
    credentials = ("devtable", "password")

    with FeatureFlagValue(
        "EXTENDED_REPOSITORY_NAMES",
        extended_repo_names,
        registry_server_executor.on(liveserver),
    ):
        v2_pusher.push(
            liveserver_session,
            "devtable",
            repo_name,
            "latest",
            basic_images,
            credentials=credentials,
            expected_failure=expected_failure,
        )

        if expected_failure is None:
            v2_puller.pull(
                liveserver_session,
                "devtable",
                repo_name,
                "latest",
                basic_images,
                credentials=credentials,
            )


@pytest.mark.parametrize(
    "tag_name, expected_failure",
    [
        ("l", None),
        ("1", None),
        ("x" * 128, None),
        ("", Failures.MISSING_TAG),
        ("x" * 129, Failures.INVALID_TAG),
        (".fail", Failures.INVALID_TAG),
        ("-fail", Failures.INVALID_TAG),
    ],
)
def test_tag_validaton(
    tag_name, expected_failure, pusher, basic_images, liveserver_session, app_reloader
):
    """Test: Various forms of tags and whether they succeed or fail as expected."""
    credentials = ("devtable", "password")

    pusher.push(
        liveserver_session,
        "devtable",
        "newrepo",
        tag_name,
        basic_images,
        credentials=credentials,
        expected_failure=expected_failure,
    )


def test_invalid_parent(legacy_pusher, liveserver_session, app_reloader):
    """Test: Attempt to push an image with an invalid/missing parent."""
    images = [
        Image(
            id="childimage",
            parent_id="parentimage",
            size=None,
            bytes=layer_bytes_for_contents(b"child"),
        ),
    ]

    credentials = ("devtable", "password")

    legacy_pusher.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        images,
        credentials=credentials,
        expected_failure=Failures.INVALID_IMAGES,
    )


def test_wrong_image_order(legacy_pusher, liveserver_session, app_reloader):
    """Test: Attempt to push an image with its layers in the wrong order."""
    images = [
        Image(
            id="childimage",
            parent_id="parentimage",
            size=None,
            bytes=layer_bytes_for_contents(b"child"),
        ),
        Image(
            id="parentimage", parent_id=None, size=None, bytes=layer_bytes_for_contents(b"parent")
        ),
    ]

    credentials = ("devtable", "password")

    legacy_pusher.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        images,
        credentials=credentials,
        expected_failure=Failures.INVALID_IMAGES,
    )


@pytest.mark.parametrize(
    "labels",
    [
        # Basic labels.
        [("foo", "bar", "text/plain"), ("baz", "meh", "text/plain")],
        # Theoretically invalid, but allowed when pushed via registry protocol.
        [("theoretically-invalid--label", "foo", "text/plain")],
        # JSON label.
        [("somejson", '{"some": "json"}', "application/json"), ("plain", "", "text/plain")],
        # JSON-esque (but not valid JSON) labels.
        [("foo", "[hello world]", "text/plain"), ("bar", "{wassup?!}", "text/plain")],
        # Empty key label.
        [("", "somevalue", "text/plain")],
    ],
)
def test_labels(labels, manifest_protocol, liveserver_session, api_caller, app_reloader):
    """Test: Image pushed with labels has those labels found in the database after the
    push succeeds.
    """
    images = [
        Image(
            id="theimage",
            parent_id=None,
            bytes=layer_bytes_for_contents(b"image"),
            config={"Labels": {key: value for (key, value, _) in labels}},
        ),
    ]

    credentials = ("devtable", "password")
    result = manifest_protocol.push(
        liveserver_session, "devtable", "newrepo", "latest", images, credentials=credentials
    )

    digest = result.manifests["latest"].digest
    api_caller.conduct_auth("devtable", "password")

    data = api_caller.get("/api/v1/repository/devtable/newrepo/manifest/%s/labels" % digest).json()
    labels_found = data["labels"]
    assert len(labels_found) == len([label for label in labels if label[0]])

    labels_found_map = {l["key"]: l for l in labels_found}
    assert set([k for k in images[0].config["Labels"].keys() if k]) == set(labels_found_map.keys())

    for key, _, media_type in labels:
        if key:
            assert labels_found_map[key]["source_type"] == "manifest"
            assert labels_found_map[key]["media_type"] == media_type


@pytest.mark.parametrize(
    "label_value, expected_expiration",
    [
        ("1d", True),
        ("1h", True),
        ("2w", True),
        ("1g", False),
        ("something", False),
    ],
)
def test_expiration_label(
    label_value,
    expected_expiration,
    manifest_protocol,
    liveserver_session,
    api_caller,
    app_reloader,
):
    """Test: Tag pushed with a valid `quay.expires-after` will have its expiration set to its
    start time plus the duration specified. If the duration is invalid, no expiration will
    be set.
    """
    images = [
        Image(
            id="theimage",
            parent_id=None,
            bytes=layer_bytes_for_contents(b"image"),
            config={"Labels": {"quay.expires-after": label_value}},
        ),
    ]

    credentials = ("devtable", "password")
    manifest_protocol.push(
        liveserver_session, "devtable", "newrepo", "latest", images, credentials=credentials
    )

    api_caller.conduct_auth("devtable", "password")

    tag_data = api_caller.get("/api/v1/repository/devtable/newrepo/tag").json()["tags"][0]
    if expected_expiration:
        diff = convert_to_timedelta(label_value).total_seconds()
        assert tag_data["end_ts"] == tag_data["start_ts"] + diff
    else:
        assert tag_data.get("end_ts") is None


def test_invalid_blob_reference(manifest_protocol, basic_images, liveserver_session, app_reloader):
    """Test: Attempt to push a manifest with an invalid blob reference."""
    credentials = ("devtable", "password")

    options = ProtocolOptions()
    options.manifest_invalid_blob_references = True

    # Attempt to push a new repository.
    manifest_protocol.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        basic_images,
        credentials=credentials,
        options=options,
        expected_failure=Failures.INVALID_BLOB,
    )


def test_delete_tag(
    pusher, puller, basic_images, different_images, liveserver_session, app_reloader
):
    """Test: Push a repository, delete a tag, and attempt to pull."""
    credentials = ("devtable", "password")

    # Push the tags.
    result = pusher.push(
        liveserver_session, "devtable", "newrepo", "one", basic_images, credentials=credentials
    )

    pusher.push(
        liveserver_session, "devtable", "newrepo", "two", different_images, credentials=credentials
    )

    # Delete tag `one` by digest or tag.
    pusher.delete(
        liveserver_session,
        "devtable",
        "newrepo",
        result.manifests["one"].digest if result.manifests else "one",
        credentials=credentials,
    )

    # Attempt to pull tag `one` and ensure it doesn't work.
    puller.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "one",
        basic_images,
        credentials=credentials,
        expected_failure=Failures.UNKNOWN_TAG,
    )

    # Pull tag `two` to verify it works.
    puller.pull(
        liveserver_session, "devtable", "newrepo", "two", different_images, credentials=credentials
    )


def test_delete_manifest(
    manifest_protocol, puller, basic_images, different_images, liveserver_session, app_reloader
):
    """Test: Push a tag, push the tag again, push it back to the original manifest,
    delete the tag, verify cannot pull."""
    credentials = ("devtable", "password")
    options = ProtocolOptions()
    options.skip_head_checks = True

    # Push the latest tag.
    manifest_protocol.push(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )

    # Verify.
    puller.pull(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )

    # Necessary for older data model.
    time.sleep(2)

    # Push the latest tag to a different manifest.
    manifest_protocol.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        different_images,
        credentials=credentials,
        options=options,
    )

    # Verify.
    puller.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        different_images,
        credentials=credentials,
    )

    # Necessary for older data model.
    time.sleep(2)

    # Push the latest tag back to the original images.
    result = manifest_protocol.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        basic_images,
        credentials=credentials,
        options=options,
    )

    # Verify.
    puller.pull(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )

    # Necessary for older data model.
    time.sleep(2)

    # Delete the basic images manifest.
    manifest_protocol.delete(
        liveserver_session,
        "devtable",
        "newrepo",
        result.manifests["latest"].digest,
        credentials=credentials,
    )

    # Attempt to pull tag `latest` and ensure it doesn't work.
    puller.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        basic_images,
        credentials=credentials,
        expected_failure=Failures.UNKNOWN_TAG,
    )


def test_cancel_upload(manifest_protocol, basic_images, liveserver_session, app_reloader):
    """Test: Cancelation of blob uploads."""
    credentials = ("devtable", "password")

    options = ProtocolOptions()
    options.cancel_blob_upload = True

    manifest_protocol.push(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )


@pytest.mark.parametrize(
    "chunks",
    [
        # Two chunks.
        [(0, 100), (100, None)],
        # Multiple chunks.
        [(0, 10), (10, 20), (20, None)],
        [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50), (50, None)],
        # Overlapping chunks.
        [(0, 90), (10, None)],
    ],
)
def test_chunked_blob_uploading(
    chunks, random_layer_data, v21_protocol, puller, liveserver_session, app_reloader
):
    """Test: Uploading of blobs as chunks."""
    credentials = ("devtable", "password")

    adjusted_chunks = []
    for start_byte, end_byte in chunks:
        adjusted_chunks.append((start_byte, end_byte if end_byte else len(random_layer_data)))

    images = [
        Image(id="theimage", parent_id=None, bytes=random_layer_data),
    ]

    options = ProtocolOptions()
    options.chunks_for_upload = adjusted_chunks

    # Push the image, using the specified chunking.
    v21_protocol.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        images,
        credentials=credentials,
        options=options,
    )

    # Pull to verify the image was created.
    puller.pull(
        liveserver_session, "devtable", "newrepo", "latest", images, credentials=credentials
    )


def test_chunked_uploading_mismatched_chunks(
    v21_protocol, random_layer_data, liveserver_session, app_reloader
):
    """Test: Attempt to upload chunks with data missing."""
    credentials = ("devtable", "password")

    images = [
        Image(id="theimage", parent_id=None, bytes=random_layer_data),
    ]

    # Note: Byte #100 is missing.
    options = ProtocolOptions()
    options.chunks_for_upload = [(0, 100), (101, len(random_layer_data), 416)]

    # Attempt to push, with the chunked upload failing.
    v21_protocol.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        images,
        credentials=credentials,
        options=options,
    )


def test_chunked_uploading_missing_first_chunk(
    v2_protocol, random_layer_data, liveserver_session, app_reloader
):
    """Test: Attempt to upload a chunk with missing data.."""
    credentials = ("devtable", "password")

    images = [
        Image(id="theimage", parent_id=None, bytes=random_layer_data),
    ]

    # Note: First 10 bytes are missing.
    options = ProtocolOptions()
    options.chunks_for_upload = [(10, len(random_layer_data), 416)]

    # Attempt to push, with the chunked upload failing.
    v2_protocol.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        images,
        credentials=credentials,
        options=options,
    )


def test_pull_disabled_namespace(
    pusher,
    puller,
    basic_images,
    liveserver_session,
    app_reloader,
    liveserver,
    registry_server_executor,
):
    """Test: Attempt to pull a repository from a disabled namespace results in an error."""
    credentials = ("devtable", "password")

    # Push a new repository.
    pusher.push(
        liveserver_session,
        "buynlarge",
        "someneworgrepo",
        "latest",
        basic_images,
        credentials=credentials,
    )

    # Disable the namespace.
    registry_server_executor.on(liveserver).disable_namespace("buynlarge")

    # Attempt to pull, which should fail.
    puller.pull(
        liveserver_session,
        "buynlarge",
        "someneworgrepo",
        "latest",
        basic_images,
        credentials=credentials,
        expected_failure=Failures.NAMESPACE_DISABLED,
    )


def test_push_disabled_namespace(
    pusher, basic_images, liveserver_session, app_reloader, liveserver, registry_server_executor
):
    """Test: Attempt to push a repository from a disabled namespace results in an error."""
    credentials = ("devtable", "password")

    # Disable the namespace.
    registry_server_executor.on(liveserver).disable_namespace("buynlarge")

    # Attempt to push, which should fail.
    pusher.push(
        liveserver_session,
        "buynlarge",
        "someneworgrepo",
        "latest",
        basic_images,
        credentials=credentials,
        expected_failure=Failures.NAMESPACE_DISABLED,
    )


def test_private_catalog_no_access(
    v2_protocol, liveserver_session, app_reloader, liveserver, registry_server_executor
):
    """Test: Ensure that accessing a private catalog with anonymous access results in no database
    connections.
    """
    with FeatureFlagValue("PUBLIC_CATALOG", False, registry_server_executor.on(liveserver)):
        # Disconnect the server from the database.
        registry_server_executor.on(liveserver).break_database()

        results = v2_protocol.catalog(liveserver_session)
        assert not results


@pytest.mark.parametrize(
    "public_catalog, credentials, expected_repos",
    [
        # No public access and no credentials => No results.
        (False, None, None),
        # Public access and no credentials => public repositories.
        (True, None, ["public/publicrepo"]),
        # Private creds => private repositories.
        (
            False,
            ("devtable", "password"),
            ["devtable/simple", "devtable/complex", "devtable/gargantuan"],
        ),
        (
            True,
            ("devtable", "password"),
            ["devtable/simple", "devtable/complex", "devtable/gargantuan"],
        ),
    ],
)
@pytest.mark.parametrize(
    "page_size",
    [
        1,
        2,
        10,
        50,
        100,
    ],
)
def test_catalog(
    public_catalog,
    credentials,
    expected_repos,
    page_size,
    v2_protocol,
    liveserver_session,
    app_reloader,
    liveserver,
    registry_server_executor,
):
    """Test: Retrieving results from the V2 catalog."""
    with FeatureFlagValue(
        "PUBLIC_CATALOG", public_catalog, registry_server_executor.on(liveserver)
    ):
        results = v2_protocol.catalog(
            liveserver_session,
            page_size=page_size,
            credentials=credentials,
            namespace="devtable",
            repo_name="simple",
        )

    if expected_repos is None:
        assert len(results) == 0
    else:
        assert set(expected_repos).issubset(set(results))


def test_catalog_caching(
    v2_protocol,
    basic_images,
    liveserver_session,
    app_reloader,
    liveserver,
    registry_server_executor,
):
    """Test: Calling the catalog after initially pulled will result in the catalog being cached."""
    credentials = ("devtable", "password")

    # Conduct the initial catalog call to prime the cache.
    results = v2_protocol.catalog(
        liveserver_session, credentials=credentials, namespace="devtable", repo_name="simple"
    )

    token, _ = v2_protocol.auth(liveserver_session, credentials, "devtable", "simple")

    # Disconnect the server from the database.
    registry_server_executor.on(liveserver).break_database()

    # Call the catalog again, which should now be cached.
    cached_results = v2_protocol.catalog(liveserver_session, bearer_token=token)
    assert len(cached_results) == len(results)
    assert set(cached_results) == set(results)


def test_catalog_disabled_namespace(
    v2_protocol,
    basic_images,
    liveserver_session,
    app_reloader,
    liveserver,
    registry_server_executor,
):
    credentials = ("devtable", "password")

    # Get a valid token.
    token, _ = v2_protocol.auth(liveserver_session, credentials, "devtable", "simple")

    # Disable the devtable namespace.
    registry_server_executor.on(liveserver).disable_namespace("devtable")

    # Try to retrieve the catalog and ensure it fails to return any results.
    results = v2_protocol.catalog(liveserver_session, bearer_token=token)
    assert len(results) == 0


@pytest.mark.parametrize(
    "username, namespace, repository",
    [
        ("devtable", "devtable", "simple"),
        ("devtable", "devtable", "gargantuan"),
        ("public", "public", "publicrepo"),
        ("devtable", "buynlarge", "orgrepo"),
    ],
)
@pytest.mark.parametrize(
    "page_size",
    [
        0,
        1,
        2,
        10,
        50,
        100,
    ],
)
def test_tags(
    username,
    namespace,
    repository,
    page_size,
    v2_protocol,
    liveserver_session,
    app_reloader,
    liveserver,
    registry_server_executor,
):
    """Test: Retrieving results from the V2 catalog."""
    credentials = (username, "password")
    results = v2_protocol.tags(
        liveserver_session,
        page_size=page_size,
        credentials=credentials,
        namespace=namespace,
        repo_name=repository,
    )

    repo_ref = registry_model.lookup_repository(namespace, repository)
    expected_tags = [tag.name for tag in registry_model.list_all_active_repository_tags(repo_ref)]
    assert len(results) == len(expected_tags) if page_size > 0 else len(results) == 0
    assert set([r for r in results]) == set(expected_tags) if page_size > 0 else len(results) == 0

    # Invoke the tags endpoint again to ensure caching works.
    results = v2_protocol.tags(
        liveserver_session,
        page_size=page_size,
        credentials=credentials,
        namespace=namespace,
        repo_name=repository,
    )
    assert len(results) == len(expected_tags) if page_size > 0 else len(results) == 0
    assert set([r for r in results]) == set(expected_tags) if page_size > 0 else len(results) == 0


def test_tags_disabled_namespace(
    v2_protocol,
    basic_images,
    liveserver_session,
    app_reloader,
    liveserver,
    registry_server_executor,
):
    credentials = ("devtable", "password")

    # Disable the buynlarge namespace.
    registry_server_executor.on(liveserver).disable_namespace("buynlarge")

    # Try to retrieve the tags and ensure it fails.
    v2_protocol.tags(
        liveserver_session,
        credentials=credentials,
        namespace="buynlarge",
        repo_name="orgrepo",
        expected_failure=Failures.NAMESPACE_DISABLED,
    )


@pytest.mark.parametrize(
    "push_user, push_namespace, push_repo, mount_repo_name, expected_failure",
    [
        # Successful mount, same namespace.
        ("devtable", "devtable", "baserepo", "devtable/baserepo", None),
        # Successful mount, cross namespace.
        ("devtable", "buynlarge", "baserepo", "buynlarge/baserepo", None),
        # Unsuccessful mount, unknown repo.
        ("devtable", "devtable", "baserepo", "unknown/repohere", Failures.UNAUTHORIZED_FOR_MOUNT),
        # Successful mount, super user has access to everything if FEATURE_SUPERUSERS_FULL_ACCESS is set
        ("public", "public", "baserepo", "public/baserepo", None),
    ],
)
def test_blob_mounting(
    push_user,
    push_namespace,
    push_repo,
    mount_repo_name,
    expected_failure,
    manifest_protocol,
    pusher,
    puller,
    basic_images,
    liveserver_session,
    app_reloader,
):
    # Push an image so we can attempt to mount it.
    pusher.push(
        liveserver_session,
        push_namespace,
        push_repo,
        "latest",
        basic_images,
        credentials=(push_user, "password"),
    )

    # Push again, trying to mount the image layer(s) from the mount repo.
    options = ProtocolOptions()
    options.scopes = [
        "repository:devtable/newrepo:push,pull",
        "repository:%s:pull" % (mount_repo_name),
    ]
    options.mount_blobs = {
        "sha256:" + hashlib.sha256(image.bytes).hexdigest(): mount_repo_name
        for image in basic_images
    }

    manifest_protocol.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        basic_images,
        credentials=("devtable", "password"),
        options=options,
        expected_failure=expected_failure,
    )

    if expected_failure is None:
        # Pull to ensure it worked.
        puller.pull(
            liveserver_session,
            "devtable",
            "newrepo",
            "latest",
            basic_images,
            credentials=("devtable", "password"),
        )


def test_blob_mounting_when_superuser_full_access_disabled(
    manifest_protocol,
    pusher,
    basic_images,
    liveserver_session,
    app_reloader,
    registry_server_executor,
    liveserver,
):
    # push as a public user
    pusher.push(
        liveserver_session,
        "public",
        "baserepo",
        "latest",
        basic_images,
        credentials=("public", "password"),
    )

    # disable SUPERUSERS_FULL_ACCESS
    with FeatureFlagValue("SUPERUSERS_FULL_ACCESS", False, registry_server_executor.on(liveserver)):
        options = ProtocolOptions()
        options.scopes = [
            "repository:devtable/newrepo:push,pull",
            "repository:public/baserepo:pull",
        ]
        options.mount_blobs = {
            "sha256:" + hashlib.sha256(image.bytes).hexdigest(): "public/baserepo"
            for image in basic_images
        }

        # Super user without full access should fail to mount
        manifest_protocol.push(
            liveserver_session,
            "devtable",
            "newrepo",
            "latest",
            basic_images,
            credentials=("devtable", "password"),
            options=options,
            expected_failure=Failures.UNAUTHORIZED_FOR_MOUNT,
        )


def test_blob_mounting_with_empty_layers(
    manifest_protocol, pusher, puller, images_with_empty_layer, liveserver_session, app_reloader
):
    # Push an image so we can attempt to mount it.
    pusher.push(
        liveserver_session,
        "devtable",
        "simple",
        "latest",
        images_with_empty_layer,
        credentials=("devtable", "password"),
    )

    # Push again, trying to mount the image layer(s) from the mount repo.
    options = ProtocolOptions()
    options.scopes = [
        "repository:devtable/newrepo:push,pull",
        "repository:%s:pull" % ("devtable/simple"),
    ]
    options.mount_blobs = {
        "sha256:" + hashlib.sha256(image.bytes).hexdigest(): "devtable/simple"
        for image in images_with_empty_layer
    }
    options.skip_head_checks = True

    manifest_protocol.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        images_with_empty_layer,
        credentials=("devtable", "password"),
        options=options,
    )


def get_robot_password(api_caller, username="ownerbot", namespace="buynlarge"):
    api_caller.conduct_auth("devtable", "password")

    if namespace == "buynlarge":
        url = "/api/v1/organization/%s/robots/%s" % (namespace, username)

    # devtable is a User, not an organization. Use a different endpoint
    elif namespace == "devtable":
        url = "/api/v1/user/robots/%s" % username

    else:
        raise NotImplementedError("Other robots have not been implemented for this test.")

    resp = api_caller.get(url)
    return resp.json()["token"]


def get_encrypted_password(api_caller):
    api_caller.conduct_auth("devtable", "password")
    resp = api_caller.post(
        "/api/v1/user/clientkey",
        data=json.dumps(dict(password="password")),
        headers={"Content-Type": "application/json"},
    )
    return resp.json()["key"]


@pytest.mark.parametrize(
    "username, password, expect_success",
    [
        # Invalid username.
        ("invaliduser", "somepassword", False),
        # Invalid password.
        ("devtable", "invalidpassword", False),
        # Invalid OAuth token.
        ("$oauthtoken", "unknown", False),
        # Invalid CLI token.
        ("$app", "invalid", False),
        # Valid.
        ("devtable", "password", True),
        # Robot.
        ("buynlarge+ownerbot", get_robot_password, True),
        # Encrypted password.
        ("devtable", get_encrypted_password, True),
        # OAuth.
        ("$oauthtoken", "%s%s" % ("b" * 40, "c" * 40), True),
        # CLI Token.
        ("$app", "%s%s" % ("a" * 60, "b" * 60), True),
    ],
)
def test_login(
    username, password, expect_success, loginer, liveserver_session, api_caller, app_reloader
):
    """Test: Login flow."""
    if not isinstance(password, str):
        password = password(api_caller)

    loginer.login(liveserver_session, username, password, [], expect_success)


@pytest.mark.parametrize(
    "username, password, scopes, expected_access, expect_success",
    [
        # No scopes.
        ("devtable", "password", [], [], True),
        # Basic pull.
        (
            "devtable",
            "password",
            ["repository:devtable/simple:pull"],
            [
                {"type": "repository", "name": "devtable/simple", "actions": ["pull"]},
            ],
            True,
        ),
        # Basic push.
        (
            "devtable",
            "password",
            ["repository:devtable/simple:push"],
            [
                {"type": "repository", "name": "devtable/simple", "actions": ["push"]},
            ],
            True,
        ),
        # Basic push/pull.
        (
            "devtable",
            "password",
            ["repository:devtable/simple:push,pull"],
            [
                {"type": "repository", "name": "devtable/simple", "actions": ["push", "pull"]},
            ],
            True,
        ),
        # Admin.
        (
            "devtable",
            "password",
            ["repository:devtable/simple:push,pull,*"],
            [
                {"type": "repository", "name": "devtable/simple", "actions": ["push", "pull", "*"]},
            ],
            True,
        ),
        # Basic pull with endpoint.
        (
            "devtable",
            "password",
            ["repository:localhost:5000/devtable/simple:pull"],
            [
                {
                    "type": "repository",
                    "name": "localhost:5000/devtable/simple",
                    "actions": ["pull"],
                },
            ],
            True,
        ),
        # Basic pull with invalid endpoint.
        (
            "devtable",
            "password",
            ["repository:someinvalid/devtable/simple:pull"],
            (
                []
                if not original_app.config["FEATURE_EXTENDED_REPOSITORY_NAMES"]
                else [
                    {
                        "type": "repository",
                        "name": "someinvalid/devtable/simple",
                        "actions": [],
                    },
                ]
            ),
            False if not original_app.config["FEATURE_EXTENDED_REPOSITORY_NAMES"] else True,
        ),
        # Pull with no access.
        (
            "public",
            "password",
            ["repository:devtable/simple:pull"],
            [
                {"type": "repository", "name": "devtable/simple", "actions": []},
            ],
            True,
        ),
        # Anonymous push and pull on a private repository.
        (
            "",
            "",
            ["repository:devtable/simple:pull,push"],
            [
                {"type": "repository", "name": "devtable/simple", "actions": []},
            ],
            True,
        ),
        # Pull and push with no push access.
        (
            "reader",
            "password",
            ["repository:buynlarge/orgrepo:pull,push"],
            [
                {"type": "repository", "name": "buynlarge/orgrepo", "actions": ["pull"]},
            ],
            True,
        ),
        # OAuth.
        (
            "$oauthtoken",
            "%s%s" % ("b" * 40, "c" * 40),
            ["repository:public/publicrepo:pull,push"],
            [
                {"type": "repository", "name": "public/publicrepo", "actions": ["pull"]},
            ],
            True,
        ),
        # Anonymous public repo.
        (
            "",
            "",
            ["repository:public/publicrepo:pull,push"],
            [
                {"type": "repository", "name": "public/publicrepo", "actions": ["pull"]},
            ],
            True,
        ),
        # Multiple scopes.
        (
            "devtable",
            "password",
            ["repository:devtable/simple:push,pull,*", "repository:buynlarge/orgrepo:pull"],
            [
                {"type": "repository", "name": "devtable/simple", "actions": ["push", "pull", "*"]},
                {"type": "repository", "name": "buynlarge/orgrepo", "actions": ["pull"]},
            ],
            True,
        ),
        # Multiple scopes - non-superuser only gets pull access to public/publicrepo
        (
            "reader",
            "password",
            ["repository:buynlarge/orgrepo:pull", "repository:public/publicrepo:push,pull"],
            [
                {"type": "repository", "name": "buynlarge/orgrepo", "actions": ["pull"]},
                {"type": "repository", "name": "public/publicrepo", "actions": ["pull"]},
            ],
            True,
        ),
        # Multiple scopes - devtable is a superuser so gets push access to public/publicrepo
        (
            "devtable",
            "password",
            ["repository:devtable/simple:push,pull,*", "repository:public/publicrepo:push,pull"],
            [
                {"type": "repository", "name": "devtable/simple", "actions": ["push", "pull", "*"]},
                {"type": "repository", "name": "public/publicrepo", "actions": ["push", "pull"]},
            ],
            True,
        ),
        # Read-Only only allows Pulls
        (
            "devtable",
            "password",
            ["repository:devtable/readonly:pull,push,*"],
            [
                {"type": "repository", "name": "devtable/readonly", "actions": ["pull"]},
            ],
            True,
        ),
        # Mirror only allows Pulls
        (
            "devtable",
            "password",
            ["repository:devtable/mirrored:pull,push,*"],
            [
                {"type": "repository", "name": "devtable/mirrored", "actions": ["pull"]},
            ],
            True,
        ),
        # Mirror State as specified Robot --> Allow Pushes
        (
            "devtable+dtrobot",
            get_robot_password,
            ["repository:devtable/mirrored:push,pull,*"],
            [
                {"type": "repository", "name": "devtable/mirrored", "actions": ["push", "pull"]},
            ],
            True,
        ),
        # Mirror State as a Robot w/ write permissions but not the assigned Robot -> No pushes
        (
            "devtable+dtrobot2",
            get_robot_password,
            ["repository:devtable/mirrored:push,pull,*"],
            [
                {"type": "repository", "name": "devtable/mirrored", "actions": ["pull"]},
            ],
            True,
        ),
        # TODO: Add: mirror state with no robot but the robot has write permissions -> no pushes
    ],
)
def test_login_scopes(
    username,
    password,
    scopes,
    expected_access,
    expect_success,
    v2_protocol,
    liveserver_session,
    api_caller,
    app_reloader,
):
    """Test: Login via the V2 auth protocol reacts correctly to requested scopes."""
    if not isinstance(password, str):
        robot_namespace, robot_username = username.split("+")
        password = password(api_caller, namespace=robot_namespace, username=robot_username)

    response = v2_protocol.login(liveserver_session, username, password, scopes, expect_success)
    if not expect_success:
        return

    # Validate the returned token.
    encoded = response.json()["token"]
    payload = decode_bearer_header(
        "Bearer " + encoded, instance_keys, {"SERVER_HOSTNAME": "localhost:5000"}
    )
    assert payload is not None
    assert payload["access"] == expected_access


@pytest.mark.parametrize(
    "username, password, scopes, expected_access, expect_success",
    [
        # only pull in scopes
        (
            "devtable",
            "password",
            "repository:somerepo/that/doesnt/exist:pull",
            [
                {"type": "repository", "name": "somerepo/that/doesnt/exist", "actions": []},
            ],
            True,
        ),
        # push-pull in scopes
        (
            "devtable",
            "password",
            "repository:somerepo/that/doesnt/exist:push,pull",
            [
                {"type": "repository", "name": "somerepo/that/doesnt/exist", "actions": []},
            ],
            True,
        ),
        # star in scope
        (
            "devtable",
            "password",
            "repository:somerepo/that/doesnt/exist:push,pull,*",
            [
                {"type": "repository", "name": "somerepo/that/doesnt/exist", "actions": []},
            ],
            True,
        ),
    ],
)
def test_login_scopes_when_superuser_full_access_disabled_create_repository_on_push_disabled(
    username,
    password,
    scopes,
    expected_access,
    expect_success,
    v2_protocol,
    liveserver_session,
    app_reloader,
    registry_server_executor,
    liveserver,
):
    """
    Tests that super users do not have access to a non-existent namespace/repo if SUPERUSERS_FULL_ACCESS
    is disabled and CREATE_NAMESPACE_ON_PUSH is false.
    """
    with ConfigChange(
        "CREATE_NAMESPACE_ON_PUSH", False, registry_server_executor.on(liveserver), liveserver
    ):
        with FeatureFlagValue(
            "SUPERUSERS_FULL_ACCESS", False, registry_server_executor.on(liveserver)
        ):
            response = v2_protocol.login(
                liveserver_session, username, password, scopes, expect_success
            )
            if not expect_success:
                return

            # Validate the returned token
            encoded = response.json()["token"]
            payload = decode_bearer_header(
                "Bearer " + encoded, instance_keys, {"SERVER_HOSTNAME": "localhost:5000"}
            )
            assert payload is not None
            assert payload["access"] == expected_access


@pytest.mark.parametrize(
    "username, password, scopes, expected_access, expect_success",
    [
        # only pull in scopes
        (
            "devtable",
            "password",
            "repository:somerepo/that/doesnt/exist:pull",
            [
                {"type": "repository", "name": "somerepo/that/doesnt/exist", "actions": ["pull"]},
            ],
            True,
        ),
        # push-pull in scopes
        (
            "devtable",
            "password",
            "repository:somerepo/that/doesnt/exist:push,pull",
            [
                {
                    "type": "repository",
                    "name": "somerepo/that/doesnt/exist",
                    "actions": ["push", "pull"],
                },
            ],
            True,
        ),
        # star in scope
        (
            "devtable",
            "password",
            "repository:somerepo/that/doesnt/exist:push,pull,*",
            [
                {
                    "type": "repository",
                    "name": "somerepo/that/doesnt/exist",
                    "actions": ["push", "pull", "*"],
                },
            ],
            True,
        ),
    ],
)
def test_login_scopes_when_superuser_full_access_enabled_create_repositories_on_push_enabled(
    username,
    password,
    scopes,
    expected_access,
    expect_success,
    v2_protocol,
    liveserver_session,
    app_reloader,
    registry_server_executor,
    liveserver,
):
    """
    Tests that super users have access to a non-existent namespace/repo if SUPERUSERS_FULL_ACCESS
    is enabled and CREATE_NAMESPACE_ON_PUSH is enabled.
    """
    with ConfigChange(
        "CREATE_NAMESPACE_ON_PUSH", True, registry_server_executor.on(liveserver), liveserver
    ):
        with FeatureFlagValue(
            "SUPERUSERS_FULL_ACCESS", True, registry_server_executor.on(liveserver)
        ):
            response = v2_protocol.login(
                liveserver_session, username, password, scopes, expect_success
            )
            if not expect_success:
                return

            # Validate the returned token
            encoded = response.json()["token"]
            payload = decode_bearer_header(
                "Bearer " + encoded, instance_keys, {"SERVER_HOSTNAME": "localhost:5000"}
            )
            assert payload is not None
            assert payload["access"] == expected_access


def test_push_pull_same_blobs(pusher, puller, liveserver_session, app_reloader):
    """Test: Push and pull of an image to a new repository where a blob is shared between layers."""
    credentials = ("devtable", "password")

    layer_bytes = layer_bytes_for_contents(b"some contents")
    images = [
        Image(id="parentid", bytes=layer_bytes, parent_id=None),
        Image(id="someid", bytes=layer_bytes, parent_id="parentid"),
    ]

    options = ProtocolOptions()
    options.skip_head_checks = True  # Since the blob will already exist.

    # Push a new repository.
    pusher.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        images,
        credentials=credentials,
        options=options,
    )

    # Pull the repository to verify.
    puller.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        images,
        credentials=credentials,
        options=options,
    )


def test_push_tag_existing_image(v1_protocol, basic_images, liveserver_session, app_reloader):
    """Test: Push a new tag on an existing image."""
    credentials = ("devtable", "password")

    # Push a new repository.
    v1_protocol.push(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )

    # Pull the repository to verify.
    pulled = v1_protocol.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        basic_images,
        credentials=credentials,
    )
    assert pulled.image_ids

    # Push the same image to another tag in the repository.
    v1_protocol.tag(
        liveserver_session,
        "devtable",
        "newrepo",
        "anothertag",
        pulled.image_ids["latest"],
        credentials=credentials,
    )

    # Pull the repository to verify.
    v1_protocol.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "anothertag",
        basic_images,
        credentials=credentials,
    )


@pytest.mark.parametrize(
    "schema_version",
    [
        1,
        2,
    ],
)
@pytest.mark.parametrize("is_amd", [True, False])
@pytest.mark.parametrize("oci_list", [True, False])
def test_push_pull_manifest_list_back_compat(
    v22_protocol,
    legacy_puller,
    basic_images,
    different_images,
    liveserver_session,
    app_reloader,
    schema_version,
    data_model,
    is_amd,
    oci_list,
):
    """Test: Push a new tag with a manifest list containing two manifests, one (possibly) legacy
    and one not, and, if there is a legacy manifest, ensure it can be pulled.
    """
    credentials = ("devtable", "password")
    options = ProtocolOptions()

    # Build the manifests that will go in the list.
    blobs = {}

    signed = v22_protocol.build_schema1(
        "devtable",
        "newrepo",
        "latest",
        basic_images,
        blobs,
        options,
        arch="amd64" if is_amd else "something",
    )

    if not oci_list:
        first_manifest = signed.unsigned()
        if schema_version == 2:
            first_manifest = v22_protocol.build_schema2(basic_images, blobs, options)

        second_manifest = v22_protocol.build_schema2(different_images, blobs, options)
    else:
        first_manifest = v22_protocol.build_oci(basic_images, blobs, options)
        second_manifest = v22_protocol.build_oci(different_images, blobs, options)

    # Create and push the manifest list.
    builder = OCIIndexBuilder() if oci_list else DockerSchema2ManifestListBuilder()
    builder.add_manifest(first_manifest, "amd64" if is_amd else "something", "linux")
    builder.add_manifest(second_manifest, "arm", "linux")
    manifestlist = builder.build()

    v22_protocol.push_list(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        manifestlist,
        [first_manifest, second_manifest],
        blobs,
        credentials=credentials,
        options=options,
    )

    # Pull the tag and ensure we (don't) get back the basic images, since they are(n't) part of the
    # amd64+linux manifest.
    legacy_puller.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        basic_images,
        credentials=credentials,
        expected_failure=Failures.UNKNOWN_TAG if not is_amd else None,
    )


@pytest.mark.parametrize("schema_version", [1, 2, "oci"])
def test_push_pull_manifest_list(
    v22_protocol,
    basic_images,
    different_images,
    liveserver_session,
    app_reloader,
    schema_version,
    data_model,
):
    """Test: Push a new tag with a manifest list containing two manifests, one (possibly) legacy
    and one not, and pull it.
    """
    credentials = ("devtable", "password")
    options = ProtocolOptions()

    # Build the manifests that will go in the list.
    blobs = {}

    signed = v22_protocol.build_schema1(
        "devtable", "newrepo", "latest", basic_images, blobs, options
    )

    if schema_version == "oci":
        first_manifest = v22_protocol.build_oci(basic_images, blobs, options)
        second_manifest = v22_protocol.build_oci(different_images, blobs, options)
    else:
        first_manifest = signed.unsigned()
        if schema_version == 2:
            first_manifest = v22_protocol.build_schema2(basic_images, blobs, options)

        second_manifest = v22_protocol.build_schema2(different_images, blobs, options)

    # Create and push the manifest list.
    builder = OCIIndexBuilder() if schema_version == "oci" else DockerSchema2ManifestListBuilder()
    builder.add_manifest(first_manifest, "amd64", "linux")
    builder.add_manifest(second_manifest, "arm", "linux")
    manifestlist = builder.build()

    v22_protocol.push_list(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        manifestlist,
        [first_manifest, second_manifest],
        blobs,
        credentials=credentials,
        options=options,
    )

    # Pull and verify the manifest list.
    v22_protocol.pull_list(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        manifestlist,
        credentials=credentials,
        options=options,
    )


def test_push_pull_manifest_remote_layers(
    v22_protocol, legacy_puller, liveserver_session, app_reloader, remote_images, data_model
):
    """Test: Push a new tag with a manifest which contains at least one remote layer, and then
    pull that manifest back.
    """
    credentials = ("devtable", "password")

    # Push a new repository.
    v22_protocol.push(
        liveserver_session, "devtable", "newrepo", "latest", remote_images, credentials=credentials
    )

    # Pull the repository to verify.
    v22_protocol.pull(
        liveserver_session, "devtable", "newrepo", "latest", remote_images, credentials=credentials
    )

    # Ensure that the image cannot be pulled by a legacy protocol.
    legacy_puller.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        remote_images,
        credentials=credentials,
        expected_failure=Failures.UNKNOWN_TAG,
    )


def test_push_manifest_list_missing_manifest(
    v22_protocol, basic_images, liveserver_session, app_reloader, data_model
):
    """Test: Attempt to push a new tag with a manifest list containing an invalid manifest."""
    credentials = ("devtable", "password")
    options = ProtocolOptions()

    # Build the manifests that will go in the list.
    blobs = {}
    manifest = v22_protocol.build_schema2(basic_images, blobs, options)

    # Create and push the manifest list, but without the manifest itself.
    builder = DockerSchema2ManifestListBuilder()
    builder.add_manifest(manifest, "amd64", "linux")
    manifestlist = builder.build()

    v22_protocol.push_list(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        manifestlist,
        [],
        blobs,
        credentials=credentials,
        options=options,
        expected_failure=Failures.INVALID_MANIFEST_IN_LIST,
    )


def test_push_pull_manifest_list_again(
    v22_protocol, basic_images, different_images, liveserver_session, app_reloader, data_model
):
    """Test: Push a new tag with a manifest list containing two manifests, push it again, and pull
    it.
    """
    credentials = ("devtable", "password")
    options = ProtocolOptions()

    # Build the manifests that will go in the list.
    blobs = {}

    first_manifest = v22_protocol.build_schema2(basic_images, blobs, options)
    second_manifest = v22_protocol.build_schema2(different_images, blobs, options)

    # Create and push the manifest list.
    builder = DockerSchema2ManifestListBuilder()
    builder.add_manifest(first_manifest, "amd64", "linux")
    builder.add_manifest(second_manifest, "arm", "linux")
    manifestlist = builder.build()

    v22_protocol.push_list(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        manifestlist,
        [first_manifest, second_manifest],
        blobs,
        credentials=credentials,
        options=options,
    )

    # Push the manifest list again. This should more or less no-op.
    options.skip_head_checks = True
    v22_protocol.push_list(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        manifestlist,
        [first_manifest, second_manifest],
        blobs,
        credentials=credentials,
        options=options,
    )

    # Pull and verify the manifest list.
    v22_protocol.pull_list(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        manifestlist,
        credentials=credentials,
        options=options,
    )


def test_push_pull_manifest_list_duplicate_manifest(
    v22_protocol, basic_images, liveserver_session, app_reloader, data_model
):
    """Test: Push a manifest list that contains the same child manifest twice."""
    credentials = ("devtable", "password")
    options = ProtocolOptions()

    # Build the manifest that will go in the list.
    blobs = {}
    manifest = v22_protocol.build_schema2(basic_images, blobs, options)

    # Create and push the manifest list.
    builder = DockerSchema2ManifestListBuilder()
    builder.add_manifest(manifest, "amd64", "linux")
    builder.add_manifest(manifest, "amd32", "linux")
    manifestlist = builder.build()

    v22_protocol.push_list(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        manifestlist,
        [manifest],
        blobs,
        credentials=credentials,
        options=options,
    )

    # Pull and verify the manifest list.
    v22_protocol.pull_list(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        manifestlist,
        credentials=credentials,
        options=options,
    )


def test_verify_schema2(
    v22_protocol, basic_images, liveserver_session, liveserver, app_reloader, data_model
):
    """Test: Ensure that pushing of schema 2 manifests results in a pull of a schema2 manifest."""
    credentials = ("devtable", "password")

    # Push a new repository.
    v22_protocol.push(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )

    # Pull the repository to verify.
    result = v22_protocol.pull(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )
    manifest = result.manifests["latest"]
    assert manifest.schema_version == 2


@pytest.mark.parametrize(
    "has_amd64_linux",
    [
        False,
        True,
    ],
)
def test_pull_manifest_list_schema2_only(
    v22_protocol,
    basic_images,
    different_images,
    liveserver_session,
    app_reloader,
    data_model,
    has_amd64_linux,
):
    """Test: Push a new tag with a manifest list containing two manifests, one schema2 (possibly)
    amd64 and one not, and pull it when only accepting a schema2 manifest type. Since the manifest
    list content type is not being sent, this should return just the manifest (or none if no
    linux+amd64 is present.)
    """
    credentials = ("devtable", "password")

    # Build the manifests that will go in the list.
    options = ProtocolOptions()
    blobs = {}

    first_manifest = v22_protocol.build_schema2(basic_images, blobs, options)
    second_manifest = v22_protocol.build_schema2(different_images, blobs, options)

    # Create and push the manifest list.
    builder = DockerSchema2ManifestListBuilder()
    builder.add_manifest(first_manifest, "amd64" if has_amd64_linux else "amd32", "linux")
    builder.add_manifest(second_manifest, "arm", "linux")
    manifestlist = builder.build()

    v22_protocol.push_list(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        manifestlist,
        [first_manifest, second_manifest],
        blobs,
        credentials=credentials,
    )

    # Pull and verify the manifest.
    options.accept_mimetypes = [DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE]
    result = v22_protocol.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        basic_images,
        credentials=credentials,
        options=options,
        expected_failure=None if has_amd64_linux else Failures.UNKNOWN_TAG,
    )

    if has_amd64_linux:
        assert result.manifests["latest"].media_type == DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE


def test_push_pull_unicode(pusher, puller, unicode_images, liveserver_session, app_reloader):
    """Test: Push an image with unicode inside and then pull it."""
    credentials = ("devtable", "password")

    # Push a new repository.
    pusher.push(
        liveserver_session, "devtable", "newrepo", "latest", unicode_images, credentials=credentials
    )

    # Pull the repository to verify.
    puller.pull(
        liveserver_session, "devtable", "newrepo", "latest", unicode_images, credentials=credentials
    )


def test_push_pull_unicode_direct(pusher, puller, unicode_images, liveserver_session, app_reloader):
    """Test: Push an image with *unescaped* unicode inside and then pull it."""
    credentials = ("devtable", "password")

    # Turn off automatic unicode encoding when building the manifests.
    options = ProtocolOptions()
    options.ensure_ascii = False

    # Push a new repository.
    pusher.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        unicode_images,
        credentials=credentials,
        options=options,
    )

    # Pull the repository to verify.
    puller.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        unicode_images,
        credentials=credentials,
        options=options,
    )


def test_push_legacy_pull_not_allowed(
    v22_protocol, v1_protocol, remote_images, liveserver_session, app_reloader, data_model
):
    """Test: Push a V2 Schema 2 manifest and attempt to pull via V1 when there is no assigned legacy
    image.
    """
    credentials = ("devtable", "password")

    # Push a new repository.
    v22_protocol.push(
        liveserver_session, "devtable", "newrepo", "latest", remote_images, credentials=credentials
    )

    # Attempt to pull. Should fail with a 404.
    v1_protocol.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        remote_images,
        credentials=credentials,
        expected_failure=Failures.UNKNOWN_TAG,
    )


def test_push_pull_emoji_unicode(
    pusher, puller, unicode_emoji_images, liveserver_session, app_reloader
):
    """Test: Push an image with unicode inside and then pull it."""
    credentials = ("devtable", "password")

    # Push a new repository.
    pusher.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        unicode_emoji_images,
        credentials=credentials,
    )

    # Pull the repository to verify.
    puller.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        unicode_emoji_images,
        credentials=credentials,
    )


def test_push_pull_emoji_unicode_direct(
    pusher, puller, unicode_emoji_images, liveserver_session, app_reloader
):
    """Test: Push an image with *unescaped* unicode inside and then pull it."""
    credentials = ("devtable", "password")

    # Turn off automatic unicode encoding when building the manifests.
    options = ProtocolOptions()
    options.ensure_ascii = False

    # Push a new repository.
    pusher.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        unicode_emoji_images,
        credentials=credentials,
        options=options,
    )

    # Pull the repository to verify.
    puller.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        unicode_emoji_images,
        credentials=credentials,
        options=options,
    )


@pytest.mark.parametrize(
    "accepted_mimetypes",
    [
        [],
        ["application/json"],
    ],
)
def test_push_pull_older_mimetype(
    pusher, puller, basic_images, liveserver_session, app_reloader, accepted_mimetypes
):
    """Test: Push and pull an image, but override the accepted mimetypes to that sent by older
    Docker clients.
    """
    credentials = ("devtable", "password")

    # Push a new repository.
    pusher.push(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )

    # Turn off automatic unicode encoding when building the manifests.
    options = ProtocolOptions()
    options.accept_mimetypes = accepted_mimetypes

    # Pull the repository to verify.
    puller.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        basic_images,
        credentials=credentials,
        options=options,
    )


def test_attempt_push_mismatched_manifest(
    v22_protocol, basic_images, liveserver_session, app_reloader, data_model
):
    """Test: Attempt to push a manifest list refering to a schema 1 manifest with a different
    architecture than that specified in the manifest list.
    """
    credentials = ("devtable", "password")
    options = ProtocolOptions()

    # Build the manifest that will go in the list. This will be amd64.
    blobs = {}
    signed = v22_protocol.build_schema1(
        "devtable", "newrepo", "latest", basic_images, blobs, options
    )
    manifest = signed.unsigned()

    # Create the manifest list, but refer to the manifest as arm.
    builder = DockerSchema2ManifestListBuilder()
    builder.add_manifest(manifest, "arm", "linux")
    manifestlist = builder.build()

    # Attempt to push the manifest, which should fail.
    v22_protocol.push_list(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        manifestlist,
        [manifest],
        blobs,
        credentials=credentials,
        options=options,
        expected_failure=Failures.INVALID_MANIFEST_IN_LIST,
    )


@pytest.mark.parametrize(
    "delete_method",
    [
        "registry",
        "api",
    ],
)
def test_attempt_pull_by_manifest_digest_for_deleted_tag(
    delete_method, manifest_protocol, basic_images, liveserver_session, app_reloader, api_caller
):
    """Test: Attempt to pull a deleted tag's manifest by digest."""
    credentials = ("devtable", "password")

    # Push a new repository.
    result = manifest_protocol.push(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )
    digests = [str(manifest.digest) for manifest in list(result.manifests.values())]
    assert len(digests) == 1

    # Ensure we can pull by tag.
    manifest_protocol.pull(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )

    # Ensure we can pull by digest.
    manifest_protocol.pull(
        liveserver_session, "devtable", "newrepo", digests[0], basic_images, credentials=credentials
    )

    if delete_method == "api":
        api_caller.conduct_auth("devtable", "password")
        resp = api_caller.delete("/api/v1/repository/devtable/newrepo/tag/latest")
        resp.raise_for_status()
    else:
        # Delete the tag by digest.
        manifest_protocol.delete(
            liveserver_session, "devtable", "newrepo", digests[0], credentials=credentials
        )

    # Attempt to pull from the repository by digests to verify it shows they are not accessible.
    manifest_protocol.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        digests[0],
        basic_images,
        credentials=credentials,
        expected_failure=Failures.UNKNOWN_TAG,
    )


@pytest.mark.parametrize(
    "state, use_robot, create_mirror, expected_failure",
    [
        ("NORMAL", True, True, None),
        ("NORMAL", False, True, None),
        ("NORMAL", True, False, None),
        ("NORMAL", False, False, None),
        ("READ_ONLY", True, True, Failures.READ_ONLY),
        ("READ_ONLY", False, True, Failures.READ_ONLY),
        ("READ_ONLY", True, False, Failures.READ_ONLY),
        ("READ_ONLY", False, False, Failures.READ_ONLY),
        ("MIRROR", True, True, None),
        ("MIRROR", False, True, Failures.MIRROR_ONLY),
        ("MIRROR", True, False, Failures.MIRROR_MISCONFIGURED),
        ("MIRROR", False, False, Failures.MIRROR_MISCONFIGURED),
    ],
)
def test_repository_states(
    state,
    use_robot,
    create_mirror,
    expected_failure,
    pusher,
    puller,
    basic_images,
    liveserver_session,
    api_caller,
    app_reloader,
):
    """
    Verify the push behavior of the Repository dependent upon its state.
    """
    namespace = "devtable"
    repo = "staterepo"
    tag = "latest"
    credentials = ("devtable", "password")
    robot = "dtrobot"
    robot_full_name = "%s+%s" % (namespace, robot)

    # Create repository
    pusher.push(liveserver_session, namespace, repo, tag, basic_images, credentials=credentials)

    # Login with API Caller
    api_caller.conduct_auth(*credentials)

    # When testing the Robot, assume the robot existed and had the correct permissions *before*
    # mirroring was configured.
    if use_robot:
        url = "/api/v1/repository/%s/%s/permissions/user/%s" % (namespace, repo, robot_full_name)
        data = json.dumps({"role": "write"})
        resp = api_caller.put(url, data=data, headers={"content-type": "application/json"})
        assert resp

    if create_mirror:
        params = {
            "is_enabled": True,
            "external_reference": "quay.io/foo/bar",
            "external_registry_username": "fakeusername",
            "external_registry_password": "fakepassword",
            "sync_interval": 1000,
            "skopeo_timeout_interval": 300,
            "sync_start_date": "2020-01-01T00:00:00Z",
            "root_rule": {"rule_kind": "tag_glob_csv", "rule_value": ["latest", "1.3*", "foo"]},
            "robot_username": robot_full_name,
            "external_registry_config": {
                "verify_tls": True,
                "proxy": {
                    "https_proxy": "https://proxy.example.net",
                    "http_proxy": "http://insecure.proxy.com",
                    "no_proxy": "local.lan.com",
                },
            },
        }

        url = "/api/v1/repository/%s/%s/mirror" % (namespace, repo)
        data = json.dumps(params)
        headers = {"Content-Type": "application/json"}
        resp = api_caller.post(url, data=data, headers=headers)
        assert resp  # Mirror was created successfully

    # Set the state of the Repository
    url = "/api/v1/repository/%s/%s/changestate" % (namespace, repo)
    data = json.dumps({"state": state})
    resp = api_caller.put(url, data=data, headers={"content-type": "application/json"})
    assert resp  # State was changed successfully

    # If testing the registry as the Robot user, use the Robot's credentials for all proceeding
    # API Calls
    if use_robot:
        resp = api_caller.get("/api/v1/user/robots/%s" % robot)
        data = resp.json()
        assert resp  # Robot credentials fetched successfully
        credentials = (data["name"], data["token"])

    # Verify pulls/reads still work
    puller.pull(liveserver_session, namespace, repo, tag, basic_images, credentials=credentials)

    # Should not be able to delete a robot attached to a mirror
    resp = api_caller.delete("/api/v1/user/robots/%s" % robot)
    assert resp.status_code == 400

    # Verify that the Repository.state determines whether pushes/changes are allowed
    options = ProtocolOptions()
    options.skip_head_checks = True
    pusher.push(
        liveserver_session,
        namespace,
        repo,
        tag,
        basic_images,
        credentials=credentials,
        expected_failure=expected_failure,
        options=options,
    )


def test_readonly_push_pull(
    pusher,
    puller,
    basic_images,
    different_images,
    liveserver_session,
    app_reloader,
    api_caller,
    liveserver,
    registry_server_executor,
):
    """Test: Basic push an image to a new repository, followed by changing the system to readonly
    and then attempting a pull and a push.
    """
    credentials = ("devtable", "password")

    # Push a new repository.
    pusher.push(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )

    # Change to read-only mode.
    with ConfigChange(
        "REGISTRY_STATE", "readonly", registry_server_executor.on(liveserver), liveserver
    ):
        # Pull the repository to verify.
        puller.pull(
            liveserver_session,
            "devtable",
            "newrepo",
            "latest",
            basic_images,
            credentials=credentials,
        )

        # Attempt to push to the repository, which should fail.
        pusher.push(
            liveserver_session,
            "devtable",
            "newrepo",
            "latest",
            different_images,
            credentials=credentials,
            expected_failure=Failures.READONLY_REGISTRY,
        )

        # Pull again to verify nothing has changed.
        puller.pull(
            liveserver_session,
            "devtable",
            "newrepo",
            "latest",
            basic_images,
            credentials=credentials,
        )


def test_malformed_schema2_config(v22_protocol, basic_images, liveserver_session, app_reloader):
    """
    Test: Attempt to push a manifest pointing to a config JSON that is missing a required
    key.
    """
    options = ProtocolOptions()
    options.with_broken_manifest_config = True

    credentials = ("devtable", "password")

    # Attempt to push a new repository. Should raise a 400.
    v22_protocol.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        basic_images,
        credentials=credentials,
        options=options,
        expected_failure=Failures.INVALID_MANIFEST,
    )


def test_conftest_policy_push(manifest_protocol, openpolicyagent_policy, liveserver_session):
    credentials = ("devtable", "password")

    options = ProtocolOptions()
    options.push_by_manifest_digest = True

    # Push policy by digest
    result = manifest_protocol.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        openpolicyagent_policy,
        credentials=credentials,
        options=options,
    )

    # Pull the policy by digests to verify.
    digests = [str(manifest.digest) for manifest in list(result.manifests.values())]
    manifest_protocol.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        digests,
        openpolicyagent_policy,
        credentials=credentials,
        expected_failure=None,
    )


def test_attempt_pull_by_tag_reference_for_deleted_tag(
    manifest_protocol, basic_images, liveserver_session
):
    """Test: Delete a tag by specifying the reference in a v2 call"""
    credentials = ("devtable", "password")

    # Push the new repository
    result = manifest_protocol.push(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )
    digests = [str(manifest.digest) for manifest in list(result.manifests.values())]
    assert len(digests) == 1

    # Ensure we can pull by digest
    manifest_protocol.pull(
        liveserver_session, "devtable", "newrepo", digests[0], basic_images, credentials=credentials
    )

    # Ensure we can pull by tag
    manifest_protocol.pull(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )

    # Delete the tag by reference
    manifest_protocol.delete(
        liveserver_session, "devtable", "newrepo", "latest", credentials=credentials
    )

    # Attempt to pull from the repository by tag to verify it's not accessible
    manifest_protocol.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "latest",
        basic_images,
        credentials=credentials,
        expected_failure=Failures.UNKNOWN_TAG,
    )


def test_push_immutable_tag_blocked(
    manifest_protocol,
    basic_images,
    different_images,
    liveserver_session,
    app_reloader,
    liveserver,
    registry_server_executor,
):
    """Test: Pushing to an immutable tag is blocked with a 409 error."""
    credentials = ("devtable", "password")

    with FeatureFlagValue("IMMUTABLE_TAGS", True, registry_server_executor.on(liveserver)):
        # Push an image to a new repository.
        manifest_protocol.push(
            liveserver_session,
            "devtable",
            "newrepo",
            "latest",
            basic_images,
            credentials=credentials,
        )

        # Make the tag immutable via the live server executor.
        registry_server_executor.on(liveserver).make_tag_immutable("devtable", "newrepo", "latest")

        # Attempt to push a different image to the same tag, which should fail.
        manifest_protocol.push(
            liveserver_session,
            "devtable",
            "newrepo",
            "latest",
            different_images,
            credentials=credentials,
            expected_failure=Failures.TAG_IMMUTABLE,
        )

        # Verify the original image is still intact.
        manifest_protocol.pull(
            liveserver_session,
            "devtable",
            "newrepo",
            "latest",
            basic_images,
            credentials=credentials,
        )


def test_delete_immutable_tag_blocked(
    manifest_protocol,
    basic_images,
    liveserver_session,
    app_reloader,
    liveserver,
    registry_server_executor,
):
    """Test: Deleting an immutable tag is blocked with a 409 error."""
    credentials = ("devtable", "password")

    with FeatureFlagValue("IMMUTABLE_TAGS", True, registry_server_executor.on(liveserver)):
        # Push an image.
        manifest_protocol.push(
            liveserver_session,
            "devtable",
            "newrepo",
            "latest",
            basic_images,
            credentials=credentials,
        )

        # Make the tag immutable.
        registry_server_executor.on(liveserver).make_tag_immutable("devtable", "newrepo", "latest")

        # Attempt to delete the tag, which should fail.
        manifest_protocol.delete(
            liveserver_session,
            "devtable",
            "newrepo",
            "latest",
            credentials=credentials,
            expected_failure=Failures.TAG_IMMUTABLE,
        )

        # Verify the tag is still accessible.
        manifest_protocol.pull(
            liveserver_session,
            "devtable",
            "newrepo",
            "latest",
            basic_images,
            credentials=credentials,
        )


def test_push_different_tag_with_immutable_tag_succeeds(
    manifest_protocol,
    basic_images,
    different_images,
    liveserver_session,
    app_reloader,
    liveserver,
    registry_server_executor,
):
    """Test: Pushing a different tag succeeds when other immutable tags exist in the repo."""
    credentials = ("devtable", "password")

    # Push an image and make it immutable.
    manifest_protocol.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "immutable",
        basic_images,
        credentials=credentials,
    )
    registry_server_executor.on(liveserver).make_tag_immutable("devtable", "newrepo", "immutable")

    # Push a different image to a different tag in the same repo - should succeed.
    manifest_protocol.push(
        liveserver_session,
        "devtable",
        "newrepo",
        "mutable",
        different_images,
        credentials=credentials,
    )

    # Verify both tags are accessible.
    manifest_protocol.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "immutable",
        basic_images,
        credentials=credentials,
    )
    manifest_protocol.pull(
        liveserver_session,
        "devtable",
        "newrepo",
        "mutable",
        different_images,
        credentials=credentials,
    )


def test_push_and_delete_immutable_tag_allowed_when_feature_disabled(
    manifest_protocol,
    basic_images,
    different_images,
    liveserver_session,
    app_reloader,
    liveserver,
    registry_server_executor,
):
    """Test: Immutability is not enforced when FEATURE_IMMUTABLE_TAGS is disabled."""
    credentials = ("devtable", "password")

    # Push an image and mark the tag immutable.
    manifest_protocol.push(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )
    registry_server_executor.on(liveserver).make_tag_immutable("devtable", "newrepo", "latest")

    # Disable the feature flag — enforcement should be bypassed.
    with FeatureFlagValue("IMMUTABLE_TAGS", False, registry_server_executor.on(liveserver)):
        # Pushing a different image to the same tag should succeed.
        manifest_protocol.push(
            liveserver_session,
            "devtable",
            "newrepo",
            "latest",
            different_images,
            credentials=credentials,
        )

        # Pull to confirm the new image replaced the old one.
        manifest_protocol.pull(
            liveserver_session,
            "devtable",
            "newrepo",
            "latest",
            different_images,
            credentials=credentials,
        )

        # Deleting the tag should also succeed.
        manifest_protocol.delete(
            liveserver_session, "devtable", "newrepo", "latest", credentials=credentials
        )

        # Pull to confirm the tag is gone.
        manifest_protocol.pull(
            liveserver_session,
            "devtable",
            "newrepo",
            "latest",
            different_images,
            credentials=credentials,
            expected_failure=Failures.UNKNOWN_TAG,
        )


def test_push_existing_blob_while_blob_delete_lock_held(
    v22_protocol,
    basic_images,
    liveserver_session,
    liveserver,
    registry_server_executor,
    app_reloader,
):
    """Test: Committing a blob that already exists does not stall while another holder (e.g. GC)
    has its BLOB_DELETE lock. The commit waits BLOB_DELETE_LOCK_ACQUIRE_TIMEOUT, then links the
    existing blob."""
    credentials = ("devtable", "password")
    v22_protocol.push(
        liveserver_session, "devtable", "newrepo", "latest", basic_images, credentials=credentials
    )

    digest = "sha256:" + hashlib.sha256(basic_images[0].bytes).hexdigest()
    executor = registry_server_executor.on(liveserver)
    assert executor.hold_blob_lock(digest, 60).status_code == 200

    # Before the fix the commit blocked until the holder's lock expired (here 60s).
    start = time.time()
    v22_protocol.push(
        liveserver_session,
        "devtable",
        "anotherrepo",
        "latest",
        basic_images,
        credentials=credentials,
    )
    assert time.time() - start < 20

    v22_protocol.pull(
        liveserver_session,
        "devtable",
        "anotherrepo",
        "latest",
        basic_images,
        credentials=credentials,
    )


def test_push_new_blob_while_blob_delete_lock_held(
    v22_protocol, liveserver_session, liveserver, registry_server_executor, app_reloader
):
    """Test: Committing a new blob while another holder has its BLOB_DELETE lock fails with a
    retryable 503 within the bound instead of stalling or creating the blob unlocked, and a retry
    after the lock is released succeeds."""
    credentials = ("devtable", "password")
    blob_bytes = layer_bytes_for_contents(b"blob delete lock contents")
    digest = "sha256:" + hashlib.sha256(blob_bytes).hexdigest()
    repo_name = v22_protocol.repo_name("devtable", "newrepo")

    token, _ = v22_protocol.auth(
        liveserver_session,
        credentials,
        "devtable",
        "newrepo",
        scopes=["repository:%s:push,pull" % repo_name],
    )
    headers = {"Authorization": "Bearer " + token}

    def upload_blob(expected_status):
        response = v22_protocol.conduct(
            liveserver_session,
            "POST",
            "/v2/%s/blobs/uploads/" % repo_name,
            expected_status=202,
            headers=headers,
        )
        location = response.headers["Location"][len("http://localhost:5000") :]
        v22_protocol.conduct(
            liveserver_session,
            "PATCH",
            location,
            data=blob_bytes,
            expected_status=202,
            headers=headers,
        )
        return v22_protocol.conduct(
            liveserver_session,
            "PUT",
            location,
            params=dict(digest=digest),
            expected_status=expected_status,
            headers=headers,
        )

    executor = registry_server_executor.on(liveserver)
    assert executor.hold_blob_lock(digest, 60).status_code == 200

    start = time.time()
    response = upload_blob(503)
    assert response.json()["errors"][0]["code"] == "UNAVAILABLE"
    assert time.time() - start < 20

    v22_protocol.conduct(
        liveserver_session,
        "HEAD",
        "/v2/%s/blobs/%s" % (repo_name, digest),
        expected_status=404,
        headers=headers,
    )

    assert executor.release_blob_lock(digest).status_code == 200
    upload_blob(201)
    v22_protocol.conduct(
        liveserver_session,
        "HEAD",
        "/v2/%s/blobs/%s" % (repo_name, digest),
        expected_status=200,
        headers=headers,
    )


def test_push_new_blob_while_registry_lock_held(
    v22_protocol, liveserver_session, liveserver, registry_server_executor, app_reloader
):
    """Test: Committing a new blob while a non-GC holder has its BLOB_DELETE lock succeeds with
    201.  A registry worker (or an unreadable response-mixing artifact such as "OK" or None) as
    the lock holder is not a GC deletion in progress, so the push proceeds and relies on the
    IntegrityError guard for any duplicate-creation race between concurrent registry workers."""
    credentials = ("devtable", "password")
    blob_bytes = layer_bytes_for_contents(b"registry lock holder contents")
    digest = "sha256:" + hashlib.sha256(blob_bytes).hexdigest()
    repo_name = v22_protocol.repo_name("devtable", "newrepo")

    token, _ = v22_protocol.auth(
        liveserver_session,
        credentials,
        "devtable",
        "newrepo",
        scopes=["repository:%s:push,pull" % repo_name],
    )
    headers = {"Authorization": "Bearer " + token}

    def upload_blob(expected_status):
        response = v22_protocol.conduct(
            liveserver_session,
            "POST",
            "/v2/%s/blobs/uploads/" % repo_name,
            expected_status=202,
            headers=headers,
        )
        location = response.headers["Location"][len("http://localhost:5000") :]
        v22_protocol.conduct(
            liveserver_session,
            "PATCH",
            location,
            data=blob_bytes,
            expected_status=202,
            headers=headers,
        )
        return v22_protocol.conduct(
            liveserver_session,
            "PUT",
            location,
            params=dict(digest=digest),
            expected_status=expected_status,
            headers=headers,
        )

    executor = registry_server_executor.on(liveserver)
    # Hold the lock as a registry worker — not a GC role, simulating response mixing
    # where the "owner" is another registry worker or an unreadable artifact.
    assert executor.hold_blob_lock(digest, 60, "registry:7:test").status_code == 200

    # Must succeed: non-GC holders do not block blob creation.
    start = time.time()
    upload_blob(201)
    assert time.time() - start < 20

    v22_protocol.conduct(
        liveserver_session,
        "HEAD",
        "/v2/%s/blobs/%s" % (repo_name, digest),
        expected_status=200,
        headers=headers,
    )


# ---------------------------------------------------------------------------------------------
# PROJQUAY-10117 Proposal 1: configurable digest algorithms (blob uploads).
# ---------------------------------------------------------------------------------------------

ALL_HASH_ALGORITHMS = ["sha256", "sha384", "sha512"]
LIVESERVER_PREFIX = "http://localhost:5000"


def _alg_digest(algorithm, data):
    return "%s:%s" % (algorithm, hashlib.new(algorithm, data).hexdigest())


@pytest.fixture()
def all_hash_algorithms(liveserver, registry_server_executor, app_reloader):
    with ConfigChange(
        "ALLOWED_HASH_ALGORITHMS",
        ALL_HASH_ALGORITHMS,
        registry_server_executor.on(liveserver),
        liveserver,
    ):
        yield


def _push_headers(protocol, session, repo="newrepo", namespace="devtable"):
    repo_name = protocol.repo_name(namespace, repo)
    token, _ = protocol.auth(
        session,
        ("devtable", "password"),
        namespace,
        repo,
        scopes=["repository:%s:push,pull" % repo_name],
    )
    return {"Authorization": "Bearer " + token}, repo_name


def _error_code(response):
    return response.json()["errors"][0]["code"]


def _location(response):
    return response.headers["Location"][len(LIVESERVER_PREFIX) :]


def _upload(protocol, session, headers, repo_name, data, digest, path, hint=None, expected=201):
    """Uploads `data` via blob upload path A, B, C or a monolithic PUT, finishing with `digest`."""
    if path == "A":
        return protocol.conduct(
            session,
            "POST",
            "/v2/%s/blobs/uploads/" % repo_name,
            params=dict(digest=digest),
            data=data,
            expected_status=expected,
            headers=headers,
        )

    params = {}
    if path == "B" or hint is not None:
        params["digest-algorithm"] = hint or digest.split(":", 1)[0]
    response = protocol.conduct(
        session,
        "POST",
        "/v2/%s/blobs/uploads/" % repo_name,
        params=params,
        expected_status=202,
        headers=headers,
    )
    location = _location(response)

    if path == "PUT":
        # The whole body arrives with the final PUT.
        return protocol.conduct(
            session,
            "PUT",
            location,
            params=dict(digest=digest),
            data=data,
            expected_status=expected,
            headers=headers,
        )

    half = len(data) // 2
    for start, chunk in ((0, data[:half]), (half, data[half:])):
        chunk_headers = dict(headers)
        chunk_headers["Content-Range"] = "%d-%d" % (start, start + len(chunk) - 1)
        response = protocol.conduct(
            session,
            "PATCH",
            location,
            data=chunk,
            expected_status=202,
            headers=chunk_headers,
        )
        location = _location(response)

    return protocol.conduct(
        session,
        "PUT",
        location,
        params=dict(digest=digest),
        expected_status=expected,
        headers=headers,
    )


def _assert_blob_served(protocol, session, headers, repo_name, data, digest):
    for method in ("HEAD", "GET"):
        response = protocol.conduct(
            session,
            method,
            "/v2/%s/blobs/%s" % (repo_name, digest),
            expected_status=200,
            headers=headers,
        )
        assert response.headers["Docker-Content-Digest"] == digest
        if method == "GET":
            assert response.content == data


def _assert_blob_not_served(protocol, session, headers, repo_name, digest):
    for method in ("HEAD", "GET"):
        protocol.conduct(
            session,
            method,
            "/v2/%s/blobs/%s" % (repo_name, digest),
            expected_status=404,
            headers=headers,
        )


@pytest.mark.parametrize("algorithm", ALL_HASH_ALGORITHMS)
@pytest.mark.parametrize("path", ["A", "B", "C", "PUT"])
def test_requested_digest_blob_upload_paths(
    algorithm, path, v22_protocol, liveserver_session, all_hash_algorithms
):
    """Every blob upload path publishes the requested digest and reports it in Location and
    Docker-Content-Digest; a SHA-384/512-only blob is never served under its internal SHA-256."""
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    data = layer_bytes_for_contents(("%s %s contents" % (algorithm, path)).encode("ascii"))
    digest = _alg_digest(algorithm, data)

    response = _upload(v22_protocol, liveserver_session, headers, repo_name, data, digest, path)
    assert response.headers["Docker-Content-Digest"] == digest
    assert response.headers["Location"].endswith("/v2/%s/blobs/%s" % (repo_name, digest))

    _assert_blob_served(v22_protocol, liveserver_session, headers, repo_name, data, digest)
    if algorithm != "sha256":
        _assert_blob_not_served(
            v22_protocol, liveserver_session, headers, repo_name, _alg_digest("sha256", data)
        )


@pytest.mark.parametrize(
    "hint, final", [("sha512", "sha384"), ("sha384", "sha512"), ("sha512", "sha256")]
)
def test_requested_digest_final_digest_overrides_hint(
    hint, final, v22_protocol, liveserver_session, all_hash_algorithms
):
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    data = layer_bytes_for_contents(("hint %s final %s" % (hint, final)).encode("ascii"))
    digest = _alg_digest(final, data)
    _upload(v22_protocol, liveserver_session, headers, repo_name, data, digest, "C", hint=hint)
    _assert_blob_served(v22_protocol, liveserver_session, headers, repo_name, data, digest)


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_requested_digest_disallowed_writes_at_default_allowlist(
    algorithm, v22_protocol, liveserver_session, app_reloader
):
    """At the default [sha256] allowlist, a write naming sha384/sha512 returns 400 DIGEST_INVALID:
    upload digest (POST and final PUT), digest-algorithm hint, and mount (opening no upload)."""
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    data = layer_bytes_for_contents(b"disallowed write contents")
    digest = _alg_digest(algorithm, data)

    response = _upload(
        v22_protocol, liveserver_session, headers, repo_name, data, digest, "A", expected=400
    )
    assert _error_code(response) == "DIGEST_INVALID"

    response = v22_protocol.conduct(
        liveserver_session,
        "POST",
        "/v2/%s/blobs/uploads/" % repo_name,
        params={"digest-algorithm": algorithm},
        expected_status=400,
        headers=headers,
    )
    assert _error_code(response) == "DIGEST_INVALID"

    response = v22_protocol.conduct(
        liveserver_session,
        "POST",
        "/v2/%s/blobs/uploads/" % repo_name,
        params={"mount": digest, "from": "devtable/simple"},
        expected_status=400,
        headers=headers,
    )
    assert _error_code(response) == "DIGEST_INVALID"
    assert "Location" not in response.headers
    assert "Docker-Upload-UUID" not in response.headers

    response = _upload(
        v22_protocol, liveserver_session, headers, repo_name, data, digest, "C", expected=400
    )
    assert _error_code(response) == "DIGEST_INVALID"

    # A sha256 hint is accepted at the default allowlist.
    v22_protocol.conduct(
        liveserver_session,
        "POST",
        "/v2/%s/blobs/uploads/" % repo_name,
        params={"digest-algorithm": "sha256"},
        expected_status=202,
        headers=headers,
    )


@pytest.mark.parametrize(
    "bad_digest",
    [
        "sha512:" + "A" * 128,
        "sha512:" + "a" * 127,
        "sha384:" + "a" * 64,
        "SHA512:" + "a" * 128,
        "md5:" + "a" * 32,
        "sha1:" + "a" * 40,
    ],
)
def test_requested_digest_malformed_or_unsupported_writes(
    bad_digest, v22_protocol, liveserver_session, all_hash_algorithms
):
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    data = layer_bytes_for_contents(b"malformed write contents")

    for path in ("A", "C"):
        response = _upload(
            v22_protocol, liveserver_session, headers, repo_name, data, bad_digest, path,
            expected=400,
        )
        assert _error_code(response) == "DIGEST_INVALID"

    response = v22_protocol.conduct(
        liveserver_session,
        "POST",
        "/v2/%s/blobs/uploads/" % repo_name,
        params={"mount": bad_digest, "from": "devtable/simple"},
        expected_status=400,
        headers=headers,
    )
    assert _error_code(response) == "DIGEST_INVALID"


@pytest.mark.parametrize("hint", ["sha1", "SHA512", "md5", "sha3-512", ""])
def test_requested_digest_bad_hint(hint, v22_protocol, liveserver_session, all_hash_algorithms):
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    response = v22_protocol.conduct(
        liveserver_session,
        "POST",
        "/v2/%s/blobs/uploads/" % repo_name,
        params={"digest-algorithm": hint},
        expected_status=400,
        headers=headers,
    )
    assert _error_code(response) == "DIGEST_INVALID"


@pytest.mark.parametrize("algorithm", ALL_HASH_ALGORITHMS)
@pytest.mark.parametrize("path", ["A", "B", "C"])
def test_requested_digest_content_mismatch(
    algorithm, path, v22_protocol, liveserver_session, all_hash_algorithms
):
    """Bytes that don't match a requested SHA-384/512 digest return 400 DIGEST_INVALID; SHA-256
    mismatches keep today's 400 BLOB_UPLOAD_INVALID. Nothing is published."""
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    data = layer_bytes_for_contents(b"mismatched contents")
    wrong = _alg_digest(algorithm, data + b"other")

    response = _upload(
        v22_protocol, liveserver_session, headers, repo_name, data, wrong, path, expected=400
    )
    expected_code = "BLOB_UPLOAD_INVALID" if algorithm == "sha256" else "DIGEST_INVALID"
    assert _error_code(response) == expected_code
    _assert_blob_not_served(v22_protocol, liveserver_session, headers, repo_name, wrong)


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_requested_digest_new_row_while_gc_holds_lock(
    algorithm, v22_protocol, liveserver_session, liveserver, registry_server_executor,
    all_hash_algorithms,
):
    """Publishing a new SHA-384/512 storage row while GC holds BLOB_DELETE_<sha256> returns a
    retryable 503 UNAVAILABLE; a retry after the lock is released succeeds."""
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    data = layer_bytes_for_contents(("gc lock %s" % algorithm).encode("ascii"))
    digest = _alg_digest(algorithm, data)

    executor = registry_server_executor.on(liveserver)
    sha256 = _alg_digest("sha256", data)
    assert executor.hold_blob_lock(sha256, 60).status_code == 200

    start = time.time()
    response = _upload(
        v22_protocol, liveserver_session, headers, repo_name, data, digest, "B", expected=503
    )
    assert _error_code(response) == "UNAVAILABLE"
    assert time.time() - start < 20
    _assert_blob_not_served(v22_protocol, liveserver_session, headers, repo_name, digest)

    assert executor.release_blob_lock(sha256).status_code == 200
    _upload(v22_protocol, liveserver_session, headers, repo_name, data, digest, "B")
    _assert_blob_served(v22_protocol, liveserver_session, headers, repo_name, data, digest)


@pytest.mark.parametrize("algorithm", ALL_HASH_ALGORITHMS)
def test_requested_digest_corrupt_stored_object(
    algorithm, v22_protocol, liveserver_session, liveserver, registry_server_executor,
    all_hash_algorithms,
):
    """Stored-bytes verification failure on reuse returns 500 UNKNOWN; nothing is published and
    the stored object is not repaired."""
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    data = layer_bytes_for_contents(("corrupt object %s" % algorithm).encode("ascii"))
    digest = _alg_digest(algorithm, data)
    corrupt = bytes([data[0] ^ 0xFF]) + data[1:]

    executor = registry_server_executor.on(liveserver)
    assert executor.put_cas_object(_alg_digest("sha256", data), corrupt.hex()).status_code == 200

    response = _upload(
        v22_protocol, liveserver_session, headers, repo_name, data, digest, "B", expected=500
    )
    assert _error_code(response) == "UNKNOWN"
    _assert_blob_not_served(v22_protocol, liveserver_session, headers, repo_name, digest)


@pytest.mark.parametrize("algorithm", ALL_HASH_ALGORITHMS)
def test_requested_digest_mount_links_exact_identity(
    algorithm, v22_protocol, liveserver_session, all_hash_algorithms
):
    """Mounting a blob publishes only the mounted digest in the target repository."""
    headers, repo_name = _push_headers(v22_protocol, liveserver_session, repo="mountsource")
    data = layer_bytes_for_contents(("mount %s" % algorithm).encode("ascii"))
    digest = _alg_digest(algorithm, data)
    _upload(v22_protocol, liveserver_session, headers, repo_name, data, digest, "A")

    target_headers, target_repo = _push_headers(v22_protocol, liveserver_session, repo="mounttarget")
    # The target token also needs pull on the source repository.
    token, _ = v22_protocol.auth(
        liveserver_session,
        ("devtable", "password"),
        "devtable",
        "mounttarget",
        scopes=[
            "repository:%s:push,pull" % target_repo,
            "repository:%s:pull" % repo_name,
        ],
    )
    target_headers = {"Authorization": "Bearer " + token}

    response = v22_protocol.conduct(
        liveserver_session,
        "POST",
        "/v2/%s/blobs/uploads/" % target_repo,
        params={"mount": digest, "from": repo_name},
        expected_status=201,
        headers=target_headers,
    )
    assert response.headers["Docker-Content-Digest"] == digest

    _assert_blob_served(v22_protocol, liveserver_session, target_headers, target_repo, data, digest)
    if algorithm != "sha256":
        _assert_blob_not_served(
            v22_protocol, liveserver_session, target_headers, target_repo,
            _alg_digest("sha256", data),
        )


# ---------------------------------------------------------------------------------------------
# PROJQUAY-10117 Proposal 1: configurable digest algorithms (manifests).
# ---------------------------------------------------------------------------------------------

import json as _json

OCI_MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX_TYPE = "application/vnd.oci.image.index.v1+json"
OCI_CONFIG_TYPE = "application/vnd.oci.image.config.v1+json"
OCI_LAYER_TYPE = "application/vnd.oci.image.layer.v1.tar+gzip"
DOCKER_V2_MANIFEST_TYPE = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_V2_CONFIG_TYPE = "application/vnd.docker.container.image.v1+json"
DOCKER_V2_LAYER_TYPE = "application/vnd.docker.image.rootfs.diff.tar.gzip"


def _push_test_blob(protocol, session, headers, repo_name, algorithm, data):
    digest = _alg_digest(algorithm, data)
    _upload(protocol, session, headers, repo_name, data, digest, "A")
    return {"digest": digest, "size": len(data)}


def _config_bytes(label):
    return _json.dumps(
        {
            "architecture": "amd64",
            "os": "linux",
            "config": {"Labels": {"test": label}},
            "rootfs": {"type": "layers", "diff_ids": []},
            "history": [{"created_by": "test"}],
        },
        sort_keys=True,
    ).encode("utf-8")


def _image_manifest_bytes(config, layers, subject=None, docker=False):
    manifest = {
        "schemaVersion": 2,
        "mediaType": DOCKER_V2_MANIFEST_TYPE if docker else OCI_MANIFEST_TYPE,
        "config": {
            "mediaType": DOCKER_V2_CONFIG_TYPE if docker else OCI_CONFIG_TYPE,
            "digest": config["digest"],
            "size": config["size"],
        },
        "layers": [
            {
                "mediaType": DOCKER_V2_LAYER_TYPE if docker else OCI_LAYER_TYPE,
                "digest": layer["digest"],
                "size": layer["size"],
            }
            for layer in layers
        ],
    }
    if subject is not None:
        manifest["subject"] = subject
    return _json.dumps(manifest, sort_keys=True).encode("utf-8")


def _index_bytes(children):
    return _json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX_TYPE,
            "manifests": [
                {
                    "mediaType": OCI_MANIFEST_TYPE,
                    "digest": child["digest"],
                    "size": child["size"],
                    "platform": {"architecture": arch, "os": "linux"},
                }
                for arch, child in children
            ],
        },
        sort_keys=True,
    ).encode("utf-8")


def _push_image_blobs(protocol, session, headers, repo_name, algorithm, label):
    config = _push_test_blob(
        protocol, session, headers, repo_name, algorithm, _config_bytes(label)
    )
    layer_data = layer_bytes_for_contents(("layer %s %s" % (label, algorithm)).encode("ascii"))
    layer = _push_test_blob(protocol, session, headers, repo_name, algorithm, layer_data)
    layer["bytes"] = layer_data
    return config, layer


def _put_manifest(
    protocol, session, headers, repo_name, ref, manifest_bytes, media_type, expected=201,
    params=None,
):
    put_headers = dict(headers)
    put_headers["Content-Type"] = media_type
    return protocol.conduct(
        session,
        "PUT",
        "/v2/%s/manifests/%s" % (repo_name, ref),
        data=manifest_bytes,
        params=params,
        expected_status=expected,
        headers=put_headers,
    )


def _get_manifest(protocol, session, headers, repo_name, ref, media_type, expected=200, method="GET"):
    get_headers = dict(headers)
    get_headers["Accept"] = media_type
    return protocol.conduct(
        session,
        method,
        "/v2/%s/manifests/%s" % (repo_name, ref),
        expected_status=expected,
        headers=get_headers,
    )


@pytest.fixture()
def default_manifest_sha512(liveserver, registry_server_executor, all_hash_algorithms):
    with ConfigChange(
        "DEFAULT_MANIFEST_HASH_ALGORITHM",
        "sha512",
        registry_server_executor.on(liveserver),
        liveserver,
    ):
        yield


@pytest.mark.parametrize("algorithm", ALL_HASH_ALGORITHMS)
@pytest.mark.parametrize("docker", [False, True])
def test_requested_digest_manifest_push_by_digest(
    algorithm, docker, v22_protocol, liveserver_session, all_hash_algorithms
):
    """Manifest path A: push by digest verifies the address algorithm, returns the requested
    digest, creates no client-visible tag, and is pullable by digest with exact bytes."""
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    config, layer = _push_image_blobs(
        v22_protocol, liveserver_session, headers, repo_name, algorithm, "by-digest"
    )
    media_type = DOCKER_V2_MANIFEST_TYPE if docker else OCI_MANIFEST_TYPE
    manifest_bytes = _image_manifest_bytes(config, [layer], docker=docker)
    digest = _alg_digest(algorithm, manifest_bytes)

    response = _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name, digest, manifest_bytes, media_type
    )
    assert response.headers["Docker-Content-Digest"] == digest
    assert response.headers["Location"].endswith("/v2/%s/manifests/%s" % (repo_name, digest))
    assert "OCI-Tag" not in response.headers

    for method in ("GET", "HEAD"):
        pulled = _get_manifest(
            v22_protocol, liveserver_session, headers, repo_name, digest, media_type,
            method=method,
        )
        assert pulled.headers["Docker-Content-Digest"] == digest
        if method == "GET":
            assert pulled.content == manifest_bytes

    tags = v22_protocol.conduct(
        liveserver_session, "GET", "/v2/%s/tags/list" % repo_name, headers=headers
    ).json()
    assert not tags.get("tags")

    if algorithm != "sha256":
        # No implicit SHA-256 manifest row.
        _get_manifest(
            v22_protocol, liveserver_session, headers, repo_name,
            _alg_digest("sha256", manifest_bytes), media_type, expected=404,
        )


@pytest.mark.parametrize("algorithm", ALL_HASH_ALGORITHMS)
def test_requested_digest_manifest_push_by_tag_uses_default(
    algorithm, v22_protocol, liveserver_session, all_hash_algorithms
):
    """Manifest path B: a push by tag hashes with DEFAULT_MANIFEST_HASH_ALGORITHM (sha256), even
    when its blob descriptors use another algorithm."""
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    config, layer = _push_image_blobs(
        v22_protocol, liveserver_session, headers, repo_name, algorithm, "by-tag"
    )
    manifest_bytes = _image_manifest_bytes(config, [layer])
    response = _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name, "latest", manifest_bytes,
        OCI_MANIFEST_TYPE,
    )
    expected = _alg_digest("sha256", manifest_bytes)
    assert response.headers["Docker-Content-Digest"] == expected

    pulled = _get_manifest(
        v22_protocol, liveserver_session, headers, repo_name, "latest", OCI_MANIFEST_TYPE
    )
    assert pulled.content == manifest_bytes
    assert pulled.headers["Docker-Content-Digest"] == expected
    # Layers resolve by their own (requested) digests.
    _assert_blob_served(
        v22_protocol, liveserver_session, headers, repo_name, layer["bytes"], layer["digest"]
    )


def test_requested_digest_manifest_push_by_tag_with_sha512_default(
    v22_protocol, liveserver_session, default_manifest_sha512
):
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    config, layer = _push_image_blobs(
        v22_protocol, liveserver_session, headers, repo_name, "sha256", "default512"
    )
    manifest_bytes = _image_manifest_bytes(config, [layer])
    response = _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name, "latest", manifest_bytes,
        OCI_MANIFEST_TYPE,
    )
    digest = _alg_digest("sha512", manifest_bytes)
    assert response.headers["Docker-Content-Digest"] == digest
    _get_manifest(v22_protocol, liveserver_session, headers, repo_name, digest, OCI_MANIFEST_TYPE)
    _get_manifest(
        v22_protocol, liveserver_session, headers, repo_name,
        _alg_digest("sha256", manifest_bytes), OCI_MANIFEST_TYPE, expected=404,
    )


@pytest.mark.parametrize("algorithm", ALL_HASH_ALGORITHMS)
def test_requested_digest_manifest_push_by_digest_with_tags(
    algorithm, v22_protocol, liveserver_session, all_hash_algorithms
):
    """Manifest path C: push by digest with repeated `tag` parameters assigns every tag and
    returns an OCI-Tag header for each."""
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    config, layer = _push_image_blobs(
        v22_protocol, liveserver_session, headers, repo_name, algorithm, "with-tags"
    )
    manifest_bytes = _image_manifest_bytes(config, [layer])
    digest = _alg_digest(algorithm, manifest_bytes)
    tags = ["latest", "1.0", "1"]

    response = _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name, digest, manifest_bytes,
        OCI_MANIFEST_TYPE, params={"tag": tags},
    )
    assert response.headers["Docker-Content-Digest"] == digest
    oci_tags = [t.strip() for value in response.raw.headers.getlist("OCI-Tag") for t in value.split(",")]
    assert oci_tags == tags

    for tag in tags:
        pulled = _get_manifest(
            v22_protocol, liveserver_session, headers, repo_name, tag, OCI_MANIFEST_TYPE
        )
        assert pulled.headers["Docker-Content-Digest"] == digest
        assert pulled.content == manifest_bytes


def test_requested_digest_tag_param_at_default_allowlist(
    v22_protocol, liveserver_session, app_reloader
):
    """`?tag=` works for SHA-256 with the default allowlist."""
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    config, layer = _push_image_blobs(
        v22_protocol, liveserver_session, headers, repo_name, "sha256", "default-tags"
    )
    manifest_bytes = _image_manifest_bytes(config, [layer])
    digest = _alg_digest("sha256", manifest_bytes)
    response = _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name, digest, manifest_bytes,
        OCI_MANIFEST_TYPE, params={"tag": ["a", "b"]},
    )
    assert response.raw.headers.getlist("OCI-Tag") == ["a", "b"]
    _get_manifest(v22_protocol, liveserver_session, headers, repo_name, "b", OCI_MANIFEST_TYPE)


def test_requested_digest_too_many_tags(v22_protocol, liveserver_session, all_hash_algorithms):
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    config, layer = _push_image_blobs(
        v22_protocol, liveserver_session, headers, repo_name, "sha512", "toomany"
    )
    manifest_bytes = _image_manifest_bytes(config, [layer])
    digest = _alg_digest("sha512", manifest_bytes)
    _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name, digest, manifest_bytes,
        OCI_MANIFEST_TYPE, params={"tag": ["t%d" % i for i in range(11)]}, expected=414,
    )
    # Nothing committed.
    _get_manifest(
        v22_protocol, liveserver_session, headers, repo_name, digest, OCI_MANIFEST_TYPE,
        expected=404,
    )
    _get_manifest(
        v22_protocol, liveserver_session, headers, repo_name, "t0", OCI_MANIFEST_TYPE,
        expected=404,
    )

    # Exactly 10 is accepted.
    _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name, digest, manifest_bytes,
        OCI_MANIFEST_TYPE, params={"tag": ["t%d" % i for i in range(10)]},
    )


@pytest.mark.parametrize("bad_tag", ["-leadingdash", "has space", "x" * 129, ".dot", ""])
def test_requested_digest_invalid_tag_name(
    bad_tag, v22_protocol, liveserver_session, all_hash_algorithms
):
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    config, layer = _push_image_blobs(
        v22_protocol, liveserver_session, headers, repo_name, "sha512", "badtag"
    )
    manifest_bytes = _image_manifest_bytes(config, [layer])
    digest = _alg_digest("sha512", manifest_bytes)
    response = _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name, digest, manifest_bytes,
        OCI_MANIFEST_TYPE, params={"tag": ["good", bad_tag]}, expected=400,
    )
    assert _error_code(response) == "TAG_INVALID"
    _get_manifest(
        v22_protocol, liveserver_session, headers, repo_name, "good", OCI_MANIFEST_TYPE,
        expected=404,
    )


def test_requested_digest_immutable_tag_is_all_or_nothing(
    v22_protocol, liveserver_session, liveserver, registry_server_executor, all_hash_algorithms
):
    """A `?tag=` push naming an immutable tag returns 409 TAG_IMMUTABLE and commits nothing:
    no manifest row and no other tag."""
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    config, layer = _push_image_blobs(
        v22_protocol, liveserver_session, headers, repo_name, "sha256", "immutable-base"
    )
    base_bytes = _image_manifest_bytes(config, [layer])
    _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name, "locked", base_bytes,
        OCI_MANIFEST_TYPE,
    )
    executor = registry_server_executor.on(liveserver)
    with FeatureFlagValue("IMMUTABLE_TAGS", True, executor):
        assert executor.make_tag_immutable("devtable", "newrepo", "locked").status_code == 200

        config2, layer2 = _push_image_blobs(
            v22_protocol, liveserver_session, headers, repo_name, "sha512", "immutable-new"
        )
        manifest_bytes = _image_manifest_bytes(config2, [layer2])
        digest = _alg_digest("sha512", manifest_bytes)
        response = _put_manifest(
            v22_protocol, liveserver_session, headers, repo_name, digest, manifest_bytes,
            OCI_MANIFEST_TYPE, params={"tag": ["fresh", "locked"]}, expected=409,
        )
        assert _error_code(response) == "TAG_IMMUTABLE"

    _get_manifest(
        v22_protocol, liveserver_session, headers, repo_name, "fresh", OCI_MANIFEST_TYPE,
        expected=404,
    )
    _get_manifest(
        v22_protocol, liveserver_session, headers, repo_name, digest, OCI_MANIFEST_TYPE,
        expected=404,
    )
    locked = _get_manifest(
        v22_protocol, liveserver_session, headers, repo_name, "locked", OCI_MANIFEST_TYPE
    )
    assert locked.content == base_bytes


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_requested_digest_manifest_put_disallowed_digest(
    algorithm, v22_protocol, liveserver_session, app_reloader
):
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    config, layer = _push_image_blobs(
        v22_protocol, liveserver_session, headers, repo_name, "sha256", "disallowed-manifest"
    )
    manifest_bytes = _image_manifest_bytes(config, [layer])
    response = _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name,
        _alg_digest(algorithm, manifest_bytes), manifest_bytes, OCI_MANIFEST_TYPE, expected=400,
    )
    assert _error_code(response) == "DIGEST_INVALID"


@pytest.mark.parametrize(
    "bad_ref", ["sha512:" + "A" * 128, "sha384:" + "a" * 95, "md5:" + "a" * 32, "sha1:" + "a" * 40]
)
def test_requested_digest_manifest_put_malformed_digest(
    bad_ref, v22_protocol, liveserver_session, all_hash_algorithms
):
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    response = _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name, bad_ref, b"{}",
        OCI_MANIFEST_TYPE, expected=400,
    )
    assert _error_code(response) == "DIGEST_INVALID"


def test_requested_digest_manifest_digest_mismatch(
    v22_protocol, liveserver_session, all_hash_algorithms
):
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    config, layer = _push_image_blobs(
        v22_protocol, liveserver_session, headers, repo_name, "sha512", "mismatch"
    )
    manifest_bytes = _image_manifest_bytes(config, [layer])
    wrong = _alg_digest("sha512", manifest_bytes + b" ")
    response = _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name, wrong, manifest_bytes,
        OCI_MANIFEST_TYPE, expected=400,
    )
    assert _error_code(response) == "MANIFEST_INVALID"


def test_requested_digest_schema1_push_by_non_sha256_digest(
    v22_protocol, liveserver_session, all_hash_algorithms
):
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    schema1 = _json.dumps({"schemaVersion": 1, "name": repo_name, "tag": "latest"}).encode()
    response = _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name, _alg_digest("sha512", schema1),
        schema1, "application/vnd.docker.distribution.manifest.v1+prettyjws", expected=400,
    )
    assert _error_code(response) == "MANIFEST_INVALID"


def test_requested_digest_manifest_references_unknown_blob(
    v22_protocol, liveserver_session, all_hash_algorithms
):
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    config, layer = _push_image_blobs(
        v22_protocol, liveserver_session, headers, repo_name, "sha512", "unknown-ref"
    )
    # The layer's SHA-256 identity was never published in this repository.
    sha256_of_layer = _alg_digest("sha256", layer["bytes"])
    manifest_bytes = _image_manifest_bytes(
        config, [{"digest": sha256_of_layer, "size": layer["size"]}]
    )
    response = _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name,
        _alg_digest("sha512", manifest_bytes), manifest_bytes, OCI_MANIFEST_TYPE, expected=400,
    )
    assert _error_code(response) == "MANIFEST_INVALID"


def test_requested_digest_manifest_references_disallowed_blob(
    v22_protocol, liveserver_session, liveserver, registry_server_executor, app_reloader
):
    """A manifest referencing a blob by a now-disallowed algorithm is rejected with 400
    MANIFEST_INVALID, even though the blob was published while allowed."""
    executor = registry_server_executor.on(liveserver)
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    with ConfigChange("ALLOWED_HASH_ALGORITHMS", ALL_HASH_ALGORITHMS, executor, liveserver):
        config, layer = _push_image_blobs(
            v22_protocol, liveserver_session, headers, repo_name, "sha512", "disabled-later"
        )

    manifest_bytes = _image_manifest_bytes(config, [layer])
    response = _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name, "latest", manifest_bytes,
        OCI_MANIFEST_TYPE, expected=400,
    )
    assert _error_code(response) == "MANIFEST_INVALID"


@pytest.mark.parametrize(
    "subject_digest, expected",
    [
        ("sha512:" + "c" * 128, 201),  # allowed, does not exist yet
        ("sha384:" + "b" * 96, 201),
        ("sha512:" + "C" * 128, 400),  # malformed
        ("md5:" + "a" * 32, 400),  # unsupported
    ],
)
def test_requested_digest_manifest_subject(
    subject_digest, expected, v22_protocol, liveserver_session, all_hash_algorithms
):
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    config, layer = _push_image_blobs(
        v22_protocol, liveserver_session, headers, repo_name, "sha512", "subject"
    )
    subject = {"mediaType": OCI_MANIFEST_TYPE, "digest": subject_digest, "size": 10}
    manifest_bytes = _image_manifest_bytes(config, [layer], subject=subject)
    response = _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name,
        _alg_digest("sha512", manifest_bytes), manifest_bytes, OCI_MANIFEST_TYPE,
        expected=expected,
    )
    if expected == 400:
        assert _error_code(response) == "MANIFEST_INVALID"


def test_requested_digest_subject_disallowed_at_default_allowlist(
    v22_protocol, liveserver_session, app_reloader
):
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    config, layer = _push_image_blobs(
        v22_protocol, liveserver_session, headers, repo_name, "sha256", "subject-default"
    )
    subject = {"mediaType": OCI_MANIFEST_TYPE, "digest": "sha512:" + "c" * 128, "size": 10}
    manifest_bytes = _image_manifest_bytes(config, [layer], subject=subject)
    response = _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name,
        _alg_digest("sha256", manifest_bytes), manifest_bytes, OCI_MANIFEST_TYPE, expected=400,
    )
    assert _error_code(response) == "MANIFEST_INVALID"


def test_requested_digest_index_with_mixed_children(
    v22_protocol, liveserver_session, all_hash_algorithms
):
    """An OCI index pushed by SHA-512 digest links SHA-512 and SHA-256 child manifests."""
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    children = []
    for arch, algorithm in (("amd64", "sha512"), ("arm64", "sha256")):
        config, layer = _push_image_blobs(
            v22_protocol, liveserver_session, headers, repo_name, algorithm, "index-" + arch
        )
        child_bytes = _image_manifest_bytes(config, [layer])
        child_digest = _alg_digest(algorithm, child_bytes)
        _put_manifest(
            v22_protocol, liveserver_session, headers, repo_name, child_digest, child_bytes,
            OCI_MANIFEST_TYPE,
        )
        children.append((arch, {"digest": child_digest, "size": len(child_bytes)}))

    index = _index_bytes(children)
    digest = _alg_digest("sha512", index)
    response = _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name, digest, index, OCI_INDEX_TYPE,
        params={"tag": ["multi"]},
    )
    assert response.headers["Docker-Content-Digest"] == digest
    pulled = _get_manifest(
        v22_protocol, liveserver_session, headers, repo_name, "multi", OCI_INDEX_TYPE
    )
    assert pulled.content == index
    for _, child in children:
        _get_manifest(
            v22_protocol, liveserver_session, headers, repo_name, child["digest"],
            OCI_MANIFEST_TYPE,
        )


def test_requested_digest_index_with_disallowed_child(
    v22_protocol, liveserver_session, app_reloader
):
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    index = _index_bytes([("amd64", {"digest": "sha512:" + "d" * 128, "size": 10})])
    response = _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name, _alg_digest("sha256", index),
        index, OCI_INDEX_TYPE, expected=400,
    )
    assert _error_code(response) == "MANIFEST_INVALID"


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_requested_digest_referrers(
    algorithm, v22_protocol, liveserver_session, all_hash_algorithms
):
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    config, layer = _push_image_blobs(
        v22_protocol, liveserver_session, headers, repo_name, algorithm, "referrers-subject"
    )
    subject_bytes = _image_manifest_bytes(config, [layer])
    subject_digest = _alg_digest(algorithm, subject_bytes)
    _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name, subject_digest, subject_bytes,
        OCI_MANIFEST_TYPE, params={"tag": ["subject"]},
    )

    config2, layer2 = _push_image_blobs(
        v22_protocol, liveserver_session, headers, repo_name, algorithm, "referrer"
    )
    referrer_bytes = _image_manifest_bytes(
        config2,
        [layer2],
        subject={
            "mediaType": OCI_MANIFEST_TYPE,
            "digest": subject_digest,
            "size": len(subject_bytes),
        },
    )
    referrer_digest = _alg_digest(algorithm, referrer_bytes)
    _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name, referrer_digest, referrer_bytes,
        OCI_MANIFEST_TYPE,
    )

    response = v22_protocol.conduct(
        liveserver_session,
        "GET",
        "/v2/%s/referrers/%s" % (repo_name, subject_digest),
        headers=headers,
    )
    assert [m["digest"] for m in response.json()["manifests"]] == [referrer_digest]


@pytest.mark.parametrize(
    "subject, fixture_allows",
    [
        ("sha512:" + "c" * 128, False),  # disallowed at the default allowlist
        ("sha512:" + "C" * 128, True),  # malformed
        ("md5:" + "a" * 32, True),  # unsupported
        ("sha512:" + "e" * 128, True),  # allowed but unknown
    ],
)
def test_requested_digest_referrers_rejections(
    subject, fixture_allows, v22_protocol, liveserver_session, liveserver,
    registry_server_executor, app_reloader,
):
    headers, repo_name = _push_headers(v22_protocol, liveserver_session, repo="simple")
    executor = registry_server_executor.on(liveserver)
    allowed = ALL_HASH_ALGORITHMS if fixture_allows else ["sha256"]
    with ConfigChange("ALLOWED_HASH_ALGORITHMS", allowed, executor, liveserver):
        response = v22_protocol.conduct(
            liveserver_session,
            "GET",
            "/v2/%s/referrers/%s" % (repo_name, subject),
            expected_status=400,
            headers=headers,
        )
    assert _error_code(response) == "MANIFEST_INVALID"


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_requested_digest_manifest_delete_disallowed(
    algorithm, v22_protocol, liveserver_session, liveserver, registry_server_executor,
    app_reloader,
):
    """A manifest DELETE by a disallowed digest returns 404 MANIFEST_UNKNOWN, even if the
    manifest exists."""
    executor = registry_server_executor.on(liveserver)
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    with ConfigChange("ALLOWED_HASH_ALGORITHMS", ALL_HASH_ALGORITHMS, executor, liveserver):
        config, layer = _push_image_blobs(
            v22_protocol, liveserver_session, headers, repo_name, algorithm, "delete"
        )
        manifest_bytes = _image_manifest_bytes(config, [layer])
        digest = _alg_digest(algorithm, manifest_bytes)
        _put_manifest(
            v22_protocol, liveserver_session, headers, repo_name, digest, manifest_bytes,
            OCI_MANIFEST_TYPE, params={"tag": ["keep"]},
        )

    response = v22_protocol.conduct(
        liveserver_session,
        "DELETE",
        "/v2/%s/manifests/%s" % (repo_name, digest),
        expected_status=404,
        headers=headers,
    )
    assert _error_code(response) == "MANIFEST_UNKNOWN"

    with ConfigChange("ALLOWED_HASH_ALGORITHMS", ALL_HASH_ALGORITHMS, executor, liveserver):
        v22_protocol.conduct(
            liveserver_session,
            "DELETE",
            "/v2/%s/manifests/%s" % (repo_name, digest),
            expected_status=202,
            headers=headers,
        )


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_requested_digest_no_schema1_conversion(
    algorithm, v22_protocol, liveserver_session, all_hash_algorithms
):
    """A tag pull that would need schema 1 conversion of non-SHA-256 content returns 404."""
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    config, layer = _push_image_blobs(
        v22_protocol, liveserver_session, headers, repo_name, algorithm, "schema1"
    )
    manifest_bytes = _image_manifest_bytes(config, [layer], docker=True)
    _put_manifest(
        v22_protocol, liveserver_session, headers, repo_name, "latest", manifest_bytes,
        DOCKER_V2_MANIFEST_TYPE,
    )
    response = _get_manifest(
        v22_protocol, liveserver_session, headers, repo_name, "latest",
        "application/vnd.docker.distribution.manifest.v1+prettyjws", expected=404,
    )
    assert _error_code(response) == "MANIFEST_UNKNOWN"


# ---------------------------------------------------------------------------------------------
# PROJQUAY-10117 Proposal 1: configurable digest algorithms (pull and /api/v1 gating).
# ---------------------------------------------------------------------------------------------


def _push_requested_image(protocol, session, headers, repo_name, algorithm, label, tag):
    config, layer = _push_image_blobs(protocol, session, headers, repo_name, algorithm, label)
    manifest_bytes = _image_manifest_bytes(config, [layer])
    digest = _alg_digest(algorithm, manifest_bytes)
    _put_manifest(
        protocol, session, headers, repo_name, digest, manifest_bytes, OCI_MANIFEST_TYPE,
        params={"tag": [tag]},
    )
    return digest, manifest_bytes, layer


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_requested_digest_pulls_gated_by_allowlist(
    algorithm, v22_protocol, liveserver_session, liveserver, registry_server_executor,
    app_reloader,
):
    """Reads by a disallowed algorithm return 404 BLOB_UNKNOWN / MANIFEST_UNKNOWN, including for
    content published while it was allowed and for tags resolving to such a manifest; enabling
    the algorithm again restores access."""
    executor = registry_server_executor.on(liveserver)
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)

    with ConfigChange("ALLOWED_HASH_ALGORITHMS", ALL_HASH_ALGORITHMS, executor, liveserver):
        digest, manifest_bytes, layer = _push_requested_image(
            v22_protocol, liveserver_session, headers, repo_name, algorithm, "gated", "gated"
        )
        # Warm any caches while allowed.
        _assert_blob_served(
            v22_protocol, liveserver_session, headers, repo_name, layer["bytes"], layer["digest"]
        )
        _get_manifest(v22_protocol, liveserver_session, headers, repo_name, digest, OCI_MANIFEST_TYPE)
        _get_manifest(v22_protocol, liveserver_session, headers, repo_name, "gated", OCI_MANIFEST_TYPE)

    with ConfigChange("ALLOWED_HASH_ALGORITHMS", ["sha256"], executor, liveserver):
        for method in ("GET", "HEAD"):
            response = v22_protocol.conduct(
                liveserver_session,
                method,
                "/v2/%s/blobs/%s" % (repo_name, layer["digest"]),
                expected_status=404,
                headers=headers,
            )
            if method == "GET":
                assert _error_code(response) == "BLOB_UNKNOWN"

            response = _get_manifest(
                v22_protocol, liveserver_session, headers, repo_name, digest,
                OCI_MANIFEST_TYPE, expected=404, method=method,
            )
            if method == "GET":
                assert _error_code(response) == "MANIFEST_UNKNOWN"

            response = _get_manifest(
                v22_protocol, liveserver_session, headers, repo_name, "gated",
                OCI_MANIFEST_TYPE, expected=404, method=method,
            )
            if method == "GET":
                assert _error_code(response) == "MANIFEST_UNKNOWN"

        # Listings still show the tag.
        tags = v22_protocol.conduct(
            liveserver_session, "GET", "/v2/%s/tags/list" % repo_name, headers=headers
        ).json()
        assert "gated" in tags["tags"]

    with ConfigChange("ALLOWED_HASH_ALGORITHMS", ALL_HASH_ALGORITHMS, executor, liveserver):
        pulled = _get_manifest(
            v22_protocol, liveserver_session, headers, repo_name, "gated", OCI_MANIFEST_TYPE
        )
        assert pulled.content == manifest_bytes
        assert pulled.headers["Docker-Content-Digest"] == digest


@pytest.mark.parametrize(
    "bad_ref", ["sha512:" + "A" * 128, "sha384:" + "a" * 95, "md5:" + "a" * 32, "sha1:" + "a" * 40]
)
def test_requested_digest_malformed_reads(
    bad_ref, v22_protocol, liveserver_session, all_hash_algorithms
):
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    for method in ("GET", "HEAD"):
        v22_protocol.conduct(
            liveserver_session, method, "/v2/%s/blobs/%s" % (repo_name, bad_ref),
            expected_status=404, headers=headers,
        )
        _get_manifest(
            v22_protocol, liveserver_session, headers, repo_name, bad_ref, OCI_MANIFEST_TYPE,
            expected=404, method=method,
        )


@pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
def test_requested_digest_api_v1_gated_by_allowlist(
    algorithm, v22_protocol, liveserver_session, liveserver, registry_server_executor,
    api_caller, app_reloader,
):
    """`/api/v1` endpoints addressed by a disallowed digest return 404; listings still show
    existing SHA-384/512 manifests and their digests."""
    executor = registry_server_executor.on(liveserver)
    headers, repo_name = _push_headers(v22_protocol, liveserver_session)
    api_caller.conduct_auth("devtable", "password")
    base = "/api/v1/repository/%s" % repo_name

    with ConfigChange("ALLOWED_HASH_ALGORITHMS", ALL_HASH_ALGORITHMS, executor, liveserver):
        digest, _, _ = _push_requested_image(
            v22_protocol, liveserver_session, headers, repo_name, algorithm, "api", "api"
        )
        other_digest, _, _ = _push_requested_image(
            v22_protocol, liveserver_session, headers, repo_name, "sha256", "api-other", "other"
        )
        assert api_caller.get("%s/manifest/%s" % (base, digest)).status_code == 200
        assert api_caller.get("%s/manifest/%s/labels" % (base, digest)).status_code == 200

    with ConfigChange("ALLOWED_HASH_ALGORITHMS", ["sha256"], executor, liveserver):
        assert api_caller.get("%s/manifest/%s" % (base, digest)).status_code == 404
        assert api_caller.get("%s/manifest/%s/labels" % (base, digest)).status_code == 404
        response = api_caller.post(
            "%s/manifest/%s/labels" % (base, digest),
            data=_json.dumps({"key": "k", "value": "v", "media_type": "text/plain"}),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 404
        assert api_caller.get("%s/manifest/%s/security" % (base, digest)).status_code == 404

        # Moving a tag to, or restoring a tag at, a disallowed digest is not found.
        response = api_caller.put(
            "%s/tag/other" % base,
            data=_json.dumps({"manifest_digest": digest}),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 404
        response = api_caller.post(
            "%s/tag/api/restore" % base,
            data=_json.dumps({"manifest_digest": digest}),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 404

        # Listings still show the SHA-384/512 manifest and digest.
        listing = api_caller.get("%s/tag/" % base).json()
        assert {t["name"]: t["manifest_digest"] for t in listing["tags"]}["api"] == digest

        # SHA-256 manifests are unaffected (positive control for the same endpoints).
        assert api_caller.get("%s/manifest/%s" % (base, other_digest)).status_code == 200
        response = api_caller.put(
            "%s/tag/moved" % base,
            data=_json.dumps({"manifest_digest": other_digest}),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code in (200, 201)
