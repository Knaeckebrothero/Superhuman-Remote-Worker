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
  - "[[overnight_minimax_m3_scholar_batch_2026-08-03]]"
  - "[[job_runtime_containment_gap]]"
  - "[[project_scoped_memory_deadlocks_under_parallel_jobs]]"
  - "[[embedding_batch_overflow_skips_citation_source_embeddings]]"
  - "[[minimax_m3_auxiliary_structured_output_flaps]]"
  - "[[phase_boundary_tags_are_moved_then_rejected_by_remote]]"
---

# Live MCP Scholar smoke test: default jobs cannot reach Git, while the virtual control writes its report and loops until cancelled

**Filed:** 2026-08-03 after a live MCP smoke test against the deployed SRW
environment.

**Status:** **MAIN-CLUSTER GIT PATH ACCEPTED (5/5); MCP CONTRACT, DELEGATION,
AND PAPER-PROVIDER DEFECTS REMAIN.**

The original failure chain was reproduced and its standard main-cluster path is
now functionally recovered. P0-A (MCP/start-path provisioning) and P0-B (SSH
configuration/authentication readiness) were implemented on 2026-08-03. On
2026-08-03/04, five fresh project Scholar jobs then automatically dispatched on
the main cluster, used ordinary sandbox workspaces, completed without operator
intervention, pushed their exact reports to five isolated job branches, and left
project `main` unchanged. That closes the normal success-path and original
wrong-tree acceptance questions for this batch.

It does **not** yet prove the exact requeue/resume credential-restoration path or
the server-enforced required-deliverable gate. The connector still advertises
the old MCP contract, the local k3d projected-key mode remains a development
quirk, `delegation.enabled=false` is not a real capability gate, and the paper
providers are broken in the deployed environment. Runtime containment remains
separate in `job_runtime_containment_gap.md`; the successful batch now supplies
its first main-cluster Scholar baseline.

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

This began as a live acceptance failure, not a claim that every symptom had one
root cause. The sandbox SSH failure's immediate cause was confirmed in source:
the resume dispatcher reconstructed only the container host and port, omitting
the deliberately non-persisted username, private-key path, and workspace path.
The main-cluster key pair subsequently passed the authenticated readiness gate
on all five fresh overnight jobs. The requeue/resume identity-restoration path
still needs its own targeted acceptance run; no secret material was inspected
to reach either conclusion.

---

## 1. Executive verdict

At the time of the initial 2026-08-03 test, no Scholar job completed end to end.
Three attempts isolated three different layers:

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

