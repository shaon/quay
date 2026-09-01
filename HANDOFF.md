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
- Pre-Story 8 HEAD: `294089883c859342c8e7470c94a346bee5b2adad`
- Pre-Story 8 subject: `NO-ISSUE: docs(registry): compact configurable-digest handoff`
- Story 7 implementation: `b59c747e7482f174dee81508dd3aca363ef7d7e6`
- Story 8 implementation: `bc21299727b875af15fa67a689677e69b9f688ff`
- The external-registry deferral is committed with subject `NO-ISSUE: docs(registry): defer external-registry digest work`. Its hash cannot be embedded in its own Git preimage; use `git rev-parse HEAD` and verify the subject.
- Expected target status after the deferral commit: clean, with no staged, unstaged, or untracked files.
- The planning repository had unrelated modified and untracked files before this update. This deferral update changed only the `Deferred` status definition, the Story 9, Story 11, and Story 12 status cells, and the external-registry scope note in `TODO.md`. The planning repository was not committed.

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
- Recommended next work: **Story 10, limited to copies between repositories managed by this Quay deployment**.

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

## Completed behavior through Story 8

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

### Static checks

Pre-commit passed over every Story 8 target-worktree file. Earlier passes reformatted Python with Black; the final pass made no changes.

Targeted mypy:

`.venv/bin/mypy data/cache/cache_key.py data/model/oci/manifest.py data/registry_model/registry_oci_model.py endpoints/v2/manifest.py endpoints/v2/referrers.py`

Result: **passed**, no issues in five source files.

Compilation and whitespace:

`.venv/bin/python -m compileall -q data/cache/cache_key.py data/cache/test/test_cache.py data/model/oci/manifest.py data/registry_model/registry_oci_model.py data/registry_model/test/test_interface.py endpoints/v2/manifest.py endpoints/v2/referrers.py endpoints/v2/test/test_manifest.py && git diff --check && git diff --check master`

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

## Active limitations and deferred work

- Proxy cache, repository mirroring, organization mirroring, external image import, and all related external-registry or live interoperability validation are explicitly deferred until reassigned.
- Story 9 remains unimplemented. A partial Story 9 attempt was fully reverted before this handoff update.
- Complete-image copy is limited to repositories managed by the same Quay deployment while the external-registry deferral is active.
- Builds, Clair, UI, deletion, garbage collection, conformance, and operational tooling remain later stories.
- Full blob unlink, upload expiration, repository or namespace deletion, registration cleanup, and physical orphan cleanup remain lifecycle work.
- PostgreSQL registration races passed in earlier sessions; MySQL concurrency remains unrun.
- SHA-384 resumable hashing passed on local macOS arm64 and an existing Linux aarch64 image. Clean Linux builds, Linux x86_64 packaging, and cross-architecture resume remain unproven.
- Podman and Skopeo rejected SHA-384 manifest pulls client-side in earlier tests; client interoperability remains tool-specific.
- No OCI Distribution conformance, broad client matrix, mixed-version, mixed-region, object-storage, replication, performance, or production-readiness claim is made.
- Deferred items D1-D8 remain deferred unless explicitly reassigned.

## Recommended next story

Proceed with **Story 10: preserve digest identities when copying content between repositories**, limited to source and destination repositories managed by the same Quay deployment.

Do not include proxy cache, external-registry copy, repository mirroring, organization mirroring, external image import, or live external-registry validation. Those surfaces remain deferred until explicitly reassigned. Preserve Story 8 alias resolution, fallback discovery, cache invalidation, authorization, repository isolation, deterministic descriptor selection, and hidden-canonical behavior.
