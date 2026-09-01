# Quay Configurable-Digest Handoff

## Purpose

This file is the current operational snapshot for the next implementation session. It is not a cumulative execution log. Detailed history remains in Git.

The feature goal is to support repository-visible SHA-256, SHA-384, and SHA-512 identities for selected Registry V2 operations while keeping SHA-256 as Quay's canonical internal storage, deduplication, manifest, and graph identity.

## Start here

1. Work only in `/Users/shossain/QuayWorkspace/shaon-feature-PQC`.
2. Use branch `shaon-feature-PQC`.
3. Read `AGENTS.md`, `IMPLEMENTATION_PLAN.md`, and the relevant `agent_docs/` files completely.
4. Recheck branch, HEAD, merge base, status, staging, untracked files, worktrees, recent history, and `master...HEAD` before changing files.
5. Continue the existing implementation. Do not reset, rebase, restore, discard, overwrite, or replace work.
6. Do not access or modify `/Users/shossain/QuayWorkspace/11537-pqc-schema`.
7. Do not push, alter remotes, update pull requests, or modify another worktree.
8. Before modifying files, update only the current session's `.PITASKS.md` section.

## Repository state

- Worktree: `/Users/shossain/QuayWorkspace/shaon-feature-PQC`
- Branch: `shaon-feature-PQC`
- Base branch: `master`
- Base and merge base: `d81004d24669132d45df8fbd1eafc86c149e38fa`
- Current feature HEAD: `b59c747e7482f174dee81508dd3aca363ef7d7e6`
- Current feature subject: `NO-ISSUE: feat(registry): support registered OCI artifact identities`
- This compact handoff is committed separately with subject `NO-ISSUE: docs(registry): compact configurable-digest handoff`. Its hash cannot be embedded in its own Git preimage; use `git rev-parse HEAD` and verify the subject.
- Expected target status after the handoff commit: clean, with no staged, unstaged, or untracked files.
- Merge commits for the independent foundations:
  - PR #6917 configuration: `7b27d59b3acdf5e9172f3ef25720655a667b5852`
  - PR #6918 registration schema: `9352a3d42478d2aa696df4b76805e5dbbe9a8290`
- Main implementation commit: `139a00109080e0236fe8c73820d5700e5d837508`
- Story 4 and Story 5 evidence commit: `844a495009ea9eafb8284ccbbccb2995fd9bd2ee`
- Story 6 evidence commit: `608ee31b6d1747b3e1184511c968b1ed58b88747`
- Story 7 implementation and evidence commit: `b59c747e7482f174dee81508dd3aca363ef7d7e6`

## Delivery status

- Story 1: **In progress**, pending independent evaluation.
- Story 2: **In progress**, pending independent evaluation.
- Story 3: **Done**.
- Story 4: **Done**.
- Story 5: **Done**.
- Story 6: **Done**.
- Story 7: **Done**.
- Recommended next work: **Story 8, referrer discovery through registered digest identities**.

Do not change Story 1 or Story 2 merely because later stories depend on their behavior.

## Non-negotiable identity contracts

1. `ImageStorage.content_checksum` remains canonical SHA-256.
2. `Manifest.digest` remains canonical SHA-256.
3. Alternative identities are stored only in repository-scoped `RepositoryBlobDigest` and `RepositoryManifestDigest` rows.
4. A new alternative-only object does not expose its hidden canonical SHA-256 identity.
5. Canonical SHA-256 remains visible only when explicitly registered, historically visible before the first registration, or intentionally created through a tag or SHA-256 route.
6. Digest parsing is strict. Encoded values are lowercase and exact length.
7. Supported algorithms are SHA-256, SHA-384, and SHA-512. An algorithm must also be active in `ALLOWED_HASH_ALGORITHMS` at the relevant API boundary.
8. Blob and manifest validation hashes exact uploaded or request bytes.
9. Registrations are immutable and idempotent. A repository digest cannot be remapped to different canonical content.
10. Blob, manifest, child, and subject resolution must remain repository-scoped.
11. Graph, registration, quota, pruning, and tag changes must remain transactionally consistent.
12. Cache invalidation must happen after the lifecycle transaction commits.
13. Persisted resumable hash state uses the validated JSON/base64 envelope. Do not introduce pickle or general object deserialization.
14. Mirror-managed repositories retain their established SHA-256-only ingestion boundary unless a dedicated later story changes it.

