# Quay Configurable-Digest Handoff

## Purpose

This file is the current operational snapshot for the next implementation session. It is not a cumulative execution log. Detailed history remains in Git.

The feature supports repository-visible SHA-256, SHA-384, and SHA-512 identities for selected Registry V2 operations while retaining SHA-256 as Quay's canonical internal storage, deduplication, manifest, and graph identity.

## Start here

1. Work only in `/Users/shossain/QuayWorkspace/shaon-feature-PQC`.
2. Use branch `shaon-feature-PQC`.
3. Read this file, the planning repository's `TODO.md` and `PQC-Features.md`, `AGENTS.md`, `IMPLEMENTATION_PLAN.md`, and relevant `agent_docs/` files completely.
4. Recheck branch, HEAD, merge base, status, staging, untracked files, worktrees, recent history, and the complete `master...HEAD` diff before changing files.
5. Continue the existing implementation. Do not reset, rebase, restore, discard, overwrite, amend, or replace work.
6. Do not access or modify `/Users/shossain/QuayWorkspace/11537-pqc-schema`.
7. Do not push, alter remotes, update pull requests, or modify another worktree.
8. Before modifying files, update only the current session's `.PITASKS.md` section.

## Repository state

- Worktree: `/Users/shossain/QuayWorkspace/shaon-feature-PQC`
- Branch: `shaon-feature-PQC`
- Base branch: `master`
- Base and merge base: `d81004d24669132d45df8fbd1eafc86c149e38fa`
- Pre-Story 13 HEAD: `2afe0fca9ccb30cd97ed1bb777dfc3c5cdd3865d`
- Pre-Story 13 subject: `NO-ISSUE: test(registry): validate same-Quay digest copy`
- Story 7 implementation: `b59c747e7482f174dee81508dd3aca363ef7d7e6`
- Story 8 implementation: `bc21299727b875af15fa67a689677e69b9f688ff`
- External-registry deferral: `229069a0180a43f36f89d25f34a5bd72ad7dab7f`
- Pre-Story 14 HEAD: `943ac2c12dff1bbc8d6310886b5ab81747e29ca4`.
- Pre-Story 14 subject: `NO-ISSUE: fix(registry): preserve legacy SHA-256 identities`.
- Story 14 changes make registered manifest deletion include hidden lifecycle tags, suppress deleted native referrers, and invalidate native and fallback referrer caches. No schema, migration, configuration, blob unlink, repository/namespace cleanup, upload expiration, garbage collection, proxy, mirror, import, or Docker schema 1 alternative-identity behavior changed.
- Story 14 implementation commit: `796bfeba39cdf66c99f41e9e5675e8f149f15365` (`NO-ISSUE: fix(registry): delete registered digest content`).
- Story 14 validation-record commit and pre-Story 15 HEAD: `897b55bd15786ebd21f761b6de83f90593803b59` (`NO-ISSUE: docs(registry): correct Story 14 validation record`).
- Story 15 removes target-repository blob registrations before canonical storage collection and removes residual target-repository manifest registrations before repository deletion. Repository and namespace marking still preserve registrations until their queued purge runs. No upload expiration, ordinary garbage-collection policy, physical orphan recovery, schema, migration, proxy, mirror, import, or Docker schema 1 alternative-identity behavior changed.
- The Story 15 implementation is committed with subject `NO-ISSUE: fix(gc): remove repository digest registrations`. Its hash cannot be embedded in its own Git preimage; use `git rev-parse HEAD` and verify the subject.
- Expected target status after the Story 15 commit: clean, with no staged, unstaged, or untracked files.
- Story 16 makes abandoned-upload expiration retry storage cancellation before deleting the `BlobUpload` row that owns storage metadata and requested-digest hash state. A failed UUID is skipped only for the rest of the current worker pass, so unrelated stale uploads continue and the failed upload is retried on the next pass. Missing storage is idempotent success.
- The Story 16 implementation is committed with subject `NO-ISSUE: fix(registry): retry abandoned upload cleanup`. Its hash cannot be embedded in its own Git preimage; use `git rev-parse HEAD` and verify the subject.
- Expected target status after the Story 16 commit: clean, with no staged, unstaged, or untracked files.
- The planning repository had unrelated modified and untracked files before this update. This session changed only its current-session `.PITASKS.md` section and Story 16 status and evidence in `TODO.md`. The planning repository was not committed. `PQC-Features.md` was unchanged because the accepted capability boundary did not change.

## Delivery status

- Story 1: **In progress**, pending independent evaluation.
- Story 2: **In progress**, pending independent evaluation.
- Story 3: **Done**.
- Story 4: **Done**.
- Story 5: **Done**.
- Story 6: **Done**.
- Story 7: **Done**.
- Story 8: **Done**.
- Story 9: **Deferred** by delivery-scope decision; no Story 9 code is present.
- Story 10: **Done**, limited to client-mediated copies between normal repositories managed by this Quay deployment.
- Story 11: **Deferred** with repository and organization mirroring.
- Story 12: **Deferred** with external image import.
- Story 13: **Done**.
- Story 14: **Done**.
- Story 15: **Done**.
- Story 16: **Done**.
- Recommended next work: **Story 17, garbage-collect unreferenced digest registrations and canonical content**.

Do not change Story 1 or Story 2 merely because later stories depend on their behavior.

## Non-negotiable identity contracts

1. `ImageStorage.content_checksum` and `Manifest.digest` remain canonical SHA-256.
2. Alternative identities remain repository-scoped `RepositoryBlobDigest` and `RepositoryManifestDigest` rows.
3. Alternative-only content does not expose hidden canonical SHA-256.
4. Canonical SHA-256 remains visible only when explicitly registered, historically visible before first registration, or intentionally created through a tag or SHA-256 route.
5. Digest parsing is strict. Encoded values are lowercase and exact length.
6. Supported algorithms are SHA-256, SHA-384, and SHA-512. The active API boundary must also allow the algorithm.
7. Registrations are immutable, idempotent, and repository-scoped.
8. Canonical graph, registration, quota, pruning, and tag changes remain transactionally consistent.
9. Cache invalidation happens only after lifecycle transactions commit.
10. Mirror-managed repositories retain their existing SHA-256-only ingestion boundary until a later story changes it.

## Completed behavior

### Stories 1-6 foundations

- Configuration accepts nonempty unique combinations of `sha256`, `sha384`, and `sha512`; the default remains SHA-256.
- Blob upload, resume, validation, pull, HEAD, and cross-repository mount support enabled registered identities while canonical bytes remain SHA-256.
- Single manifests, OCI indexes, and Docker manifest lists support enabled route identities and mixed registered descriptors.
- Tags resolve to deterministic enabled repository-visible identities.
- Hidden canonical identities, disabled algorithms, and cross-repository aliases remain inaccessible.

### Story 7 artifact publication and pull

