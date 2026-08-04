---
tags:
  - issue
  - jobs
  - scholar
  - minimax
  - smoke-test
  - delegation
  - research
related:
  - "[[mcp_scholar_smoke_test_dispatch_ssh_overhead_and_stranded_deliverable]]"
  - "[[job_runtime_containment_gap]]"
  - "[[delegation_light_mode_missing]]"
  - "[[project_scoped_memory_deadlocks_under_parallel_jobs]]"
  - "[[embedding_batch_overflow_skips_citation_source_embeddings]]"
  - "[[minimax_m3_auxiliary_structured_output_flaps]]"
  - "[[phase_boundary_tags_are_moved_then_rejected_by_remote]]"
aliases:
  - overnight Scholar batch 2026-08-03
---

# Overnight MiniMax-M3 Scholar batch — 2026-08-03

**Status:** **AUDITED 2026-08-04 — 5/5 COMPLETED WITH GIT-BACKED REPORTS.**

The functional recovery result is positive: all five jobs automatically
dispatched on the main cluster, ran concurrently through the ordinary sandbox
path, produced substantive reports, committed and pushed them to their exact
job branches, and reached `completed` without operator intervention. The shared
project `main` branch was unchanged. This is the first successful multi-job
acceptance evidence following the 2026-08-03 dispatch/workspace corrections.

The batch also exposed three front-door functional defects and two expensive
but non-blocking behaviors:

- `delegation.enabled=false` still does not remove `spawn_subagent` from the
  model tool surface;
- the deployed paper adapters were broken (`arxiv==4.0.0` API mismatch and
  Semantic Scholar HTTP 403), although the Scholar recovered through web search;
  the source repair is now implemented, a replacement Semantic Scholar key has
  been requested, and the optional provider's deployment acceptance is deferred
  until that credential arrives;
- the connector still publishes the stale MCP creation/file-reader schema;
- the phase-transition review prompt caused two completed reports to spend
  another 53 and 62 parent LLM rounds reconciling already-successful evidence;
- the new passive `verify-before-done` gate is fail-closed, but MiniMax ignored
  its corrective error often enough to produce 50 rejected completion calls.

The latter two are overhead/containment findings, not artifact-loss findings.
They must not obscure that every requested result was completed and preserved.

Five project-scoped Scholar jobs were created through the live SRW MCP at
approximately 20:36 UTC on 2026-08-03. They use the existing
`Research RAG technologies` project
(`becb5a96-1d8d-4916-a2e2-755dfd86cb3a`) so each job should receive an isolated
project job branch. Every LLM tier was explicitly overridden to `MiniMax-M3`:
base, strategic, tactical, summarization, and subagent.

The purpose is correctness and diagnostic evidence, not cost reduction. A job
that performs redundant work but produces and commits a good report is a useful
result. Do not judge this batch against a tight call or token budget.

## Jobs

| Variant | Job ID | Required output named in the task | Primary observation |
|---|---|---|---|
| No-delegation control | `66e5878c-3968-4e43-bd1d-9eaf2a97d315` | `output/overnight-rag-evaluation-control.md` | Does `delegation.enabled=false` actually remove and reject `spawn_subagent`? |
| Light readers, 10 iterations | `cb847a4b-b315-4a55-9387-8e28e2229b48` | `output/overnight-agentic-rag-subagents-10.md` | Three-reader fan-out using the existing default-sized reader budget |
| Light readers, 24 iterations | `96bb50c2-51d3-4a6b-ac39-e808582d389c` | `output/overnight-agentic-rag-subagents-24.md` | Same research task with larger reader iteration/token/time budgets |
| Paper-search isolation | `44d67053-d203-4a66-a9f6-3e4d140567f6` | `output/overnight-rag-literature-2024-2026.md` | Real `search_papers`/arXiv behavior and fallback honesty |
| Current web/platform research | `90c74b6a-f69d-4a58-afa5-9b93c4c71877` | `output/overnight-rag-platform-comparison.md` | Tavily/DNS/provider behavior, official-source retrieval, and useful synthesis |

All five creation responses said:

> Dispatch: Queued for automatic workspace provisioning and agent assignment.
> Monitor the job status; manual assignment is only an administrative override.

