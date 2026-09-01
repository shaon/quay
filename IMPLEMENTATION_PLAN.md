# Quay Multi-Algorithm Digest Implementation Plan

## Purpose

Implement repository-visible alternative OCI digests while preserving SHA-256 as Quay's canonical internal identity.

All implementation work happens in `/Users/shossain/QuayWorkspace/shaon-feature-PQC` on branch `shaon-feature-PQC`.

## Existing foundations

The integration branch contains these independent PRs merged locally:

- [quay/quay#6917](https://github.com/quay/quay/pull/6917): `ALLOWED_HASH_ALGORITHMS` configuration.
- [quay/quay#6918](https://github.com/quay/quay/pull/6918): digest-registration tables and upload-session fields.

The PR branches must remain independent. Do not modify, push, force-push, close, or retarget either PR while implementing the full feature.

## Delivery model

The complete feature will be implemented locally before it is divided into reviewable PRs.

Each handoff below must leave the integration branch in a tested and understandable state. A handoff is complete when its behavior and tests are complete locally. Opening a PR is not part of these implementation handoffs.

Later, the completed implementation will be divided into smaller PRs. The project tracker may consider those later tasks done when their PRs are opened.

## Design invariants

1. SHA-256 remains the canonical internal identity for blobs and manifests.
2. Alternative digests are repository-scoped external identities.
3. An algorithm is accepted only when it is enabled by `ALLOWED_HASH_ALGORITHMS`.
4. Digest parsing and validation are shared by real call sites. Do not add unused helpers.
5. Digest validation uses the exact uploaded blob bytes or exact manifest bytes.
6. A digest registration is created only after content validation succeeds.
7. Registration and canonical-object updates must be transactionally safe and idempotent.
8. A digest cannot be remapped to different content in the same repository.
9. Persisted resumable hash state must not rely on unsafe deserialization of untrusted data.
10. Existing SHA-256, hintless, monolithic, chunked, resumed, mount, pull, and manifest behavior must remain compatible.
11. Repository boundaries must be enforced for all alternative-digest lookups.
12. New code must be used by the behavior introduced in the same handoff.

## Handoff 1: Alternative-digest blob lifecycle

### Goal

Allow a blob addressed by an enabled alternative digest to be uploaded, resumed, validated, registered, and retrieved while retaining canonical SHA-256 storage.

### Work

- Define one shared digest representation and parser for `<algorithm>:<encoded-value>`.
- Validate algorithm names, encoded values, expected lengths, and malformed input.
- Enforce `ALLOWED_HASH_ALGORITHMS` at the blob API boundary.
- Accept the agreed `digest-algorithm` upload hint when an upload begins.
- Define behavior when the hint is absent.
- Integrate `resumablehash` for the requested digest algorithm.
- Verify the library's state format, safe restoration behavior, supported algorithms, and platform packaging.
- Persist and restore `BlobUpload.requested_digest_algorithm` and `BlobUpload.requested_digest_state`.
- Continue computing canonical SHA-256 during every upload.
- Validate the final digest against the exact uploaded bytes.
- Reject malformed, disabled, mismatched, or corrupted digest state without committing a blob registration.
- Commit the canonical blob and `RepositoryBlobDigest` registration safely.
- Make duplicate registration idempotent and reject conflicting mappings.
- Resolve blob `GET` and `HEAD` requests through repository-scoped registrations.
- Return correct digest and location information without exposing an unintended internal identity.

### Compatibility coverage

- Existing SHA-256 upload and pull.
- Chunked and resumed uploads.
- Monolithic uploads.
- Upload cancellation and failed finalization.
- Missing upload hints.
- Disabled algorithms.
- Corrupted or incompatible persisted state.
- Concurrent finalization and duplicate registration.
- Repository isolation.

### Completion result

A SHA-512 blob can be uploaded, resumed, validated, registered, and retrieved through the registry API. Full image push and pull are not yet supported because manifest handling remains SHA-256-only.

## Handoff 2: Blob compatibility and mount behavior

### Goal

Make the alternative-digest blob lifecycle safe for legacy clients, mounts, and rolling deployments.

### Work

- Support cross-repository blob mount through repository-visible digest registrations.
- Enforce source and destination authorization and repository boundaries.
- Define hintless upload behavior without depending on storage readback.
- Preserve legacy SHA-256 lookup behavior.
- Add lazy SHA-256 registration only where required by the final lookup contract.
- Handle upload sessions created by older or newer Quay versions.
- Define behavior when nodes have different algorithm configuration during a rolling deployment.
- Preserve duplicate upload and deduplication behavior.
- Return precise registry errors for unsupported, disabled, malformed, unknown, and mismatched digests.
- Verify digest headers and upload locations.

### Completion result

Blob operations remain compatible across existing clients and mixed-version Quay deployments.

## Handoff 3: Single-manifest image lifecycle

### Goal

Support complete push and pull of a single-platform image using an enabled alternative manifest digest.

### Work

- Parse and validate manifest references through the shared digest implementation.
- Validate the requested digest over the exact manifest bytes.
- Compute canonical SHA-256 over the same bytes.
- Resolve config and layer descriptors through `RepositoryBlobDigest`.
- Preserve repository boundaries for every referenced blob.
- Create or reuse the canonical `Manifest` and existing graph relationships.
- Create `RepositoryManifestDigest` only after validation and graph persistence succeed.
- Make registration idempotent and reject conflicting mappings.
- Assign requested tags to the canonical manifest.
- Resolve manifest `GET` and `HEAD` by tag or registered digest.
- Return consistent digest headers, locations, media types, and errors.

### Completion result

A single-platform image using SHA-512 external identities can be pushed and pulled end to end.

## Handoff 4: OCI graph support

### Goal

Extend alternative digest behavior to complete OCI graphs.

### Work

- Support OCI indexes and Docker manifest lists.
- Resolve child manifests through repository-scoped registrations.
- Support OCI artifacts, subjects, and referrers.
- Preserve canonical SHA-256 identities for every stored graph object.
- Prevent partial graph visibility when validation fails.
- Verify tag operations and graph traversal by tag and registered digest.
- Add multi-architecture, artifact, subject, and referrer tests.

### Completion result

Complete multi-platform and artifact graphs can use enabled alternative digests.

## Handoff 5: Secondary ingestion paths

### Goal

Apply the same identity and registration invariants outside direct registry-client pushes.

### Work

- Proxy cache ingestion.
- Repository mirroring.
- Cross-repository copy operations.
- Import paths.
- Background ingestion and repair paths.
- Security-scanner interactions where digest identity is passed externally.
- Consistent registration behavior for content created without an upload hint.
- Failure and retry behavior without conflicting or partial registrations.

### Completion result

All supported ingestion paths produce the same canonical objects and repository-visible digest registrations.

## Handoff 6: Lifecycle and cleanup

### Goal

Keep digest registrations consistent throughout deletion, expiration, and garbage collection.

### Work

- Blob deletion and unlink behavior.
- Manifest deletion and tag removal.
- Repository and namespace deletion.
- Upload expiration and cancellation.
- Garbage collection and orphan prevention.
- Registration cleanup ordering.
- Retry and transaction-failure safety.
- Concurrency tests for registration, deletion, and garbage collection.

### Completion result

Registrations cannot outlive, remap, or corrupt their canonical objects.

## Handoff 7: Conformance and rollout

### Goal

Validate the complete feature and make it safe to operate.

### Work

- Registry API and client compatibility tests.
- End-to-end push and pull tests for SHA-256 and SHA-512.
- OCI index, artifact, subject, and referrer end-to-end tests.
- HTTP digest header, location, tag, media-type, and error conformance.
- Disabled-algorithm and malformed-input security tests.
- Performance and memory checks for parallel hashing.
- Metrics and logs for algorithm use, validation failures, state failures, and registration conflicts.
- Mixed-region and mixed-version configuration checks.
- Rollback behavior after alternative-digest registrations exist.
- Operator and configuration documentation.

### Completion result

The feature is ready to divide into reviewable PRs and prepare for controlled rollout.

## Verification rules for every handoff

- Run focused unit and integration tests for changed behavior.
- Run existing SHA-256 regression tests for affected registry paths.
- Run migration tests when persistence behavior changes.
- Run `git diff --check`.
- Keep the worktree free of unrelated changes.
- Record exact test commands and outcomes in `HANDOFF.md`.
- Record known failures as test failures or infrastructure failures. Do not describe an attempted test as passed.
- Update `HANDOFF.md` before ending the task.

## Handoff document protocol

`HANDOFF.md` is the current operational state for the next session. Update it after every completed handoff with:

- Current branch, base, HEAD, and relevant commit references.
- Completed behavior.
- Files and subsystems changed.
- Design decisions and invariants enforced.
- Exact tests and outcomes.
- Known limitations, risks, and unresolved questions.
- The next handoff and recommended starting files.

Keep this implementation plan stable unless the delivery sequence or an invariant changes. Keep transient progress and current state in `HANDOFF.md`.