The later five-job acceptance in section 14 supersedes the first two statements
for the normal fresh-dispatch path; this historical table remains the evidence
that led to the corrections.

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
| F2 | The MCP creation response told callers to use `assign_job`, while manual assignment bypassed the only sandbox provisioning stage | Fixed in checkout; automatic start accepted 5/5, admin override still needs a targeted live check | P0 |
| F3 | A requeued sandbox job used the resume path, which restored host/port but omitted its non-persisted SSH identity fields, producing `No authentication methods available` | Root cause fixed in checkout; fresh SSH accepted 5/5, requeue/resume acceptance still pending | P0 |
| F4 | Sandbox SSH failures are logged as “VM workspace unavailable” and consume the generic three-attempt recovery budget | Confirmed; fixed in checkout | P1 |
| F5 | Scholar-specific initialization forces five strategic process tasks and asks for a 10–20-todo phase even for a bounded one-file answer | Confirmed live + config | P1 |
| F6 | Enforced skill-read gates caused predictable failed calls and extra LLM turns before both todo creation and citation | Confirmed in audit + config | P1 |
| F7 | The source budget was not honored at the source-library layer: two searches automatically archived ten results, including irrelevant, non-primary, and inaccessible pages | Confirmed live | P1 |
| F8 | The report was complete at LLM request 31, but 21 more verification rounds issued 88 tool calls through request 55. A literal-search/regex mismatch, a phase-boundary-only stale manifest, and per-turn reinjection of the verification skill formed a completion deadlock; no hard loop breaker fired | Confirmed live + source | P0 cost / P1 function |
| F9 | Live progress, phase, todo, and config reporting did not reflect the active job | Confirmed live | P2 |
| F10 | The virtual report is proven written but never exported to Gitea and is unavailable through both deployed MCP readers after cancellation | Confirmed visibility failure; physical deletion not proven | P1 |
| F11 | `pause_job` is a preemption primitive that immediately re-enters dispatch, while its MCP description reads like an operator hold | Confirmed live + code | P1 operator control |
| F12 | There was no usable live steer/interrupt channel because `send_message_to_job` requires a thread ID and the job had no message thread | Confirmed live | P2 |
| F13 | The original runs never reached Git, leaving the wrong-tree/worktree concern untested | Closed for the fresh path: five isolated job branches passed; resume remains separate | Acceptance passed |
| F14 | The local k3d authenticated readiness gate correctly rejected the SSH key, but for a chart/dev-runtime incompatibility already described in chart comments: the root-running dev orchestrator invoked OpenSSH on a root-owned `0444` projected key | Confirmed local + source | P0 local acceptance |
| F15 | `delegation.enabled=false` persisted in the resolved local config but did not remove or disable `spawn_subagent`; the model ignored the textual prohibition and launched three readers (33 additional LLM calls) | Confirmed local + source | P1 cost / contract |
| F16 | Local Tavily calls were real provider errors caused by broken k3d external DNS, but `_direct_web_search` discarded the response's `error` field and reported a successful “No web results found” observation | Confirmed local provider probe + CoreDNS logs + source | P1 error semantics |
| F17 | `search_papers` is incompatible with installed `arxiv==4.0.0`: source calls removed `Search.results()` instead of `Client.results(search)` | Confirmed local runtime + source | P1 tool function |
| F18 | The local Scholar retried empty/broken research tools from main calls 37-55, including exact duplicate queries, and consumed 1.63M raw tokens before operator cancellation; the configured 120-tool budget and current warning-only loop detector did not contain it in time | Confirmed local audit | P0 cost / P1 function |
| F19 | Five fresh main-cluster Scholar jobs automatically provisioned/authenticated, completed, and pushed exact deliverables to isolated job branches; project `main` was unchanged | Confirmed live, 5/5 | P0 recovery passed |
| F20 | `delegation.enabled=false` still leaves `spawn_subagent` in the bound tool definitions; the two disabled jobs made no calls only because the model obeyed prose | Confirmed live prompt/audit | P1 capability contract |
| F21 | The generic transition todo says the stop condition comes first while requiring todo 1's review to have completed; two jobs repeated successful Git/file checks for 53 and 62 parent rounds before context compaction broke the attractor | Confirmed live + template | P1 overhead / completion risk |
| F22 | One-shot skill delivery replaced continuous reinjection, but the passive `verify-before-done` gate rejected 50 completion calls because MiniMax repeatedly retried instead of reading the named skill | Confirmed live audit | P1 overhead; gate remained safe |
| F23 | Main-cluster Tavily worked under heavy use; the paper job still reproduced the arXiv 4 adapter error and Semantic Scholar HTTP 403, then recovered through web/arXiv pages | Confirmed live | P1 paper-tool function |
| F24 | The connector schema remains stale after the successful batch: no `required_deliverables`, obsolete manual-assignment copy, and incorrect `get_workspace_file` semantics | Confirmed from current connector schema | P1 deployment contract |
| F25 | Citation traceability is inconsistent across successful reports: 1,083 registered sources produced 236 engine citations, while two reports used manual links with zero or near-zero engine citation coverage | Confirmed live stats/report reads | P1 quality contract |
| F26 | CitationEngine skipped 380 auto-embedding attempts covering 359 unique source IDs; 374 exceeded the embedding backend's 64-input limit, while the source still counted as registered | Confirmed archived worker logs + source | P1 search/evidence quality |
| F27 | Five concurrent jobs sharing one project produced 138 contained memory-retrieval deadlocks; per-turn project-wide TTL/access writes contend on the same rows and shared TTL is decremented once per consumer | Confirmed archived worker logs + source | P1 concurrency/context quality |
| F28 | MiniMax-M3 failed 75 memory-extraction and 19 memory-assembly structured outputs, causing ten auxiliary degraded/recovered cycles while main jobs continued | Confirmed archived worker logs | P2 quality/overhead |
| F29 | Every job logged `checkpoint.db not found` at all three phase snapshots (15 total), so the successful fresh path does not prove snapshot-backed resume | Confirmed archived worker logs | P1 recovery durability |
| F30 | Two jobs force-moved an already-pushed tactical tag; seven `git push --tags` calls were rejected and the remote phase tag remained behind the final branch history | Confirmed archived logs/Gitea/source | P1 phase evidence |
| F31 | One web extraction crashed on a string response (`.get` assumption), and two PDF sources could not register because NUL bytes reached PostgreSQL text | Confirmed archived worker logs | P2 provider/source ingestion |
| F32 | MCP `list_llm_requests` does not expose the REST endpoint's `call_type`/`status`/`error` filters or render those fields, so batch token totals exclude/unidentify auxiliary failures | Confirmed connector output + source | P2 telemetry/MCP completeness |

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
reconcile” sequence for 21 more LLM calls. Those were not three slow or hung
tools: requests 35-55 contained **21 separate batches and 88 tool calls**. Every
LLM response selected another batch, the tools returned, and the graph invoked
the LLM again.

The loop's stable signature was roughly:

```text
file_exists(output/report.md)
read_file(output/report.md)
search_files(output/report.md, "Mitigation")
search_files(output/report.md, heading/URL patterns)
```

