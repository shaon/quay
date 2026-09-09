from __future__ import annotations

import json
import logging
from typing import Callable

from peewee import Select, fn

import features
from app import app, proxy_cache_blob_queue, storage
from data.database import (
    ImageStorage,
)
from data.database import Manifest as ManifestTable
from data.database import ManifestBlob, ManifestChild
from data.database import Tag as TagTable
from data.database import (
    db_disallow_replica_use,
    db_transaction,
    get_epoch_timestamp_ms,
)
from data.model import (
    ImmutableTagException,
    ManifestDoesNotExist,
    QuotaExceededException,
    RepositoryDoesNotExist,
    TagDoesNotExist,
    namespacequota,
    oci,
)
from data.model.oci.manifest import is_child_manifest
from data.model.proxy_cache import get_proxy_cache_config_for_org
from data.model.quota import (
    QuotaOperation,
    get_namespace_id_from_repository,
    is_blob_alive,
    update_quota,
)
from data.model.repository import create_repository, get_repository
from data.model.storage import (
    StorageContentStatus,
    get_layer_path,
    get_or_create_blob_with_lock,
    get_storage_content_status,
    with_blob_lock_or_fallback,
)
from data.registry_model.blobuploader import (
    BlobDigestMismatchException,
    BlobRangeMismatchException,
    BlobTooLargeException,
    BlobUploadException,
    BlobUploadSettings,
    complete_when_uploaded,
    create_blob_upload,
)
from data.registry_model.datatypes import Manifest, RepositoryReference, Tag
from data.registry_model.registry_oci_model import OCIModel
from digest import digest_tools
from image.docker.schema1 import (
    DOCKER_SCHEMA1_MANIFEST_CONTENT_TYPE,
    DOCKER_SCHEMA1_SIGNED_MANIFEST_CONTENT_TYPE,
    DockerSchema1Manifest,
)
from image.docker.schema2 import (
    DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
    DOCKER_SCHEMA2_MANIFESTLIST_CONTENT_TYPE,
)
from image.oci import OCI_IMAGE_INDEX_CONTENT_TYPE, OCI_IMAGE_MANIFEST_CONTENT_TYPE
from image.shared import ManifestException
from image.shared.interfaces import ManifestInterface
from image.shared.schemas import parse_manifest_from_bytes
from proxy import (
    Proxy,
    ProxyDigestDisabledError,
    ProxyDigestUnsupportedError,
    UpstreamAuthError,
    UpstreamManifestTooLargeError,
    UpstreamRegistryError,
)
from util.bytes import Bytes

logger = logging.getLogger(__name__)

MAX_PROXY_MANIFEST_SIZE_BYTES = 4 * 1024 * 1024


class ProxyManifestSizeExceeded(ManifestDoesNotExist):
    """Raised when an upstream manifest exceeds the proxy response limit."""

    def __init__(self, max_allowed):
        self.max_allowed = max_allowed
        super().__init__(f"upstream manifest exceeds response size limit ({max_allowed})")


ACCEPTED_MEDIA_TYPES = [
    OCI_IMAGE_MANIFEST_CONTENT_TYPE,
    OCI_IMAGE_INDEX_CONTENT_TYPE,
    DOCKER_SCHEMA2_MANIFESTLIST_CONTENT_TYPE,
    DOCKER_SCHEMA2_MANIFEST_CONTENT_TYPE,
    DOCKER_SCHEMA1_MANIFEST_CONTENT_TYPE,
    DOCKER_SCHEMA1_SIGNED_MANIFEST_CONTENT_TYPE,
]