The first follow-up at approximately 20:37 UTC found all five with zero audit
entries. The no-delegation control then transitioned to `processing`; the other
four remained `created` and queued. This proves automatic dispatch selected the
first job without `assign_job`. At 20:38:30 UTC it emitted its first two
`initialize` audit entries, proving that worker execution began; no LLM request
had been issued at the last observation. Two pre-existing jobs were already
processing, so the cluster may provision or queue the remaining work overnight.

## Variant configuration

The no-delegation control and paper-search job set:

```yaml
delegation:
  enabled: false
```

The two matched agentic-RAG jobs use the same research question and request
exactly three independent readers in one parent turn. Their relevant differences
are:

| Setting | 10-turn job | 24-turn job |
|---|---:|---:|
| `max_iterations` | 10 | 24 |
| `max_tokens` | 40,000 | 100,000 |
| `timeout_seconds` | 240 | 480 |
| `max_parallel` | 3 | 3 |
| `allow_writes` | false | false |

The platform comparison uses an intermediate light-reader budget of 16
iterations, 70,000 tokens, and 360 seconds.

## Live MCP contract caveat

The connector's visible creation schema was still stale when this batch was
created:

- `create_project_job` did not expose `required_deliverables`;
- `create_job` still claimed that a caller must manually assign the created job;
- the actual mutation response correctly described automatic provisioning and
  dispatch.

Consequently, the five output paths above are strong task instructions but were
not proven to be server-side `required_deliverables` contracts. Tomorrow's audit
must not equate terminal `completed` with artifact success; it must read each
named file from the authoritative Git ref.

## Inspection checklist for 2026-08-04

For every job:

1. Record terminal status, start/end time, parent LLM calls, subagent LLM calls,
   raw tokens, tool calls, phases, and final error or completion payload.
2. Read the exact named output through MCP/Gitea and assess whether it actually
   answers the task with traceable citations.
3. List commits on the assigned job branch and prove the output is committed and
   pushed there—not only present in a virtual workspace or audit argument.
4. Confirm the shared project `main` branch and unrelated job branches were not
   changed by another job's deliverable.
5. Check whether any job completed without its named report, remained in
   `created`, failed during SSH/workspace setup, or stopped in review.

For the no-delegation control:

- search audit/LLM requests for `spawn_subagent`;
- verify the tool and Scholar fan-out guidance were absent from the live prompt;
- treat any successful invocation as confirmation that
  `delegation.enabled=false` is still unenforced in the deployed runtime.

For the 10-vs-24 reader comparison:

- count reader loops and cap-triggered forced-synthesis calls per reader;
- distinguish a useful partial synthesis from a result with no verifiable
  evidence;
- compare report correctness, source quality, completion rate, elapsed time,
  parent calls, and child calls;
- do not conclude that the larger cap helped if both variants faced the same
  broken provider response.

For paper and web research:

- identify real arXiv client errors separately from valid zero-result searches;
- identify DNS/auth/quota/timeout/provider errors separately from valid empty
  Tavily results;
- verify the agent adapts to an alternate source path without fabricating
  references.

Finally, update the originating smoke-test incident with the batch outcome and
leave these records intact until the audit is complete.

## Audit result — 2026-08-04

### Execution outcome

All five jobs moved through automatic dispatch without `assign_job`. Their first
LLM requests arrived within a 20-second window (`20:40:24Z`–`20:40:43Z`), which
also proves the cluster ran the batch concurrently rather than serially parking
four jobs behind the first.

| Variant | Terminal state | Created-to-updated | Audit entries | Parent LLM calls | Reader LLM calls | Raw tokens | Tool calls |
|---|---|---:|---:|---:|---:|---:|---:|
| No-delegation control | `completed` | 66m 52s | 606 | 99 | 0 | 7,243,169 | 174 |
| Light readers, 10 iterations | `completed` | 78m 03s | 953 | 119 | 14 | 11,446,108 | 450 |
| Light readers, 24 iterations | `completed` | 78m 02s | 731 | 111 | 23 | 15,456,888 | 293 |
| Paper-search isolation | `completed` | 72m 59s | 636 | 109 | 0 | 10,305,374 | 171 |
| Current web/platform research | `completed` | 80m 02s | 524 | 87 | 66 | 14,323,168 | 448 |
| **Batch total** | **5/5 completed** | | **3,450** | **525** | **103** | **58,774,707** | **1,536** |