The heading-pattern check repeatedly returned no matches while direct reads
proved the headings existed. The exact sequence explains why:

1. Request 31 wrote the final report successfully. Request 32 immediately proved
   `output/report.md` existed and read all 4,248 characters. The internal object
   key echoed by the write result also led to one failed check of
   `jobs/<job-id>/output/report.md`, but that path confusion did not persist.
2. Request 34 tried to translate the writing check from the
   `verify-before-done` skill into `wc`/`grep`, but called unavailable
   `shell_execute` on the virtual workspace.
3. The fallback used `search_files` with grep-style expressions such as
   `^## [1-3]\.` and `^## `. The virtual backend implements case-folded **literal
   substring** search (`needle in line`), while the SSH backend invokes grep and
   therefore accepts regular expressions. The public tool text says “Text or
   pattern” and does not disclose that backend-dependent semantic split. The
   literal query returned no match even though direct reads showed the headings;
   searches for literal `https://` and `Mitigation` succeeded.
4. `output/manifest_status.json` still said `exists: false`. That file was a
   correct phase-0 boundary snapshot created before the tactical report write.
   It is refreshed only by `_complete_phase_with_git`, after all tactical todos
   complete. The model made a fresh/true manifest part of `todo_8`'s definition
   of done, but completing `todo_8` was itself the action required to reach the
   boundary that refreshes the manifest. This was a circular condition.
5. `verify-before-done` is actively injected at the tail of **every** tactical
   request (`src/graph.py:_inject_transient_messages`), immediately before the
   unchanged active todo list. The next model turn did receive the preceding tool
   results, but the newly injected four-step gate and unchanged `todo_8` caused
   deterministic MiniMax (temperature 0) to restart at “define done / run fresh”
   instead of reconciling and deciding. Requests `93973` onward repeatedly say
   that explicitly.

This was therefore a prompt/graph attractor with two misleading state signals,
not filesystem loss and not a blocked process. The filesystem and each tool call
continued to answer; the model kept choosing another verification batch.

The existing detector could not contain it. It counts identical tool name +
argument fingerprints in a 30-call window, warns at ten, appends a nudge, and
still executes the call (`src/graph.py:4107-4114`, `4424-4467`). Variations in
the multi-tool bundles delayed even that warning. The progress-stall control is
also a reminder, not a stop, and the ordinary job-level ceiling is 5,000 tool
calls. No LLM-call, token, or verification-round ceiling exists.

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

### Priority decision — restore useful completed jobs before optimizing them

The next release gate is one correct, durable, Git-backed Scholar result—not a
lower token count. Fix SSH execution, truthful research tools, final artifact
durability, exact-branch push, MCP readability, and terminal completion first.
Ceremony reduction and runtime ceilings follow once successful-job baselines
exist. A costly job that completes correctly is acceptable during recovery; an
efficient job that produces no usable deliverable is not.

### P0-A — make every start path provision-aware — standard main path accepted

- [x] Stop telling ordinary MCP callers to use the admin manual-assign override.
- [x] Route a workspaceless manual assignment back through the dispatcher.
- [x] Share the sandbox SSH config builder across start/resume and reuse the
  dispatcher's missing-workspace preflight for the admin path.
- [x] Prove automatic provisioning/assignment on five concurrent fresh
  main-cluster project jobs without `assign_job`.
- [ ] Refresh the deployed MCP schema/tool cache and prove revision 2 in live
  acceptance.

### P0-B — validate SSH authentication, not just workspace readiness — main path accepted; targeted resume/local gates open

- [x] Calculate the non-secret private-key fingerprint and prove authorized-key
  parity with a real authenticated command.
- [x] Validate the private key from the actual worker UID and filesystem.
- [x] Require authenticated SSH before Kubernetes publishes workspace readiness;
  independently validate/connect from the worker at initialization.
- [x] Classify deterministic authentication failures separately and fail once
  with a useful backend-specific message.
- [x] Pass the ordinary main-cluster worker-to-workspace path on five fresh
  sandbox jobs.
- [ ] Target the requeue/resume path specifically so restored non-persisted SSH
  identity fields are proven rather than inferred from a fresh dispatch.
- [ ] Stage the projected key into a runtime-owned `0600` identity in local and
  deployed environments, then pass the authenticated k3d workspace handshake.

### P0-C — restore the research-to-Git result path — main web path accepted; paper tools open

- [x] Prove real Tavily/web retrieval from main-cluster agent pods (the web job
  alone completed 155 searches and 26 extracts).
- [ ] Restore k3d external DNS and prove one Tavily request from the local agent
  pod if local acceptance remains a required development gate.
- [ ] Preserve Tavily/provider failures as typed errors instead of successful
  empty result sets.
- [ ] Update the paper-search adapter for the installed arXiv client and prove a
  real query.
- [x] Complete five Scholar reports, commit and push each to its exact assigned
  job branch, and reach the orchestrator-owned terminal state.
