# Story 22 Completion and Story 23 Planning Handoff

## Story 23 deferral decision

Story 23, **Publish Quay build output using configured digest algorithms**, is **Deferred** by explicit product decision. It remains unimplemented and must not be reported as Done or as supporting SHA-384/SHA-512 build output.

The baseline confirmed the intended cross-repository boundary: Quay queues and manages builds, while the external `quay-builder` performs the image build and Registry V2 publication. This worktree has no worker source or worker rebuild path. Local tooling can only extract a prebuilt worker binary from `quay.io/projectquay/quay-builder`, which cannot provide implementation or live-proof evidence. The existing protocol has neither worker capability advertisement nor an output-digest field.

No production, test, configuration, protobuf, generated, schema, migration, or worker file was changed for Story 23. Only this handoff, the planning `TODO.md`, and this session's `.PITASKS.md` section were updated. Read-only inspection used `git status --short --branch`, local `find`/`env` checks for a designated worker source, `wc -l`, `rg`, and complete reads of the required planning/configuration/build files. No tests, PostgreSQL commands, protobuf generation, compilation, mypy, pre-commit, or live builds were run because implementation did not begin. `git diff --check` was the only required validation for this documentation-only decision.

The implementation plan below remains authoritative if the story resumes. Resume only after a local `quay-builder` source and rebuild path are designated. Until then, worker implementation and SHA-256/SHA-384/SHA-512 live completion evidence remain infrastructure-blocked. Recommendation: keep Story 23 **Deferred**, not Done or Blocked.

Stories 24–33 are also **Deferred** by explicit product decision. No next implementation story is designated. Their status-only deferral changes no production behavior and provides no implementation, validation, interoperability, rollout, or release-readiness evidence.

Story 22 resolves authorized vulnerability-report requests through enabled repository-registered SHA-384 and SHA-512 identities while keeping Clair deduplicated on canonical SHA-256.

- `endpoints/api/secscan.py` validates the requested digest and active allowlist after repository authorization, then uses repository-scoped registration lookup. Disabled, malformed, unregistered, and cross-repository identities return 404.
- Both scanner workers wrap manifests with canonical SHA-256. Report lookup and caching use the canonical manifest row rather than the requested alias.
- Existing completed scans keyed by the pre-Story-22 first registration remain readable during transition. A missing canonical report records the legacy key in `ManifestSecurityStatus.metadata_json`, atomically returns the status to `PENDING`, and continues serving the legacy report while that canonical reindex is pending or in progress.
- Clair index requests use canonical SHA-256 storage identities for local layer hashes. For storage without direct-download URLs, the internal registry URL uses the layer's registered descriptor identity so Clair can retrieve the canonical bytes without exposing an unregistered canonical digest. Remote layers retain their descriptor digest and external URL because Quay does not own their storage.
- Scanner GC queues the canonical report identity and the one legacy report identity that the pre-Story-22 implementation could have created. Cleanup preserves canonical and legacy reports across repositories until their final corresponding owner is removed; missing legacy reports are idempotent cleanup.
- Existing SHA-256 behavior, authorization decorators, registrations, and non-Clair integrations remain unchanged. API v1 now reapplies Story 19's exact canonical SHA-256 rule after resolving a Docker schema 1 manifest. No schema or migration was added.

Final corrective validation: the new API suite passed **22 tests on SQLite**; the two worker-transition tests passed on SQLite; the complete affected scanner/Clair API/security endpoint/GC set passed **221 tests on SQLite**; the Story 19 schema 1 regression selection passed **36 tests with 236 deselected**; the complete PostgreSQL endpoint/scanner/GC set passed **195 tests**; and the final PostgreSQL API plus worker-transition slice passed **24 tests**. A broader SQLite authorization/scanner/GC selection passed **1,825 tests with 43 deselected**. Targeted compilation, mypy, pre-commit, and `git diff --check` passed. The running Quay instance returned healthy status for auth, database, disk, registry, service keys, and web. Live Clair validation remains infrastructure-blocked because no Clair container is running.

## Implementation status

The two high-severity static-review findings are fixed in the uncommitted Story 22 work:

- `data/secscan_model/secscan_v4_model.py` falls back to the one legacy report key selected by pre-Story-22 code when a completed scan has no canonical report. It stores that key in `ManifestSecurityStatus.metadata_json`, conditionally returns the status to `PENDING`, serves the legacy report while canonical reindexing is pending or in progress, and keeps the response identity canonical SHA-256.
- `data/model/oci/manifest.py` identifies the historical first-registration report key and determines whether another manifest still owns that legacy key.
- `data/model/gc.py` durably queues both the canonical report key and the one possible legacy key. Canonical and legacy ownership checks prevent cross-repository report deletion, and Clair 404 remains idempotent cleanup.
- `util/secscan/v4/api.py` still sends canonical SHA-256 hashes for local layers, while `util/secscan/blob.py` uses the registered descriptor identity in fallback Registry V2 download URLs. This lets Clair retrieve canonical bytes from storage backends without direct-download URLs without exposing hidden canonical digests.
- Regression coverage is in `data/secscan_model/test/test_secscan_v4_model.py`, `data/model/test/test_gc.py`, `util/secscan/v4/test/test_v4_api.py`, and `util/secscan/test/test_blob_retriever.py`.

The worktree remains intentionally dirty with unrelated pre-existing Stories 17/19/20/21 changes. Do not reset, restore, rebase, stage, commit, or attribute the complete working-tree diff to Story 22.

## Story 22 completion

- `endpoints/api/secscan.py` now compares a resolved Docker schema 1 manifest request with the digest of its parsed historical payload. Retained SHA-384, SHA-512, and noncanonical SHA-256 registrations return the same API v1 404 as other inaccessible identities. Repository authorization still runs first, and canonical SHA-256 schema 1 access remains unchanged.
- `endpoints/api/test/test_secscan.py` covers malformed syntax, unknown algorithms, enabled but unregistered identities, disabled canonical SHA-256, cross-repository aliases, all three stale schema 1 identity forms, canonical schema 1 success, and unauthenticated/unauthorized precedence. The three schema 1 cases failed with HTTP 200 before the production correction and pass with HTTP 404 afterward.
- Both scanner implementations are now covered from legacy-report fallback through `PENDING` claim, canonical SHA-256 manifest/local-layer indexing, successful metadata cleanup, and canonical-only subsequent report lookup. These tests passed without another scanner production change.
- Existing GC coverage proves durable canonical and legacy key queuing, cross-repository ownership preservation, final-owner deletion, retries, and idempotent missing-report cleanup. The complete GC suite passed without another GC production change.
- Story 22 is **Done** under the local definition of done. Full mixed-version/region rollout remains Story 30. SHA-384/SHA-512 remote-layer interoperability remains explicitly infrastructure-blocked until an isolated live Clair instance is available; mocked tests are not treated as live support evidence.

Final corrective files:

- `endpoints/api/secscan.py`
- `endpoints/api/test/test_secscan.py`
- `data/secscan_model/test/test_secscan_v4_model.py`
- `data/secscan_model/test/test_secscan_v4_model_v2.py`
- `HANDOFF.md`

## Deferred Story 23 implementation plan

Story 23, **Publish Quay build output using configured digest algorithms**, is deferred in `TODO.md`. Its dependencies, Stories 2 and 3, are Done. Story 23 must preserve canonical SHA-256 storage while publishing each build through one explicitly selected repository-visible digest algorithm and proving that the expected registrations were created.

### Architectural finding and completion boundary

Quay does not build or push the image in this repository. `buildman/manager/ephemeral.py::start_job` sends repository, registry, token, and tag arguments to an external `quay-builder` process. `buildman/buildmanagerservicer.py::RegisterBuildJob` serializes those arguments into `buildman/buildman_pb/buildman.proto::BuildPack`. The current protobuf has no output-digest field, and the current worker contract reports only phases and logs. The worker source is not present in this worktree; local development extracts a prebuilt binary from `quay.io/projectquay/quay-builder`.

A normal worker push is tag-addressed. Story 2 deliberately keeps tag-addressed manifest PUT canonical SHA-256 because it carries no explicit algorithm. Alternative output therefore cannot be implemented by adding a Quay-side default around the existing push. The worker must use the explicit Registry V2 extension already implemented by Stories 2 and 3: publish content under the selected digest and attach tags through digest-addressed manifest PUT tag parameters.

Story 23 cannot be marked Done from Quay-only protobuf plumbing, mocks, or a stock worker that silently continues to push SHA-256. Completion requires a compatible `quay-builder` implementation and a live build proving the selected registrations. Do not fetch, inspect, or compare upstream code without explicit approval; first obtain the designated local worker source or record the dependency as blocked.

### Proposed contract

1. Add a singular deployment setting, provisionally named `BUILD_OUTPUT_HASH_ALGORITHM`, with a backward-compatible default of `sha256`. Do not infer build output from the order of `ALLOWED_HASH_ALGORITHMS`: that list controls availability, not which one build workers publish.
2. Accept only `sha256`, `sha384`, or `sha512`, and require the selected value to be enabled by `ALLOWED_HASH_ALGORITHMS` whenever builds are enabled. A missing setting preserves legacy SHA-256 behavior. If SHA-256 is disabled and no explicit alternative is selected, fail configuration/build startup clearly rather than silently choosing an algorithm.
3. Snapshot the selected algorithm into the repository build's internal `job_config` in `endpoints/building.py`. Existing queued records with no field fall back to SHA-256. Revalidate the snapshot against the active allowlist before handing work to a worker so a hard-disabled algorithm is not published after a configuration change.
4. Extend the protobuf additively. A new worker advertises its supported output algorithms in `BuildJobArgs`; `BuildPack` returns the selected `output_digest_algorithm`. Permit an old worker only for the legacy SHA-256 path. Reject an alternative build before execution when the worker does not advertise support, preventing an old binary from ignoring an unknown field and silently publishing SHA-256.
5. Update the external worker to preserve its current SHA-256 path and add explicit SHA-384/SHA-512 publication. For alternative output, calculate the selected digest over exact blob/config bytes, upload or register each local blob under that identity using the existing `digest-algorithm` upload contract, rewrite manifest descriptors to those selected registrations, calculate the selected digest over the final exact manifest bytes, and use digest-addressed manifest PUT with every requested `tag` parameter. Quay continues to deduplicate the bytes and manifest internally by canonical SHA-256; the worker must not expose an unregistered canonical identity.
6. Before accepting a worker's `COMPLETE` phase, verify repository-locally that every requested tag points to the produced manifest, that the manifest has the exact selected registration, and that its local config/layer descriptors have the corresponding repository blob registrations. Missing or mismatched registrations are build failures, not successful builds. Keep this verification out of notifications and audit payloads; those schema changes belong to Stories 24 and 25.

The exact protobuf capability shape and completion-failure classification should be locked down with failing compatibility tests before production edits. Generated Python protobuf files must be regenerated from `buildman.proto`, not hand-edited. The matching worker protobuf must change in the designated worker source.

### Test-first implementation phases

1. **Baseline and contract tests**
   - Capture the current default SHA-256 build arguments and protobuf behavior.
   - Add configuration tests for omission/default, each supported value, unknown values, selected-but-disabled values, and build-support-disabled behavior.
   - Add queued-job tests proving manual, trigger, and webhook builds all snapshot the same selected algorithm and old records retain SHA-256 compatibility.
