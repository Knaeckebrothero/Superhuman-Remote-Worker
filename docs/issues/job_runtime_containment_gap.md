---
tags:
  - issue
  - jobs
  - agent-runtime
  - reliability
  - containment
  - cost
related:
  - "[[mcp_scholar_smoke_test_dispatch_ssh_overhead_and_stranded_deliverable]]"
  - "[[agent_phase_guardrails_burn_legitimate_work]]"
  - "[[phase_model_overhead_amnesia_loop]]"
  - "[[dev_snapshot_ssh_key_perms_0444]]"
aliases:
  - job no-progress circuit breaker
  - insufficient runtime containment
  - repeated tool-result containment
---

# Job runtime containment is warning-only and does not bound no-progress loops

**Filed:** 2026-08-03, separated from the Scholar MCP smoke-test incident.

**Status:** **OPEN — MAIN-CLUSTER SCHOLAR SUCCESS PATH PASSED; STAGE A
BASELINE CAPTURED.**

**Priority decision:** containment is not the next job-recovery release. SRW must
first prove that an ordinary job can start, use its required tools, produce a
correct deliverable, commit and push it to the assigned branch, expose it through
the operator surface, and reach a valid terminal state. During that recovery
period, a job that uses more calls than ideal but finishes correctly is
acceptable. A cheap or aggressively bounded job that fails to produce a useful
result is not.

This issue preserves the containment gap without allowing it to displace the
functional blockers. It should be picked up after the Git-backed acceptance gate
in the originating incident passes, unless an unbounded loop itself makes that
acceptance impossible.

**Update 2026-08-04:** the fresh main-cluster Git-backed gate passed for five of
five overnight Scholar jobs. Every report completed and remained durable. The
batch supplies the first successful Stage A baseline and one clear no-progress
episode, described in section 8. MCP schema refresh, delegation disablement, and
the paper-provider repairs remain ahead of armed containment because they are
still functional/contract defects. Shadow-mode episode telemetry can now begin
without imposing a low ceiling on the working path.

## 1. Problem statement

The worker runtime can notice some repetitive behavior, but it cannot reliably
contain a model that keeps selecting tool calls which return the same result
without advancing workspace, todo, citation, source, Git, or deliverable state.
The current controls are predominantly warnings and prompt nudges. They depend on
the same model that is already failing to reconcile the evidence.

Two smoke runs demonstrated the gap:

1. The remote virtual Scholar completed `output/report.md`, then spent 21 more
   main-model rounds issuing 88 successful verification tool calls against the
   unchanged report.
2. The local virtual Scholar encountered broken research providers, repeated
   empty/failed research from main calls 37–55, spawned 33 additional subagent
   calls despite `delegation.enabled=false`, and consumed 1,634,637 raw tokens
   before operator cancellation.

These were not hung tool processes. The LLM repeatedly received results and
chose another round. Small changes in query strings and multi-tool bundle shape
prevented the existing fingerprint warning from recognizing one stable
no-progress episode early enough.

## 2. Why this is not the immediate fix

Containment limits the blast radius of a failure; it does not make a broken job
capable of succeeding. In the local run, the model's research todo was genuinely
impossible because:

- k3d external DNS/Tavily failed;
- the provider error was mislabeled as a successful empty result;
- `search_papers` called an API removed by installed `arxiv==4.0.0`;
- the sandbox path could not authenticate with the projected SSH key; and
- an interrupted virtual deliverable never became a committed Gitea artifact.

A tighter call or token ceiling would only have made those jobs fail sooner. It
would not have produced the requested report. Similarly, freezing on repeated
reads before reliable progress signals exist risks stopping long but productive
jobs—the failure mode already documented in
`agent_phase_guardrails_burn_legitimate_work.md`.

The correctness-first recovery gate is therefore:

1. Stage the SSH identity as a runtime-readable, runtime-owned `0600` file and
   pass a fresh worker-to-workspace authenticated handshake.
2. Restore truthful research tools: external DNS/Tavily readiness, error
   propagation, and the arXiv 4 client contract.