“Raw tokens” is the sum of provider-reported prompt and completion tokens in the
MCP-visible main/light-reader request list. It is useful for comparing those
trajectories, but is not a claim about billed tokens or price. Archived logs show
additional memory extraction/assembly LLM work that this MCP view does not make
identifiable or totalable because it omits `call_type`, `status`, and `error`.
The 58.77M figure is therefore a lower bound on complete job-wide LLM usage. The
batch deliberately had no tight cost acceptance criterion.

### Phase shape

Every job used the same three logical phases:

1. strategic planning and scope;
2. tactical research and report production; and
3. strategic review and submission.

| Variant | Planning todos | Tactical todos | Closeout todos | Logical phases |
|---|---:|---:|---:|---:|
| Control | 5 | 5 | 2 | 3 |
| 10-turn readers | 5 | 11 | 2 | 3 |
| 24-turn readers | 5 | 8 | 2 | 3 |
| Papers | 5 | 6 | 2 | 3 |
| Web | 5 | 7 | 2 | 3 |
| **Mean** | **5.0** | **7.4** | **2.0** | **3.0** |

This is the intended plan → execute → review/submit shape and was appropriate
for these bounded one-report jobs. All five completed without needing another
logical execution phase. The former model of roughly twenty phases with only a
few todos each would have multiplied planning, archive, Git-tag, snapshot,
skill-gate, and context-reconciliation work without evidence of improved report
quality.

Mechanical phase-completion accounting is slightly higher: the 24-turn and web
jobs each archive-committed tactical phase 1 twice. There were therefore 17
completion events across 15 logical phases, or 3.4 events per job. Those two
extra events were duplicate finalization, not useful new phases, and caused the
stale/rejected phase-tag defect recorded separately.

The remaining phase problem is inside the boundaries, not the number of them.
The generic five-todo opening still contains ceremony, and the contradictory
two-todo closeout caused the measured 53/62-round loops. Keep three phases as
the bounded-job default; add a phase only for a material new milestone, plan
revision, external review, or loss of coherent execution scope—not after an
arbitrary number of completed todos.

### Git-backed artifact acceptance

The exact file named in each task is readable through `get_job_file` from the
job's committed Gitea ref. The final `Job completed` commits are:

| Variant | Assigned branch | Final commit | Report characters / words | Result |
|---|---|---|---:|---|
| Control | `job/66e5878c` | `9612fd72` | 34,600 / 4,595 | exact report present |
| 10-turn | `job/cb847a4b` | `911a495d` | 46,415 / 5,383 | exact report present |
| 24-turn | `job/96bb50c2` | `967ddf0c` | 65,212 / 8,322 | exact report present |
| Papers | `job/44d67053` | `a7031b15` | 23,808 / 3,330 | exact report present |
| Web | `job/90c74b6a` | `6521997f` | 90,619 / 11,290 | exact report present |

For each job, the `output/` tree on its branch contains its own named overnight
report and not the other four reports. An explicit `ref="main"` browse showed
only the pre-existing `output/overview.md`; reading any of the five overnight
paths from `main` returned not found. Audit-side `git_status`/`git_log` checks
reported clean branches and origin parity. This closes the original
wrong-branch/unpushed-deliverable acceptance question for this batch.

Two MCP caveats remain:

1. These files were declared in natural-language task text because the live
   `create_project_job` schema still did not expose `required_deliverables`.
   Therefore the run proves worker/Git correctness, but not the server-enforced
   missing-deliverable gate.
2. `list_job_commits` documents/default-labels its ref as `main`, while the API
   implementation remaps the default to the stored job branch. Explicit branch
   refs were used above. The operator description should not imply that a
   job-branch history is a `main` history.
3. Two jobs force-moved an already-pushed tactical boundary tag locally. Seven
   later `git push --tags` attempts were rejected because the remote tag already
   existed. Branch commits and reports were safe, but the remote phase tag is an
   earlier boundary than the final branch history. See
   `phase_boundary_tags_are_moved_then_rejected_by_remote.md`.

