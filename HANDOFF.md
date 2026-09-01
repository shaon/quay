# Quay Multi-Algorithm Digest Handoff

## Start here

1. Work only in `/Users/shossain/QuayWorkspace/shaon-feature-PQC`.
2. Read `AGENTS.md` and `IMPLEMENTATION_PLAN.md` completely.
3. Read `agent_docs/api.md`, `agent_docs/database.md`, `agent_docs/architecture.md`, and `agent_docs/testing.md`.
4. Inspect the branch and uncommitted diff before changing anything.
5. Continue the existing work. Do not discard, overwrite, commit, or revert it.

## Strict constraints

- Use branch `shaon-feature-PQC`.
- Do not modify another worktree.
- Do not update PR #6917 or PR #6918.
- Do not open a PR, push, commit, or alter remote branches.
- SHA-256 remains Quay's canonical internal identity.
- Alternative digests remain repository-scoped external identities.

## Current repository state

- Integration worktree: `/Users/shossain/QuayWorkspace/shaon-feature-PQC`
- Integration branch: `shaon-feature-PQC`
- Base branch: `master`
- Base commit and merge base: `d81004d24669132d45df8fbd1eafc86c149e38fa`
- Current committed HEAD: `9352a3d42478d2aa696df4b76805e5dbbe9a8290`
- HEAD subject: `Merge PR #6918 into shaon-feature-PQC`
- PR #6917 local merge: `7b27d59b3acdf5e9172f3ef25720655a667b5852`
- PR #6918 local merge: `9352a3d42478d2aa696df4b76805e5dbbe9a8290`
- Nothing is staged or committed for Handoff 1, Handoff 2, Handoff 3, or Handoff 4.
- There are 27 modified tracked files with 4,569 insertions and 332 deletions relative to HEAD before the final Handoff 4 documentation update.
- `.PITASKS.md`, `HANDOFF.md`, and `IMPLEMENTATION_PLAN.md` are untracked.
- No other worktree, PR, remote branch, or commit was modified.

## Handoff status

### Handoff 1

**Alternative-digest blob lifecycle** remains implemented in the working tree.

Enabled SHA-512 blobs can be uploaded monolithically or in chunks, resumed, validated against the uploaded bytes, registered as repository-scoped external identities, and retrieved through `GET` and `HEAD`. SHA-256 remains the canonical storage identity.

### Handoff 2

**Blob compatibility and mount behavior is complete and has passed final verification and review.**

Completed implementation and focused tests cover:

- Cross-repository SHA-512 mounts through source and destination repository registrations.
- Exact source-blob mounting by database ID and canonical digest.
- Source pull authorization, destination push authorization, public-source behavior, and repository isolation.
- Transactional destination linking and registration.
- Idempotent repeated mounts and uploads.
- Destination registration-conflict rejection without remapping the digest.
- Preservation of legacy SHA-256 mounts and lookups.
- Prevention of unintended destination SHA-256 visibility for alternative-only mounts.
- Hintless chunked SHA-512 compatibility without storage readback.
- Legacy, mixed-version, and mixed-configuration upload-session behavior.
- Explicit PATCH rejection when the stored algorithm is disabled on the current worker.
- Final SHA-256 completion of an alternative session because the final digest is authoritative.
- Precise malformed, unsupported, disabled, unknown, mismatched, and conflict errors.
- Upload UUID, digest header, and location checks for chunked, resumed, monolithic, mounted, and repeated operations.
- A real two-connection PostgreSQL registration race test.
- A real savepoint around registration insertion so an idempotent uniqueness race cannot roll back the caller's canonical link work.
- Explicit cache verification after an alternative mount and rollback verification for destination mount conflicts.

The complete Handoff 1 and Handoff 2 diff has passed final security, transaction, compatibility, and repository-isolation review.

### Handoff 3

**Transactional repository-scoped single-manifest lifecycle is complete and has passed final verification and review.**

Completed implementation and focused tests cover:

- Strict SHA-256 and SHA-512 parsing for manifest digest `GET`, automatic `HEAD`, `PUT`, and `DELETE` routes.
- Exact-request-byte digest verification before persistence.
- SHA-256 as the canonical `Manifest.digest` identity and `RepositoryManifestDigest` as the repository-scoped external identity.
- Alternative-only manifests that do not expose their internal canonical SHA-256 identity.
- Explicit canonical registration when a manifest is pushed by tag or SHA-256 digest.
- Preservation of historically visible SHA-256 identities before the first alternative registration.
- Idempotent retries and immutable registration conflicts.
- Repository-boundary enforcement for both manifest registrations and every local config/layer descriptor.
- Descriptor resolution through repository-scoped blob registrations, including mixed external digest descriptors.
- Atomic graph, registration, quota, and tag changes, with rollback on conflicts and quota rejection.
- Durable quota-error notifications after lifecycle rollback.
- Persisted `ManifestBlob` graph reads instead of reinterpreting external descriptor identities.
- Alternative digest tag assignment, `GET`, `HEAD`, `DELETE`, response headers, and locations.
- Cache invalidation for every registered identity when tags are moved or deleted.
- Proxy-cache registration for a single manifest fetched by an alternative digest.
- Precise malformed, unsupported, disabled, mismatched, conflicting, and unknown-descriptor errors.
- A real two-connection PostgreSQL manifest-registration race with caller transaction work preserved.
- Explicit rejection of alternative manifest-list identities, which remained Handoff 4 scope at Handoff 3 completion.

### Handoff 4

**Repository-scoped multi-algorithm OCI graph support is complete and has passed final verification and adversarial review.**

SHA-512 OCI indexes and Docker manifest lists, mixed child identities, artifacts, subjects, referrers, nested availability, transactional graph/tag registration, descendant cache invalidation, proxy-cache mixed indexes, and manifest-registration GC are implemented. Exact outcomes and remaining secondary-ingestion limits are documented below.

## Design decisions and contracts

### Identity and registration

- `ImageStorage.content_checksum` remains canonical SHA-256.
- `RepositoryBlobDigest` remains the repository-scoped external identity.
- A new alternative-only upload or mount receives only its alternative registration.
- Its internal SHA-256 identity is not exposed through registry lookup.
- If a repository/blob pair already had legacy SHA-256 visibility before its first alternative registration, SHA-256 is first preserved as an explicit registration.
- Mounting SHA-256 explicitly creates a destination SHA-256 registration.
- Registrations are idempotent for the same repository, digest, and canonical content.
- A digest cannot be remapped to different canonical content in the same repository.

### Mount behavior and authorization

- The `mount` digest is strictly parsed and checked against `ALLOWED_HASH_ALGORITHMS` before source lookup.
- The destination route requires write permission.
- A private source requires an exact source pull grant. Public sources retain existing public-read behavior.
- Superuser and global read-only superuser source reads follow the existing feature contracts.
- Source lookup occurs only in the repository named by `from`.
- The exact authorized source `ImageStorage` row is linked into the destination.
- The requested external digest is registered in the destination in the same transaction as the temporary link.
- Unknown source repositories, inaccessible private sources, unknown valid source digests, and omitted `from` continue to fall back to a normal `202` upload response. This preserves Distribution mount fallback behavior without revealing private source contents.
- Malformed, unsupported, and disabled mount digests fail immediately with a registry error instead of falling back.
- A destination digest conflict returns `DIGEST_INVALID` and does not remap the existing registration.

### Hintless uploads and persisted state

- When exactly one supported non-SHA-256 algorithm is configured, currently SHA-512, a hintless upload tracks it from byte zero in parallel with canonical SHA-256.
- This permits a hintless multi-request upload to finalize as SHA-256 or SHA-512 without reading uploaded bytes back from storage.
- The persisted JSON envelope contains the algorithm, architecture, byte order, byte count, origin (`hint` or `hintless`), format version, and base64 native state.
- Restoration validates the persisted byte count against `BlobUpload.byte_count`. This detects chunks accepted by an older worker that did not update the alternative state.
- No pickle or other unsafe deserialization is used.
- An invalid or stale explicit state rejects resume.
- An invalid, stale, incompatible-architecture, or unsupported hintless state degrades to legacy SHA-256 behavior. It cannot later finalize with the discarded alternative identity.
- Legacy sessions with null requested-digest fields remain SHA-256 sessions.
- A legacy empty session may infer an enabled alternative algorithm from its final request because all bytes arrive after inference.
- A legacy session with prior bytes cannot switch to an untracked alternative algorithm.

### Rolling configuration and final digest

- PATCH rejects an explicitly requested algorithm if it is disabled on the current worker. No bytes are accepted.
- Status and cancellation do not restore hash state and remain available when an algorithm is disabled or state is corrupt.
- Hintless alternative tracking may continue internally on a worker where the alternative is disabled. This prevents rolling configuration from breaking legacy SHA-256 clients.
- Finalization still rejects an alternative digest disabled on the current worker.
- A final SHA-256 digest can complete a session originally hinted as SHA-512, even if SHA-512 is now disabled. The final digest is authoritative.

### Registry errors

- Malformed digest: `DIGEST_INVALID`, HTTP 400, detail reason `malformed`.
- Unsupported algorithm: `UNSUPPORTED`, HTTP 400, detail reason `unsupported`.
- Disabled algorithm: `UNSUPPORTED`, HTTP 400, detail reason `disabled`.
- Uploaded-content mismatch or registration conflict: `DIGEST_INVALID`, HTTP 400.
- Unknown valid blob: `BLOB_UNKNOWN`, HTTP 404.
- Unknown upload UUID or wrong repository: `BLOB_UPLOAD_UNKNOWN`, HTTP 404.
- Invalid or incompatible persisted explicit state: `BLOB_UPLOAD_INVALID`, HTTP 400.

### Response contract

- Upload start returns `202`, `Docker-Upload-UUID`, upload `Location`, and `Range`.
- PATCH returns `202`, the same upload UUID, current upload `Location`, and `Range`.
- Upload status returns `204`, the same upload UUID, current upload `Location`, and `Range`.
- Successful finalization returns `201`, the authoritative external digest, and blob `Location`.
- Successful mount returns `201`, the mounted external digest, and destination blob `Location`.
- Successful monolithic and repeated finalizations do not invent an upload UUID response header.
- Alternative `GET` and `HEAD` return the requested registered external digest.

### Single-manifest identity and descriptors

- A manifest digest route is parsed strictly before model lookup. Only lowercase, exact-length SHA-256 and SHA-512 values are accepted.
- A digest `PUT` hashes the exact request bytes with the requested algorithm. The body is never normalized before this validation.
- `Manifest.digest` remains the canonical SHA-256 identity used by Quay's internal graph.
- `RepositoryManifestDigest` maps one repository-scoped external digest to that canonical manifest. The registration helper rejects cross-repository manifest rows and digest remapping.
- A new alternative-only manifest receives no canonical registration. A tag push or explicit SHA-256 digest push intentionally registers SHA-256.
- A legacy canonical identity that was already visible is explicitly preserved before the first alternative registration.
- Every config and layer descriptor is strictly parsed, checked against the enabled algorithm set, and resolved through that repository's blob registrations before graph writes.
- Unknown or cross-repository descriptors return `MANIFEST_BLOB_UNKNOWN` with the descriptor digest.
- `ManifestBlob` stores canonical blob relationships. Later graph reads use those persisted relationships rather than re-resolving external descriptor strings.

### Single-manifest transactions, cache, and proxy behavior

- Manifest graph creation, external registration, quota accounting, and tag assignment run in one production transaction.
- Registration insertion uses a nested Peewee `atomic()` savepoint. An idempotent unique-index race does not roll back the caller's work.
- Quota rejection rolls back graph, registration, quota, and tag rows. Its durable error notification is created after rollback.
- Cache invalidation covers the canonical digest and every repository registration for both the old and new manifest when tags move, and for affected manifests when tags are deleted.
- Proxy-cache digest pulls register the requested identity only after the single-manifest placeholder graph and temporary tag exist. A previously visible canonical identity is preserved when necessary.
- Alternative manifest-list identities are rejected until Handoff 4 supplies complete child identity and graph traversal semantics.

### Manifest response and error contract

- Successful digest `PUT` returns `201`, the requested digest in `Docker-Content-Digest`, and a digest `Location` using that same identity.
- Digest `GET` and automatic `HEAD` return the exact persisted manifest bytes, original media type, and requested registered identity.
- Tag `PUT` and tag `GET` continue to return canonical SHA-256.
- Malformed manifest references or descriptors return `DIGEST_INVALID` with reason `malformed`.
- Unsupported and disabled algorithms return `UNSUPPORTED` with reasons `unsupported` and `disabled` respectively.
- Exact-byte mismatch and registration conflict return `DIGEST_INVALID` with reasons `mismatch` and `conflict`.
- An unknown valid manifest identity returns `MANIFEST_UNKNOWN`. An unknown local descriptor returns `MANIFEST_BLOB_UNKNOWN`.

## Files changed

### Handoff 1 and shared implementation

- `requirements.txt`
- `mypy.ini`
- `digest/digest_tools.py`
- `digest/test/test_digest_tools.py`
- `data/model/__init__.py`
- `data/model/blob.py`
- `data/model/oci/blob.py`
- `data/registry_model/blobuploader.py`
- `data/registry_model/datatypes.py`
- `data/registry_model/interface.py`
- `data/registry_model/registry_oci_model.py`
- `data/registry_model/test/test_blobuploader.py`
- `data/registry_model/test/test_interface.py`
- `endpoints/v2/blob.py`
- `endpoints/v2/test/test_blob.py`

### Added to the tracked diff during Handoff 2

- `endpoints/v2/errors.py`

### Added to the tracked diff during Handoff 3

- `data/model/oci/manifest.py`
- `data/model/oci/test/test_oci_manifest.py`
- `data/registry_model/registry_proxy_model.py`
- `data/registry_model/test/test_registry_proxy_model.py`
- `endpoints/test/shared.py`
- `endpoints/v2/manifest.py`
- `endpoints/v2/test/test_manifest.py`

Handoff 3 also extends shared files already modified by Handoff 1 and Handoff 2: `data/model/__init__.py`, `data/registry_model/interface.py`, `data/registry_model/registry_oci_model.py`, `data/registry_model/test/test_interface.py`, `digest/digest_tools.py`, and `digest/test/test_digest_tools.py`.

### Local planning and task state

- `.PITASKS.md`
- `HANDOFF.md`
- `IMPLEMENTATION_PLAN.md`

No schema or migration file was changed during Handoff 2.

