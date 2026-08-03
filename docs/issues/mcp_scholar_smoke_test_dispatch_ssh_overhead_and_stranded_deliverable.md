---
tags:
  - issue
  - mcp
  - jobs
  - scholar
  - workspace
  - ssh
  - git
  - observability
  - cost
related:
  - "[[officer_blind_reads_and_worker_bureaucracy]]"
  - "[[phase_model_overhead_amnesia_loop]]"
  - "[[deliverable_lost_to_nested_repo_commit_and_stranded_mode_a_job]]"
  - "[[job_resume_direct_path_skips_credential_injection]]"
  - "[[resume_never_provisions_a_missing_workspace]]"
  - "[[subjob_inherits_stale_workspace_container_snapshot]]"
---

# Live MCP Scholar smoke test: default jobs cannot reach Git, while the virtual control writes its report and loops until cancelled

**Filed:** 2026-08-03 after a live MCP smoke test against the deployed SRW
environment.

**Status:** **PARTIALLY REMEDIATED IN THIS CHECKOUT; LIVE ACCEPTANCE PENDING.**
The failure chain is reproduced and bounded. P0-A (MCP/start-path provisioning)
and P0-B (SSH configuration/authentication readiness) were implemented on
2026-08-03. The deployed connector has not yet been refreshed and the three
diagnostic job records were left intact. P0-C and the P1/P2 findings remain open.

**Severity:**

- **P0 — default Git-backed job execution:** both fresh sandbox paths failed
  before object-level agent work or Git could begin.
- **P1 — runtime/cost and deliverable durability:** a virtual-workspace control
  wrote the requested report, then spent more than twenty LLM turns repeating
  verification and never completed. The report is not reachable through either
  deployed MCP file reader after cancellation.
- **P2 — operator controls and telemetry:** Pause means preempt-and-auto-resume,
  live messaging requires a pre-existing thread, and progress/todo/config
  reporting did not describe the live run.

This is a current live acceptance failure, not a claim that every symptom has one
root cause. The sandbox SSH failure's immediate cause is now confirmed in source:
the resume dispatcher reconstructed only the container host and port, omitting
the deliberately non-persisted username, private-key path, and workspace path.
The deployed key pair must still pass the new authenticated readiness gate in the
live acceptance run; no secret material was inspected to reach this conclusion.

---

## 1. Executive verdict

No Scholar job completed end to end. Three attempts isolated three different
layers:

| Attempt | Job | Backend/path | Terminal result | Runtime evidence | Gitea result |
|---|---|---|---|---|---|
| Project-scoped smoke test | `ecc13fd4-76c8-4917-bfc2-39480bfc503a` | Existing project `becb5a96-1d8d-4916-a2e2-755dfd86cb3a`, default sandbox, manual MCP assignment | `failed` | 0 audit entries; agent never ran | Git behavior not reached |
| Standalone sandbox isolation | `f54c874d-501b-4553-a690-8aff199a52fa` | Default sandbox; first manual assignment, then requeued through automatic dispatch | `failed` | 0 audit entries; worker process reached workspace initialization, then SSH recovery exhausted | One initial commit `107fc8c1`; only `README.md` |
| Scholar runtime control | `fc7e60e1-91fc-425a-8a44-563f2c878d7e` | Explicit `workspace.backend=virtual` to remove SSH/Git from the critical path | `cancelled` by operator after repetition loop | 365 audit entries; 56 LLM requests; final report proven written | One initial commit `052f7e09`; only `README.md` |

The Git-backed experiments therefore **do not validate or invalidate** the
2026-08-02 wrong-tree/push fix in
`deliverable_lost_to_nested_repo_commit_and_stranded_mode_a_job.md`. Both died
before an agent could create a deliverable or make a commit. The virtual control
has no Git branch (`output/manifest_status.json` recorded `branch: null`) and
cannot exercise worktree behavior.

What the experiment does establish:

1. The MCP-created default job path is presently unusable through its documented
   manual-assignment sequence.