### Delegation experiment

`delegation.enabled=false` remains unenforced at tool resolution. The first
LLM request for both the control and paper jobs still listed
`spawn_subagent` among the bound tool definitions. Neither model called it,
because both obeyed the prose prohibition, but model compliance is not a
capability boundary. A future model can still violate the flag exactly as the
local k3d Scholar did.

The enabled variants behaved normally:

| Job | `spawn_subagent` calls | Returned status | Reader LLM requests | Cap/forced-synthesis evidence |
|---|---:|---|---:|---|
| 10-turn | 3 | 3 × `[subagent done]` | 14 | none; all three stopped naturally |
| 24-turn | 3 | 3 × `[subagent done]` | 23 | none; all three stopped naturally |
| Web | 8 | 8 × `[subagent done]` | 66 | none; all eight stopped naturally |

The terminal reader requests contain no injected “You have reached your time
limit/token budget/iteration limit” synthesis prompt. The 10-turn readers made
only 14 requests in aggregate; an iteration-capped reader would require 10 loop
requests plus a forced-synthesis request before counting the other two readers,
so none could have exhausted the iteration cap in this run.

The matched 10-vs-24 result does **not** justify replacing light readers with
full jobs or raising every reader cap:

- both variants completed in essentially the same 78-minute wall time;
- both produced useful, contract-shaped reports;
- the 24-turn report was deeper and used CitationEngine more consistently, but
  the model naturally chose 23 reader calls rather than being rescued from a
  10-turn cutoff;
- only about 412k of the 4.01M-token total difference was reader-token
  consumption (approximately 337k vs 749k); most of the difference was in the
  parent trajectory, especially the final strategic review; and
- the 10-turn job actually made more parent calls and tool calls because it
  entered the longer file/Git reconciliation loop.

The correct immediate change remains: make `enabled=false` remove the tool and
reject invocation. Keep the existing configurable light-reader budgets; obtain
more matched runs before choosing a larger default.

### Research-provider behavior

The main-cluster Tavily/web path was healthy in this batch. No job recorded
`No web results found`, DNS, `ConnectionError`, or equivalent provider-error
text. The web comparison alone made 155 `web_search` and 26
`extract_webpage` calls and still completed. The k3d Tavily/DNS failure was
therefore local-development-specific or transient; its error-propagation fix is
still desirable, but it was not reproduced here.

The paper path was broken in the deployed batch image:

- four arXiv `search_papers` calls returned
  `'Search' object has no attribute 'results'`;
- both `get_paper_info` calls failed through the same removed arXiv API; and
- three Semantic Scholar searches returned HTTP 403.

The paper Scholar recognized the provider failures, recorded them, switched to
`web_search`/`extract_webpage` against arXiv pages, and produced a 20-paper-link
report. Its methodology note discloses the fallback and reduced confidence.
That is good agent recovery, but it did not make the deployed paper tools
healthy at the time. The source adapter is now updated as described below. Its
deployment remains to be exercised, while Semantic Scholar credential
acceptance is optional and deferred rather than a job-completion blocker.

#### Paper-provider remediation implemented 2026-08-04

The two failures are independent, and the source-side repair is now in this
checkout:

- `src/tools/research/utils/arxiv_client.py` owns one reusable
  `arxiv.Client(delay_seconds=...)` and calls `Client.results(search)` for
  search, metadata lookup, and download. `requirements.txt` now constrains the
  reviewed contract to `arxiv>=2.1.0,<5.0.0`.
- The arXiv unit tests no longer invent `Search.results()` on a `MagicMock`.
  They assert calls through `Client.results(search)` and include a contract test
  against the actually installed package. An isolated `arxiv==4.0.0` runtime
  then completed a real one-result arXiv search through the repaired wrapper.
- `src/tools/research/utils/semantic_scholar_client.py` is now the single
  Semantic Scholar transport for both direct paper tools and the combined
  research workflow. It classifies 401/403 as non-retryable authentication,
  429 as throttling, connection failures separately, and provider 5xx responses
  as retryable availability errors. It never includes a credential or raw
  provider body in its agent-facing error.