- Digest-addressed OCI artifacts accept enabled SHA-256, SHA-384, and SHA-512 identities.
- Tag-addressed artifact publication remains canonical SHA-256 because the route carries no selected algorithm.
- Artifact config, layers, and subject descriptors are strict-parsed and repository-resolved.
- `Manifest.subject` stores the canonical subject SHA-256 internally.
- Exact artifact bytes, media type, size, `artifactType`, registered response identity, GET, and HEAD behavior are preserved.

### Story 8 referrer discovery

- The referrers endpoint strict-parses and allowlist-checks the requested subject digest before repository lookup or referrer cache access.
- Malformed, unsupported, disabled, unknown, hidden canonical, and cross-repository identities retain their established registry errors.
- SHA-256, SHA-384, and SHA-512 subject aliases resolve only through target-repository visibility rules.
- Canonical SHA-256 is used only for the internal subject graph query.
- Native and fallback referrers return a deterministic enabled repository-visible artifact identity. Canonical SHA-256 remains preferred only when it is actually visible and enabled.
- Descriptor digest, exact manifest byte size, media type, `artifactType`, annotations, and `OCI-Filters-Applied` are preserved.
- Multiple subject aliases, multiple artifact aliases, and native/fallback overlap deduplicate by canonical manifest ID.
- Fallback tags are searched across every repository-visible subject alias. Hidden canonical fallback tags are not searched.
- Exact lowercase SHA-512 fallback names (`sha512-<128 hex>`) have a narrow manifest GET/HEAD/PUT route because their 135-character form cannot fit the ordinary 128-character OCI tag route. Normal tag limits are unchanged.
- Digest-derived fallback tags enforce subject-algorithm hard-disable behavior on GET/HEAD, PUT, and digest-route tag parameters.
- Filtered and unfiltered cache keys remain separate. The Story 8 cache namespace is versioned so stale SHA-256-only entries cannot suppress alternative referrers after deployment.
- Cache entries retain all visible canonical artifacts and select enabled identities on every hit, so disable and re-enable changes apply without waiting for TTL.
- Cached records are copied before hydration; repeated cache hits do not mutate cached dictionaries.
- Native artifact publication invalidates filtered and unfiltered caches for every visible subject alias after commit.
- Fallback-index tag publication resolves only visible digest-derived subject tags and invalidates every visible alias after commit. It invalidates artifact types from both previous and current fallback indexes.
- Existing SHA-256 discovery, filtering, authorization, publication, pull, and protocol behavior remain intact.

### Story 10 same-Quay copy

- A client can copy a complete mixed-digest image graph between normal repositories on this Quay deployment through standard Registry V2 requests.
- Source tag resolution selects one deterministic enabled root identity. Digest-addressed child and root publication preserves the identities carried by the selected graph.
- Cross-repository mounts reuse canonical `ImageStorage` and placements while registering only the requested blob identity at the destination.
- The copied destination graph retains canonical SHA-256 internally while alternative-only canonical identities remain hidden.
- Unrelated source manifest and blob aliases are not propagated to the destination.
- Repeated mount and publication requests are idempotent. Existing authorization, repository isolation, conflict rollback, and hard-disable contracts apply unchanged.
- Mirror-managed repositories remain SHA-256-only. Mirror workers, external registries, proxy cache, import, and artifact-copy variations are outside Story 10.

### Story 13 legacy SHA-256 compatibility

- A successful canonical SHA-256 blob or manifest lookup materializes the historically valid repository-scoped SHA-256 registration when the repository/content pair has no registrations.
- Legacy tag resolution materializes the canonical manifest registration only when SHA-256 is enabled and selected for the successful client-visible response.
- Lazy writes recheck on the primary database, use the existing idempotent unique-index registration helpers, and participate in the caller's transaction. The steady-state registered manifest read remains one query.
- Legacy single manifests, their configuration and layer blobs, direct digest GET/HEAD, and tag GET/HEAD retain exact bytes and SHA-256 response identities.
- Adding SHA-384 or SHA-512 identities after lazy registration leaves canonical SHA-256 visible. Registrations and fallback remain repository-scoped.
- Disabling SHA-256 blocks legacy and explicitly registered SHA-256 reads before cache lookup without deleting registrations or canonical content. Enabled alternative identities remain usable, and re-enabling SHA-256 restores canonical access and tag preference.
- Unauthorized, malformed, disabled, and unknown requests retain their established registry errors. Normal SHA-256 publication, pull, and mount behavior remains compatible.

### Story 14 registered-identity deletion

- Authorized manifest deletion resolves exact repository-scoped SHA-256, SHA-384, or SHA-512 registrations even when the requested algorithm is disabled.
- Deletion expires every alive tag for the manifest, including temporary hidden lifecycle tags used by untagged digest publication. All aliases then become unavailable through manifest GET, HEAD, referrer discovery, and repeated DELETE.
- Native OCI artifact deletion invalidates filtered and unfiltered referrer caches for every visible subject identity after the tag transaction commits.
- Fallback-index deletion invalidates subject caches derived from its digest-form fallback tag. Fresh and primed native/fallback discovery no longer returns deleted content.
- Native referrer response hydration rechecks canonical reachability, so a stale cache record cannot re-expose a deleted artifact.
- Manifest registrations, canonical manifests, graph links, blobs, and blob registrations remain for later lifecycle garbage collection. Story 14 does not remove canonical content physically.
- Arbitrary blob DELETE remains HTTP 405 `UNSUPPORTED` for registered and disabled identities because unlinking a referenced blob would corrupt manifest graphs. It does not mutate registrations, upload links, or bytes.
- Strict malformed/unsupported errors, unknown-content behavior, write authorization, repository isolation, immutable-tag behavior, and normal SHA-256 deletion remain intact.

### Story 15 repository and namespace registration cleanup

- Repository marking, namespace marking, and namespace grace periods retain digest registrations while content remains recoverable. Cleanup begins only when the repository GC path actually purges the hidden repository.
- Repository purge removes every `RepositoryBlobDigest` row owned by the target before attempting canonical `ImageStorage` collection, preventing registration foreign keys from blocking cleanup.
- Per-manifest cleanup retains `RepositoryManifestDigest` rows long enough to address all registered scanner identities, then removes them with each manifest. A final repository-scoped sweep removes residual legacy or inconsistent manifest registrations before repository deletion.
- Namespace GC bulk-marks its repositories and sends each through the same repository purge invariant. Both dedicated repository and namespace workers are covered.
- Cleanup is repository-scoped and retry-safe. Registrations owned by unrelated repositories and shared canonical SHA-256 storage still referenced outside the deleted repository remain unchanged.
- No active algorithm allowlist is consulted for lifecycle cleanup. No registration is remapped, and canonical SHA-256 remains Quay's internal storage identity.

### Story 16 abandoned-upload and hash-state cleanup