## Completed behavior at the current feature HEAD

### Configuration and digest handling

- Python and Go configuration accept nonempty unique combinations of `sha256`, `sha384`, and `sha512`.
- The default remains `['sha256']`.
- Shared strict parsing distinguishes malformed, unsupported, and valid-but-disabled algorithms.
- SHA-384 is a Quay extension. OCI Image Specification 1.1.1 does not register it.

### Blob lifecycle

- Enabled SHA-256, SHA-384, and SHA-512 blobs support monolithic, chunked, resumed, and hintless-compatible uploads.
- Exact bytes are validated against the final requested digest while canonical SHA-256 is calculated independently.
- Repository registrations are created only after successful validation.
- GET and HEAD return the requested registered identity and canonical stored bytes.
- Hidden canonical SHA-256 does not resolve for alternative-only blobs.
- Legacy SHA-256 behavior remains available under the established fallback rules.
- Rolling-configuration behavior, persisted resumable state, malformed input, mismatch, conflict, disabled algorithms, and cancellation have focused coverage.

### Blob mounts

- Cross-repository mounts resolve the exact requested digest in the named source repository.
- Private sources require source pull authorization. Destinations require push authorization.
- Unknown or inaccessible valid sources use the normal Distribution HTTP 202 upload fallback without disclosing private content.
- Malformed, unsupported, or disabled mount digests fail immediately.
- Destination link and registration occur in one transaction.
- Mounts reuse the exact canonical `ImageStorage` row and do not copy physical bytes.
- New alternative-only mounts expose only the requested destination identity.

### Manifest and image graph lifecycle

- Single manifests, OCI indexes, and Docker manifest lists support enabled SHA-256, SHA-384, and SHA-512 route identities.
- Manifest PUT validates exact request bytes before publication.
- Config, layer, child, and subject descriptors resolve through registrations in the target repository.
- Mixed SHA-256, SHA-384, and SHA-512 descriptor graphs are supported.
- Descriptor size and media type are checked against persisted content.
- `ManifestBlob`, `ManifestChild`, and canonical subject fields store canonical relationships.
- Parent graph, requested registration, quota accounting, and tag changes share the lifecycle transaction.
- Digest and tag GET and HEAD preserve exact bytes, media type, and the selected repository-visible identity.
- Nested availability and descendant cache invalidation remain repository-scoped.
- Proxy coordination, traversal limits, scanner identity, mirroring command preservation, and selected background paths exist from the main implementation commit, subject to the limitations below.

### OCI artifacts: Story 7

- Digest-addressed OCI artifacts accept enabled SHA-256, SHA-384, and SHA-512 identities.
- Tag-addressed artifact publication remains canonical SHA-256 because a tag route carries no client-selected digest algorithm.
- Artifact config, layer, and subject descriptors are strict-parsed and allowlist-checked before model persistence.
- Subjects resolve through explicit target-repository registrations.
- Unknown, cross-repository, size-mismatched, and media-type-mismatched subjects fail before artifact publication.
- Artifact bytes and `artifactType` are preserved.
- `Manifest.subject` stores the resolved canonical subject SHA-256 internally.
- Alternative-only artifact and subject canonical SHA-256 identities remain hidden.
- Repeated artifact publication is idempotent.
- `create_manifest_with_temp_tag()` returns the requested repository-visible digest rather than hidden canonical SHA-256.
- Digest and tag GET and automatic HEAD return exact artifact bytes and the selected registered artifact identity.

## Story 7 root cause and changed files

Story 7 was blocked by two deliberate SHA-256-only Demo 1 publication gates. One was in the endpoint and one was below registry-model publication. The graph and registration implementation was already capable of repository-scoped alternative artifact and subject identities.