- [x] Read all five committed reports back through MCP/Gitea and prove `main`
  was unchanged.

These are the current release blockers. Call count, token count, phase ceremony,
and redundant-but-progressing work are observations during this gate, not reasons
to reject an otherwise correct result.

### P0-D — make successful required artifacts durable and readable — immediate

- [x] Demonstrate that normal finalization exports the natural-language-declared
  report before terminal success on five jobs.
- [x] Read each file from the exact committed job ref through the
  operator-facing MCP surface.
- Refuse terminal success when a declared required deliverable is absent from
  that ref.
- Separate live-workspace and committed-Gitea readers in names and descriptions.
- Do not leak internal object-store prefixes into logical path confirmations.

After normal successful completion passes, extend the same durability contract
to hold, terminal failure, and cancellation, including recovery with provenance
from object storage or audit. That interrupted-work recovery is important, but it
must not delay proving the ordinary success path.

### Deferred follow-up — stop repeated identical execution loops

Tracked in `job_runtime_containment_gap.md`. This is not the immediate
job-recovery gate and must not be implemented as a low ceiling that prematurely
stops legitimate work. Start with shadow telemetry after functional acceptance,
then add an adapt-or-hold policy which preserves required deliverables.

### Deferred optimization — give Scholar a bounded-task path

- Honor explicit no-delegation/source-count/finish-early instructions as hard
  constraints.
- Remove the unconditional five-KB-note + multi-phase plan requirement for small
  tasks.
- Permit 2-4 tactical steps rather than requiring 10-20.
- Avoid enforced skill-read failure turns: inject the required small contract
  before the gated call or make the tool result itself actionable without another
  LLM cycle.

### P2 — repair operator controls and telemetry

- Distinguish preempt from hold.
- Allow job-scoped steering without a pre-existing message thread.
- Populate phase/todo/progress/config for virtual jobs.
- Stop elapsed-time counters at terminal status.
- Include backend, failure stage, retryability, and recovery count in job logs.

---

## 11. Required live acceptance test

After the functional fixes, repeat the same small Scholar task through the
deployed MCP surface—not a unit-only substitute. The recovery gate requires all
of the following:

1. `create_project_job` exposes `required_deliverables=["output/report.md"]`.
2. The documented next action cannot bypass workspace provisioning.
3. A fresh worker pod authenticates to a fresh sandbox workspace; warm-pod
   environment leakage must not be allowed to mask the test.
4. At least one configured research provider returns real source evidence;
   provider/DNS/client failures remain explicit errors rather than empty success.
5. The final report answers the task and respects explicit correctness
   constraints such as requested source count and citation requirements.
6. The report is finalized, committed, and pushed to the
   assigned project job branch.
7. `get_job_file("output/report.md")` returns the committed report and
   `get_workspace_file` has unambiguous live-vs-committed semantics.
8. The job reaches `completed` or `pending_review` without operator intervention.
9. Inspect the project repo/worktree and prove the deliverable landed on the
   intended job branch, not `main`, another job's branch, a nested repo, or an
   unpushed local commit. This is the explicit acceptance gate for the user's
   original wrong-tree concern.

Record time, parent/child LLM calls, token totals, delegation, phase ceremony,
and repeated bundles during this run, but do not fail the functional gate merely
because those numbers are inefficient. Once the report passes the gate above,
use those observations as the baseline for the bounded-Scholar and containment
follow-ups. True operator hold and interrupted-artifact recovery receive their
own acceptance runs; neither replaces this successful-completion proof.

**2026-08-04 gate result:** items 2, 3, 4 (through Tavily/web), 5, 6, 8,
and 9 passed across five jobs. Item 1 remains blocked by the stale connector
schema, and item 7 remains semantically ambiguous because the connector still
describes the committed-repo reader as a live local-workspace reader. The paper
provider half of item 4 failed, but the agent recovered through an authoritative
alternative. See section 14 and
`overnight_minimax_m3_scholar_batch_2026-08-03.md`.

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

---

## 13. Local k3d rerun after P0-A/P0-B (2026-08-03)

The exact bounded RAG task was repeated against the local `k3d-srw` cluster in
namespace `srw`. It used a fresh project job, automatic dispatch, an explicit
`required_deliverables=["output/report.md"]`, `delegation.enabled=false`, and a
temporary smoke-test ceiling of 120 tool calls. No manual assignment was used.

### 13.1 Results

| Attempt | Job | Backend | Outcome |
|---|---|---|---|
| Authenticated sandbox acceptance | `67e4e76b-394d-4b3a-b271-8cd9be69eb14` | `sandbox` | Automatic dispatch provisioned `workspace-67e4e76b-394`; the new handshake rejected the mounted key before worker execution; job `failed` with 0 audit entries |
| Graph/runtime isolation | `77e7e85c-b5e0-4c2f-b1a9-81d6210e0e3c` | `virtual` | Scholar wrote a 569-character report scaffold, then entered a research retry loop; operator cancelled after enough evidence |