## Verification completed in Handoff 2

### Focused SQLite behavior suite

Command:

`TEST=true PYTHONPATH=. .venv/bin/pytest digest/test/test_digest_tools.py data/registry_model/test/test_blobuploader.py data/registry_model/test/test_interface.py endpoints/v2/test/test_blob.py -q --tb=short`

Result: **201 passed, 1 skipped**.

The skipped test is intentionally PostgreSQL-only live concurrency coverage. It was run separately against PostgreSQL and passed.

### Broad blob, proxy-cache, migration, field, and configuration regression suite

Command:

`TEST=true PYTHONPATH=. .venv/bin/pytest digest/test/test_digest_tools.py data/model/test/test_blob.py data/model/test/test_model_blob.py data/registry_model/test/test_blobuploader.py data/registry_model/test/test_interface.py endpoints/v2/test/test_blob.py data/migrations/test/test_repository_digest_registration.py data/test/test_fields.py util/config/test/test_schema.py -q --tb=short`

Result: **283 passed, 1 skipped** with existing deprecation warnings.

The skip is the PostgreSQL-only live race test. Migration upgrade/downgrade coverage passed as part of this command.

### Live PostgreSQL concurrency test

Infrastructure: existing local `quay-db` PostgreSQL container on `localhost:5432`. The test fixture created an isolated PostgreSQL schema.

Command:

`TEST=true SKIP_DB_SCHEMA=true PYTHONPATH=. TEST_DATABASE_URI='postgresql://quay:quay@localhost:5432/quay' .venv/bin/pytest data/registry_model/test/test_interface.py::test_repository_digest_registration_live_concurrency -q --tb=short`

Result: **1 passed**.

This is a real two-connection PostgreSQL unique-index race. Both workers passed the initial absence check before insertion. One insert won and the other resolved the unique conflict idempotently through the transaction/savepoint path. Both workers also wrote an outer-transaction marker before registration, and both markers survived, proving that the losing registration savepoint did not roll back caller work. This is not simulated concurrency.

No live MySQL race was run.

### Registry protocol regressions

Command:

`TEST=true PYTHONPATH=. .venv/bin/pytest test/registry/registry_tests.py -k 'test_chunked_blob_uploading or test_blob_mounting' -q --tb=short`

Result: **268 passed, 1191 deselected** with existing warnings.

This covered existing SHA-256 chunking and mount behavior across the registry protocol fixtures.

### Pre-commit

First command:

`.venv/bin/pre-commit run --files requirements.txt mypy.ini digest/digest_tools.py digest/test/test_digest_tools.py data/model/__init__.py data/model/blob.py data/model/oci/blob.py data/registry_model/blobuploader.py data/registry_model/datatypes.py data/registry_model/interface.py data/registry_model/registry_oci_model.py data/registry_model/test/test_blobuploader.py data/registry_model/test/test_interface.py endpoints/v2/blob.py endpoints/v2/errors.py endpoints/v2/test/test_blob.py`

Initial result: Black reformatted six files and isort reformatted one test file, so the command exited nonzero as expected for modifying hooks. Flake8 and all other applicable hooks passed.

The exact same command was rerun.

Final result: **all applicable hooks passed**.

The exact command was run again after the final review fixes. Result: **all applicable hooks passed** without modifying files.

### Mypy

Command:

`.venv/bin/mypy digest/digest_tools.py data/model/blob.py data/model/oci/blob.py data/registry_model/blobuploader.py data/registry_model/datatypes.py data/registry_model/registry_oci_model.py endpoints/v2/blob.py endpoints/v2/errors.py`

Result: **passed**, no issues in eight source files.

### Compilation and whitespace

Command:

`.venv/bin/python -m compileall -q digest/digest_tools.py data/model/blob.py data/model/oci/blob.py data/registry_model endpoints/v2/blob.py endpoints/v2/errors.py && git diff --check`

Result: **passed**.

A later standalone `git diff --check` also passed before this handoff update.

### Go validation

Command:

`go test ./...`

Result: **platform failure on macOS**. Every package except `internal/migrate` passed. The only failing test was `internal/migrate.TestInPostgresNetworkNamespace`, because `/proc/self/ns/net` does not exist on macOS. The same result was confirmed after the final edits. This is not a product failure.

Fallback command:

`go test ./internal/migrate -skip '^TestInPostgresNetworkNamespace$'`

Result: **passed**.

Command:

`go vet ./...`

Result: **passed** with no output. The same result was confirmed after the final edits.

## Verification completed in Handoff 3

### Focused manifest, model, interface, and proxy-cache suite