class ProxyModel(OCIModel):
    def __init__(self, namespace_name, repo_name, user):
        super().__init__()
        self._config = get_proxy_cache_config_for_org(namespace_name)
        self._user = user
        self._namespace_name = namespace_name

        # when Quay is set up to proxy a whole upstream registry, the
        # upstream_registry_namespace for the proxy cache config will be empty.
        # the given repo then is expected to include both, the upstream namespace
        # and repo. Quay will treat it as a nested repo.
        target_ns = self._config.upstream_registry_namespace
        if target_ns != "" and target_ns is not None:
            repo_name = f"{target_ns}/{repo_name}"

        self._proxy = Proxy(self._config, repo_name)

    def lookup_repository(
        self,
        namespace_name,
        repo_name,
        kind_filter=None,
        raise_on_error=False,
        manifest_ref=None,
        model_cache=None,
    ):
        """
        Looks up and returns a reference to the repository with the given namespace and name, or
        None if none.

        If the repository does not exist and the given manifest_ref exists upstream,
        creates the repository.
        """
        if manifest_ref is not None and ":" in manifest_ref:
            self._parse_enabled_proxy_digest(manifest_ref)

        repo = get_repository(namespace_name, repo_name)
        exists = repo is not None
        if exists:
            return RepositoryReference.for_repo_obj(
                repo,
                namespace_name,
                repo_name,
                repo.namespace_user.stripe_id is None if repo else None,
                state=repo.state if repo is not None else None,
            )

        # we only create a repository for images that exist upstream, and if
        # we're not given a manifest reference then we can't check whether the
        # image exists upstream or not, so we refuse to create the repo.
        if manifest_ref is None:
            return None

        try:
            upstream_digest = self._proxy.manifest_exists(manifest_ref, ACCEPTED_MEDIA_TYPES)
            if upstream_digest is not None:
                self._parse_enabled_proxy_digest(upstream_digest)
        except UpstreamRegistryError as e:
            if raise_on_error:
                raise RepositoryDoesNotExist(str(e))
            return None

        visibility = "private" if app.config.get("CREATE_PRIVATE_REPO_ON_PUSH", True) else "public"

        repo = create_repository(
            namespace_name, repo_name, self._user, visibility=visibility, proxy_cache=True
        )
        return RepositoryReference.for_repo_obj(
            repo,
            namespace_name,
            repo_name,
            repo.namespace_user.stripe_id is None if repo else None,
            state=repo.state if repo is not None else None,
        )

    def _check_image_upload_possible_or_prune(
        self, repo_ref: RepositoryReference, upstream_manifest: ManifestInterface
    ) -> None:
        """
        Checks whether the given image fits within the quota size for the namespace
        the repository is part of. If it doesn't, it prunes older tags in the namespace
        by marking them expired which is eventually garbage collected by the gc worker.

        Raises QuotaExceededException if the given tag is larger than the max quota
        allotted for the namespace or if there are not enough tags to prune to free up space.
        """
        if upstream_manifest.is_manifest_list:
            return

        curr_ns_size = namespacequota.get_namespace_size(repo_ref.namespace_name)

        # if quota limit is not set for the namespace, skip auto pruning of images
        quotas = namespacequota.get_namespace_quota_list(repo_ref.namespace_name)
        if quotas:
            # currently only one quota per namespace is supported
            ns_quota_limit = quotas[0].limit_bytes
        else:
            logger.info("No quota configured")
            return

        image_size = upstream_manifest.layers_compressed_size
        if image_size > ns_quota_limit:
            raise QuotaExceededException

        if (curr_ns_size + image_size) <= ns_quota_limit:
            return

        reclaimable_size = 0
        namespace_id = get_namespace_id_from_repository(repo_ref.id)
        tags = oci.tag.get_tag_with_least_lifetime_end_for_ns(repo_ref.namespace_name)
        if tags is not None:
            for tag in tags:
                is_manifest_list = (
                    ManifestChild.select(1).where(ManifestChild.manifest == tag.manifest).exists()
                )

                # Get all the blobs under this manifest. If a manifest list get all the blobs
                # under the child manifests as well
                blobs = None
                if is_manifest_list:
                    blobs = (
                        ImageStorage.select(ImageStorage.id, ImageStorage.image_size)
                        .join(ManifestBlob, on=(ManifestBlob.blob == ImageStorage.id))
                        .join(
                            ManifestChild,
                            on=(ManifestChild.child_manifest == ManifestBlob.manifest),
                        )
                        .where(ManifestChild.manifest == tag.manifest)
                    )
                else:
                    blobs = (
                        ImageStorage.select(ImageStorage.id, ImageStorage.image_size)
                        .join(ManifestBlob, on=(ManifestBlob.blob == ImageStorage.id))
                        .where(ManifestBlob.manifest == tag.manifest)
                    )

                # We remove duplicates within the loop to prevent using "distinct" in the above query.
                # If the blob is not being referenced by an alive tag we'll get that size back
                # when it's GC'd, so add it to the reclaimable total.
                seen_blobs = []
                for blob in blobs:
                    if blob.id not in seen_blobs and not is_blob_alive(
                        namespace_id, tag.id, blob.id
                    ):
                        size = blob.image_size if blob.image_size is not None else 0
                        reclaimable_size = reclaimable_size + size
                    seen_blobs.append(blob.id)

                updated = oci.tag.remove_tag_from_timemachine(
                    tag.repository_id,
                    tag.name,
                    tag.manifest,
                    include_submanifests=is_manifest_list,
                    is_alive=True,
                )

                # If we get enough size back from deleting this tag, exit
                if updated and reclaimable_size > image_size:
                    return

        # if we got here, then there aren't enough tags in the namespace to expire, so we raise an exception
        raise QuotaExceededException

    def lookup_manifest_by_digest(
        self,
        repository_ref,
        manifest_digest,
        allow_dead=False,
        allow_hidden=False,
        require_available=False,
        raise_on_error=True,
    ):
        """
        Looks up the manifest with the given digest under the given repository and returns it or
        None if none.

        If a manifest with the digest does not exist, fetches the manifest upstream
        and creates it with a temp tag.

        Raises QuotaExceededException if the given tag is larger than the max quota
        allotted for the namespace or if there are not enough tags to prune.
        """

        self._parse_enabled_proxy_digest(manifest_digest)

        wrapped_manifest = super().lookup_manifest_by_digest(
            repository_ref, manifest_digest, allow_dead=True, require_available=False
        )
        if wrapped_manifest is None:
            try:
                wrapped_manifest, _ = self._create_and_tag_manifest(
                    repository_ref, manifest_digest, self._create_manifest_with_temp_tag
                )
            except (UpstreamRegistryError, ManifestDoesNotExist) as e:
                raise ManifestDoesNotExist(str(e))
            return wrapped_manifest

        db_tag = oci.tag.get_tag_by_manifest_id(repository_ref.id, wrapped_manifest.id)
        if db_tag is None:
            oci.manifest.lookup_manifest(
                repository_ref.id, manifest_digest, allow_dead=True, require_available=True
            )
            db_tag = oci.tag.get_tag_by_manifest_id(repository_ref.id, wrapped_manifest.id)
        existing_tag = Tag.for_tag(
            db_tag, self._legacy_image_id_handler, manifest_row=db_tag.manifest
        )
        new_tag = False
        try:
            tag, new_tag = self._update_manifest_for_tag(
                repository_ref,
                existing_tag,
                existing_tag.manifest,
                manifest_digest,
                self._create_manifest_with_temp_tag,
            )
        except ManifestDoesNotExist:
            raise
        except UpstreamAuthError:
            raise ManifestDoesNotExist("upstream authentication failed")
        except UpstreamRegistryError:
            # when the upstream fetch fails, we only return the tag if
            # it isn't yet expired. note that we don't bump the tag's
            # expiration here either - we only do this when we can ensure
            # the tag exists upstream.
            isplaceholder = wrapped_manifest.internal_manifest_bytes.as_unicode() == ""
            return wrapped_manifest if not existing_tag.expired and not isplaceholder else None

        if tag.expired or not new_tag:
            with db_disallow_replica_use():
                new_expiration = (
                    get_epoch_timestamp_ms() + self._config.expiration_s * 1000
                    if self._config.expiration_s
                    else None
                )
                oci.tag.set_tag_end_ms(db_tag, new_expiration)
                # if the manifest is a child of a manifest list in this repo, renew
                # the parent(s) manifest list tag too.
                # select tag ids by most recent lifetime_end_ms uniquely by name,
                # based on the link between sub-manifest and parent manifest in the
                # manifest link.
                q = (
                    TagTable.select(fn.MAX(TagTable.id).alias("id"))
                    .join(ManifestChild, on=(TagTable.manifest_id == ManifestChild.manifest_id))
                    .where(
                        (ManifestChild.repository_id == repository_ref.id)
                        & (ManifestChild.child_manifest_id == wrapped_manifest.id)
                    )
                    .group_by(TagTable.name)
                )
                tag_ids = [item for item in q]
                TagTable.update(lifetime_end_ms=new_expiration).where(
                    TagTable.id.in_(tag_ids)
                ).execute()

        return super().lookup_manifest_by_digest(
            repository_ref,
            manifest_digest,
            allow_dead=True,
            allow_hidden=True,
            require_available=False,
            raise_on_error=True,
        )

    def get_repo_tag(self, repository_ref, tag_name, raise_on_error=True):
        """
        Returns the latest, *active* tag found in the repository, with the matching
        name or None if none.

        If both manifest and tag don't exist, fetches the manifest with the tag
        from upstream, and creates them both.
        If tag and manifest exists and the manifest is a placeholder, pull the
        upstream manifest and save it locally.

        Raises QuotaExceededException if the given tag is larger than the max quota
        allotted for the namespace or if there are not enough tags to prune.
        """
        db_tag = oci.tag.get_current_tag(repository_ref.id, tag_name)
        existing_tag = Tag.for_tag(db_tag, self._legacy_image_id_handler)
        if existing_tag is None:
            try:
                _, tag = self._create_and_tag_manifest(
                    repository_ref, tag_name, self._create_manifest_and_retarget_tag
                )
            except (UpstreamRegistryError, ManifestDoesNotExist) as e:
                raise TagDoesNotExist(str(e))
            return tag

        new_tag = False
        try:
            tag, new_tag = self._update_manifest_for_tag(
                repository_ref,
                existing_tag,
                existing_tag.manifest,
                tag_name,
                self._create_manifest_and_retarget_tag,
            )
        except ManifestDoesNotExist as e:
            raise TagDoesNotExist(str(e))
        except UpstreamAuthError as e:
            raise TagDoesNotExist(str(e))
        except UpstreamRegistryError:
            # when the upstream fetch fails, we only return the tag if
            # it isn't yet expired. note that we don't bump the tag's
            # expiration here either - we only do this when we can ensure
            # the tag exists upstream.
            isplaceholder = existing_tag.manifest.internal_manifest_bytes.as_unicode() == ""
            return existing_tag if not existing_tag.expired and not isplaceholder else None

        # always bump tag expiration when retrieving tags that both are cached
        # and exist upstream, as a means to auto-renew the cache.
        if tag.expired or not new_tag:
            with db_disallow_replica_use():
                new_expiration = (
                    get_epoch_timestamp_ms() + self._config.expiration_s * 1000
                    if self._config.expiration_s
                    else None
                )
                oci.tag.set_tag_end_ms(db_tag, new_expiration)
            return super().get_repo_tag(repository_ref, tag_name, raise_on_error=True)

        return tag

    def _create_and_tag_manifest(
        self,
        repo_ref: RepositoryReference,
        manifest_ref: str,
        create_manifest_fn: Callable[
            [RepositoryReference, ManifestInterface, str | None], tuple[Manifest | None, Tag | None]
        ],
    ) -> tuple[Manifest | None, Tag | None]:
        """
        Returns the newly created SHA-256 manifest and tag.

        Alternative upstream identities are rejected before manifest download or persistence.
        """
        if ":" in manifest_ref:
            self._parse_enabled_proxy_digest(manifest_ref)

        upstream_digest = self._proxy.manifest_exists(manifest_ref, ACCEPTED_MEDIA_TYPES)
        if upstream_digest is not None:
            self._parse_enabled_proxy_digest(upstream_digest)
        requested_digest = manifest_ref if ":" in manifest_ref else upstream_digest
        upstream_manifest = self._pull_upstream_manifest(
            repo_ref.name,
            manifest_ref,
            expected_digest=requested_digest,
        )
        return create_manifest_fn(repo_ref, upstream_manifest, manifest_ref)

    def _parse_enabled_proxy_digest(self, digest):
        try:
            parsed = digest_tools.Digest.parse_digest(digest, strict=True)
        except digest_tools.UnsupportedDigestAlgorithmException as exc:
            algorithm = digest.split(":", 1)[0] if isinstance(digest, str) else str(digest)
            raise ProxyDigestUnsupportedError(algorithm) from exc
        except digest_tools.InvalidDigestException as exc:
            raise ManifestDoesNotExist("invalid upstream digest") from exc

        # Alternative-digest proxy-cache ingestion is deferred to Demo 9. This capability
        # boundary takes precedence over the global allowlist, just like mirror and import.
        if parsed.hash_alg != "sha256":
            raise ProxyDigestUnsupportedError(parsed.hash_alg)
        if parsed.hash_alg not in app.config.get("ALLOWED_HASH_ALGORITHMS", ["sha256"]):
            raise ProxyDigestDisabledError(parsed.hash_alg)
        return parsed

    def _validate_proxy_manifest_digests(self, manifest):
        digests = [str(digest) for digest in manifest.blob_digests or []]
        if manifest.subject is not None:
            digests.append(
                manifest.subject.get("digest")
                if isinstance(manifest.subject, dict)
                else manifest.subject.digest
            )
        if manifest.is_manifest_list:
            digests.extend(
                descriptor.get("digest")
                for descriptor in manifest.manifest_dict.get("manifests", [])
            )

        for digest in digests:
            self._parse_enabled_proxy_digest(digest)

    def _rollback_created_blobs_and_quota(self, repo_ref, manifest, created_blobs):
        """
        Rolls back the created blobs and quota for a specified manifest and repository
        in case an error occurs.
        """
        blob_ids = [blob_id for blob_id, _ in created_blobs]
        ManifestBlob.delete().where(
            ManifestBlob.manifest == manifest.id, ManifestBlob.blob << blob_ids
        ).execute()

        # Rollback quota for the blobs we added
        blob_sizes = {blob_id: size for blob_id, size in created_blobs}
        update_quota(repo_ref.id, manifest.id, blob_sizes, QuotaOperation.SUBTRACT)

    def _update_manifest_for_tag(
        self,
        repo_ref: RepositoryReference,
        tag: Tag,
        manifest: Manifest,
        manifest_ref: str,
        create_manifest_fn,
    ) -> tuple[Tag, bool]:
        """
        Updates a placeholder manifest with the given tag name.

        If the manifest is stale, downloads it from the upstream registry
        and creates a new manifest and retargets the tag.

        A manifest is considered stale when the manifest's digest changed in
        the upstream registry.
        A manifest is considered a placeholder when its db entry exists, but
        its manifest_bytes field is empty.

        Raises UpstreamRegistryError if the upstream registry returns anything
        other than 200.
        Raises ManifestDoesNotExist if the given manifest was not found in the
        database.

        Returns a new tag if one was created, or the existing one with a manifest
        freshly out of the database, and a boolean indicating whether the returned
        tag was newly created or not.
        """
        upstream_manifest = None
        upstream_digest = self._proxy.manifest_exists(manifest_ref, ACCEPTED_MEDIA_TYPES)

        up_to_date = False
        if upstream_digest:
            parsed_upstream_digest = self._parse_enabled_proxy_digest(upstream_digest)
            up_to_date = manifest.digest == str(parsed_upstream_digest)
        else:
            upstream_manifest = self._pull_upstream_manifest(repo_ref.name, manifest_ref)
            upstream_digest = upstream_manifest.digest
            up_to_date = manifest.digest == upstream_digest

        logger.debug(f"Found upstream manifest with digest {upstream_digest}, {manifest_ref=}")
        placeholder = manifest.internal_manifest_bytes.as_unicode() == ""
        if up_to_date and not placeholder:
            if tag.expired:
                if upstream_manifest is None:
                    upstream_manifest = self._pull_upstream_manifest(
                        repo_ref.name,
                        manifest_ref,
                        expected_digest=upstream_digest,
                    )
                self._check_image_upload_possible_or_prune(repo_ref, upstream_manifest)
            return tag, False

        if upstream_manifest is None:
            upstream_manifest = self._pull_upstream_manifest(
                repo_ref.name,
                manifest_ref,
                expected_digest=upstream_digest,
            )

        created_blobs = []

        if up_to_date and placeholder:
            self._check_image_upload_possible_or_prune(repo_ref, upstream_manifest)
            # We try to create placeholder blobs and update the manifest
            try:
                created_blobs = self._create_placeholder_blobs(
                    upstream_manifest, manifest.id, repo_ref.id
                )
                with db_disallow_replica_use():
                    with db_transaction():
                        q = ManifestTable.update(
                            manifest_bytes=upstream_manifest.bytes.as_unicode(),
                            layers_compressed_size=upstream_manifest.layers_compressed_size,
                        ).where(ManifestTable.id == manifest.id)
                        q.execute()
                        db_tag = oci.tag.get_tag_by_manifest_id(repo_ref.id, manifest.id)
                        return Tag.for_tag(db_tag, self._legacy_image_id_handler), False

            # If manifest update fails, orphan the already created blobs so they can be picked up
            # by gc
            except Exception as e:
                logger.warning(
                    "Failed to update manifest %s, cleaning up blob references", manifest.id
                )
                # only delete newly created manifest blobs
                if created_blobs:
                    self._rollback_created_blobs_and_quota(repo_ref, manifest, created_blobs)
                raise

        # If we got here, the manifest is stale, so create it and retarget the tag.
        _, tag = create_manifest_fn(repo_ref, upstream_manifest, manifest_ref)
        return tag, True

    def _create_manifest_and_retarget_tag(
        self, repository_ref: RepositoryReference, manifest: ManifestInterface, tag_name: str
    ) -> tuple[Manifest | None, Tag | None]:
        """
        Creates a manifest in the given repository.

        Also checks whether the given image size is within the quota limit
        of the namespace the repository is part of. If not, it prunes older tags.
        Raises QuotaExceededException if there are not enough tags to prune.

        Also creates placeholders for the objects referenced by the manifest.
        For manifest lists, creates placeholder sub-manifests. For regular
        manifests, creates placeholder blobs.

        Placeholder objects will be "filled" with the objects' contents on
        upcoming client requests, as part of the flow described in the OCI
        distribution specification.

        Returns a reference to the (created manifest, tag) or (None, None) on error.
        """
        self._check_image_upload_possible_or_prune(repository_ref, manifest)

        db_manifest = oci.manifest.lookup_canonical_manifest(
            repository_ref.id, manifest.digest, allow_dead=True
        )

        if db_manifest is None:
            with db_disallow_replica_use(), db_transaction():
                canonical_subject = oci.manifest.resolve_manifest_subject(
                    repository_ref.id, manifest
                )
                db_manifest = oci.manifest.create_manifest(
                    repository_ref.id,
                    manifest,
                    raise_on_error=True,
                    canonical_subject_digest=(
                        canonical_subject.digest if canonical_subject else None
                    ),
                )
            if db_manifest is None:
                return None, None

        created_blobs = []  # Track ManifestBlob rows created in this attempt
        try:
            # Check if the tag is immutable and if it is, return it immediately
            existing_tag = oci.tag.get_tag(repository_ref.id, tag_name)
            if existing_tag and existing_tag.immutable:
                wrapped_manifest = Manifest.for_manifest(
                    existing_tag.manifest, self._legacy_image_id_handler
                )
                wrapped_tag = Tag.for_tag(
                    existing_tag,
                    self._legacy_image_id_handler,
                    manifest_row=existing_tag.manifest,
                )
                return wrapped_manifest, wrapped_tag

            if not manifest.is_manifest_list:
                created_blobs = self._create_placeholder_blobs(
                    manifest, db_manifest.id, repository_ref.id
                )

            with db_disallow_replica_use():
                with db_transaction():
                    # 0 means a tag never expires - if we get 0 as expiration,
                    # we set the tag expiration to None.
                    expiration = self._config.expiration_s or None
                    try:
                        tag = oci.tag.retarget_tag(
                            tag_name,
                            db_manifest,
                            raise_on_error=True,
                            expiration_seconds=expiration,
                            track_repo_modification=False,
                        )
                    except ImmutableTagException:
                        raise
                    if tag is None:
                        return None, None

                    wrapped_manifest = Manifest.for_manifest(
                        db_manifest, self._legacy_image_id_handler
                    )
                    wrapped_tag = Tag.for_tag(
                        tag, self._legacy_image_id_handler, manifest_row=db_manifest
                    )

                    if not manifest.is_manifest_list:
                        oci.manifest.register_repository_manifest_digest(
                            repository_ref.id, db_manifest, manifest.digest
                        )
                    else:
                        child_references = list(manifest.child_manifests(content_retriever=None))
                        child_descriptors = manifest.manifest_dict.get("manifests", [])
                        if len(child_references) != len(child_descriptors):
                            raise ManifestDoesNotExist(
                                "upstream index descriptors could not be resolved"
                            )

                        manifests_to_connect = []
                        for child in child_references:
                            m = oci.manifest.lookup_manifest(
                                repository_ref.id, child.digest, allow_dead=True
                            )
                            if m is None:
                                m = oci.manifest.create_manifest(repository_ref.id, child)
                                oci.tag.create_temporary_tag_if_necessary(
                                    m, self._config.expiration_s or None
                                )
                            try:
                                ManifestChild.get(manifest=db_manifest.id, child_manifest=m.id)
                            except ManifestChild.DoesNotExist:
                                manifests_to_connect.append(m)

                        oci.manifest.connect_manifests(
                            manifests_to_connect, db_manifest, repository_ref.id
                        )
                        oci.manifest.register_repository_manifest_digest(
                            repository_ref.id, db_manifest, manifest.digest
                        )

            oci.tag.mark_repository_modified(repository_ref.namespace_name, repository_ref.name)
            return wrapped_manifest, wrapped_tag
        except Exception as e:
            logger.warning(
                "Failed to create blob/tag for manifest %s, cleaning up: %s", db_manifest.id, e
            )
            # Clean up only the ManifestBlob rows we created in this attempt
            if created_blobs:
                self._rollback_created_blobs_and_quota(repository_ref, db_manifest, created_blobs)
            raise

    def _create_manifest_with_temp_tag(
        self,
        repository_ref: RepositoryReference,
        manifest: ManifestInterface,
        manifest_ref: str | None = None,
    ) -> tuple[Manifest | None, Tag | None]:
        """
        Creates a manifest in the given repository. Also creates placeholders for the
        objects referenced by the manifest. For manifest lists, it creates
        sub manifests entries attached to the manifest list along with a temporary tag.

        Also checks whether the given image size is within the quota limit
        of the namespace the repository is part of. If not, it prunes older tags.
        Raises QuotaExceededException if there are not enough tags to prune.
        """
        self._check_image_upload_possible_or_prune(repository_ref, manifest)
        requested_digest = manifest_ref or manifest.digest
        self._parse_enabled_proxy_digest(requested_digest)
        if ":" in requested_digest:
            try:
                parsed_requested_digest = digest_tools.Digest.parse_digest(
                    requested_digest, strict=True
                )
            except digest_tools.InvalidDigestException as exc:
                raise ManifestDoesNotExist("invalid upstream manifest digest") from exc
            computed_digest = (
                manifest.digest
                if manifest.schema_version == 1
                else digest_tools.digest_bytes(
                    parsed_requested_digest.hash_alg,
                    manifest.bytes.as_encoded_str(),
                )
            )
            if computed_digest != requested_digest:
                raise ManifestDoesNotExist("upstream manifest digest mismatch")

        if manifest.is_manifest_list:
            return self._create_proxy_manifest_list_with_temp_tag(
                repository_ref,
                manifest,
                requested_digest,
            )

        db_manifest = oci.manifest.lookup_canonical_manifest(
            repository_ref.id, manifest.digest, allow_dead=True
        )
        if db_manifest is None:
            with db_disallow_replica_use(), db_transaction():
                canonical_subject = oci.manifest.resolve_manifest_subject(
                    repository_ref.id, manifest
                )
                db_manifest = oci.manifest.create_manifest(
                    repository_ref.id,
                    manifest,
                    canonical_subject_digest=(
                        canonical_subject.digest if canonical_subject else None
                    ),
                )

        created_blobs = []  # Track ManifestBlob rows created in this attempt
        try:
            created_blobs = self._create_placeholder_blobs(
                manifest, db_manifest.id, repository_ref.id
            )
            with db_disallow_replica_use(), db_transaction():
                expiration = self._config.expiration_s or None
                tag = Tag.for_tag(
                    oci.tag.create_temporary_tag_if_necessary(db_manifest, expiration),
                    self._legacy_image_id_handler,
                )
                wrapped_manifest = Manifest.for_manifest(db_manifest, self._legacy_image_id_handler)

                oci.manifest.register_repository_manifest_digest(
                    repository_ref.id, db_manifest, requested_digest
                )
                return wrapped_manifest, tag
        except Exception as e:
            logger.warning(
                "Failed to create blob/tag for manifest %s, cleaning up: %s", db_manifest.id, e
            )
            if created_blobs:
                self._rollback_created_blobs_and_quota(repository_ref, db_manifest, created_blobs)
            raise

    def _create_proxy_manifest_list_with_temp_tag(
        self,
        repository_ref,
        manifest,
        requested_digest,
    ):
        child_rows = []
        child_references = list(manifest.child_manifests(content_retriever=None))
        child_descriptors = manifest.manifest_dict.get("manifests", [])
        if len(child_references) != len(child_descriptors):
            raise ManifestDoesNotExist("upstream index descriptors could not be resolved")

        for child in child_references:
            child_row = oci.manifest.lookup_manifest(
                repository_ref.id,
                child.digest,
                allow_hidden=True,
                allow_dead=True,
            )
            child_rows.append((child, child_row))

        with db_disallow_replica_use(), db_transaction():
            db_manifest = oci.manifest.lookup_canonical_manifest(
                repository_ref.id, manifest.digest, allow_dead=True
            )
            if db_manifest is None:
                canonical_subject = oci.manifest.resolve_manifest_subject(
                    repository_ref.id, manifest
                )
                db_manifest = oci.manifest.create_manifest(
                    repository_ref.id,
                    manifest,
                    canonical_subject_digest=(
                        canonical_subject.digest if canonical_subject else None
                    ),
                )

            manifests_to_connect = []
            expiration = self._config.expiration_s or None
            for child, child_row in child_rows:
                if child_row is None:
                    child_row = oci.manifest.create_manifest(repository_ref.id, child)
                manifests_to_connect.append(child_row)
                oci.tag.create_temporary_tag_if_necessary(child_row, expiration)

            existing_child_ids = {
                relationship.child_manifest_id
                for relationship in ManifestChild.select(ManifestChild.child_manifest).where(
                    ManifestChild.repository == repository_ref.id,
                    ManifestChild.manifest == db_manifest,
                )
            }
            oci.manifest.connect_manifests(
                [child for child in manifests_to_connect if child.id not in existing_child_ids],
                db_manifest,
                repository_ref.id,
            )
            tag = Tag.for_tag(
                oci.tag.create_temporary_tag_if_necessary(db_manifest, expiration),
                self._legacy_image_id_handler,
            )
            oci.manifest.register_repository_manifest_digest(
                repository_ref.id, db_manifest, requested_digest
            )
            return (
                Manifest.for_manifest(db_manifest, self._legacy_image_id_handler),
                tag,
            )

    def get_repo_blob_by_digest(self, repository_ref, blob_digest, include_placements=False):
        """
        Returns the repository-visible SHA-256 blob and fills a missing placement from upstream.
        """
        self._parse_enabled_proxy_digest(blob_digest)
        blob = oci.blob.lookup_repository_blob_by_digest(repository_ref.id, blob_digest)
        if blob is None:
            return None

        content_status = get_storage_content_status(blob, storage)
        if content_status != StorageContentStatus.READABLE:
            logger.warning(
                "Proxy blob %s requires promotion or repair: %s",
                blob_digest,
                content_status.value,
            )
            try:
                self._download_blob(repository_ref, blob_digest)
            except BlobDigestMismatchException:
                raise UpstreamRegistryError("blob digest mismatch")
            except BlobTooLargeException as e:
                raise UpstreamRegistryError(f"blob too large, max allowed is {e.max_allowed}")
            except BlobRangeMismatchException:
                raise UpstreamRegistryError("range mismatch")
            except BlobUploadException:
                raise UpstreamRegistryError("invalid blob upload")

        return super().get_repo_blob_by_digest(repository_ref, blob_digest, include_placements)

    def _prefetch_blob(self, repo_ref: RepositoryReference, digest: str):
        """Download, validate, and finalize bytes without repository registration."""
        parsed_digest = self._parse_enabled_proxy_digest(digest)
        expiration = (
            self._config.expiration_s
            if self._config.expiration_s
            else app.config["PUSH_TEMP_TAG_EXPIRATION_SEC"]
        )
        settings = BlobUploadSettings(
            maximum_blob_size=app.config["MAXIMUM_LAYER_SIZE"],
            committed_blob_expiration=expiration,
        )
        uploader = create_blob_upload(
            repo_ref,
            storage,
            settings,
            requested_digest_algorithm=parsed_digest.hash_alg,
        )
        if uploader is None:
            raise BlobUploadException("could not create proxy blob upload")
        try:
            with self._proxy.get_blob(digest) as resp:
                start_offset = 0
                length = int(resp.headers.get("content-length", -1))
                uploader.prepare_for_digest(parsed_digest)
                uploader.upload_chunk(app.config, resp.raw, start_offset, length)
                uploader.prefetch_to_storage(app.config, parsed_digest)
            return uploader
        except Exception:
            uploader.cancel_upload()
            raise

    def _download_blob(self, repo_ref: RepositoryReference, digest: str) -> None:
        """Download, validate, and repository-register a blob under its upstream identity."""
        uploader = self._prefetch_blob(repo_ref, digest)
        with complete_when_uploaded(uploader):
            committed = uploader.commit_prefetched_blob(digest)
            if committed is None:
                raise BlobUploadException("could not commit proxy blob upload")

    def convert_manifest(
        self,
        manifest,
        namespace_name,
        repo_name,
        tag_name,
        allowed_mediatypes,
        storage,
    ):
        return None

    def get_schema1_parsed_manifest(
        self, manifest, namespace_name, repo_name, tag_name, storage, raise_on_error=False
    ):
        if raise_on_error:
            raise ManifestException("manifest is not acceptable by the client")
        return None

    def _create_blob_in_storage(
        self, digest: str, size: int, manifest_id: int, repo_id: int, skip_lock: bool
    ):
        blob = get_or_create_blob_with_lock(digest=digest, image_size=size, skip_lock=skip_lock)
        newly_created = False
        try:
            ManifestBlob.get(manifest_id=manifest_id, blob=blob, repository_id=repo_id)
        except ManifestBlob.DoesNotExist:
            ManifestBlob.create(manifest_id=manifest_id, blob=blob, repository_id=repo_id)
            newly_created = True

            # Add blob sizes if quota management is enabled
            update_quota(repo_id, manifest_id, {blob.id: blob.image_size}, QuotaOperation.ADD)
        return blob, newly_created

    def _create_blob(self, digest: str, size: int, manifest_id: int, repo_id: int):
        return with_blob_lock_or_fallback(
            digest,
            self._create_blob_in_storage,
            digest,
            size,
            manifest_id,
            repo_id,
        )

    def _create_placeholder_blobs(
        self, manifest: ManifestInterface, manifest_id: int, repo_id: int
    ):
        """
        Creates placeholder blobs for the manifest and returns a list of newly created blob IDs with sizes.

        Returns:
            List of tuples (blob_id, size) for ManifestBlob rows created in this call.
        """
        if manifest.is_manifest_list:
            return []

        created_blobs = []  # Track (blob_id, size) for newly created ManifestBlob rows

        if manifest.schema_version == 2:
            blob, newly_created = self._create_blob(
                manifest.config.digest,
                manifest.config.size,
                manifest_id,
                repo_id,
            )
            if newly_created:
                created_blobs.append((blob.id, blob.image_size))

        for layer in manifest.filesystem_layers:
            blob, newly_created = self._create_blob(
                layer.digest, layer.compressed_size, manifest_id, repo_id
            )
            if newly_created:
                created_blobs.append((blob.id, blob.image_size))

            username = self._user.username if self._user else None
            queue_id = proxy_cache_blob_queue.put(
                [self._namespace_name, str(repo_id), str(layer.digest)],
                json.dumps(
                    {
                        "digest": str(layer.digest),
                        "repo_id": repo_id,
                        "username": username,
                        "namespace": self._namespace_name,
                    }
                ),
                available_after=5,
            )

        return created_blobs

    def _upstream_namespace(self, repo: str) -> str:
        upstream_namespace = self._config.upstream_registry_namespace
        if upstream_namespace is None:
            parts = repo.split("/")
            upstream_namespace = parts[0]
        return upstream_namespace

    def _upstream_repo(self, repo: str) -> str:
        upstream_repo_name = repo
        if self._config.upstream_registry_namespace is None:
            parts = repo.split("/")
            if len(parts) == 1:
                return repo
            upstream_repo_name = parts[1]
        return upstream_repo_name

    def _pull_upstream_manifest(
        self,
        repo: str,
        manifest_ref: str,
        expected_digest: str | None = None,
    ) -> ManifestInterface:
        if expected_digest is not None:
            self._parse_enabled_proxy_digest(expected_digest)

        try:
            raw_manifest, content_type = self._proxy.get_manifest(
                manifest_ref,
                ACCEPTED_MEDIA_TYPES,
                max_bytes=MAX_PROXY_MANIFEST_SIZE_BYTES,
            )
        except UpstreamAuthError:
            raise
        except UpstreamManifestTooLargeError as e:
            raise ProxyManifestSizeExceeded(e.max_bytes) from e
        except UpstreamRegistryError as e:
            if e.status_code == 404:
                raise ManifestDoesNotExist(str(e))
            raise

        if len(raw_manifest) > MAX_PROXY_MANIFEST_SIZE_BYTES:
            # Keep the model boundary fail-closed for mocked or alternate Proxy implementations
            # that return a materialized body instead of honoring Proxy.get_manifest's stream cap.
            raise ProxyManifestSizeExceeded(MAX_PROXY_MANIFEST_SIZE_BYTES)

        upstream_repo_name = self._upstream_repo(repo)
        upstream_namespace = self._upstream_namespace(repo)

        # TODO: do we need the compatibility check from v2._parse_manifest?
        mbytes = Bytes.for_string_or_unicode(raw_manifest)
        manifest = parse_manifest_from_bytes(mbytes, content_type, sparse_manifest_support=True)
        self._validate_proxy_manifest_digests(manifest)
        if expected_digest is not None:
            parsed_expected = self._parse_enabled_proxy_digest(expected_digest)
            computed_digest = (
                manifest.digest
                if manifest.schema_version == 1
                else digest_tools.digest_bytes(
                    parsed_expected.hash_alg,
                    mbytes.as_encoded_str(),
                )
            )
            if computed_digest != str(parsed_expected):
                raise ManifestDoesNotExist("upstream manifest digest mismatch")
        valid = self._validate_schema1_manifest(upstream_namespace, upstream_repo_name, manifest)
        if not valid:
            raise ManifestDoesNotExist("invalid schema 1 manifest")
        return manifest

    def _validate_schema1_manifest(
        self, namespace: str, repo: str, manifest: DockerSchema1Manifest
    ) -> bool:
        if manifest.schema_version != 1:
            return True

        if (
            manifest.namespace == ""
            and features.LIBRARY_SUPPORT
            and namespace == app.config["LIBRARY_NAMESPACE"]
        ):
            pass
        elif manifest.namespace != namespace:
            return False

        if manifest.repo_name != repo:
            return False

        return True