The virtual run's complete LLM accounting was:

| Main calls | Light-subagent calls | Prompt tokens | Completion tokens | Total raw tokens | Wall interval |
|---:|---:|---:|---:|---:|---:|
| 55 | 33 | 1,593,463 | 41,174 | **1,634,637** | 15m49s |

This local run did **not** reproduce the original post-final-report loop because
it never obtained the sources needed to finalize the scaffold. It reproduced the
same missing containment at an earlier impossible todo: main calls 37-55 were
`web_search`/`research_topic` retries, including several byte-identical queries,
and every result was empty. Cancellation was requested at 15:04:24 UTC; the
current LLM node completed at 15:04:33 before the graceful stop took effect.
The local Gitea reader returned HTTP 404 for `output/report.md` on both jobs, so
even the successfully written virtual scaffold was not exported to committed
operator-visible state before cancellation.

### 13.2 The SSH fix exposed a local deployment defect

The readiness diagnostics worked as intended. They verified that the private key
existed and parsed, logged only its public SHA256 fingerprint, then attempted an
authenticated `ssh ... true`. The deterministic failure was:

```text
Permissions 0444 for '/run/secrets/vm-ssh-key' are too open.
Load key "/run/secrets/vm-ssh-key": bad permissions
Permission denied (publickey).
```

This is not a key-pair mismatch. `container_provisioner._wait_for_ready` invokes
the OpenSSH client from the orchestrator pod. The local dev orchestrator runs as
root; Kubernetes projects the root-owned secret with `defaultMode: 0444`; and
OpenSSH refuses an identity owned by its own UID when group/world-readable.
`helm/templates/orchestrator/deployment.yaml` already documents this exact dev
case and says the key should be stage-copied to a runtime-owned `0600` file, but
that staging path is not implemented. Production's non-root process may avoid
OpenSSH's owner check on the root-owned file, but acceptance should use a
runtime-owned `0600` copy in both environments rather than rely on that
difference.

This means P0-B's diagnostic/classification code is effective, but its local k3d
deployment acceptance is still red.

### 13.3 The bounded instructions did not override Scholar policy

The first 24 main calls before useful fan-out were almost entirely prescribed
framework work:

- read brief and instructions; inventory tools/documents/reference;
- search the KB and write five notes;
- create `plan.md` and the report scaffold;
- hit the enforced todo-guide rejection/read cycle;
- stage exactly ten tactical todos.

The model explicitly followed the high-salience Scholar strategic template even
though the task said to begin research within three decisions, use at most four
tactical steps, avoid delegation, and finish early. The template says the first
phase normally has 10-20 todos and mandates fan-out for independent questions.

More seriously, `delegation.enabled=false` was present in both the requested and
resolved config, yet `tools.delegation` still contained `spawn_subagent` and the
factory never checks the `enabled` flag. The parent launched three readers. Each
reader ran about ten iterations and reached forced synthesis, adding 33 LLM calls
without providing verifiable sources. A job-level disable must remove the tool
and reject any residual invocation; a prompt prohibition is not an enforcement
boundary.

### 13.4 Research failures were laundered into retryable emptiness

The empty local web results were not valid zero-result searches. A direct probe
inside the same agent pod, without exposing the configured key, showed
`TavilySearch.invoke` returning a dictionary whose only field was `error`, holding
a DNS `ConnectionError`. `_direct_web_search` reads only
`response.get("results", [])`, ignores `response["error"]`, and returns:

```text
No web results found for: <query>
```

The audit consequently marked each infrastructure failure as `success: true`.
CoreDNS logs showed its upstream queries to the k3d Docker gateway timing out:

```text
api.tavily.com A/AAAA -> 172.18.0.1:53: i/o timeout
```

The separate paper-search fallback was also broken. The agent image has
`arxiv==4.0.0`, where `Search.results` no longer exists and results are obtained
through `Client.results(search)`. `src/tools/research/utils/arxiv_client.py`
still calls `search.results()` in search, get, and download paths, producing:

```text
arXiv search error: 'Search' object has no attribute 'results'
```

The tactical todo explicitly required `web_search` evidence, so the model could
not honestly complete it. The framework nevertheless offered no bounded
provider-failure policy, no repeated-result hard stop, and no alternate
completion path. The memory subsystem also retrieved and reinjected ten memories
plus five KB notes on most retry turns, compounding the token cost.

### 13.5 Containment requirements sharpened by the rerun

The deferred containment follow-up should eventually be implemented as a
deterministic runtime policy, not another prompt:

1. Treat a provider response containing `error` as an error. Preserve a stable
   classification such as DNS/auth/quota/timeout; never convert it to a valid
   empty search.