Command:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q digest/test/test_digest_tools.py data/model/oci/test/test_oci_manifest.py data/registry_model/test/test_interface.py data/registry_model/test/test_registry_proxy_model.py endpoints/v2/test/test_manifest.py`

Result: **239 passed, 2 skipped**. The skips were the two PostgreSQL-only registration race tests, which passed separately against live PostgreSQL.

A final endpoint/proxy rerun after strict `DELETE` handling and proxy-cache review passed: **72 passed**. The complete SHA-512 manifest lifecycle test, including alternative digest deletion, also passed independently after its final change.

### Broad blob, quota, migration, GC, and proxy regressions

Commands and results:

- Blob endpoint, uploader, and digest migration suite: **58 passed**.
- Quota enforcement and garbage collection suite: **48 passed, 2 expected failures, 1 expected-pass result**.
- Full registry proxy model coverage was included in the focused suite and passed.
- Existing registry protocol manifest selection: **55 passed, 1,404 deselected**.

The quota regression includes the revised transactional rejection contract: a quota-rejected push leaves no manifest graph, registration, or tag, while its quota-error notification is created after rollback.

### Live PostgreSQL migrations and concurrency

Infrastructure: an existing local PostgreSQL 18 server on `127.0.0.1:5432`. The session created the isolated temporary database `quay_handoff3_01a03548`, enabled `pg_trgm`, upgraded it through Alembic head, ran the tests, and dropped the database.

Result: **2 passed**:

- `test_repository_digest_registration_live_concurrency`
- `test_repository_manifest_digest_registration_live_concurrency`

Both tests used two real database connections, synchronized both workers after the initial absence check, raced the unique insert, and verified that both caller-transaction markers survived. This proves that the losing idempotent registration uses a savepoint rather than rolling back caller work.

The first manifest-race attempt correctly exposed an invalid test fixture that selected a manifest outside the tested repository. The fixture now selects a manifest from the target repository, and the real race passes. No live MySQL race was run.

### Static checks

- Pre-commit on all Handoff 3 files: **all applicable hooks passed** after Black and isort's initial formatting pass.
- Mypy on the digest, manifest model, registry interface/model, proxy model, and endpoint sources: **passed**, no issues.
- Python compilation and `git diff --check`: **passed**.

### Go validation

- `go vet ./...`: **passed** with no output.
- `go test ./...`: every package except `internal/migrate` passed. The sole failure was the existing macOS-only absence of `/proc/self/ns/net` in `TestInPostgresNetworkNamespace`.
- `go test ./internal/migrate -skip '^TestInPostgresNetworkNamespace$'`: **passed**.

## Intermediate failures already resolved

These are not current product failures:

- An early focused test run failed two legacy mount cases because those fixtures used the malformed string `sha256:unknown` to represent an unknown digest. The implementation correctly returned the new malformed-digest error. The tests now use a valid unknown SHA-256 digest and preserve the required `202` fallback.
- One new stale-hintless-state test exposed use of `_replace` on Quay's custom datatype. The manager now rebuilds the datatype through its supported dictionary representation. The focused suite passes.
- The first pre-commit invocation reformatted files. The second invocation passed fully.
- A new SQLite rollback assertion initially failed because Quay's SQLite test configuration deliberately uses `FakeTransaction`. The test now temporarily uses the production transaction factory and proves the mount link rolls back. This was a test-harness artifact, not a product failure.
- Final review found that `register_repository_blob_digest()` used nested `db.transaction()` while claiming savepoint behavior. Peewee rolls back the entire outer transaction on an inner uniqueness error. The insertion now uses `db.atomic()`, and the strengthened live PostgreSQL race proves both workers' outer transaction markers survive.
- Two focused-suite attempts failed during collection because isort had moved `test.fixtures` below `data.registry_model` in `test_blobuploader.py`, exposing a circular app-initialization dependency. The test now initializes `app` explicitly. The file alone and the exact focused suite pass. This was test code, not a runtime product failure.

## Final full-diff review

The complete uncommitted diff was reviewed against the required invariants:

- **Authorization:** destination writes remain protected by `require_repo_write`; private source mounts require the exact source pull grant; public, superuser, and global read-only source-read behavior follows existing permission contracts.
- **Repository isolation:** alternative lookups include the repository ID and require an `UploadedBlob` or `ManifestBlob` link in that same repository. Upload UUID lookup, update, deletion, and commit also verify repository identity.
- **Transactions and conflicts:** mount linking and destination registration share one transaction; conflict tests prove the existing mapping is unchanged and the temporary source link rolls back. Registration insertion now uses a true nested savepoint, remains idempotent, and cannot remap a digest.
- **Rolling uploads:** legacy null fields remain SHA-256 compatible; stale explicit state fails closed; stale hintless state degrades to SHA-256; unsupported future implicit state is discarded; disabled explicit PATCH requests accept no bytes.
- **Final digest:** the final request digest determines validation and registration. A final SHA-256 digest can complete a previously SHA-512-hinted session when SHA-512 is disabled.
- **SHA-256 and proxy cache:** legacy SHA-256 lookup and protocol tests pass. Existing SHA-256 visibility is preserved before adding an alternative registration. A proxy-fetched single manifest is registered under the requested alternative digest without exposing a new canonical identity. Alternative descriptor ingestion through proxy cache remains deferred.
- **Identity exposure:** new alternative-only uploads and mounts register only the external digest. Canonical SHA-256 remains internal and does not resolve unless legacy visibility existed or SHA-256 was explicitly requested.
- **Persisted state safety:** state uses a validated JSON/base64 envelope and native fixed-format hash state. No pickle or general object deserialization is used. Algorithm, format, architecture, byte order, origin, and byte count are checked before restoration.
- **Cache behavior:** repository blob cache keys include repository and digest. Missing values are not cached, so new registrations are immediately visible. Positive mappings are immutable because remapping is rejected.

- **Manifest transactions:** descriptor resolution precedes graph writes. Canonical graph rows, external registration, quota accounting, and tag changes share a production transaction. Conflict and quota tests prove rollback. Quota-error notification persistence occurs after rollback.
- **Manifest responses and errors:** `PUT`, `GET`, `HEAD`, tag assignment, and `DELETE` preserve requested/canonical header contracts. Strict parser failures are distinguished from unknown valid identities and unknown descriptors.
- **Manifest cache behavior:** tag retarget and deletion invalidate canonical and all registered external identity keys. Proxy-cache single-manifest registration is immediately resolvable.

No unresolved blocking finding remains for Handoff 3.

## Handoff 4 completion

**Repository-scoped multi-algorithm OCI graph support is implemented and has passed final verification and adversarial review.**

### Behavior and contracts

- SHA-512 OCI indexes and Docker schema 2 manifest lists are accepted by digest after exact request-byte verification. SHA-256 remains the canonical `Manifest.digest`.
- Parent, child, and subject identities resolve through `RepositoryManifestDigest` in the same repository. A registration in another repository never satisfies a descriptor.
- Mixed SHA-256 and SHA-512 child descriptors are supported. Child and subject descriptor size and media type are checked before graph persistence.
- Unknown blobs, children, and subjects fail before parent graph or registration visibility. Descriptor mismatches return a manifest-invalid error. Registration conflicts cannot remap an identity.
- `ManifestChild`, `ManifestBlob`, canonical subject, repository registration, quota work, and tag assignment share the production lifecycle transaction. Existing rows are repaired idempotently when retrying an interrupted or raced write.
- Subject digests are stored canonically internally. Referrer descriptors use a deterministic repository-visible identity and do not expose an unregistered canonical SHA-256.
- Referrer cache invalidation covers every registered subject alias plus the internal canonical cache key. Filtered and unfiltered referrer caches remain separate.
- Manifest availability walks the complete ancestor graph, including nested indexes. Tag retarget and deletion invalidate all registered identities for the affected parent and every descendant.
- Digest `GET`, `HEAD`, tag assignment, and deletion preserve the requested external identity in response headers and locations while returning exact stored bytes.
- Proxy-cache digest ingestion validates the requested parent digest. Mixed indexes may use an alternative child only after that child is cached and registered in the repository. Tag-based proxy insertion explicitly registers canonical identity. Existing SHA-256 placeholder behavior remains compatible.
- GC explicitly removes `RepositoryManifestDigest` rows before deleting a manifest. This is safe on migrated production schemas and on test/development schemas that do not reproduce the migration's cascade.
- Cache invalidation runs only after the lifecycle transaction commits, preventing another request from repopulating pre-commit state.

### Handoff 4 files

- `data/model/oci/manifest.py`
- `data/model/oci/retriever.py`
- `data/model/gc.py`
- `data/registry_model/datatypes.py`
- `data/registry_model/registry_oci_model.py`
- `data/registry_model/registry_proxy_model.py`
- `endpoints/v2/manifest.py`
- `endpoints/v2/referrers.py`
- `data/model/test/test_gc.py`
- `data/registry_model/test/test_registry_proxy_model.py`
- `endpoints/v2/test/test_manifest.py`

### Verification completed in Handoff 4

- Consolidated digest, manifest-model, registry-interface, proxy-cache, and endpoint suite: **245 passed, 2 skipped**. The two skips were the PostgreSQL-only races run separately.
- Full manifest endpoint suite after final transaction and cache changes: **25 passed**.
- Full registry proxy model suite after final proxy compatibility changes: **53 passed**.
- Full GC suite after registration cleanup changes: **41 passed**.
- Repository digest migration tests: **2 passed**.
- Quota and namespace-quota regressions: **31 passed**.
- Registry protocol manifest selection: **55 passed, 1,404 deselected**.
- Live PostgreSQL two-connection registration races: **2 passed** against isolated database `quay_handoff4_01a03581`; the database was dropped afterward. Both blob and manifest registration races preserved both caller-transaction markers and produced one registration.
- Pre-commit across all 27 modified tracked files: **passed**. Black and isort reformatted files on the first invocation; the final invocation passed every applicable hook.
- Targeted mypy over digest, manifest, GC, datatype, interface/model, proxy, and endpoint sources: **passed**, no issues.
- Python compilation, `git diff --check`, branch, HEAD, merge-base, staging, status, and worktree checks: **passed**.
- `go vet ./...`: **passed**.
- `go test ./...`: all packages except `internal/migrate` passed. The only failure was the existing macOS absence of `/proc/self/ns/net` in `TestInPostgresNetworkNamespace`.
- `go test ./internal/migrate -skip '^TestInPostgresNetworkNamespace$'`: **passed**.

### Adversarial review findings resolved

- Child retrieval originally bypassed repository registrations. It now resolves through the repository-scoped manifest lookup.
- Subjects originally retained the external digest in the canonical database field. They now resolve and store canonical identity.
- Referrer descriptors originally exposed canonical SHA-256. They now use repository-visible identities.
- Availability and cache invalidation originally handled one graph level. Both now traverse nested graphs completely and protect against cycles.
- Cache invalidation originally occurred before the outer tag transaction committed. It now runs after commit.
- Duplicate previous/current manifest inputs caused query-count regressions. Cache traversal now deduplicates each level.
- Proxy-cache tag ingestion did not explicitly register canonical identity and did not validate alternative child descriptors. Both are fixed while retaining historical SHA-256 placeholder compatibility.
- Development/test schemas could retain manifest registrations during GC. GC now deletes them explicitly and has focused coverage.
- The legacy GC repository-scope test constructed a cross-repository subject through the normal write API. New writes correctly reject it; the test now inserts the legacy/corrupt row directly and continues to verify defensive repository scoping.

No unresolved blocking Handoff 4 finding remains.

## Known limitations and unresolved risks

- The live race test covers PostgreSQL idempotent registration concurrency and preservation of caller transaction work. It does not cover MySQL, concurrent conflicting-content registration, concurrent mounts, or mount rollback races.
- Hintless multi-algorithm tracking currently relies on there being exactly one configured supported alternative algorithm. Quay currently supports only SHA-512 as the alternative. A future configuration with multiple alternatives will require an explicit hint or a multi-state persistence design.
- A corrupt or incompatible hintless alternative state deliberately falls back to SHA-256 compatibility. It cannot later produce an alternative registration.
- A corrupt or incompatible explicit alternative state is rejected.
- Persisted native hash state is architecture-specific. Explicit sessions cannot resume across incompatible architectures.
- The native `resumablehash` dependency remains pinned to Git commit `94242192899e91306271b2cd8be7d66a570e92b7`. Production builders require Git and a native-extension toolchain unless packaging changes later.
- Direct-push manifest lists, OCI indexes, artifacts, subjects, referrers, nested traversal, proxy-cache mixed indexes, and manifest-registration GC are implemented.
- Proxy cache fails closed when an alternative child has not already been cached and registered. Coordinated upstream child fetching, mirroring, imports, copy paths, background repair, and scanner-facing external identities remain Handoff 5 work.
- Existing SHA-256 proxy placeholders retain historical permissive size behavior for compatibility. Newly supported alternative proxy children require exact cached descriptor size and media type.
- MySQL concurrency was not run. The live race coverage used PostgreSQL 18 and the SQLite suite.
- Wide or deeply nested graphs require additional database queries during availability checks and cache invalidation. The common directly tagged manifest path retains its one-query availability behavior, and the legacy endpoint query-count test passes.
- Full blob unlink, repository/namespace deletion, upload cancellation, concurrent deletion, and all remaining registration cleanup paths remain Handoff 6 work.
- No Playwright test was added. The affected behavior is registry protocol API behavior and is covered by endpoint and registry protocol suites.

## Recommended next starting point

### Start Handoff 5 in a new session

Handoff 4 is closed. Handoff 5 is **secondary ingestion paths**. Start with:

1. Repository mirroring and proxy-cache orchestration that fetches alternative children before a parent index.
2. Cross-repository copy operations and import paths.
3. Background ingestion and repair jobs.
4. Security-scanner interactions that pass externally visible digest identities.
5. Retry and failure behavior that cannot create conflicting or partial registrations.

Reuse the Handoff 4 repository-scoped descriptor resolvers and canonical graph persistence. Do not make canonical SHA-256 externally visible unless it is explicitly registered, historically visible, or created through a tag/SHA-256 path. Preserve the fail-closed proxy rule for uncached alternative children until coordinated child ingestion is transactional.

## Handoff 5 implementation status

**Secondary ingestion support is implemented, but the post-compaction adversarial re-review below supersedes the earlier completion verdict. Confirmed correctness and reliability gaps remain.**

### Behavior and contracts

- Proxy cache validates fetched manifest bytes against the exact requested or upstream-advertised digest. Disabled, malformed, mismatched, or unavailable identities fail before graph persistence.
- A proxy root with a directly visible alternative manifest, blob, child, or subject identity triggers full descendant-first fetching. Descendants and blobs are validated and registered before the parent is exposed.
- Coordinated proxy registration, graph rows, temporary or visible tags, quota accounting, and any quota-driven pruning run in one outer database transaction. A failed descendant cannot expose a partial parent graph or lifecycle side effect. Storage bytes left by a failed transaction are unreferenced and remain eligible for normal cleanup.
- Existing all-SHA-256 proxy roots retain historical lazy child behavior. This avoids forcing eager downloads for legacy indexes. A directly visible alternative child remains fail-closed and coordinated.
- Proxy blob lookups and the proxy blob worker resolve alternative identities only through repository-local registrations. Another repository's alias or a global canonical checksum cannot satisfy the request.
- When an existing tagged SHA-256 manifest later gains an upstream alternative alias, both its historically visible SHA-256 identity and the validated new alias are explicitly registered. The alias cannot hide the old identity or remap to another manifest.
- Skopeo mirror copies now use `--preserve-digests` for complete and filtered copy paths. Destination authorization, digest validation, registration, and graph persistence continue through the registry API. All mirror worker command expectations cover the flag.
- Legacy manifest-builder import explicitly supplies the manifest's SHA-256 requested identity. Hintless and cross-repository copy behavior continues through the Handoff 1–4 upload, mount, and registry API contracts rather than bypassing registration logic.
- Scanner indexing, report lookup, report caching, and V2 indexing use a stable repository-visible manifest identity. Alternative-only content is not sent to Clair under hidden canonical SHA-256.
- Scanner report existence checks understand repository digest registrations. GC removes reports for registered identities and uses canonical fallback only for legacy manifests with no registrations, so cleanup does not disclose a hidden canonical identity.
- Subject backfill resolves the descriptor in the manifest's repository and stores only canonical subject identity internally. Unknown or cross-repository subjects remain pending for retry.
- Digest lookup datatypes retain the requested repository-visible identity. Referrer model queries canonicalize that identity repository-locally before matching the internal subject field, preserving external response identity without breaking canonical graph lookup.
- Scanner layer enumeration verifies that resolved blobs are actually connected to the candidate manifest. An unrelated repository blob cannot make an incomplete manifest appear indexable.

### Handoff 5 files

- `data/model/gc.py`
- `data/model/oci/manifest.py`
- `data/model/test/test_gc.py`
- `data/registry_model/manifestbuilder.py`
- `data/registry_model/registry_oci_model.py`
- `data/registry_model/registry_proxy_model.py`
- `data/registry_model/test/test_registry_proxy_model.py`
- `data/secscan_model/secscan_v4_model.py`
- `data/secscan_model/secscan_v4_model_v2.py`
- `data/secscan_model/test/test_secscan_v4_model.py`
- `util/repomirror/skopeomirror.py`
- `util/test/test_skopeomirror.py`
- `workers/manifestsubjectbackfillworker.py`
- `workers/proxycacheblobworker.py`
- `workers/repomirrorworker/test/test_repomirrorworker.py`
- `workers/test/test_manifestsubjectbackfillworker.py`

### Verification completed in Handoff 5

- Consolidated digest, manifest model, registry interface, proxy, and manifest endpoint suite: **250 passed, 2 skipped**. The skips are the PostgreSQL-only races run separately.
- Full proxy model suite: **58 passed** after the final alias, quota, and formatting changes.
- Clair V4 and V4 V2 suites: **107 passed**.
- Mirror worker suite: **53 passed**. Skopeo unit coverage excluding external integration calls: **10 passed, 1 skipped, 2 deselected**.
- Secondary worker, mirror, proxy worker, subject backfill, and legacy manifest builder aggregate: **78 passed, 1 skipped, 2 deselected**.
- GC and quota aggregate: **72 passed**. Focused scanner-cleanup identity coverage also passed.
- Blob uploader and endpoint unit run reached **54 passing tests**; two `@pytest.mark.e2e` Docker Hub pull-through tests failed because the local external-storage/network setup could not retrieve the remote blob. They are not unit regressions and were excluded from completion results.
- The two live PostgreSQL registration races passed against PostgreSQL 18 in isolated database `quay_handoff5_01a03717`. Alembic upgraded through `a2f338ee672c`; the database was dropped afterward.
- Pre-commit across all modified tracked files passed after Black reformatted four files.
- Targeted mypy passed with no issues in 20 source files. Python compilation and `git diff --check` passed.
- `go vet ./...` passed. `go test ./...` passed except for the existing macOS-only `TestInPostgresNetworkNamespace`, where `/proc/self/ns/net` does not exist. `go test ./internal/migrate -skip '^TestInPostgresNetworkNamespace$'` passed.
- External Skopeo integration tests were not run because `/usr/bin/skopeo` is absent. Command construction and all mirror worker paths are covered with mocks.

### Adversarial review findings resolved

- Initial proxy coordination eagerly fetched every SHA-256 index child and broke historical lazy behavior. Coordination now starts only when the root directly exposes an alternative identity, while coordinated graphs still traverse fully.
- A stale tag could add an alternative registration and unintentionally disable legacy SHA-256 fallback. The transition now explicitly preserves the historically visible canonical registration.
- Digest lookup began returning the external identity, exposing that referrer queries compared external identity directly with canonical internal subject fields. Referrer lookup now canonicalizes repository-locally before querying.
- Scanner layer resolution could accept an unrelated repository blob after a `ManifestBlob` link was removed. Layer lookup now requires linkage to the candidate manifest.
- Scanner GC initially included canonical SHA-256 unconditionally. It now sends only registered identities, using canonical fallback solely for legacy manifests with no registrations.
- Coordinated proxy ingestion initially bypassed proxy quota checks. Quota checks and pruning now execute inside the graph transaction.
- V2 scanner tests patch the local `ManifestDataType` symbol. The compatibility import remains while scanner wrapping is shared through the repository-visible identity helper.

### Remaining limits and Handoff 6 starting point

- A SHA-256 root remains lazy. Alternative identities hidden only inside a not-yet-fetched SHA-256 child are coordinated when that child is requested; they are not discoverable from the parent descriptor alone.
- Mirror synchronization across multiple independent tags is not globally atomic. Each destination registry push is transactional under the existing registry lifecycle contract.
- Failed proxy storage writes can leave unreferenced canonical bytes after database rollback. No graph, registration, quota row, or visible tag points to them; normal upload expiration and GC handle physical cleanup.
- MySQL concurrency was not run. SQLite coverage and live PostgreSQL two-connection races passed.
- Handoff 6 should audit unlink, upload cancellation and expiration, repository and namespace deletion, concurrent deletion/GC, and physical orphan cleanup for both blob and manifest registrations.


## Handoff 5 post-compaction adversarial re-review

The post-compaction adversarial review is in progress. Static inspection and focused dynamic reproduction are complete. Six behavior defects and the coordinated-ingestion coverage gap were confirmed. Implementation fixes have not been applied.

Do not treat the earlier Handoff 5 completion statement as the final review verdict. The current recommendation is **Request changes** because material proxy, cache, referrer, and mirroring gaps remain.

No implementation files were changed during the re-review or this document consolidation. Dynamic reproductions were created only under `/tmp`.

## Repository state pinned for review

- Worktree: `/Users/shossain/QuayWorkspace/shaon-feature-PQC`
- Branch: `shaon-feature-PQC`
- HEAD: `9352a3d42478d2aa696df4b76805e5dbbe9a8290`
- Merge base: `d81004d24669132d45df8fbd1eafc86c149e38fa`
- Staged files: none
- The intentionally uncommitted Handoffs 1–5 diff remains present.
- No commit, push, rebase, reset, remote change, or worktree change was performed.

The Handoff 4 baseline was reconstructed by applying `/tmp/shaon-feature-PQC-handoff1-4.diff` to HEAD in `/tmp/pqc-handoff4-baseline`. The current Handoff 5 delta was then compared against that reconstructed baseline.

## Dynamic reproduction evidence

- Existing proxy baseline: `TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short data/registry_model/test/test_registry_proxy_model.py` — **58 passed**.
- Temporary coordinated-ingestion reproductions: `TEST=true PYTHONPATH=. .venv/bin/python -m pytest --rootdir=. -q --tb=short /tmp/test_pqc_handoff5_repro.py` — **4 failed as expected**, confirming placeholder promotion failure, blob download inside the transaction, duplicate child fetches, and absent model-cache propagation.
- The placeholder path raised `InvalidImageException` before `_download_blob`; this refines the static explanation. The repository resolver does not return usable placeholder content because `get_storage_by_uuid` requires a placement.
- A depth-1,100 temporary graph failed with raw `RecursionError` instead of a controlled limit error.
- Actual proxy artifact persistence made a referrer visible through an uncached lookup, while a primed empty referrer cache remained empty.
- An actual SHA-512 subject alias failed to find an OCI fallback index tagged under its visible canonical SHA-256 alias.
- The sparse mirror root helper made only a tag-addressed manifest PUT; it did not first register the source SHA-512 root identity.
- Local PostgreSQL 18 is available on port 5432 for transaction-sensitive follow-up. `/usr/bin/skopeo` is absent, so real full-copy Skopeo behavior remains infrastructure-blocked.

## Static review scope

The Handoff 5 delta contains these implementation and test paths:

- `data/model/gc.py`
- `data/model/oci/manifest.py`
- `data/model/test/test_gc.py`
- `data/registry_model/manifestbuilder.py`
- `data/registry_model/registry_oci_model.py`
- `data/registry_model/registry_proxy_model.py`
- `data/registry_model/test/test_registry_proxy_model.py`
- `data/secscan_model/secscan_v4_model.py`
- `data/secscan_model/secscan_v4_model_v2.py`
- `data/secscan_model/test/test_secscan_v4_model.py`
- `util/repomirror/skopeomirror.py`
- `util/test/test_skopeomirror.py`
- `workers/manifestsubjectbackfillworker.py`
- `workers/proxycacheblobworker.py`
- `workers/repomirrorworker/test/test_repomirrorworker.py`
- `workers/test/test_manifestsubjectbackfillworker.py`

The review also traced relevant unchanged callers and callees in:

- `data/model/oci/blob.py`
- `data/registry_model/registry_oci_model.py`
- `endpoints/v2/manifest.py`
- `endpoints/v2/referrers.py`
- `workers/repomirrorworker/__init__.py`
- `util/repomirror/skopeomirror.py`
- Proxy, scanner, referrer, mirror, and registry model tests

## Provisional verdict

- Recommendation: **Request changes**
- Confidence: high for the dynamically reproduced findings
- Final verdict: request changes pending implementation fixes and regression verification
- quay.io applicability: conditional on proxy cache, repository mirroring, referrers, or Clair being enabled
- OCI Distribution impact: yes
- Database impact: yes; no new Handoff 5 schema, but transaction duration, graph visibility, cache state, and repository registrations are affected
- Security impact: tenant isolation is generally preserved, but synchronous unbounded upstream traversal creates a denial-of-service risk

## Findings

### Medium: Coordinated proxy ingestion cannot promote a repository-local placeholder

Locations:

- `data/registry_model/registry_proxy_model.py:556-567`
- `data/model/oci/blob.py:14-30`
- `data/model/storage.py:291-299,402-408`

Evidence:

A temporary test created an `ImageStorage` row and repository-local `ManifestBlob` link without an `ImageStoragePlacement`, matching the lazy proxy placeholder state. `oci.blob.get_repository_blob_by_digest` found the linked row but then called `get_storage_by_uuid`, which requires a placement and raised `InvalidImageException`. `_ingest_proxy_manifest_graph` did not treat this as missing content and never called `_download_blob`.

Impact:

A graph first seen through the historical lazy SHA-256 proxy path can contain placeholders. Later coordinated alternative-digest ingestion fails before filling those placeholders, and retries repeat the same failure. A stale-tag shortcut can also register an alternative alias while referenced content remains lazy.

Required remediation:

Check placement or readable storage availability, not only repository identity, before skipping download. Add a regression test that creates a repository-local placeholder, promotes the same content through coordinated alternative ingestion, and proves the placement and graph become available.

### Medium: Upstream blob downloads run inside the graph database transaction

Location:

- `data/registry_model/registry_proxy_model.py:556-579`

Evidence:

The outer `db_transaction` starts before quota checks, upstream blob streaming, storage writes, upload state changes, manifest registration, and graph persistence. A multi-platform graph can download many large blobs while the transaction and database connection remain open.

Impact:

Slow or stalled upstream storage can hold transactions and connections for minutes. This increases lock duration, deadlock risk, pool exhaustion, replication lag, and request latency. The risk applies to a synchronous registry request path.

Required remediation:

Fetch and verify content before opening the short graph/tag transaction. Keep repository registration, graph rows, quota accounting, pruning, and visible tag changes atomic. Treat pre-fetched storage as unreferenced temporary content until the final transaction commits.

### Medium: Coordinated proxy traversal is unbounded and repeats duplicate fetches

Location:

- `data/registry_model/registry_proxy_model.py:467-542`

Evidence:

The recursive traversal has no maximum depth, descriptor count, total manifest bytes, or total graph size. Child manifests are fetched before the visited check can reject duplicate descriptors. A manifest list containing repeated descriptors can therefore cause repeated upstream requests even when persistence is deduplicated.

Impact:

A user pulling a tag from a configured upstream can synchronously consume a request worker with a wide, deep, repeated, or slow graph. Python recursion depth can also terminate the request without a controlled registry error.

Required remediation:

Use iterative traversal with explicit limits. Track requested descriptor digests before fetching. Enforce maximum graph depth, node count, manifest response size, and aggregate work. Return a controlled fail-closed error when limits are exceeded.

### Medium: Proxy artifact ingestion does not invalidate referrer caches

Locations:

- `data/registry_model/registry_proxy_model.py:583-599`
- `data/registry_model/registry_oci_model.py:607-640`
- `endpoints/v2/referrers.py:48-60`

Evidence:

The coordinated proxy path calls the base create methods without a `model_cache`. Proxy lookup methods also do not accept or retain the endpoint model cache. The base referrer invalidation helper returns immediately when `model_cache` is `None`.

Impact:

If a subject's referrer response was cached as empty, proxy-ingesting an artifact can persist the artifact while the referrer API continues returning stale data until cache expiry. This contradicts the stated post-commit cache contract.

Required remediation:

Propagate the model cache into proxy ingestion and invalidate every visible subject identity only after the outer transaction commits. Add a test that primes an empty referrer cache, proxy-ingests an artifact, and immediately observes the referrer.

### Medium: Mirror root alternative identity is not proven and is lost in the filtered path

Locations:

- `util/repomirror/skopeomirror.py:130-138`
- `workers/repomirrorworker/__init__.py:849-861`
- `workers/repomirrorworker/__init__.py:1060-1090`

Evidence:

Adding `--preserve-digests` preserves bytes and prevents conversion, but the filtered architecture path pushes the root manifest list directly to `/manifests/{tag}`. A tag PUT carries no upstream digest algorithm or alias. Quay therefore computes canonical SHA-256 and cannot know that the source tag was advertised as SHA-512. Child descriptors copied by digest can retain their identities, but the root alias is not registered by this direct tag push.

The new Skopeo unit test checks only command arguments. It does not prove destination root registration.

Impact:

Mirrored content can have correct bytes and child graph while failing lookup by the source repository's SHA-512 root identity. This does not meet the Handoff 5 completion requirement that supported ingestion paths produce equivalent repository-visible registrations.

Required remediation:

Capture the source root digest and perform a destination digest-addressed registration before or atomically with tag creation. Add destination registry tests for full and architecture-filtered mirrors with SHA-512 roots and mixed children. Full Skopeo behavior remains an unresolved assumption until tested with a real binary.

### Medium: Alternative lookup can miss legacy referrer fallback tags

Locations:

- `data/registry_model/registry_oci_model.py:280-287`
- `data/registry_model/registry_oci_model.py:363-381`

Evidence:

`lookup_manifest_by_digest` now wraps the manifest with the requested external alias. `lookup_referrers_for_tag_schema` constructs exactly one fallback tag by replacing the colon in `manifest.digest`. If the same subject has a historically visible canonical SHA-256 identity and an added SHA-512 alias, a SHA-512 request searches only the SHA-512 fallback tag and can miss an existing `sha256-...` fallback index.

Impact:

Legacy Cosign-style referrers can disappear depending on which registered alias the client uses, even though both aliases resolve to the same canonical manifest.

Required remediation:

Search fallback tags for all repository-visible subject registrations, plus canonical identity only when it is historically or explicitly visible. Deduplicate returned referrers. Add canonical-tag/SHA-512-query and SHA-512-tag/canonical-query tests.

### Medium: New tests do not exercise coordinated persistence or failure behavior

Locations:

- `data/registry_model/test/test_registry_proxy_model.py:1169-1267`
- `util/test/test_skopeomirror.py:94-112`

Evidence:

The coordinated proxy tests call `_build_proxy_ingestion_plan` and exact manifest validation. They do not call `_ingest_proxy_manifest_graph`. There is no new test proving blob placement repair, complete nested graph persistence, parent invisibility on failure, registration conflict rollback, quota/pruning rollback, post-commit cache invalidation, or retry convergence.

The mirror test checks only the Skopeo argument prefix.

Impact:

The previous green suites did not execute the highest-risk Handoff 5 behavior. They cannot disprove the defects above.

Required remediation:

Add end-to-end model tests around the coordinated ingestion method and destination registry behavior. Use production transaction semantics or live PostgreSQL where SQLite's fake transaction behavior differs.

## Additional observations

- Repository-local proxy worker resolution is an improvement and appears to preserve tenant isolation.
- Exact upstream manifest digest verification is correct for an advertised or digest-addressed identity.
- Scanner identity selection is stable because the first repository registration remains the report key. Historical canonical visibility is preserved before adding a new alias.
- Scanner GC no longer sends an unregistered canonical digest when registrations exist.
- Subject backfill now resolves repository-locally, clears an unsafe stale external subject, and leaves unresolved rows pending.
- Subject backfill can retry permanent malformed or cross-repository rows indefinitely and rewrite `subject=NULL` each pass. This is an operability concern, but it is lower priority than the findings above.
- Scanner identity lookup adds per-manifest registration queries during indexing. This may reduce Clair indexing throughput at scale and should be measured or batch-prefetched.
- No Handoff 5 dependency, CI, executable, symlink, submodule, migration, or generated-file change was found.
- No authorization bypass or cross-repository digest resolution was found in the reviewed Handoff 5 delta.

## Earlier verification evidence

The prior implementation session recorded these results before this re-review:

- Consolidated manifest lifecycle suite: 250 passed, 2 PostgreSQL-only skips
- Full proxy model suite: 58 passed
- Clair V4 and V4 V2 suites: 107 passed
- Mirror worker suite: 53 passed
- Secondary worker aggregate: 78 passed, 1 skipped, 2 deselected
- GC and quota aggregate: 72 passed
- Live PostgreSQL registration races: 2 passed
- Pre-commit: passed
- Targeted mypy: passed
- Python compilation and `git diff --check`: passed
- `go vet ./...`: passed
- Go tests passed with the documented macOS `/proc/self/ns/net` exclusion

These results remain useful regression evidence. They did not cover the proxy placement, transaction duration, traversal bounds, proxy referrer cache, mirror root alias, or fallback-tag defects subsequently reproduced above.

## Remaining Handoff 5 verification and fix work

1. Fix placeholder promotion and prove retry convergence.
2. Move remote downloads and cryptographic verification before the final short database transaction. Keep graph rows, registrations, quota accounting, pruning, and tags atomic.
3. Replace recursive graph traversal with bounded iterative traversal and deduplicate descriptors before fetch.
4. Propagate the model cache through proxy ingestion and invalidate referrer caches only after final commit.
5. Search referrer fallback tags across repository-visible subject aliases without exposing hidden canonical SHA-256.
6. Preserve and test alternative root registration for full and architecture-filtered mirrors. Run real Skopeo integration only if a trusted local binary becomes available.
7. Add permanent coordinated-ingestion persistence, rollback, quota, pruning, retry, cache-timing, traversal-limit, and destination-mirror regression tests.
8. Use live PostgreSQL for graph rollback and concurrency behavior where SQLite cannot prove production semantics.
9. Run focused and broad Handoff 5 suites, pre-commit, targeted mypy, Python compilation, `git diff --check`, and relevant Go checks after fixes.
10. Recheck branch, HEAD, merge base, staging, worktrees, and the complete Handoffs 1–5 diff before the final verdict.

## Constraints for continuation

- Work only in `/Users/shossain/QuayWorkspace/shaon-feature-PQC` on branch `shaon-feature-PQC`.
- Preserve the intentionally uncommitted Handoffs 1–5 diff.
- Do not stage, commit, push, rebase, reset, change remotes, alter pull requests, discard changes, or touch another worktree.
- Keep alternative identities repository-scoped.
- Do not expose hidden canonical SHA-256.
- Keep registration, graph persistence, quota accounting, pruning, and visible references transactional and idempotent.
- Do not expose a proxy parent before required alternative descendants are validated and registered.
- Invalidate caches only after the final outer transaction commits.
- Treat upstream registry responses and manifest graphs as untrusted and bounded input.

## Handoff 5 Session 1: Proxy placeholder promotion and retry convergence

**Status: Session 1 is implemented and verified. Handoff 5 is not complete. The complete proxy transaction refactor remains Session 2 work.**

This section supersedes item 1 in the earlier remaining-work list. It does not change the status or scope of the transaction, traversal-limit, referrer-cache, fallback-alias, or mirroring findings.

### Confirmed root cause

The earlier theory that coordinated ingestion silently accepted a placeholder as downloaded content was incorrect. Dynamic reproduction confirmed this path:

1. A lazy SHA-256 proxy path had created an `ImageStorage` row and repository-local `ManifestBlob` relationship without an `ImageStoragePlacement`.
2. Coordinated alternative-root ingestion called `oci.blob.get_repository_blob_by_digest()` for that repository-local descriptor.
3. The identity lookup found the correct `ImageStorage` row, but the same function immediately called `get_storage_by_uuid()`.
4. `get_storage_by_uuid()` requires a placement and raised `InvalidImageException`.
5. `_ingest_proxy_manifest_graph()` never reached `_download_blob()`, so every retry failed at the same point.

Repository identity and authorization, placement existence, physical readability, and complete blob retrieval were coupled in one lookup. Placeholder promotion requires repository-scoped identity resolution without treating an unplaced row as complete.

### Implementation changes

- `data/model/oci/blob.py`
  - Added `lookup_repository_blob_by_digest()` to resolve only repository-local registrations or legacy SHA-256 relationships without requiring a placement.
  - Kept `get_repository_blob_by_digest()` as the complete placed-blob API. Existing callers that need readable content still pass through `get_storage_by_uuid()`.
  - The raw lookup selects the full `ImageStorage` row so placement and CAS-path checks have the required fields.
  - Alternative registrations remain repository-scoped and must also have an `UploadedBlob` or `ManifestBlob` relationship in that repository.
- `data/model/storage.py`
  - Added `StorageContentStatus` with distinct `MISSING_PLACEMENT`, `MISSING_CONTENT`, `READ_ERROR`, and `READABLE` states.
  - Added `get_storage_content_status()` to inspect every database placement and prove physical readability through the configured storage backend.
  - Any backend probe exception fails closed as `READ_ERROR`; another readable placement still satisfies the blob.
- `data/registry_model/registry_proxy_model.py`
  - Coordinated ingestion now resolves repository identity without requiring placement, downloads on absent identity, absent placement, stale physical content, or a read error, and verifies that promotion produced repository-local readable content before graph persistence continues.
  - Direct proxy blob lookup uses the same status helper. An unknown direct request still returns `None`; it does not use a global checksum or another repository's registration to authorize a download.
  - Download, digest validation, placement creation, and repository registration continue through the existing blob uploader.
- `workers/proxycacheblobworker.py`
  - Placeholder and alternative identity lookup now uses the repository-scoped raw resolver.
  - Download decisions use the same placement/readability status helper as coordinated and direct proxy lookup.
  - Manifest security status resets now require every linked repository blob to be physically readable, not merely to have a placement row.
- `data/registry_model/test/test_registry_proxy_model.py`
  - Added real coordinated-ingestion tests around mocked upstream bytes and the configured local test storage.
- `workers/test/test_proxycacheblobworker.py`
  - Added no-placement worker coverage and corrected old completeness fixtures to use valid digests and real physical bytes.

No schema, migration, feature flag, endpoint, remote, or transaction-boundary change was made in Session 1.

### Permanent test coverage

The tests now prove:

1. A repository-local placeholder without placement is downloaded, validated, placed, registered, connected to the graph, and physically readable.
2. A placement whose physical bytes are absent is repaired.
3. Existing physically readable content is not downloaded again.
4. An alternative blob identity resolves through the current repository registration and relationship only.
5. Another repository's valid alternative registration and readable canonical content cannot satisfy the current repository; coordinated ingestion still fetches, validates, links, and registers it locally.
6. A blob digest mismatch creates no requested registration, placement, or root manifest registration.
7. The same state succeeds on a later retry with valid upstream bytes.
8. Repeated successful ingestion creates no duplicate registration or graph relationship and performs no second download.
9. Alternative-only blob and manifest paths do not expose their internal canonical SHA-256 identities.
10. The existing all-SHA-256 root remains lazy and does not fetch children while building its ingestion plan.
11. Direct proxy lookup and the worker repair missing physical bytes and treat backend probe exceptions as requiring repair.

### Exact verification outcomes

Expected failing regression before the production change:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short data/registry_model/test/test_registry_proxy_model.py::TestRegistryProxyCoordinatedIngestion::test_promotes_repository_placeholder_during_coordinated_ingestion`

