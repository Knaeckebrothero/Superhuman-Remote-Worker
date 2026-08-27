# Live validation — loop born-parked spawn on model-cooldown fail-fast

**Type:** deployed end-to-end validation (k3d/Tilt). Not a pytest file; it needs
the local `srw` k3d cluster, Tilt, and a synthetic cooldown stub. Update this
runbook in place as checks are run.

**Status (2026-07-27): PENDING — never run.** The implementation (24 unit tests
across 6 suites) is green on develop (uncommitted); this live gate is the only
owed verification. At filing time the gate was blocked by a HOST issue, not the
change: all container-to-container traffic on the docker bridge answered
`Host is unreachable` (firewalld clobbered docker's forward rules), so the k3d
API was unreachable from the host and registry pulls would fail.
**Fix first: `sudo systemctl restart docker`**, then `k3d cluster start srw`
if needed, and confirm `kubectl --context k3d-srw get nodes` shows Ready.

**Source documents:**

- `knowledge-base/knowledge/issues/loop_advances_into_active_model_cooldown.md` — the incident,
  design (Option A), and implementation status block.
- `knowledge-base/knowledge/features/llm_cooldown_pause_and_resume.md` — the outage park/sweep
  machinery this rides, incl. the prior k3d harness notes (synthetic
  `model_cooldown` stub recipe).

## What this validates (and why live)

When a loop member fails on a quota cooldown longer than the 12h pause budget,
the barrier winner now creates the NEXT member **born-parked**: one atomic
INSERT with `status='paused'` + `freeze_data{freeze_type:'llm_unavailable',
next_retry_at:<reset>}` + `context.llm_outage{attempt:0, next_retry_at}` —
deliberately **without `first_failed_at`** (with it, `evaluate_llm_outage`
would read the whole park as elapsed outage and ceiling-kill the member at its
wake instant). The existing llm_outage sweeper wakes it at the reset.

Unit tests already prove the control flow against mocks. Live-only evidence
this gate adds:

- the `::timestamptz` casts accept the ISO strings we write (due-gate
  `list_due_llm_outage_jobs`, claim CAS) on a real Postgres;
- the born-parked row really is invisible to the real dispatcher poll
  (`idx_jobs_dispatchable` partial-index predicate, not a mock);
- the real 30s sweeper claims it and the real dispatch loop assigns it;
- `jobs.error_details` (previously dormant column) round-trips through
  `/complete` → row → heal-path extractor;
- the owner notification row lands in `message_log`.

## Harness setup

1. **Stack up:** `tilt up` on the `srw` k3d cluster (values-local; neo4j off,
   orchestrator x1). The change is in the working tree — verify Tilt actually
   rebuilt `orchestrator` + `agent` images (touch-check a changed file in the
   built image; the local Dockerfile cache has masked stale code before):
   `kubectl exec <orchestrator-pod> -- grep -c park_until /app/orchestrator/main.py`
   → must be ≥ 15.
2. **Cooldown stub:** in-cluster HTTP server answering every POST with
   `429 {"error":{"code":"model_cooldown","message":"All credentials for model
   stub-cool are cooling down via provider codex","model":"stub-cool",
   "provider":"codex","reset_seconds":172800,"reset_time":"48h0m0s"}}`
   (48h > the 12h pause budget → agent fail-fasts). Same recipe as the
   2026-07-16 cooldown-pause gate: a throwaway pod on the orchestrator image
   running a small `http.server` script; see the harness notes in
   `knowledge-base/knowledge/features/llm_cooldown_pause_and_resume.md`.
3. **Route a model at the stub:** seed an OpenAI-compatible catalog entry
   (e.g. name `stub-cool`, base_url = the stub service URL, any api_key) via
   the Admin models UI or the seed path — zero code needed. Alternative:
   project `default_config_override` with
   `llm: {strategic: {model, base_url}, tactical: {model, base_url}}`.
4. **Drive the API** with `X-Internal-Key: dev_mcp_internal_key` and the
   seeded k3d admin user id in the request BODY (grants bypass); create a
   project, then a loop: `model=stub-cool`,
   `role_sequence=["scholar","critic"]`, `max_iterations=4`.
5. `PG` shorthand below = `kubectl exec -it <srw-postgres-pod> -- psql -U srw -d srw`.

## P0 — release gate (all must pass before commit/push of the feature)

- [ ] **1. Fail-fast with structured row.** Member 1 (scholar) dispatches,
  hits the stub, and fails within ~2 min. Assert:
  `SELECT status, error_message, error_details FROM jobs WHERE id='<m1>';`
  → `failed`; message contains "quota cooldown"; `error_details` JSONB has
  `classification='cooldown'`, `model='stub-cool'`, `reset_at` ≈ epoch(now+48h).
- [ ] **2. Born-parked member 2.** The advance spawns the critic parked:
  `SELECT status, assigned_agent_id, freeze_data, context->'llm_outage' FROM
  jobs WHERE id='<m2>';` → `paused`, agent NULL,
  `freeze_data->>'freeze_type'='llm_unavailable'`,
  `freeze_data->>'origin'='loop_cooldown_park'`,
  `freeze_data->>'next_retry_at'` ≈ now+48h, `attempt=0`, and
  `context->'llm_outage'` = `{"attempt":0,"next_retry_at":...}` with
  **no `first_failed_at` key** (the ceiling landmine).
- [ ] **3. Loop state + notification.** Loop row: `status='running'`,
  `current_stage_jobs=[<m2>]`, `consecutive_failures=1`, no stop_reason.
  `message_log` has an outbound row, `thread_id='loop-<6hex>'`, subject
  "Loop waiting for model cooldown".
- [ ] **4. Dispatcher blindness.** Over ≥2 dispatch cycles (~1 min),
  member 2's `assigned_agent_id` stays NULL and its status stays `paused`
  (freeze non-NULL keeps it outside `idx_jobs_dispatchable`).
- [ ] **5. Wake → claim → dispatch.** Rewind both timers:
  `UPDATE jobs SET freeze_data = jsonb_set(freeze_data,'{next_retry_at}',
  to_jsonb(((now()-interval '1 minute')::timestamptz)::text)), context =
  jsonb_set(context,'{llm_outage,next_retry_at}',
  to_jsonb(((now()-interval '1 minute')::timestamptz)::text))
  WHERE id='<m2>';`
  Within ~30s the leader orchestrator logs the llm-outage sweeper
  re-dispatch, `freeze_data` goes NULL, and member 2 is assigned/processing.
  **Either** terminal outcome then passes the mechanics check:
  (a) stub flipped healthy / model swapped to a real one → member 2 runs and
  the loop rotates on its completion; or (b) stub still cooling → member 2
  fail-fasts again AND the advance born-parks member 3 (the re-arm proof —
  check member 3's row as in step 2).
- [ ] **6. Negative control.** Non-cooldown failure (e.g. kill member N's
  agent pod mid-run, or point at a 500-ing stub): next member is born
  `status='created'`, `freeze_data` NULL — byte-identical legacy behavior.

## P1 — nice-to-have hardening evidence

- [ ] Campaign-mode loop (planner) member cooldown → next campaign member
  born-parked (threading through `_spawn_campaign_member`).
- [ ] Orchestrator restart while a member is parked → sweeper on the new
  leader still claims it at the (rewound) reset; no reaper touches it while
  parked (`recover_orphaned_jobs`, stale-verification, loop sweeper).
- [ ] Cockpit renders the parked member as paused with the freeze summary
  ("Created parked: model 'stub-cool' in quota cooldown until …").

## Cleanup

Delete the loop + its jobs, the stub pod/service, and the `stub-cool` catalog
entry (or project override). Record results below and mirror the outcome into
the issue doc's status block; when P0 is fully green, the feature is clear to
commit/push and the issue doc moves toward `knowledge-history/done/`.

## Results log

| Date | Runner | Stack (tilt SHA / images) | Outcome | Notes |
|---|---|---|---|---|
| — | — | — | not yet run | blocked on host docker-bridge firewall at filing time |