2. Track normalized `(tool, arguments, result, workspace/todo state)` bundles.
   After two or three identical no-progress outcomes, stop that action and give
   the graph one explicit adapt-or-freeze decision. A second failure to adapt
   parks the job.
3. Add independent ceilings for LLM calls, raw tokens, verification rounds, and
   repeated provider failures. A tool-call cap alone does not bound 33 subagent
   LLM calls or large per-turn prompt reinjection.
4. Make cancellation/hold visible before starting another LLM call; retain the
   graceful current-node drain, but do not let auxiliary memory work delay the
   stop decision.
5. Compute deliverable status on demand from the declared contract and the
   authoritative live workspace/Gitea ref. Do not materialize a derived
   phase-boundary status file in the worker workspace.
6. Give `search_files` one backend-independent contract: either literal search
   everywhere (and say so) or an explicit regex flag implemented consistently.
7. Inject `verify-before-done` once when verification begins, or record its
   current gate step in graph state. Do not re-present step 1 at the highest-
   salience prompt position after every tool result.
8. Enforce `delegation.enabled=false` at tool resolution and invocation, not in
   prose.

Before another paid Scholar smoke test, local readiness should additionally
prove external DNS, one simple Tavily query, one arXiv query, the runtime-owned
`0600` SSH key path, and the intended research-tool surface.

### 13.6 Preserved local evidence

The two local job rows, 269 virtual-run audit entries, 88 LLM request records,
the 569-character scaffold write, and both agent pods were left intact. The
sandbox workspace was reaped by the normal lifecycle after the failed readiness
gate. No diagnostic job or audit record was deleted.

### 13.7 Capability-aware verification correction (2026-08-03)

The immediate skill contradiction is corrected in the working tree. Bound job
skills are rendered after backend filtering against the tools that actually
loaded. `verify-before-done` now uses `has_tool("run_command")` to select one of
two procedures:

- shell-capable jobs receive the test/build, `wc`, `grep`, and scripted-analysis
  checks;
- shell-less jobs receive bounded `file_exists` and `read_file` checks, plus the
  available citation check, and no shell command names.

The procedure now requires reconciliation immediately after one fresh evidence
pass. Re-injection of the skill is explicitly not a reason to restart the gate;
successful checks cannot be repeated without an intervening artifact change;
and an unavailable verifier must be reported as a limitation rather than treated
as either an artifact failure or a reason to retry indefinitely. This also
removes the former "one logged tool call per claim" wording, which encouraged
redundant calls even when one result established several criteria.

Regression coverage renders both capability branches directly and exercises the
real `_deploy_instruction_files(loaded_tool_names)` bound-skill path. The focused
suite passed with 50 tests.

This is a prompt-level prevention measure, not the separately tracked runtime
containment boundary. The following framework gaps remain open:

1. Catalog/on-demand skill files are materialized verbatim by
   `skill_files_to_workspace`; only bound job skills consistently receive the
   capability-aware rendering path.
2. Persistent-session bound instructions currently call
   `render_instruction_content(content, [])`, so every `has_tool` branch is
   rendered as unavailable regardless of the session's actual tools.
3. `has_tool` expresses presence only. It cannot distinguish incompatible
   semantics behind the same tool name, such as literal versus regular-expression
   `search_files`; that contract must be normalized or represented as a separate
   capability.
4. A misbehaving model can still ignore the skill. The repeated-bundle detector
   and independent LLM/token/verification ceilings remain tracked in
   `job_runtime_containment_gap.md`, but they do not block the next functional
   recovery run. That run must prioritize producing and preserving a correct
   Git-backed result.

### 13.8 Agent-visible boundary manifest retired (2026-08-03)

The stale status signal identified in section 8 is removed rather than refreshed
more often. The boundary writer, its phase-transition wiring, and its task-brief
instruction were deleted. New briefs explicitly tell workers not to create a
separate manifest/status artifact; `job_complete` continues to validate the live
workspace and the orchestrator's deliverable gate continues to validate committed
Gitea state and stamp the final result in `jobs.context.deliverable_gate`.

Old repositories and resumed/inherited workspaces can still contain a tracked copy
from an earlier worker version. Job startup therefore removes the retired path
before tools are loaded, so it cannot remain visible to the model or become a more
deeply stale signal. The deletion is committed by the normal progress/boundary/final
commit path. Historical copies remain recoverable through Git history.

No production component in this checkout read the boundary file. The only removed
information was a derived convenience snapshot (job, branch, phase, timestamp,
path existence and size). Its authoritative inputs remain available from the job
row and `required_deliverables`; Git-backed point-in-time state is derivable from
the repository ref, and phase timestamps already exist in append-only audit events.
If a phase-history UI is later required, it should store or compute an orchestrator
event keyed by commit SHA, never reintroduce worker-visible bookkeeping.

### 13.9 Continuous skill injection retired (implemented 2026-08-03)