3. Ensure required deliverables survive finalization, interruption, and the
   selected backend's export path.
4. Commit and push the report to the exact assigned job branch—never `main`, an
   unrelated branch, a nested repository, or an unpushed local commit.
5. Read the committed report through the documented MCP/Gitea surface and reach
   `completed` or `pending_review` normally.
6. Refresh the deployed MCP schema and prove the job was created with the
   declared `required_deliverables` contract.

Only after this path works should call-count and token efficiency become the
primary optimization target.

## 3. Current containment gap

The runtime lacks a single authoritative, job-wide definition of “progress.” In
particular:

- repeated-call detection does not combine normalized tool name, arguments,
  result, and durable state changes;
- warning thresholds still allow the repeated tool call to execute;
- equivalent bundles evade detection through ordering or small argument changes;
- there is no independent verification-round or provider-failure budget;
- main-agent limits do not effectively bound child/subagent LLM consumption;
- ordinary tool-call budgets do not bound expensive no-tool reasoning turns;
- graceful cancellation can wait for the current LLM node and auxiliary work;
  and
- stopping a virtual job does not guarantee required deliverables have been
  exported to an operator-readable durable ref.

The result is not merely inefficiency. Once the model enters a stable attractor,
there is no deterministic component with authority to say: the evidence has not
changed; choose a materially different action or yield to the operator.

## 4. Guardrail requirements for the later implementation

Containment must be designed to preserve useful work and avoid becoming another
source of non-completion.

### 4.1 Observe before enforcing

First ship the detector in shadow/telemetry mode. Record candidate no-progress
episodes without changing execution, then evaluate them against completed and
failed jobs. The detector should consider at least:

- normalized tool name and arguments;
- normalized result or stable error classification;
- required-deliverable content/version changes;
- workspace writes and Git commits;
- todo state transitions;
- newly registered or verified sources/citations;
- plan changes that materially alter the next action; and
- parent plus child-agent activity.

Repeated calls alone are not proof of a loop. Pagination, polling, iterative
tests, long-running data collection, and independent source checks can be
legitimate. The signal is repeated equivalent outcomes **without relevant state
change**.

### 4.2 Escalate instead of immediately killing

After calibration, use a staged response:

1. On the first detected episode, return one structured observation naming the
   repeated action, result, and unchanged state.
2. Give the model one explicit adapt decision: choose a materially different
   method, complete with a stated limitation, or request operator input.
3. If the same no-progress episode resumes, enter a real operator hold rather
   than silently rewinding todos or declaring the task failed.
4. Before holding, snapshot required deliverables and push any safe progress to
   the authoritative job branch or recovery store.

The orchestrator remains authoritative for final status. Runtime code should
produce typed freeze/hold data and `should_stop`; it must not invent a terminal
job state locally.

### 4.3 Treat budgets as emergency ceilings, not success criteria

Later ceilings may cover:

- main and child LLM calls;
- raw and billed tokens;
- repeated provider failures;
- verification rounds; and
- wall-clock duration.

Defaults should initially be generous and role/task aware. They must aggregate
subagent usage, be visible in telemetry, and support an operator override. Hitting
a ceiling means “needs review with work preserved,” not “the deliverable is
incorrect.” A budget is not a substitute for repairing tool availability,
workspace durability, or completion logic.

## 5. Explicit non-goals during functional recovery

Until the end-to-end job gate passes, do **not** use this issue to:

- shrink Scholar phases or source work solely to reduce tokens;
- lower call limits until ordinary successful-job baselines exist;
- freeze on repeated reads without checking whether the artifact changed;
- convert provider errors into empty results to keep the graph moving;
- mark partial output complete merely because a budget expired;
- remove verification needed for correctness; or
- declare success because a job failed quickly and cheaply.

Some overhead changes may still be necessary when they directly prevent
completion—for example, removing a circular manifest dependency or per-turn
skill reinjection. Their acceptance criterion is restored completion, not token
savings.

## 6. Deferred implementation sequence