2. The automatic provisioning sequence gets further, but the provisioned
   workspace is not SSH-authenticatable by the worker.
3. Once SSH is removed, the Scholar can research and write a reasonable artifact,
   but orchestration/skill ceremony dominates the task and the completion path can
   loop indefinitely.
4. Work written in the virtual workspace can be left outside Gitea and outside
   the deployed operator read surface when a job is interrupted.

---

## 2. Test contract and scope

The deliberately small task was:

> Using at most three authoritative primary sources, identify three practical
> RAG failure modes and one mitigation for each. Produce a concise Markdown
> report at `output/report.md`. Do not modify application code. Finish as soon as
> the report is complete.

Additional control instructions prohibited delegation, subjobs, workspace
upgrades, and unrelated exploration. The virtual control explicitly prohibited
shell/tool-tier upgrades so it would measure the Scholar graph rather than the
workspace substrate.

The intended deliverable contract was:

```json
{"required_deliverables": ["output/report.md"]}
```

The deployed `create_project_job` MCP schema did **not** expose
`required_deliverables`, so the project attempt could only state the contract in
the task text. The standalone jobs put it in context. The checked-out MCP server
does expose the parameter (`orchestrator/mcp/server.py:1939-1980`), which is one
piece of evidence that the deployed/cached MCP tool schema is stale relative to
this checkout.

### Investigation boundary

The MCP connector targets a remote SRW environment. The local `kubectl` context
in this checkout points at an unrelated local `k3d-srw` cluster, so it was not
used to draw conclusions about the remote agent pod or secret. No secret values
were read, printed, or copied. Secret/key parity and the complete start path must
still be proven against the actual connector target after deployment.

---

## 3. Findings ledger

| # | Finding | Confidence | Priority |
|---|---|---|---|
| F1 | The deployed MCP surface is stale/internally contradictory: it omits a source-defined deliverable parameter and describes a Gitea reader as a local-workspace reader | Confirmed from live schema + checkout | P1 |
| F2 | The MCP creation response tells callers to use `assign_job`, but manual assignment bypasses the only sandbox provisioning stage | Confirmed; fixed in checkout, live acceptance pending | P0 |
| F3 | A requeued sandbox job used the resume path, which restored host/port but omitted its non-persisted SSH identity fields, producing `No authentication methods available` | Root cause confirmed; fixed in checkout, live acceptance pending | P0 |
| F4 | Sandbox SSH failures are logged as “VM workspace unavailable” and consume the generic three-attempt recovery budget | Confirmed; fixed in checkout | P1 |
| F5 | Scholar-specific initialization forces five strategic process tasks and asks for a 10–20-todo phase even for a bounded one-file answer | Confirmed live + config | P1 |
| F6 | Enforced skill-read gates caused predictable failed calls and extra LLM turns before both todo creation and citation | Confirmed in audit + config | P1 |
| F7 | The source budget was not honored at the source-library layer: two searches automatically archived ten results, including irrelevant, non-primary, and inaccessible pages | Confirmed live | P1 |
| F8 | The report was complete at LLM request 31, but verification repeated through request 55; no repeated-action/result loop breaker fired | Confirmed live | P0 cost / P1 function |
| F9 | Live progress, phase, todo, and config reporting did not reflect the active job | Confirmed live | P2 |
| F10 | The virtual report is proven written but never exported to Gitea and is unavailable through both deployed MCP readers after cancellation | Confirmed visibility failure; physical deletion not proven | P1 |
| F11 | `pause_job` is a preemption primitive that immediately re-enters dispatch, while its MCP description reads like an operator hold | Confirmed live + code | P1 operator control |
| F12 | There was no usable live steer/interrupt channel because `send_message_to_job` requires a thread ID and the job had no message thread | Confirmed live | P2 |
| F13 | The original wrong-tree/worktree concern remains untested because neither Git-backed run reached Git | Confirmed limitation | Acceptance blocker |

---

## 4. F1/F2 — the MCP happy path directs a fresh job around provisioning

### Deployed MCP surface drift

Two independent differences were visible:

1. The checked-out `create_project_job` definition accepts and documents
   `required_deliverables` (`orchestrator/mcp/server.py:1939-1980` and
   `orchestrator/mcp/client.py:1835-1876`), but the connector schema available to
   this run did not contain it.
2. The connector described `get_workspace_file` as reading the current local
   filesystem. Current source explicitly describes it as Gitea-backed committed
   state and calls `/api/jobs/{id}/repo/file`
   (`orchestrator/mcp/server.py:831-854`,
   `orchestrator/mcp/client.py:1037-1054`). The live error also said the file was
   “not found in repo”, confirming that the deployed behavior is the repo reader,
   not the advertised local reader.

This is more than cosmetic. It prevented attaching the normal deliverable
contract to the first project test and caused the operator to expect a
real-time/interrupt-recovery read path that does not exist.

All three `get_job` responses also displayed `Config: N/A`, even though the jobs
were requested as Scholar jobs and the control visibly loaded the Scholar
templates. That is a smaller instance of the same observability/schema drift.

### Confirmed dispatch mismatch

The MCP creation formatter ends with:

```text
Next step: Use assign_job(job_id, agent_id) to assign this job to an agent.
```

That text is emitted at `orchestrator/services/formatters.py:799-818`.

The manual endpoint explicitly calls itself an override which bypasses the
auto-assign queue (`orchestrator/main.py:17448-17458`). For a `created` or
`failed` job it calls `_dispatch_job_to_agent` directly
(`orchestrator/main.py:17488-17492`). It does not run the dispatcher's workspace
pre-filter.

The only fresh-sandbox provisioning step lives in the automatic dispatcher:

- pending `created`/`paused` jobs are selected at `main.py:5645-5652`;
- `_job_needs_sandbox` routes them into the workspace lifecycle at
  `main.py:6008-6075`;
- only on a later tick, once context contains a ready container, does
  `_dispatch_job_to_agent` inject host, port, username, and key path at
  `main.py:2569-2601`.

Manual assignment skips the first two bullets. The dispatch backstop correctly
refuses to send a non-lite backend without `workspace.remote`
(`main.py:2659-2682`), producing the project job's exact error:

```text
Workspace backend requires SSH credentials but none were resolved at dispatch
(backend=sandbox (default)).
```

The standalone sandbox job reproduced the same missing-remote failure on its
first manual assignment. Only after it was requeued for automatic dispatch did
it advance to workspace creation and expose the distinct SSH failure in F3.

The backstop is doing its job; the MCP's prescribed transition into that
backstop was wrong.

### Implemented correction (2026-08-03)

The checkout now has one coherent contract:

- `format_created_job` reports automatic workspace provisioning/assignment and
  no longer tells ordinary callers to invoke `assign_job`.
- MCP and REST creation docs describe automatic dispatch.
- `assign_job` remains an admin override, but `_resume_missing_workspace` now
  gates it before direct worker dispatch. Missing/stale managed workspaces are
  shed and queued for the normal dispatcher; the requested agent is explicitly
  not reserved.
- The MCP health response exposes source/release provenance plus
  `tool_schema_revision=2`, giving deployment acceptance a direct way to detect
  an old MCP image/schema.
- Contract tests pin both creation signatures' `required_deliverables` argument,
  the automatic-dispatch response, and the committed-Gitea reader semantics.

The provisioning implementation remains owned by the automatic dispatcher; the
admin path now joins that queue whenever its backend preflight is not satisfied.

---

## 5. F3/F4 — automatic provisioning reaches a pod, then fails authentication

The standalone job was requeued so the automatic dispatcher, rather than manual
assignment, owned provisioning. This got materially further:

1. A workspace was provisioned with the stable DNS
   `workspace-f54c874d-501.superhuman-remote-worker.svc.cluster.local:30022`.
2. A worker started and attempted to initialize the remote backend.
3. SSH connection recovery ran to its cap.
4. The job failed with zero audit entries and the terminal error:

```text
workspace unavailable; recovery exhausted after 3 attempts: Failed to connect
to workspace workspace-f54c874d-501.superhuman-remote-worker.svc.cluster.local:30022
after 2 attempt(s) [ambiguous]: No authentication methods available
```

This is not the same failure as F2: F2 never had a remote block. F3 had a host
and reached Paramiko, but no usable authentication method reached the connection.

### Confirmed immediate cause

The job was requeued from its initial failure, so the automatic dispatcher used
`_resume_job_on_agent`, not the first-start payload builder. The two paths were
asymmetric:

- first dispatch injected host, port, username, key path, and workspace path;
- resume deliberately reloaded the bare creation-time `config_override` because
  injected credentials are not persisted, but then re-injected only host and
  port.

That leaves Paramiko with no explicit identity and exactly explains the archived
`No authentication methods available` error. This was a payload reconstruction
defect; it does not require assuming that the Kubernetes secret itself was bad.

### Expected key wiring

Current code intends one Kubernetes secret to supply both halves:

- dispatch sets the worker key path to `/run/secrets/vm-ssh-key`
  (`orchestrator/main.py:2583-2593`);
- the dynamic agent mounts secret key `ssh-privatekey` at that path
  (`orchestrator/services/agent_provisioner.py:1314-1321`) from the configured
  secret (`:1370-1379`);
- the workspace pod mounts `ssh-publickey`
  (`orchestrator/services/container_provisioner.py:1475-1487`);
- `RemoteBackend.connect` passes a configured key path to Paramiko as
  `key_filename` (`src/core/backends/remote.py:333-349`).

### Implemented authentication controls (2026-08-03)

Without printing key material:

1. First dispatch and resume now call one sandbox config helper. Resume replaces
   stale endpoints and always restores the managed username/key mount; both paths
   preserve the job's `worktree_path`.
2. The Kubernetes provisioner no longer publishes `status=ready` after a TCP-only
   pod readiness check. It validates the private-key file as the orchestrator UID,
   calculates only its SHA256 public fingerprint, and runs `ssh ... true` with
   `BatchMode=yes`, `IdentitiesOnly=yes`, and public-key-only authentication.
   A successful command is cryptographic proof that the mounted private key
   matches the workspace's installed authorized public key.
3. `RemoteBackend` independently validates existence, readability, and parsing
   as the actual worker UID. Paramiko is forced to use the explicit configured
   key (`allow_agent=False`, `look_for_keys=False`), so a warm SSH agent or home
   directory cannot mask a broken deployment.
4. Missing, unreadable, invalid, passphrase-protected, or rejected credentials
   raise `WorkspaceAuthenticationError`. Completion reports them once as
   `workspace_authentication`, `recoverable=false`; they no longer consume the
   pod/VM recovery budget.
5. Agent logs now say `workspace`, not `VM`, and call authentication failures
   non-retryable.

The live deployment still needs to prove that its key mount and authorized key
pass this gate. Recording Kubernetes Secret resource versions on both pods is a
useful follow-up diagnostic, but is no longer needed to explain this incident.

### Former misclassification

The four-line archived worker log called this a “VM workspace unavailable” event
even though the backend was sandbox. The old wording was hard-coded for generic
workspace exceptions in both non-streaming and streaming handlers. The checkout
now uses backend-neutral wording and reports the deterministic auth class as:

```text
backend=sandbox, stage=initial_connect, class=authentication, retryable=false
```

This exact symptom string has appeared before in
`docs/done/job_resume_direct_path_skips_credential_injection.md`. This incident
revealed another resume payload builder with the same missing-in-flight-config
class. Acceptance must therefore include a real fresh-pod SSH handshake, not
only payload/unit tests.

---

## 6. F5-F8 — the virtual control completed the work, not the job

The virtual backend bypassed SSH and isolated the Scholar graph/tool behavior. It
successfully researched the topic, produced five verified citations across two
sources, and wrote a coherent 4,248-character report. The runtime nevertheless
failed its much simpler operational contract: finish when the report is complete.

### Cost and timeline