Result: **1 failed as expected**. The traceback showed `InvalidImageException` from `get_storage_by_uuid()` before `_download_blob()`.

Focused coordinated-ingestion class after the fix:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_registry_proxy_model.py::TestRegistryProxyCoordinatedIngestion`

Result: **9 passed**.

Full proxy registry model file before final formatting:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_registry_proxy_model.py`

Result: **63 passed**.

Proxy blob worker file:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings workers/test/test_proxycacheblobworker.py`

Result: **11 passed**.

Blob uploader and blob model files:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_blobuploader.py data/model/test/test_blob.py data/model/test/test_model_blob.py`

Result: **26 passed**.

OCI manifest model file:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/model/oci/test/test_oci_manifest.py`

Result: **21 passed**.

Explicit lazy SHA-256 and proxy blob lookup selection:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_registry_proxy_model.py -k 'all_sha256_root_preserves_lazy_child_fetch or create_temp_tags_for_newly_created_sub_manifests_on_manifest_list or get_repo_blob_by_digest'`

Result: **2 passed, 61 deselected**. The full 63-test proxy run covered the remaining proxy behavior.

Mocked endpoint pull-through storage coverage:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/v2/test/test_blob.py::TestBlobPullThroughStorage`

Result: **2 passed**. No production registry was contacted.

Final post-format proxy and worker aggregate:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_registry_proxy_model.py workers/test/test_proxycacheblobworker.py`