- `requested_digest_algorithm`, `requested_digest_state`, canonical resumable SHA-256 state, byte counts, and storage metadata are all owned by one `BlobUpload` row. Deleting that row removes all persisted requested-digest state; no separate alternative-hash table, temporary link, or storage metadata row exists.
- Explicit cancellation and successful monolithic or resumed finalization already deleted the whole row. Cancellation restores no hash state and therefore works for disabled, unsupported legacy, missing, partial, and corrupt persisted values without unsafe deserialization.
- Successful finalization still writes exact bytes to the canonical SHA-256 CAS path, creates the repository-scoped requested identity and temporary `UploadedBlob` reachability in one database transaction, then deletes the upload row. `ImageStorage.content_checksum` remains SHA-256.
- Abandoned-upload expiration now deletes a row only after targeted storage cancellation succeeds or reports `FileNotFoundError`. A transient storage failure retains the row, requested hash state, and storage metadata for the next scheduled pass.
- A failed UUID is excluded only from the remainder of the current pass. Other stale uploads, including uploads in other repositories, continue to expire. The exclusion set resets on the next pass so failures are retried without a hot loop or starvation.
- Concurrent upload or repository purge that removes the row after worker selection is a safe no-op at database deletion. Repeated worker cleanup and already-missing chunks are idempotent.
- The worker never parses or restores requested hash state and never consults `ALLOWED_HASH_ALGORITHMS`. SHA-256, hintless legacy, SHA-384, SHA-512, disabled, unknown, partially populated, and corrupt rows follow the same cleanup path.
- The Swift chunk cleanup worker remains storage-only. It idempotently removes queued segments and does not terminate or mutate a `BlobUpload` session.

## Story 8 root causes and changed files

The unfinished behavior had six causes:

1. `endpoints/v2/referrers.py` deliberately rejected every non-SHA-256 subject query.
2. The registry model rebuilt native and cached descriptors through a SHA-256-only selector.
3. Fallback discovery searched only the queried subject digest's fallback tag.
4. Cache misses stored only identities enabled at load time, stale cached dictionaries were mutated during hydration, and the cache namespace still represented Demo 1 semantics.
5. Fallback indexes carry no OCI subject, so their successful publication did not invalidate subject referrer caches.
6. A SHA-512 fallback tag is 135 characters and could not match Quay's normal 128-character tag route.

Production files changed:

- `endpoints/v2/referrers.py`
- `endpoints/v2/manifest.py`
- `data/registry_model/registry_oci_model.py`
- `data/model/oci/manifest.py`
- `data/cache/cache_key.py`

Test files changed:

- `endpoints/v2/test/test_manifest.py`
- `data/registry_model/test/test_interface.py`
- `data/cache/test/test_cache.py`

`data/registry_model/datatypes.py` and publication persistence were traced but required no Story 8 change. No schema or migration changed.

## Story 10 result and changed files

No production-code gap was found. The existing blob mount, digest-addressed manifest publication, `OCI-Tag`, graph persistence, and repository-scoped registration paths already compose into a correct same-deployment copy.

Target-worktree files changed:

- `endpoints/v2/test/test_manifest.py`
- `HANDOFF.md`

The planning repository also received the scoped Story 10 updates described under repository state. No schema or migration changed.

## Story 13 root cause and changed files

The legacy fallback was lookup-only. Before Story 13, canonical SHA-256 content with no registration row remained readable, and publication or mount paths preserved that identity before adding an alternative registration, but successful legacy blob GET/HEAD, manifest GET/HEAD, and tag resolution did not lazily persist the historical SHA-256 identity required by the contract.

Production files changed:

- `data/model/oci/blob.py`
- `data/model/oci/manifest.py`
- `data/registry_model/interface.py`
- `data/registry_model/registry_oci_model.py`
- `endpoints/v2/manifest.py`

Test files changed:

- `data/model/oci/test/test_oci_manifest.py`
- `endpoints/v2/test/test_blob.py`
- `endpoints/v2/test/test_manifest.py`

Documentation changed:

- `HANDOFF.md`

No schema or migration changed.

## Story 14 root causes and changed files

The deletion gaps had four causes:

1. The manifest DELETE endpoint used normal visible lookup, so a digest-pushed manifest with only a hidden lifecycle tag returned `MANIFEST_UNKNOWN`.
2. `delete_tags_for_manifest` selected visible tags only, leaving temporary hidden tags alive and the registered manifest reachable.
3. Native referrer selection trusted cached/database candidates without rechecking canonical reachability after deletion.
4. Manifest deletion invalidated manifest and tag caches but did not invalidate native subject or digest-form fallback referrer caches.

Production files changed:

- `data/model/oci/tag.py`
- `data/registry_model/registry_oci_model.py`
- `endpoints/v2/manifest.py`

Test files changed:

- `data/model/oci/test/test_oci_tag.py`
- `endpoints/v2/test/test_blob.py`
- `endpoints/v2/test/test_manifest.py`

Documentation changed:

- `HANDOFF.md`

No schema or migration changed. Blob unlink remained explicitly unsupported.

## Story 15 root cause and changed files

The registration tables have non-cascading foreign keys, while recursive repository deletion deliberately skips both registration models. Manifest GC already removed manifest registrations one manifest at a time, but repository purge never removed blob registrations before deleting canonical `ImageStorage`. Real placed blobs therefore raised a foreign-key failure and interrupted both dedicated repository GC and namespace GC. Placeholder-only model data could avoid the storage delete and complete through Peewee dependency cleanup, masking the production path gap.

Production file changed:

- `data/model/gc.py`

Test files changed:

- `data/model/test/test_gc.py`
- `data/model/test/test_user.py`
- `workers/test/test_namespacegcworker.py`
- `workers/test/test_repositorygcworker.py`

Documentation changed:

- `HANDOFF.md`

No schema or migration changed.

## Story 16 root causes and changed files

The persistence trace found one production gap and two pre-existing test defects:

1. Requested-digest state is not separately persisted: it lives only in `BlobUpload`. Existing successful commit and explicit cancellation therefore already removed it correctly by deleting the row.
2. The abandoned-upload worker caught every storage cancellation exception and then unconditionally deleted `BlobUpload`. A transient storage failure could leave chunks behind while discarding the only targeted `storage_metadata`, making the failed cleanup impossible to retry.
3. Preserving that row without excluding it from the current selection loop would repeatedly select the same failed UUID and block unrelated stale uploads. Stale lookup therefore gained an optional per-pass exclusion collection.
4. The existing cleanup test called `blob_upload_exists` without asserting its false value, and the corrupt-state uploader test incorrectly assumed fixture repositories had zero digest registrations. These test defects were corrected without changing product behavior.

Production files changed:

- `data/model/blob.py`
- `workers/blobuploadcleanupworker/blobuploadcleanupworker.py`
- `workers/blobuploadcleanupworker/models_interface.py`
- `workers/blobuploadcleanupworker/models_pre_oci.py`

Test files changed:

- `data/registry_model/test/test_blobuploader.py`
- `endpoints/v2/test/test_blob.py`
- `workers/blobuploadcleanupworker/test/test_blobuploadcleanupworker.py`
- `workers/test/test_chunkcleanupworker.py`

Documentation changed:

- `HANDOFF.md`

No schema, migration, configuration, digest registration, manifest/blob garbage collection, repository/namespace deletion, proxy, mirror, import, or Docker schema 1 behavior changed.

## Verification evidence

### Baseline and failing regressions

Baseline before Story 8 changes:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/v2/test/test_manifest.py -k 'sha256_referrer_query_and_artifact_type_filter or alternative_referrer_subject_query_is_unsupported_when_enabled or referrer_subject_query_parses_strictly_before_capability_check'`

Result: **6 passed, 68 deselected**.

The first Story 8 endpoint regression failed before production changes: **4 failed, 74 deselected**. SHA-384 and SHA-512 subjects returned the temporary `UNSUPPORTED` boundary, and a SHA-384 artifact was omitted from a SHA-256 subject response.

The fallback cache regression also failed before its supporting production change: **1 failed, 121 deselected** because fallback-tag publication left primed alias caches empty.

### Final focused and broad tests

Focused Story 8 endpoint selection:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/v2/test/test_manifest.py -k 'story8 or referrer_subject_query or alternative_referrer_subject_query or sha256_referrer_query'`

Result: **16 passed, 68 deselected**.

Focused registry referrer selection:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_interface.py -k 'referrer or fallback_tag_publication'`

Result: **9 passed, 113 deselected**.

Complete manifest endpoint suite:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/v2/test/test_manifest.py`

Result: **84 passed**.

Complete registry interface suite:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_interface.py`

Result: **120 passed, 2 skipped**. The skips remain the PostgreSQL-only blob and manifest registration race tests that passed in earlier sessions.

OCI manifest and cache suites:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/model/oci/test/test_oci_manifest.py data/cache/test/test_cache.py`

Result: **43 passed**.

SHA-256 registry protocol push/pull regression:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings test/registry/registry_tests.py -k 'test_basic_push_pull_by_manifest'`

Result: **3 passed, 1,456 deselected**.

### Story 10 tests

Focused same-Quay copy contract:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/v2/test/test_manifest.py -k story10`

Result: **1 passed, 84 deselected**.

Complete manifest and blob endpoint suites:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/v2/test/test_manifest.py endpoints/v2/test/test_blob.py`

Result: **141 passed**.

Complete registry interface suite:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_interface.py`

Result: **120 passed, 2 skipped**. The skips remain the PostgreSQL-only registration race tests that passed in earlier sessions.

Relevant SHA-256 registry protocol push, pull, and mount coverage:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings test/registry/registry_tests.py -k 'test_basic_push_pull_by_manifest or blob_mount'`

Result: **255 passed, 1,204 deselected**.

### Story 13 tests

Initial characterization before production changes:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/v2/test/test_blob.py endpoints/v2/test/test_manifest.py -k story13`

Result: **4 failed, 141 deselected**. Legacy content was readable, but no lazy blob or manifest SHA-256 registration was created.

Focused final Story 13 coverage:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/v2/test/test_blob.py endpoints/v2/test/test_manifest.py data/model/oci/test/test_oci_manifest.py -k story13`

Result: **5 passed, 164 deselected**.

Complete affected manifest and blob endpoint suites:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/v2/test/test_manifest.py endpoints/v2/test/test_blob.py`

Result: **145 passed**.

Complete relevant OCI manifest model and registry-interface suites:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/model/oci/test/test_oci_manifest.py data/registry_model/test/test_interface.py`

Result: **144 passed, 2 skipped**. The skips are the PostgreSQL-only registration races covered separately below.

PostgreSQL Story 13 atomicity and registration-race coverage:

`TEST=true TEST_DATABASE_URI='postgresql://quay:quay@localhost:5432/quay' PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/model/oci/test/test_oci_manifest.py data/registry_model/test/test_interface.py -k 'story13 or repository_digest_registration_live_concurrency'`

Result: **2 passed, 144 deselected**.

`TEST=true TEST_DATABASE_URI='postgresql://quay:quay@localhost:5432/quay' PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_interface.py::test_repository_digest_registration_live_concurrency data/registry_model/test/test_interface.py::test_repository_manifest_digest_registration_live_concurrency`

Result: **2 passed**.

Existing SHA-256 registry protocol push, pull, and mount regressions:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings test/registry/registry_tests.py -k 'test_basic_push_pull_by_manifest or blob_mount'`

Result: **255 passed, 1,204 deselected**.

Targeted mypy:

`.venv/bin/mypy data/model/oci/blob.py data/model/oci/manifest.py data/registry_model/interface.py data/registry_model/registry_oci_model.py endpoints/v2/manifest.py`

Result: **passed**, no issues in five source files.

### Story 14 tests

Initial characterization from the Quay worktree:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/model/oci/test/test_oci_tag.py endpoints/v2/test/test_manifest.py endpoints/v2/test/test_blob.py -k story14`

Result: **4 failed, 5 passed, 241 deselected**. Two failures demonstrated product gaps: an untagged registered artifact returned `MANIFEST_UNKNOWN`, and a deleted fallback index remained in a primed referrer cache. Two failures were test defects: one helper created a repository under a name different from the fixture helper's fixed lookup, and one assertion passed a database `Repository` row to an API expecting `RepositoryReference` after deletion had succeeded. After those test defects and the first production gaps were corrected, a later focused run exposed the additional product gap that native referrer hydration did not recheck deleted-artifact reachability. Code tracing and the corrected model characterization also confirmed that hidden lifecycle tags had to be included in deletion. The first attempted command was run from the planning directory and could not find `.venv/bin/python`; it did not execute tests.

Final focused SQLite coverage:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/model/oci/test/test_oci_tag.py endpoints/v2/test/test_manifest.py endpoints/v2/test/test_blob.py -k story14`

Result: **9 passed, 241 deselected**.

Complete manifest and blob endpoint suites:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/v2/test/test_manifest.py endpoints/v2/test/test_blob.py`

Result: **153 passed**.

Complete OCI tag model suite:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/model/oci/test/test_oci_tag.py`

Result: **96 passed, 1 skipped**. The skip is an existing database-specific case.

Complete registry interface suite:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_interface.py`

Result: **120 passed, 2 skipped**. The skips remain the PostgreSQL-only registration races validated in Story 13.

Focused PostgreSQL Story 14 coverage:

`TEST=true TEST_DATABASE_URI='postgresql://quay:quay@localhost:5432/quay' PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/model/oci/test/test_oci_tag.py endpoints/v2/test/test_manifest.py endpoints/v2/test/test_blob.py -k story14`

Result: **9 passed, 241 deselected**.

Broad SHA-256 registry deletion, deleted-digest pull, push/pull, and mount regressions:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings test/registry/registry_tests.py -k 'test_delete_manifest or test_attempt_pull_by_manifest_digest_for_deleted_tag or test_basic_push_pull_by_manifest or blob_mount'`

