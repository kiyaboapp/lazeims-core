# LAZEIMS ⇄ ExaMetrics (`backend-sis`) Integration — Follow-up Work Plan

**Audience:** the engineer who owns the `backend-sis` (ExaMetrics) API, plus the
owners of `lazaims`, `lazeims-central-api`, `lazeims-common`, `lazeims-station`.

**Goal:** remove manual ID pasting, let a LAZEIMS operator self-serve everything
they are entitled to (create the exam, send marks, read basic details), and put
**processing behind an explicit approval gate because it is a paid action**.

**Status of this document:** design + task breakdown. Sections marked
`[EXISTS]` describe verified current behaviour with file references. Sections
marked `[PROPOSED]` are new work. Nothing here has been implemented.

> `backend-sis` is **not** checked out in this workspace, so its internals are
> described only through the contract LAZEIMS already calls. Anything about its
> internal storage is deliberately left to its owner.

---

## 1. Current state `[EXISTS]`

### 1.1 How the link is made today

`ExamProcessingLink` (`lazeims-central-api/app/models/processing.py`) binds one
Central exam to one ExaMetrics exam:

| Column | Type | Meaning |
|---|---|---|
| `exam_id` | UUID FK → `exams.id` | Central exam (unique, one link per exam) |
| `backend_exam_id` | `String(36)` | **Hand-pasted** ExaMetrics exam id |
| `api_key` | `String(120)` | Per-exam purchased key, sent as `X-API-Key` |
| `last_submitted_at`, `last_status` | | Last handoff bookkeeping |

The key is intentionally **not** in `Exam.settings` so it never leaks through
ordinary exam serialisation, and `ProcessingLinkOut` never returns it.

### 1.2 Endpoints LAZEIMS already calls

From `lazeims-central-api/app/services/backend_sis.py`, all authenticated with
`X-API-Key` against `settings.backend_sis_base_url`:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/integration/registrations/extract` | PDF/ZIP → candidate rows. **Not exam-scoped**, described as free |
| `POST` | `/integration/exams/{backend_exam_id}/collection` | Push the sealed collection |
| `POST` | `/integration/exams/{backend_exam_id}/process` | Trigger processing |
| `GET` | `/integration/exams/{backend_exam_id}/status` | Poll processing status |
| `GET` | `/integration/exams/{backend_exam_id}/results/stats` | Result statistics |
| `GET` | `/integration/exams/{backend_exam_id}/results/school/{centre}.pdf` | Per-centre PDF (`include_marks`) |
| `GET` | `/integration/exams/{backend_exam_id}/results/rawdata.xlsx` | Raw export (filters: `centre_number`, `region_name`, `council_name`, `ward_name`) |

### 1.3 The collection payload LAZEIMS sends

Built by `app/services/processing_submit.py::build_collection_payload`. This is
the **de facto contract** and must keep working during migration:

```jsonc
{
  "schools":  [{ "centre_number", "school_name", "school_type",
                 "region_name", "council_name", "ward_name" }],
  "subjects": [{ "subject_code", "subject_name", "subject_short",
                 "has_theory_2", "has_practical",
                 "theory_max", "theory_2_max", "practical_max",
                 "is_primary", "is_olevel", "is_alevel" }],
  "students": [{ "student_id", "centre_number",
                 "first_name", "middle_name", "surname", "sex" }],
  "marks":    [{ "student_id", "centre_number", "subject_code",
                 "theory_marks", "theory_2_marks", "practical_marks",
                 "sat_theory", "sat_theory_2", "sat_practical" }]
}
```

Natural keys throughout (`student_id` + `centre_number` + `subject_code`) — no
Central row ids cross the boundary. Keep that property.

### 1.4 Gating that exists today

- `_require_link` (`app/routers/integration.py`) → **409** `PROCESSING_NOT_CONFIGURED`
  when no link, **503** `PROCESSING_DISABLED` when `backend_sis_base_url` is empty.
- Link write, submit, publish, and PDF extraction all require `require_exam_admin()`.
- `ENTRY_LOCKED → PROCESSING` requires a configured link (`app/services/exam_phase.py`).
- `PROCESSING → RESULTS_PUBLISHED` requires ExaMetrics to report results ready.

### 1.5 Known blockers

1. **`BACKEND_SIS_BASE_URL` is not set in the deployed `.env`**, so
   `processing_enabled` is `False` and every ExaMetrics call raises. Credentials
   can be saved but nothing can actually reach ExaMetrics.
2. **Migrations have drifted from the ORM.** Applying the alembic chain produces
   `exams.id` as `integer`; the ORM declares `PG_UUID`. Tests never catch it
   because `tests/conftest.py` builds the schema with
   `Base.metadata.create_all`, not alembic. Fix before any schema work below.

---

## 2. What is wrong with the current model

| # | Problem | Consequence |
|---|---|---|
| P1 | Operator must paste an opaque `backend_exam_id` | Nobody knows where to get it; typos silently target the wrong exam or a competitor's exam |
| P2 | The exam must already exist in ExaMetrics | Two systems are created by hand and can disagree on subjects/papers/max marks |
| P3 | One key grants *everything* the key can reach | A key that can push marks can also trigger a **paid** processing run |
| P4 | No entitlement discovery | LAZEIMS cannot tell the operator what they may do; it can only fail at the moment of action |
| P5 | Payment/approval is out of band | Processing can be triggered with no record of who authorised the spend |
| P6 | No schema negotiation | A field added on either side is discovered as a runtime error |
| P7 | Status is poll-only | Long runs waste requests and the operator refreshes blindly |

---

## 3. Target model

```
LAZEIMS operator                Central API                    backend-sis (ExaMetrics)
     │                               │                                  │
     │ 1. paste API key only ───────►│                                  │
     │                               │ 2. GET /integration/me ─────────►│  capabilities + tenant
     │◄── capabilities shown ────────│◄─────────────────────────────────│
     │                               │                                  │
     │ 3. work normally              │ 4. PUT  /integration/exams       │  upsert by external_ref
     │    (no ids to copy)           │    (auto-provision) ────────────►│  → returns exam ref
     │                               │ 5. POST .../collection ─────────►│  free
     │                               │                                  │
     │ 6. request processing ───────►│ 7. POST .../processing-requests ►│  quote + PENDING_APPROVAL
     │◄── "awaiting approval" ───────│◄──────── webhook/poll ───────────│  approver pays/approves
     │◄── results available ─────────│◄──────── webhook/poll ───────────│  processing runs