Result: **74 passed**.

Final relevant uploader, blob model, OCI manifest, and endpoint aggregate:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_blobuploader.py data/model/test/test_blob.py data/model/test/test_model_blob.py data/model/oci/test/test_oci_manifest.py endpoints/v2/test/test_blob.py::TestBlobPullThroughStorage`

Result: **49 passed**.

Pre-commit command over all 39 tracked files modified by the complete uncommitted Handoffs 1–5 diff:

`files=$(git diff --name-only --diff-filter=ACMR) && .venv/bin/pre-commit run --files $files`

Initial result: **nonzero because hooks modified files**. Black reformatted `workers/test/test_proxycacheblobworker.py`, `data/registry_model/registry_proxy_model.py`, and `data/registry_model/test/test_registry_proxy_model.py`. Isort adjusted the proxy model and worker test. Every other applicable hook passed.

The exact command was rerun and then run once more after the final storage-exception review fix. Final result: **all applicable hooks passed**.

Targeted mypy:

`.venv/bin/mypy data/model/oci/blob.py data/model/storage.py data/registry_model/blobuploader.py data/registry_model/registry_proxy_model.py workers/proxycacheblobworker.py`

Result: **passed**, no issues in five source files.

Compilation and whitespace:

`.venv/bin/python -m compileall -q data/model/oci/blob.py data/model/storage.py data/registry_model/blobuploader.py data/registry_model/registry_proxy_model.py data/registry_model/test/test_registry_proxy_model.py workers/proxycacheblobworker.py workers/test/test_proxycacheblobworker.py && git diff --check`

Result: **passed** with no output.

Live PostgreSQL was **not run**. Session 1 did not change transaction behavior or schema, and SQLite plus mocked local storage covered the placeholder state and retry convergence. Production transaction shortening remains Session 2 scope.

### Focused adversarial review

- **Tenant isolation:** The raw resolver filters registrations and legacy relationships by the requested repository. A registration in another repository and globally deduplicated canonical bytes do not authorize or satisfy the current repository. The cross-repository coordinated test proves an upstream download still occurs and only then creates the current repository relationship and registration.
- **Digest mismatch:** The uploader hashes the exact mocked upstream bytes before storage finalization and registration. Mismatch leaves no placement, requested blob registration, or root manifest registration. A valid retry converges.
- **Canonical SHA-256 leakage:** Alternative-only blob lookup by the hidden canonical digest returns `None`. Alternative-root manifest lookup by hidden canonical digest also returns `None`. SHA-256 becomes visible only when explicitly used by a descriptor or the established tag/legacy contract requires it.
- **Missing and stale placements:** Missing placement, missing physical bytes, backend read failure, and readable content are separate states. Every proxy promotion/repair decision uses the shared status helper.
- **Physical storage failures:** Any exception from a backend `exists()` probe fails closed into repair. A readable alternate placement wins over a failed or stale placement.
- **Retry convergence:** Failed uploads are canceled by `complete_when_uploaded`; no valid registration or root graph becomes visible. The next valid attempt promotes the existing placeholder row successfully.
- **Idempotency:** A readable successful promotion is skipped on repetition. Registration and manifest-blob uniqueness remain unchanged, and the permanent test proves one registration and one graph relationship.
- **Lazy SHA-256 compatibility:** Coordination gating is unchanged. An all-SHA-256 root still returns the historical one-node lazy plan without child fetches. Existing proxy, endpoint, uploader, and blob tests pass.

No blocking Session 1 finding remains. quay.io applicability is conditional on proxy cache. OCI blob availability and retry behavior are impacted. Database queries and rows are impacted, but schema and transaction boundaries are unchanged. No new flag is appropriate because the fix restores integrity rather than introducing optional behavior.

### Remaining risks and Session 2 boundary

- `_ingest_proxy_manifest_graph()` still opens its outer database transaction before upstream blob downloads and cryptographic verification. **The complete proxy transaction refactor remains Handoff 5 Session 2 work.** Session 1 deliberately did not move downloads or alter graph/tag/quota transaction semantics.
- The bounded iterative traversal, duplicate fetch prevention, referrer cache propagation, fallback alias search, and mirror-root registration findings remain later-session work and were not modified.
- The shared physical-readability check performs backend `exists()` probes. Eventual consistency or transient backend failure can trigger a redundant repair download; digest validation and idempotent registration keep that retry fail-closed.
- Worker manifest-completeness checks now prove physical readability for each linked blob. This is more expensive than the prior placement-only query and should be observed at proxy-cache scale, but it prevents stale placements from re-enabling scanning.
- A failure after validated physical finalization but before database registration can still leave unreferenced canonical bytes. This was an existing Handoff 5 limitation and belongs with Session 2 transaction/failure work and later lifecycle cleanup.
- No live PostgreSQL or MySQL run was needed for this non-transactional scope. Session 2 must use PostgreSQL where SQLite cannot prove production transaction behavior.

## Handoff 5 Session 2: Coordinated proxy ingestion transaction refactor

**Status: Session 2 is implemented and verified. Handoff 5 is not complete. Traversal limits, duplicate manifest fetch prevention, referrer-cache propagation, fallback aliases, and mirroring remain later-session work.**

### Confirmed transaction root cause

`ProxyModel._ingest_proxy_manifest_graph()` previously opened its outer `db_transaction()` before quota checks, upstream blob reads, chunk streaming, digest hashing and validation, storage finalization, blob placement/link/registration, manifest graph persistence, quota updates, pruning, and tag changes. Production `DB_TRANSACTION_FACTORY` returns `db.transaction()`. `CloseForLongOperation` only disconnects before a slow operation when no testing override is active; it does not end or shorten an already-open logical transaction. The normal SQLite fixture uses `FakeTransaction`, which hid the production lock and connection lifetime.

The permanent pre-change regression recorded transaction depth 1 during upstream read, storage streaming, digest validation, and config storage validation, against baseline depth 0.

### Transaction boundaries before and after

Before:

1. Open the final graph transaction.
2. Run quota checks and pruning.
3. Read each upstream blob.
4. Stream and hash bytes.
5. Validate the exact requested digest.
6. Finalize physical storage.
7. Create placement, repository link, and digest registration.
8. Persist manifests, relationships, registrations, quota, and tags.
9. Commit.

After:

1. Outside the final graph transaction, resolve repository-local identity with the Session 1 helper and prove placement/readability with `get_storage_content_status()`.
2. Outside the final graph transaction, download only missing, placeholder, or stale content; stream, hash, validate the exact descriptor digest, and finalize canonical CAS bytes.
3. Outside the final graph transaction, read required config content and validate manifests and child-label data through an in-memory ingestion retriever. No repository blob link or digest registration is created by this prefetch.
4. Open one short final graph transaction and acquire a repository-scoped transaction advisory lock.
5. Perform database-only blob placement metadata, repository links, and exact digest registrations.
6. Run quota checks and quota-driven pruning.
7. Persist `Manifest`, `ManifestBlob`, `ManifestChild`, canonical subject relationships, repository manifest registrations, quota accounting, temporary tags, and visible tag changes.
8. Commit atomically. On failure, roll back every database-visible lifecycle change and delete restored pending upload rows without deleting finalized content-addressed bytes.

### Implementation changes

- `data/registry_model/blobuploader.py`
  - Split finalization into `prefetch_to_storage()` and `commit_prefetched_blob()`.
  - `prefetch_to_storage()` validates the exact requested digest and finalizes CAS bytes without creating repository links or registrations.
  - `commit_prefetched_blob()` performs only database metadata, link, registration, and upload-row work.
  - Existing `commit_to_blob()` composes both phases, preserving direct upload and direct proxy behavior.
  - Cancellation preserves already-finalized CAS bytes after a later database failure while removing the pending upload row.
- `data/registry_model/registry_proxy_model.py`
  - Added a prefetch content retriever for validated manifest/config reads outside the graph transaction.
  - Added `_prefetch_blob()` and `_prepare_proxy_manifest_graph()`.
  - Coordinated ingestion now prefetches, hashes, validates, finalizes, and prevalidates manifests before opening the final transaction.
  - Existing readable content still uses the Session 1 repository-scoped identity and storage-readability helpers and is not downloaded again.
  - The final transaction performs database-only blob registration, graph persistence, quota/pruning, and tag work.
  - A repository-scoped PostgreSQL advisory transaction lock serializes concurrent final promotion without serializing downloads.
- `data/model/oci/manifest.py`
  - Added an internal prevalidated persistence mode. It performs repository-scoped blob, child, and subject resolution and normal graph/quota/registration persistence without repeating storage reads.
  - Preserved child-label intersection for manifest-list tag expiration and immutability behavior by carrying prevalidated child labels into persistence.
- `data/registry_model/registry_oci_model.py`
  - Passed the internal prevalidated manifest and child-label state through existing manifest/tag creation APIs. Default direct-push behavior is unchanged.
- `data/registry_model/test/test_blobuploader.py`
  - Added focused proof that physical prefetch creates neither an `UploadedBlob` link nor a `RepositoryBlobDigest`, and that the database-only commit creates visibility.
- `data/registry_model/test/test_registry_proxy_model.py`
  - Added production-transaction depth, mismatch, rollback, retry, quota, pruning, and live PostgreSQL visibility tests.

No schema, migration, endpoint, feature flag, referrer cache, fallback alias, mirroring, traversal, scanner, copy, import, or lifecycle-cleanup change was made in Session 2.

### Permanent Session 2 coverage

The new and retained tests prove:

1. Upstream reads, storage streaming, digest validation, storage finalization, and required config reads run at baseline transaction depth, outside the final graph transaction.
2. Prefetched bytes do not create repository links or digest registrations.
3. A PostgreSQL observer connection cannot see blob registrations, manifest registrations, or graph rows before final commit.
4. A later blob mismatch starts no quota/pruning phase and leaves no graph, registration, placement, quota, upload, or visible-tag state.
5. A database failure after successful prefetch rolls back blob placement/link/registration, manifest graph, quota, and tags.
6. Failed mismatch and database attempts retry successfully.
7. Repeated success remains idempotent and does not redownload readable content.
8. Repository-local placeholder promotion and missing physical-content repair remain successful.
9. Another repository's registration and globally deduplicated canonical bytes cannot satisfy the current repository.
10. Alternative-only content does not expose hidden canonical SHA-256.
11. All-SHA-256 roots retain historical lazy behavior.
12. Quota rejection rolls back all prefetched database lifecycle state.
13. Quota-driven pruning rolls back on a later database failure and succeeds atomically on retry.
14. The final PostgreSQL transaction executes the repository-scoped advisory lock and rolls back all observer-visible state on forced failure.

### Exact verification outcomes

Expected failing regression before the implementation change:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings --show-capture=no data/registry_model/test/test_registry_proxy_model.py::TestRegistryProxyCoordinatedIngestion::test_prefetch_and_storage_validation_run_outside_graph_transaction`

Result: **1 failed as expected**. Upstream read, storage stream, digest validation, and storage validation all ran at transaction depth 1 instead of baseline depth 0.

Smallest regression after the change: the same command — **1 passed**.

Focused coordinated-ingestion class after final changes:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_registry_proxy_model.py::TestRegistryProxyCoordinatedIngestion`

Result: **14 passed, 1 skipped**. The skip is the PostgreSQL-only observer test run separately below.

Final full proxy registry model file:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_registry_proxy_model.py`

Result: **68 passed, 1 skipped**. The skip is the PostgreSQL-only observer test.

