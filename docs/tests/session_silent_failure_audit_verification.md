# Session Silent-Failure Audit — Verification Runbook

Verifies the 11 fixes + 1 stopgap implemented 2026-06-12 for
`docs/issues/session_silent_failure_audit.md` (the "messages swallowed, UI says
Connected" investigation, threads `1f39a5a6` / `b60166ee` on dev). Numbering
matches the audit doc. All fixes are unit-verified (pytest 6264 ✅, vitest 500 ✅);
this runbook is the **live-cluster** verification that was still outstanding.

Issues **not** covered here: #5/#6/#7 + full #4 (summarization rework track,
separate design doc) and #15 (needs a repro first — hunt steps in §15).

## Setup

Works on local k3d (preferred — full inner loop) or dev. Set once:

```bash
# k3d:
CTX=k3d-srw; NS=srw
# dev:
CTX=main; NS=superhuman-remote-worker

K="kubectl --context=$CTX -n $NS"
PSQL="$K exec srw-postgres-0 -- psql -U srw -d srw -c"
```

- k3d: `k3d cluster start srw` + `tilt up`. Agent-image changes need the ~50 s
  rebuild loop; watch Tilt UI for green before testing.
- dev: fixes arrive via develop push → CI → Fleet (~30 min). Confirm the agent
  pod runs the new build: `$K get pods -l srw/component=agent --show-labels | grep build-sha`.
- Most tests need a **supervised session with a workspace** (Sessions → New
  Session). Capture its thread id as `TID` (from the URL `/sessions/<TID>`).
- A **long-running turn** is the recurring test fixture. Reliable recipe: ask
  the agent — *"Run `sleep 90` with your shell tool, then tell me the time."*
  Approve the command; the turn now sits quiet for ~90 s.

## Unit gate (run first, ~7 min)

```bash
pytest tests/ -q --tb=short
cd cockpit && npx vitest run
ruff check src/ orchestrator/ tests/
```

Expected: cockpit 500 ✅; pytest green **except** up to 7 pre-existing failures
that belong to the uncommitted memory-overhaul working tree, NOT these fixes
(6× `test_lite_workspace_dispatch` — the WIP `main.py` removes
`_is_lite_config_override` from commit `41921f53` — and 1× order-dependent
`test_database_phase1::test_connect_disconnect`). If those are gone, the WIP
was resolved; anything else failing is a regression.

---

## #1 — Mid-turn user inputs are persisted at accept time

Fixed by `_accept_user_input` (`src/api/persistent_app.py`, REST + WS input
paths) + dict-shaped queue items consumed in `src/persistent_graph.py`.

