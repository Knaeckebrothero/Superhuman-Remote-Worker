# User-Defined Experts (Slice 1) — test-coverage gaps (what is NOT yet tested)

**As of 2026-06-16.** Companion to
`docs/tests/user_defined_experts_slice1_verification.md` (which has the runnable
T1–T6 procedures). This doc is the **inventory** of everything still untested or
unverified, so post-compaction work can resume without rediscovery.

Feature: `docs/done/global_expert_management.md` ·
Plan/status: `docs/superpowers/plans/2026-06-15-user-defined-experts-slice-1.md`
(see the "CURRENT STATE" callout at the top).

**Legend:** ✅ tested · 🟡 partially · ❌ not tested · ⛔ not implemented (so untestable)

---

## A. Agent runtime application — ❌ NOT TESTED (highest priority)

The core value path — an agent loading a DB expert, **fencing** the persona, and
**freezing** it into `resolved_config` — has **never been observed end-to-end**.
The first dev-k3d job froze the *default* persona (the bug that became fix #3);
after fixing it, the env's job-dispatch / session-provisioning pipeline was
wedged and no further job reached an agent on the fixed image.

Run **T1–T6** in `..._verification.md` to close this:
- ❌ **T1** worker job → fragment merged onto base + **fenced** sentinel persona in `resolved_config`
- ❌ **T2** session → lifespan `_apply_db_expert` (deterministic; only needs a cockpit WS attach)
- ❌ **T3** delete blocked (409) while live-referenced, with blocker enumeration
- ❌ **T4** fail-loud on a missing expert row (decision 6)
- ❌ **T5** flag-off regression (bundled unaffected; DB endpoints 404)
- ❌ **T6** automation expert-name → `expert_id`

Confidence the code is correct is high (fix #3 mirrors the proven `config_name`
path; the rebuilt agent image was confirmed to contain the field), but it is
**unobserved**. Treat T1 or T2 as the gate before trusting the dev rollout.

## B. Orchestrator API endpoints — 🟡 partial

Exercised live on dev k3d (via `X-Internal-Key` + `X-MCP-User-Id`):
- ✅ `POST /api/experts` (create) · ✅ `GET /api/experts` (merge + `source` tags)
- ✅ `GET /api/experts/{id}` (detail; fragment merged onto `defaults`)
- ✅ `GET /api/experts/{id}/export` · ✅ `POST /api/experts/import` (fork-on-import suffix)

**Not exercised on-cluster:**
- ❌ `PUT /api/experts/{id}` (update + `version` bump + owner/admin gate + immutability of `expert_type`)
- ❌ `POST /api/experts/{id}/duplicate` (fork a **bundled** expert, and fork a **DB** expert)
- ❌ `DELETE /api/experts/{id}` — only the *attempt* was made; the **409 blocker
  enumeration** (decision 15/26) and the happy-path delete were never cleanly run
  (the test job was stuck/cancelled). Covered by T3.
- ❌ create-time **409 on `(name, owner_id)` collision** (only the import-suffix path was seen)
- ❌ slug / `expert_type` / color **422 validation** (pydantic — only via CI, see §E)
- ❌ `GET /api/experts/{id}` for a **bundled** expert with the flag ON (DB branch vs disk branch interplay)

## C. Unit-test gaps — ❌ (repo has no live-DB fixture)

- ✅ Pure logic is unit-tested (22 green): `deep_merge` aliasing, `hard_deny_scan`
  + `canonical_key`, name precedence, `build_expert_config`, `to_export_bundle`,
  `fence_persona`, freeze overlay (all in `tests/test_expert_resolution.py`,
  `tests/test_persona_fencing.py`, `tests/test_experts_migration.py`).
- ❌ The orchestrator **DB CRUD methods** have **no automated test** — `create_expert`,
  `update_expert` (dynamic SET builder!), `list_experts_visible` (the visibility
  WHERE + `project_ids` array_agg), `expert_delete_blockers`, `delete_expert`.
  The repo has no live-DB pytest fixture; these are only exercised by the on-cluster
  API calls (§B) and T1–T6. **`update_expert`'s dynamic SQL and `list_experts_visible`'s
  precedence are the riskiest unverified SQL.**
- ❌ The pydantic model test was **dropped** — `from orchestrator.main import
  ExpertCreate` raises `Vector DB credentials missing` at import, so it can't run
  outside a full env. Slug/type/color validation relies on CI + manual API calls.

## D. project_experts linking — ⛔ NOT IMPLEMENTED (so untestable)

- The `project_experts` junction (link / `default_for` / `config_override`) is
  **created** by migration `0028` and **read** by `list_experts_visible` /
  `pick_expert_by_name` (resolution tier 2), but **no API populates it** — the
  link/unlink/set-default endpoints are deferred to **Slice 3 (Cockpit)**.
- Consequence: ❌ project-linked / project-default experts cannot be created, so
  **name-resolution tier 2 (project) is unreachable and untestable today.** Only
  tier 3 (owner) and tier 1 (global, admin-set `is_global`) are reachable.
- ⛔ `project_experts.config_override` (project-level tweak on the fragment) —
  read into dispatch metadata was a deferral lever; not wired.

## E. CI / regression suite — ❌ not run locally (CI is the gate)

- ❌ Full `pytest` on **Py3.12** (CI) — only pure-logic ran locally on Py3.14;
  heavy modules (`orchestrator.main`, `src.agent`) don't import without the full
  env. My changes touch `loader.py`, `main.py` (~21k lines), `agent.py`,
  `postgres.py`, `dual_app.py`/`app.py` — **existing tests covering those could
  regress; CI is the only gate that runs them.**
- ❌ **squawk** on migration `0028` — not installed locally; CI's `db-migrations`
  job runs it. The migration uses the two-phase `NOT VALID` → `VALIDATE` FK
  pattern specifically to pass it, but that is **unconfirmed**.
- ✅ `ruff` clean on all changed files (local). ✅ `helm lint` clean.
  ✅ `helm template` renders `EXPERTS_DB_ENABLED`.

## F. Known Slice-1 runtime limitations — ❌ unverified, documented as scope

These are *intended* Slice-1 boundaries (see the design doc Security model /
Slices), not bugs, but they are untested and worth a deliberate check:
- ❌ **Session warm-attach reuse:** a session that lands on an already-running
  idle **pool** agent (`_send_session_attach`) won't apply an expert — only
  dedicated-pod and fresh-pool-pod paths inject `AGENT_EXPERT_ID`. Dev runs a
  pool, so this path is reachable.
- ❌ **Session tools-after-init:** the lifespan applies the expert *after*
  `initialize()`; persona + model take effect, but whether a session expert that
  changes the **tool set** lands depends on persistent-graph build timing — T2
  should check this.
- ❌ **Revocation / mid-run:** N/A in Slice 1 (grants are Slice 2).

## G. Out of scope for Slice 1 — ⛔ not implemented, not tested

Not built, so explicitly untested (future slices, per the design doc):
- ⛔ **Slice 2 — capability grants + enforcement:** `capability_grants` (migration
  `0029`, reserved-by-note), the grant catalog, save-time 422, dispatch-time
  reject on the merged stack, `can_use_vm` migration, the **adversarial credential
  tests** (duplicate-key, Unicode/case-alias, cross-layer assembly, restrict-only
  scope), `model_selection` gate. None exist.
- ⛔ **Slice 3 — Cockpit UI:** experts page, type-aware editor, greyed controls,
  picker integration, **project-expert link/default UI** (the §D gap).
- ⛔ **Slice 4 — polish:** version-history surfacing, test-drive button, per-expert
  outcome stats, Gitea `experts/` scan deprecation.

---

## Minimum bar before trusting the dev-cluster rollout

The flag is ON in dev, so the cluster exercises the agent path immediately. Before
(or right after) pushing, run at least:
1. **T2 (session)** — deterministic, only needs a cockpit session open. Confirms
   `_apply_db_expert` + fencing + freeze on a real pod. *(Cheapest, highest signal.)*
2. **T1 (worker)** — once the dispatcher/workspace pipeline is healthy (see the
   "getting a worker job to dispatch" appendix in `..._verification.md`).
3. **T5 (flag-off)** — cheap regression guard that bundled experts are untouched.

CI must be green (§E) — especially the `db-migrations` squawk/dry-run job and the
full pytest on Py3.12.