- `get_paper_info` no longer translates every Semantic Scholar HTTP error into
  “not found.” It discloses the provider failure and still uses arXiv metadata
  for arXiv identifiers. `research_topic` likewise includes provider warnings
  while preserving results from the healthy provider.
- Cached, secret-free paper-provider state is exposed on agent status. The
  actual worker image has an explicit acceptance probe:

  ```bash
  python -m src.tools.research.utils.provider_health
  ```

  This is intentionally not a Kubernetes liveness dependency: paper providers
  are optional and the web fallback must remain available during an external
  outage. The command exits non-zero unless the installed arXiv contract and a
  real low-payload Semantic Scholar handshake both pass.

The live credential diagnosis is now narrower than “authorization/rate-policy
failure.” A current main-cluster agent has a non-empty
`SEMANTIC_SCHOLAR_API_KEY`; it has no surrounding whitespace or quotes. The
configured-key request returns HTTP 403 with a generic forbidden response. The
same request without the header reaches the provider but returns HTTP 429 from
the shared anonymous pool. Therefore DNS, egress, endpoint selection, and header
wiring work; the configured credential is rejected, revoked, suspended, or no
longer authorized. The exact provider-side reason cannot be recovered from the
generic response.

The Kubernetes Secret is owned by External Secrets and synced from
`homelab/superhuman-remote-worker/srw-secrets` in Vault. No replacement key is
available in this checkout, and generating one requires the Semantic Scholar
account/email workflow. Once a replacement is issued, acceptance requires an
operator to:

1. obtain a new Semantic Scholar API key and replace only the
   `SEMANTIC_SCHOLAR_API_KEY` property through the approved Vault workflow;
2. force the `srw` ExternalSecret to refresh rather than waiting for its hourly
   interval;
3. drain/recreate the agent pods, because Secret-backed environment variables
   do not change in already-running containers; and
4. run the worker-image probe above in a newly created agent before the next
   paper-job acceptance run.

#### Paper-provider disposition — 2026-08-04

An operator submitted the replacement-key request on 2026-08-04. Semantic
Scholar manually reviews requests, so receipt may be delayed. No further code
work is blocked on that external response: Semantic Scholar is an optional
metadata/citation enrichment provider, arXiv and web research remain usable,
and the repaired workflow now exposes provider failure while preserving healthy
fallback results. This incident is therefore resolved for the current
correctness track; credential installation and the probe above are deferred
acceptance steps.

The requested introductory key is also not a production-scale SaaS contract.
Semantic Scholar documents a one-request-per-second introductory keyed limit and
routes commercial use toward a separately approved/expanded license. A hosted
multi-tenant product must not fan thousands of agents through a personal key.
That future product decision belongs in a separate scholarly-provider design:
central request brokering, caching and deduplication, plus either a negotiated
Semantic Scholar agreement or a provider with explicit commercial capacity
such as OpenAlex. Self-hosted OSS users may configure their own optional key;
the shared product key must never be distributed.

References:

- <https://www.semanticscholar.org/product/api>
- <https://api.semanticscholar.org/license/>
- <https://developers.openalex.org/api-reference/authentication>

The code-side focused gate is 129 passing tests across the arXiv client,
Semantic Scholar transport/probe, paper/workflow tools, and worker API surface.

### Deliverable quality and citation traceability

This audit checks task coverage, structure, source traceability, and honest
handling of failed sources. It is not a full independent peer review of every
technical claim.

| Variant | Headings | Distinct report URLs | CitationEngine sources | Citations | Audit assessment |
|---|---:|---:|---:|---|---|
| Control | 18 | 38 across 23 domains | 72 | 29: 18 verified, 11 pending | Good coverage of all six requested dimensions and an implementable protocol; pending citations weaken its “verified” wording |
| 10-turn | 16 | 39 across 15 domains | 159 | 0 | Useful eight-stage analysis with a 73-item checklist; sources are manually linked, so its claim of “verified citations” is not represented in CitationEngine |
| 24-turn | 61 | 30 across 14 domains | 130 | 49: 42 verified, 7 failed | Deepest failure atlas and strongest engine-backed trace; none of the seven failed citation IDs is used in the final report, and failed URLs are quarantined as dead ends |
| Papers | 13 | 20, all arXiv | 118 | 2 pending | Covers all six requested technique families and evidence limitations; useful fallback result, but intended paper-provider verification did not run |
| Web | 49 | 157 across 23 domains | 604 | 156: 116 verified, 40 failed | Very comprehensive six-platform matrix and explicit failure log; also a source-explosion baseline rather than a concise decision memo |

