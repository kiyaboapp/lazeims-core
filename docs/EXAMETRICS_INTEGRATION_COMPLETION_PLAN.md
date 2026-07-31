# ExaMetrics Integration — Completion Plan (final pass, all remaining tasks)

Implements everything left in `docs/BACKEND_SIS_INTEGRATION_PLAN.md` that is not
already shipped: **B2–B12, B14, C1(remainder), C6–C12, F3–F8, K3, S1–S2**, plus
the §10.2 enforcement gaps, the §12 contract-test harness, and an approval
console. The five-phase rollout is collapsed: this is one body of work.

Already merged to `main` and **out of scope**: C0, B1, K1, K2, C1(`identity`,
`provision_exam`), C2, C3, C4, C5, B13, F1, F2.

---

## 0. Environment reality check (read before starting)

The sandbox no longer matches the toolchain notes from earlier rounds. Verified
just now:

* **PostgreSQL was not installed at all** and no Python dependency from any repo
  was present. `~/.pyenv/versions/3.11.15` had no `lazeims_common`, no
  SQLAlchemy, no FastAPI. Node was absent too.
* `pg_ctl` still has to be started **inside the same shell call** as any
  DB-backed test (fresh PID namespace + tmpfs `/tmp` per call), and redirecting
  its output to `/dev/null` still breaks it.
* `frontend-sis`'s `npm run build` **cannot** be a verification gate: static
  export of `/sitemap/shuleni.xml` fetches `127.0.0.1:8000` and the build exits
  1. Use `npx tsc --noEmit` plus a scoped `vitest run`.
* `frontend-sis`'s full `npx vitest run` has **pre-existing failures**
  (14 failed / 82 passed / 49 skipped, 5 files) unrelated to this work, and
  takes ~10 minutes. Never gate on it; gate on `tsc` and on the new test file.
* `backend-sis` has **16 alembic heads**, so `alembic upgrade head` cannot run
  there. New backend-sis migrations chain off the integration lineage head
  `d2b3c4d5e6f8` and are verified through `Base.metadata.create_all` (which is
  what the test fixture uses) — not by running alembic.
* `lazeims-core`'s alembic head is `f1a2b3c4d5e6` and **is** runnable
  (`tests/test_migrations.py` does `alembic upgrade head` on a pristine schema).

Baselines re-measured on the current branches (all six repos on
`feat/exametrics-integration-complete`):

| Repo | Command | Baseline |
|---|---|---|
| `lazeims-common` | `pytest -q` (py3.11) | **124 passed** |
| `lazeims-core` | `pytest -q` (py3.11, needs pg) | **136 passed** |
| `lazeims-station` | `pytest -q` (py3.11) | **34 passed** |
| `backend-sis` | `pytest tests/exametrics -q -p no:warnings` (py3.12, needs pg) | **26 passed** |
| `lazaims` | `npx tsc --noEmit`; `npm run build` | both clean |
| `frontend-sis` | `npx tsc --noEmit` | clean |

---

## 1. Decisions taken here (the implementer must not re-decide these)

### 1.1 Answers assumed for §14

The `backend-sis` owner is unavailable. Each answer below is implemented; each is
one config value or one localised change away from being corrected. The four
already assumed in the Phase-1.5 amendment are repeated unchanged so nothing
drifts.

| §14 | Question | Answer implemented | Rationale | Where to change it |
|---|---|---|---|---|
| 14.1 | Is `exam_ref` stable? Is `external_ref` unique per tenant? | `exam_ref` is stable for the life of the exam; `external_ref` **is** the Central exam UUID, so per-tenant uniqueness is free and global uniqueness comes for nothing. *(unchanged from the amendment)* | Provisioning is already idempotent on it and no reconciliation needs a human. | n/a — inherent to using a UUID |
| 14.2a | Billing unit — per student, per subject registration, or per run? | **`per_student`**, at `INTEGRATION_PRICE_PER_STUDENT` minor units (default 250 TZS). The quote *also* reports `centres` and `subject_registrations` counts. | §6.1's only concrete worked example is 18 432 students × 250 = 4 608 000 TZS; matching the document's own number is the least surprising choice, and reporting all three counts means switching unit later is a config change, not a contract change. | `INTEGRATION_BILLING_UNIT` + `INTEGRATION_PRICE_PER_UNIT` env vars read by the quote engine |
| 14.2b | Does a re-run after a reopen cost again? | **Yes.** Approval binds to `(exam_ref, closeout_revision, configuration_hash)`. A reopen bumps `closeout_revision`, which invalidates the approval and forces a new request + quote. A re-run at the *same* revision and hash is free and returns the same `run_id`. | This is precisely the property §5.2 says stops "pay once, reprocess anything forever", and §7.1 says a replay must never double-charge. | `entitlement_matches()` in `processing_request_service` |
| 14.3 | Who approves? | An ExaMetrics **SUPER_ADMIN** in the ExaMetrics console. Keys are issued automatically to the partner *server* holding the zone enrolment secret; paid scopes and paid runs both queue for that SUPER_ADMIN. LAZEIMS cannot submit payment proof. *(unchanged from the amendment)* | Keeps the spend decision with the party being paid. | `require_super_admin_or_membership` on the approval routes |
| 14.4a | Quote lifetime | **30 days** (`INTEGRATION_QUOTE_TTL_DAYS`, default 30). On expiry the request moves to `EXPIRED` and must be re-requested. | Long enough to clear a school-district payment cycle, short enough that a price change is not honoured forever. | `INTEGRATION_QUOTE_TTL_DAYS` |
| 14.4b | Behaviour when counts change after approval | The approved amount is **frozen**. At `process` time the billable count is recomputed: `<=` approved ⇒ run; `>` approved ⇒ **409 `QUOTE_COUNTS_EXCEEDED`** carrying both counts, requiring re-approval. A changed `configuration_hash` or `closeout_revision` ⇒ **409 `CONFIGURATION_HASH_MISMATCH`**. | Never under-charge, and never hand the operator a bill larger than the one they approved. Shrinking is harmless, so it is allowed. | `assert_runnable()` in `processing_request_service` |
| 14.5 | Are `results.read` and `results.download` separately purchasable? | **One bundle** for the processing approval: approving a run grants both. They stay *separable at key level* because `decide_key_approval` already supports approving a subset, so support can still withhold one. | A tenant that has paid for a run but cannot read its own results has bought nothing. | `GRANT_ON_APPROVAL` list in `processing_request_service` |
| 14.6 | Webhooks now, or polling only? | **Both.** Webhooks (B10/C9) are implemented as an accelerator; polling remains the authoritative fallback and every state is reachable without a webhook ever arriving. Webhooks are only registered when `CENTRAL_PUBLIC_BASE_URL` is set. | §7.3 requires polling to be kept regardless (NAT), so webhooks add no single point of failure, and this is the last pass — deferring them leaves C9 unbuilt. | unset `CENTRAL_PUBLIC_BASE_URL` to disable |
| 14.7 | Max payload; is chunked upload required day one? | **25 MB** — matching backend-sis's real `_MAX_UPLOAD` — published via `GET /integration/me`. Chunked upload is **always available but never required**: Central switches to it automatically when the canonical payload exceeds `max_payload_mb`, and on any `413 PAYLOAD_TOO_LARGE`. Chunk size 5 000 rows, matching the already-published `max_collection_chunk_size`. | Small zones keep a single round trip; a real zone gets resumability without a flag day. | `_MAX_UPLOAD` / `max_collection_chunk_size` in `integration.py` |
| 14.8 | Retention; can LAZEIMS request deletion? | Collections and results are retained until the ExaMetrics exam is deleted. **No new deletion endpoint.** The existing `POST /integration/exams/{ref}/collection/reset` is documented as the discard affordance. | Inventing a retention policy on the owner's behalf risks deleting data he is contractually obliged to keep; `reset` already covers the real need (replace a bad collection generation). | documented in the amendment, not in code |
| 14.9 | Sandbox tenant for CI | **None exists.** Contract tests instead run from shared fixtures in `lazeims-common`: Central and station assert against a fake transport, and backend-sis asserts the *same* fixtures against its real routes in-process. | Removes the CI dependency on a live third-party tenant entirely, which is what §12 already prescribes. | `lazeims_common/fixtures/exametrics.py` |
| 14.10 | Is extraction permanently free and unmetered? | **Yes**, and it needs no key at all. *(unchanged from the amendment)* | Already true in code; F5 drives the UI affordance from the `registrations.extract` capability so the answer is visible rather than assumed. | `capability_map()` |