2. **Manager/worker protocol**
   - Add failing tests around `EphemeralBuilderManager.start_job` and `BuildManagerServicer.RegisterBuildJob` for selected-algorithm propagation, old-worker SHA-256 compatibility, alternative capability acceptance, unsupported-worker rejection, and active-allowlist revalidation.
   - Add protobuf wire compatibility tests and regenerate `buildman_pb2.py`, `buildman_pb2_grpc.py`, and typing output as required by this repository.
3. **Worker publication**
   - In the separately designated `quay-builder` source, add exact-byte digest, descriptor rewrite, blob publication, multi-tag manifest publication, retry, and registry-error tests for SHA-256, SHA-384, and SHA-512.
   - Prove the alternative path does not first perform a tag-addressed SHA-256 manifest PUT. Preserve authentication, build logs, cancellation, cache lookup, and existing Dockerfile build behavior.
4. **Completion verification**
   - Add a repository-scoped model/helper that verifies the selected manifest and local blob registrations without synthesizing registrations or exposing canonical fallback.
   - Test all tags, missing manifest registration, missing blob registration, wrong repository, changed tag, duplicate tags, disabled-after-queue behavior, and successful SHA-256/SHA-384/SHA-512 completion on SQLite and PostgreSQL.
   - Integrate verification before success notification and queue completion. Do not add digest fields to notifications, webhooks, action logs, or API responses in Story 23.
5. **End-to-end validation**
   - Build a minimal image with a locally rebuilt compatible worker under separate SHA-256, SHA-384, and SHA-512 output configurations.
   - For each build, verify success, all requested tags, digest-addressed GET/HEAD, exact manifest and blob registrations, and canonical SHA-256 internal deduplication. With alternative-only configuration, prove no repository-visible canonical SHA-256 registration was created.
   - Verify omitted configuration and an old compatible worker preserve the existing SHA-256 path. Verify invalid/disabled selection and an incapable old worker fail closed with a clear build result.

### Likely Quay files

- `config.py`
- `util/config/schema.py` and `util/config/test/test_schema.py`
- `internal/config/*` and their Go tests if the new top-level field must be parsed there
- `endpoints/building.py` and `endpoints/test/test_building.py`
- `buildman/manager/ephemeral.py`
- `buildman/buildmanagerservicer.py`
- `buildman/buildman_pb/buildman.proto` and generated Python/typing files
- New focused build-manager/servicer tests; the existing `buildman/test/test_buildman.py` is not reliable evidence for the current constructor/API without first reconciling its stale assumptions
- A repository-scoped build-output verification helper and focused model tests
- `local-dev/builds/builds-config.yaml` or local build tooling only as needed for live validation
- `HANDOFF.md`, planning `TODO.md`, and configuration documentation

### Required validation and exclusions

Run focused suites first, then the complete build endpoint/build-trigger/build-manager/configuration and affected registry manifest/blob suites on SQLite. Run persistence and completion-verification coverage on PostgreSQL. Run Python compilation, targeted mypy, Go config tests if changed, protobuf regeneration/diff checks, pre-commit over every changed file, and `git diff --check`. Live evidence must use a locally controlled compatible worker binary; a mocked worker or the existing stock image is not proof of alternative publication.

Story 23 does not add notification/webhook payloads, audit digest fields, export/reporting/admin tools, hard-disable certification across all integrations, multi-architecture build output, proxy cache, mirrors, imports, Docker schema 1 alternatives, schema changes, migrations, rollout certification, logs/metrics, or physical-orphan recovery. Partial publication recovery remains Story 18; mixed worker/version rollout remains Story 30.

## Historical implementation prompt (completed)

The following prompt records the completed correction scope and is not an outstanding work list.

```text
Work only in `/Users/shossain/QuayWorkspace/shaon-feature-PQC`. The worktree contains unrelated, pre-existing uncommitted Stories 17/19/20/21 changes. Preserve every existing change. Do not reset, restore, rebase, stage, commit, amend, or access another worktree.

Read completely before modifying files:

- `/Users/shossain/QuayWorkspace/shaon-feature-PQC/AGENTS.md`
- The Story 22 section at the top of `/Users/shossain/QuayWorkspace/shaon-feature-PQC/HANDOFF.md`
- `/Users/shossain/Project_Documents/Quay_Post_Quantum_Cryptography/TODO.md`
- Demo 14 and the cross-cutting requirements in `/Users/shossain/Project_Documents/Quay_Post_Quantum_Cryptography/PQC-Features.md`

Story 22 already has post-review fixes for legacy alternative-keyed Clair reports and non-direct local-layer download URLs. Preserve those fixes and their tests. Do not redesign canonical report storage or layer routing.

Resolve the remaining Story 22 implementation issues using test-first development:

1. Enforce the Story 19 Docker schema 1 boundary in the API v1 security endpoint. After repository authorization and repository-local lookup, only the manifest's exact historical canonical SHA-256 identity may access schema 1 security information. Retained stale SHA-384, SHA-512, and noncanonical SHA-256 registrations must return the established API v1 404 without exposing whether another identity or report exists. Preserve normal canonical SHA-256 schema 1 behavior and allowlist semantics.
2. Add endpoint tests for malformed syntax, unknown algorithms, enabled but unregistered identities, disabled canonical SHA-256, stale schema 1 alternative registrations, noncanonical schema 1 SHA-256 registrations, cross-repository aliases, and unauthenticated/unauthorized requests. Prove authorization runs before malformed, disabled, or schema-aware identity checks and that all inaccessible cases use the intended non-disclosing response.
3. Add transition-completion tests for both `V4SecurityScanner` and `V4SecurityScannerV2`. Start with a completed status and a report available only under the old first registration. Prove API lookup serves the old report, changes the status to `PENDING`, both worker paths index the canonical SHA-256 manifest and local-layer hashes, successful completion removes `legacy_scanner_digest` metadata, and later API/cache reads request only canonical SHA-256.
4. Recheck GC compatibility for canonical and legacy report keys, including final-owner deletion across repositories and idempotent missing-report cleanup. Do not weaken the durable queue or canonical ownership checks.
5. Keep remote layers unchanged: Quay passes their original descriptor digest and external URL because it does not own their bytes. If no live Clair environment is available, record SHA-384/SHA-512 remote-layer validation as infrastructure-blocked rather than passed.

Likely files:

- `endpoints/api/secscan.py`
- `endpoints/api/test/test_secscan.py`
- `data/secscan_model/secscan_v4_model.py`
- `data/secscan_model/secscan_v4_model_v2.py` only if a failing transition test proves a production change is required
- `data/secscan_model/test/test_secscan_v4_model.py`
- `data/secscan_model/test/test_secscan_v4_model_v2.py`
- `data/model/gc.py` and `data/model/test/test_gc.py` only for proven cleanup defects
- `HANDOFF.md` and planning `TODO.md`

Do not add a schema migration, new digest registration API, SHA-384/SHA-512 schema 1 semantics, proxy-cache/mirror/import work, build behavior, notifications/webhooks, or Story 30 rollout certification.

Before running tests or tools, present the exact command set and receive approval. Use the worktree executables through `.venv/bin` or add `$PWD/.venv/bin` to `PATH`; bare `pytest`, `pre-commit`, and `alembic` are not available on the host PATH.

Required validation after approval:

- Focused API schema 1 and negative tests on SQLite.
- Focused legacy-transition tests for both scanner workers on SQLite and PostgreSQL.
- Complete affected security endpoint, scanner model, Clair API/URL, and GC suites.
- Existing Story 19 schema 1 regression selection.
- Targeted pre-commit over every changed file and `git diff --check`.
- Live Clair only if an isolated Clair environment is actually available; otherwise report it as infrastructure-blocked.

Explain the root cause of every production change. Keep evidence for product defects, test defects, command failures, and infrastructure blocks separate. Finish by updating the Story 22 handoff with exact changed files, commands, outcomes, remaining risks, and a concise recommendation on whether Story 22 can remain Done.

Start by reporting the preserved worktree state, the current post-review fixes, the exact stale-schema-1 execution path, and the first failing tests you will add.
```

---

# Stories 20 and 21 API/UI Implementation Handoff

## Status and authority

Stories 20 and 21 are implemented and locally **Done**:

- Story 20: **Expose registered digest identities in the Quay API**.
- Story 21: **Display and manage registered digest identities in the Quay UI**.

Story 20 defines the additive `manifest_digests` contract and Story 21 consumes that contract in both React and Angular. No database schema, migration, configuration, Jira issue, pull request, canonical storage rule, or unrelated Story 17/19 behavior was changed.

The designated Quay worktree is `/Users/shossain/QuayWorkspace/shaon-feature-PQC` on branch `shaon-feature-PQC`, HEAD `ad50cf9b983726edb0a1db2aef94399ab1d40230`, with merge base `d81004d24669132d45df8fbd1eafc86c149e38fa` against `upstream/master`. All pre-existing Story 17 and Story 19 modifications remain in place.

## Implementation result

- The OCI model now performs one repository-scoped bulk lookup of explicit `RepositoryManifestDigest` rows, preserves registration order, computes enabled/preferred state from the active allowlist, and suppresses stale noncanonical Docker schema 1 registrations. It never synthesizes canonical SHA-256 inventory entries.
- API v1 manifest detail, tag list/history, and repository-embedded tags now return `manifest_digests` entries containing `digest`, `algorithm`, `is_enabled`, and `is_preferred`. Existing singular fields retain their prior values. Discovery response schemas document the additive field.
- React now shares absent-versus-empty fallback and preferred-selection logic across tag display, digest search/sort, copy, digest pull commands, navigation, details, tag operations, and history. Disabled identities remain visible but cannot drive normal actions.
- Angular now uses the same identity rules in tag lists, filtering, manifest links/pages, pull dialogs, labels/tag operations, and history. Its manifest link no longer assumes `sha256:` or a fixed prefix length.
- Added model/API tests, React utility/component tests, an Angular manifest-link Jasmine spec, and a dual-UI Playwright test. No generated frontend bundles were added.

Validation completed:

- OCI manifest model: **29 passed**.
- API v1 manifest/tag/repository suites: **89 passed**.
- React: **1,312 passed** across 177 files. Production build passed.
- Angular production build passed. Dual-UI Playwright: **2 passed**, including preferred selection, retained disabled identities, React full-digest copy, and Angular navigation after identity selection.
- Python compilation, Black checks for changed backend files, Prettier checks for changed React/Playwright files, and `git diff --check` passed.
- The legacy Angular Karma runner is not a usable local gate: its installed dependency set omits `d3` and other configured browser files, causing a pre-test `d3 is not defined` failure with zero tests executed. `test:node` is also blocked by the legacy decorator/reflect-metadata harness under Node 25. The successful Angular production build and Chromium Playwright path provide executable Angular coverage.

Post-review corrections separate React manifest-list architecture selection from repository identity selection, load each selected child manifest's own inventory, keep Clair on its pre-Story-22 manifest reference, and avoid passing parent aliases through ordinary manifest-list tag navigation. React and Angular permanent-history cleanup now choose explicit retained registrations and may intentionally select disabled registrations only for that destructive workflow. Added focused tests cover child inventory loading, navigation separation, retained disabled cleanup, digest-pull suppression, selected pull values, and alias search/navigation. Playwright's mocked digest values now use valid algorithm lengths.

## Main finding

The Quay API v1 and both supported UIs still treat one digest as the manifest identity. That is insufficient once a manifest has multiple repository registrations:

- API v1 manifest detail returns `digest` from the identity used to resolve the manifest.
- API v1 tag history/list responses and repository-embedded tags return one `manifest_digest`, currently sourced from the canonical manifest row.
- React types, filtering, links, copy actions, pull commands, details, history, and tag operations consume those singular fields.
- Angular tag lists, history, manifest pages, pull dialogs, and tag operations do the same. Its reusable `manifest-link` additionally recognizes only `sha256:`, strips a fixed seven-character prefix, labels the result `SHA256`, and falls back to an image ID for every other algorithm.

The existing singular fields cannot safely be redefined: tests and clients depend on them, and manifest detail intentionally echoes the successfully resolved route identity. The required design is therefore additive. The new field must come only from explicit rows in `RepositoryManifestDigest`; it must never use the canonical-SHA-256 fallback in `get_repository_manifest_digests` when no registration exists.

## Story 20 API contract

Add `manifest_digests` to all three API v1 response families that currently expose a manifest identity:

1. `GET /api/v1/repository/{repository}/manifest/{manifestref}`: top-level `manifest_digests` for the returned manifest.
2. `GET /api/v1/repository/{repository}/tag/`: `manifest_digests` on every tag-history or active-tag item.
3. `GET /api/v1/repository/{repository}` with `includeTags=true`: `manifest_digests` on every value in the embedded `tags` map.

The field is always present on these successful responses when served by the new backend. This lets a new UI distinguish an old backend, where the field is absent, from a new backend with a legacy manifest that has no explicit registrations, where the field is an empty array.

Each array item has this shape:

| Field | Type | Meaning |
| --- | --- | --- |
| `digest` | string | Complete registered identity, including algorithm prefix |
| `algorithm` | string | Parsed algorithm name, such as `sha256`, `sha384`, or `sha512` |
| `is_enabled` | boolean | Whether the algorithm is in the request-serving instance's current `ALLOWED_HASH_ALGORITHMS` |
| `is_preferred` | boolean | Whether normal tag resolution would select this enabled registration under the existing deterministic selection rules |

Contract rules:

- Return only explicit registrations whose `repository_id` and `manifest_id` both match the authorized repository and returned manifest.
- Never synthesize or return unregistered `Manifest.digest` in `manifest_digests`.
- Keep existing `digest` and `manifest_digest` fields byte-for-byte compatible. Mark them as legacy singular fields in API documentation; do not silently reinterpret them as the registration list or preferred identity.
- Include retained registrations for disabled algorithms with `is_enabled=false`. Inventory is not a pullability claim. This is necessary so authorized destructive lifecycle workflows can still identify retained content.
- Set `is_preferred=true` only for an identity that is both returned and currently enabled. At most one item is preferred. If no returned identity is enabled, every item is false.
- Apply the Story 19 schema-aware boundary: Docker schema 1 may return only its explicitly registered historical canonical SHA-256 identity. Stale SHA-384 or SHA-512 schema 1 rows remain stored for lifecycle cleanup but must not be advertised as usable manifest identities.
- Preserve deterministic order. Use the existing registration creation order (`RepositoryManifestDigest.id`), because current repository-visible selection uses that order after preferring an explicit canonical identity. Do not rely on unordered SQL results.
- Do not add canonical-storage fields, internal IDs, registration row IDs, or blob/layer aliases. Layer `blob_digest` and manifest-list descriptor digests are different contracts.
- Do not materialize legacy registrations during an API inventory GET. A new backend returns an empty array for a manifest with no explicit row; Registry V2's existing successful legacy read remains responsible for lazy SHA-256 materialization.
- Treat `is_enabled` as response-time advisory state. Every operation must still enforce its own server-side authorization, repository scope, schema rules, and eventually Story 28's complete hard-disable policy.

An illustrative response fragment is:

```json
"manifest_digest": "sha256:legacy-value",
"manifest_digests": [
  {
    "digest": "sha384:registered-value",
    "algorithm": "sha384",
    "is_enabled": true,
    "is_preferred": true
  },
  {
    "digest": "sha512:registered-value",
    "algorithm": "sha512",
    "is_enabled": false,
    "is_preferred": false
  }
]
```

The fragment deliberately shows why the legacy singular value and the authoritative registration list must remain separate.

### API implementation shape

Implement one repository-model bulk read, not one query per tag:

- Add an explicit-registration bulk helper in `data/model/oci/manifest.py`. It should accept a repository ID and a bounded collection of manifest rows/IDs, query `RepositoryManifestDigest` once through the existing `(repository_id, manifest_id)` index, and return registrations grouped by manifest ID in registration-ID order. It must not call the fallback-returning helper for empty groups.
- Put schema 1 filtering and preferred-selection logic in the model/registry-model layer so manifest detail, tag history, and repository-embedded tags cannot diverge.
- Add the operation to `data/registry_model/interface.py` and implement it in `data/registry_model/registry_oci_model.py`. Keep endpoint code responsible only for serializing `algorithm`, `is_enabled`, and `is_preferred` against the current app configuration if the registry-layer return type does not carry those values.
- Extend registry datatypes only as needed to transport already-loaded registrations. Avoid lazy per-object properties that create N+1 queries.
- Pass the bulk result into `endpoints/api/tag.py::_tag_dict` for the page of at most 100 tags.
- Extend `endpoints/api/repository_models_interface.py` and `endpoints/api/repository_models_pre_oci.py` so the repository endpoint's embedded map receives the same contract for its existing maximum of 500 tags.
- Add the field in `endpoints/api/manifest.py::_manifest_dict` using the single returned manifest.
- Define and attach JSON response schemas with `define_json_response` for these routes, allowing existing extra properties while making the new field visible in `/api/v1/discovery`. The current discovery output documents request schemas but has no response schema for these routes.

No database migration or index is needed. PostgreSQL already has `repositorymanifestdigest_repository_id_manifest_id`. A read-only live plan for four Story 19 manifest IDs used one index scan, returned seven registrations, and completed in 0.043 ms on the local data set.

### Story 20 correctness and compatibility tests

Add focused coverage in the existing model, registry-interface, and API test files for:

- SHA-256, SHA-384, and SHA-512 registrations on one manifest; exact values, order, algorithm labels, enabled state, and one preferred identity.
- Manifest GET through each registered alias while preserving the existing singular `digest` behavior.
- Active tag list, complete tag history including dead/reversion rows, pagination, `specificTag`, and repository-embedded tags.
- A disabled registration remaining listed but not preferred, then becoming enabled/preferred after a config change without changing stored rows.
- A manifest with no registration returning `manifest_digests: []` without a write or canonical fallback leak.
- Explicit canonical SHA-256 registration versus an unregistered canonical SHA-256 value.
- Docker schema 1 returning only an explicit canonical SHA-256 registration even if stale alternative rows exist.
- Same canonical bytes or digest aliases in another repository never appearing in the response.
- Existing 401/403/404 behavior and repository read authorization remaining unchanged.
- A bounded query-count assertion for 100 tag-history items and 500 embedded tags to prevent N+1 regressions.
- Swagger/discovery response schemas containing the additive field.
- Exact regression assertions for all old singular fields and current response shapes.

Do not expand Story 20 into a registration create/delete API, API-wide digest search endpoint, Clair, builds, events, exports, reporting, or operational repair.

## Story 21 UI contract

Both React and Angular are supported. The running local configuration has `FEATURE_UI_V2: true`, `DEFAULT_UI: "angular"`, and all three algorithms enabled. Story 21 is incomplete if only React changes.

Shared UI behavior:

- Treat `manifest_digests` as authoritative when present. If it is absent, fall back to the legacy singular field for mixed-version compatibility. If it is present but empty, show “No registered digest identities”; do not relabel the singular canonical value as registered.
- Render the algorithm from the prefix; never assume a fixed prefix length or SHA-256 hash length.
- Show a compact algorithm badge plus a shortened hash in tables, with the complete value available to assistive text, tooltip, and copy action.
- Show all registered identities in a selector/popover. Select `is_preferred` by default; otherwise select the first enabled item. If none is enabled, show the retained identities as disabled and select none for normal actions.
- Generate pull-by-digest commands only for an enabled selected identity. Tag-based pull commands remain unchanged.
- Search the digest column across every `manifest_digests[].digest`, not only the legacy singular field. Sort by the displayed selected identity and keep tag grouping/tracks keyed to the existing manifest object, not split into one logical image per alias.
- Permit enabled selected identities in existing navigation, copy, add/retarget, and restore flows. Permit disabled identities only where an existing destructive lifecycle endpoint intentionally bypasses the allowlist, such as permanent tag-history deletion. Disable normal pull, navigation, retarget, and restore actions for disabled identities with a clear reason.
- Re-read state after mutations. Do not trust `is_enabled` as authorization or concurrency control.
- Keep Clair/security requests on their existing identity path until Story 22. Keep cosign naming/correlation behavior unchanged. Keep builds, notifications, webhooks, audit payloads, exports, and admin repair outside Story 21.
- Do not present layer/blob descriptor digests as alternative manifest registrations.

### React implementation areas

- Extend `Tag` and `ManifestByDigestResponse` in `web/src/resources/TagResource.ts` with an optional `manifest_digests` array and a shared identity type. Optionality is required for old-backend compatibility.
- Harden `web/src/components/ManifestDigest.tsx` for arbitrary validated algorithm prefixes and hash lengths, and add a reusable identity selector/list component rather than duplicating selection rules.
- Update `TagsList.tsx`, `TagsTable.tsx`, `TablePopover.tsx`, and the digest search selector so list display, copy, search, and pull commands use the selected registered identity.
- Remove the hard-coded “SHA256” label and fixed `'sha256:'.length` slicing in the expanded tag row.
- In tag details, separate “selected manifest object/architecture” from “selected registered identity.” The current `digest` state serves both purposes. Fetch the selected manifest's detail contract before offering its aliases; this is especially important for child manifests in an OCI index. Do not rewrite raw `manifest_data` descriptors.
- Update `Details.tsx` and `DetailsCopyTags.tsx` so the displayed/copied identity and digest pull commands follow selection while labels, size, and security calls retain their existing integration semantics unless independently proven alias-safe.
- Update tag-history rendering and restore/permanent-delete dialogs to carry the identity selected for that historical manifest, with the enabled/destructive distinction above.
- Preserve current routes. A digest selector need not put aliases into the URL. Existing `?digest=` architecture handling should remain limited to the selected parent/child graph and must not allow an unrelated repository manifest merely because it resolves.

### Angular implementation areas

- Extend repository/tag and manifest response use in `static/js/directives/repo-view/repo-panel-tags.js` and `static/js/pages/manifest-view.js` with the same absent-versus-empty fallback rule.
- Generalize `static/js/directives/ui/manifest-link/manifest-link.component.ts` and its template: parse at the first colon, render the real algorithm, shorten only the hash portion, copy the complete digest, and accept the registration collection/selection state.
- Update `static/directives/repo-view/repo-panel-tags.html` and `static/partials/manifest-view.html` to show/select registered identities without duplicating a tag or manifest per alias.
- Update `static/js/directives/ui/fetch-tag-dialog.js` so digest pull formats use an enabled selected identity and remain absent when no enabled registration exists.
- Update Angular history and tag-operation dialogs so normal actions reject disabled identities while permanent cleanup may use retained registrations.
- Add a derived all-identities search value to the existing `TableService.buildOrderedItems` fields; retain grouping and image tracks by the legacy manifest key so aliases do not fragment the table.
- Do not hand-edit `static/build` bundles. Use the repository's supported Angular build pipeline and commit generated artifacts only if the normal project policy explicitly requires them.