Result: **273 passed, 1,186 deselected**.

Final SHA-256 registry deletion and deleted-digest pull selection after the last production adjustment:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings test/registry/registry_tests.py -k 'test_delete_manifest or test_attempt_pull_by_manifest_digest_for_deleted_tag'`

Result: **18 passed, 1,441 deselected**.

### Story 15 tests

The first characterization command used the final focused selection but produced **3 test-defect failures and 75 deselections** because a reused manifest fixture helper is fixed to `devtable/newrepo`; it could not populate the differently named repositories. No production conclusion was drawn from that run. After correcting only test setup, the same selection produced **3 product failures, 1 pass, and 74 deselections**. Dedicated repository purge and both worker paths raised `peewee.IntegrityError: FOREIGN KEY constraint failed` while deleting `ImageStorage` referenced by `RepositoryBlobDigest`. The placeholder-storage namespace model case passed because existing storage cleanup deliberately defers placeholders.

Final focused SQLite coverage:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/model/test/test_gc.py data/model/test/test_user.py workers/test/test_repositorygcworker.py workers/test/test_namespacegcworker.py -k story15`

Result: **4 passed, 74 deselected**.

Focused PostgreSQL coverage:

`TEST=true TEST_DATABASE_URI='postgresql://quay:quay@localhost:5432/quay' PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/model/test/test_gc.py data/model/test/test_user.py workers/test/test_repositorygcworker.py workers/test/test_namespacegcworker.py -k story15`

Result: **4 passed, 74 deselected**.

Complete affected model and worker suites:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/model/test/test_gc.py data/model/test/test_user.py workers/test/test_repositorygcworker.py workers/test/test_namespacegcworker.py`

Result: **78 passed**.

Complete repository API endpoint and model-adapter suites:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/api/test/test_repository.py endpoints/api/test/test_repository_models_pre_oci.py`

Result: **49 passed**.

SHA-256 registry deletion regressions:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings test/registry/registry_tests.py -k 'test_delete_manifest or test_attempt_pull_by_manifest_digest_for_deleted_tag'`

Result: **18 passed, 1,441 deselected**.

### Story 16 tests

The first three baseline commands were mistakenly launched from the planning directory. Each returned `/bin/bash: .venv/bin/python: No such file or directory`; no tests ran. The corrected baseline commands ran from the Quay worktree.

Corrected pre-change baseline:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_blobuploader.py`

Result: **2 failed, 29 passed**. Both failures were pre-existing test defects: the corrupt-state test assumed an initialized repository had zero digest registrations.

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings workers/blobuploadcleanupworker/test/test_blobuploadcleanupworker.py`

Result: **8 passed**. The existing row-removal check did not assert its result, so this did not prove state deletion.

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/v2/test/test_blob.py -k 'cancel or alternative_chunked_blob_upload_resume_and_pull or alternative_monolithic_upload_infers_algorithm_without_hint or final_sha256_is_authoritative'`

Result: **6 passed, 54 deselected**.

Initial Story 16 characterization before production changes:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_blobuploader.py -k story16`

Result: **4 passed, 31 deselected**.

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings workers/blobuploadcleanupworker/test/test_blobuploadcleanupworker.py -k story16`

Result: **2 failed, 1 passed, 8 deselected**. One failure demonstrated the product gap: the row and retry metadata disappeared after storage cancellation failed. The other was a test defect caused by attempting to hash `mock.call` objects.

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/v2/test/test_blob.py -k story16`

Result: **2 passed, 60 deselected**.

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings workers/test/test_chunkcleanupworker.py`

Result: **1 passed**.

After production correction, the same four selections passed with **4**, **4**, **2**, and **1** tests respectively. A later direct repository-deletion characterization produced **1 test-defect failure and 4 passes** because it bypassed Quay's required purge sequence and hit expected foreign-key protection. It was removed; repository purge removes the upload row before the repository, which is the same missing-row race already covered.

Complete affected upload model, uploader, endpoint, and worker suites:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/model/test/test_blob.py data/model/test/test_model_blob.py data/registry_model/test/test_blobuploader.py endpoints/v2/test/test_blob.py workers/blobuploadcleanupworker/test/test_blobuploadcleanupworker.py workers/test/test_chunkcleanupworker.py`

Result: **115 passed**.

Complete storage suites:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings storage/test`

Result: **159 passed**.

Existing SHA-256 upload, finalization, cancellation, chunk, overlap, and resume regressions:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings test/registry/registry_tests.py -k 'test_basic_push_pull_by_manifest or test_cancel_upload or test_chunked_blob_uploading or test_chunked_uploading_mismatched_chunks or test_chunked_uploading_missing_first_chunk'`

Result: **24 passed, 1,435 deselected**.

Relevant PostgreSQL coverage:

`TEST=true TEST_DATABASE_URI='postgresql://quay:quay@localhost:5432/quay' PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/model/test/test_blob.py data/model/test/test_model_blob.py data/registry_model/test/test_blobuploader.py endpoints/v2/test/test_blob.py workers/blobuploadcleanupworker/test/test_blobuploadcleanupworker.py workers/test/test_chunkcleanupworker.py -k 'story16 or cancel_upload or basic_upload_blob or blobupload_sha_state or blobuploadcleanupworker'`

Result: **29 passed, 86 deselected**.

After Black formatting, the final focused selection passed:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_blobuploader.py endpoints/v2/test/test_blob.py workers/blobuploadcleanupworker/test/test_blobuploadcleanupworker.py workers/test/test_chunkcleanupworker.py -k 'story16 or alternative_monolithic_upload_infers_algorithm_without_hint'`

Result: **13 passed, 97 deselected**.

### Static checks

Story 16's first pre-commit pass reformatted `workers/blobuploadcleanupworker/test/test_blobuploadcleanupworker.py` with Black, so that pass correctly returned nonzero. The final pass over every changed Quay file passed without changes.

Story 16 targeted mypy:

`.venv/bin/mypy data/model/blob.py workers/blobuploadcleanupworker/blobuploadcleanupworker.py workers/blobuploadcleanupworker/models_interface.py workers/blobuploadcleanupworker/models_pre_oci.py`

Result: **passed**, no issues in four source files.

Story 16 compilation and whitespace:

`.venv/bin/python -m compileall -q data/model/blob.py data/registry_model/test/test_blobuploader.py endpoints/v2/test/test_blob.py workers/blobuploadcleanupworker/blobuploadcleanupworker.py workers/blobuploadcleanupworker/models_interface.py workers/blobuploadcleanupworker/models_pre_oci.py workers/blobuploadcleanupworker/test/test_blobuploadcleanupworker.py workers/test/test_chunkcleanupworker.py && git diff --check && git diff --check upstream/master`

