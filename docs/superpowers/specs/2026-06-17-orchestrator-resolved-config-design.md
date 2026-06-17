# Orchestrator-Resolved Config — Design Spec

**Status:** Approved design (2026-06-17), pending implementation plan.
**Supersedes:** `docs/features/global_expert_management.md` **Decision 6**
(agent-side resolution). That supersession must be written into the vault as part
of implementation — this doc is the new decision of record.

**Goal:** Move *all* agent config resolution (bundled base + expert + override
layers) into the orchestrator. The orchestrator emits a single frozen, fully
resolved config blob; the agent becomes a pure executor that **hydrates** it and
no longer reads `experts`/`config_overrides` from the DB.

---

## Why (the decision, recap)

Resolution today is smeared across the agent (loads experts + 0022 overrides +
merges layers + settings-matrix) and the orchestrator (injects credentials at
dispatch). Consequences this bit us on:

- The **warm-pool session attach** path has no `expert_id` channel → a DB-expert
  session can't deliver the expert to an already-running pool agent → crash / a
  fixed ~3-minute stall (the live bug).
- `config_name` ≠ `config_override`: `config_name` is full-profile resolution
  (`$extends` + settings-matrix + deployment-dir prompts); `config_override` is a
  flat overlay. Delivering an expert as a flat override silently degrades it.

Moving resolution to the orchestrator gives us, by construction:

1. **Enforcement co-location** (Decision 9): the dispatch-time capability check
   runs on the *exact* config handed to the runner — no resolve-after-enforce gap.
2. **Multi-runtime uniformity**: pods, VMs, future runtimes consume one blob; none
   needs DB access or the resolver.
3. **Control/execution separation**: smaller agent blast radius (no broad app-DB
   read for config); one place answers "why did this run get this config."
4. Kills the "loading here and there" smell.

---

## The contract

**One shared resolver in the orchestrator**, used by *both* job dispatch and
session attach (identical resolution; only timing/delivery differ):

```
resolve_config(base_config_name, expert_id, project_ids, user_id,
               request_overrides, *, for_runtime) -> resolved_config: dict
```

- **Reuses `src/core/loader`** (the orchestrator already imports it —
  `main.py:16801/16861/21092`): `resolve_config_path` → `load_and_merge_config`
  (bundled base + `$extends`) → `deep_merge` the layers in order →
  `_apply_settings_matrix` → `serialize_resolved_config`-shaped dict. **No new
  resolver, no duplicated logic.**
- **Layer order (preserve the documented pipeline, `global_expert_management.md`
  lines 246-260):**
  `bundled base → expert fragment → project_experts.config_override → DB
  config_overrides (0022) → user persistent_agent settings (base of the session
  override) → job/thread request config_override (most-specific wins)`. (Corrects
  this session's earlier error of putting the expert at the *top* layer; and note
  user `persistent_agent` settings sit *below* explicit request overrides,
  `global_expert_management.md:92`.)
- **Output shape** = the existing `serialize_resolved_config` dict (so the agent's
  existing `load_agent_config_from_dict` consumes it unchanged), plus the fenced
  persona/instructions handling (`_resolved_prompts` + `_persona_source="db"` for
  DB experts — decision 7 fencing preserved).

## Delivery + agent consumption

| Path | Resolve when | Store / deliver | Mutability |
|------|--------------|-----------------|------------|
| **Job** | at dispatch | freeze into `jobs.resolved_config` (exists) + carry in `JobStartRequest` | immutable run |
| **Session** | at **attach** (warm *and* cold) | deliver in the attach payload; persist to a thread-side `resolved_config` for re-attach | mutable — re-resolve on (re)attach / config change, not a one-time freeze |

**Agent hydrate (the migration seam — this single branch is the flag):**
```
resolved_config present?  → load_agent_config_from_dict(resolved_config)   # orchestrator-resolved
              absent?      → from_config(config_name)                       # today's path (fallback)
```
Session attach delivering the resolved blob to a running pool agent is exactly the
missing warm-pool channel → the 3-minute stall dies.

## Credentials invariant (unchanged)

The resolved blob the agent receives **includes** injected credentials
(`llm.api_key`, `base_url`, `env_keys`) so it can run — but any **persisted** copy
(`jobs.resolved_config`, thread `resolved_config`) is **stripped of plaintext
secrets**, exactly like today's `redact_config_override`. Credential injection
happens *within/after* resolution so creds match the resolved model.

## Live config changes (future) — natively supported, and a reason *for* this design