## Story 21 test plan

React unit/component tests should prove:

- SHA-256/SHA-384/SHA-512 labels, shortening, full-value copy, preferred selection, disabled rendering, empty state, and old-API fallback.
- Digest search matches any alias; sort, selection, manifest tracks, and tag bulk selection remain stable.
- Table and detail pull commands contain the chosen enabled full digest and never a disabled one.
- Tag detail separates index architecture selection from identity selection and does not confuse layer descriptors with manifest identities.
- Restore/retarget uses enabled identities; permanent cleanup can retain a disabled identity.
- Existing SHA-256 snapshots and behavior remain unchanged.

Add focused Angular Karma/Jasmine tests for `manifest-link`, tags filtering/selection, the fetch dialog, manifest page display, and history/operation gating. Angular currently has no focused manifest-link or fetch-tag spec, so Story 21 must add them rather than relying only on React tests.

Add Playwright coverage that creates an isolated repository through existing fixtures, publishes a manifest with multiple explicit registrations, and runs against both `/react` and `/angular` modes. Verify:

- every registered algorithm is visible and the full values copy correctly;
- searching by a non-default alias finds the tag;
- choosing SHA-384 or SHA-512 changes the digest pull command and the command succeeds against the registry;
- disabled state suppresses normal pull/restore/retarget but leaves authorized permanent cleanup available;
- schema 1 displays only SHA-256;
- switching UI modes does not change the API contract or selected-value rules.

Restore configuration byte-for-byte after any hard-disable test, use a fresh repository name, and remove only the test repository. Do not reuse or mutate retained Story 17/19 validation repositories.

## Read-only evidence collected

- Quay, PostgreSQL, Redis, and frontend containers were healthy; `/health/instance` returned HTTP 200. The Quay container is bind-mounted to the designated worktree.
- Unchanged API characterization passed: `endpoints/api/test/test_manifest.py`, `test_tag.py`, and `test_repository.py` — **88 passed**.
- Live read-only PostgreSQL inventory found repositories with SHA-256, SHA-384, and SHA-512 registrations. The retained Story 19 repository has four manifests and seven registration rows, including explicit canonical schema 1 registrations and deliberately stale schema 1 alternatives used to prove the boundary.
- `/api/v1/discovery` confirms no response schema on the three target GET routes today. Existing write schemas document singular `manifest_digest` inputs.
- API tag create/retarget, restore, and permanent-delete paths already call repository-scoped `lookup_manifest_by_digest`; registered alternative identities can therefore resolve without a new routing syntax. Deletion/lifecycle paths already have allow-dead or allowlist-independent behavior where required by prior stories.
- Registry registration lookup is repository-scoped, and affected cache invalidation already iterates all registrations. No new identity cache is required for this API field.
- Current React Playwright coverage exercises tag details, architecture switching, pull command copy, tag list digest copy, layers, and history, but it publishes ordinary SHA-256 content only.
- Both UIs are selectable and tested through `/angular` and `/react`; Angular is not removable from this story merely because React is enabled.

## Boundaries and risks

- Story 20 must land before Story 21 or provide the exact contract in the same dependent change series.
- Story 28 remains responsible for complete server-side hard-disable enforcement across Quay integrations. Stories 20/21 must not claim that the response-time status flag itself enforces an operation.
- Story 22 owns Clair alias resolution. Using a selected alternative identity indiscriminately for current security widgets would silently expand scope and can break reports.
- Raw OCI index descriptors identify exact child bytes and must remain unchanged. Child alias display requires fetching that child manifest's own API detail; replacing descriptor digests in `manifest_data` would corrupt the represented content contract.
- API list/history responses can include dead tags whose registrations survive until GC. After GC removes the registration, an old history row can legitimately return an empty identity list while retaining its legacy singular value.
- Mixed-version deployment behavior depends on the absent-versus-empty rule. Do not use JavaScript truthiness in a way that treats `[]` as permission to fall back to canonical SHA-256.
- There is no evidence that a schema or migration is needed. Stop for a scope decision if implementation appears to require either, a new canonical identity field, registration mutation endpoints, or changes to deferred integrations.

## Recommended implementation order

1. Add failing model/interface tests for explicit-only bulk registration inventory, repository isolation, schema 1 filtering, disabled state, and bounded query count.
2. Implement the Story 20 registry-model helper and API serialization; add response schemas and all three endpoint test families.
3. Run the complete API/model/interface regressions and verify unchanged singular fields before starting UI work.
4. Add the shared React identity type/component and unit tests, then integrate tag list, detail, pull, search, history, and operations.
5. Add the equivalent Angular component/controller behavior and Karma tests.
6. Add isolated dual-UI Playwright coverage and live pull verification through selected enabled identities.
7. Run backend suites, React Vitest/type/lint/build checks, Angular Karma/build checks, focused Playwright in both modes, pre-commit, compilation, mypy for changed Python, and both diff checks.
8. Review repository isolation, authorization, N+1 behavior, schema 1 boundaries, disabled cleanup, mixed-version fallback, and unchanged Story 17/19 work before changing either story's status.

---

# Story 19 Implementation Handoff

## Authoritative product decision

Docker schema 1 will remain strictly SHA-256-only. SHA-384 and SHA-512 schema 1 manifest identities and layer descriptors are **not required and will not be implemented**. Story 19 must create a complete, explicit, tested `UNSUPPORTED` boundary around PQC/configurable-digest interactions while preserving all historical SHA-256 behavior.

Story 19 is not authorized to fix unrelated Docker schema 1 defects that existed before the PQC work. A production change is in scope only when the failure requires at least one PQC capability: an alternative repository registration, the configurable algorithm allowlist or SHA-256 hard-disable, generic registered-digest resolution, or alternative-digest publication/conversion. If a failure reproduces with ordinary SHA-256 schema 1 behavior without PQC features, classify it as pre-existing, document it, and stop for a scope decision rather than fixing it.

This decision supersedes the earlier investigation option that allowed safe alternative schema 1 semantics. There is no alternative-semantics design branch to pursue.

## Objective

Implement and verify a comprehensive Docker schema 1 SHA-256-only boundary across Registry V2 publication, reads, tag selection, registered aliases, descriptors, conversion, deletion, caches, and lifecycle compatibility. Preserve Docker's historical signed-payload digest, signatures, embedded repository/tag behavior, rewriting and re-signing, SHA-256 conversions, and cleanup paths exactly unless a failing PQC-specific test proves a narrow boundary change is necessary.

Do not assume every characterization failure authorizes production work. Separate PQC product failures, pre-existing defects, test defects, command defects, and environment failures.

## Repository state to preserve

- Worktree: `/Users/shossain/QuayWorkspace/shaon-feature-PQC`
- Branch: `shaon-feature-PQC`
- HEAD at this planning update: `ad50cf9b983726edb0a1db2aef94399ab1d40230`
- HEAD subject: `NO-ISSUE: fix(gc): bound abandoned upload cleanup retries`
- `upstream/master` at the prior handoff: `d81004d24669132d45df8fbd1eafc86c149e38fa`
- Prior merge base: `d81004d24669132d45df8fbd1eafc86c149e38fa`
- Story 17 code/test diff SHA-256, excluding `HANDOFF.md`: `7e8ab50e2c777db5346c376e56ad346556b6ec1e12f4b3343e92787c893a636e`
- Existing modified Quay files recorded by the prior handoff are `data/model/gc.py`, `data/model/oci/tag.py`, `data/model/storage.py`, `data/model/test/test_gc.py`, `util/secscan/v4/api.py`, `util/secscan/v4/test/test_v4_api.py`, `workers/gc/gcworker.py`, `workers/gc/test/test_gcworker.py`, and this handoff.
- Story 18 is Deferred by explicit product decision. Failed publication may continue to leave physical bytes unreferenced; do not add physical-orphan recovery during Story 19.
- This planning-only update changed no production or test code and ran no tests. The implementation session must verify current repository state rather than assuming the recorded state is unchanged.

Do not reset, restore, rebase, amend, discard, overwrite existing work, alter remotes, push, update a pull request, or access `/Users/shossain/QuayWorkspace/11537-pqc-schema` or another worktree.

## Required start sequence

1. Read this entire handoff and `/Users/shossain/QuayWorkspace/shaon-feature-PQC/AGENTS.md` completely.
2. Read the relevant database, testing, architecture, registry endpoint, schema 1, digest, conversion, registry-model, and OCI-model documentation and code listed below.
3. Read `/Users/shossain/Project_Documents/Quay_Post_Quantum_Cryptography/AGENTS.md` and the relevant Story 19 and Demo 14 sections of `TODO.md` and `PQC-Features.md`.
4. Audit branch, HEAD, upstream, merge base, status, staging, untracked files, worktrees, history, and complete committed/uncommitted diffs without accessing another worktree.
5. Recompute the Story 17 code/test hash above and preserve every existing change. If it differs, explain the difference before proceeding.
6. Create only the new implementation session's `.PITASKS.md` section. Update planning status only as permitted by the user's instructions and without disturbing unrelated planning changes.
7. Run untouched baseline tests before adding characterization.
8. Use test-first development and keep the failure classifications separate.

## Confirmed current behavior and PQC-specific gaps

Static inspection confirmed the following. The implementation session must prove them with tests before changing production code.

- `endpoints/v2/manifest.py::write_manifest_by_digest` already rejects SHA-384/SHA-512 schema 1 route identities when those algorithms pass the generic allowlist check. SHA-256 compares against `parsed.digest`, Docker's historical schema 1 payload identity.
- Schema 1 digest PUT tag query parameters are already rejected.
- `endpoints/v2/test/test_manifest.py::test_schema1_digest_push_remains_sha256_only` covers enabled SHA-384/SHA-512 rejection and successful SHA-256 PUT, but not the complete boundary.
- Generic repository manifest registration and resolution are schema-agnostic. A deliberately inserted alternative registration can identify a schema 1 manifest on generic lookup paths.
- Generic tag identity selection can choose an alternative registration and can allow any registration to hide the canonical SHA-256 fallback.
- Generic descriptor validation accepts SHA-384/SHA-512 schema 1 `blobSum` values when enabled.
- Schema 2 and OCI schema 1 conversion currently copy source layer digest strings into generated schema 1 `blobSum` entries. An alternative-only source graph can therefore produce an unusable or unsupported schema 1 representation.
- Digest GET/HEAD and digest DELETE do not currently apply a schema-aware alternative-alias boundary after resolving a registration.
- These gaps arise from applying PQC's generic registered-identity behavior to schema 1. The historical schema 1 digest, signing, retargeting, and all-SHA-256 conversion implementations predate PQC and are preservation targets, not rewrite targets.

## Required external contract