Proxy blob worker:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings workers/test/test_proxycacheblobworker.py`

Result: **11 passed**.

Blob uploader and blob models:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_blobuploader.py data/model/test/test_blob.py data/model/test/test_model_blob.py`

Result: **27 passed**. A later final full blob-uploader-only run was **22 passed**.

OCI manifest model:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/model/oci/test/test_oci_manifest.py`

Result: **21 passed**.

Quota and namespace-quota models:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/model/test/test_quota.py data/model/test/test_namespacequota.py`

Result: **31 passed**.

Manifest endpoint suite:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/v2/test/test_manifest.py`

Result: **25 passed**.

Mocked direct proxy blob endpoint coverage:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/v2/test/test_blob.py::TestBlobPullThroughStorage`

Result: **2 passed**. No external registry was contacted.

Registry interface suite:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_interface.py`

Result: **109 passed, 2 skipped**. The skips are PostgreSQL-only registration races previously covered in Handoff 5.

Pre-commit over every one of the 39 modified tracked files:

`files=$(git diff --name-only --diff-filter=ACMR) && .venv/bin/pre-commit run --files $files`

Initial Session 2 result: **nonzero because hooks modified files**. Black reformatted the proxy model and both modified test files; isort adjusted the proxy test. All other applicable hooks passed. After the advisory-lock review change, Black reformatted the proxy model once more. The final exact command passed every applicable hook.

Targeted mypy:

`.venv/bin/mypy data/model/oci/manifest.py data/registry_model/blobuploader.py data/registry_model/registry_oci_model.py data/registry_model/registry_proxy_model.py workers/proxycacheblobworker.py`

Result: **passed**, no issues in five source files.

Compilation and whitespace:

`.venv/bin/python -m compileall -q data/model/oci/manifest.py data/registry_model/blobuploader.py data/registry_model/registry_oci_model.py data/registry_model/registry_proxy_model.py data/registry_model/test/test_registry_proxy_model.py data/registry_model/test/test_blobuploader.py workers/proxycacheblobworker.py && git diff --check`

Result: **passed** with no output.

### Live PostgreSQL setup and results

Infrastructure: local PostgreSQL on `127.0.0.1:5432`. The session created isolated database `quay_handoff5_s2_01a044a6`, enabled `pg_trgm`, migrated through Alembic head `a2f338ee672c`, ran the tests, terminated no remaining sessions, and dropped the database.

The first Alembic invocation omitted `PYTHONPATH=.` and failed with `ModuleNotFoundError: No module named 'app'`. The corrected command with `TEST=true PYTHONPATH=. TEST_DATABASE_URI=...` passed.

An intermediate four-test PostgreSQL command produced **3 passed, 1 failed**. The failed quota-pruning assertion ran inside the test fixture's pre-existing outer transaction; forcing a nested production `db.transaction()` rolled back the fixture setup tag as well. This was a test-harness transaction artifact, not product behavior. The quota tests now use real `db.atomic()` rollback semantics under the fixture, while the dedicated observer test runs the production `db.transaction()` on a separate connection with committed setup.

Final PostgreSQL test command:

`TEST=true SKIP_DB_SCHEMA=true PYTHONPATH=. TEST_DATABASE_URI='postgresql://quay:quay@127.0.0.1:5432/quay_handoff5_s2_01a044a6' .venv/bin/python -m pytest -q --tb=short --disable-warnings --show-capture=no data/registry_model/test/test_registry_proxy_model.py::TestRegistryProxyCoordinatedIngestion::test_prefetch_and_storage_validation_run_outside_graph_transaction data/registry_model/test/test_registry_proxy_model.py::TestRegistryProxyCoordinatedIngestion::test_live_postgresql_hides_graph_until_commit_and_rolls_back`

Result: **2 passed**.

The first test proves network, stream, hash, and storage validation phases stay outside the production transaction. The second uses a separate PostgreSQL connection while the graph transaction is paused after its writes. The observer sees no repository blob registration, manifest registration, or manifest row. A forced failure then rolls back all of that state. A retry succeeds and makes the registrations visible.

### Focused adversarial review

- **External calls and slow storage:** Upstream reads, chunk streaming, digest hashing, CAS finalization, physical readability probes, and config reads occur before the final transaction. The prevalidated persistence path performs database queries and writes only.
- **Premature visibility:** A prefetched upload has physical bytes and a pending upload row but no `UploadedBlob`, `RepositoryBlobDigest`, manifest registration, graph row, quota update, pruning, or tag. PostgreSQL observer coverage proves uncommitted final state is not visible.
- **Tenant isolation:** Reuse begins with `lookup_repository_blob_by_digest(repo_id, requested_digest)` and the Session 1 readability helper. Another repository's alias or global canonical row is never used to authorize the current repository. Exact validated prefetch is required before the final local registration.
- **Digest mismatch:** Exact descriptor hashing completes before storage finalization and before quota/pruning. A later mismatch cancels pending uploads and starts no final transaction.
- **Canonical SHA-256 leakage:** Final registration uses the requested descriptor identity. Canonical SHA-256 is preserved only under the established explicit or historical visibility contract. Alternative-only tests remain green.
- **Placeholder and stale placement:** Missing placement, missing bytes, and storage probe failure still enter Session 1 promotion/repair. Existing readable content remains a no-download path.
- **Quota and pruning:** Both execute inside the final transaction after database-only blob registration. Rejection or a later graph failure rolls back registrations, graph, quota totals, pruning, and tags. Successful retry applies pruning and graph visibility together.
- **Partial storage/database failure:** Failures before finalization cancel temporary storage. Failures after finalization preserve unreferenced content-addressed bytes, roll back all database-visible lifecycle state, and remove pending upload rows. A later retry converges.
- **Retry and idempotency:** Mismatch and database-failure retries pass. Repeated successful ingestion creates one registration and one graph relationship and performs no second download.
- **Concurrent promotion:** Downloads remain outside locks. The short final phase takes a repository-scoped transaction advisory lock, preventing concurrent coordinated manifest uniqueness races from rolling back earlier blob work. Registration helpers retain savepoint-based idempotency.
- **PostgreSQL behavior:** A real second connection proved pre-commit invisibility and rollback. SQLite `FakeTransaction` was not relied on for that claim.
- **Lazy SHA-256 compatibility:** Coordination gating and the all-SHA-256 one-node plan are unchanged. Full proxy and endpoint regressions pass.

quay.io applicability is **conditional** on proxy cache and enabled alternative digest algorithms. OCI Distribution behavior is impacted through pull-through cache ingestion. Database behavior is impacted through shorter transaction and connection lifetime but no schema change. No feature flag is appropriate because the change restores transactional and availability invariants.

### Remaining risks and later-session boundaries

- Final graph transactions are serialized per repository, not globally. Downloads remain parallel. PostgreSQL advisory-lock contention should be observed at proxy-cache scale.
- A failed attempt after CAS finalization can leave unreferenced content-addressed bytes. They expose no repository identity or graph and are safe to remove. Broader physical orphan discovery and lifecycle cleanup remain Handoff 6 work.
- Storage eventual consistency can still trigger a redundant repair download. Exact validation and idempotent final registration keep this fail-closed.
- MySQL concurrency was not run. SQLite behavior and live PostgreSQL transaction visibility/rollback passed.
- No external proxy, Docker Hub, quay.io, or real Skopeo test was run.
- **Traversal limits and iterative traversal remain later-session work.**
- **Duplicate child or manifest fetch prevention remains later-session work.**
- **Referrer-cache propagation remains later-session work.**
- **Fallback-tag alias lookup remains later-session work.**
- **Mirror root registration and mirroring remain later-session work.**
- **Handoff 6 lifecycle cleanup remains later work.**

Session 2 closes only the coordinated proxy ingestion transaction finding. Do not claim Handoff 5 complete until the remaining findings above and the other unresolved Handoff 5 findings are resolved.

## Handoff 5 Session 3: Bounded iterative proxy graph traversal

**Status: Session 3 is implemented and verified. Handoff 5 is not complete. Referrer-cache propagation, fallback-tag alias lookup, mirror-root registration and mirroring remain later Handoff 5 work. Handoff 6 lifecycle cleanup remains later work.**

### Confirmed traversal and duplicate-fetch root causes

`ProxyModel._build_proxy_ingestion_plan()` previously used recursive depth-first traversal. It had no maximum graph depth, distinct manifest or descriptor count, individual manifest response size, or aggregate manifest-byte bound. A depth-1,100 regression reached Python's recursion limit and raised raw `RecursionError`.

The recursive code called `_pull_upstream_manifest()` before entering `visit()`. The visited check therefore ran only after child or subject bytes had already been requested. Duplicate descriptors in one index, repeated subjects, a descriptor used as both child and subject, and cycle-closing edges could all issue duplicate upstream manifest requests even though later database persistence deduplicated relationships.

`Proxy.get_manifest()` also read `requests.Response.content` in one unbounded operation. `_pull_upstream_manifest()` parsed that materialized body before applying any size check.

### Internal limits and rationale

Session 3 adds fixed internal limits for coordinated synchronous proxy traversal:

- Maximum graph depth: **32 edges**, with the supplied root at depth 0. A graph containing 33 manifests on one path is allowed; the next distinct edge fails.
- Maximum distinct graph descriptors: **256**, including the root requested identity, distinct child and subject manifest identities, and distinct blob descriptor identities. Child and subject uses of the same validated requested identity count once. Blob and manifest contracts remain separate descriptor kinds.
- Maximum individual upstream manifest response: **4 MiB** (`4 * 1024 * 1024` bytes).
- Maximum aggregate bytes across distinct fetched manifests, including the root: **16 MiB** (`16 * 1024 * 1024` bytes).

The existing `REPO_MIRROR_MAX_MANIFEST_LIST_SIZE` and `REPO_MIRROR_MAX_MANIFEST_ENTRIES` settings were not reused. They govern architecture-filtered mirroring, which is a different path with different operational semantics. Coupling synchronous proxy behavior to mirror configuration would make an unrelated mirror setting change registry request-worker exposure. The Session 3 values are internal named constants because they restore a safety boundary rather than define a supported product mode. No feature flag or new public configuration was added.

Depth 32 is well above ordinary OCI index-to-manifest and artifact-to-subject paths. The 256-descriptor bound accommodates normal multi-platform graphs and layered images while preventing unlimited upstream requests. The 4 MiB individual and 16 MiB aggregate bounds permit unusually large JSON manifests but cap response memory and parsing work in one registry request.

### Implementation changes

- `data/registry_model/registry_proxy_model.py`
  - Replaced recursive graph planning with an explicit LIFO work stack that preserves descendant-first, subject-first, descriptor-order traversal.
  - Added `ProxyManifestTraversalLimitExceeded`, a controlled `ManifestDoesNotExist` subtype. Limit errors retain a distinct limit name and maximum value and follow the existing proxy endpoint error mapping.
  - Validates and inserts each requested child or subject identity into `requested_manifests` before queuing any fetch.
  - Uses the strict, enabled requested descriptor identity as the fetch-deduplication key. It does not use internal canonical SHA-256 for authorization or deduplication.
  - Counts distinct blob and manifest descriptors, exact manifest bytes, and depth with explicit fail-closed checks. Boundary values are accepted; only values above a bound fail.
  - Keeps all-SHA-256 roots on the existing one-node lazy path. No descendant request is introduced.
  - Passes the 4 MiB cap into the proxy client and repeats the materialized-body check before parsing as defense for mocked or alternate proxy implementations.
  - Maps the proxy client's oversized-response error to the controlled traversal-limit model error.
- `proxy/__init__.py`
  - Added optional bounded streaming to `Proxy.get_manifest()`.
  - Rejects an advertised `Content-Length` above the limit before reading the body.
  - Streams in 64 KiB chunks and rejects a body that crosses the limit when the length is absent, malformed, or understated.
  - Closes the response on success or failure and maps stream transport failures to `UpstreamRegistryError`.
  - The default unbounded mode remains available to unchanged non-coordinated callers, while every `ProxyModel` manifest read supplies the internal limit.
- `proxy/fixtures.py`
  - Extended the mocked manifest response helper to accept the optional bounded-read argument.
- `data/registry_model/test/test_registry_proxy_model.py`
  - Added bounded iterative traversal, request deduplication, cycle, OCI duplicate-descriptor persistence, failure-state, and retry coverage.
- `proxy/test_proxy.py`
  - Added streaming overflow and exact-boundary response tests.

No schema, migration, endpoint, quota, storage, uploader, referrer cache, fallback alias, mirroring, scanner, copy, import, feature flag, or lifecycle-cleanup behavior changed in Session 3.

### Before-and-after traversal behavior

Before:

1. Fetch and parse the root without a response-size limit.
2. Recursively visit its subject and child descriptors.
3. Fetch each referenced manifest before checking whether the requested identity was already visited.
4. Retain no explicit depth, descriptor-count, individual-response, or aggregate-byte bound.
5. Allow raw `RecursionError` and duplicate network requests.

After:

1. Stream every root, child, and subject manifest through the 4 MiB individual cap before parsing.
2. Preserve the lazy one-node result if the root directly exposes only SHA-256 identities.
3. For coordinated graphs, put the validated root requested identity in the requested set.
4. Iteratively expand stack entries. Count exact bytes and distinct blob or manifest descriptors.
5. Validate and mark each new child or subject requested identity before queuing its upstream fetch.
6. Skip repeated requested identities, including cycle-closing edges, without a second request.
7. Append an expanded node only after its distinct descendants, preserving the persistence plan's descendant-first contract.
8. Fail the planning attempt with a controlled model error if any bound is exceeded. Planning still precedes blob prefetch and the final graph transaction, so no graph lifecycle state is exposed.

### Permanent Session 3 coverage

The new tests prove:

1. A depth-1,100 graph fails with a controlled depth error and cannot raise `RecursionError`.
2. A graph exactly at a patched depth boundary succeeds.
3. A graph wider than a patched distinct-descriptor limit fails.
4. Distinct-descriptor and aggregate-byte exact boundaries succeed.
5. An individual oversized response fails before `parse_manifest_from_bytes()` is called.
6. Aggregate distinct manifest bytes above the limit fail.
7. Repeated child descriptors issue one upstream request.
8. Repeated subject descriptors issue one request for the shared subject.
9. One identity used as both child and subject is fetched once.
10. A cycle terminates without refetching the root.
11. A traversal-limit failure creates no manifest, manifest registration, blob registration, quota, or tag state.
12. The same graph retries successfully when bounded, and duplicate OCI index descriptors persist one `ManifestChild` relationship.
13. The proxy client rejects an unknown-length streamed body after crossing its limit and closes the response.
14. A streamed body exactly at the individual limit succeeds.

Retained tests continue to prove exact digest mismatch handling, placeholder promotion, missing physical-content repair, readable-content reuse, repository isolation, hidden canonical identity, rollback, retry convergence, repeated-success idempotency, quota rejection, quota-driven pruning rollback, Session 2 transaction depth and visibility, and lazy all-SHA-256 behavior.

### Exact verification outcomes

Expected failing regression before the production change:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings --show-capture=no data/registry_model/test/test_registry_proxy_model.py::TestRegistryProxyCoordinatedIngestion::test_graph_depth_limit_is_controlled_instead_of_recursing`