Audit activity ran from `2026-08-03T08:34:39.808Z` through
`08:56:59.942Z` — about 22 minutes 20 seconds.

| LLM request(s) | Time (UTC) | What happened |
|---|---|---|
| 1-4 | 08:35:38-08:37:23 | Read brief/instructions, listed workspace, searched KB, then wrote four KB notes restating scope, deliverable, constraints, and acceptance criteria |
| 5-7 | 08:38:11-08:38:38 | Wrote a fifth KB note and a 3,881-character `plan.md`; first `next_phase_todos` attempt was rejected until the required todo-guide was read |
| 8-15 | 08:39:03-08:40:41 | More KB/process/todo work; no object-level web research yet |
| 16 | 08:40:52 | First `web_search`, more than six minutes after audit start |
| 17 | 08:41:21 | Wrote the first 622-character report scaffold; manifest write hit another read-before-write gate |
| 19 | 08:42:22 | Second `web_search` |
| 20-30 | 08:43:21-08:47:15 | Wrote a 3,573-character `notes/task.md`, loaded citation skill after an enforced failure, registered citations, and continued todo ceremony |
| 31 | 08:47:38 | Wrote the final 4,248-character `output/report.md`; the requested object-level task was substantively complete |
| 34 | 08:48:55 | Tried unavailable `shell_execute` despite the explicit virtual/no-upgrade constraint |
| 35-55 | 08:49:03-08:55:22 | Repeated almost identical `file_exists`, `read_file`, and `search_files` verification bundles; remaining verification todo never completed |
| 56 | 08:56:15 | Final file/list checks around cancellation |

Token totals from all 56 recorded LLM requests:

| Input | Output | Total |
|---:|---:|---:|
| 2,456,862 | 30,411 | **2,487,273** |

The context grew from 15,148 input tokens on request 1 to more than 70,000 per
turn near the end. Repeating the verification ritual therefore became more
expensive on every pass.

### Scholar-specific bureaucracy remains after generic fixes

The live behavior comes directly from the current Scholar specialization:

- initialization has five mandatory strategic todos
  (`config/experts/scholar/strategic_todos_initial.yaml:7-143`);
- it requires four persistent KB notes before research (`:19-29`), then another
  exploration-focus note (`:44-51`);
- it requires a multi-phase exploration plan even though this task asks for one
  short artifact (`:58-96`);
- it says to create 10-20 tactical todos (`:121-143`);
- the Scholar todo guide repeats the 10-20 target
  (`config/experts/scholar/todo_guide.md:1-13`);
- Scholar enables delegation by default and requires a fan-out decision for
  every planned phase (`config/experts/scholar/config.yaml:23-29`; strategic
  template `:68-77`), even though this run explicitly prohibited delegation;
- enforced pre-tool skills reject `next_phase_todos` and citation calls until
  their files have been read (`config/experts/scholar/config.yaml:42-59`).

The agent did obey the no-delegation instruction, but it still paid the planning
and fan-out-decision tax. It ultimately staged eight tactical todos for a task
whose natural plan is approximately: search, read, write, verify.

This is a Scholar-specific gap in the generic bureaucracy reductions recorded in
`officer_blind_reads_and_worker_bureaucracy.md`. It is also direct token-side live
evidence for `phase_model_overhead_amnesia_loop.md`, whose status said token
confirmation was still owed.

### Source-budget drift

The final report itself used only two sources and all five citations verified.
That is the good part. However, each of two `web_search(max_results=5)` calls
automatically archived all five hits, leaving ten registered job sources despite
the explicit “at most three” budget. The library included:

- two inaccessible HTTP 403 pages;
- Medium, LinkedIn, and general blog material rather than only primary sources;
- an irrelevant AWS Builder page about JSON generation.

Therefore the final prose stayed within the source-count cap, but the agent/tool
workflow did not. A task-level source budget must constrain search result count
and/or source registration, not merely the bibliography the model eventually
writes.

### Verification loop

The final report was readable at the logical path and the agent repeatedly said
it already contained 4,248 characters, three sections, two sources, and five
citations. It nevertheless restarted the same “define done, run fresh checks,
reconcile” sequence for more than twenty LLM calls.