| Path | Required Story 19 behavior |
| --- | --- |
| Schema 1 PUT by SHA-384/SHA-512 digest | HTTP 400 `UNSUPPORTED`; no manifest, tag, registration, graph, quota, or cache mutation |
| Schema 1 PUT containing a SHA-384/SHA-512 `blobSum` | HTTP 400 `UNSUPPORTED` before blob lookup or mutation |
| GET/HEAD through an alternative registration pointing to schema 1 | HTTP 400 `UNSUPPORTED`; never return manifest bytes or establish supported cache behavior |
| Schema 1 tag pull | Select only the canonical historical SHA-256 identity; ignore alternative registrations |
| Schema 1 read/write while SHA-256 is disabled | HTTP 400 `UNSUPPORTED` with reason `disabled`; retain registrations, graph, tags, and bytes |
| DELETE through an alternative schema 1 identity | HTTP 400 `UNSUPPORTED`; do not expire tags or mutate lifecycle state |
| DELETE through canonical SHA-256 | Preserve authorized cleanup even when SHA-256 is disabled |
| Retarget/re-sign | Preserve current behavior and create the historical SHA-256 payload identity for the rewritten manifest |
| Schema 2/OCI/list/index conversion with an all-SHA-256 emitted schema 1 representation | Preserve existing bytes, content type, digest, signature behavior, architecture selection, and tag semantics |
| Conversion that would emit a SHA-384/SHA-512 `blobSum` | HTTP 400 `UNSUPPORTED`; do not synthesize canonical registrations or expose unregistered canonical values |
| Existing alternative schema 1 registrations | Retain for normal deletion/GC cleanup, but never use for reads, tag selection, or alternative-identity deletion |
| Schema 1 child referenced by an alternative descriptor in a manifest list | Reject before graph publication; do not use an alternative alias to establish schema 1 semantics |

### Error precedence to implement

- Authentication and authorization decorators retain their existing precedence.
- Malformed digest syntax remains `DIGEST_INVALID` with reason `malformed`.
- Unknown digest algorithms remain `UNSUPPORTED` with reason `unsupported`.
- A well-formed SHA-384/SHA-512 identity known to apply to schema 1 is unsupported even if that algorithm is disabled globally, because the schema 1 capability does not exist.
- Canonical SHA-256 schema 1 reads and writes use reason `disabled` when SHA-256 is disabled.
- A cross-repository alternative digest that has no registration in the requested repository remains `MANIFEST_UNKNOWN`; do not probe or leak another repository merely to report the schema 1 boundary.
- Deletion through canonical SHA-256 remains allowlist-independent. Deletion through an alternative schema 1 alias is unsupported rather than a supported cleanup identity.

A digest-addressed GET cannot know the target media type from the route alone while alternative identities remain valid for schema 2 and OCI. Therefore, “before lookup” is not a realizable requirement for this read path. The enforceable boundary is: resolve only within the authorized repository, reject schema 1 before returning bytes or mutating state, and ensure cold or primed caches cannot bypass the rejection.

## Scope classification gate

Before each production change, answer:

1. Does the failing test require an alternative digest registration, configurable allowlist state, hard-disable, or another PQC behavior?
2. Does the proposed fix leave the equivalent all-SHA-256 behavior unchanged?
3. Can the fix be located in PQC endpoint/model selection and validation rather than historical schema 1 parsing, signing, or rewriting?
4. Does it avoid migrations, alias repair, deferred integrations, and physical storage recovery?

If the answer to any question is no, stop and request a scope decision.

## Implementation plan

### Phase 1: preservation and untouched baseline

- Verify repository state and the Story 17 hash before modification.
- Read and trace all code paths listed under “Code paths to trace.”
- Run the existing schema 1 unit, endpoint, model, conversion, registry-interface, and focused protocol baselines on SQLite.
- Run the database-sensitive baseline on PostgreSQL.
- Record failures exactly; do not weaken or rewrite existing assertions.

### Phase 2: test-first PQC characterization

Add focused tests before production changes.

#### Publication

- Signed and unsigned schema 1 digest PUT using SHA-384 and SHA-512 while enabled.
- The same requests while the route algorithm is disabled, proving capability precedence.
- Canonical SHA-256 digest PUT while enabled and disabled.
- Tag-addressed schema 1 PUT while SHA-256 is enabled and disabled.
- Alternative, disabled, malformed, unknown, hidden, and cross-repository schema 1 `blobSum` values.
- Schema 1 tag query parameter rejection.
- Exact zero-mutation assertions for manifests, tags, `RepositoryManifestDigest`, `ManifestBlob`, `ManifestChild`, quota state, notifications, and relevant cache entries.

#### Reads and caches

- Deliberately insert a SHA-384/SHA-512 `RepositoryManifestDigest` for an existing schema 1 manifest to represent stale or corrupted PQC state.
- GET and HEAD through that alias with cold and primed manifest caches.
- Canonical SHA-256 GET and HEAD remain available and may materialize the historical SHA-256 registration without exposing the alternative alias.
- Alternative aliases cannot hide canonical SHA-256 tag or digest behavior.
- Disable SHA-256 while an alternative algorithm remains enabled; tag, canonical digest, and alternative digest requests must not bypass the hard disable.
- Re-enable SHA-256 and prove immediate recovery without rewriting content.
- Cover authorization, repository isolation, malformed and unknown identities, repeated operations, and no cross-repository leakage.

#### Tag selection, retargeting, and re-signing

- Tag pulls select only canonical SHA-256 for schema 1, even when alternative registrations exist first or exclusively.
- Existing lazy SHA-256 materialization remains idempotent and repository-scoped.
- Retargeting a schema 1 manifest to a different tag rewrites and re-signs exactly as before.
- The rewritten manifest has its own historical payload SHA-256 identity; no alternative registration is inherited or synthesized.
- Embedded namespace, repository, and tag mismatch behavior remains unchanged unless a PQC-specific bypass is proven.

#### Deletion and lifecycle

- Alternative schema 1 digest DELETE returns exact `UNSUPPORTED` before tag expiry or cache invalidation.
- Canonical SHA-256 digest DELETE still works while SHA-256 is disabled.
- Tag deletion, repository purge, namespace purge, registration GC, canonical manifest/blob GC, scanner cleanup, and repeated cleanup remain independent of the active allowlist.
- Existing alternative registration rows are removed only through established lifecycle cleanup; do not add a repair or migration path.
- Story 18 physical bytes without database state remain untouched.

#### Conversion and manifest lists

- Characterize schema 2 and OCI single-manifest conversion to schema 1.
- Characterize Docker manifest-list and OCI-index amd64/linux selection and conversion.
- For all-SHA-256 inputs, assert exact existing returned bytes, historical digest, media type, signature validity, layer order, architecture selection, and zero repository-registration synthesis.
- For source manifests whose generated schema 1 would contain SHA-384/SHA-512 `blobSum` values, assert precise `UNSUPPORTED` and no database/cache mutation.
- Reject publication of a manifest-list descriptor that explicitly identifies a schema 1 child by SHA-384/SHA-512.
- Do not create SHA-256 aliases solely to make an alternative-only graph consumable by a schema 1 client.

#### Historical characterization only

Add or retain regression evidence for signed JWS payload reconstruction, unsigned payload hashing, signature verification, malformed payloads, Unicode, embedded names/tags, image-ID rewriting, all-SHA-256 conversion, repeated push/pull, delete, and GC. A failure that reproduces without PQC features is not automatically in scope for production correction.

### Phase 3: minimal PQC-only production enforcement

Production edits are conditional on the failing tests. The likely change set is:

- `endpoints/v2/manifest.py`
  - Complete schema 1 route, descriptor, response, conversion, cache-result, and deletion enforcement.
  - Preserve non-schema-1 SHA-384/SHA-512 behavior and existing error contracts.
  - Validate stored or generated schema 1 before returning bytes.

- `data/registry_model/registry_oci_model.py`
  - Force allowlist-aware schema 1 tag selection to canonical SHA-256.
  - Prevent cold or already-primed caches from exposing an alternative schema 1 identity.
  - Preserve retarget/re-sign and all-SHA-256 conversion behavior.

- `data/model/oci/manifest.py`
  - Prevent PQC registration helpers from creating SHA-384/SHA-512 schema 1 registrations.
  - Ensure a stale alternative registration cannot hide canonical schema 1 SHA-256 lookup and materialization.
  - Reject alternative descriptors that explicitly identify schema 1 children before graph publication.

Avoid changing `image/docker/schema1.py`, JWS signing, payload reconstruction, image-ID rewriting, `data/model/oci/tag.py`, schema 2/OCI conversion algorithms, or GC unless a failing PQC-specific test proves endpoint/model enforcement cannot establish the contract. Any such need is a review checkpoint, not automatic scope.

### Phase 4: focused and broad validation

Run focused Story 19 tests on SQLite and PostgreSQL where database behavior is involved, then run complete affected suites:

- `image/docker/test/test_schema1.py`
- `image/docker/schema2/test/test_manifest.py`
- `image/docker/schema2/test/test_list.py`
- `image/docker/schema2/test/test_conversion.py`
- relevant OCI manifest and index conversion tests
- `endpoints/v2/test/test_manifest.py`
- `endpoints/v2/test/test_blob.py` when descriptor behavior is exercised
- `data/model/oci/test/test_oci_manifest.py`
- `data/model/oci/test/test_oci_tag.py`
- `data/registry_model/test/test_interface.py`
- relevant schema 1 selections in `test/registry/registry_tests.py`
- focused Story 13 legacy compatibility, Story 14 deletion, Story 15 purge, Story 16 upload, and Story 17 GC regressions

Also run targeted mypy for every changed production file, Python compilation for every changed Python file, pre-commit over every changed Quay file, `git diff --check`, and `git diff --check upstream/master`. Use PostgreSQL for registration materialization, transaction rollback, and any concurrency-sensitive evidence. Do not run destructive live cleanup against shared state.

### Phase 5: final review and documentation

- Review the complete diff for repository isolation, authorization, digest/error precedence, mutation ordering, cache bypasses, hidden canonical exposure, disabled-algorithm behavior, conversion correctness, deletion safety, and lifecycle independence.
- Confirm all equivalent all-SHA-256 paths remain unchanged.
- Update this Story 19 section with root cause, exact changed files, commands and outcomes, PostgreSQL evidence, failure classifications, remaining risks, and any pre-existing issues deliberately left unchanged.
- Confirm explicitly that no migration, physical-orphan recovery, or deferred integration entered scope.
- Mark Story 19 Done only when the complete SHA-256-only boundary and corrective definition of done are satisfied.

## Code paths to trace

- `image/docker/schema1.py`: signed/unsigned payloads, historical digest, signatures, embedded names/tags, `blobSum`, rewriting, and signing.
- `image/docker/schema2/manifest.py`, `image/docker/schema2/list.py`, `image/oci/manifest.py`, and `image/oci/index.py`: generated schema 1 representations and platform selection.
- `endpoints/v2/manifest.py`: PUT by tag/digest, GET/HEAD, content negotiation, response digest, conversion, DELETE, errors, and namespace checks.
- `data/registry_model/registry_oci_model.py`: registration-aware lookup, tag selection, cache behavior, conversion, retarget/re-sign, deletion, and legacy-image adapters.
- `data/model/oci/manifest.py` and `data/model/oci/tag.py`: canonical lookup, repository registrations, lazy SHA-256 materialization, graph publication, schema 1 tag restrictions, and lifecycle behavior.
- `data/model/oci/retriever.py`, `data/registry_model/datatypes.py`, and `data/registry_model/manifestbuilder.py`: alias resolution, legacy image behavior, and generated schema 1 construction.
- `test/registry/protocol_v2.py`, `test/registry/registry_tests.py`, schema 1 unit tests, conversion tests, endpoint tests, OCI model/tag tests, and registry-interface tests.

## Non-goals and stop conditions

Do not implement:

- SHA-384/SHA-512 Docker schema 1 semantics.
- A change to Docker's historical payload SHA-256 identity or signature format.
- A fix for a defect reproducible on an equivalent pre-PQC all-SHA-256 path without explicit approval.
- A schema, migration, index, configuration key, or stale-registration repair job.
- Repository-visible aliases for generated schema 1 bytes or hidden canonical blob identities.
- Proxy cache, mirroring, imports, builds, Clair lookup, API/UI, notifications, webhooks, events, export/reporting, operational tools, or physical-orphan recovery.
- Changes to Stories 9, 11, 12, or 18.

Stop and request a scope decision if satisfying the contract appears to require changing historical schema 1 parsing/signing/rewriting, exposing unregistered canonical values, synthesizing registrations for conversion, a migration, a deferred integration, or correction of a pre-existing non-PQC defect.

## Copy-ready prompt for the implementation session

```text
Work only in `/Users/shossain/QuayWorkspace/shaon-feature-PQC`. Do not access another worktree.

Before responding, read completely:

- `/Users/shossain/QuayWorkspace/shaon-feature-PQC/AGENTS.md`
- `/Users/shossain/QuayWorkspace/shaon-feature-PQC/HANDOFF.md`
- `/Users/shossain/Project_Documents/Quay_Post_Quantum_Cryptography/AGENTS.md`
- The relevant Story 19 sections of the planning repository's `TODO.md` and `PQC-Features.md`
- Every repository document and code path required by the Story 19 section of `HANDOFF.md`

Treat `HANDOFF.md` as authoritative for repository preservation, completed Stories 17–18 decisions, the explicit product decision that Docker schema 1 remains SHA-256-only, the PQC-only scope gate, the implementation contract, test strategy, validation requirements, and stop conditions.

Implement Story 19 using test-first development. Do not design or implement SHA-384/SHA-512 Docker schema 1 semantics. Fix only failures caused by PQC/configurable-digest capabilities. If a failure reproduces on an equivalent ordinary all-SHA-256 pre-PQC path, classify and document it, then stop for a scope decision rather than fixing it.

Before modifying production code:

1. Audit and report branch, HEAD, upstream, merge base, status, staging, untracked files, worktrees, history, and complete committed/uncommitted diffs.
2. Recompute and verify the Story 17 code/test hash recorded in `HANDOFF.md`.
3. Preserve every existing change and create only this session's `.PITASKS.md` section.
4. Run the untouched SQLite and PostgreSQL baselines required by `HANDOFF.md`.
5. Add focused failing Story 19 characterization tests.

Then implement the smallest PQC-only endpoint/model changes required by those tests. Preserve historical schema 1 payload hashing, signatures, rewriting, retargeting, all-SHA-256 conversion, deletion, and GC behavior. Keep Story 18 and every deferred integration out of scope.

Classify product failures, pre-existing defects, test defects, command defects, and environment failures separately. Run the full validation matrix in `HANDOFF.md`, perform the final security and compatibility review, and update `HANDOFF.md` with exact changed files, commands, outcomes, PostgreSQL evidence, remaining risks, and scope confirmation.

Start by giving me a concise summary of the preserved state, the SHA-256-only contract, the PQC-only scope gate, and the first failing-test slice you will add. Then proceed with the plan unless repository state conflicts with `HANDOFF.md`; if it conflicts, stop and report the conflict.
```

## Story 19 implementation outcome

### Status

Story 19 implementation is complete. Docker schema 1 remains strictly canonical SHA-256. SHA-384 and SHA-512 can still be configured for schema 2 and OCI content, but they cannot become Docker schema 1 route identities, registrations, response digests, generated layer descriptors, child descriptors, tag-selected identities, or deletion identities.

The worktree remained on `shaon-feature-PQC` at pre-Story-17 HEAD `ad50cf9b983726edb0a1db2aef94399ab1d40230`. Before Story 19 edits, the preserved code/test diff excluding `HANDOFF.md` still matched `7e8ab50e2c777db5346c376e56ad346556b6ec1e12f4b3343e92787c893a636e`. Existing Story 17 and planning changes were not reverted, staged, or committed.

### Root cause and implementation

Generic configurable-digest support treated Docker schema 1 like schema 2 and OCI content at several shared boundaries. This allowed a requested SHA-384/SHA-512 digest or a stale `RepositoryManifestDigest` row to identify schema 1 bytes, hide the canonical SHA-256 lookup, influence tag selection, survive cache priming, enter graph publication, or reach deletion. Conversion also needed an output check because generated schema 1 descriptors are protocol-defined SHA-256 values even when their source manifest uses another configured digest.

The correction is deliberately schema-aware and local:

- `endpoints/v2/manifest.py` rejects alternative schema 1 publication before graph, tag, quota, alias, or blob mutation; rejects stale alternative identities after repository-local resolution on GET/HEAD/DELETE; validates converted schema 1 response and layer descriptors; and rejects graph descriptors that resolve locally to schema 1 through a stale alternative alias.
- `data/model/oci/manifest.py` preserves canonical SHA-256 lookup when stale registrations exist, lazily and idempotently materializes the canonical registration, always selects canonical SHA-256 for schema 1, and rejects every noncanonical schema 1 registration, including a different SHA-256 value.
- `data/registry_model/registry_oci_model.py` allows schema 1 tags to expose only the canonical SHA-256 identity and treats the tag as unavailable when SHA-256 is disabled.
- Endpoint checks preserve authentication and strict parsing precedence. A known repository-local schema 1 alternative is `UNSUPPORTED` even when SHA-384/SHA-512 is disabled. An unregistered disabled digest retains the established generic `disabled` response, avoiding a compatibility regression or cross-repository probe.
- Stale alternative rows are not repaired on read. They are ignored for serving and remain available to normal repository/namespace GC cleanup. Canonical deletion remains independent of the configurable alternative-digest allowlist; deleting through an alternative identity is mutation-free and unsupported.
- Generated/retargeted schema 1 output remains signed and canonical. Existing schema 1 payload hashing, JWS construction, embedded-name rewriting, image-ID rewriting, `blobSum` behavior, and all-SHA-256 conversion code were not changed.

Story 19 production changes are limited to:

- `endpoints/v2/manifest.py`
- `data/model/oci/manifest.py`
- `data/registry_model/registry_oci_model.py`

Story 19 coverage or fixture clarification was added in:

- `endpoints/v2/test/test_manifest.py`
- `data/model/oci/test/test_oci_manifest.py`
- `data/registry_model/test/test_interface.py`
- `workers/test/test_namespacegcworker.py`
- `workers/test/test_repositorygcworker.py`

No schema, migration, index, configuration key, schema 1 parser/signer, storage behavior, deferred integration, physical-orphan recovery, or Story 18 implementation was added.

### PostgreSQL through Podman

Docker was not installed (`/bin/bash: docker: command not found`). This was an environment limitation, not a product failure. PostgreSQL validation was completed with Podman 6.0.2 instead:

- Container: `quay-pqc-postgres`
- Image: `docker.io/library/postgres:18`
- Server observed during validation: PostgreSQL 18.6
- Host endpoint: `localhost:55432`
- Test URI: `postgresql://quay:quay@localhost:55432/quay`
- The isolated database was recreated and `CREATE EXTENSION pg_trgm` was applied after the first Alembic bootstrap reported the missing required extension.

Focused PostgreSQL iterations passed with **34 passed**, then **36 passed** after model-level enforcement. The final strict registration slice passed **4 passed**. Stories 13–17 PostgreSQL regression coverage passed **41 passed**.

The final broad database-sensitive command was:

```text
TEST=true TEST_DATABASE_URI='postgresql://quay:quay@localhost:55432/quay' PYTHONPATH=. \
  .venv/bin/python -m pytest -q --tb=short --disable-warnings \
  endpoints/v2/test/test_manifest.py \
  data/model/oci/test/test_oci_manifest.py \
  data/registry_model/test/test_interface.py \
  workers/test/test_repositorygcworker.py \
  workers/test/test_namespacegcworker.py \
  -k 'not test_manifest_registration_conflict_rolls_back_new_graph and not test_story6_parent_registration_conflict_rolls_back_graph_and_tag and not test_mount_rejects_destination_digest_remap'
```

Result: **269 passed, 4 deselected**. This includes the PostgreSQL live canonical schema 1 registration race after its fixture was made schema-aware.

A complete unfiltered PostgreSQL attempt produced **266 passed, 6 failed**. Two Story 19 effects were corrected: the bounded schema check raises the repeated schema 1 PUT ceiling from 27 to 28 queries, and a configurable registration race that selected an unspecified first manifest now exercises canonical schema 1 registration rather than attempting a forbidden SHA-512 schema 1 alias. The four remaining failures are pre-existing PostgreSQL test-transaction defects and were deliberately not changed:

- `test_manifest_registration_conflict_rolls_back_new_graph[sha384]`
- `test_manifest_registration_conflict_rolls_back_new_graph[sha512]`
- `test_story6_parent_registration_conflict_rolls_back_graph_and_tag`
- `test_mount_rejects_destination_digest_remap`

Each test replaces `db_transaction` with `db.transaction()` while already running inside pytest's PostgreSQL transaction. The product reaches the expected conflict, but rollback also removes fixture state inserted before the nested transaction, so the postcondition cannot find that fixture. The blob-mount case does not execute any Story 19 code, all four pass on SQLite, and changing this generic transaction harness would violate the PQC-only scope gate.

### Final validation evidence

Untouched baselines before implementation passed:

- Schema 1/schema 2/OCI image and conversion baseline: **71 passed**.
- Focused SQLite endpoint/model/interface baseline: **32 passed**.
- Focused registry protocol baseline: **20 passed**.
- Focused PostgreSQL baseline: **16 passed**.

End-state evidence:

- Complete affected SQLite set (`endpoints/v2/test/test_manifest.py`, OCI manifest model, registry interface, repository GC, and namespace GC): **271 passed, 2 expected PostgreSQL-only skips**.
- Earlier wider affected SQLite matrix, including the other selected model/interface/worker regressions: **426 passed, 3 expected skips**.
- Schema 1, schema 2, OCI image, and conversion suite: **71 passed**.
- Image/schema plus selected protocol compatibility slice: **78 passed**.
- Broad registry protocol slice: **279 passed**.
- GC/security-scanner regression slice: **21 passed**.
- Migration tests: **2 passed**; no Story 19 migration exists.
- Final pre-commit over every modified tracked file: passed, including hardcoded-secret detection, Black, isort, flake8, configuration checks, whitespace, and EOF checks.
- Targeted mypy over eight affected production modules: passed with no issues.
- Python compilation of the three Story 19 production modules: passed.
- `git diff --check` and `git diff --check upstream/master`: passed.
- Final Story 19 code/test diff SHA-256 across the eight files listed above, excluding handoff/task documentation: `232fdf5075526d88f5094d7b0a752b9b4967f39ec8b7ca4d855ca8f58c3bad48`.

One attempted regression command named nonexistent `data/model/test/test_storage.py`; this was a command defect and was rerun with valid paths. Ambiguous generic configurable-digest and namespace-GC fixtures that accidentally selected schema 1 were narrowed to explicit non-schema-1 behavior or direct stale-row construction rather than weakening the production guard. A final error-precedence experiment expecting a disabled unregistered cross-repository digest to become `MANIFEST_UNKNOWN` contradicted established coverage; the test-only assumption was removed and the existing generic `disabled` contract was retained.