### 1.2 Design decisions

**D1 — The per-exam processing gate stands *beside* the key-approval queue, not inside it.**
`GET /integration/keys/pending` + `POST /integration/keys/{id}/approval` answer
"may this partner ever buy processing?" — per key, permanent, no expiry. The new
gate answers "is *this run*, at this revision and this configuration hash, paid
for?" — per exam, expiring, invalidated by a reopen. Folding the second into the
first would make one approval reusable forever, which is exactly what §5.2
forbids. So: new `exam_processing_requests` table and state machine, with
`require_api_scope(SCOPE_RESULTS_PROCESS)` staying as the **outer** gate (does
the key have the capability at all) and request approval as the **inner** gate
(is this run authorised). Both surfaces render on one console page (D8).

**D2 — B5 stays a per-route dependency, not middleware.**
`require_api_scope` already exists and already returns `SCOPE_NOT_GRANTED` /
`SCOPE_AWAITING_APPROVAL` / `EXAM_SCOPE_MISMATCH` with the right shapes. The real
gap is that nothing *proves* every exam-scoped route declares it. A middleware
would need a second path→scope table, i.e. a second source of truth. Instead: keep
the dependency and add a route-table test that walks the `/integration` router and
fails if any exam-scoped route lacks a scope dependency.

**D3 — Fix the `backend-sis` DB test fixture; do not work around it.**
Root cause found and confirmed by patching and re-running — two small model bugs,
neither related to this work:
1. `app/db/models/exametrics/user_exam.py`: the JSON `server_default` is wrapped
   in `text()`, so SQLAlchemy parses `:false` / `:true` as **bind parameters** and
   emits `'{"edit"NULL,...}'::jsonb` → `invalid input syntax for type json`.
   Fix: escape the single colons as `\:` (`::jsonb` is already handled).
2. `app/db/models/shuleyetu/behavioral_assessment.py`: `centre_number` and
   `enrollment_id` carry `index=True` **and** explicit `Index()` entries using the
   auto-generated names, so `create_all` emits each index twice →
   `DuplicateTableError`. Fix: drop the two redundant `Index()` entries.

With both applied, the session `engine` fixture builds the whole schema and the
suite passes. This scope adds a quote engine, a state machine, upload sessions,
webhook retry/DLQ and rate limits — all of which are only meaningfully testable
against a database. Two one-line fixes buying real integration tests is a far
better trade than another round of DB-free helper shims.

**D4 — Encrypt `api_key` at rest via a model property, with `MultiFernet`.**
Rename the column to `api_key_encrypted` and expose a plain `api_key` Python
property that encrypts on set and decrypts on get. Eight call sites read
`link.api_key`; a property keeps the diff tiny and makes it impossible to miss
one. Use `cryptography`'s `MultiFernet` so encryption-key rotation is a config
change (new key first, old keys still decrypt) rather than a migration.
`PROCESSING_KEY_ENCRYPTION_KEYS` is a comma-separated list; when empty, a key is
derived from `session_secret_key` so no existing deployment breaks on upgrade.
C8 is done **first** among the Central items: keys are minted automatically per
exam since the last round, so the number of plaintext secrets at rest grows with
exam count.

**D5 — Server-side idempotency lives in backend-sis, not Central.**
§7.1 makes the *server* authoritative. Add one `integration_idempotency` table in
backend-sis keyed on `(key_prefix, idempotency_key)` storing `payload_hash` +
`response_json`; replay returns the stored body with `"replayed": true`, and a
same-key/different-payload replay is a 409 `IDEMPOTENCY_KEY_REUSED`. Central just
sends `Idempotency-Key` per §7.1's table. Central's existing
`app/services/idempotency.py` is not reused directly — it is FK'd to
`mark_batch_receipts` and shaped for marks — but the check-replay / record-receipt
pattern is copied deliberately.

**D6 — `lazeims-common` becomes a *test-only* dependency of `backend-sis`.**
`app/db/schemas/exametrics/integration.py` says outright that backend-sis has no
dependency on `lazeims_common` and mirrors the shapes by hand. Keep that for
runtime — making the vendor's API depend on a partner's library inverts the
relationship. But §12 wants one JSON asserted by both sides, so add
`lazeims-common` to backend-sis's dev/test install and import it **only** from
`tests/`. The fixtures themselves are plain dicts, not models, so the assertion is
about bytes rather than about two pydantic versions agreeing.

**D7 — `CapabilitiesResponse` must stop rejecting what backend-sis actually sends.**
Confirmed defect: backend-sis's `identity_payload()` emits both `tenant_exam` and
the deprecated `tenant` mirror, while `CapabilitiesResponse` is `extra="forbid"`
with `tenant` only as a *validation alias*. Validating the real payload raises
`extra_forbidden` on `tenant`. The §12 contract test would fail on its first line.
Fix: `extra="ignore"` on `CapabilitiesResponse` only (every other model keeps
`forbid`), plus a test that validates the genuine `identity_payload()` output.

**D8 — The approval console belongs in `frontend-sis`.**
`frontend-sis` is ExaMetrics' own frontend and already has an admin area with the
exact pattern needed (`app/admin/requests/page.tsx`: react-query + `Table` +
`Dialog` + `sonner`). Without a console, the only way to approve a paid run is
curl, so nothing would ever be approved and processing would never run — that is a
functional blocker, not polish. `GET /keys/pending` alone does **not** suffice: it
shows key-scope requests, has no quote, and has no per-revision run requests.
Scope is kept deliberately small — one page, two tables (pending keys, pending
runs), approve/reject with an optional note, quote shown verbatim from the server.

**D9 — Central maps upstream error codes instead of flattening them.**
Today `_sis_http` turns every upstream failure into `502 EXAMETRICS_ERROR`, which
destroys exactly the information §8 tells LAZEIMS to act on. C11 replaces it with
a code-preserving mapper that keeps the upstream status for the codes in §8's
table (401/403/402/409/413/422/429/503) and only falls back to 502 for genuinely
unknown failures.

**D10 — `ProcessingRequestIn` / `ProcessingQuoteOut` are re-shaped in place.**
Both exist in `lazeims_common.schemas.exametrics` as placeholders with
`force`/`dry_run`/`cost_units` fields that match nothing in §6.1, and are
referenced only by `lazeims-common`'s own tests — never on the wire. Reuse the
names for §6.1's real shapes rather than adding near-duplicates. The contract
version stays `exametrics-integration/v2` because no deployed client sends these.