The loop's stable signature was roughly:

```text
file_exists(output/report.md)
read_file(output/report.md)
search_files(output/report.md, "Mitigation")
search_files(output/report.md, heading/URL patterns)
```

The heading-pattern check repeatedly returned no matches while direct reads
proved the headings existed. No code recognized that the normalized tool calls
and their results were repeating, no bounded verifier terminated with a clear
pass/fail, and the model never completed `todo_8`.

A loop breaker should operate below the model:

- hash normalized tool name + arguments + result;
- detect repeated bundles with no workspace/todo/citation state change;
- after a small threshold, either mark the deterministic check inconclusive and
  proceed using the successful checks, or freeze once with a precise diagnostic;
- never let a one-file smoke test spend another million tokens re-reading an
  unchanged file.

For small explicit tasks, Scholar also needs a fast path that can skip persistent
project KB writes, fan-out planning, and a 10-20-todo phase. User constraints such
as source count, no delegation, and “finish as soon as the report is complete”
must override specialization defaults.

---

## 7. F9/F10 — the report exists in audit history but not in the operator artifact surface

The audit contains two successful writes:

```text
Written: jobs/fc7e60e1-91fc-425a-8a44-563f2c878d7e/output/report.md (622 chars)
Written: jobs/fc7e60e1-91fc-425a-8a44-563f2c878d7e/output/report.md (4248 chars)
```

Logical `file_exists("output/report.md")` and `read_file("output/report.md")`
also succeeded repeatedly inside the worker. Thus “the model hallucinated the
file” is ruled out.

After cancellation:

- Gitea `main` still had only initial commit `052f7e09` and `README.md`;
- `get_job_file(output/report.md)` returned “not found in repo”;
- the deployed `get_workspace_file(output/report.md)` returned the same repo
  error because it is also Gitea-backed;
- the report content remained recoverable from full LLM request/audit tool
  arguments, but there is no normal artifact recovery operation in the exposed
  MCP surface.

This does **not** prove that the object-store bytes were deleted. It proves that a
successfully written required deliverable can become inaccessible to the operator
through both advertised readers when the virtual job is interrupted before its
final export/commit path.

The write result itself contributed to agent confusion: it echoed an object-store
key prefixed with `jobs/<id>/`, while the file tool's logical path remained
`output/report.md`. The model began checking both paths and incorrectly reasoned
that the earlier write may have landed at the wrong root. Tool results should
clearly distinguish a logical workspace path from an internal storage key.

### Telemetry gaps observed during the run

- `get_job_progress` remained `0.0%` with no useful phase while real work and
  tactical todos were active.
- the MCP current-todo view returned no todos while the audit showed eight
  tactical todos being executed;
- `get_job` displayed `Config: N/A`;
- after cancellation, `get_job_progress` continued reporting wall-clock elapsed
  time (30m20s at a later read) rather than the approximately 22m20s audit span;
- workspace overview showed only Gitea, so it could not reveal the active virtual
  files.

The audit/LLM-request APIs were the one strong observability path: they made the
report write, token cost, citations, and repetition loop reconstructable. They
should remain the forensic backstop, not the only recovery path.

### Required durability/read contract

At least one of these must hold for virtual jobs:

1. `get_workspace_file` genuinely reads the live/durable virtual filesystem,
   with a separate name for the Gitea committed-state reader; or
2. every pause/failure/cancellation synchronously snapshots/export-pushes current
   virtual files before releasing the worker; or
3. a first-class “recover artifact from workspace/audit” operation materializes
   a selected file into Gitea with provenance.

Required deliverables deserve stronger handling: if one has been successfully
written, cancellation/failure metadata should list its logical path and recovery
location even when the completion gate was never reached.

---

## 8. F11/F12 — there was no non-destructive way to stop the loop and preserve a held state

At 51 LLM requests the operator called MCP `pause_job`, expecting the job to stop
after its current node. The job did become paused, but the scheduler immediately
assigned it to another ready worker and execution continued.