**A. Reload survival (the student's literal symptom).**
1. Start the long-turn fixture. While the agent is mid-`sleep`, send two
   messages: `ping one`, `ping two`. They render locally.
2. Reload the page (F5).
- **Pass:** both messages still render after reload (pre-fix: vanished).
- Evidence — rows exist with accept-time timestamps, *before* the turn ended:
  ```bash
  $PSQL "SELECT seq, role, left(content,30), turn_number, created_at
         FROM thread_messages WHERE thread_id='$TID'
         ORDER BY seq DESC LIMIT 6;"
  ```
- The `POST /input` response now carries `message_id` (DevTools → Network).

**B. Pod-death survival.**
1. Repeat step A1, then kill the agent mid-turn:
   `$K delete pod $($K get pods -l "srw.io/thread-id=$TID,srw/component=agent" -o name)`
2. Let the tab auto-resume (or reload).
- **Pass:** `ping one` / `ping two` still in the transcript (pre-fix: died with
  the pod's in-memory queue). The agent answering them is NOT required — input
  *durability* is what was fixed; queued-delivery-across-restart is future work.

**C. No duplicates after consume.**
After the turn finishes and the loop consumes the queued messages:
```bash
$PSQL "SELECT content, count(*) FROM thread_messages
       WHERE thread_id='$TID' AND role IN ('user','human')
       GROUP BY content HAVING count(*) > 1;"
```
- **Pass:** zero rows (the loop upserted onto the accept-time row ids), and the
  consumed rows' `turn_number` now matches their real turn.

## #2 — Turn errors are visible and durable

Fixed by `turn.error` broadcast + `role='error'` row (`_loop_on_error`),
cockpit rendering (`persistent-chat.service.ts` dispatcher + `historyToTurns`).

**Trigger** (doubles as #3's trigger): create a session on a **small-context
model** (e.g. a 128k model from the picker), put a few very large PDFs in its
cloud/project folder, and ask: *"Read all PDFs in the cloud folder completely,
then compare them."* On a 128k model the turn dies on context overflow.

- **Pass, live:** the assistant bubble's spinner STOPS and a muted
  `⚠ The conversation no longer fits the model's context window …` line
  appears (pre-fix: spinner forever, nothing rendered).
- **Pass, durable:** reload → the ⚠ line is still there.
- **Pass, no self-poisoning:** send *"hello?"* afterwards — the agent must
  respond normally (the error row is excluded from its LLM context restore).
- Evidence:
  ```bash
  $PSQL "SELECT role, left(content,70) FROM thread_messages
         WHERE thread_id='$TID' AND role='error';"
  $PSQL "SELECT kind FROM thread_events WHERE thread_id='$TID'
         AND kind IN ('error','turn.error') ORDER BY id DESC LIMIT 4;"
  ```

## #3 — Context overflow fails fast (no retry storm, no fallback misdiagnosis)

Fixed by synthetic HTTP 413 in `src/llm/reasoning_chat.py` (both capture
clients) + typed handling in `src/persistent_graph.py`.

Use #2's trigger, then read the agent log:

```bash
AGENT_POD=$($K get pods -l "srw.io/thread-id=$TID,srw/component=agent" -o name | head -1)
$K logs $AGENT_POD -c agent --tail=300 | grep -E "Context overflow|Retrying request|Streaming not supported|Error in turn"
```

- **Pass:** `Context overflow at HTTP layer` appears **once per attempt** with
  NO `Retrying request to /chat/completions` after it and NO
  `Streaming not supported (APIConnectionError)` line. Pre-fix signature: ~6
  retry lines + the misleading fallback + `APIConnectionError: Connection error.`
- The transcript error message must name the token counts (proves the typed
  413 path, not a generic exception).

## #4 stopgap — Failed summarization keeps history

Fixed in `src/core/context.py` (`_single_pass_summarize` → `None`,
`summarize_and_compact` aborts instead of placeholder-compacting).

**Trigger:** break the auxiliary model, then force compaction.
1. Admin → Models: edit the aux model's endpoint base URL to a dead address
   (e.g. `http://127.0.0.1:9`), or stop whatever serves it. (Restore after!)
2. Create a session (it captures the broken aux), chat a few turns so there is
   history, then type the `/compact` slash command.
- **Pass:** agent log shows
  `Compaction aborted: summarization unavailable (aux LLM failure) — keeping N messages uncompacted`;
  NO new `summary` row; and the agent still remembers turn-1 facts (ask it).
- **Fail (old behavior):** a `summary` row containing literally
  `[Summarization failed: …]` and the agent amnesiac about earlier turns.
  ```bash
  $PSQL "SELECT left(content,60) FROM thread_messages
         WHERE thread_id='$TID' AND role='summary' ORDER BY seq DESC LIMIT 3;"
  ```

## #8 — "Working — no output for Ns" badge

Fixed by `agentSilenceSeconds` signal + status-bar badge
(`persistent-chat.service.ts`, `persistent-chat.component.ts`).

1. Run the long-turn fixture (`sleep 90`).
2. Watch the status bar (model/temp/turn badges row).
- **Pass:** ≥30 s into the silence a warning badge appears:
  `Working — no output for 35s`, counting up in ~5 s steps; it disappears as
  soon as the tool result lands (or the turn ends). It must NOT appear while
  the session is idle (no open turn).

## #9 — Control-WS keepalive + watchdog reconnect

Fixed by `ws.ping` every 20 s in the agent's subscriber pump
(`_run_subscriber_pump`) + cockpit watchdog (45 s) with fresh-token reconnect.

**A. Pings flow.** DevTools → Network → the `/p/<TID>/ws` connection →
Messages tab. Leave the session idle.
- **Pass:** `{"method":"ws.ping","params":{}}` arrives every ~20 s.

**B. Half-open detection.** Freeze the agent process so the socket stays open
but goes silent (SSE stays green — its pings come from the orchestrator, which
isolates exactly the WS watchdog):
```bash
$K exec $AGENT_POD -c agent -- kill -STOP 1
# wait ~50-60 s, watching the browser console, then:
$K exec $AGENT_POD -c agent -- kill -CONT 1
```
- **Pass:** within ~45–60 s the console logs
  `[persistent-chat] control WS silent past watchdog — forcing reconnect`,
  a fresh `GET /api/sessions/<TID>/connection` fires (Network tab), and a new
  WS connects (agent log: new `WebSocket /p/… [accepted]` with a newer `iat`
  in the token) once the process is CONTed.
- Caveat: if the freeze exceeds the pod's liveness window the pod restarts —
  that's fine, the session resumes; just note it skipped the half-open case.
  Keep the STOP window ≤60 s for the clean variant.

## #10 — No stale permission cards / graceful 409

Fixed by `permission.resolved` broadcast (`_loop_permission_check`) + cockpit
`permission.resolved` handling and approve-409 path.

1. In a supervised session, trigger a shell command → approval card appears →
   **approve** it.
2. Reload the page.
- **Pass:** the card does NOT come back as pending/actionable (pre-fix: SSE
  replay resurrected it; clicking it 409'd into an error).
- Evidence: `$PSQL "SELECT kind FROM thread_events WHERE thread_id='$TID' AND kind='permission.resolved';"`

**Two-tab 409 path:** open the session in two tabs, trigger an approval,
approve in tab A, then click approve in tab B (if its card is still up).
- **Pass:** tab B shows the muted line
  `This permission request was already decided.` — no red toast, no duplicate
  WS fallback send.

## #11 — Mid-turn guard on ending a session

Fixed by `end_thread` 409 + `?force=true` (`orchestrator/main.py`,
`_thread_turn_in_flight` probing the agent's `/session/status`), confirm
dialogs in `persistent-chat.service.ts` `endSession()` and
`sessions-page.component.ts` `deleteSession()`.

**A. UI path.** Start the long-turn fixture. While mid-turn, go to the
Sessions list and delete that session (or use the chat page's end/disconnect).
- **Pass:** a confirm dialog warns the agent is mid-turn. **Cancel** → session
  keeps running, turn completes normally. Repeat and **confirm** → teardown
  proceeds (force path).

**B. API contract** (replace `$TOKEN` with a bearer for the owner/admin):
```bash
ORCH="$K exec deploy/srw-orchestrator -c orchestrator -- curl -s -o /dev/null -w '%{http_code}' -H \"Authorization: Bearer $TOKEN\""
# mid-turn:
$ORCH -X DELETE http://localhost:8085/api/persistent/threads/$TID        # → 409
$ORCH -X DELETE "http://localhost:8085/api/persistent/threads/$TID?force=true"  # → 200
```
- **Pass:** 409 mid-turn, 200 with force, 200 when idle, and — fail-open —
  200 without force when the agent pod is already gone (delete the pod first;
  ending dead sessions must never be blocked).

## #12 — Warm-pool thrash gone

Fixed in `agent_provisioner.scale_down_idle` (idle pods within `AGENT_BUFFER`
are not "excess"). Regression unit test:
`pytest tests/test_agent_provisioner.py -k buffer -q`.

Live check (dev runs `buffer=1`, which is what thrashed; on k3d set
`AGENT_BUFFER=1` via values if not already):
```bash
$K logs deploy/srw-orchestrator --since=30m | grep -E "Warm pool: created|Scale-down: terminated"
```
- **Pass:** after one initial create, **no create/delete alternation** — the
  idle pod persists ≥30 min. Pre-fix signature: `Warm pool: created 1 …` every
  odd minute + `Scale-down: terminated 1 …` every even minute, for hours.
- Bonus: scale-down still works — with 0 busy agents and >min+buffer pods,
  excess idle pods above the buffer DO get terminated.

## #13 — Suspend teardown fires once

Fixed by already-suspended → success in
`workspace_suspension.suspend_thread_workspace` + `_threads_suspending`
in-flight guard in `orchestrator/main.py`.

Deterministic-ish trigger: create a session, close the tab, and wait for the
idle/drain suspend (or end it via API the moment the suspend starts — the
12:44:53 incident was the watchdog racing the agent's own `status→ended` PUT).
Then:
```bash
$K logs deploy/srw-orchestrator --since=1h | grep -B2 -A4 "Workspace suspended to S3 for thread"
```
- **Pass:** per suspend: ONE `Workspace suspended to S3`, ONE
  `Agent pod deleted: <pod>`, and **never** a
  `Workspace suspend unavailable or failed … deleting the agent pod` WARNING
  *after* a success line for the same thread. A
  `already suspended/suspending — skipping duplicate` INFO line is the new
  guard firing — that's a pass, not a failure.
- Because the race is timing-dependent, also leave this as a soak assertion:
  grep dev logs for the contradictory pattern after a few days of normal use.

## #14 — Persistent sessions write llm_requests audit

Fixed by `archive_llm_call` callback (`persistent_graph._execute_turn` →
`persistent_app._loop_archive_llm_call`). Needs MongoDB configured (it is on
k3d + dev).

Run any turn in any session, then:
```bash
$K exec srw-mongodb-0 -- mongosh --quiet --eval '
db = db.getSiblingDB("srw_logs");
var d = db.llm_requests.find({agent_type: "persistent"}).sort({$natural:-1}).limit(1).next();
print(d.job_id, d.model, d.latency_ms, "turn", d.iteration, d.timestamp);'
```
- **Pass:** a document exists with `job_id` = the thread id, the session's
  main model, sane `latency_ms`, `iteration` = turn number. Pre-fix:
  `countDocuments({agent_type:"persistent"})` was 0 — sessions were
  unauditable (this is exactly what blocked the original investigation).
- Non-fatal contract: stop MongoDB and run a turn — the turn must still
  complete (only a debug log about the failed archive).

## #16 — Resumed sessions get truthful pod labels

Fixed in `session_router.ensure_route` (patches `srw/purpose=session` +
short `srw/thread-id` alongside the existing full-id label).

1. End a session (idle), then reopen it so it resumes via a warm **job** pool
   pod (orchestrator log: `resumed via idle pool agent srw-agent-j-…`). If no
   warm pod exists, send any message twice to force provision-or-assign.
2. ```bash
   $K get pod <that-pod> --show-labels
   ```
- **Pass:** the pod now carries `srw/purpose=session`,
  `srw/thread-id=<first-12-of-TID>`, and `srw.io/thread-id=<TID>` (pre-fix:
  `purpose=job`, short label missing → dashboards and the lifecycle
  reconciler's purpose accounting saw a job pod serving a session).
- Regression guard: a directly-provisioned `srw-agent-s-*` session still works
  and keeps the same labels (patch is idempotent).

## #15 — (unfixed) repro hunt: post-restore PDF tool 404s

Observed once (13:08:35, thread `1f39a5a6`): after a snapshot-restore the PDF
tool reported `File not found: /home/agent-host/workspace/uploads/…` for files
a shell `find` had just listed. To reproduce:

1. Session with an uploaded PDF + a cloud mount. Read the PDF once (works).
2. End the session (forces snapshot to S3) → reopen (restore path).
3. Ask for shell `find . -name '*.pdf'` **and** a PDF-tool read of the same
   absolute path.
- If the 404 reproduces, capture: agent log around the call, whether the SFTP
  target IP matches the CURRENT workspace pod (`$K get pod ws-thread-… -o wide`
  vs the IP in the agent's workspace context), and whether `cloud/` is an
  rclone mount that survived restore. Suspects ranked in the audit doc §15.

---

## Checklist

| # | Test | k3d | dev |
|---|------|-----|-----|
| 1A | mid-turn messages survive reload | ☐ | ☐ |
| 1B | mid-turn messages survive pod death | ☐ | ☐ |
| 1C | no duplicate user rows after consume | ☐ | ☐ |
| 2 | turn error renders, persists, doesn't poison agent | ☐ | ☐ |
| 3 | overflow: single attempt, no retry storm | ☐ | ☐ |
| 4 | broken aux + `/compact` → history kept | ☐ | ☐ |
| 8 | quiet badge ≥30 s into a silent turn | ☐ | ☐ |
| 9A | ws.ping every ~20 s | ☐ | ☐ |
| 9B | SIGSTOP → watchdog reconnect ≤60 s | ☐ | ☐ |
| 10 | approved card doesn't resurrect on reload; 409 → muted line | ☐ | ☐ |
| 11 | end mid-turn → confirm; API 409/force/fail-open | ☐ | ☐ |
| 12 | no create/delete alternation 30 min | ☐ | ☐ |
| 13 | single teardown per suspend, no contradictory WARNING | ☐ | ☐ |
| 14 | llm_requests doc per session turn | ☐ | ☐ |
| 16 | resumed pool pod relabeled to session | ☐ | ☐ |
| 15 | repro attempt recorded (issue still open) | ☐ | ☐ |

End-to-end finale (recreates the original incident, now survivable): small-ctx
model + giant PDFs + two mid-turn messages + a reload + an end-attempt from the
sessions list. Expected experience: quiet badge counting up → visible ⚠ error
line → both messages still present after reload → 409 confirm dialog on the
delete. That single walk exercises #1, #2, #3, #8, and #11 together.
