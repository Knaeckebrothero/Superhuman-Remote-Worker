# User-Defined Experts (Slice 1) — runtime acceptance verification

**Status:** Orchestrator side verified on dev k3d 2026-06-15. The **runtime
agent-application tests below (T1–T6) are NOT yet run** — the dev cluster's
worker-dispatch / session-provisioning pipeline was wedged during the original
pass (jobs stalled in `waiting`; session agent pods are provisioned lazily on
WebSocket attach). This doc captures exactly how to finish them.

**Feature:** `docs/features/global_expert_management.md` ·
**Plan:** `docs/superpowers/plans/2026-06-15-user-defined-experts-slice-1.md` ·
**Untested inventory:** `docs/tests/user_defined_experts_slice1_test_gaps.md`

## What is already verified (don't re-run)

On dev k3d (namespace `srw`), via the API: migration `0028` applied; `experts`
+ `jobs.expert_id` schema correct; `POST/GET /api/experts`; detail with the
fragment merged onto the `defaults` base; DB-aware list with `source` tags;
export→import round-trip (fork-on-import); `expert_id` plumbed through
`create_job` → `get_job` → `JobStartRequest`; session thread stores
`metadata.expert_id`. Three integration bugs were found and fixed during that
pass (orchestrator deployment env, the `'waiting'` delete-blocker status, and
the agent receive-plumbing in `src/api/models.py` + `dual_app.py` + `app.py`).

What remains is the **runtime** half: an agent actually *loading* the DB expert,
*fencing* the persona, and *freezing* it — plus deletion/fail-loud/flag-off
behavior under a live run.

---

## Prerequisites (verify BEFORE running T1–T6)

These bit us last time; check them first.

1. **Flag on the orchestrator.** The orchestrator deployment uses explicit
   `env:` (not `envFrom`), so it needs the `EXPERTS_DB_ENABLED` entry added in
   `helm/templates/orchestrator/deployment.yaml`:
   ```bash
   OPOD=$(kubectl get pods -n srw | grep srw-orchestrator | grep Running | awk '{print $1}' | head -1)
   kubectl exec -n srw $OPOD -c orchestrator -- sh -c 'echo EXPERTS_DB_ENABLED=$EXPERTS_DB_ENABLED'   # want: true
   ```
2. **Agents use the FIXED image.** The agent-receive fix lives in `src/api/*`.
   Confirm the provisioner image and that it contains the fix:
   ```bash
   kubectl logs -n srw $OPOD -c orchestrator --since=5m | grep "AgentProvisioner ready"   # note the image tag
   # The provisioner reads AGENT_IMAGE at orchestrator startup. After tilt rebuilds the
   # agent image, RESTART the orchestrator so it picks up the new tag from the configmap:
   #   kubectl rollout restart deploy/srw-orchestrator -n srw
   # Verify a provisioned agent has the fix:
   #   kubectl exec -n srw <agent-pod> -- grep -c expert_id /app/src/api/models.py   # want: >=1
   ```
3. **Migration applied:**
   ```bash
   kubectl exec -n srw srw-postgres-0 -- psql -U srw -d srw -tA -c \
     "SELECT filename,success FROM schema_migrations WHERE filename LIKE '0028%';"   # want: 0028_experts.sql|t
   ```

## Auth + tooling (how to drive the API on k3d)

The experts endpoints use `require_approved_user`, which accepts **MCP internal
header auth** (`get_current_user` path 3): `X-Internal-Key` + `X-MCP-User-Id`.
No session cookie/JWT needed. Run from *inside* the orchestrator pod (port-forward
drops; the pod has `python3`). DB is `srw`/`srw`.

```bash
OPOD=$(kubectl get pods -n srw | grep srw-orchestrator | grep Running | awk '{print $1}' | head -1)
UID=$(kubectl exec -n srw srw-postgres-0 -- psql -U srw -d srw -tA -c \
      "SELECT id FROM users WHERE email='admin@localhost' LIMIT 1;")
# Reusable in-pod caller (X-Internal-Key value is the dev default):
api() {  # api METHOD PATH [JSON_BODY]
  kubectl exec -n srw $OPOD -c orchestrator -- python3 - "$1" "$2" "${3:-}" "$UID" <<'PY'
import sys,urllib.request,json
m,p,b,uid=sys.argv[1:5]
H={'X-Internal-Key':'dev_mcp_internal_key','X-MCP-User-Id':uid,'Content-Type':'application/json'}
data=b.encode() if b else None
req=urllib.request.Request('http://localhost:8085'+p,data=data,headers=H,method=m)
try: print(urllib.request.urlopen(req,timeout=25).read().decode())
except urllib.error.HTTPError as e: print('HTTP',e.code,e.read().decode())
PY
}
```