After removing those gates, focused testing found that temporary-tag publication returned a datatype carrying canonical SHA-256 even though the requested alternative registration was persisted. The wrapper now receives `requested_digest`.

Production files changed by Story 7:

- `endpoints/v2/manifest.py`
- `data/model/oci/manifest.py`
- `data/registry_model/registry_oci_model.py`

Story 7 tests:

- `endpoints/v2/test/test_manifest.py`
- `data/registry_model/test/test_interface.py`

`data/model/oci/retriever.py`, `data/registry_model/datatypes.py`, and the other starting files were traced but required no Story 7 change.

## Current Story 8 boundary

Story 7 changed artifact publication and pull only. Referrer discovery remains deliberately limited:

- `endpoints/v2/referrers.py` still rejects SHA-384 and SHA-512 subject queries.
- The current registry model rebuilds native and cached referrer descriptors through repository-visible SHA-256 selection.
- Alternative-only artifacts can be published and pulled directly but are not yet returned through alternative-identity referrer discovery.
- Existing SHA-256 native referrer lookup and `artifactType` filtering continue to work.

Do not infer that Story 7 completed Story 8.

## Relevant verification evidence

### Story 7 focused and regression tests

Artifact and subject baseline before the Story 7 production change:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/v2/test/test_manifest.py -k 'alternative_artifact_identity or alternative_subject_identity or sha256_referrer_query_and_artifact_type_filter'`

Result: **7 passed, 64 deselected**. These were the then-current SHA-256-only boundary tests.

Final Story 7 selection:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/v2/test/test_manifest.py -k 'story7'`

Result: **9 passed, 65 deselected**.

Complete manifest endpoint suite:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/v2/test/test_manifest.py`

Result: **74 passed**.

Registry interface suite:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_interface.py`

Result: **116 passed, 2 skipped**. The skips are the PostgreSQL-only blob and manifest registration races that passed in earlier implementation sessions.

OCI manifest and digest aggregate:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/model/oci/test/test_oci_manifest.py digest/test/test_digest_tools.py`

Result: **77 passed**.

SHA-256 registry protocol push and pull:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings test/registry/registry_tests.py -k 'test_basic_push_pull_by_manifest'`

Result: **3 passed, 1,456 deselected**.

### Static checks

Targeted mypy:

`.venv/bin/mypy data/model/oci/manifest.py data/registry_model/registry_oci_model.py endpoints/v2/manifest.py`

Result: **passed** with no issues.

Compilation and whitespace:

`.venv/bin/python -m compileall -q data/model/oci/manifest.py data/registry_model/registry_oci_model.py data/registry_model/test/test_interface.py endpoints/v2/manifest.py endpoints/v2/test/test_manifest.py && git diff --check && git diff --check master`

Result: **passed**.

Pre-commit over all Story 7 files passed. The first focused invocation reformatted the two test files with Black; subsequent focused and final invocations passed without changes.

### Live Story 7 validation

The local Quay container was restarted with the target worktree bind-mounted. The active live allowlist was `['sha256', 'sha384', 'sha512']`.

A fresh SHA-512 BusyBox OCI index was published as the subject:

- Repository: `localhost:8080/testuser/pqc-story7-01a05e7e`
- Tag: `subject`
- Subject digest: `sha512:0bd23dcfecb44322dd952511fc3c392f2eb652f3eb6dac7c352156a43b782a957b8cf23b26633bffc66c56acbdb82e6f805501661cd55b011e01673a4a72963c`

A direct Registry V2 script then uploaded a SHA-384 config and SHA-512 payload and published a 781-byte OCI artifact:

- Tag: `artifact`
- Artifact digest: `sha384:aba053dc276e8a3cfff16a39c8ed0b38e5253648efec41329e906e716a67a295eda1f38af93246b9da12733dc1b76b09`
- Internal artifact SHA-256: `sha256:ded291334c713cb1a960fe6031a2d7d2e9dbf022d76199d212fa47dfaeb5dca0`
- Internal subject SHA-256: `sha256:9edfb5319801ada456f67d05aeee6fc8946d09731ff8337e873ec3648255ccb8`