Result: **passed**.

Pre-commit passed over every Story 8 target-worktree file. Earlier passes reformatted Python with Black; the final pass made no changes.

Targeted mypy:

`.venv/bin/mypy data/cache/cache_key.py data/model/oci/manifest.py data/registry_model/registry_oci_model.py endpoints/v2/manifest.py endpoints/v2/referrers.py`

Result: **passed**, no issues in five source files.

Compilation and whitespace:

`.venv/bin/python -m compileall -q data/cache/cache_key.py data/cache/test/test_cache.py data/model/oci/manifest.py data/registry_model/registry_oci_model.py data/registry_model/test/test_interface.py endpoints/v2/manifest.py endpoints/v2/referrers.py endpoints/v2/test/test_manifest.py && git diff --check && git diff --check master`

Result: **passed**.

Story 10 pre-commit passed for `endpoints/v2/test/test_manifest.py` after the first Black pass reformatted the file. Python compilation and `git diff --check` also passed.

Story 13 pre-commit passed for every changed Quay file after the first pass reformatted five Python files. Python compilation and `git diff --check` passed.

Story 14 pre-commit passed for all six changed Quay files. The first virtual-environment run reformatted `endpoints/v2/test/test_blob.py`; the final run made no changes. A prior invocation through the shell `PATH` did not run because `pre-commit` was unavailable there.

Story 14 targeted mypy:

`.venv/bin/mypy data/model/oci/tag.py data/registry_model/registry_oci_model.py endpoints/v2/manifest.py`

Result: **passed**, no issues in three source files.

Story 14 compilation and whitespace:

`.venv/bin/python -m compileall -q data/model/oci/tag.py data/model/oci/test/test_oci_tag.py data/registry_model/registry_oci_model.py endpoints/v2/manifest.py endpoints/v2/test/test_blob.py endpoints/v2/test/test_manifest.py && git diff --check && git diff --check master`

Result: **passed**.

Story 15 pre-commit passed over all six changed Quay files. The first run reformatted and reordered imports in the five Python files; the final run made no changes.

Story 15 targeted mypy:

`.venv/bin/mypy data/model/gc.py`

Result: **passed**, no issues in the production file.

Story 15 compilation and whitespace:

`.venv/bin/python -m compileall -q data/model/gc.py data/model/test/test_gc.py data/model/test/test_user.py workers/test/test_namespacegcworker.py workers/test/test_repositorygcworker.py && git diff --check && git diff --check upstream/master`

Result: **passed**.

## Live Story 8 validation

The running local Quay container was bind-mounted from the target worktree. `REFERRERS_API` was enabled and the active allowlist was `['sha256', 'sha384', 'sha512']`.

The final successful repository was `localhost:8080/testuser/pqc-story8-1fff541c`; isolation was checked against `testuser/pqc-story8-1fff541c-other`.

Key identities:

- Subject SHA-384: `sha384:8fb7cf51f5221ee322067a3c70dd73f718d0a999a1f4a0a4d1a89bbe409da8cd534acc2bc42ca7d738c2eed4506e3b59`
- Subject SHA-512: `sha512:722558fc07e5c2d82f47739d74f71fe04814832c9cd31e00a2e3139bb35d7b0f5ac3ed522f90501d12e0a87d39a137cc23ea13c859391332a1fbc567d2125b68`
- Hidden subject SHA-256: `sha256:f559c30eefea1de04aa122544e1a5ac85cf67d57bb8b3bddb0cd70098eec2b11`
- Native artifact SHA-384: `sha384:4a4a382ffd22e7b9b6e294db2543cf7480c0f0f23ff1329d0cdb9cbb5570ff7705ea6dc1bbed6475e9fd5d7fc089f44a`
- Native artifact SHA-512 alias: `sha512:a2a78ebc8252a1e87c198667c4bd4de3ed3fad77bda40704e4ff892f01f1c1739a7140dad24511841c7e5497134cd18a1b9a2d90cf751b0be39ef6ba75e8cf2b`
- Fallback artifact SHA-512: `sha512:62a76e7af3ab625507ec368483b33133d37ec30fe78232e7b15b27cd94e1882ae499b09edb0b56119561e71498c5712f1d36cfc2e32b537efb5976432d9e6865`

Live outcomes:

- Empty filtered and unfiltered caches were primed through both subject aliases before artifact publication.
- Native artifact publication refreshed both aliases immediately.
- SHA-384 and SHA-512 fallback indexes published successfully, including the 135-character SHA-512 fallback tag.
- Both subject aliases returned byte-for-byte identical two-descriptor responses. The response SHA-256 was `df2c550ff7ba73e7342a85b55af85d978b74d951e9fd8c7e8eb7f9d7f2f04779`.
- Native/fallback overlap deduplicated to one native descriptor. The native descriptor selected SHA-384 deterministically; the fallback-only descriptor selected SHA-512.
- Descriptor size, media type, and `artifactType` matched exact artifact bytes.
- Signature, SBOM, and missing filters returned the expected descriptors and `OCI-Filters-Applied` header.
- GET and HEAD through returned identities returned exact artifact bytes, content lengths, media types, and digest headers.
- Hidden canonical subject GET returned HTTP 404 `MANIFEST_UNKNOWN`; referrer query returned HTTP 400 `MANIFEST_INVALID`.
- A fallback tag built from hidden canonical SHA-256 was not included in discovery.
- The cross-repository subject alias returned HTTP 400 `MANIFEST_INVALID`.
- An unauthenticated referrer request returned HTTP 401.
- Read-only database inspection confirmed canonical SHA-256 artifact and subject storage with repository-scoped SHA-384/SHA-512 registrations.

The first live attempt failed because the temporary script supplied an invalid OCI image config; that was a validation-script defect. The second attempt identified the real 128-character route limit for SHA-512 fallback tags. The final flow passed after the narrow route fix. Live repositories were not deleted because lifecycle cleanup is outside Story 8.

## Live Story 10 validation

The local Quay container was bind-mounted from the target worktree and used `ALLOWED_HASH_ALGORITHMS: ['sha256', 'sha384', 'sha512']`. `regctl` 0.11.5 copied the existing source `localhost:8080/testuser/pqc-story4:sha512` to `localhost:8080/testuser/pqc-story10-1788297397:copy` on the same deployment with forced recursive traversal.

Live outcomes:

- Source and destination returned the same SHA-512 root: `sha512:0bd23dcfecb44322dd952511fc3c392f2eb652f3eb6dac7c352156a43b782a957b8cf23b26633bffc66c56acbdb82e6f805501661cd55b011e01673a4a72963c`.
- Source and destination root manifest bytes were identical.
- Independent hashing verified 52 unique objects: one index, 17 child manifests, 17 configurations, and 17 layers.
- The destination had 18 SHA-512 manifest registrations and 34 SHA-512 blob registrations.
- Every destination blob registration referenced the same `ImageStorage` row as its source registration. No copied blob required another physical object.
- The unregistered canonical SHA-256 root and a sampled canonical SHA-256 blob remained inaccessible at the destination.