---

## T1 — Worker job: expert applied + **fenced** persona frozen (PRIMARY)

Goal: a worker job carrying `expert_id` makes the agent merge the fragment onto
the `defaults` base, inject the persona **fenced** (decision 7), and freeze it
into `jobs.resolved_config` (decisions 6, 10, 25; Tasks 8–10).

```bash
# 1. Create a worker expert with a unique sentinel persona
api POST /api/experts '{"name":"tdd-coder","display_name":"TDD Coder","expert_type":"worker","config":{"llm":{"reasoning_level":"high"}},"prompts":{"persona":"PERSONA-SENTINEL-XYZ. Be extremely terse."}}'
#   -> note the returned "id" as EID

# 2. Run a short job with it
api POST /api/jobs '{"description":"Reply hello then job_complete.","expert_id":"<EID>","user_id":"<UID>"}'
#   -> note the returned "id" as JID

# 3. Wait until resolved_config is frozen (status reaches processing), then check the persona:
kubectl exec -n srw srw-postgres-0 -- psql -U srw -d srw -tA -c "
  SELECT 'frozen='||(resolved_config IS NOT NULL)
       ||' has_sentinel='||(resolved_config->'prompts'->>'persona' LIKE '%PERSONA-SENTINEL-XYZ%')
       ||' is_fenced='||(resolved_config->'prompts'->>'persona' LIKE '%<user_persona%')
       ||' reasoning='||(resolved_config->'agent'->'llm'->>'reasoning_level')
  FROM jobs WHERE id='<JID>';"
```

**PASS** when: `frozen=t has_sentinel=t is_fenced=t reasoning=high`, i.e. the
persona is the sentinel, wrapped in `<user_persona …>` (fenced/subordinated),
and the fragment's `reasoning_level` took effect. The agent log should show
`Applied DB expert tdd-coder (worker)`:
```bash
kubectl logs -n srw <agent-pod-for-JID> | grep "Applied DB expert"
```
**FAIL signature** (the pre-fix bug): persona head is `# Role\nGeneralist remote
worker …` (the bundled default) with `has_sentinel=f`.

---

## T2 — Session: expert applied at lifespan startup

Goal: a session bound to a session-type expert applies it deterministically at
pod startup (no dispatcher), via `AGENT_EXPERT_ID` → lifespan `_apply_db_expert`
+ `_create_phase_llms` (Task 12; `src/api/dual_app.py` / `app.py` lifespan).

```bash
# 1. Session-type expert
api POST /api/experts '{"name":"sess-helper","display_name":"Session Helper","expert_type":"session","config":{"llm":{"reasoning_level":"high"}},"prompts":{"persona":"SESSION-SENTINEL-ABC. Be warm and brief."}}'
#   -> SEID

# 2. Thread bound to it
api POST /api/persistent/threads '{"expert_id":"<SEID>","title":"expert-test","config_name":"persistent_defaults"}'
#   -> THREAD; confirm storage:
kubectl exec -n srw srw-postgres-0 -- psql -U srw -d srw -tA -c "SELECT metadata->>'expert_id' FROM threads WHERE id='<THREAD>';"

# 3. The session AGENT pod is provisioned lazily on WebSocket attach — OPEN THE SESSION
#    IN THE COCKPIT UI (that establishes the WS). Then find the pod and check:
SPOD=persistent-$(echo <THREAD> | cut -c1-12)
kubectl exec -n srw $SPOD -- sh -c 'echo AGENT_EXPERT_ID=$AGENT_EXPERT_ID'          # want: <SEID>
kubectl logs -n srw $SPOD | grep -iE "Session bound to DB expert|Applied DB expert" # want: a match
```

**PASS** when: the session pod carries `AGENT_EXPERT_ID=<SEID>` and logs
`Session bound to DB expert <SEID>`, and the agent's first system prompt /
resolved config contains the fenced `SESSION-SENTINEL-ABC`.

---

## T3 — Delete blocked while live-referenced (409)

Goal: decision 15 / 26 — refuse delete while a pending (`created`/`waiting`)
job or an active (non-`ended`) thread references the expert; enumerate blockers.