Live outcomes:

- Artifact PUT returned HTTP 201 with the requested SHA-384 identity, matching location, and `OCI-Tag: artifact`.
- Artifact GET and HEAD by digest and tag returned HTTP 200, exact bytes, OCI artifact media type, and the SHA-384 identity.
- Config and payload GET and HEAD returned exact bytes through their registered identities.
- Hidden canonical SHA-256 GET requests for artifact, subject, config, and payload returned HTTP 404.
- Database inspection confirmed one artifact registration, one subject registration, two canonical artifact blob edges, and canonical subject storage.
- The live validation did not call the referrers endpoint.
- The live repository remains available. It was not deleted because lifecycle cleanup was outside Story 7.

## Known limitations and deferred work

- Story 8 referrer discovery through registered subject and artifact identities is not implemented.
- Referrer fallback-tag lookup across all repository-visible subject aliases remains unresolved.
- Proxy artifact ingestion cache propagation and alternative referrer cache behavior require Story 8 review if those paths are included.
- Mirror root alternative registration remains unproven for full and architecture-filtered mirroring.
- Mocked Docker schema-1 pull-through tests have an existing upstream digest mismatch. Docker schema-1 remains SHA-256-only.
- Failed coordinated proxy storage work can leave unreferenced canonical bytes. No graph or registration exposes them. Physical orphan cleanup remains lifecycle work.
- Full blob unlink, upload expiration, repository or namespace deletion, concurrent deletion, registration cleanup, and garbage collection remain later lifecycle scope.
- PostgreSQL registration races passed previously. MySQL concurrency has not been run.
- SHA-384 resumable hashing passed on local macOS arm64 and the existing Linux aarch64 Quay image. Clean Linux builds, Linux x86_64 packaging, and cross-architecture resume remain unproven.
- Podman and Skopeo rejected SHA-384 manifest pulls client-side in earlier interoperability testing. ORAS accepted exact SHA-384 manifest bytes. Client interoperability must be evaluated individually.
- No OCI Distribution conformance, broad client matrix, mixed-version, mixed-region, object-storage, replication, performance, or production-readiness claim is made.
- Deferred items D1-D8 remain deferred unless explicitly reassigned.

## Recommended next story

Proceed with **Story 8: referrer discovery through registered digest identities**.

Start with:

- `endpoints/v2/referrers.py`
- `data/registry_model/registry_oci_model.py`
- `data/model/oci/manifest.py`
- `data/registry_model/datatypes.py`
- `endpoints/v2/test/test_manifest.py`
- Relevant referrer cache-key and registry-interface tests

Required Story 8 properties:

1. Strictly parse and allowlist-check the requested subject digest before lookup or cache use.
2. Resolve the subject only through its target-repository registration.
3. Query the canonical internal subject relationship without exposing canonical SHA-256.
4. Return only repository-visible registered artifact identities.
5. Preserve exact artifact bytes, descriptor size, media type, and `artifactType` filtering.
6. Handle subjects and artifacts with mixed SHA-256, SHA-384, and SHA-512 registrations.
7. Keep native and fallback-tag discovery consistent across visible subject aliases.
8. Deduplicate results by canonical artifact while selecting a deterministic enabled external identity.
9. Invalidate filtered and unfiltered caches for every visible subject alias only after publication commits.
10. Reject cross-repository aliases and disabled algorithms even when cache entries were primed while enabled.
11. Preserve existing SHA-256 behavior.
12. Do not expand into proxy cache, mirroring, copying, imports, builds, Clair, UI, deletion, garbage collection, conformance, or operational tooling unless Story 8 correctness requires a narrow supporting change.

Before implementation, run the existing SHA-256 referrer tests and the current alternative-query rejection tests to establish the baseline. Keep Story 1 and Story 2 independently In progress.