The first live health probe hung while the long-running local container was otherwise present. Restarting only `quay-quay` restored service. The first read-only database inspection script omitted application initialization and failed before querying; the corrected script passed. These were environment and validation-script issues, not copy failures. The live repository was not deleted because repository cleanup remains later lifecycle work.

## Live Story 13 validation

The running `quay-quay` container was bind-mounted from the target worktree. After one restart repaired an unhealthy local service key and loaded the changed code, instance health returned HTTP 200 and the active allowlist was SHA-256, SHA-384, and SHA-512.

A fresh OCI single manifest and its configuration and layer were published to `localhost:8080/testuser/pqc-story13-live:legacy`. The three SHA-256 registration rows were then removed directly from the dedicated local validation repository to simulate content predating the registration tables.

Key identities:

- Manifest SHA-256: `sha256:79503b66a3368375691e60ce9466ec219eb0623630ee0396ffaab554aca12e96`
- Configuration SHA-256: `sha256:1469859180c47a3ef84f3c0e939ca79282ad0875c07eb5190f1ef01653d2c1de`
- Layer SHA-256: `sha256:d7f71ce14f226b9d64143dad86d197fbc60588fe837ef13dcc5e11172b805321`
- Manifest SHA-512: `sha512:fa35dee192b657c691347a52b07ba35c7ee11432431666473a620bbb3048a8cebe6c664d3f6b66b28c245f03a1aaf4779565b6534098ddc1ce69bdae91ac472a`
- Configuration SHA-512: `sha512:615023b5bdc18c85f3de512d279b3748cb40219348abdd2188d0b66b6fddbf13dece2a8557ab28a26a01c3cb72bab703e285237a027f49d069c406854a961b9b`
- Layer SHA-512: `sha512:7619b0352ead07abc02f1fb67c8132ca0a48a636850361bc8aced4e27e14b010dc83e3404f983429c34c649fb3d63e5dc7093fb3aa861dcc27129bd0414f8746`

Live outcomes:

- Tag and canonical digest manifest GET/HEAD returned HTTP 200, exact bytes, media type, and canonical digest headers.
- Configuration and layer GET/HEAD returned HTTP 200, exact bytes, lengths, and canonical digest headers.
- Repeated requests produced exactly one manifest and two blob SHA-256 registrations with the expected canonical mappings.
- Manifest and blob GET/HEAD in an unrelated existing repository returned HTTP 404 and created no registration there.
- SHA-512 blob uploads and digest-addressed manifest publication added alternative registrations while both canonical SHA-256 identities remained readable.
- With SHA-256 removed from the active allowlist, canonical manifest and blob GET/HEAD returned HTTP 400 `UNSUPPORTED` with `reason: disabled`; SHA-512 GET/HEAD remained HTTP 200; and the legacy tag selected the enabled SHA-512 identity.
- Database inspection while disabled still showed two manifest registrations and four blob registrations. Canonical rows and bytes were not deleted.
- The configuration file was restored byte-for-byte, Quay was restarted, health returned HTTP 200, canonical manifest and blob access returned HTTP 200, and the tag again preferred SHA-256.
- Unauthenticated manifest and blob requests returned HTTP 401. Malformed SHA-256 returned HTTP 400 `DIGEST_INVALID/malformed`. Unknown canonical values returned `MANIFEST_UNKNOWN` or `BLOB_UNKNOWN` with HTTP 404.

The first `regctl` copy attempts returned unauthorized because the running stack's service key was unhealthy; a direct request confirmed `Unknown service key`, and restarting only `quay-quay` repaired the environment. Three temporary validation commands were also defective: the first SQL query used the nonexistent `quayuser` table instead of quoted `user`, one `psql -c` command incorrectly expected variable interpolation, and the first HEAD loop used `curl -X HEAD`, which waited for a body. Corrected commands passed. These were environment or validation-script failures, not product test failures. The dedicated live repository and two empty repositories from the failed copy attempts were not deleted because repository cleanup is outside Story 13.

## Live Story 14 validation

The running `quay-quay` container was bind-mounted from the target worktree. Instance health returned HTTP 200 and the initial allowlist was SHA-256, SHA-384, and SHA-512. The dedicated repository was `localhost:8080/testuser/pqc-story14-live-1788331630`.

Key identities:

- Subject SHA-512: `sha512:067885a63490613fce2dc53160df716bb9250ff1a6aa9f0339c0ae948eaabd87a5eb7c5cfe0d80b2e5140943e697b27624980642153c766bfcf52402d98ee563`
- Native artifact SHA-512: `sha512:41821b2e957a31e19c0e27e4cfbd7243b9e933fd6c1b9b54e12b7a0c34a8d90153080ebba90d233f449994ee7d3807859d03c75e4504b45c62b52ac8b2893b3f`
- Native artifact SHA-384 alias: `sha384:49b5eb7722d6de746bddf6b572e7ccb17aad4cd89d4994c3e420ff11a3729518969ecd40ce221c28d7d269ffbb1b05b4`
- Fallback-only artifact SHA-384: `sha384:c106fbe1acdc0031eaf2843446242a0b3aa4d041588bdee851ee7a37adc9c56b3168204ade23e81ad6a4889c2d2ba753`
- Fallback index SHA-512: `sha512:04fc6c6f2253a362ba96bc1482cfd0953ba0bbc795f39a9bc5c60405dbed49ff879e0b6d5a527856be001061b8781292d908fab9187d5f4f6415e3cd2cd616ba`
- Sample registered blob SHA-512: `sha512:2cad476a6a6a68bb7e0319ab26f8d5c0e74fdbe0e3b4da7f6022ba605994eafd15b53e5343e669f42c87ab7fb7cb6e7fe734a4f9fd1bbeea6f932a9506f3c7a7`

Live outcomes:

- A tagged SHA-512 subject, untagged native artifact with SHA-512 and SHA-384 identities, untagged fallback-only SHA-384 artifact, and SHA-512 fallback index were published successfully.
- Two identical referrer requests primed the cache and returned exactly the native SHA-512 artifact and fallback-only SHA-384 artifact.
- SHA-512 was removed temporarily from the active allowlist. While disabled, deleting the native artifact and fallback index through their SHA-512 identities each returned HTTP 202.
- Blob DELETE through the disabled registered SHA-512 identity returned HTTP 405 `UNSUPPORTED`, and a SHA-512 manifest GET returned the expected HTTP 400 disabled error.
- The configuration was restored byte-for-byte to SHA-256 `4de46dadfc727018452ddd490056beb5f5af736e1d0f5751cb8ddb942430731f`, Quay restarted, and health returned HTTP 200.
- After restore, native artifact GET/HEAD through SHA-512 and SHA-384 and fallback-index GET/HEAD through SHA-512 returned HTTP 404. The fallback tag returned HTTP 404. Referrer discovery returned an empty descriptor list from the previously primed cache path.
- The subject, fallback-only artifact, and sample blob remained readable through their registered identities, proving deletion was scoped and blob DELETE did not mutate content.
- Read-only PostgreSQL inspection showed canonical SHA-256 manifests with repository registrations retained. The deleted native artifact and fallback index had zero alive tags; the subject and fallback-only artifact each had one. Six blob registrations, six recent-upload links, and six manifest-blob links remained.