The five jobs registered 1,083 CitationEngine sources and 236 citations. Source
registration is automatic per search result, so those figures are not equivalent
to distinct references used in the reports. Citation behavior is inconsistent:
some reports use numeric engine citations, while others use ordinary Markdown
links despite registering hundreds of sources. A later quality pass should define
one enforceable traceability contract rather than infer verification from source
registration alone.

### Archived pod warning/error sweep

The model audit trail is not a substitute for worker logs. All five completed
pods were archived to object storage, so their full WARNING/ERROR tails were
searched after the report audit.

The positive infrastructure evidence is direct: the control log records a
successful private-key parse/fingerprint check, connection to
`workspace-66e5878c-396...:30022`, remote workspace initialization, checkout of
`job/66e5878c`, final push, and orchestrator-owned completion/release. This
confirms the main-cluster SSH result rather than merely inferring it from the
terminal status.

The same sweep exposed hidden subsystem degradation:

| Finding | Batch evidence | Effect |
|---|---:|---|
| Citation source auto-embedding failed | 380 attempts / 359 unique source IDs | 374 oversize 422s (provider max 64, attempted 65–1,458), five `NaN` vectors, one 429; registered sources remain absent from semantic index |
| Project memory retrieval deadlocked | 138 turns | retriever contained/skipped for the affected turn |
| Project TTL update deadlocked/timed out | 5 / 4 | shared TTL decay is both contended and consumer-order dependent |
| Retrieval-message persistence failed | 17 | memory observability/history gap |
| Auxiliary structured-output validation failed | 75 extraction + 19 assembly | auxiliary health degraded/recovered ten times; memory functionality flapped while main work continued |
| Phase snapshot lacked `checkpoint.db` | 15 (three per job) | fresh-workspace resume cannot assume checkpoint-backed recovery |
| Tactical phase tag update rejected | 7 across two jobs | branch safe; remote phase evidence stale |
| Web extraction response-shape exception | 1 | `_extract_webpage` received a string and attempted `.get()` |
| PDF source registration rejected NUL bytes | 2 | one arXiv PDF and one ACL PDF were not registered as web sources |

The high-volume issues now have dedicated records:

- `project_scoped_memory_deadlocks_under_parallel_jobs.md`;
- `embedding_batch_overflow_skips_citation_source_embeddings.md`;
- `minimax_m3_auxiliary_structured_output_flaps.md`; and
- `phase_boundary_tags_are_moved_then_rejected_by_remote.md`.

The memory deadlocks are strongly associated with this test shape: every job
shared one project, and every retrieval turn updates overlapping project-memory
rows (`remaining_turns`, then access count/timestamp). Besides lock ordering,
the data model is semantically wrong for concurrency: five consumers decrement
one shared TTL about five times as fast. The fact that the manager contains the
deadlock is good resilience, not a reason to leave shared context
nondeterministic.

The embedding failures also qualify the citation table above. Citation
registration and direct verification can succeed without source embeddings, so
the reported source/citation counts do not imply complete semantic/hybrid search
coverage. At least roughly one third of distinct registered source records in
this batch missed automatic embedding.

The auxiliary health tracker behaved correctly by escalating and recovering,
but MiniMax-M3 did not reliably satisfy the extraction/assembly schemas even
after raw fallback repair. These failed calls are additional overhead and can
remove memory continuity without failing the job. Model capability routing and
complete MCP call-type/error accounting are follow-ups; they do not invalidate
the five final reports.

### Finalization behavior: old manifest loop gone, transition loop remains

No audit trail contains `manifest_status`; the retired boundary manifest did not
reappear. The former continuous `verify-before-done` injection also did not
reappear: skill bodies were read explicitly rather than synthetically appended
on every turn.