The affected `phase:<name>` implementation did not fire on a phase transition as
its schema and configuration comments claimed. On every LLM request it reread each
matching instruction file and appended a fresh synthetic `read_file` call/result at
the highest-salience end of the prompt. `enforce` was ignored on this path. Thus
Scholar received the full `research-guide` and `verify-before-done` bodies on every
tactical request, while Designer received `design_guide.md` every tactical request
even though that entry says `enforce: true`.

No current binding needs continuous full-document injection. It has been replaced
with two bounded activation forms:

1. `phase_start:<name>` injects an instruction once per concrete phase instance.
   The checkpoint records a key containing phase number, phase kind, and path.
   The legacy `phase:<name>` spelling remains a compatibility alias
   with the same once-only semantics; there is no while-phase mode.
2. `before_tool:<name>` with `enforce: true` remains a passive gate. It gains
   optional phase filtering, phase-instance read scope, and a maximum LLM-turn age.
   A stale or out-of-scope read cannot unlock a later completion action.

`research-guide` and the Designer guide now use one-shot phase-start delivery.
`verify-before-done` now uses a tactical `todo_complete` gate and a strategic
`job_complete` gate, each requiring a read in the current phase instance within 20
LLM turns. The ordinary todo guide remains a job-scoped passive gate: its stable
planning procedure need only be read once per worker run.

This is distinct from dynamic per-turn context such as the live todo list, memory,
and supervisor guidance. Those are state, not skills. A future repeated reminder
must be a separately named, size-bounded feature with an interval and injection
cap; it must not silently reuse a full `SKILL.md` binding.

Implementation status: `InstructionFileEntry` now carries `phases`, `read_scope`,
and `max_read_age_turns`; `ToolContext` stamps instruction reads against the
current phase instance and LLM turn; enforcement wrappers evaluate those values
at tool invocation time; and worker state checkpoints one-shot phase-injection
keys. Worker, Scholar, Product QA, and Designer bindings have been migrated; the
interactive Designer's unimplemented `on_setup` trigger is now a
`before_tool:write_file` read gate. Both runtime/Cockpit schemas describe the
bounded contract.

## 14. Main-cluster overnight acceptance (2026-08-03/04)

Five project-scoped MiniMax-M3 Scholar jobs were scheduled through the live MCP
after the preceding corrections. The full per-job ledger, task text, report
assessment, requests, and citation counts are preserved in
`overnight_minimax_m3_scholar_batch_2026-08-03.md`.

### 14.1 Functional result

| Variant | Job | Result | Branch | Required report |
|---|---|---|---|---|
| no-delegation control | `66e5878c-3968-4e43-bd1d-9eaf2a97d315` | `completed` | `job/66e5878c` | present |
| light readers, 10 iterations | `cb847a4b-b315-4a55-9387-8e28e2229b48` | `completed` | `job/cb847a4b` | present |
| light readers, 24 iterations | `96bb50c2-51d3-4a6b-ac39-e808582d389c` | `completed` | `job/96bb50c2` | present |
| paper-provider isolation | `44d67053-d203-4a66-a9f6-3e4d140567f6` | `completed` | `job/44d67053` | present |
| current web/platform research | `90c74b6a-f69d-4a58-afa5-9b93c4c71877` | `completed` | `job/90c74b6a` | present |

All five:

- queued through automatic dispatch without `assign_job`;
- began LLM work within the same 20-second interval, proving concurrent worker
  capacity;
- passed the ordinary main-cluster sandbox/SSH path;
- produced substantive 23.8k–90.6k-character reports;
- committed and pushed to their exact isolated job branches;
- left project `main` and the other four job branches free of their report; and
- were read back through the Git-backed MCP file API.

This closes F13 for the fresh main-cluster path: the original wrong-tree,
nested-repository, unpushed-local-commit, and cross-job-clobber concerns did not
recur. It also isolates F14 as a k3d development-runtime/key-projection issue,
not evidence that the main cluster currently cannot authenticate.

The acceptance is not server-contract complete. The jobs named their reports in
task prose; the live connector still omits `required_deliverables`. A final
contract run must create a new job through the refreshed schema and prove the
orchestrator refuses terminal success when that Gitea path is absent.

### 14.2 Research and delegation defects still live

The main web path did not reproduce the local Tavily/DNS failure. The five jobs
contained no empty-result/DNS/connection error, and the web job successfully
issued 155 searches plus 26 extracts. Provider error laundering still warrants
its source fix, but local DNS is not a main-cluster release blocker on this
evidence.

The paper job reproduced F17 in the deployed environment: four arXiv searches
and two arXiv lookups failed on `Search.results()`, while three Semantic Scholar
searches returned HTTP 403. The Scholar adapted through web retrieval of arXiv
pages and disclosed the limitation. The report's success is evidence for agent
resilience, not for paper-tool correctness.