### 1.3 Merge order

Each repo is an independent PR. Merge in this order; each is safe to merge alone
because every consumer degrades to its current behaviour when the producer is
absent.

1. **`lazeims-common`** — contract of record. Nothing else compiles against the
   new shapes until this lands.
2. **`backend-sis`** — the new server surface. Additive: every existing route
   keeps working (B14), so Central can merge before or after.
3. **`lazeims-core`** — the client, phase guard, encryption, webhook receiver.
   Its tests use a fake transport, so it does **not** need backend-sis deployed.
4. **`lazaims`** and **`frontend-sis`** — independent of each other; either order.
5. **`lazeims-station`** — needs `SyncResponse.exam_phase` from (1) and Central's
   emission of it from (3).

### 1.4 Deployment actions for the PR descriptions (§9 residual risk — code cannot fix these)

Surface these verbatim in the `lazeims-core` PR body; do not attempt them.

* `SESSION_SECRET_KEY` and `STATION_PACKAGE_INTEGRITY_KEY` leaked through git
  history and **have not been rotated**. The integrity key signs station
  manifests; the session key signs cookies. Both must be treated as compromised
  until rotated.
* `BACKEND_SIS_BASE_URL` is still unset in the deployed `.env`, so
  `processing_enabled` is `False` and none of this integration can reach
  ExaMetrics in production.
* New settings this work introduces:
  `PROCESSING_KEY_ENCRYPTION_KEYS` (Central), `CENTRAL_PUBLIC_BASE_URL`
  (Central, optional — omit to disable webhooks),
  `INTEGRATION_BILLING_UNIT` / `INTEGRATION_PRICE_PER_UNIT` /
  `INTEGRATION_QUOTE_TTL_DAYS` / `INTEGRATION_WEBHOOK_MAX_ATTEMPTS`
  (backend-sis).

---

## Implementation Plan

### Bootstrap

- [ ] 1. Rebuild the toolchain and re-record the baselines, because nothing from the
      earlier rounds survives in this sandbox. Install PostgreSQL 15 + contrib,
      `initdb -A trust`, set the `postgres` password to the one
      `lazeims-core/tests/conftest.py` hard-codes, create `lazeims_test`, add
      `pg_trgm` to `template1`; `pip install -e ./lazeims-common`,
      `pip install -e "./lazeims-core[dev]"`, `pip install -e ./lazeims-station`
      (deps included this time — `argon2-cffi`, `itsdangerous`, `python-multipart`,
      `fastapi`, `httpx`) into `~/.pyenv/versions/3.11.15`; install
      `/projects/sandbox/.tooling/backend-sis-requirements-utf8.txt` plus
      `matplotlib` and `python-docx` into `~/.pyenv/versions/3.12.13` and
      **uninstall the `docx` shim** (the Python-2 package shadows `python-docx`);
      `MISE_NODE_VERIFY=0 mise install node@22 && mise use -g node@22`;
      `npm ci` in `frontend-sis`.
      Files: none (environment only).
      Verify: in one shell call each — `cd lazeims-common && $PY311 -m pytest -q`
      → 124 passed; `chmod 1777 /tmp; su postgres -c "/usr/bin/pg_ctl -D /var/lib/pgsql/data -w start -l /var/lib/pgsql/pg.log" 2>&1|tail -1; cd lazeims-core && $PY311 -m pytest -q`
      → 136 passed; `cd lazeims-station && $PY311 -m pytest -q` → 34 passed;
      `… pg_ctl start …; cd backend-sis && $PY312 -m pytest tests/exametrics -q -p no:warnings`
      → 26 passed; `cd lazaims && npx tsc --noEmit && npm run build` → clean;
      `cd frontend-sis && npx tsc --noEmit` → clean.

### `lazeims-common` — the contract of record (merge first)

- [ ] 2. Add repo hygiene that is currently missing: a `.gitignore` covering
      `__pycache__/`, `*.pyc`, `*.egg-info/`, `.pytest_cache/`, and untrack the
      committed `lazeims_common.egg-info/` tree. Right now an editable install
      dirties the working tree, which will fight every later commit.
      Files: `lazeims-common/.gitignore`, `git rm -r --cached lazeims_common.egg-info`.
      Verify: `cd lazeims-common && $PY311 -m pip install -e . && git status --porcelain`
      → empty output.

- [ ] 3. Fix `CapabilitiesResponse` so it can validate what backend-sis really
      emits: switch that one model to `extra="ignore"` (leave every other model on
      `forbid`), keeping `tenant` as a validation alias and `tenant_exam` as the
      only serialised key. Add tests asserting a payload carrying **both** keys
      validates and that `model_dump(by_alias=True)` still emits `tenant_exam`
      only. See D7.
      Files: `lazeims_common/schemas/exametrics.py`,
      `tests/test_exametrics_schemas.py`.
      Verify: `cd lazeims-common && $PY311 -m pytest tests/test_exametrics_schemas.py -q`
      → all pass, including the new both-keys case that fails before the change.

- [ ] 4. Add K3: shared canonicalisation for the collection digest in a new
      `lazeims_common/exametrics_digest.py` — `canonical_collection(payload)` that
      sorts `schools` by `centre_number`, `subjects` by `subject_code`, `students`
      and `marks` by `(centre_number, student_id[, subject_code])` and drops
      `None` values; `collection_digest(payload) -> "sha256:…"` built on
      `hashing.canonical_bytes` / `sha256_prefixed`; and `chunk_payload(payload,
      max_rows)` / `chunk_manifest(chunks)` for §7.2. Export from
      `lazeims_common/__init__.py`. Both sides must compute the identical digest,
      so ordering and null-dropping are part of the contract, not an
      implementation detail.
      Files: `lazeims_common/exametrics_digest.py`, `lazeims_common/__init__.py`,
      `tests/test_exametrics_digest.py`.
      Verify: `cd lazeims-common && $PY311 -m pytest tests/test_exametrics_digest.py -q`
      → passes, including a test that shuffling row order and adding explicit
      `None` fields leaves the digest unchanged, and that the chunks of a payload
      re-assemble to the same digest as the whole.

- [ ] 5. Extend K2 with the remaining §4–§8 shapes in
      `lazeims_common/schemas/exametrics.py`: give `SubjectSpec` the max-marks and
      level flags §4.2 requires (`theory_max`, `theory_2_max`, `practical_max`,
      `subject_short`, `is_primary`/`is_olevel`/`is_alevel`); add
      `ProvisionWarning` and make `ExamProvisionResponse.warnings` a list of it;
      add `EntitlementState` + `EntitlementsResponse` (§5.2), `QuoteOut`,
      re-shaped `ProcessingRequestIn` and `ProcessingQuoteOut` plus
      `ProcessingRequestOut` (§6.1, see D10), `CollectionSessionStart` /
      `CollectionChunkAck` / `CollectionSessionComplete` (§7.2), `WebhookEnvelope`
      (§7.3), and `ErrorEnvelope` + `RowValidationDetail`
      (`{student_id, subject_code, paper, code, message}`, §8). Update the three
      existing placeholder tests.
      Files: `lazeims_common/schemas/exametrics.py`,
      `lazeims_common/schemas/__init__.py`, `tests/test_exametrics_schemas.py`.
      Verify: `cd lazeims-common && $PY311 -m pytest -q` → previous 124 plus the
      new cases, no failures.

