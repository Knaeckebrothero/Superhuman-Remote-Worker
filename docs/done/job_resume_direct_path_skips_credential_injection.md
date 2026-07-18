---
tags:
  - issue
  - orchestrator
  - dispatcher
  - credentials
  - regression
---

# Cockpit Resume's direct fast-path ships the bare persisted `config_override` — no `_inject_dispatch_credentials` — killing jobs that land on fresh agent pods

**Status:** FIXED 2026-07-18 — the endpoint's direct fast-path now delegates to
`_resume_job_on_agent` (see "Fix shipped" below). Diagnosed the same day (root
cause + all victims traced).
**Severity:** high — the user-facing Resume button is a job-killer whenever a
fresh (clean-env) agent pod picks the job up; killed at least 5 jobs
2026-07-17.
**Component:** `orchestrator/main.py` `POST /api/jobs/{job_id}/resume`
(~9977), direct fast-path ~10132-10193.
**Same class (already fixed elsewhere):**
`docs/done/orchestrator_phase_override_credentials_not_injected.md`,
`docs/done/phase_pin_endpoint_credentials_not_injected.md`.

## Symptom shapes (all one root cause)

1. **Reranker bind failure** — job fails seconds after resume with
   `reranker needs a base_url: set memory.reranker.base_url or
   EMBEDDING_BASE_URL (the reranker rides the embedding endpoint)`
   (raised in `RerankerScorer.__init__`,
   `src/services/memory/plugins/reranker.py:93`, at `create_graph` bind
   time). Victims 2026-07-17: `612abf13` ("med v1", 09:05Z), `b988e3f0`
   ("netzteil", 09:07Z — 1336 audit entries lost), `db3169da` (scholar
   "Research phase for: stab", 09:35Z).
2. **Workspace backend rejection** — `workspace.backend='sandbox' but no
   workspace.remote config was provided. The orchestrator must inject SSH
   credentials pointing at a provisioned workspace container or VM.`
   Victim: `bffa0dac` (scholar, "Research phase for: Meridian V1",
   re-failed 2026-07-17 12:25Z on a cockpit resume).
3. **Workspace SSH auth failure** — `workspace unavailable; recovery
   exhausted after 3 attempts: Failed to connect to workspace
   workspace-….svc.cluster.local:30022 … No authentication methods
   available`. Victim: `19b3f59a` (scholar, "Research phase for: meridian
   v2", re-failed 2026-07-17 12:28Z on a cockpit resume). Here the pod DNS
   name was still derivable (from `context.workspace_container`) but no SSH
   password/key arrived — consistent with the same missing injection; the
   exact seam (host-from-context, creds-from-injection) is unverified.

DB evidence for shapes 2/3: both jobs' persisted `config_override` key-sets
(`[curator, scholar, autonomy, verification]` / plus `llm`) contain **no
`env_keys`, no `workspace.remote`, no API keys** — exactly what the
creation-time override looks like before dispatch-time injection. Both
`updated_at` timestamps sit in the 12:25–12:28Z window when the user was
bulk-resuming old failed jobs from the cockpit.

## Root cause

`POST /api/jobs/{job_id}/resume` has two branches:

- **No ready agent** → `_queue_for_dispatch` → job goes `paused` → the
  dispatcher's `_resume_job_on_agent` (`main.py:2535`) calls
  `_inject_dispatch_credentials` — safe.
- **Ready agent available** → **direct fast-path** (~10132-10193) POSTs the
  **bare persisted `jobs.config_override`** straight to the agent's
  `/job/resume` — creation-time content, no env keys, no LLM api_key, no
  workspace credentials. The internal helpers
  (`_internal_resume_job`, `_resume_job_without_vm_internal`) all use the
  safe queue pattern; only this user-facing fast-path skips injection.

**Why it only kills sometimes:** warm-pool agents keep `os.environ` from
earlier credentialed jobs, masking the hole. It kills when the resume lands
on a **fresh agent pod** (clean env) — all three 07-17 reranker victims'
agents had registered 1–3 minutes before the resume.

Agent-side failure shape for the reranker victims (audit
`setup_job_tools`): `memory_unavailable` + `kb_unavailable` with
`embedding_model:"unknown", embedding_provider:"local"`, then the reranker
ValueError fails the job terminally (generic `job_error`, so the
memory-unavailable pause/retry net never applies).

## Fix shipped (2026-07-18)

**One payload builder.** The endpoint's fast-path no longer hand-builds a
resume payload: after agent selection (and the endpoint-unique S3 snapshot
restore), it delegates to `_resume_job_on_agent(job, agent)` — the
dispatcher's resume path — so a user-triggered resume ships exactly what an
auto re-dispatch ships: `_inject_dispatch_credentials` (llm api_key/base_url
+ `env_keys` incl. `EMBEDDING_*`, `include_kb_profile` from knowledge scope),
VM workspace config, container-host override, sticky sudo denial, lite
mounts, and queued feedback/delegation results. If the agent declines, the
endpoint falls back to `_queue_for_dispatch` (unchanged).

Details:
- `request.feedback` now travels via `context.queued_feedback` (merged into
  the DB row AND stamped onto the in-memory job before delegation; the shared
  path clears it only after the agent accepts, so a rejected resume keeps it).
- Credential-only injection was rejected as insufficient: it would not have
  saved the two workspace-shape victims. "Always queue" was rejected because
  it would relocate the snapshot restore into the dispatcher hot path and
  drop the synchronous resume confirmation.
- Parity additions to `_resume_job_on_agent` while unifying: it now sends
  `config_upload_id` from context (previously endpoint-only — the dispatcher
  path silently dropped it for uploaded-config jobs), and the 409
  stale-'ready' agent demotion moved into it (previously endpoint-only — the
  dispatcher could re-pick the same zombie agent).
- The `snapshot_restored` payload flag was dropped — zero consumers in
  `src/`; the restore side effect itself stays in the endpoint.
- Bonus: the endpoint previously called `resolve_datasources_for_job`
  without `project_id`; the shared path passes it.

Regression tests: `tests/test_resume_endpoint_delegation.py` (13 tests —
injected credentials reach the POSTed payload, `include_kb_profile`
derivation, `config_upload_id` passthrough, queued-feedback
deliver-then-clear / keep-on-reject, 409 demotion, endpoint delegation +
queue fallback + feedback stamping).

Note: the LLM model registry was never the problem (qwen3-embedding-8b row
enabled, untouched since 05-03).

## Interaction that makes this worse

The critic-feedback wedge
(`critic_feedback_resume_parent_freeze_data_wedge.md`) leaves review-loop
parents stuck in `paused`; the cockpit Resume button is the natural user
response — i.e. the wedge funnels traffic straight into this bug
(`b988e3f0` died exactly that way).

## Related

- Memory/topic: `project_cockpit_resume_skips_credential_injection`
  (evidence trail: `srw-auditdb-0` `agent_audit`, app-DB
  `jobs.config_override`, `agents.registered_at`).
- `docs/issues/reranker_transient_fault_hard_fails_job.md` — why a missing
  reranker config is job-fatal at all (configured ⇒ required).