### Security, compatibility, and residual risk review

- Authorization decorators and repository-scoped resolution remain in their original order. No global alias lookup or cross-repository existence oracle was added.
- Rejections happen before publication/deletion mutation. Snapshot coverage includes manifests, digest registrations, blobs, graph links, tags, and quota rows.
- Cold and primed cache behavior, repository isolation, malformed/unsupported/disabled precedence, SHA-256 hard-disable, canonical recovery, and stale-row preservation are covered.
- Canonical all-SHA-256 GET/HEAD/tag/PUT/DELETE, signing, conversion bytes, retarget/re-sign, and GC lifecycle remain green.
- Generic SHA-384/SHA-512 schema 2 and OCI support remains green.
- No MySQL run was performed. The Podman PostgreSQL run covers the required registration materialization and live unique-index race; the four unrelated PostgreSQL transaction-fixture failures above remain visible technical debt.
- No destructive cleanup was run against shared state. The Podman database was isolated for this validation.

Story 19 satisfies the local definition of done. Stories 9, 11, 12, and 18 remain deferred, and all integrations listed as Story 19 non-goals remain outside this change.

---

# Story 18 Deferred Handoff

## Status and accepted decision

Story 18 is **Deferred** by explicit product decision. Quay will retain its existing storage-first publication behavior: if database publication fails after physical finalization, unreferenced canonical bytes may remain in storage indefinitely. No physical-orphan discovery or deletion worker will be added in the current PQC delivery sequence.

This does not block core configurable-digest publication, pull, tagging, mounting, artifact, referrer, deletion, or registration-aware GC behavior. It retains an existing upstream Quay storage-leak limitation rather than introducing a new deletion or data-corruption risk. `upstream/master` already finalizes physical storage before `commit_blob_upload` creates database state.

No Story 18 production code, tests, schema, migration, configuration, worker, or storage-driver behavior was added. A future implementation still requires either migration-backed publication/recovery state or the backend-wide inventory, durable queue state, grace period, and primary-database synchronization described below. A local/S3-only, best-effort, unbounded, or grace-period-only implementation remains unacceptable.

## Exact recovery contract derived from code

A conforming implementation must scan only finalized canonical paths of the form `sha256/<prefix>/<hex>`, never `uploads/`, Swift `segments/`, legacy `sharedimages/`, or repository-visible SHA-384/SHA-512 identities. For each configured physical location it must:

- enumerate a fixed-size age-aware page, persist the backend cursor, and resume after process failure without starving sparse or later keys;
- defer objects younger than a fixed safe grace period;
- durably and idempotently retry each failed deletion;
- acquire the same primary-database critical synchronization used by publication, then recheck immediately before deletion that no `ImageStorage` exists for the canonical checksum and that no durable publication intent is active;
- treat any `ImageStorage` row as a global reference. Its registrations, placements, manifest graph, uploads, repositories, namespaces, and replication state are thereby preserved transitively;
- treat a missing object as successful convergence, while treating partial or uncertain backend state as a retryable failure;
- remain independent of `ALLOWED_HASH_ALGORITHMS` and never create a registration or repository-visible identity.

CAS paths contain no repository or namespace identity. Recovery therefore must use global primary-database absence; repository-scoped discovery is neither possible nor safe.

## Root cause and publication trace

`data/registry_model/blobuploader.py::_BlobUploadManager.commit_to_blob` calls `prefetch_to_storage` before `commit_prefetched_blob`. `_finalize_blob_storage` moves or copies the temporary upload to canonical SHA-256 storage while the database connection is deliberately closed. Only afterward does `OCIModel.commit_blob_upload` transactionally create `ImageStorage`, `ImageStoragePlacement`, `UploadedBlob`, and `RepositoryBlobDigest` and delete `BlobUpload`.

If that transaction fails, `complete_when_uploaded` invokes `cancel_upload`. Because `prefetched_digest` is set, cancellation intentionally does not remove the canonical object; it deletes the remaining `BlobUpload` row. This is the exact physical-orphan window. The same ordering is used by normal Registry V2, legacy V1, and the deferred proxy prefetch primitive. Manifest, index, and artifact publication stores manifest bytes in PostgreSQL and references already-published blobs; it does not create a second physical manifest CAS object.

Storage replication copies a referenced CAS object to a destination before adding `ImageStoragePlacement`. A failed placement write can leave an unrecorded destination copy, but the canonical checksum still has an `ImageStorage` owner. Under the required invariant it must be preserved, not treated as a physical orphan.

## Why the current architecture is insufficient

- `BaseStorage`/`BaseStorageV2` and `DistributedStorage` expose no finalized-object inventory or object-age API. Local and fake storage expose no listing; Azure and Swift expose no Quay inventory wrapper; cloud listing exists only inside upload/MPU cleanup; MultiCDN delegates only its existing methods.
- There is no durable CAS inventory cursor or physical-deletion retry queue. `QueueItem` can store such state, but no queue lifecycle, per-location seed, or continuation protocol exists.
- A grace period does not close the publication race. A concurrent re-upload can find an old canonical object, cancel its temporary bytes, and then publish database state without refreshing the old object's modification time.
- `BLOB_DELETE_<digest>` Redis locking does not span `_finalize_blob_storage` through database publication. `GlobalLock` explicitly says Redis is not tier 1 and must not protect critical code. Existing blob creation also falls back to lockless creation when the lock is unavailable.
- `BlobUpload` has canonical SHA-256 hash state but no queryable canonical digest, finalized timestamp, or foreign key to an `ImageStorage` reservation. An orphan has no database row on which recovery can take a row lock.

## Failure characterization

- **Finalized CAS bytes with no database record / publication rollback:** confirmed by the existing prefetch characterization on SQLite and PostgreSQL. Code ordering shows a later transaction failure leaves the finalized object and cancellation removes the upload row.
- **Live content adjacent to an orphan:** an indexed exact `ImageStorage.content_checksum` lookup can distinguish each candidate, but inventory does not exist.
- **Publication/recovery race:** unsafe without a shared primary-database synchronization point, even with object age and an immediate recheck.
- **Repeated cleanup / deletion failure:** deletion is idempotent on local, fake, and cloud missing objects, but Azure and Swift surface missing/delete errors differently. Durable retry state is absent.
- **Multiple locations and shared paths:** the same canonical path exists independently per location and can be shared by all repositories. Any global `ImageStorage` owner must preserve every copy under the stated invariant.
- **Missing objects and partial backend state:** S3 multipart data, cloud `uploads/`, Azure upload blobs, and Swift segments have backend-specific lifecycle behavior. Existing upload, MPU, and Swift chunk cleanup must remain separate from finalized-CAS recovery.
- **Sparse inventories / more than one batch:** no backend-neutral continuation token or durable cursor exists, so bounded exhaustive progress cannot currently be guaranteed.
- **Disabled algorithms:** irrelevant to physical discovery because final paths and `ImageStorage.content_checksum` remain canonical SHA-256.
- **Repository/namespace isolation:** finalized CAS keys encode neither; only global absence is valid.
- **Temporary versus finalized objects:** temporary keys use `uploads/` or Swift `segments/`; finalized CAS uses `sha256/...`. Existing `BlobUploadCleanupWorker`, cloud partial-upload/MPU cleanup, and `ChunkCleanupWorker` own the temporary paths.

## PostgreSQL and storage evidence

Read-only PostgreSQL inspection found `imagestorage_content_checksum` on `ImageStorage.content_checksum`. The required global preservation probe is constant size:

`SELECT 1 FROM imagestorage WHERE content_checksum = $1 LIMIT 1`.

The tiny local table selected a sequential scan at normal cost. With `enable_seqscan=off`, PostgreSQL used an `Index Only Scan` on `imagestorage_content_checksum` with an index condition on the candidate checksum. This proves the immediate primary-database recheck is bounded and index-capable without another index. No destructive storage or database operation was run.

Backend inspection found modification time only through backend-specific APIs: S3 listing returns `LastModified`; Azure blob properties and Swift object metadata can provide age but are not normalized; local `stat` is not exposed; fake storage has no age. No storage driver offers a bounded finalized-CAS page contract.

## Commands and outcomes

All Quay commands ran from `/Users/shossain/QuayWorkspace/shaon-feature-PQC` with `TEST=true PYTHONPATH=.` unless noted.

- Story 17 diff verification: `git diff --no-ext-diff -- . ':!HANDOFF.md' | shasum -a 256` before Story 18 changes — **passed**, `7e8ab50e2c777db5346c376e56ad346556b6ec1e12f4b3343e92787c893a636e`.
- Finalized-without-publication characterization: `.venv/bin/python -m pytest -q --tb=short --disable-warnings data/registry_model/test/test_blobuploader.py::test_prefetch_finalizes_bytes_without_repository_visibility` — **2 passed** on SQLite and **2 passed** on PostgreSQL.
- Preserved Story 17 focus: `.venv/bin/python -m pytest -q --tb=short --disable-warnings data/model/test/test_gc.py -k story17` — **17 passed, 43 deselected** on SQLite and PostgreSQL.
- Replication and temporary chunk behavior: `.venv/bin/python -m pytest -q --tb=short --disable-warnings workers/test/test_storagereplication.py workers/test/test_chunkcleanupworker.py` — **6 passed**.
- Read-only plan: `PGOPTIONS='-c default_transaction_read_only=on -c enable_seqscan=off' psql ... -c "EXPLAIN ... SELECT 1 FROM public.imagestorage WHERE content_checksum = ... LIMIT 1"` — **Index Only Scan using `imagestorage_content_checksum`**.
- `git diff --check` and `git diff --check upstream/master` — **passed**.

The complete affected regression, mypy, compilation, migration, and pre-commit matrix was intentionally not run because Story 18 was deferred before production implementation. This is an accepted scope decision, not a product-test failure.

## Changed files and scope confirmation

Story 18 changed only:

- `HANDOFF.md` — this deferred decision, assessment, and evidence.
- Planning repository `.PITASKS.md` — only this session's task section.
- Planning repository `TODO.md` — Story 18 status and accepted deferral note.

All Story 17 code and test changes remain byte-for-byte preserved. Stories 9, 11, and 12 remain Deferred. Proxy cache, mirroring, imports, Docker schema 1, API/UI, builds, Clair lookup, operational tooling, schema, migrations, configuration, and pull requests were not changed.

---

# Story 17 Corrective Handoff

## Status

Story 17 corrective work is complete in `/Users/shossain/QuayWorkspace/shaon-feature-PQC`. Both independent-review findings are resolved without a schema change or Story 18 work.

- Branch: `shaon-feature-PQC`
- Pre-Story 17 HEAD: `ad50cf9b983726edb0a1db2aef94399ab1d40230`
- HEAD subject: `NO-ISSUE: fix(gc): bound abandoned upload cleanup retries`
- `upstream/master`: `d81004d24669132d45df8fbd1eafc86c149e38fa`
- Merge base: `d81004d24669132d45df8fbd1eafc86c149e38fa`
- Nothing is staged and there are no untracked Quay files.
- Final uncommitted code/test diff SHA-256, excluding this handoff: `7e8ab50e2c777db5346c376e56ad346556b6ec1e12f4b3343e92787c893a636e`

Modified Quay files:

- `data/model/gc.py`
- `data/model/oci/tag.py`
- `data/model/storage.py`
- `data/model/test/test_gc.py`
- `util/secscan/v4/api.py`
- `util/secscan/v4/test/test_v4_api.py`
- `workers/gc/gcworker.py`
- `workers/gc/test/test_gcworker.py`
- `HANDOFF.md`