```

Three principles:

1. **The operator supplies exactly one secret: the API key.** Every identifier is
   negotiated machine-to-machine.
2. **Capabilities are declared by the server, not guessed by the client.** Same
   principle already applied to exam navigation via `GET /exams/{id}/access`.
3. **Free actions are self-service; paid actions require approval.** The boundary
   is explicit in the API, not a convention.

---

## 4. Provisioning: replace the pasted exam id `[PROPOSED]`

### 4.1 Identity handshake

`GET /integration/me` — validates the key and tells LAZEIMS what it may do.

```jsonc
// 200
{
  "contract_version": "exametrics-integration/v2",
  "tenant": { "id": "tnt_9f3", "name": "Lake Zone", "environment": "production" },
  "capabilities": {
    "exam.provision":      true,
    "collection.push":     true,
    "registrations.extract": true,
    "exam.read":           true,
    "processing.request":  true,   // may ASK
    "processing.execute":  false,  // may NOT run without approval
    "results.read":        false,
    "results.download":    false
  },
  "limits": { "max_students_per_exam": 200000, "max_payload_mb": 50,
              "rate_limit_per_minute": 120 },
  "supported_rules_versions": ["1.0"]
}
```

This single endpoint kills P4 and lets the LAZEIMS UI stop guessing. It must work
with **no exam context** so the key can be validated the moment it is pasted.

### 4.2 Idempotent exam upsert

`PUT /integration/exams` — keyed on `external_ref`, which is the **Central exam
UUID**. Calling it twice with the same body is a no-op; calling it with a changed
body updates the definition while processing has not started.

```jsonc
// request
{
  "contract_version": "exametrics-integration/v2",
  "external_ref": "9c1f...-uuid-from-central",   // idempotency key
  "name": "FTNA 2026",
  "exam_code": "FTNA-2026",
  "level": "FTNA",                                // registry exam level name
  "board": "NECTA",
  "start_date": "2026-10-05",
  "end_date": "2026-10-16",
  "zone_name": "Lake Zone",
  "filling_mode": "TOTAL_MARKS",                  // lazeims_common FillingMode
  "rules_version": "1.0",
  "configuration_hash": "sha256:...",             // seal from Central
  "subjects": [
    { "subject_code": "011", "subject_name": "History", "subject_short": "011",
      "has_theory_2": false, "has_practical": false,
      "theory_max": 100, "theory_2_max": null, "practical_max": null,
      "is_primary": false, "is_olevel": true, "is_alevel": false }
  ]
}
```

```jsonc
// 200 / 201
{
  "exam_ref": "exm_01J8...",        // replaces the hand-pasted id
  "external_ref": "9c1f...",
  "state": "OPEN_FOR_COLLECTION",
  "created": true,
  "configuration_accepted": true,
  "warnings": [
    { "code": "SUBJECT_MAX_MARKS_DIFFERS", "subject_code": "011",
      "message": "Existing definition had theory_max 90; updated to 100." }
  ]
}
```

**Why `external_ref` matters:** retries after a timeout are safe, and the two
systems can always be reconciled without a human comparing names.

### 4.3 Central-side changes

| Change | File |
|---|---|
| Add `external_ref` concept (Central exam UUID is already it — just send it) | `app/services/backend_sis.py` |
| Make `backend_exam_id` **nullable** and populate it from `exam_ref` | `app/models/processing.py` + migration |
| Add `provision_exam()` client call | `app/services/backend_sis.py` |
| Add `capabilities` + `capabilities_fetched_at` cache columns | `app/models/processing.py` |
| Link endpoint accepts **key only**; verifies via `/integration/me`; provisions on demand | `app/routers/integration.py` |
| Auto-provision before first `push_collection` if `backend_exam_id` is null | `app/routers/integration.py` |

Keep `backend_exam_id` writable by an admin as an **escape hatch** for exams that
already exist in ExaMetrics (migration + support cases).

---

## 5. Capability & entitlement model `[PROPOSED]`

### 5.1 Scopes

Issue keys with explicit scopes rather than one all-powerful key:

| Scope | Paid? | Grants |
|---|---|---|
| `exam.provision` | free | Create/update an exam definition |
| `collection.push` | free | Upload collected marks/attendance |
| `registrations.extract` | free | PDF/ZIP → rows (already free today) |
| `exam.read` | free | Basic exam state, counts, validation feedback |
| `processing.request` | free | **Ask** for processing; creates a request + quote |
| `processing.execute` | **paid** | Actually run processing — granted per approval |
| `results.read` | **paid** | Statistics/aggregates |
| `results.download` | **paid** | Per-centre PDF, raw XLSX |

A default LAZEIMS key is issued with the five free scopes plus
`processing.request`. That satisfies the requirement: *do everything except
processing, which needs approval*.

### 5.2 Entitlements are per exam, not just per key

Approval is granted for **one exam and one closeout revision**, so a paid
approval cannot be silently reused after the collection is reopened and changed.

```jsonc
GET /integration/exams/{exam_ref}/entitlements
{
  "exam_ref": "exm_01J8...",
  "closeout_revision": 1,
  "processing": { "state": "APPROVED", "approved_at": "...",
                  "approved_by": "billing@…", "expires_at": "...",
                  "valid_for_configuration_hash": "sha256:..." },
  "results":    { "state": "LOCKED", "reason": "PROCESSING_NOT_COMPLETE" }
}
```

If `valid_for_configuration_hash` no longer matches, processing must be
re-approved. This prevents "pay once, reprocess a different dataset forever".

---

## 6. Approval / payment gate `[PROPOSED]`

### 6.1 Request → quote → approve → execute

```
POST /integration/exams/{exam_ref}/processing-requests
```

```jsonc
// request
{ "external_ref": "9c1f...", "closeout_revision": 1,
  "configuration_hash": "sha256:...",
  "counts": { "students": 18432, "centres": 96, "subject_registrations": 121004 },
  "requested_by": { "name": "A. Mwita", "role": "EXAM_ADMIN",
                    "contact": "<supplied by the LAZEIMS operator>" },
  "note": "FTNA 2026 final submission" }