This is current intentional backend behavior:

- the endpoint says the paused job “re-enters the dispatch queue and will be
  auto-resumed” (`orchestrator/main.py:9447-9457`);
- the dispatch query selects both `created` and `paused` jobs
  (`orchestrator/database/postgres.py:6029-6059`).

The deployed MCP description says only that the agent finishes its current node,
saves a checkpoint, and becomes available. It omits that the same job is eligible
for immediate reassignment. “Pause” is therefore a scheduler preemption primitive,
not an operator hold.

The attempted alternative, an urgent job message, was unavailable:
`send_message_to_job` requires a `thread_id`, and the job had not opened any
message thread. There is no generic job-ID guidance/interrupt operation for this
case.

Cancellation was the only exposed control that held. It stopped the runaway work
and both workers returned to ready, but it also created the artifact visibility
problem in F10.

Recommended API split:

- `preempt_job`: save checkpoint and return to the queue;
- `hold_job` or `pause_job(auto_resume=false)`: save checkpoint and remain held
  until explicit resume;
- job-ID-scoped `steer_job` that does not require an agent-originated thread;
- preserve/snapshot required artifacts before acknowledging hold/cancel.

---

## 9. Causal chain

```text
MCP create project job
  -> deployed formatter says “assign_job next”
  -> manual override bypasses workspace ensure
  -> dispatch credential backstop fails before agent/audit/Git                 [F1/F2]

Requeue standalone job through automatic dispatcher
  -> workspace pod + stable DNS created
  -> worker receives/attempts remote backend
  -> no usable SSH authentication method
  -> generic workspace recovery spends 3 attempts
  -> job fails before agent graph/audit/Git                                    [F3/F4]

Force virtual backend to isolate graph
  -> five strategic todos + KB/plan/skill ceremony
  -> first research after 16 LLM calls
  -> report complete at request 31
  -> invalid shell attempt + repeated verification bundle through request 55
  -> Pause immediately redispatches
  -> Cancel finally holds
  -> report remains outside Gitea and both MCP readers                         [F5-F12]
```

The experiment never reaches the branch/worktree portion of the first two paths,
so the next acceptance run must explicitly cover it after F2/F3 are repaired.

---

## 10. Prioritized remediation plan

### P0-A — make every start path provision-aware — implemented, live gate open

- [x] Stop telling ordinary MCP callers to use the admin manual-assign override.
- [x] Route a workspaceless manual assignment back through the dispatcher.
- [x] Share the sandbox SSH config builder across start/resume and reuse the
  dispatcher's missing-workspace preflight for the admin path.
- [ ] Refresh the deployed MCP schema/tool cache and prove revision 2 in live
  acceptance.

### P0-B — validate SSH authentication, not just workspace readiness — implemented, live gate open

- [x] Calculate the non-secret private-key fingerprint and prove authorized-key
  parity with a real authenticated command.
- [x] Validate the private key from the actual worker UID and filesystem.
- [x] Require authenticated SSH before Kubernetes publishes workspace readiness;
  independently validate/connect from the worker at initialization.
- [x] Classify deterministic authentication failures separately and fail once
  with a useful backend-specific message.

### P0-C — stop repeated identical execution loops

- Add normalized tool-call/result repetition detection.
- Bound verification retries independently of phase/token budgets.
- Freeze/fail once with the repeated check and last result instead of silently
  continuing.

### P1-A — give Scholar a bounded-task path

- Honor explicit no-delegation/source-count/finish-early instructions as hard
  constraints.
- Remove the unconditional five-KB-note + multi-phase plan requirement for small
  tasks.
- Permit 2-4 tactical steps rather than requiring 10-20.
- Avoid enforced skill-read failure turns: inject the required small contract
  before the gated call or make the tool result itself actionable without another
  LLM cycle.

### P1-B — make virtual artifacts durable and readable

- Separate live-workspace and committed-Gitea readers in names and descriptions.
- Snapshot/export required files on hold, terminal failure, and cancellation.
- Expose artifact recovery with provenance from object storage/audit when final
  export did not occur.