- [ ] 6. Add the optional `exam_phase: str | None` field to `SyncResponse` for S1.
      Adding a field is a compatible change under §7.4, so `station-sync/v1` is
      unchanged; the field is optional so a station on an older Central still
      parses the response.
      Files: `lazeims_common/schemas/station_sync.py`,
      `tests/test_schemas.py`.
      Verify: `cd lazeims-common && $PY311 -m pytest tests/test_schemas.py -q` →
      passes, including a case that omitting `exam_phase` still validates.

- [ ] 7. Add the §12 shared contract fixtures as plain dicts in a new
      `lazeims_common/fixtures/exametrics.py` — `IDENTITY_RESPONSE`,
      `EXAM_PROVISION_REQUEST/RESPONSE`, `COLLECTION_PAYLOAD`,
      `ENTITLEMENTS_APPROVED/LOCKED`, `PROCESSING_REQUEST_IN`,
      `PROCESSING_REQUEST_PENDING/APPROVED`, `WEBHOOK_COMPLETED`,
      `VALIDATION_FAILED_422` — and a `frozen_digest()` constant so both repos
      assert the same digest value. Plain dicts, not models: the point is that
      three repos on three pydantic versions agree on **bytes**. Depends on items
      4 and 5. This module is what `backend-sis` imports under D6.
      Files: `lazeims_common/fixtures/__init__.py`,
      `lazeims_common/fixtures/exametrics.py`,
      `tests/test_exametrics_fixtures.py`.
      Verify: `cd lazeims-common && $PY311 -m pytest tests/test_exametrics_fixtures.py -q`
      → passes; every fixture validates against its schema from item 5 and
      `collection_digest(COLLECTION_PAYLOAD) == frozen_digest()`.

### `backend-sis` — the new server surface

- [ ] 8. Fix the two model bugs that break the session `engine` fixture, per D3:
      escape the single colons in `user_exam.permissions`'s `text()` server
      default, and delete the two redundant explicit `Index()` entries on
      `BehavioralAssessment` that collide with `index=True` on the same columns.
      Add `lazeims-common` to the dev/test install (D6). Everything after this
      item depends on it.
      Files: `app/db/models/exametrics/user_exam.py`,
      `app/db/models/shuleyetu/behavioral_assessment.py`,
      `/projects/sandbox/.tooling/backend-sis-requirements-utf8.txt` (add
      `-e ../lazeims-common` comment or install note in `AGENTS.md`).
      Verify: in one shell call, start pg then
      `cd backend-sis && $PY312 -m pytest tests/exametrics -q -p no:warnings`
      with a temporary test that merely requests the `engine` fixture — the whole
      schema builds and the suite passes (26 + 1). Before the fix the same test
      fails with `invalid input syntax for type json`.

- [ ] 9. Add the persistence for the approval gate: an `ExamProcessingRequest`
      model (`exam_processing_requests`: `request_id` PK `prq_…`, `exam_id` FK,
      `external_ref`, `closeout_revision`, `configuration_hash`, `state`,
      `quote_json`, `billable_count`, `counts_json`, `requested_by` JSONB,
      `run_id`, `decided_at`/`decided_by`/`decision_reason`, `expires_at`,
      timestamps, unique on `(exam_id, closeout_revision, configuration_hash)`)
      and an `IntegrationIdempotency` model (D5:
      `(key_prefix, idempotency_key)` unique, `payload_hash`, `response_json`,
      `created_at`). One alembic revision for both, chaining off `d2b3c4d5e6f8`.
      Files: `app/db/models/exametrics/processing_request.py`,
      `app/db/models/exametrics/integration_idempotency.py`,
      `app/db/models/exametrics/__init__.py`,
      `alembic/versions/d3c4d5e6f7a9_exam_processing_requests_and_idempotency.py`.
      Verify: start pg then `$PY312 -m pytest tests/exametrics -q -p no:warnings`
      — the `engine` fixture creates both tables (proving the models are valid
      DDL); add a DB-free test asserting the unique constraint names and the
      `state` enum values.

- [ ] 10. Add the quote engine as a pure module plus one count query:
      `app/services/exametrics/quote_service.py` with `billable_counts(db,
      exam_id)` returning `{students, centres, subject_registrations}` computed
      **from the pushed collection** (`Student` / `ExamSchool` /
      `StudentSubject`), and a side-effect-free `build_quote(counts, *, unit,
      unit_amount, currency, ttl_days, now)`. §6.2 is a hard security property:
      the client-supplied `counts` in the request body are stored for display and
      comparison only and never feed the price.
      Files: `app/services/exametrics/quote_service.py`,
      `tests/exametrics/test_quote_engine.py`.
      Verify: start pg then
      `$PY312 -m pytest tests/exametrics/test_quote_engine.py -q -p no:warnings`
      → passes, including a DB-backed test that inflating the client's `counts`
      by 10× leaves `quote.amount` unchanged, and a unit test that
      `unit`/`unit_amount` come from config.

- [ ] 11. Add B2 `PUT /integration/exams` and B3 `GET /integration/exams/{ref}`.
      B2 upserts on `external_ref` (idempotent: same body ⇒ no-op with
      `state:"UNCHANGED"`, changed body ⇒ update while no run has started, else
      `409 EXAM_STATE_CONFLICT`), accepts §4.2 verbatim including `subjects` with
      max marks, resolves the board through the existing `resolve_board_id`, and
      returns `exam_ref` + `warnings[]` (e.g. `SUBJECT_MAX_MARKS_DIFFERS`). B3
      returns state, counts, `configuration_hash` and accepted subjects. Both use
      `require_api_scope`; B2 also honours `Idempotency-Key` via item 12's helper
      once that lands — until then it is idempotent on `external_ref` alone.
      Files: `app/api/exametrics/v1/integration.py`,
      `app/db/schemas/exametrics/integration.py`,
      `app/services/exametrics/integration_service.py`,
      `tests/exametrics/test_integration_exam_upsert.py`.
      Verify: start pg then
      `$PY312 -m pytest tests/exametrics/test_integration_exam_upsert.py -q -p no:warnings`
      → passes: two identical PUTs yield one exam and `created:false` the second
      time; a changed `theory_max` returns the warning; a PUT after a run started
      returns 409.

- [ ] 12. Add the idempotency helper and wire it into every mutating integration
      route per §7.1: `app/services/exametrics/idempotency_service.py` with
      `replay_or_reserve(db, key_prefix, idempotency_key, payload)` and
      `record(…)`; a replay returns the stored body with `"replayed": true`, and a
      same-key/different-payload replay is `409 IDEMPOTENCY_KEY_REUSED`. Apply to
      `PUT /exams`, `POST .../collection`, `POST .../processing-requests`,
      `POST .../process`. Depends on items 9 and 11.
      Files: `app/services/exametrics/idempotency_service.py`,
      `app/api/exametrics/v1/integration.py`,
      `tests/exametrics/test_integration_idempotency.py`.
      Verify: start pg then
      `$PY312 -m pytest tests/exametrics/test_integration_idempotency.py -q -p no:warnings`
      → passes: replaying a collection push returns the original report with
      `replayed: true` and creates no second row; a different payload under the
      same key is 409.