```

```jsonc
// 202 Accepted
{ "request_id": "prq_01J8...",
  "state": "PENDING_APPROVAL",
  "quote": { "currency": "TZS", "amount": 4608000,
             "unit": "per_student", "unit_amount": 250,
             "billable_students": 18432,
             "expires_at": "2026-11-05T00:00:00Z" },
  "approval": { "method": "EXAMETRICS_CONSOLE",
                "instructions_url": "https://…/approvals/prq_01J8…" },
  "next_poll_after": "2026-10-31T06:00:00Z" }
```

States: `PENDING_APPROVAL → APPROVED → RUNNING → COMPLETED`
with `REJECTED`, `EXPIRED`, `FAILED` as terminals.

`POST /integration/exams/{exam_ref}/process` then becomes **idempotent and
approval-checked**:

- no approval → **402 `PAYMENT_REQUIRED`** (or 403 `APPROVAL_REQUIRED`) carrying
  `request_id` and the quote so LAZEIMS can show it verbatim;
- approved → starts once and returns the same `run_id` on retry.

### 6.2 Why a quote before payment

The quote is computed from counts LAZEIMS already has after sealing, so the
operator sees the price **before** committing, and the approver sees exactly what
they are paying for. Never derive the price from a number the client can inflate
— recompute from the pushed collection.

### 6.3 Central-side representation

New table `exam_processing_requests`:

| Column | Notes |
|---|---|
| `exam_id` FK | Central exam |
| `request_id` | ExaMetrics id |
| `state` | mirror of remote state |
| `quote_json` | shown to the operator, never trusted for billing |
| `closeout_revision`, `configuration_hash` | what was approved |
| `requested_by` FK users | audit |
| `decided_at`, `decision_reason` | audit |

Add phase guard: `ENTRY_LOCKED → PROCESSING` requires an **APPROVED** request for
the current `closeout_revision`, not merely a configured link.

---

## 7. Reliability contract `[PROPOSED]`

### 7.1 Idempotency

Every mutating call carries `Idempotency-Key` (Central already does this
internally for marks — reuse the pattern in `app/services/idempotency.py`):

| Call | Key |
|---|---|
| `PUT /integration/exams` | `external_ref` + `configuration_hash` |
| `POST .../collection` | `external_ref` + `closeout_revision` + payload hash |
| `POST .../processing-requests` | `external_ref` + `closeout_revision` |
| `POST .../process` | `request_id` |

Replaying must return the original result with `"replayed": true`, never a second
charge.

### 7.2 Chunked collection upload

`max_payload_mb` will be exceeded by real zones. Support a session upload:

```
POST   /integration/exams/{exam_ref}/collection-sessions        → session_id
PUT    /integration/collection-sessions/{id}/chunks/{n}          → per-chunk ack
POST   /integration/collection-sessions/{id}/complete            → manifest + digest
```

`complete` must verify a client-supplied SHA-256 over the canonical payload.
Central already has `lazeims_common.hashing.sha256_prefixed` and
`canonical_bytes` — reuse them so both sides compute the digest identically.

### 7.3 Webhooks with polling fallback

```jsonc
POST {lazeims_callback_url}
{
  "event": "processing.completed",     // also: processing.approval_changed,
                                       // processing.failed, results.ready
  "exam_ref": "exm_…", "external_ref": "9c1f…",
  "request_id": "prq_…", "state": "COMPLETED",
  "occurred_at": "…",
  "signature": "hmac-sha256:…"          // over canonical JSON
}
```

Sign with a **per-tenant webhook secret**, and keep polling as the fallback since
many deployments sit behind NAT. Central verifies with the same
`hmac.compare_digest` approach used in `app/services/station_package.py`.

### 7.4 Versioning

Send `contract_version: "exametrics-integration/v2"` on every request and have
the server reject unknown majors. Add the constant to `lazeims-common`
alongside the existing ones:

```python
# lazeims-common/lazeims_common/__init__.py  [EXISTS: pattern]
RULES_VERSION = "1.0"
STATION_PACKAGE_CONTRACT = "station-package/v1"
STATION_SYNC_CONTRACT = "station-sync/v1"
COLLECTION_EXPORT_CONTRACT = "collection-export/v1"
EXAMETRICS_INTEGRATION_CONTRACT = "exametrics-integration/v2"   # [PROPOSED]
```

Adding a field is compatible; removing/renaming one is a major bump.

---

## 8. Error contract `[PROPOSED]`

Mirror the envelope Central already emits (`app/main.py`):

```jsonc
{ "error": { "code": "APPROVAL_REQUIRED",
             "message": "Processing needs approval before it can run.",
             "details": { "request_id": "prq_…", "state": "PENDING_APPROVAL" },
             "request_id": "…" } }