Result: **1 failed as expected** with raw `RecursionError` from recursive `visit()` after repeated child fetches.

The same smallest regression after the implementation change: **1 passed**.

Initial new-test aggregate:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings --show-capture=no data/registry_model/test/test_registry_proxy_model.py::TestRegistryProxyCoordinatedIngestion -k 'depth_limit or depth_boundary or distinct_manifest_count or individual_manifest_size or aggregate_manifest_bytes or repeated_child or repeated_subject or child_and_subject or cycle_terminates or traversal_failure' proxy/test_proxy.py::TestProxy::test_get_manifest_rejects_oversized_response_while_streaming`

Result: **9 passed, 1 failed**. The retry test initially set the descriptor limit to 2 but the actual graph has three distinct descriptors: root manifest, child manifest, and config blob. The test boundary was corrected to 3. The isolated retry test then passed. This was a test expectation error, not a production defect.

Focused coordinated-ingestion class before final boundary additions: **24 passed, 1 skipped**. The skip was the PostgreSQL-only observer test.

Full proxy registry model file after implementation and before final formatting:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_registry_proxy_model.py`

Result: **78 passed, 1 skipped**. The skip was the PostgreSQL-only observer test.

Proxy client file before final additions:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings proxy/test_proxy.py`

Result: **35 passed**.

Final post-format proxy model and proxy client aggregate:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_registry_proxy_model.py proxy/test_proxy.py`

Result: **115 passed, 1 skipped**. This command passed twice after the final production edit. The skip is the PostgreSQL-only observer test run separately below.

Relevant proxy worker, uploader, blob model, manifest model, quota, namespace-quota, and manifest endpoint aggregate:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings workers/test/test_proxycacheblobworker.py data/registry_model/test/test_blobuploader.py data/model/test/test_blob.py data/model/test/test_model_blob.py data/model/oci/test/test_oci_manifest.py data/model/test/test_quota.py data/model/test/test_namespacequota.py endpoints/v2/test/test_manifest.py`

Result: **115 passed**.

Registry interface and mocked direct proxy blob endpoint aggregate:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_interface.py endpoints/v2/test/test_blob.py::TestBlobPullThroughStorage`

Result: **111 passed, 2 skipped**. The skips are the existing PostgreSQL-only registration races already covered in prior Handoff 5 sessions.

Mocked manifest pull-through endpoint command, with external tests excluded:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings -m 'not e2e' endpoints/v2/test/test_manifest_pullthru.py`

Result: **45 passed, 13 skipped, 12 deselected, 10 failed**. All ten failures are the current combined working tree's schema-1 fixtures. `manifest_exists()` returns Docker's historical schema-1 digest, while the existing `_pull_upstream_manifest()` exact full-response-byte calculation reports `upstream manifest digest mismatch`. Session 3 did not change the schema-1 digest calculation or error path. This is recorded as a regression-harness/product-contract failure and was not expanded into this traversal-only session.

The same mocked endpoint file excluding those schema-1 cases:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings -m 'not e2e' endpoints/v2/test/test_manifest_pullthru.py -k 'not busybox_schema1'`

Result: **41 passed, 9 skipped, 30 deselected**. No external registry was contacted.

Pre-commit over every modified tracked file in the complete working tree:

`files=$(git diff --name-only --diff-filter=ACMR) && .venv/bin/pre-commit run --files $files`

The first Session 3 invocation covered **42 modified tracked files** and exited nonzero because Black reformatted `data/registry_model/registry_proxy_model.py` and `data/registry_model/test/test_registry_proxy_model.py`. All other applicable hooks passed. The exact command was rerun and passed. It was run again after the final stream-error mapping edit and **all applicable hooks passed**.

Targeted mypy:

`.venv/bin/mypy proxy/__init__.py data/registry_model/registry_proxy_model.py`

Result: **passed**, no issues in two source files. Existing unchecked-body notes were informational.

Compilation and whitespace:

`.venv/bin/python -m compileall -q proxy/__init__.py proxy/fixtures.py proxy/test_proxy.py data/registry_model/registry_proxy_model.py data/registry_model/test/test_registry_proxy_model.py && git diff --check`

Result: **passed** with no output. The final combined pre-commit, mypy, compilation, and diff-check command also passed.

No external proxy, Docker Hub, quay.io, production registry, or production service test was run.

### PostgreSQL setup and results

The expected service on `127.0.0.1:5432` was initially unavailable. Podman was not running and Docker was not installed.

A temporary local PostgreSQL 14 cluster was created under `/tmp/quay-handoff5-s3-pg-01a044bb` on `127.0.0.1:55433` with local-only trust. It created role `quay`, database `quay_handoff5_s3_01a044bb`, enabled `pg_trgm`, and migrated through Alembic head `a2f338ee672c`.

Running the entire coordinated class against that one migrated database produced **6 passed, 20 setup errors**. The PostgreSQL observer test intentionally commits its fixed-name organization fixture. Tests ordered after it could not recreate `quayio-cache`. This is class-order database contamination, not a traversal or transaction failure.

A fresh temporary PostgreSQL 14 cluster then ran the established production-transaction command:

`TEST=true SKIP_DB_SCHEMA=true PYTHONPATH=. TEST_DATABASE_URI='postgresql://quay:quay@127.0.0.1:55433/quay_handoff5_s3_01a044bb' .venv/bin/python -m pytest -q --tb=short --disable-warnings --show-capture=no data/registry_model/test/test_registry_proxy_model.py::TestRegistryProxyCoordinatedIngestion::test_prefetch_and_storage_validation_run_outside_graph_transaction data/registry_model/test/test_registry_proxy_model.py::TestRegistryProxyCoordinatedIngestion::test_live_postgresql_hides_graph_until_commit_and_rolls_back`

Result: **2 passed**. The temporary server was stopped and its data directory deleted by the command's cleanup trap.

After Podman became available, the existing `quay-db` PostgreSQL **18.6** container was started. The session created isolated database `quay_handoff5_s3_01a044bb`, enabled `pg_trgm`, migrated it through `a2f338ee672c`, and ran the same two-test command on port 5432.

Result: **2 passed**. The isolated database was dropped afterward and its absence was verified. The user-started `quay-db` container was left running.

These tests prove upstream reads, storage streaming, digest validation, storage finalization, and config reads remain outside the final transaction. A separate PostgreSQL connection sees no blob registration, manifest registration, or manifest graph before commit. Forced final-transaction failure rolls back all visible state, and retry succeeds.

### Focused adversarial review

- **Depth and recursion:** Traversal contains no recursive call. The explicit stack is bounded by distinct descriptors. Depth is checked before a new distinct child or subject is queued. Cyclic edges already requested are skipped before depth enforcement.
- **Width and work:** Distinct manifest, subject, child, and blob descriptor identities share the 256-entry cap. Individual and aggregate exact manifest bytes bound parsing and retained manifest data. Duplicate JSON descriptors still require bounded scanning of the already capped 4 MiB response but do not create extra fetches.
- **Off-by-one behavior:** Root depth is 0. `>` checks accept exact depth and byte boundaries. Descriptor insertion checks the current count before adding a new identity. Exact depth, descriptor-count, individual-stream, and aggregate-byte boundary tests pass.
- **Duplicate network requests:** Requested descriptor identity is strict-parsed, enabled, and inserted before a fetch task is queued. Duplicate child, duplicate subject, child-plus-subject, and cycle tests each prove one fetch.
- **Identity and authorization:** Deduplication uses only the validated requested repository descriptor identity. Canonical-equivalent content under another alias is not used to authorize or suppress an upstream request. Repository-scoped resolution remains in Session 1 and Session 2 helpers.
- **Digest validation:** Every distinct fetched manifest still passes exact-byte validation against its requested identity in `_pull_upstream_manifest()`. The retained mismatch test remains distinct from limit messages and passes.
- **Controlled errors:** Every traversal bound raises `ProxyManifestTraversalLimitExceeded`, which is handled through the existing `ManifestDoesNotExist` proxy contract. Endpoint behavior remains controlled `MANIFEST_UNKNOWN` rather than raw recursion, parser, or memory errors. Limit names remain visible in the internal detail for diagnosis.
- **External work and transactions:** Root and descendant reads, traversal, limit checks, blob prefetch, hashing, storage, and prevalidation all remain before `_ingest_proxy_manifest_graph()` opens the final transaction. No Session 2 external call or storage operation moved into that transaction.
- **Partial visibility:** A traversal failure occurs before physical blob prefetch and final graph persistence. The permanent state test proves no manifest, graph relationship, blob or manifest registration, quota change, pruning, temporary tag, or visible tag. PostgreSQL still proves final-transaction isolation and rollback.
- **Placeholder and stale placement:** Session 1 resolution and physical-readability helpers are unchanged. Placeholder promotion, missing-content repair, readable-content reuse, and retry tests pass.
- **Quota and pruning:** Traversal limits run before quota work. Session 2 quota rejection and later-failure pruning rollback tests remain in the passing coordinated class and quota suites.
- **Retry and idempotency:** Requested and pending sets are per attempt. A failed bounded attempt leaves no durable traversal state. A later valid attempt persists successfully. Existing repeated success still creates one registration and relationship and does not redownload readable content.
- **Concurrent ingestion:** No shared mutable traversal state was added. Downloads remain outside the repository advisory lock. The short final transaction and repository-scoped advisory lock are unchanged.
- **PostgreSQL:** Both temporary PostgreSQL 14 and containerized PostgreSQL 18.6 passed the transaction-depth and observer rollback tests. No MySQL test was run.
- **Lazy SHA-256:** The coordination gate remains before descendant scheduling. All-SHA-256 roots return one root plan and make no child request. The full proxy model and endpoint regressions pass apart from the separately recorded schema-1 digest cases.
- **quay.io and latency:** Applicability is conditional on proxy cache and enabled alternative algorithms. The work is now finite, but the 256-descriptor cap is not an overall wall-clock deadline. Existing per-request manifest and blob timeouts can still produce a long synchronous attempt when many distinct upstream objects are slow. This remains an operational risk to observe.

No blocking Session 3 finding remains. OCI Distribution pull-through behavior and database graph persistence are impacted. Authorization and tenant boundaries are unchanged. No feature flag is appropriate because unsafe unbounded traversal must not remain operator-selectable.

### Remaining risks and later-session boundaries

- The limits are internal constants. Changing them requires a code rollout. This avoids public configuration coupling but prevents emergency operator tuning.
- The 256-descriptor bound and per-request timeouts still permit a long finite synchronous attempt against a slow upstream. There is no aggregate wall-clock deadline or request cancellation budget in this session.
- A failure after later blob CAS finalization can still leave unreferenced physical bytes under the Session 2 contract. It exposes no repository identity. Physical orphan cleanup remains Handoff 6 work.
- The combined working tree's mocked schema-1 pull-through digest mismatch remains documented. It was not caused or changed by Session 3 and was not expanded into this traversal-only session.
- The PostgreSQL coordinated class cannot currently run in full in one fixed database after its observer test commits the shared organization fixture. The two production transaction tests pass in their established isolated order.
- MySQL concurrency and transaction behavior were not run.
- **Referrer-cache propagation was not changed and remains later Handoff 5 work.**
- **Fallback-tag alias lookup was not changed and remains later Handoff 5 work.**
- **Mirror-root registration, complete mirroring, and filtered mirroring were not changed and remain later Handoff 5 work.**
- **Scanner, copy, import, and unrelated proxy worker behavior were not changed.**
- **Handoff 6 lifecycle cleanup was not changed and remains later work.**

Session 3 closes only bounded iterative coordinated traversal and duplicate child or subject manifest-fetch prevention. Do not claim Handoff 5 complete until every other remaining Handoff 5 finding above is resolved.

## SHA-384 extension

**Status: implemented and focused verification passed. This does not close the remaining Handoff 5 findings or establish production readiness.**

### Behavior and scope

- Shared strict digest handling now supports SHA-384 with exactly 96 lowercase hexadecimal characters.
- Exact-byte hashing uses Python `hashlib`; resumable SHA-384 state uses the already pinned `resumablehash` implementation and the existing safe JSON/base64 envelope.
- Python and Go configuration accept every nonempty unique combination of `sha256`, `sha384`, and `sha512`. The default remains `["sha256"]`.
- Blob upload, resume, finalization, pull, HEAD, mount, isolation, mismatch, conflict, idempotency, and authoritative SHA-256 finalization tests now run for SHA-384 and SHA-512.
- Manifest push, exact-byte validation, pull, HEAD, tagging, hidden canonical identity, descriptor resolution, isolation, conflicts, OCI indexes and Docker manifest lists, mixed SHA-256/SHA-384/SHA-512 descriptors, artifacts, subjects, and referrers now have SHA-384 coverage.
- Focused proxy tests cover SHA-384 root and child identities, alternative blob descriptors, exact upstream validation, repository isolation, failed-attempt cleanup, and retry.
- Unsupported-algorithm tests now use `sha999`. Valid but disabled SHA-384 and malformed-length SHA-384 remain distinct.
- Docker schema-1 behavior was not changed.
- No database model, migration, registration size, registration index, dependency, or default configuration change was made for SHA-384.

### Files added to the existing tracked diff

- `util/config/schema.py`
- `util/config/test/test_schema.py`
- `internal/config/digest.go`
- `internal/config/config_test.go`
- `internal/config/validate_test.go`

Existing modified digest and lifecycle files were extended without discarding prior work:

- `digest/digest_tools.py`
- `digest/test/test_digest_tools.py`
- `data/model/oci/test/test_oci_manifest.py`
- `data/registry_model/test/test_blobuploader.py`
- `data/registry_model/test/test_registry_proxy_model.py`
- `endpoints/v2/test/test_blob.py`
- `endpoints/v2/test/test_manifest.py`