A planned feature is changing config on **running** agents — live in persistent
sessions, and mid-run for jobs (agent reports it's blocked needing tool/grant X →
user grants → run resumes with the new config). Orchestrator-resolution suits this
*better* than agent-side, not worse:

- **A live change is just re-resolve + re-deliver.** Same `resolve_config()`,
  triggered again, pushed as a full blob down the agent's existing WS/control
  channel; the agent **hydrates** it. One mechanism for initial *and* live config —
  no delta-merge logic on the agent. (Evolves the existing
  `persistent_app._handle_config_update` from "merge a delta" to "hydrate a blob".)
- **Centralized = top-down conflict control.** Live config has multiple writers
  (user UI, automations, grant approvals, project-default edits). A single
  orchestrator resolver serializes them into one source of truth; N agents
  resolving locally would diverge/race. This is the decisive reason.
- **Mid-run grants *require* it.** Granting a capability is Decision 9 territory —
  the agent must not resolve its own capabilities. Flow: agent requests →
  orchestrator enforces + re-resolves → pushes blob → agent applies + resumes.
- **It unifies further:** resolution happens at *lifecycle events* — dispatch,
  attach, **resume**, **config-change** — always orchestrator, always full-blob
  re-delivery, always agent-hydrate. A job's "freeze" becomes "stable within a run
  segment, re-resolved on resume", mirroring session re-resolve-on-attach.

**v1 constraint to preserve this:** the agent hydrate path must be **re-runnable at
a turn boundary** (idempotent/re-entrant), not boot/attach-only. `_handle_config_update`
already live-rebuilds the LLM/tools, so the capability exists — keep it. Live
re-resolution is then a later extension, not a rearchitecture.

## Capability enforcement (Decision 9) — seam in v1, grants later

The resolver exposes the merged config at the point where dispatch-time
enforcement belongs; v1 places a **pass-through hook** there (resolution +
enforcement co-located by construction). `capability_grants` / save-time + dispatch
reject (Slice 2) land later against that hook — not in v1.

---

## v1 scope — jobs **and** sessions, unified

Build the shared resolver + both call sites at once (per the "share as much as
possible" call). Concretely, restore in the *orchestrator-resolves* shape (not the
old agent-side shape):

- `list_experts` **DB-merge** + detail (picker surfaces DB experts again).
- Cockpit sends **`expert_id`** (session-create *and* job-create), not
  `config_name` (Decision 5 — fixes the upstream conflation).
- `expert_id` propagation: `threads.metadata.expert_id` + `jobs.expert_id`
  (columns already exist from `0028`).
- The shared `resolve_config()` + delivery + agent hydrate, for both paths.
- Flag-gated, with the `from_config` fallback on.

**Stays removed (we are retiring it):** `_apply_db_expert`, `AGENT_EXPERT_ID` —
the agent no longer resolves experts.

**Deferred (explicit):** expert **CRUD UI** (fast-follow; the 3 existing DB rows
are the test fixtures), `capability_grants`/S2 enforcement, import/export, any
0022-override consolidation beyond what resolution needs.

## Migration / rollout

- One flag (reuse `EXPERTS_DB_ENABLED` or a new `ORCHESTRATOR_RESOLVED_CONFIG` —
  decided in the plan) gates whether the orchestrator emits `resolved_config` and
  whether the agent prefers it. Flag off / blob absent → agent `from_config`
  (today's behavior; safe rollback).
- Land both paths behind the flag → verify on k3d → the agent-side resolver path
  is then dead code → delete in a follow-up.

## Reconciliation with current (post-`6f8c635e`) state

- **Do not revert `6f8c635e`** — it cleared the agent-side resolver we're
  retiring. Build forward; restore only the data/propagation/picker bits in the
  new shape.
- **Keep `abe3a90b`** (config_name UUID guards) — harmless defense-in-depth once
  the cockpit sends `expert_id`.
- **Keep the 3 DB test experts** as fixtures.
- **Update `global_expert_management.md`**: mark Decision 6 superseded, link here.

## Open items to settle in the implementation plan

1. Session `resolved_config` storage: a `threads.resolved_config` column vs
   `metadata.resolved_config` (migration vs JSONB key).
2. Resolver home + exact signature: `orchestrator/services/config_resolver.py`?
3. Call `load_agent_config` directly orchestrator-side vs compose the
   `load_and_merge_config`/`deep_merge`/`_apply_settings_matrix` steps (the former
   resolves from a file path; we need to inject DB layers mid-pipeline).
4. Flag name + default (dev on / prod off).
5. Exact attach-payload field carrying the blob (extend the existing
   `_attach_session` / `workspace_override` channel vs a new field).