F15 is also reconfirmed. In both jobs configured with
`delegation.enabled=false`, the bound LLM tool definitions still contained
`spawn_subagent`. Neither model invoked it, but only because it followed the
text instruction. The enabled jobs spawned 14, 23, and 66 reader LLM calls and
all readers returned naturally; no reader hit its configured iteration/token/
time cap. This batch therefore does not support replacing the light readers or
raising their default cap. It supports enforcing the existing `enabled` flag at
tool resolution and invocation.

### 14.3 Completion overhead after the report was already safe

The batch made 628 LLM requests (525 parent and 103 reader), consumed
58,774,707 raw prompt+completion tokens, and emitted 1,536 tool calls. Those
figures were not functional acceptance limits. They are now a successful-job
baseline.

The retired manifest and continuous skill injection stayed retired. A new,
narrower transition problem remained:

- transition todo 1 requires a Git-backed review and quality confirmation;
- transition todo 2 says its stop condition comes first, but allows completion
  only after todo 1 confirmed quality; and
- the active todo bodies are injected every turn.

The 10- and 24-turn agentic-RAG jobs therefore spent 62 and 53 parent rounds,
respectively, between the tactical-complete commit and completing transition
todo 1. The two windows consumed 17.93M raw tokens and 360 tool calls, dominated
by unchanged `file_exists`, `git_tags`, `git_diff`, `search_files`, and
`list_files` results. Tool calls succeeded; the models repeatedly announced
that they would check the stop condition first and review second. Both escaped
immediately after context compaction reduced the next prompt from roughly
200k-plus to roughly 20k tokens.

This is live confirmation of P-2 in
`phase_model_overhead_amnesia_loop.md`, not a resurrection of the deleted
manifest. The fast path should reconcile an orchestrator/Git artifact fact with
completed todos, then review/complete in dependency order. It should not ask the
model to perform a prerequisite “first” while stating that the prerequisite
depends on a different pending todo.

The action-gated verification skill also rejected 50 completion calls across the
five jobs before the model performed a valid phase-scoped skill read. The gate
remained safe and was not continuously injected, but the current corrective
error is not sufficient for this model. Improve the recovery interaction without
weakening the proof requirement.

### 14.4 Archived worker-log findings

The audit-store trace did not contain every subsystem failure. A full
WARNING/ERROR sweep of the five archived pod logs found:

- 380 CitationEngine auto-embed failures across 359 unique sources (374
  deterministic batches over the backend maximum of 64, plus five non-finite
  vectors and one overload response);
- 138 contained project-memory retrieval deadlocks, five TTL deadlocks, four
  TTL timeouts, and 17 failed retrieval-message writes while the five jobs
  shared one project memory scope;
- 94 auxiliary structured-output validation failures and ten explicit
  MiniMax-M3 degraded/recovered cycles;
- 15 missing-checkpoint warnings—three phase snapshots per job had no
  `checkpoint.db`;
- seven rejected phase-tag pushes across two jobs after a local force-move of an
  existing remote tag;
- one web extractor response-shape exception; and
- two PDF source registrations rejected because NUL bytes reached a PostgreSQL
  text field.

These failures were non-fatal by design or accident; all reports still
completed. They nevertheless mean that terminal job success is not a health
signal for memory, source indexing, auxiliary extraction, snapshot recovery, or
phase-tag evidence.

Dedicated issue records:

- `project_scoped_memory_deadlocks_under_parallel_jobs.md`;
- `embedding_batch_overflow_skips_citation_source_embeddings.md`;
- `minimax_m3_auxiliary_structured_output_flaps.md`; and
- `phase_boundary_tags_are_moved_then_rejected_by_remote.md`.

The MCP-visible 58.77M raw-token figure covers the rendered main/light-reader
request list and is a lower bound. The connector does not pass through or render
the REST request-list fields needed to identify and total the auxiliary calls
that produced the log failures.

### 14.5 Updated priority boundary

The ordinary main-cluster job path now produces durable results. Remaining work
should be ordered as follows:

1. **Functional:** refresh the MCP schema and prove a real
   `required_deliverables` contract; enforce delegation disablement; repair the
   arXiv and Semantic Scholar adapters.
2. **Shared context/evidence:** eliminate project-memory deadlocks and
   per-consumer TTL corruption, split embedding batches, make source-index
   coverage visible, and keep phase tags immutable/exact.
3. **Auxiliary/provider quality:** route structured tasks to a compatible model,
   expose auxiliary accounting, and normalize web/PDF ingestion failures.
4. **Efficiency:** implement the conditional transition fast path and improve
   passive skill-gate recovery; then tune Scholar/source budgets.
5. **Containment:** add shadow no-progress telemetry using this batch as the
   successful baseline before arming holds or ceilings.

The local k3d `0444` projected-key issue can remain deferred unless local
sandbox acceptance itself becomes a required release gate. It no longer blocks
the main-cluster correctness result.