Preserve all changes. Do not reset, restore, rebase, amend, discard, overwrite, push, alter remotes, or access `/Users/shossain/QuayWorkspace/11537-pqc-schema`.

## Corrected behavior

### Bounded repository discovery

`data/model/oci/tag.py::find_repository_with_garbage` no longer starts from `Repository` with four correlated `EXISTS` branches.

- Tags, uploads, blob registrations, and manifest registrations each drive an independent primary-key range query.
- Every source range spans at most `GC_CANDIDATE_COUNT` IDs, is ordered and limited, and uses a randomized start for large source tables. Tables whose maximum ID fits in one range start at ID 1, avoiding small-database misses and test flakiness.
- The four repository-ID streams are combined with `UNION ALL`, deduplicated, limited to `GC_CANDIDATE_COUNT`, and randomized before one repository is returned.
- Expired-tag policy, immutable-tag behavior, namespace enabled state, repository deletion state, expired-upload behavior, registration reachability, and read-replica selection are preserved.
- Selection uses constant-size SQL and parameters. It creates no growing bind list, unbounded Python collection, repository N+1 probe, index, or migration.
- A sparse primary-key range can return no candidate even when another range contains garbage. This is intentional bounded sampling; the 30-second worker repeats with new random ranges. Expired content is never treated as live because of a missed range.

### Durable scanner cleanup

Manifest deletion no longer removes the only scanner identities and then performs best-effort API calls.

- Registration IDs determine scanner identity order. A legacy manifest with no registration uses only its canonical SHA-256 digest.
- `QueueItem`/`WorkQueue`, an existing durable facility, stores one cleanup item per identity inside the same database transaction that deletes registrations and the manifest.
- A failed destructive transaction rolls back both registry deletion and queue insertion.
- The GC worker processes a bounded scanner-cleanup batch independently of repository candidate discovery, including when no repository policy or candidate is found.
- Scanner API failures leave queue items available for later passes and restore the retry count, so retries do not expire after a fixed number of failures.
- Before any external delete, the primary database is checked for a canonical `Manifest.digest` or `RepositoryManifestDigest.digest` in any repository. A still-referenced report is preserved; deletion of the final owner will enqueue the identity again.
- Scanner API calls occur outside the destructive manifest transaction.
- Clair report DELETE now treats HTTP 404 as idempotent success. This covers duplicate queue entries and a successful API delete followed by failed queue completion.
- Repeated cleanup converges and does not expose, create, remap, or consult the configured allowlist for any digest identity.

The scanner implementation trace found one active cleanup implementation, `V4SecurityScanner.garbage_collect_manifest_report`; `SecurityScannerModelProxy` delegates to it, `NoopV4SecurityScanner` rejects unsupported use, and the V2 scanner class is an indexer rather than the proxy's cleanup implementation. Clair reports are addressed by manifest digest, so global exact-digest ownership is the preservation boundary.

### Existing Story 17 behavior retained

- Ordinary GC independently retries unreachable manifest and blob registration cleanup with bounded primary-key cursors and chunks.
- Manifest and blob reachability is rechecked in destructive transactions.
- Blob GC locks `ImageStorage`, removes only target-repository registrations, and treats every remaining registration, graph link, and upload as a global canonical-content reference.
- Shared storage survives cleanup of one repository and is collected only after its final global reference disappears.
- Quota rollback/exactly-once behavior, foreign-key safety, cache ordering, repository/namespace purge paths, and disabled-algorithm cleanup remain intact.
- Registry V2 arbitrary blob DELETE remains HTTP 405 `UNSUPPORTED`.

## PostgreSQL query-plan evidence

A read-only `EXPLAIN (COSTS ON, FORMAT JSON)` was executed against `postgresql://quay:quay@localhost:5432/quay` for the final four-source query.

- Root node: `Limit`, followed by `Unique`/`Sort` and `Append` of the four source branches.
- All four source tables appeared below bounded source subqueries: `tag`, `uploadedblob`, `repositoryblobdigest`, and `repositorymanifestdigest`.
- There were no correlated `SubPlan` nodes and no repository-root discovery scan.
- The tiny local tables caused PostgreSQL to choose sequential scans for some bounded source ranges and small lookup tables; `repositoryblobdigest` already used its primary-key index.
- The same read-only plan with `SET LOCAL enable_seqscan = off` used `pk_tag`, `pk_uploadedblob`, `pk_repositoryblobdigest`, and `pk_repositorymanifestdigest`, proving every source range is index-capable without a new index.

The generated-SQL characterization also requires four lower/upper primary-key bounds, four source orders/limits, the four source tables, three `UNION ALL` operators, an outer limit, and a constant parameter count.

## Test-first record and failure classification

Pre-change baseline:

- SQLite Story 17: **10 passed, 43 deselected**.
- PostgreSQL Story 17: **10 passed, 43 deselected**.

Corrective characterization:

- Finder contract tests failed before production changes because no bounded four-source query builder or matching query shape existed.
- Scanner characterization failed because cleanup was called directly after identity deletion and no durable queue item existed.
- Exact Clair idempotence characterization produced **1 failed, 1 passed**: HTTP 404 was incorrectly converted to `APIRequestFailure`; HTTP 503 remained retryable as required.

Failure classifications:

- **Product failures:** repository-root correlated discovery, absent durable scanner retry state, and non-idempotent handling of an already-deleted Clair report. All are fixed.
- **Test defects:** initial finder/scanner fixtures used a manifest helper fixed to `devtable/newrepo`, one invalid policy setter value was replaced with a direct fixture update, one legacy scanner fixture lacked an expired tag, and one SQL-shape assertion assumed SQLite `?` placeholders on PostgreSQL. These were corrected without weakening behavior.
- **Validation-command defects:** the first baseline command ran from the planning directory and could not find `.venv/bin/python`; the first direct scanner API test command exposed the existing need to bootstrap `app` before importing that module. Neither was a product result.
- **Formatting passes:** initial pre-commit passes reformatted Python and reordered imports; final pre-commit passed without changes.
- **Environment failures:** none.

## Final validation evidence

All commands ran from `/Users/shossain/QuayWorkspace/shaon-feature-PQC` with `TEST=true PYTHONPATH=.` unless shown otherwise.

- Focused Story 17 SQLite: `.venv/bin/python -m pytest -q --tb=short --disable-warnings data/model/test/test_gc.py -k story17` — **17 passed, 43 deselected**.
- Focused Story 17 PostgreSQL: `TEST_DATABASE_URI='postgresql://quay:quay@localhost:5432/quay' .venv/bin/python -m pytest -q --tb=short --disable-warnings data/model/test/test_gc.py -k story17` — **17 passed, 43 deselected**.
- GC/repository/namespace model and worker group: `data/model/test/test_gc.py data/model/test/test_repository.py data/model/test/test_user.py workers/gc/test/test_gcworker.py workers/test/test_repositorygcworker.py workers/test/test_namespacegcworker.py` — **114 passed**.
- Storage: `storage/test` — **159 passed**.
- Quota model/workers: the seven `data/model/test/test_*quota*.py` files plus the three quota worker test files — **104 passed**.
- Scanner model/interface: `data/secscan_model/test` — **113 passed**.
- Clair API: `util/secscan/v4/test/test_v4_api.py` — **17 passed**.
- Manifest/blob endpoints: `endpoints/v2/test/test_manifest.py endpoints/v2/test/test_blob.py` — **155 passed**.
- Repository API/model adapters: `endpoints/api/test/test_repository.py endpoints/api/test/test_repository_models_pre_oci.py` — **49 passed**.
- OCI manifest/tag models: `data/model/oci/test/test_oci_manifest.py data/model/oci/test/test_oci_tag.py` — **120 passed, 1 existing skip**.
- Registry interface: `data/registry_model/test/test_interface.py` — **120 passed, 2 expected SQLite skips for PostgreSQL-only races**.
- Registration migration: `data/migrations/test/test_repository_digest_registration.py` — **2 passed**.
- Story 14 focused SQLite/PostgreSQL: OCI tag plus manifest/blob endpoint files with `-k story14` — **9 passed** on each database.
- Story 15 focused SQLite/PostgreSQL: GC/user and repository/namespace worker files with `-k story15` — **4 passed** on each database.
- Story 16 final focused SQLite: uploader/blob endpoint/upload worker/chunk worker/GC files with `-k 'story16 or alternative_monolithic_upload_infers_algorithm_without_hint'` — **16 passed**.
- Story 16 focused PostgreSQL cursor/race/retry selection — **5 passed**.
- Broad SHA-256 deletion, deleted-digest pull, push/pull, and mount selection: `test/registry/registry_tests.py -k 'test_delete_manifest or test_attempt_pull_by_manifest_digest_for_deleted_tag or test_basic_push_pull_by_manifest or blob_mount'` — **273 passed, 1,186 deselected**.
- Five repeated finder runs selecting `test_has_garbage` and the mount-only Story 17 finder test — **2 passed** per run.
- Targeted mypy: `data/model/gc.py data/model/storage.py data/model/oci/tag.py util/secscan/v4/api.py workers/gc/gcworker.py` — **passed**.
- Python compilation over every changed Python file — **passed**.
- `git diff --check` and `git diff --check upstream/master` — **passed**.
- Pre-commit over every changed Python file — **passed without changes** after the recorded formatting passes.

No destructive live GC was run. The shared local instance still contains retained validation repositories and potentially unrelated data; isolated PostgreSQL tests and read-only plan inspection provide the required database evidence without risking them.

## Scope and remaining risks

- No schema, migration, index, configuration, physical-storage inventory, or physical-orphan repair was added.
- Physical storage with no database record remains Story 18.
- Proxy cache, mirroring, imports, Docker schema 1 alternative identities, API/UI, builds, events, Clair report lookup, and operational tooling remain unchanged.
- Stories 9, 11, and 12 remain Deferred. D1-D8 remain unchanged.
- The finder deliberately trades one-pass exhaustive discovery for a fixed amount of indexed work. Sparse ID ranges can delay a candidate until a later 30-second pass; they cannot cause deletion of live content.
- Duplicate scanner tasks are safe because global references are rechecked and Clair report deletion is idempotent. A prolonged scanner outage grows the durable queue with deleted manifest identities; retries remain bounded per worker pass but require scanner recovery to drain.
- MySQL concurrency remains outside the local PostgreSQL validation boundary.

## Completion checklist

- [x] Preserve existing work and create only the corrective session's planning task section.
- [x] Set Planning Story 17 to In progress before corrective production changes.
- [x] Rerun the untouched SQLite and PostgreSQL baseline.
- [x] Add failing finder-scale/query-shape characterization.
- [x] Implement bounded source-driven discovery and obtain a safe read-only PostgreSQL plan.
- [x] Add failing scanner API characterization and implement durable, idempotent, cross-repository-safe retries using existing persistence.
- [x] Add reachability-recheck coverage and run it on PostgreSQL.
- [x] Run every required focused, affected, regression, typing, compilation, whitespace, and pre-commit group.
- [x] Keep Story 18 physical-orphan recovery out of scope.
- [x] Update this handoff and restore Planning Story 17 to Done only after corrective completion.

Story 17 is **Done** under the local definition of done. Story 18 may be planned next, but should start only after this corrective pass is accepted.