- [ ] 13. Add B4 `GET /integration/exams/{ref}/entitlements` returning §5.2's
      shape: `closeout_revision`, `processing` (`state`, `approved_at`,
      `approved_by`, `expires_at`, `valid_for_configuration_hash`) and `results`
      (`state` + `reason`, e.g. `PROCESSING_NOT_COMPLETE`, `SCOPE_NOT_GRANTED`).
      Reads the latest `ExamProcessingRequest` for the exam. Depends on item 9.
      Files: `app/api/exametrics/v1/integration.py`,
      `app/db/schemas/exametrics/integration.py`,
      `tests/exametrics/test_integration_entitlements.py`.
      Verify: start pg then
      `$PY312 -m pytest tests/exametrics/test_integration_entitlements.py -q -p no:warnings`
      → passes: no request ⇒ `processing.state == "NONE"`; approved request ⇒
      `APPROVED` with the hash echoed; a key without `results:read` ⇒
      `results.state == "LOCKED"` with reason `SCOPE_NOT_GRANTED`.

- [ ] 14. Add B6 `POST /integration/exams/{ref}/processing-requests`: 202 with
      `request_id`, `state: PENDING_APPROVAL`, the server-computed `quote` from
      item 10, `approval` (`method: "EXAMETRICS_CONSOLE"`, `instructions_url`) and
      `next_poll_after`. Idempotent on `(external_ref, closeout_revision,
      configuration_hash)` — a repeat returns the existing request rather than a
      second quote. Requires only the free `processing.request` capability, per
      §5.1. Depends on items 9, 10, 12.
      Files: `app/api/exametrics/v1/integration.py`,
      `app/services/exametrics/processing_request_service.py`,
      `app/db/schemas/exametrics/integration.py`,
      `tests/exametrics/test_processing_requests.py`.
      Verify: start pg then
      `$PY312 -m pytest tests/exametrics/test_processing_requests.py -q -p no:warnings`
      → passes: a key with only free scopes can create a request; two calls with
      the same triple return one `request_id`; the amount matches
      `build_quote(billable_counts(...))` and ignores the body's `counts`.

- [ ] 15. Add B7: the state machine plus the approval surface. In
      `processing_request_service` implement the transitions
      `PENDING_APPROVAL → APPROVED | REJECTED | EXPIRED`,
      `APPROVED → RUNNING → COMPLETED | FAILED`, with `EXPIRED` applied lazily on
      read once `expires_at` has passed, and pure predicates
      `entitlement_matches(req, revision, hash)` and `assert_runnable(req,
      recomputed_count)` implementing 14.2b/14.4b (`CONFIGURATION_HASH_MISMATCH`,
      `QUOTE_COUNTS_EXCEEDED`). Add
      `GET /integration/processing-requests/pending` and
      `POST /integration/processing-requests/{id}/decision`, both behind
      `require_super_admin_or_membership` (14.3), where approval also grants the
      `results:read` + `results:download` bundle on the exam's active key (14.5).
      Depends on items 9, 10, 14.
      Files: `app/services/exametrics/processing_request_service.py`,
      `app/api/exametrics/v1/integration.py`,
      `tests/exametrics/test_processing_state_machine.py`.
      Verify: start pg then
      `$PY312 -m pytest tests/exametrics/test_processing_state_machine.py -q -p no:warnings`
      → passes: every illegal transition raises; a request past `expires_at` reads
      as `EXPIRED`; approval flips the key's `approved_scopes` to include both
      results scopes; the transition table is asserted exhaustively by a DB-free
      parametrised test.

- [ ] 16. Add B8: make `POST /integration/exams/{ref}/process` approval-checked
      and idempotent. No matching APPROVED request ⇒ **402 `APPROVAL_REQUIRED`**
      carrying `request_id` and the quote verbatim so LAZEIMS can show it (§8
      allows 402 or 403; 402 is chosen because the blocker is payment). Matching
      approval ⇒ transition to `RUNNING`, store the Celery task id as a stable
      `run_id`, and return the same `run_id` on retry with `"replayed": true`,
      never enqueuing twice. Keep the existing `has_students_registered` /
      `has_marks_uploaded` 409s ahead of the approval check so an unrunnable exam
      is not charged. Depends on items 12, 15.
      Files: `app/api/exametrics/v1/integration.py`,
      `app/services/exametrics/processing_request_service.py`,
      `tests/exametrics/test_process_approval_gate.py`.
      Verify: start pg then
      `$PY312 -m pytest tests/exametrics/test_process_approval_gate.py -q -p no:warnings`
      → passes: unapproved ⇒ 402 with the quote; approved ⇒ 200 with a `run_id`;
      a second POST returns the identical `run_id` with `replayed: true` and the
      task is dispatched once; a bumped `closeout_revision` ⇒ 409
      `CONFIGURATION_HASH_MISMATCH`.