### Verification

- Final focused Python aggregate: **258 passed**.
- Final focused SHA-384/SHA-512 proxy selection: **10 passed, 21 deselected**.
- Full proxy model suite: **84 passed, 1 skipped**. The skip is the PostgreSQL-only observer test from existing work.
- Registry interface and proxy client aggregate: **145 passed, 2 skipped**. The skips are existing PostgreSQL-only registration race tests.
- Registry protocol blob, mount, and manifest selection: **271 passed, 1,188 deselected**.
- Mocked manifest pull-through suite: **45 passed, 10 failed, 13 skipped, 12 deselected**. All 10 failures are the pre-existing Docker schema-1 upstream digest mismatch cases. Excluding those cases produced **41 passed, 9 skipped, 30 deselected**.
- `go test ./internal/config -count=1`: passed.
- `go test ./...`: 30 tested packages passed, 1 tested package failed, and 4 packages had no tests on `darwin/arm64`. The sole failure is the existing `internal/migrate.TestInPostgresNetworkNamespace` use of `/proc/self/ns/net` on macOS.
- `go test ./internal/migrate -skip '^TestInPostgresNetworkNamespace$'`: passed.
- `go vet ./...`: passed.
- Pre-commit over all 47 modified tracked files: passed. An earlier focused invocation exited nonzero only because Black reformatted six changed test files; its rerun passed.
- Targeted mypy over seven production files: passed with no issues.
- Python compilation and `git diff --check`: passed.
- Both HTML documents parsed with unique IDs, valid local fragments, and present local image assets. No browser runner was available for desktop or mobile rendering.

### Remaining validation and risks

- No new live PostgreSQL or MySQL run was performed because SHA-384 adds no persistence or transaction behavior. Existing PostgreSQL-only tests remained skipped in the SQLite aggregates.
- The installed native `resumablehash` SHA-384 implementation passed on local macOS arm64. Linux architectures, wheel or image packaging, and compiler/toolchain availability were not validated.
- No external Docker, Podman, Skopeo, ORAS, containerd, Buildah, Clair, production registry, or quay.io interoperability test was run.
- SHA-384 is a Quay extension under OCI Image Specification 1.1.1. External clients may reject it even though Quay, Python `hashlib`, the pinned `resumablehash`, and opencontainers/go-digest can process it.
- The known schema-1 proxy defect, referrer-cache issue, mirror-root issue, fallback-tag issue, and lifecycle cleanup remain outside this SHA-384 extension.

## Final SHA-384 adversarial review and interoperability validation

**Status: the SHA-384 review is complete. No SHA-384 production-code defect was found. Two verified test gaps were closed. This is not a production-readiness verdict.**

### Review scope and findings

- The initial worktree matched the expected branch `shaon-feature-PQC`, HEAD `9352a3d42478d2aa696df4b76805e5dbbe9a8290`, merge base `d81004d24669132d45df8fbd1eafc86c149e38fa`, 47 modified tracked files, three expected untracked files, and no staged files.
- The complete tracked diff was inventoried by file and hunk. SHA-384 references and every production SHA-512 reference were searched separately. Production SHA-384 support remains confined to `digest/digest_tools.py`, `util/config/schema.py`, and `internal/config/digest.go`. No SHA-512-only production branch outside those registries blocks SHA-384.
- Exact-byte blob, manifest, and proxy validation selects the requested algorithm and hashes unmodified bytes. SHA-256 remains the independently computed canonical storage and manifest identity.
- Blob and manifest aliases resolve through repository-scoped registrations and repository relationships. Existing cross-repository, mount, descriptor, proxy, artifact, subject, and referrer SHA-384 tests passed.
- New alternative-only content does not gain an externally resolvable canonical SHA-256 alias. Existing legacy canonical visibility is preserved explicitly before an alternative alias is added.
- Blob and manifest registration conflicts remain immutable, idempotent, and covered by transaction rollback tests parameterized for SHA-384 and SHA-512.
- Persisted resumable state records and validates the algorithm, format version, architecture, byte order, byte count, origin, and native state. Cross-algorithm restoration between SHA-384 and SHA-512 is rejected.
- Malformed SHA-384, valid-but-disabled SHA-384, and unsupported `sha999` use distinct error paths. Existing endpoint tests passed.
- When both SHA-384 and SHA-512 are configured, a hintless chunked upload deliberately tracks neither alternative. It can finish as SHA-256, but an alternative final digest after prior bytes requires an explicit opening hint. New coverage fixes this fail-closed contract for both alternatives.
- Docker schema-1 remains SHA-256-only. A new real signed schema-1 endpoint test proves SHA-384 and SHA-512 digest-addressed pushes are rejected while the historical schema-1 SHA-256 digest-addressed push succeeds.
- Remaining SHA-512-only tests cover algorithm-independent lifecycle, GC, scanner, traversal-limit, or mirroring behavior already exercised by direct SHA-384 endpoint/model tests. Parameterizing them would add runtime without proving a new SHA-384 invariant.
- No production file, database model, migration, index, field size, default, dependency, generated file, or external documentation file changed in this final session.

### Tests added

- `endpoints/v2/test/test_blob.py`
  - `test_hintless_chunked_upload_with_multiple_alternatives_requires_a_hint`, parameterized for SHA-384 and SHA-512.
  - Proves no alternative state is selected arbitrarily, prior bytes cannot be finalized under an untracked alternative, the upload is canceled, and no registration is created.
- `endpoints/v2/test/test_manifest.py`
  - `test_schema1_digest_push_remains_sha256_only`, parameterized for SHA-384 and SHA-512.
  - Uses a real signed schema-1 manifest. Proves alternative digest routes return `UNSUPPORTED`, SHA-256 still succeeds, and no alternative registration is created.

### Exact validation commands and outcomes

The first focused invocation was accidentally run from the harness start directory rather than the target worktree:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/v2/test/test_blob.py::test_hintless_chunked_upload_with_multiple_alternatives_requires_a_hint endpoints/v2/test/test_manifest.py::test_schema1_digest_push_remains_sha256_only`

Result: **pytest did not start**; shell exit 127 because `.venv/bin/python` was not found. This was an invocation failure, not a test failure.

The command was rerun from `/Users/shossain/QuayWorkspace/shaon-feature-PQC` before formatting and again after Black:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings endpoints/v2/test/test_blob.py::test_hintless_chunked_upload_with_multiple_alternatives_requires_a_hint endpoints/v2/test/test_manifest.py::test_schema1_digest_push_remains_sha256_only`

Result on each run: **4 passed**.

Changed SHA-focused Python files:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings digest/test/test_digest_tools.py util/config/test/test_schema.py data/model/oci/test/test_oci_manifest.py data/registry_model/test/test_blobuploader.py data/registry_model/test/test_registry_proxy_model.py endpoints/v2/test/test_blob.py endpoints/v2/test/test_manifest.py`

Result: **346 passed, 1 skipped**. The skip is the existing PostgreSQL-only proxy observer test.

Go configuration:

`go test ./internal/config -count=1`

Result: **passed**.

Registry interface and proxy client:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_interface.py proxy/test_proxy.py`

Result: **145 passed, 2 skipped**. The skips are existing PostgreSQL-only registration races.

Registry protocol blob, mount, and basic manifest selection:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings test/registry/registry_tests.py -k 'test_chunked_blob_uploading or test_blob_mounting or test_basic_push_pull_by_manifest'`

Result: **271 passed, 1,188 deselected**.

Mocked manifest pull-through excluding known schema-1 cases:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings -m 'not e2e' endpoints/v2/test/test_manifest_pullthru.py -k 'not busybox_schema1'`

Result: **41 passed, 9 skipped, 30 deselected**.

Full mocked manifest pull-through:

`TEST=true PYTHONPATH=. .venv/bin/python -m pytest -q --tb=short --disable-warnings -m 'not e2e' endpoints/v2/test/test_manifest_pullthru.py`

Result: **45 passed, 10 failed, 13 skipped, 12 deselected**. Every failure is an existing `busybox_schema1` upstream manifest digest mismatch. SHA-384 code and the new direct schema-1 test did not alter this path.

Go aggregate:

`go test ./...`

Result: **30 tested packages passed, 1 tested package failed, and 4 packages had no tests** on macOS arm64. The sole failure was the known `internal/migrate.TestInPostgresNetworkNamespace` use of absent `/proc/self/ns/net`.

Fallback and vet:

`go test ./internal/migrate -skip '^TestInPostgresNetworkNamespace$' && go vet ./...`

Result: **passed**; vet emitted no findings.

Targeted mypy:

`.venv/bin/mypy digest/digest_tools.py data/model/oci/blob.py data/model/oci/manifest.py data/registry_model/blobuploader.py data/registry_model/registry_oci_model.py data/registry_model/registry_proxy_model.py endpoints/v2/blob.py endpoints/v2/manifest.py`

Result: **passed**, no issues in eight source files.

Compilation and whitespace:

`.venv/bin/python -m compileall -q digest/digest_tools.py data/model/oci/blob.py data/model/oci/manifest.py data/registry_model/blobuploader.py data/registry_model/registry_oci_model.py data/registry_model/registry_proxy_model.py endpoints/v2/blob.py endpoints/v2/manifest.py endpoints/v2/test/test_blob.py endpoints/v2/test/test_manifest.py && git diff --check`

Result: **passed**.

Focused pre-commit:

`.venv/bin/pre-commit run --files endpoints/v2/test/test_blob.py endpoints/v2/test/test_manifest.py`

First result: **nonzero because Black reformatted both files**. The exact command was rerun and **all applicable hooks passed**.

Full pre-commit:

`files=$(git diff --name-only --diff-filter=ACMR) && .venv/bin/pre-commit run --files $files`

Result: **all applicable hooks passed across 47 modified tracked files**.

### Live Quay and external-client interoperability

Available local tools and services:

- Podman 6.0.2: available.
- Skopeo 1.21.0: available.
- ORAS 1.3.1: available.
- Docker, `ctr`/containerd CLI, Nerdctl, Buildah CLI, and Clair CLI/service: unavailable.
- Local Quay, PostgreSQL, and Redis containers were running. `quay-quay` bind-mounted this target worktree.

The local development config was temporarily changed from the default SHA-256 allowlist to `['sha256', 'sha384', 'sha512']`, and `quay-quay` was restarted. A temporary verified user and a tiny Podman-imported image were used. The configuration was restored byte-for-byte, Quay was restarted, the live allowlist was verified as `['sha256']`, and the temporary user, repository metadata, registrations, and local image were removed.

Baseline external push:

`podman push --authfile /tmp/sha384-interop-01a04710-valid/auth.json --tls-verify=false localhost:8080/sha384interop/image:source`

Result: **passed** using canonical SHA-256 descriptors.

A registry API script fetched the exact tag manifest bytes, computed SHA-384, and sent:

`PUT http://localhost:8080/v2/sha384interop/image/manifests/sha384:265bc0ea9cf3200b7a1f269897c0e07ef2f1427140b0ed533a241c17f77291773862204c7e53814a34e543d85c334612`

Result: **HTTP 201** with the requested SHA-384 `Docker-Content-Digest`. A subsequent digest `GET` returned **HTTP 200**, the same digest header, and byte-for-byte identical manifest content. This proves the live Quay extension accepted SHA-384; it does not prove OCI conformance or production readiness.

Podman pull:

`podman pull --authfile /tmp/sha384-interop-01a04710-valid/auth.json --tls-verify=false localhost:8080/sha384interop/image@sha384:265bc0ea9cf3200b7a1f269897c0e07ef2f1427140b0ed533a241c17f77291773862204c7e53814a34e543d85c334612`

Result: **failed, exit 125** with `Manifest does not match provided manifest digest sha384:...`. Quay had already returned exact bytes under that identity; this is Podman's client-side digest behavior for the unregistered OCI algorithm.

Skopeo inspect:

`skopeo inspect --authfile /tmp/sha384-interop-01a04710-valid/auth.json --tls-verify=false docker://localhost:8080/sha384interop/image@sha384:265bc0ea9cf3200b7a1f269897c0e07ef2f1427140b0ed533a241c17f77291773862204c7e53814a34e543d85c334612`

Result: **failed, exit 1** with the same manifest-digest mismatch. This is client-side rejection, not Quay rejecting SHA-384.

ORAS fetch:

`oras manifest fetch --plain-http --registry-config /tmp/sha384-interop-01a04710-valid/auth.json localhost:8080/sha384interop/image@sha384:265bc0ea9cf3200b7a1f269897c0e07ef2f1427140b0ed533a241c17f77291773862204c7e53814a34e543d85c334612`

Result: **passed, exit 0**. `cmp` against the original manifest returned **exit 0**, proving exact bytes.

The first external attempt was infrastructure-invalid: ORAS rejected an absolute payload path before contacting Quay, and stale saved `testuser` credentials caused HTTP 401. No SHA-384 identity was created by that attempt. Both attempts restored `local-dev/stack/config.yaml` byte-for-byte.

Initial temporary-user cleanup exposed the existing repository lifecycle foreign-key defect: repository purge tried to delete `ImageStorage` while `RepositoryBlobDigest` still referenced it. No lifecycle code was changed. Removing only the temporary repository's two digest-registration rows allowed the existing cleanup path to delete the temporary user and repository successfully.

### Native dependency and release assumptions

- Local native probe: Python 3.12.12 on macOS 26.6.1 arm64, `resumablehash` 1.0.0 from the pinned Git dependency. SHA-384 and SHA-512 hashing and native-state extraction both succeeded; each native state was 216 bytes.
- Existing Quay-container probe: Python 3.12.13 on Linux aarch64 with glibc 2.34 contained `resumablehash/_hash_ext.cpython-312-aarch64-linux-gnu.so`. SHA-384 and SHA-512 native-state extraction, restoration, continuation, and final hashing succeeded; each native state was 216 bytes. This proves only the already-built local Linux aarch64 image.
- Linux x86_64, a clean Linux image build, wheel availability, Git availability, compiler/toolchain availability, cross-architecture resume behavior, and production packaging remain **unproven**.
- No live MySQL run was performed. No new PostgreSQL test was needed because this session changed only tests and documentation; existing PostgreSQL-only tests remained skipped in SQLite aggregates.
- OCI Image Specification 1.1.1 does not register SHA-384. Podman and Skopeo rejected the extension in this environment; ORAS accepted it. External interoperability must be treated client by client.
- No database migration was added.