Container restarts produced transient connection resets and one three-second readiness-probe timeout before health returned 200; no registry operation failed. The first read-only `psql -c` command incorrectly assumed client-variable expansion and failed before querying; the corrected literal-safe command passed. This was a validation-script defect, not a product failure. The live repository was not deleted because repository cleanup is Story 15.

## Live Story 15 validation

The running `quay-quay` container was bind-mounted from the target worktree. Instance health returned HTTP 200, and local PostgreSQL was accepting connections. Two pre-existing disposable repositories were purged through the real `mark_repository_for_deletion` and `RepositoryGCWorker._perform_gc` path.

- `testuser/pqc-story14-live-1788331630` had five manifest registrations and six blob registrations before marking. Marking retained all registrations. Worker purge removed the repository and all eleven target rows while leaving every unrelated manifest and blob registration count unchanged.
- The Story 14 purge attempted existing security-scanner report cleanup, but the optional local scanner at `localhost:6000` was unavailable. The existing cleanup code logged and tolerated those environment failures; repository and registration cleanup completed.
- `testuser/pqc-story5-destination-01a05e37` had one blob registration pointing to canonical `ImageStorage` shared with another repository. Marking retained it. Worker purge removed the target repository and registration, preserved every unrelated registration, preserved the other repository's registration, and retained the shared canonical SHA-256 storage row.
- Queue rows created by the direct validation scripts were removed after synchronous worker invocation.

The first read-only repository inventory script called the guarded `FullIndexedCharField.startswith` operation and failed before querying. The corrected script used `match_prefix` and passed. This was a validation-script defect. Later five-second and fifteen-second health probes timed out while the container remained running; restarting only `quay-quay` restored HTTP 200 health on the sixth two-second retry. This was a local environment failure after the successful registry cleanup, not a product-test failure. No live namespace was destroyed; the namespace worker path passed against both SQLite and PostgreSQL test databases.

## Live Story 16 validation

The running `quay-quay` container was bind-mounted from the target worktree. Instance health returned HTTP 200, local PostgreSQL was healthy, and the active allowlist was SHA-256, SHA-384, and SHA-512. Before the test, PostgreSQL contained zero `BlobUpload` rows and zero uploads older than the two-day cleanup threshold, so invoking expiration after aging only the dedicated upload was safe.

The dedicated repository was `localhost:8080/testuser/pqc-story16-live-1788335016`. Four chunked sessions were created: a SHA-512 finalization session, SHA-384 cancellation session, SHA-512 expiration session, and unrelated active SHA-384 session. Read-only PostgreSQL inspection after the chunks showed all four rows with non-null requested hash state and storage metadata, with byte counts 21, 25, 23, and 31 respectively.

Key identities:

- Requested SHA-512: `sha512:f2cc8db58e36410acf437557b30f7d19e5f14f5aa60141ce6cb89637bdaed84807c7d92bb5b68bd8a25ae837f6830106c0d8fd0202dc1552cb7979dbe5d4f2d4`
- Canonical SHA-256: `sha256:a6be1b18cc1f2272cdfc292b9d6cc2471a075ee264f16ec17d1a122687514f5a`

Live outcomes:

- The SHA-512 upload finalized from a second chunk with HTTP 201. Its upload row disappeared, one canonical SHA-256 `ImageStorage` row and one requested SHA-512 repository registration existed, and exact GET bytes independently hashed to the requested SHA-512 value.
- The SHA-384 upload canceled with HTTP 204 and its row, requested state, metadata, and temporary upload disappeared.
- The dedicated SHA-512 abandoned upload was aged by 60 days only after confirming no unrelated stale upload existed. Direct invocation of the real `BlobUploadCleanupWorker._cleanup_uploads` removed its row and local temporary upload.
- The unrelated fresh SHA-384 row and non-null requested state remained after expiration. It was then canceled normally, leaving no dedicated live upload rows.
- The finalized canonical row, requested registration, exact bytes, unrelated content, and instance health remained intact throughout cleanup.

One initial container configuration inspection used `podman exec` without stdin attachment and printed no output. The corrected `python -c` command showed the three-algorithm allowlist. This was a validation-command defect, not a product failure. No configuration was changed. The dedicated finalized repository remains for inspection.

## Active limitations and deferred work

- Proxy cache, repository mirroring, organization mirroring, external image import, and all related external-registry or live interoperability validation are explicitly deferred until reassigned.
- Story 9 remains unimplemented. A partial Story 9 attempt was fully reverted before this handoff update.
- Complete-image copy is supported only between normal repositories managed by the same Quay deployment while the external-registry deferral is active. Artifact-copy and referrer-copy variations remain unvalidated.
- Builds, Clair, UI, garbage collection, conformance, and operational tooling remain later stories.
- Full blob unlink, ordinary registration-aware garbage collection, and physical orphan cleanup remain lifecycle work. Upload cancellation and expiration now remove requested hash state and retry failed targeted storage cleanup. Repository and namespace purge continue to remove upload rows through their existing bulk path; changing repository deletion storage orchestration remains outside Story 16.
- Story 14 expires manifest lifecycle tags but intentionally leaves registrations, canonical rows, graph links, and bytes until repository deletion or later garbage collection. Arbitrary blob DELETE remains unsupported.
- PostgreSQL blob and manifest registration races passed again in Story 13; MySQL concurrency remains unrun.
- SHA-384 resumable hashing passed on local macOS arm64 and an existing Linux aarch64 image. Clean Linux builds, Linux x86_64 packaging, and cross-architecture resume remain unproven.
- Podman and Skopeo rejected SHA-384 manifest pulls client-side in earlier tests; client interoperability remains tool-specific.
- No OCI Distribution conformance, broad client matrix, mixed-version, mixed-region, object-storage, replication, performance, or production-readiness claim is made.
- Deferred items D1-D8 remain deferred unless explicitly reassigned.

## Recommended next story

Proceed with **Story 17: garbage-collect unreferenced digest registrations and canonical content**.

Start with characterization of ordinary manifest and blob garbage collection after tags and temporary `UploadedBlob` links expire. Prove registration-removal ordering, shared canonical-content preservation, retries, concurrency, and repository isolation. Keep physical orphan recovery, Docker schema 1 alternative identities, and deferred integrations outside Story 17 unless the tracker is deliberately revised.