- [ ] 17. Add B9: collection sessions for chunked upload —
      `POST /integration/exams/{ref}/collection-sessions`,
      `PUT /integration/collection-sessions/{id}/chunks/{n}`,
      `POST /integration/collection-sessions/{id}/complete`. Persist a
      `CollectionUploadSession` + `CollectionUploadChunk` (alembic revision
      chaining off item 9's), have `complete` verify the client-supplied SHA-256
      over the canonical payload using the **same** algorithm as
      `lazeims_common.exametrics_digest` (re-implemented in
      `app/services/exametrics/collection_digest.py`, asserted equal to the shared
      fixture in item 21 — D6 keeps runtime free of the dependency), then apply
      the reassembled payload through the existing `push_collection`. Digest
      mismatch ⇒ `409 CONFIGURATION_HASH_MISMATCH` and nothing applied.
      Files: `app/db/models/exametrics/collection_session.py`,
      `alembic/versions/e4d5e6f7a9b1_collection_upload_sessions.py`,
      `app/services/exametrics/collection_digest.py`,
      `app/services/exametrics/integration_service.py`,
      `app/api/exametrics/v1/integration.py`,
      `tests/exametrics/test_collection_sessions.py`.
      Verify: start pg then
      `$PY312 -m pytest tests/exametrics/test_collection_sessions.py -q -p no:warnings`
      → passes: a three-chunk upload produces exactly the same DB state as one
      whole-payload push; a wrong digest leaves the DB untouched; re-`PUT`ting a
      chunk is idempotent; `complete` twice is a replay.

- [ ] 18. Add B10: webhooks with per-exam secret, HMAC signing, retry and DLQ.
      Generate a `webhook_secret` at provision time, return it once alongside
      `api_key` in `ProvisionOut`, accept a `callback_url` on provisioning, and
      emit `processing.approval_changed`, `processing.completed`,
      `processing.failed`, `results.ready` from the state machine. Sign
      `hmac-sha256:` over the canonical JSON of the envelope minus `signature`.
      Deliveries go through a `WebhookDelivery` table with exponential backoff up
      to `INTEGRATION_WEBHOOK_MAX_ATTEMPTS` (default 6) and a terminal `DLQ`
      state; a delivery failure must never fail the state transition that caused
      it. Depends on items 9, 15.
      Files: `app/db/models/exametrics/webhook_delivery.py`,
      `alembic/versions/f5e6a7b8c9d2_webhook_deliveries.py`,
      `app/services/exametrics/webhook_service.py`,
      `app/services/exametrics/processing_request_service.py`,
      `app/api/exametrics/v1/integration.py`,
      `tests/exametrics/test_webhooks.py`.
      Verify: start pg then
      `$PY312 -m pytest tests/exametrics/test_webhooks.py -q -p no:warnings`
      → passes with an injected transport (no network): the signature verifies
      against an independently computed HMAC; a transport that always raises lands
      the delivery in `DLQ` after the configured attempts while the request state
      still reaches `APPROVED`; no envelope contains `api_key` or
      `webhook_secret`.

- [ ] 19. Add B11: make every 422 row-addressable. Reshape `PushReport.errors`
      entries to `{student_id, subject_code, paper, code, message}` for mark and
      student rows (keeping `entity` and the offending `row` for schools and
      subjects), turn the existing free-text `error` strings into stable codes,
      and return them as `details[]` inside the §8 envelope from both
      `push_collection` and `collection-sessions/complete`.
      Files: `app/services/exametrics/integration_service.py`,
      `app/api/exametrics/v1/integration.py`,
      `tests/exametrics/test_row_addressable_validation.py`.
      Verify: start pg then
      `$PY312 -m pytest tests/exametrics/test_row_addressable_validation.py -q -p no:warnings`
      → passes: a mark with `sat_theory: true` and no theory mark yields exactly
      one detail naming that `student_id`, `subject_code` and `THEORY1`; a payload
      with one bad row commits nothing and reports one detail.

- [ ] 20. Add B12: rate limits with `Retry-After`, reusing the existing
      `app/middleware/rate_limiter.py` (which already emits `X-RateLimit-*` and
      `Retry-After`) but keyed on `key_prefix` rather than client IP — a partner
      is a key, not an address — and with an in-process fallback counter when
      Redis is unavailable so tests and single-node deployments still enforce.
      Return the §8 code `RATE_LIMITED` (429). Publish the effective limit through
      `GET /integration/me`'s `limits.rate_limit_rpm`, which already exists.
      Files: `app/middleware/rate_limiter.py`,
      `app/api/exametrics/deps.py`,
      `app/api/exametrics/v1/integration.py`,
      `tests/exametrics/test_integration_rate_limit.py`.
      Verify: start pg then
      `$PY312 -m pytest tests/exametrics/test_integration_rate_limit.py -q -p no:warnings`
      → passes with the limit monkeypatched to 3: the fourth call in a window is
      429 with a positive integer `Retry-After`, and a different `key_prefix` in
      the same window is unaffected.

- [ ] 21. Add the B5 route-table guard, the B14 v1-compatibility guard, and the
      §12 contract test. The route-table test walks the `/integration` router and
      asserts every route with an `exam_id`/`exam_ref` path parameter declares a
      scope dependency (D2). The v1 test asserts the sixteen paths that existed
      before this work are all still mounted with unchanged methods. The contract
      test imports `lazeims_common.fixtures.exametrics` (D6) and asserts
      backend-sis's real handlers accept those exact request fixtures and that
      `identity_payload()` matches `IDENTITY_RESPONSE` key-for-key, and that
      `collection_digest` agrees with `frozen_digest()`. Depends on items 7 and
      8–20.
      Files: `tests/exametrics/test_integration_route_contract.py`,
      `tests/exametrics/test_integration_shared_fixtures.py`.
      Verify: start pg then
      `$PY312 -m pytest tests/exametrics -q -p no:warnings` → the full
      backend-sis integration suite passes, well above the 26-test baseline.

### `lazeims-core` — the client, the gate and the hardening

- [ ] 22. C8 first, per D4 and the context's escalation: encrypt `api_key` at
      rest. Add `app/services/key_crypto.py` (`MultiFernet` over
      `PROCESSING_KEY_ENCRYPTION_KEYS`, falling back to a key derived from
      `session_secret_key` when unset), rename the column to
      `api_key_encrypted` (Text) with a plain `api_key` property that
      encrypts/decrypts, add the settings field, and write an alembic revision
      chaining off `f1a2b3c4d5e6` that adds the column, encrypts existing rows
      in a data migration, and drops the plaintext column. No call site changes
      because the property keeps the name.
      Files: `app/services/key_crypto.py`, `app/models/processing.py`,
      `app/config.py`,
      `alembic/versions/a2b3c4d5e6f7_phase13_encrypt_processing_api_key.py`,
      `tests/test_key_crypto.py`.
      Verify: start pg then `cd lazeims-core && $PY311 -m pytest -q` → 136 baseline
      tests still pass unchanged (proving the property is transparent), plus new
      tests that the stored column is not the plaintext, that a second key in the
      list still decrypts rows written under the first, and that
      `tests/test_migrations.py`'s `alembic upgrade head` succeeds.

- [ ] 23. Complete C1 and add C10 + C11 in the client layer. New
      `backend_sis` methods: `upsert_exam`, `get_exam`, `get_entitlements`,
      `request_processing`, `get_processing_request`, `start_collection_session`,
      `put_chunk`, `complete_collection_session`; all mutating calls send
      `Idempotency-Key` per §7.1's table and `contract_version`. Add
      `push_collection_auto` that measures the canonical payload and switches to
      the chunked path above `max_payload_mb` or on a `413`. Replace `_sis_http`
      with a code-preserving mapper (D9) that keeps the upstream status and code
      for §8's table and passes `details[]` through, falling back to
      `502 EXAMETRICS_ERROR` only for unrecognised failures. Depends on items 4
      and 5 (for the digest and shapes).
      Files: `app/services/backend_sis.py`, `app/routers/integration.py`,
      `tests/test_backend_sis_client.py`.
      Verify: start pg then
      `$PY311 -m pytest tests/test_backend_sis_client.py -q` → passes against an
      injected `httpx.MockTransport`: a 24 MB payload goes single-shot, a 60 MB
      payload is chunked and completed with the shared digest, a 413 on the
      single-shot path retries chunked, and an upstream
      `403 SCOPE_NOT_GRANTED` surfaces as 403 `SCOPE_NOT_GRANTED` (not 502).

- [ ] 24. Add C6: the `ExamProcessingRequest` model
      (`exam_processing_requests`: `exam_id` FK, `request_id`, `state`,
      `quote_json`, `closeout_revision`, `configuration_hash`, `requested_by` FK
      users, `decided_at`, `decision_reason`, `run_id`, `last_polled_at`), an
      alembic revision chaining off item 22's, and the endpoints
      `POST /exams/{id}/processing/requests` (seals nothing — reads the current
      snapshot's `closeout_revision` + `configuration_hash`, calls
      `request_processing`, mirrors the remote state locally),
      `GET /exams/{id}/processing/requests` and
      `POST /exams/{id}/processing/requests/{rid}/refresh`. `quote_json` is
      displayed and never trusted for billing. Depends on items 22, 23.
      Files: `app/models/processing.py`,
      `alembic/versions/b3c4d5e6f7a8_phase14_exam_processing_requests.py`,
      `app/routers/integration.py`, `app/schemas_exam.py`,
      `tests/test_processing_requests.py`.
      Verify: start pg then `$PY311 -m pytest tests/test_processing_requests.py -q`
      → passes with a fake transport: requesting processing persists the mirrored
      state and quote; refresh updates the state; the endpoint 409s when the exam
      has no sealed snapshot for the current revision.

- [ ] 25. Add C7: the phase guard. In `assert_transition_allowed`, replace the
      `ENTRY_LOCKED → PROCESSING` "has a link" check with "has an **APPROVED**
      `ExamProcessingRequest` whose `closeout_revision` **and**
      `configuration_hash` match the exam's current values", raising
      `CONFIGURATION_MISMATCH` with the request id, its state and the mismatched
      field as evidence. Update `submit_for_processing` to surface
      `APPROVAL_REQUIRED` with the quote rather than a bare 409. Depends on
      item 24.
      Files: `app/services/exam_phase.py`, `app/routers/integration.py`,
      `tests/test_processing_requests.py`, `tests/test_exams.py`.
      Verify: start pg then `$PY311 -m pytest tests/test_exams.py tests/test_processing_requests.py -q`
      → passes: `PROCESSING` is refused with no approval and with an approval for
      a stale revision, and allowed with a matching one; the existing exam-phase
      tests still pass.

- [ ] 26. Add C9: the webhook receiver — `POST /integration/exametrics/webhooks`
      (unauthenticated by session; authenticated by HMAC only), verifying
      `signature` with `hmac.compare_digest` over the canonical JSON exactly as
      `app/services/station_package.py` does, resolving the exam by
      `external_ref`, rejecting a bad signature with **401 and no state change**,
      and applying idempotently by `(request_id, event, occurred_at)` so a
      redelivery is a no-op. Applying updates the mirrored request state and, for
      `results.ready`, sets `link.last_status = RESULTS_READY_STATUS` so publish
      can be gated without polling. Depends on items 22, 24.
      Files: `app/routers/webhooks.py`, `app/routers/routers_registry.py`,
      `app/services/exametrics_webhooks.py`, `app/models/processing.py`
      (a `WebhookReceipt` table in item 24's migration or a follow-on revision),
      `tests/test_exametrics_webhooks.py`.
      Verify: start pg then `$PY311 -m pytest tests/test_exametrics_webhooks.py -q`
      → passes: a valid `processing.completed` advances the mirrored state; a
      tampered signature returns 401 and leaves the state untouched; the same
      envelope delivered twice changes state once.

- [ ] 27. Add C12: audit and notify across the whole spend path. Call
      `notifications.record` + `notifications.notify` on request created,
      approval changed (from both poll and webhook), run started, run
      completed/failed and results published, recording actor and `request_id`
      and never the key or the webhook secret. Depends on items 24, 25, 26.
      Files: `app/routers/integration.py`, `app/services/exametrics_webhooks.py`,
      `app/services/exametrics_provision.py`,
      `tests/test_audit_notifications.py`.
      Verify: start pg then `$PY311 -m pytest tests/test_audit_notifications.py -q`
      → passes, including a test that walks request→approve→run→publish and
      asserts one audit row per step with the expected `action` values, and that
      no audit `after_snapshot` anywhere contains a value matching the key.

- [ ] 28. Close the §10.2 enforcement gaps, which decide what can ever reach
      billing. Add phase gates to Excel import confirm
      (`app/routers/excel.py::confirm_import` — only `ENTRY_OPEN`), station sync
      apply (`app/services/station_sync.py::process_events` — reject events with
      `PHASE_NOT_OPEN` when the exam is past `ENTRY_LOCKED`, per event so one
      stale station cannot fail a batch) and closeout seal
      (`app/routers/closeout.py::create_snapshot` — only `ENTRY_LOCKED`). Enforce
      `DataEntererScope` on writes by extending
      `app/services/scope_assignment.py` with an
      `assert_data_enterer_scope(db, exam_id, user, school_id, subject_id)` called
      from the marks and attendance write paths, rejecting out-of-scope writes
      with the existing `RejectionCode` envelope.
      Files: `app/routers/excel.py`, `app/services/station_sync.py`,
      `app/routers/closeout.py`, `app/services/scope_assignment.py`,
      `app/routers/marks.py`, `tests/test_closeout.py`, `tests/test_excel.py`,
      `tests/test_station_sync.py`, `tests/test_marks.py`.
      Verify: start pg then `$PY311 -m pytest -q` → the whole Central suite passes,
      with new cases: confirming an Excel import in `PROCESSING` is 409; a sync
      event for a locked exam is rejected (not accepted, not a 500) while other
      events in the same batch still apply; sealing outside `ENTRY_LOCKED` is 409;
      a DE writing outside their `DataEntererScope` is rejected.

- [ ] 29. Add the §12 Central contract tests from the shared fixtures, using the
      transport-injection pattern `tests/test_cross_repo_sync.py` already
      establishes. Cover the document's named cases not yet covered:
      `test_exam_autoprovisioned_once`, `test_processing_requires_approval`,
      `test_approved_processing_runs_once`,
      `test_approval_invalidated_by_reopen`, `test_results_hidden_without_scope`,
      `test_webhook_signature_rejected`, `test_capabilities_drive_ui_payload`,
      and one asserting Central's collection payload digest equals
      `frozen_digest()`. Depends on items 7, 22–28.
      Files: `tests/test_exametrics_contract.py`.
      Verify: start pg then `$PY311 -m pytest tests/test_exametrics_contract.py -q`
      → all pass with no network; then `$PY311 -m pytest -q` for the full suite.

### `lazaims` — the operator experience

- [ ] 30. Add F7: typed bindings for every new endpoint on
      `processingApi` — `requests.create/list/refresh`, `entitlements`,
      `capabilities` (exists), plus the `ProcessingRequest`, `Quote`,
      `Entitlements` and `RowValidationDetail` interfaces.
      Files: `src/lib/api/exams.ts`.
      Verify: `cd lazaims && npx tsc --noEmit` → clean.

- [ ] 31. Add F8: surface row-addressable 422 detail. `apiClient.ts` currently
      only reads `details` from `payload.error.details`, but Central raises
      `HTTPException(detail={... "details": [...]})`, so FastAPI nests it under
      `detail.details` and every row detail is silently dropped today — fix
      `ApiError` to read both. Add a shared
      `src/components/ui/ValidationDetailTable.tsx` rendering
      `{student_id, subject_code, paper, code, message}` as a table with a
      row-count summary and a "copy as CSV" action.
      Files: `src/lib/apiClient.ts`,
      `src/components/ui/ValidationDetailTable.tsx`.
      Verify: `npx tsc --noEmit && npm run build` → both clean; the component is
      rendered from item 32 and 33's pages.

- [ ] 32. Add F3 + F4 to `ProcessingContent.tsx`: replace the boolean
      `configured` gating with three explicit capability-driven states per
      capability — **available** / **needs approval** / **not entitled** — and add
      the approval panel showing the quote (billable students, unit, amount,
      currency, expiry), who must approve, the instructions link, the live request
      state with a last-checked timestamp, and a §13.6 timeline
      (sealed → pushed → requested → approved → running → completed → published)
      with a timestamp per step. Say the price before the click (§13.2), name the
      blocker not the failure (§13.3), and warn before a reopen that it
      invalidates a paid approval (§13.9). Depends on items 30, 31.
      Files: `src/app/exams/[examId]/components/ProcessingContent.tsx`,
      `src/app/exams/[examId]/components/CloseoutContent.tsx` (the reopen warning).
      Verify: `npx tsc --noEmit && npm run build` → both clean; walk the page
      against a Central instance and confirm an unapproved exam shows the quote
      and "awaiting approval" rather than a 402.

- [ ] 33. Add F5: drive the PDF/ZIP extraction affordance in
      `RegistrationHubContent.tsx` from the `registrations.extract` capability
      instead of showing it unconditionally, and render item 31's table for the
      422 detail the extract/validate path returns.
      Files: `src/app/exams/[examId]/components/RegistrationHubContent.tsx`.
      Verify: `npx tsc --noEmit && npm run build` → both clean; with
      `registrations.extract` false the drop zone is disabled with a reason, and
      a malformed PDF produces a row table rather than a wall of text.

- [ ] 34. Add F6: on the results page distinguish "not processed yet" from "not
      entitled to read" — read `capabilities['results.read']` /
      `['results.download']` and the entitlements `results.reason`, and branch the
      empty state instead of showing one generic error (§13.4). Gate the two
      download buttons on `results.download` separately from the stats on
      `results.read`.
      Files: `src/app/results/components/ResultsContent.tsx`.
      Verify: `npx tsc --noEmit && npm run build` → both clean; a
      `403 SCOPE_NOT_GRANTED` renders the "not entitled" copy and a
      `409 PROCESSING_NOT_COMPLETE` renders the "not processed yet" copy.

### `frontend-sis` — the approval console (D8)

- [ ] 35. Add one admin page, `app/admin/exametrics-approvals/page.tsx`, with two
      tables — pending key-scope requests (`GET /integration/keys/pending`,
      `POST /integration/keys/{id}/approval`) and pending processing runs
      (`GET /integration/processing-requests/pending`,
      `POST /integration/processing-requests/{id}/decision`) — showing requester,
      exam, revision, the server-computed quote verbatim, and approve/reject with
      an optional note. Follow the existing `app/admin/requests/page.tsx` pattern
      (react-query + `Card`/`Table`/`Dialog`/`Badge` + `sonner`) and add the typed
      client in `lib/api/exametrics-integration.ts`. Add the page to the admin
      nav. Depends on items 15 and 21.
      Files: `app/admin/exametrics-approvals/page.tsx`,
      `lib/api/exametrics-integration.ts`, the admin nav component,
      `tests/exametrics/approvals-console.test.tsx`.
      Verify: `cd frontend-sis && npx tsc --noEmit` → clean (this is the gate;
      `npm run build` cannot be used, see §0), and
      `npx vitest run tests/exametrics` → the new test passes: the table renders a
      quote from mocked data and approve fires the decision mutation with the
      chosen scopes and note.

### `lazeims-station` — the two UX items

- [ ] 36. Add S1: carry `exam.phase` to the station. Central includes
      `exam_phase` in the `process_events` return (item 6 added the field to
      `SyncResponse`); the station stores it in `station_meta` on each successful
      sync and `GET /api/status` reports it, with `/api/progress` warning when the
      phase is at or past `ENTRY_LOCKED` so an operator learns before entry closes
      rather than after. Depends on item 6; the Central half lands with item 28's
      `station_sync.py` change.
      Files: `lazeims-core/app/services/station_sync.py`,
      `lazeims-station/station/sync.py`, `lazeims-station/station/main.py`,
      `lazeims-station/tests/test_sync.py`,
      `lazeims-core/tests/test_cross_repo_sync.py`.
      Verify: `cd lazeims-station && $PY311 -m pytest -q` → 34 baseline plus the
      new cases pass; then start pg and
      `cd lazeims-core && $PY311 -m pytest tests/test_cross_repo_sync.py -q` → the
      real station reads the real Central's `exam_phase` off a real sync response.

- [ ] 37. Add S2: surface rejected events locally with a correction workflow. Add
      a v2→v3 SQLite migration (bump `SCHEMA_VERSION` to 3) adding
      `rejected_at`, `rejection_code` and `superseded_by` to `outbox_events`; add
      `GET /api/rejections` listing REJECTED events with the decoded natural key,
      code and human message, and `POST /api/rejections/{event_id}/supersede`
      which marks the rejected event `SUPERSEDED` and links the corrected event
      the operator has just re-entered. `closeout.compute_readiness`'s
      `REJECTED_SYNC_EVENTS` blocker must not count superseded ones, so a
      correction actually unblocks closeout — and therefore approval. Migrations
      must stay additive and never drop marks or outbox rows, as
      `apply_migrations` already guarantees.
      Files: `station/migrations.py`, `station/__init__.py`, `station/outbox.py`,
      `station/main.py`, `tests/test_sync.py`, `tests/test_station.py`,
      `lazeims-core/app/services/closeout.py`,
      `lazeims-core/tests/test_closeout.py`.
      Verify: `cd lazeims-station && $PY311 -m pytest -q` → passes, including a
      test that upgrading a populated v2 database to v3 preserves every mark and
      outbox row; then start pg and
      `cd lazeims-core && $PY311 -m pytest tests/test_closeout.py -q` → a
      superseded rejection no longer blocks closeout.

### Close-out

- [ ] 38. Record the outcome in the design document: add an "Amendment II —
      completion pass" section with the §14 assumption table from §1.1 of this
      plan verbatim (so the owner can correct one answer without reading code),
      the D1–D10 decisions in one line each, and mark every task in §10 as
      implemented with its file references. Also correct the environment notes in
      any repo `AGENTS.md`/`README.md` that claim dependencies are pre-installed.
      Files: `lazeims-core/docs/BACKEND_SIS_INTEGRATION_PLAN.md`,
      `backend-sis/AGENTS.md`, `lazeims-common/README.md`.
      Verify: `grep -c "^| 14\." docs/BACKEND_SIS_INTEGRATION_PLAN.md` → 10 rows,
      one per §14 question; every §10 table row carries a file path.

- [ ] 39. Run every suite one final time, commit per repo on
      `feat/exametrics-integration-complete`, push each branch and open one PR per
      repo in the merge order of §1.3. Each PR body states which tasks it closes,
      the assumption table, and — in the `lazeims-core` PR — the §1.4 deployment
      actions (rotate `SESSION_SECRET_KEY` and
      `STATION_PACKAGE_INTEGRITY_KEY`; set `BACKEND_SIS_BASE_URL`; set the new
      settings). Cross-link the six PRs.
      Files: none (VCS only).
      Verify: in one shell call per repo — `lazeims-common` `pytest -q`;
      pg + `lazeims-core` `pytest -q`; `lazeims-station` `pytest -q`;
      pg + `backend-sis` `pytest tests/exametrics -q -p no:warnings`;
      `lazaims` `npx tsc --noEmit && npm run build`; `frontend-sis`
      `npx tsc --noEmit` and `npx vitest run tests/exametrics`. Every suite is at
      or above its §0 baseline with no new failures, and `git status --porcelain`
      is empty in all six repos.

---

## 2. Known gaps and assumptions carried forward

* **No sandbox ExaMetrics tenant** (14.9). Every cross-repo assertion is against
  a fake transport plus shared fixtures. If the two services' shapes drift, item
  21's and item 29's fixture tests fail on both sides simultaneously — which is
  the point — but neither proves a live deployment agrees.
* **`backend-sis` has 16 alembic heads.** The new revisions chain correctly off
  the integration lineage but `alembic upgrade head` remains unrunnable there.
  Merging the heads is a separate, riskier change and is deliberately not
  attempted here.
* **`frontend-sis` has 14 pre-existing vitest failures** and a `npm run build`
  that needs a live backend. Its gate is `tsc` + the scoped new test file. Fixing
  those failures is out of scope.
* **Retention and deletion (14.8)** are documented rather than implemented; the
  existing `collection/reset` is named as the discard affordance.
* **`processing.execute` grantable directly** for accounts already paying under
  the old arrangement (Phase 3's escape hatch) is preserved: the key-level
  approval queue still grants `results:process` independently of the per-run gate,
  so an existing payer is not blocked. The per-run gate still applies, so nothing
  paid happens without a record — see D1.
