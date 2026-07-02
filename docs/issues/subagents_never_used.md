---
tags:
  - issue
  - delegation
  - subagents
  - prompts
  - adoption
  - project-loop
  - cost
aliases:
  - subagents never used
  - delegation never invoked
  - delegate_work adoption
related:
  - "[[delegation_light_mode_missing]]"
  - "[[scholar_delegation_not_exercised]]"
  - "[[subagent_delegation]]"
  - "[[loop_optimization]]"
  - "[[loop_review]]"
  - "[[loop_run6_deep_dive_forensics]]"
---

# Subagent delegation is never used — 0 invocations fleet-wide, all-time, despite rendered instructions

**Date:** 2026-07-02
**Status:** **FIX IMPLEMENTED 2026-07-02** (uncommitted, develop) — adoption package
shipped as Phase 5 of [[delegation_light_mode_missing]]; see "Fix session record" below
for the recommendation-by-recommendation mapping and a **sixth mechanism** discovered
during verification. Remaining: k3d e2e (Phase 6) + post-deploy adoption measurement
with the queries below. Absorbs and supersedes the evidence in
[[scholar_delegation_not_exercised]] (2026-06-03, one gpt-5.5 job) with fleet-wide audit
data; sibling of [[delegation_light_mode_missing]] (the tool-shape gap). The light ReAct
subagent tool this doc assumed was built in the same session (Phases 0–4) — this doc's
job was to make sure that tool actually gets *used*, because the evidence below shows
tool availability + prompt instructions achieve exactly nothing on their own.
**Component:** `src/tools/delegation/delegate_work.py` (tool + framing),
`config/experts/scholar/` + `config/experts/critic/` prompts and todo scaffolds,
`orchestrator/services/project_loops.py` (`_ROLE_BLOCKS`, loop kickoff).

## Headline evidence (dev cluster, audited 2026-07-02)

1. **All-time invocation count: zero.** Delegation children carry
   `creation_order IS NOT NULL` on their job row;
   `SELECT count(*) FROM jobs WHERE creation_order IS NOT NULL` on dev returns **0**.
   The feature shipped 2026-03-23 (`66f1fd56`). In ~3 months of production jobs it has
   never fired once — any model, any role, any job type.

2. **It is not an availability problem.** In loop run 6 (loop `7ca259e2`, 10 jobs,
   788 main-loop calls, MiniMax-M3), `delegate_work` was in the tool menu on
   **100% of calls** for every scholar/critic/developer job (audit:
   `llm_requests.request` contains the tool definition on `menus_with_tool == total_calls`
   for all 16 loop jobs since 07-01).