The passive completion gate is functioning fail-closed, but its ergonomics are
poor for MiniMax-M3. Across the batch it rejected 50 `todo_complete` or
`job_complete` calls because the model had not read the skill in the current
scope/age window:

| Job | Rejected completion calls | Explicit skill reads found |
|---|---:|---:|
| Control | 15 | 3 |
| 10-turn | 22 | 2 |
| 24-turn | 4 | 3 |
| Papers | 3 | 3 |
| Web | 6 | 4 |

This is not per-turn reinjection. It is the model ignoring a precise corrective
tool error and retrying the protected action, sometimes many times, before
reading the file. Correctness was preserved, but a passive gate should not need
dozens of rejected calls to teach the same action. Any improvement must retain
the fail-closed rule while making the required next action unambiguous or
temporarily exclusive.

A separate phase-transition attractor accounts for much more overhead. The
generic transition creates:

1. `todo_1`: review the completed tactical phase and confirm quality from Git;
2. `todo_2`: “the stop condition comes FIRST,” but completion is allowed only if
   “the review (todo 1) confirmed their quality.”

Those instructions are circular in ordering. On two matched agentic-RAG jobs,
the per-turn `<active_tasks>` reminder repeatedly pulled the model back to
todo 2's first step, so it reran successful file and Git checks instead of
completing todo 1. From the tactical-complete commit until the todo-1 commit:

| Job | Parent rounds | Raw tokens | Tool calls | Dominant repeated tools |
|---|---:|---:|---:|---|
| Control | 10 | 1,253,742 | 19 | reads and Git diff |
| 10-turn | 62 | 7,731,257 | 242 | 99 `file_exists`, 34 `search_files`, 31 each `git_tags`/`list_files` |
| 24-turn | 53 | 10,195,140 | 118 | 25 `git_tags`, 21 `file_exists`, 19 `git_diff` |
| Papers | 5 | 784,244 | 7 | Git review |
| Web | 5 | 920,328 | 10 | Git/list review |
| **Total** | **135** | **20,884,711** | **396** | |

This is a conservative transition-review window; it excludes the calls after
todo 1 while the jobs performed `job_complete` and closed the final todo. In
the 10-turn job, the last 20 rounds alone issued 95 verification calls against
unchanged successful results. In the 24-turn job, prompt size climbed above
220k tokens while it repeated the same promise to review and then complete.
Both models progressed immediately after context compaction dropped the next
prompt to roughly 20k tokens. That timing is strong evidence of a prompt-history
attractor, although the causal claim is an inference rather than an explicit
runtime marker.

The tools did not hang and the reports were already durable. This is the live
confirmation for the open P-2 “conditional REVIEW-AND-ADAPT” work in
`phase_model_overhead_amnesia_loop.md`: when todos and declared artifacts agree,
the transition should take a cheap deterministic/fast path, and the stop check
must not be worded as preceding the review it depends on.

### Prioritized conclusions after the batch

Correctness-first recovery has crossed an important line: ordinary main-cluster
Scholar jobs can now start, research, write, commit, push, and complete. The next
work should stay separated by consequence:

1. **Functional contract:** enforce `delegation.enabled=false`; deploy the
   paper-provider source repair; refresh the MCP deployment/schema and rerun one
   job with a real server-side `required_deliverables` value. Probe Semantic
   Scholar after the requested replacement key arrives, but do not block useful
   jobs or the current correctness track on this optional provider.
2. **Shared context and evidence:** remove project-memory write-on-read
   deadlocks/per-consumer TTL corruption; split embedding batches; make citation
   traceability and source-index coverage machine-checkable; keep phase tags
   immutable and exact.
3. **Auxiliary/provider robustness:** route structured-output tasks to a
   compatible model, surface their calls/errors in MCP accounting, and normalize
   web/PDF extraction error handling.
4. **Efficiency after correctness:** remove the circular transition ordering,
   improve the action-gate recovery path, then tune source and phase budgets
   from these successful baselines.
5. **Containment later:** use the recorded successful trajectories to design a
   no-progress guard that would catch the unchanged transition loop without
   stopping the productive research that preceded it.

The detailed originating incident is updated separately. This ledger remains
the authoritative per-job evidence for the overnight batch.