```

Codes LAZEIMS should handle explicitly:

| HTTP | Code | LAZEIMS behaviour |
|---|---|---|
| 401 | `INVALID_API_KEY` | Mark link unverified, prompt to re-enter the key |
| 403 | `SCOPE_NOT_GRANTED` | Hide/disable the action, name the missing scope |
| 402/403 | `APPROVAL_REQUIRED` | Show the quote and the approval instructions |
| 409 | `EXAM_STATE_CONFLICT` | Explain what must change before retrying |
| 409 | `CONFIGURATION_HASH_MISMATCH` | Offer to re-push the collection |
| 413 | `PAYLOAD_TOO_LARGE` | Switch to chunked upload |
| 422 | `VALIDATION_FAILED` | Render `details[]` per row/field |
| 429 | `RATE_LIMITED` | Honour `Retry-After` |
| 503 | `PROCESSING_TEMPORARILY_UNAVAILABLE` | Keep state, retry with backoff |

Every 422 should return **row-addressable** detail
(`{ student_id, subject_code, paper, code, message }`) so LAZEIMS can point at
the offending candidate instead of showing a wall of text.

---

## 9. Security requirements

1. **The key never reaches the browser.** Already true — all calls are proxied by
   Central and `ProcessingLinkOut` omits the key. Preserve this.
2. **Encrypt `api_key` at rest.** It is currently a plain `String(120)`. Use an
   app-level encryption key (envelope encryption) so a DB dump is not a key leak.
3. **Verify on save.** Call `/integration/me` before persisting so a bad key
   fails immediately instead of at submission time.
4. **Scope-limit by default.** Issue keys without `processing.execute` /
   `results.*`; grant those through approval only.
5. **Rotation without downtime.** Support two active keys per tenant during
   rotation, and add a Central endpoint to replace the key in place.
6. **Audit the spend path.** Record who requested and who approved processing;
   reuse `app/services/notifications.py::record`.
7. **Least data.** Reports/exports return only what the tenant owns; never allow
   a centre number from another tenant to resolve.

### Pre-existing exposure to fix first (unrelated to this design)

- `lazaims/.env` and `lazaims/.env.local` are **tracked in git** and
  `lazaims/.gitignore` does not list `.env`.
- `lazeims-central-api/.env.bak.1785156329` is **tracked** and contains
  non-placeholder `SESSION_SECRET_KEY` and `STATION_PACKAGE_INTEGRITY_KEY`.

Untrack these, add ignore rules, and **rotate both secrets** — the station
package integrity key signs station manifests, and the session secret signs
cookies. Treat both as compromised until rotated.

---

## 10. Work breakdown per repository

### 10.1 `backend-sis` (ExaMetrics API) — the new surface

| # | Task | Notes |
|---|---|---|
| B1 | `GET /integration/me` | Tenant + capabilities + limits + supported rules versions |
| B2 | `PUT /integration/exams` | Idempotent upsert keyed on `external_ref`; returns `exam_ref` |
| B3 | `GET /integration/exams/{ref}` | State, counts, config hash, accepted subjects |
| B4 | `GET /integration/exams/{ref}/entitlements` | Per-exam, per-revision entitlement |
| B5 | Scope enforcement middleware | Reject with `SCOPE_NOT_GRANTED` |
| B6 | `POST .../processing-requests` + quote engine | Price recomputed server-side |
| B7 | Approval console + state machine | `PENDING_APPROVAL → APPROVED/REJECTED/EXPIRED` |
| B8 | Make `POST .../process` approval-checked + idempotent | Return stable `run_id` |
| B9 | Collection sessions (chunked upload) | Digest-verified `complete` |
| B10 | Webhooks + HMAC signing + retry/DLQ | Per-tenant secret |
| B11 | Row-addressable 422 validation | `{student_id, subject_code, paper, code}` |
| B12 | Rate limits + `Retry-After` | Publish limits via B1 |
| B13 | Key issuance/rotation with scopes | Two active keys during rotation |
| B14 | Keep v1 paths working | Until Central migrates (§11) |

**Fields `backend-sis` must accept** — exactly §4.2 for the definition and §1.3
for the collection. Do not require any Central row id.

### 10.2 `lazeims-central-api`

| # | Task | Files |
|---|---|---|
| C0 | **Fix migration/ORM drift** (`exams.id` integer vs UUID) and make tests run against migrations | `alembic/`, `tests/conftest.py` |
| C1 | Client methods: `identity()`, `provision_exam()`, `get_entitlements()`, `request_processing()` | `app/services/backend_sis.py` |
| C2 | `backend_exam_id` nullable + `external_ref` send + capability cache columns | `app/models/processing.py`, migration |
| C3 | Link endpoint takes **key only**, verifies via `/integration/me`, stores capabilities | `app/routers/integration.py` |
| C4 | Auto-provision on demand before first collection push | `app/routers/integration.py` |
| C5 | `GET /exams/{id}/processing/capabilities` for the UI | new |
| C6 | `exam_processing_requests` table + request/poll endpoints | new model + router |
| C7 | Phase guard: `PROCESSING` requires an APPROVED request for the current revision | `app/services/exam_phase.py` |
| C8 | Encrypt `api_key` at rest; add rotation endpoint | `app/models/processing.py`, `app/security.py` |
| C9 | Webhook receiver with HMAC verification + idempotent apply | new router |
| C10 | Chunked upload client | `app/services/backend_sis.py` |
| C11 | Map remote error codes → Central envelope | `app/routers/integration.py` |
| C12 | Audit + notify on request/approve/run/publish | `app/services/notifications.py` |

Also close the enforcement gaps found earlier, which affect what gets sent for
billing: no phase gate on **Excel confirm**, **station sync apply**, or
**closeout seal**, and `DataEntererScope` is never enforced on writes.

### 10.3 `lazaims` (frontend)

| # | Task | Files |
|---|---|---|
| F1 | Processing page: **key-only** form; drop the exam-id field to an "Advanced" escape hatch | `src/app/exams/[examId]/components/ProcessingContent.tsx` |
| F2 | Show verified tenant + capability list after the key is saved | same |
| F3 | Replace boolean gating with capability-driven states: *available / needs approval / not entitled* | same |
| F4 | Approval panel: quote, who must approve, instructions link, live state | same |
| F5 | Drive PDF/ZIP extraction affordance from `registrations.extract` capability | `RegistrationHubContent.tsx` |
| F6 | Results page: distinguish "not processed" from "not entitled to read" | `src/app/results/components/ResultsContent.tsx` |
| F7 | Typed bindings for the new endpoints | `src/lib/api/exams.ts` |
| F8 | Surface row-addressable 422 detail in a table | shared component |

### 10.4 `lazeims-common`

| # | Task |
|---|---|
| K1 | Add `EXAMETRICS_INTEGRATION_CONTRACT` |
| K2 | Pydantic schemas for exam provisioning, collection push, processing request/quote (`schemas/exametrics.py`) so Central and tests share one definition |
| K3 | Shared canonicalisation for the collection digest (reuse `hashing.canonical_bytes`) |

### 10.5 `lazeims-station`

No contract change — stations never talk to ExaMetrics. Two UX improvements
that matter once processing is gated:

| # | Task |
|---|---|
| S1 | Include `exam.phase` in sync responses so a station can warn before entry locks |
| S2 | Surface rejected events locally with a correction workflow, so they don't block closeout (and therefore approval) |

---

## 11. Rollout

**Phase 0 — unblock.** Set `BACKEND_SIS_BASE_URL`; fix migration drift (C0);
untrack + rotate leaked secrets (§9).

**Phase 1 — additive, no behaviour change.** Ship B1–B3; Central verifies keys
via `/integration/me` and caches capabilities. Manual exam id still accepted.

**Phase 2 — auto-provisioning.** Ship B2 + C2–C4. New exams never need a pasted
id; existing links keep working. Backfill `external_ref` for current links.

**Phase 3 — approval gate.** Ship B4–B8 + C6–C7 + F3–F4. Until every tenant has
migrated, keep `processing.execute` grantable directly for accounts already
paying under the old arrangement.

**Phase 4 — hardening.** Chunked upload (B9/C10), webhooks (B10/C9), encryption
(C8), rate limits (B12), key rotation (B13).

**Phase 5 — retire v1.** Remove the manual-id path and v1 endpoints once no link
lacks an `external_ref`.

Each phase is independently shippable and reversible; nothing requires a
flag-day cutover.

---

## 12. Testing

**Contract tests (shared fixtures in `lazeims-common`)** so both sides assert the
same JSON — the pattern already used for station sync
(`tests/test_cross_repo_sync.py`).

Central tests to add:

| Test | Asserts |
|---|---|
| `test_link_accepts_key_only` | No `backend_exam_id` required; capabilities cached |
| `test_invalid_key_rejected_on_save` | Bad key fails at save, not at submit |
| `test_exam_autoprovisioned_once` | Two pushes → one `exam_ref`, idempotent |
| `test_processing_requires_approval` | `process` → 402/403 with quote when unapproved |
| `test_approved_processing_runs_once` | Retry returns same `run_id`, no double charge |
| `test_approval_invalidated_by_reopen` | Revision/hash change ⇒ re-approval needed |
| `test_results_hidden_without_scope` | `results.read` absent ⇒ clear "not entitled" |
| `test_webhook_signature_rejected` | Bad HMAC ⇒ 401, no state change |
| `test_capabilities_drive_ui_payload` | `/processing/capabilities` matches remote |

Use a **fake ExaMetrics transport** (the station sync suite already injects a
transport callable) so these run with no network.

---

## 13. Making it genuinely nice

These are the differences between "integrated" and "pleasant":

1. **Zero ids in the UI.** The operator pastes a key once. If they ever see an
   `exam_ref`, it is read-only diagnostic text, never an input.
2. **Say the price before the click.** Show billable students and amount from the
   quote, plus what changes if they reopen and re-seal.
3. **Name the blocker, not the failure.** "Processing needs approval — requested
   31 Jul, awaiting Finance" beats "403 Forbidden".
4. **Separate *not yet* from *not allowed*.** Results absent because processing
   has not run is a different sentence from results absent because the tenant has
   no `results.read`.
5. **Make waiting calm.** Persist request state server-side, show the last
   checked time, and let webhooks update it without a manual refresh.
6. **One place to see the handoff.** A timeline on the Processing page: sealed →
   pushed → requested → approved → running → completed → published, each with a
   timestamp and actor.
7. **Reconciliation is a first-class action.** "Compare with ExaMetrics" showing
   counts on both sides and a digest match, using the existing
   `lazeims_common.reconcile` helpers.
8. **Never lose work to a timeout.** Chunked upload plus idempotent retry means a
   dropped connection resumes instead of restarting.
9. **Explain re-approval honestly.** If reopening entry invalidates a paid
   approval, say so *before* the reopen is confirmed.
10. **Keep the escape hatch discoverable but quiet.** Manual `exam_ref` entry
    lives under "Advanced", for support cases only.

---

## 14. Decisions needed from the `backend-sis` owner

1. Is `exam_ref` stable for the life of an exam, and is `external_ref` unique
   per tenant (not globally)?
2. Billing unit — per student, per subject registration, or per run? Does a
   re-run after a reopen cost again?
3. Who approves: an ExaMetrics console user, or can LAZEIMS submit payment proof?
4. Quote lifetime, and behaviour when counts change after approval.
5. Are `results.read` and `results.download` separately purchasable, or one bundle?
6. Webhook support now, or polling only for the first release?
7. Maximum accepted payload, and whether chunked upload is required from day one.
8. Retention: how long are collections and results kept, and can LAZEIMS request
   deletion?
9. Sandbox tenant for CI, so contract tests run against a real implementation.
10. Is `/integration/registrations/extract` permanently free and unmetered?

---

## 15. Quick reference — endpoint map

| Concern | Today `[EXISTS]` | Target `[PROPOSED]` | Paid |
|---|---|---|---|
| Validate key | *(none)* | `GET /integration/me` | free |
| Create/update exam | *(manual, out of band)* | `PUT /integration/exams` | free |
| Read exam | *(none)* | `GET /integration/exams/{ref}` | free |
| Entitlements | *(none)* | `GET /integration/exams/{ref}/entitlements` | free |
| Extract PDF/ZIP | `POST /integration/registrations/extract` | unchanged | free |
| Push collection | `POST /integration/exams/{id}/collection` | + collection sessions | free |
| Request processing | *(none)* | `POST /integration/exams/{ref}/processing-requests` | free |
| Run processing | `POST /integration/exams/{id}/process` | approval-checked, idempotent | **paid** |
| Status | `GET /integration/exams/{id}/status` | + webhooks | free |
| Result stats | `GET /integration/exams/{id}/results/stats` | scope `results.read` | **paid** |
| Centre PDF | `GET /integration/exams/{id}/results/school/{centre}.pdf` | scope `results.download` | **paid** |
| Raw XLSX | `GET /integration/exams/{id}/results/rawdata.xlsx` | scope `results.download` | **paid** |