3. **It is not a prompting problem either.** The rendered system prompts *contained the
   delegation playbook*: the scholar's "Parallel research via delegation" section
   (`strategic_minimax.txt:41-68`) appeared in ~50% of scholar calls (82/168 on scholar
   #10, the strategic-phase calls), and the critic's "independent verification streams"
   section in ~55% of critic calls (34/63, 35/55, 37/66). The `{% if has_tool %}` gates
   evaluated true; the minimax prompt forks are in sync with the base prompts. Hundreds
   of calls saw both the tool and the instructions. Zero invocations.

4. **It is not model-specific.** [[scholar_delegation_not_exercised]] documented the same
   on **gpt-5.5** (job `111be2e8`, 118 requests, textbook 3-topic comparison case, 0
   calls). Run 6 reproduces it on **MiniMax-M3** across 5 scholars and 5 critics. That
   doc's open diagnostic — "run the same task on another family to isolate model vs
   prompt" — is now answered: **both families decline. The cause is structural.**

## Why it never fires — mechanisms, ranked

1. **The decision is made at planning time, and the todo scaffold owns planning.**
   The harness is todo-driven: agents execute the current todo, and KB/context injection
   keys on it. The scholar's seeded strategic todos
   (`config/experts/scholar/strategic_todos_initial.yaml`) walk explore→scope→plan with
   **zero mention of delegation** (grep confirms); `todo_guide.md:118-147` presents the
   "Delegated Parallel Research Phase" as an *optional* structure the model may choose
   while writing its plan. The gpt-5.5 trace shows the fork exactly: at the single
   `next_phase_todos` call (iter 18) the model wrote sequential todos, and the rest of
   the run followed the plan. Concrete scaffold beats optional prose, every time.

2. **Permissive wording + project-wide direct-execution bias.** The guidance is
   conditional ("*When* the remaining work has 2+ separable threads…") presented
   side-by-side with "do it yourself", against an explicit repo-wide default of direct
   implementation (`59cb3d44` hard-coded "default direct implementation due to
   coordination and merge costs" for the developer). An optional lever loses to the
   default. (Root cause carried over from [[scholar_delegation_not_exercised]].)

3. **Deterrent tool framing.** The tool description says *"Always synchronous — you will
   suspend until all subagents complete"* (`delegate_work.py:32`) with a 2h default
   timeout. To a model this reads as expensive, slow, and risky — the opposite of an
   attractive shortcut.

4. **Wrong-phase salience.** The playbook renders only in *strategic*-phase prompts. By
   tactical/execution — when the agent is actually drowning in sequential research and
   delegation would visibly pay — the text is no longer in the prompt.

5. **Heavy shape (see [[delegation_light_mode_missing]]).** Every child is a full worker
   job: dispatcher round-trip, worktree, full 10-node graph with strategic phases,
   parent freeze + re-dispatch, squash-merge. Even *had* a model reached for it, the
   overhead is disproportionate for "research these 3 topics" — and in the loop's
   VM-backend runs it would have silently diverged: the pre-spawn snapshot push is
   `if git.has_remote(): git.push()` (`delegate_work.py:299`), and F29's git-init
   fallback means VM parents have **no remote** — children would clone a project repo
   the parent never touched. Same swallowed-failure family as F29.

## Why it matters (sized)

Context accumulation is the loop's dominant cost, and fan-out is the *structural* fix
(F35 arg-trimming and F37 cache stability only reduce the price of carrying context;
delegation means never carrying it):

- Loop run 6 scholar #10: **35.8M tokens**, 169 calls at 212k avg prompt, 4% cache.
  Rough resizing with bounded fan-out: ~3 explorers at 2–4M each + a parent staying
  under ~50k avg ≈ **10–15M** — a 50–70% cut on analysis roles, compounding with the
  cache fix instead of competing with it.
- Critic verification fan-out ("verify claim X against the repo") chips at **F40**
  (no role can currently see artifact truth).
- Developer pre-edit exploration would have caught the run-6 duplication
  (dev #10 re-implementing existing code).

Full run-6 numbers: [[loop_run6_deep_dive_forensics]] §2; lever priorities:
[[loop_optimization]] Tier 2.

## What the fix session should do

Assume the new light subagent tool exists (in-pod bounded ReAct, fresh context,
result-as-string, mid-tier child models — e.g. a strong parent spawning mid-tier
children). The evidence above says the work is **adoption engineering**, not more
prose:

1. **Wire it into the todo scaffold, not just phase prompts.** Add a delegation step to
   `strategic_todos_initial.yaml` (or a planning-time directive: "if the task spans 2+
   independent topics, your first tactical phase MUST be a delegation phase").
   This was already the recommended fix in [[scholar_delegation_not_exercised]]
   (option 1) — the decision point is demonstrably `next_phase_todos`.
2. **One line in the loop `_ROLE_BLOCKS`** (`orchestrator/services/project_loops.py:49`)
   for scholar ("fan research threads out to subagents; keep your own context for
   synthesis") and critic ("fan independent verification streams out to subagents") —
   the role block is the highest-salience task text the loop roles see.
3. **Flip the wording from permissive to default-with-exception** in
   `scholar/strategic*.txt` and `critic/strategic*.txt` (both forks): "delegate by
   default when threads are separable; sequential self-execution is the exception."
4. **Frame the tool as cheap and non-blocking** in its description — "runs inline,
   returns shortly, your context stays small". For the light tool this is actually
   true, unlike `delegate_work`'s freeze language. Never describe it with
   suspend/timeout vocabulary.
5. **Expose it in tactical/execution phases too** (delegate_work is strategic/tactical
   only, and the guidance only renders in strategic). The need becomes visible
   mid-work; the tool and a one-line reminder should still be there.
6. **Bound and attribute it:** turn cap, restricted (read-leaning) tool list,
   summary-only return; log child calls in the audit DB under the parent job
   (`call_type='subagent'`) so usage accounting doesn't grow another blind spot
   (F16/F27 family) and adoption is measurable (see queries below).
7. **Decide the fate of the heavy path's prose.** Keep `delegate_work` for
   workspace-mutating parallel implementation (its actual design goal); retarget the
   research/verification playbooks in scholar/critic prompts at the light tool. Don't
   leave two competing delegation instructions in the same prompt.

## Fix session record (2026-07-02)

User decisions: **mandatory explicit decision** at the planning point (not mandatory
delegation — avoids garbage fan-out on non-separable tasks), **remove `delegate_work`
from scholar + critic** entirely (0 all-time invocations = nothing lost; one tool, one
playbook), **full scope** (scholar + critic + loop role blocks).

Recommendation → what shipped:

1. **Todo scaffold** ✅ — `strategic_todos_initial.yaml` PLAN todo now requires a
   fan-out decision per phase row in plan.md ("fan-out (N subagents)" or
   "sequential: <reason>", fan-out the default for 2+ independent threads); the
   decision lands in plan.md, which later `next_phase_todos` calls follow — durable
   past phase 0. CREATE todo nudges fan-out-todo-first.
2. **Loop `_ROLE_BLOCKS`** ✅ — one fan-out sentence each for scholar + critic in
   `orchestrator/services/project_loops.py`.
3. **Default-with-exception wording** ✅ — scholar + critic `strategic*.txt`, both
   forks in sync; all `delegate_work` prose removed (zero mentions left in either
   expert dir).
4. **Cheap/non-blocking framing** ✅ — tool description: "runs inline with its own
   fresh context and returns its result directly — delegating the reading keeps your
   own context small". No suspend/timeout vocabulary anywhere (render-checked).
5. **Tactical-phase salience** ✅ — short gated reminder block in all 4 tactical
   prompt files (tool was already bound in tactical; now the guidance is too).
6. **Bound + attributed** ✅ — was already built in Phases 2–4 (read-leaning toolset,
   iteration/token caps, `call_type='subagent'` under the parent job).
7. **Heavy-path prose fate** ✅ — resolved by removal: scholar/critic grant only
   `spawn_subagent` (`delegation.mode: light`); `delegate_work` remains available to
   other experts and converges under the spawn_subagent name as a fast-follow.

**Mechanism #6, found during verification** (would have silently defeated the entire
fix): `"delegation"` is a parsed/known config field, so it is stripped from
`config.extra` — and `tool_config` (what tools see as `context.config`) is built from
`extra` in `agent.py`/`persistent_session.py`. `create_spawn_subagent_tools` therefore
saw no `delegation` key → defaulted to the heavy stub even with `mode: light`
configured. Worse: `delegate_work`'s call-time check
`config.get("delegation", {}).get("enabled", False)` could **never** pass in a real
agent — had any model ever called it, it would have errored "delegation is not enabled
in this agent's config". The 0-invocation evidence concealed that the feature was
doubly broken: never chosen, and non-functional if chosen. Fixed by adding
`mode`/`light` to `DelegationConfig` and injecting `asdict(config.delegation)` into
both `tool_config` sites; regression-pinned in
`tests/test_spawn_subagent.py::TestDelegationConfigPlumbing`.

## How to measure adoption (post-fix verification)

Audit DB (`srw-auditdb-0`, db `srw_audit`, `llm_requests` partitioned by month):

```sql
-- Was the tool offered? (menu presence)
SELECT substring(job_id::text,1,8) AS job, agent_type,
       count(*) FILTER (WHERE request::text LIKE '%<tool_name>%') AS menus,
       count(*) AS total
FROM llm_requests
WHERE "timestamp" > '<date>' AND call_type = 'main'
GROUP BY 1,2 ORDER BY 4 DESC;

-- Was it invoked? (assistant tool_calls in responses)
SELECT count(*) FROM llm_requests
WHERE "timestamp" > '<date>' AND response::text LIKE '%"<tool_name>"%';
```

Heavy-path children (should stay ~0 unless genuinely parallel implementation):
`SELECT count(*) FROM jobs WHERE creation_order IS NOT NULL;` (main DB `srw`).

Healthy light-delegation trace shape: parent planning call → N `call_type='subagent'`
bursts with small fresh prompts → parent synthesis calls with prompt size *flat*, not
climbing — contrast with run 6's monotonic 212k-avg climb.

## References

- **Evidence details:** [[loop_run6_deep_dive_forensics]] (run-6 audit method + token
  anatomy); [[scholar_delegation_not_exercised]] (gpt-5.5 trace, iter-18 planning fork,
  prompt-resolution rule-outs: family-variant shadowing and grants both cleared).
- **Tool shape gap + light-mode design space:** [[delegation_light_mode_missing]]
  (Send()-fan-out vs agent-as-tool vs no-merge job variant; open questions on limits,
  observability, model selection).
- **Code:** `src/tools/delegation/delegate_work.py:32` (deterrent description),
  `:299` (`has_remote` silent-skip push — F29 interplay), `:183` (depth check);
  `orchestrator/services/project_loops.py:49` (`_ROLE_BLOCKS`), `:288`
  (`config_name=role` — loop roles load the full bundled experts, which is why the
  playbooks rendered);
  `config/experts/scholar/config.yaml:92` + `config/experts/critic/config.yaml:52`
  (tool grants); `config/experts/scholar/strategic.txt:30-60`,
  `strategic_minimax.txt:41-68`, `todo_guide.md:118-147`,
  `strategic_todos_initial.yaml` (no delegation step);
  `config/defaults.yaml:350` (`delegation.enabled: true` bundled default).
- **Findings registry:** F42 in [[loop_review]] points here.