### Stage A — after functional acceptance

- Capture successful baselines for at least a small Scholar report, a coding
  change, and a longer multi-source job.
- Add shadow-mode no-progress episode telemetry with parent/child usage totals.
- Verify that productive repeated operations are not classified as loops.

### Stage B — narrow deterministic containment

- Block only exact/equivalent repeated tool-result bundles with no relevant state
  change.
- Provide one adapt-or-hold turn.
- Preserve required deliverables before hold.
- Expose the episode and thresholds to operators.

### Stage C — broader budgets and tuning

- Add configurable job-wide LLM/token/provider/verification ceilings.
- Aggregate subagent consumption.
- Tune by expert and task shape using successful-job distributions.
- Add operator override and resume semantics that do not discard todos or work.

## 7. Acceptance criteria

This issue is complete only when all of the following hold:

1. A synthetic unchanged-result loop is contained after the configured staged
   response, with a precise diagnostic.
2. A long but state-advancing job is not stopped by the same policy.
3. Parent and subagent usage participate in one job-wide accounting boundary.
4. Provider errors remain errors and are classified consistently.
5. Required deliverables remain readable while held and after cancellation.
6. The job enters a real hold/review state and can resume without losing todos,
   workspace changes, or the assigned Git branch.
7. Operators can see the threshold, current usage, repeated episode, and last
   meaningful state change.

These tests come **after** the originating Scholar acceptance job produces and
pushes a correct report. Containment is a resilience follow-up, not a replacement
for making jobs work.

## 8. Successful overnight baseline and a containable transition episode (2026-08-04)

Five concurrent MiniMax-M3 Scholar jobs completed on the main cluster. They made
628 LLM requests (525 parent, 103 light-reader), consumed 58,774,707 raw
prompt+completion tokens, and issued 1,536 tool calls. All five requested
reports were committed and pushed to their isolated job branches; project
`main` was unchanged. The detailed ledger is
`overnight_minimax_m3_scholar_batch_2026-08-03.md`.

This result validates the correctness-first sequencing in this issue. A hard
ceiling modeled on the earlier 1.63M-token cancelled run would have killed every
one of these successful jobs. Even the smallest successful control consumed
7.24M raw tokens. Raw-token volume alone is therefore not a safe progress
classifier for this model/runtime.

The same batch provides a good candidate for shadow no-progress detection. Two
matched agentic-RAG jobs had already committed their final reports and completed
their tactical todos, then spent 62 and 53 parent rounds in the final strategic
review. Their tools repeatedly returned unchanged successful file/Git evidence.
Together those two review windows consumed 17.93M raw tokens and 360 tool calls.
Both jobs eventually escaped after context compaction and completed normally.

The episode has the properties a later detector should observe without acting
yet:

- required report path and Git HEAD were unchanged and already successful;
- todos did not advance;
- normalized tool families repeated (`file_exists`, `search_files`,
  `list_files`, `git_tags`, `git_diff`, `git_log`);
- results stayed equivalent even when arguments/order varied;
- the model repeatedly stated the same intended next step; and
- no provider outage or missing capability made further research necessary.

The immediate causal fix belongs to the phase issue: remove the circular
transition ordering and add the conditional REVIEW-AND-ADAPT fast path. The
containment issue should still learn from the episode. In shadow mode it should
have emitted one episode spanning equivalent bundles and attributed the last
durable progress to the tactical-complete/report commit. It should **not** have
failed or cancelled the job.

Stage A is therefore partially satisfied for Scholar reports:

- [x] multiple successful small/medium research baselines;
- [x] parent and reader raw-usage accounting;
- [x] one long-but-productive trajectory and one post-progress repetition
  episode to compare;
- [ ] a successful coding-change baseline;
- [ ] shadow normalized-bundle/state-change telemetry; and
- [ ] a true hold/resume preservation exercise.

Do not tune a production ceiling directly from n=5. First repair the transition
prompt and repeat at least one matched job; otherwise the detector would be
calibrated to an avoidable prompt bug rather than the residual runtime risk.