```bash
# With a still-pending job (T1's JID before it finishes) OR T2's active thread referencing EID:
api DELETE /api/experts/<EID>
```
**PASS** when: `HTTP 409` and the body's `detail.blockers` lists the job/thread
(`{type,id,label}`). After ending/cancelling the refs, `DELETE` returns
`{"deleted": true}`. (This exercises the `'waiting'` status fix — bug #2.)

---

## T4 — Fail-loud on a missing expert row (decision 6)

Goal: with the flag on, an `expert_id` that has no row must **fail the job
loudly**, not silently fall back to base config.

```bash
# Point a fresh job at a non-existent expert UUID:
api POST /api/jobs '{"description":"x","expert_id":"00000000-0000-0000-0000-000000000000","user_id":"<UID>"}'
#   -> JID; let it dispatch, then:
kubectl exec -n srw srw-postgres-0 -- psql -U srw -d srw -tA -c "SELECT status FROM jobs WHERE id='<JID>';"   # want: failed
kubectl logs -n srw <agent-pod> | grep "not found in DB"   # want: "Expert 0000… not found in DB … Failing loud"
```
**PASS** when: the job ends `failed` and the agent logged the fail-loud
`RuntimeError` from `_apply_db_expert`.

---

## T5 — Flag-off regression (feature fully dormant)

Goal: with `EXPERTS_DB_ENABLED=false`, bundled experts behave exactly as before
and the DB-expert surface is inert.

```bash
kubectl set env deploy/srw-orchestrator -n srw EXPERTS_DB_ENABLED=false
kubectl rollout status deploy/srw-orchestrator -n srw
api GET /api/experts          # want: only source=bundled entries (no user/global)
api POST /api/experts '{"name":"x","display_name":"X","expert_type":"worker"}'   # want: HTTP 404 "DB-backed experts are not enabled"
# A bundled-config_name job still runs normally:
api POST /api/jobs '{"description":"hi","config_name":"developer","user_id":"<UID>"}'   # runs as before
# Restore:
kubectl set env deploy/srw-orchestrator -n srw EXPERTS_DB_ENABLED=true && kubectl rollout status deploy/srw-orchestrator -n srw
```
**PASS** when: list has only `source=bundled`; write endpoints 404; bundled
jobs unaffected.

---

## T6 — Automation name → `expert_id` (Task 13, optional)

Goal: when an automation's `expert` *name* resolves to a DB expert, the spawned
job gets `expert_id` (decision 5/15).

Create a DB worker expert named e.g. `nightly`; create an automation with
`expert: "nightly"`; fire it (cron or run-now); then:
```bash
kubectl exec -n srw srw-postgres-0 -- psql -U srw -d srw -tA -c \
  "SELECT expert_id FROM jobs WHERE context->>'automation_name' = '<name>' ORDER BY created_at DESC LIMIT 1;"
```
**PASS** when: the spawned job's `expert_id` equals the DB expert's id (falls
through to `config_name`/bundled when no DB row matches the name).

---

## Appendix — getting a worker job to actually dispatch on this k3d

The original pass stalled here; this is why, and how to work around it.

- **`waiting` is a trap.** The auto-assign dispatcher only claims
  `status IN ('created','paused')` (`orchestrator/database/postgres.py`). A job
  that goes `created → waiting` (e.g. no agent free at claim time) and fails to
  assign is **never re-claimed** — it's orphaned. Don't restart the orchestrator
  mid-run (that orphans in-flight jobs).
- **Worker experts spawn a scholar subjob** (priority 10) that grabs an agent
  before the priority-5 main job, so each logical job needs **2 of the 3** pool
  agents (`agent.pool` max=3 on dev). Cancel stale jobs to free capacity:
  `PUT /api/jobs/{id}/cancel`.
- **To force a clean run onto a fixed-image agent:** offline any pre-fix idle
  agents and reset the job to `created` so the dispatcher re-claims it:
  ```bash
  kubectl exec -n srw srw-postgres-0 -- psql -U srw -d srw -tA -c "
    UPDATE agents SET status='offline' WHERE hostname='<pre-fix-agent>';
    UPDATE jobs   SET status='created', assigned_agent_id=NULL WHERE id='<JID>';"
  ```
- **Cleanest path may be T2 (session)** — it bypasses the worker dispatcher
  entirely; the lifespan applies the expert at pod startup. It only needs a
  cockpit WS attach to provision the pod.

## References
- Bugs found during the 2026-06-15 pass (now fixed): orchestrator deployment
  `EXPERTS_DB_ENABLED` env; delete-blocker `'queued'`→`'waiting'`; agent receive
  plumbing (`JobStartRequest.expert_id` in `src/api/models.py`,
  `_process_orchestrator_job` in `dual_app.py`/`app.py`).
- Local-k3d testing conventions: see the project memory on driving the
  orchestrator API via `X-Internal-Key` + in-pod `urllib`.