- Do not leak internal object-store prefixes into logical path confirmations.

### P2 — repair operator controls and telemetry

- Distinguish preempt from hold.
- Allow job-scoped steering without a pre-existing message thread.
- Populate phase/todo/progress/config for virtual jobs.
- Stop elapsed-time counters at terminal status.
- Include backend, failure stage, retryability, and recovery count in job logs.

---

## 11. Required live acceptance test

After the fixes, repeat the same bounded Scholar task through the deployed MCP
surface—not a unit-only substitute—and require all of the following:

1. `create_project_job` exposes `required_deliverables=["output/report.md"]`.
2. The documented next action cannot bypass workspace provisioning.
3. A fresh worker pod authenticates to a fresh sandbox workspace; warm-pod
   environment leakage must not be allowed to mask the test.
4. Object-level research begins within the first few LLM calls; no forced
   10-20-todo plan is created for this task.
5. No more than three sources are registered for the job, and inaccessible or
   irrelevant search hits are not silently counted as selected sources.
6. The report is scaffolded early, finalized, committed, and pushed to the
   assigned project job branch.
7. `get_job_file("output/report.md")` returns the committed report and
   `get_workspace_file` has unambiguous live-vs-committed semantics.
8. The job reaches `completed` or `pending_review` without operator intervention.
9. No normalized tool/result bundle repeats more than three times without a state
   change.
10. A second run pauses into a true hold; the report remains retrievable while
    held and after cancellation.
11. Inspect the project repo/worktree and prove the deliverable landed on the
    intended job branch, not `main`, another job's branch, a nested repo, or an
    unpushed local commit. This is the explicit acceptance gate for the user's
    original wrong-tree concern.

Suggested smoke-test budget for this exact one-file task: first web/research tool
within three LLM decisions, final artifact within ten minutes, and no unbounded
verification tail. Record full token totals so the improvement is measurable
against today's **2,487,273-token** baseline.

---

## 12. Preserved evidence and related incidents

The following live records were intentionally not deleted:

- `ecc13fd4-76c8-4917-bfc2-39480bfc503a` — manual project assignment failure;
- `f54c874d-501b-4553-a690-8aff199a52fa` — automatic sandbox SSH failure;
- `fc7e60e1-91fc-425a-8a44-563f2c878d7e` — virtual runtime/audit/citation loop,
  cancelled after evidence capture.

The last job's full `list_llm_requests` record has 56 request IDs (`93915` through
`94009` with gaps). Request `93967` contains the final report write; requests
`93971` onward show the verification repetition. The job has ten registered
sources and five verified citations.

Related but distinct history:

- `docs/issues/deliverable_lost_to_nested_repo_commit_and_stranded_mode_a_job.md`
  — real Git push parser/root-anchor incident fixed on 2026-08-02; not exercised
  here.
- `docs/issues/officer_blind_reads_and_worker_bureaucracy.md` — earlier blind
  reader and generic phase bureaucracy postmortem; today's deployed MCP metadata
  remains stale and Scholar-specific templates retain the ceremony.
- `docs/issues/phase_model_overhead_amnesia_loop.md` — broader phase-cost analysis;
  today's 56-call/2.49M-token run supplies direct Scholar token evidence.
- `docs/done/job_resume_direct_path_skips_credential_injection.md` — previous
  fresh-agent credential omission with the same SSH symptom, fixed on another
  endpoint.
- `docs/done/resume_never_provisions_a_missing_workspace.md` — previous direct
  resume/provisioning mismatch; manual start now exhibits the same architectural
  hazard on a different path.
- `docs/issues/subjob_inherits_stale_workspace_container_snapshot.md` — source of
  the dispatch backstop's missing-remote error; today's project job was not a
  stale-inheritance subjob, so the backstop message's “usually means the parent”
  hint was misleading for this case.

Both worker agents were back in `ready` state after cancellation. No live project,
job, commit, workspace, or audit evidence was cleaned up.
