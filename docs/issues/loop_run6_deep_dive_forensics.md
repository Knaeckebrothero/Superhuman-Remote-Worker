---
tags:
  - issue
  - investigation
  - project-loop
  - self-improvement
  - forensics
  - cost
  - knowledge-base
  - vm
  - cache
aliases:
  - run 6 deep dive
  - loop forensics
  - run 6 forensics
related:
  - "[[loop_review]]"
  - "[[loop_optimization]]"
  - "[[project_self_improvement_loop]]"
  - "[[loop_repo_compounding]]"
  - "[[kb_convergence_ttl_reverification]]"
---

# Run-6 Deep-Dive Forensics — five-subagent investigation of loop `7ca259e2`

**Status:** investigation COMPLETE (2026-07-02). This is the full evidence archive behind
findings **F29 (root cause), F35–F41** and the F22/F23 resolutions registered in
[`loop_review.md`](../loop_review.md); the resulting fixes are prioritized in
[`features/loop_optimization.md`](../features/loop_optimization.md). All investigation was
read-only (SELECT-only against `srw`, `srw_audit`, `srw_vector`; read-only pod logs, Gitea
API, and repo greps).

**Subject:** run 6 — loop `7ca259e2-64c6-41ce-84fc-8445a683411c`, project `68137e29`
("Hotel Rheinland ERP"), `MiniMax-M3` direct + **vm** workspace backend, 10/10 jobs
completed over ~18.5 h unattended (3 full scholar→critic→developer cycles + a 4th
scholar), user-paused at 10 of 33. First operationally clean run; artifact layer totally
lost (F29 ×3). ~194 M tokens total.

| # | Job | Role | Wall | Calls | Tokens | Cache |
|---|-----|------|------|-------|--------|-------|
| 1 | `1b56f2ba` | scholar | 95 min | 108 | 14.9 M | 7 % |
| 2 | `7b75ebf9` | critic | 28 min | 55 | 6.5 M | 7 % |
| 3 | `1a1dc601` | developer | 93 min | 241 | 27.6 M | 10 % |
| 4 | `87e427c9` | scholar | 64 min | 70 | 14.9 M | 4 % |
| 5 | `7cc65a17` | critic | 37 min | 66 | 8.2 M | 7 % |
| 6 | `a9ec0996` | developer | 152 min | 299 | 33.4 M | 10 % |
| 7 | `5ac90405` | scholar | 61 min | 98 | 13.2 M | 8 % |
| 8 | `9a504c7a` | critic | 41 min | 63 | 10.0 M | 5 % |
| 9 | `e1ede3c0` | developer | 180 min | 248 | 29.8 M | 11 % |
| 10 | `5a65f284` | scholar | **353 min** | 168 | **35.8 M** | 4 % |

**One-sentence synthesis: the agents are fine; the plumbing loses their work and then
lies about it.** The developers' lost code was real, tested, and honestly reported; the
infrastructure (a) never delivered the repo (DNS, §1), (b) hid the failure (silent
git-init + swallowed push warnings), (c) let destroyed work live on as "SHIPPED GREEN" KB
notes that later critics cite as ground truth (§3), and (d) burned ~50 % of every long
prompt re-sending the agent's own tool-call arguments uncached (§2).

---

## §1 — F29 root cause: VM workspaces never receive the project repo

### Mechanism

The clone is performed by the **agent harness pod** at job setup by executing
`git clone <url>` **on the workspace backend via SSH** (`GitManager.clone` →
`backend.shell_run`, remote tab "git"). The URL is the authenticated Gitea URL persisted
in `project_repositories.repo_url`, minted from `GITEA_INTERNAL_URL` =
`http://srw-gitea:3000` — a cluster-internal Service DNS name. On the pod backend the
shell runs in an in-cluster workspace container which resolves `srw-gitea` → clone
succeeds. On the vm backend the identical command runs on the VM (a tailnet node outside
the cluster, e.g. `100.64.19.138`), which cannot resolve/route `srw-gitea` → `git clone`
exits 128 (`Could not resolve host: srw-gitea`) → the code **silently falls back to
`git init`** (root commit "Initialize workspace"), adds the same unreachable URL as
`origin`, so every subsequent phase-boundary/completion `git push` also fails
(warning only) — and the job still completes "successfully" with all work stranded on the
ephemeral VM.

### Evidence

Live smoking gun — agent pod `srw-agent-j-f4b460e5` logs (job `5a65f284`, same loop,
caught running during the investigation):

- `09:43:33 "Connected to VM 100.64.19.138:22"` (remote.py:239) — harness in-cluster,
  workspace on VM.
- `09:43:35 WARNING git_manager.py:790: git clone failed for
  http://srw:***@srw-gitea:3000/srw/project-68137e29-jobs.git: Exit code: 128 … fatal:
  unable to access … Could not resolve host: srw-gitea`
- `09:43:35 WARNING workspace.py:416: "Failed to clone jobs repo, falling back to
  standard init"` — so `repositories` WAS in the dispatch payload; the orchestrator side
  did its job.
- Second clone attempt (from `git_remote_url`) fails identically →
  `workspace.py:363: "Failed to clone remote repo, falling back to git init"`.
- `09:55:59`, `10:09:32 WARNING git_manager.py:654: "git push failed: "` — pushes ran
  (origin added by the fallback, workspace.py:371-372) and failed; swallowed as warnings.

Audit-DB corroboration (same fingerprint on all sampled vm jobs): `1a1dc601`'s first
`list_files` (~8 s after init) shows a fresh git-init with the bare workspace skeleton and
no project files; `e1ede3c0`'s phase-0 LLM observes "git is initialized here with an
initial commit `bd9f8d5 Initialize workspace`. The repo is empty (no source files)" —
`"Initialize workspace"` is `init_repository`'s commit message
(`src/managers/git_manager.py:171`); run-5's `011fcfce` saw ".venv/ and tools/ already
tracked in the initial commit" (git-init over the VM's pre-baked runtime dirs). Job
context rows are **identical** to the working pod job `19707fa1` — no orchestrator-side
difference. There is **no VM-side repo seeding at all**: cloud-init/golden image provide
only the runtime (`.venv/`, `tools/`); the divergence is purely the network position of
the shell that runs the clone.

### Fix options (detail behind loop_optimization Tier 1 #4)

The externally reachable endpoint already exists and works: ingress `srw-gitea-ingress` →
`https://git.superhuman-remote-worker.com` (Cloudflare-proxied, HTTP 200; git smart-HTTP
is plain HTTPS). `GITEA_URL` already carries it.

1. **Recommended — dispatch-time URL rewrite**: in `_dispatch_job_to_agent`, in/next to
   the existing VM-injection block at `orchestrator/main.py:2159-2176` (which already
   knows `vm_ctx.status == ready`): when backend is vm, rewrite `git_remote_url`
   (extracted at :2090, derived at :2139-2144) and every
   `repositories_payload[*].repo_url` (:2120-2132), swapping the `GITEA_INTERNAL_URL`
   host for the `GITEA_URL` host (keep embedded credentials, https). Covers loop jobs,
   subjobs, scholar/critic (all flow through this dispatch); pod jobs untouched; fixes
   clone AND push in one stroke because origin is set from the same URL.
2. **Alternative — agent-side SSH reverse tunnel** (`src/core/backends/remote.py`):
   paramiko reverse port-forward VM `127.0.0.1:<port>` → `srw-gitea:3000` + URL host
   rewrite in `_setup_workspace`. Keeps Gitea off the public path but adds a long-lived
   tunnel dependency to every git op.
3. **Not sufficient alone**: fixing URL minting in
   `orchestrator/services/gitea.py:_build_clone_url` (:385-400) — it can't choose
   per-backend, and project URLs are *persisted* at repo-creation time
   (`job_provisioning.py:216` reads the stored value), so existing rows stay broken.
4. **Hardening (do regardless)**: `src/core/workspace.py:415-418 / 362-372` — for loop
   execution roles (`work_on_main`), a failed jobs-repo clone must fail the job loudly
   (or emit an audit event), never silently git-init; same for `git_manager.py:654`'s
   empty-message push warning.

Side observation (separate bug, not F29): the same agent pod later failed a *session*
clone with "destination path already exists and is not an empty directory" — a
workspace-reuse issue.

---

## §2 — Context-bloat + cache anatomy (F35, F36, F37)

Subject: the runaway job-10 scholar `5a65f284` (168 calls, 35.59 M prompt tokens, 5.9 h,
8 phases, avg 212k prompt-tokens/call, max 298k, 127/168 calls > 150k, ~31 s/call).
Request config: `model_kwargs={max_tokens: 65536, model_name: "MiniMax-M3",
temperature: 1.0}` — no cache-related request fields at all (implicit provider-side
prefix caching).

### Prompt anatomy of the peak call (id 13923: 298,257 tokens, 466 messages, 1.24 M chars)

Method: per-role/per-message char sums via `jsonb_array_elements … WITH ORDINALITY`,
tool-call args unpacked by function name, chars→tokens pro-rata (4.14 chars/tok).

| Category | Chars | ~Tokens | % |
|---|---:|---:|---:|
| **Assistant `tool_calls` (serialized args)** | 655,482 | ~158k | **53 %** |
| — of which kb_write 267k / write_file 238k / edit_file 78k | 583,309 | ~141k | 47 % |
| **Tool results (136 "real" kept)** | 267,607 | ~65k | **22 %** |
| — top: todo_complete echoes 54.7k, web_search 34.7k, git_log 33.3k, extract_webpage 28.2k, kb_read 27.7k, kb_search 25.6k | | | |
| Assistant text (mostly retained `<think>` blocks) | 83,890 | ~20k | 7 % |
| JSON envelope/ids | ~148k | ~36k | 12 % |
| Tool definitions (45 tools) | 42,217 | ~10k | 3.4 % |
| System prompt | 11,972 | ~2.9k | 1.0 % |
| Kickoff (`# Task Brief` + `<active_tasks>`) | 11,011 | ~2.7k | 0.9 % |
| Memory+KB injection (current call) | 6,877 | ~1.7k | 0.6 % |
| Stubbed results (140 × 44 chars) + phase directives | 8,056 | ~1.9k | 0.7 % |

**Accumulated history ≈ 94 % of the prompt; the single biggest component is the agent's
own written artifacts** — kb_write/write_file arguments kept verbatim forever (**F35**).
A trimmer exists but only stubs tool *results* (`[Result processed - see workspace if
needed]`, 140 of 276 tool msgs) — never tool-call *args* — and retains 37 near-identical
`todo_complete` echoes (~55k chars of redundant todo-list snapshots).

### Growth pattern + compaction (F36)

Monotonic 17.3k → 298.3k tokens (7 → 466 messages) across all 8 phases; 10
phase-transitions **swap** the system prompt (9,253 ↔ 11,972 chars strategic/tactical)
and the 45-tool set and insert a `[PHASE_TRANSITION]` message — but never reset context.
Small sawtooth dips (250k→238k @iter 91; 297k→282k @iters 134-135) are progressive
result-stubbing, not compaction. **Compaction never fired for the scholar or critic**
(scholar 298k, critic 250k) — but it *does work*: the cycle-3 **developer** compacted once,
call 187 (200,853 tok, 488 msgs) → call 188 (24,294 tok, 18 msgs) with a 9,831-char
`[Summary of prior work]` system message. Threshold inconsistency to trace.

### memory_inject + reranker

168 memory_injects (1/call), each ~15 items / ~1,000 tokens in 2 blocks ("Pinned Memories
(TTL-active)" + "Project Knowledge"), fresh retrieval every call (168/168 distinct).
Direct cost trivial (~0.5 %). **Bug: the reranker 404'd on every single call**
(`scorer:reranker: 404 https://api.minimax.io/v1/rerank`) — memory scoring silently
degraded on all MiniMax runs (F19 annotated).

### Cache mechanics (F37) — prefix instability by design, not a provider limit

Provider caching works: `cached_tokens` hits 14,080–16,000 on 106/168 calls. But the
ceiling is ~16k of ~212k (5–7 %) because message positions 2–6 mutate **every call**:

- idx 2 `<active_tasks>` (human) — rewritten on every todo change; when it flips, cached
  drops 16k → 128.
- idx 3/5 — synthetic assistant tool-calls with **random ids regenerated per call**
  (`memory_inject_c4b30e98`, `knowledge_inject_590eaa01`) — md5-distinct on every
  consecutive pair sampled.
- idx 4/6 — fresh memory/KB selections each call.

The deep history (msgs 8–460) is append-mostly and *would* cache — it is structurally
unreachable behind the mutating front. Cached plateaus match tools+system (~14.1k) and
tools+system+active_tasks (~15.7–16k) exactly.

Comparison — same disease, different organ: **critic `9a504c7a`** 63 calls / 9.89 M,
16k→250k monotonic, 4.9 % cache; peak call dominated by tool *results* (502k chars — the
critic *reads*, the scholar *writes*). **Developer `e1ede3c0`** 248 calls / 29.6 M,
10.6 % cache, one compaction reset (200.9k→24.3k), regrew to 123.8k. All three share the
identical front-block pattern → identical ~14–16k cache ceiling.

### Lever sizing (scholar baseline 35.59 M)

| Lever | Saving |
|---|---|
| (a) Cap analysis roles at 2 phases | keep ~6.6 M → **−29.0 M (−81 %)**; per-phase context reset w/ handoff summary ≈ −70 % |
| (b) Stub write-side tool-call args >10 turns old + todo-echo dedup | **−13–16 M (−38–43 %)** |
| (c) Stable prefix (tail-relocate injections + active_tasks, deterministic ids) | 21–28 M tokens move to cache-hit tier (**58–78 % cache** vs 4 %); main latency lever (31 s/call is prefill-dominated) |
| (d) Smaller KB injection | ≤0.3 M (<1 %) — not a token lever; its value is enabling (c) |

---

## §3 — Critic audit (F22 sharpened, F40)

### Per-critic behavior

**Cycle-1 `7b75ebf9`** (verdict: Loop 3 P001 offline-first) — Goal-level *on selection*:
built a 7-criterion framework (evidence anchor, DoD advancement, Resavio gap severity,
regulatory correctness, competitive white-space, scope risk, composite scoring); verdict
anchored to the DoD ("L3-001 is the only proposal that closes a remaining DoD gap")
with concrete red-phase tests prescribed for the next developer. But prior progress is
asserted **purely from KB notes** — its 21 repo-inspection calls all ran against its own
fresh job workspace. **Supersede: documented, not executed** — wrote 3 "Superseded —"
*learning tombstones*, made **zero kb_update calls**, and its verdict claims the losers
"are marked `superseded`". Did notice it couldn't inspect phase outputs (curator notes
tagged `workspace-empty`) and recorded it as open questions, then moved on.

**Cycle-2 `7cc65a17`** (verdict: Loop 4 P004 Bad Orb) — same framework; its
"verification" is KB-note cross-referencing labeled verification ("✅ Verified" against
`iteration-3-status-kurort-engine-mvp` — a *note*, not the artifact). **Supersede: never
attempted** — 13 kb_update calls, **all `add_links`**, no call ever carried a `status`
argument — while the verdict **falsely states** "NON-SELECTED (each marked
`SUPERSEDED`…)". Loop-4 losers are still `active`. Closest any critic came to the truth:
retro open question #6 — "*Iter-6 Developer must verify these stubs exist in the
workspace OR re-bootstrap from the locked spec SHA*" — it **suspected** the code might be
gone and delegated the check forward instead of performing it.

**Cycle-3 `9a504c7a`** (verdict: Loop 7 P003 MinStay) — most rigorous: 7 criteria +
self-imposed PR-1..PR-4 rules + forced-flaw scrutiny + weighted synthesis. **Supersede:
fully executed** — `kb_update {"note": "loop-7-proposal-00X-…", "status": "superseded"}`
on all 4 losers (06:26) + SUPERSEDED_BY links. **Artifact check: it looked, found
nothing, and rationalized** — ran `find . -type d -name "kurort*"` (06:03), saw a fresh
workspace (bare .git, no `repo/`, no Python), then wrote Learning 8: "*the files DO NOT
EXIST in this iteration's workspace… **Resolution:** The KB plan notes ARE the working
inputs… Workspace state may be stale*" — absence noticed, misdiagnosed as normal per-job
isolation, never escalated. Its verdict also asserts "Reuses **SHIPPED** `kurort_engine`
rates.py…" — the SHIPPED claim sourced entirely from the destroyed developer's retro note.

### The structural conclusion (F40)

**The critic cannot detect a non-compounding artifact, and the KB actively misleads it.**
Critics run in fresh per-job workspaces with a bare git repo — there is no shared `main`
they can reach (on vm, doubly so per §1) — so even the critic that hunted for the code
had no way to distinguish "workspace isolation" from "work destroyed". Meanwhile
`iteration-6-developer-retrospective-l4-004-green-implementation` (job `a9ec0996`, still
`active`) claims "shipped GREEN with 9/9 tests passing, ruff clean, 85.23 % project
coverage… 6 GREEN files in `repo/src/kurort_engine/`" and later critics treat such notes
as verified ground truth. The critics' own evidence discipline (every claim needs a KB
slug) makes the chain **self-referential: KB notes validate KB notes; artifact reality
never enters.** → The orchestrator-side no-op `main`-HEAD guard (loop_optimization
Tier 1 #2) is the only viable artifact-integrity check; consider invalidating
`state`/retro notes when it detects a no-op.

### Self-invented critic rules (quoted)

- Pinned operating rules (`7b75ebf9`): "1. **The loop is unconditional.** The Critic must
  always select a NEXT action. Approval of completion is forbidden… 2. **Read-only on
  repo.** … 3. **Verdict anchored to evidence** — every criterion cites a file path, line
  number, or KB note slug… 4. **Three losers always superseded** — `status=superseded` +
  a `SUPERSEDED_BY` link to the winner… 5. **Goal-level verification.** 'Code compiles' /
  'tests pass' / 'no leftover TODOs' are NOT acceptance…" *(rule 4's status flip is the
  part its own author skipped)*.
- Tie-breaker stack (`7cc65a17`): "1 (dominant) DoD gap-closing — the proposal that
  closes the MOST-UNIVERSAL sub-aspect; 2 iteration-budget feasibility — smallest tier-1
  scope fitting 1 iteration; 3 lowest-risk highest-impact v1 (≤3-point composite spread);
  sub-tie-breaker: first-closer status."
- iteration-8 pinned rules (`9a504c7a`): "PR-1: ONE WINNER ONLY… PR-2: FOUR LOSERS ALWAYS
  SUPERSEDED — a `kb_update` with `status=superseded` + a `SUPERSEDES` link. **No silent
  discard. No 'still active but lower priority'.** PR-3: VERDICT ANCHORED TO EVIDENCE…
  PR-4: GOAL-LEVEL VERIFICATION — closes ≥1 DoD item + ≥1 Resavio pain point…"

### Mechanical-supersede identifiers (verified across generations L2/L3/L4/L7/L10)

Losers selectable as: `note_type='plan' AND status='active' AND note_id ~
'^loop-{N}-proposal-\d+' AND job_id = <same-cycle scholar job_id> AND note_id !=
<winner>`; winner extractable from the verdict slug
(`…choose-loop-7-proposal-003…`) or the critic's IMPLEMENTS link. **Not reliable:**
`tags` (empty on most proposal/verdict notes), `phase` (always empty), and critic prose
(cycle-2 claimed flips it never made). The TTL/convergence pass is only an eventual
backstop (fires cycles later).

---

## §4 — Developer forensics (the lost work, reconstructed)

All three developers ran MiniMax-M3 on vm workspaces (empty per §1); the only surviving
record is `agent_audit` tool calls + freeze summaries.

**Cycle-1 `1a1dc601`** (93 min, 307 tool calls, 27.6 M) — 46 write_file: spec.yaml (11
EARS ACs), spec_lock.md, plan.md, verify/rebuild spec scripts, `repo/` bootstrap
(pyproject + 9 modules + README + 2 profile YAMLs + 10 test files, 9 of them
placeholders), ~2.3k LOC incl. docs. Claim "MVP slice at the red boundary" — **verified
but thin**: 9 placeholder tests PASSED, AC tests deliberately RED (it even patched a stub
so RED failed with AssertionError, honoring its pinned "honest RED" rule). No working
feature. **Duplication ≈ 95–100 %** — everything already existed from pod-job `19707fa1`
(it read the KB note and wrote "*Critical realization: the workspace has been reset… KB
describes shipped work, filesystem has none of it*"). It also duplicated **itself**: a
mid-job `todo_rewind` rollback ("Operating Rule 8 violation") wiped its phase-2/3
bootstrap — at 23:41 `ls repo/` was empty again — and it rebuilt in phase 5 (**F41b**).
Git: heavy status/log/diff/tag use on the harness-local repo; **zero
push/remote/clone/fetch attempts**.

**Cycle-2 `a9ec0996`** (152 min, 359 calls, 33.4 M) — 70 write_file: rebuilt
spec/pyproject/9 stub modules, then novel
`reporting/{remittance_csv,period_calculator,dsgvo_vvt}.py`,
`kurverwaltung/{bad_orb,platforms/{secra,generic}}.py`, demo fixture, ~497-line test file.
Claims **fully verified**: genuine red→green (03:08 real failures `assert 'stub' ==
'secra'`), and at 04:49 `9 passed / All checks passed! / Required test coverage of 80.0%
reached. Total coverage: 85.23%` — the claimed number **verbatim in pytest-cov output**.
Duplication ≈ 25–30 % (venv, spec-lock machinery, stubs, packaging fights). Git:
accidentally committed `.venv` → cleaned up; annotated tags; **no push/remote**.
⚠ **F41a: at 04:50 shell output capture died** — ~20 final commands (even `echo HELLO`)
returned "Exit code: 0 / (no output)"; it probed, then made its final commits blind;
freeze summary doesn't mention it.

**Cycle-3 `e1ede3c0`** (180 min, 362 calls, 29.8 M) — 76 write_file: minimal core +
novel `minstay/{rules,saisons,enforce}.py` + `minstay/channel/{booking,expedia,
airbnb_ical}.py` + 6 profile YAMLs + 7 test files (16 tests). Claims **fully verified**:
`16 passed in 0.10s` observed in three independent runs; genuine spec-first TDD (all
tests RED in phase 1, GREEN in phase 3). **Anti-gaming self-audit**: grepped its own
tests for `assert True|pytest.skip|xfail` to prove none were fake; caught its own wrong
Whitsun expectation and fixed it with correct Easter math (Easter 2027 = Mar 28 → +49d =
May 16). Duplication ≈ 15–20 %. Git: the only one to run `git init` (nested inside
`repo/`, then `rm -rf repo/.git` twice, deciding the workspace repo was canonical — the
closest anyone came to noticing the topology problem, resolved in the wrong direction);
**no push/remote**.

### Run-level conclusions

- **No developer ever ran `git push`/`remote`/`clone`/`fetch`/`pull`, and no reasoning
  ever mentions origin/remote/push.** Pushing is the harness's job (src/core/phase.py) —
  which failed silently per §1 — and the harness-initialized local repo (root commit,
  auto-commit-per-todo, phase tags) looked complete from inside. All three noticed the
  empty workspace ("*this is the third restart of iteration 3*") and treated it as
  expected loop behavior — rebuilt without escalating. The KB carried perfect
  *descriptions* of prior code but zero code: **a lossy compiler of previous iterations.**
- **Token split:** ≈ 27.6 M (cycle 1, ~100 % dup) + ~9 M of 33.4 M + ~5 M of 29.8 M →
  **~42 M of ~91 M developer tokens (~45 %) spent re-creating bootstrap** predecessors
  had already built.
- **Quality verdict: real and good — worth saving had it landed.** Domain-correct German
  Kurtaxe material (Hessen KAG, Heilbad/Satzung 5-band rate tables, secra Meldeschein CSV
  `fall_id;fall_pseudonym;anreise;abreise;anzahl_naechte;kurtaxe`, DSGVO Art. 30 VVT,
  guest pseudonymisation, correct Easter/Pentecost arithmetic). TDD discipline genuine,
  not performative. **No fabrication or gaming detected anywhere.** Cycles 2–3 each
  delivered ~1.5–2k LOC of tested feature code.

---

## §5 — KB dynamics (F38, F39; F23 settled)

### Growth composition (230 new notes / 10 jobs = 23/job, ~2.76 MB)

| type | n | avg | total | TTL |
|---|---:|---:|---:|---|
| learning | 71 | 9.0 kB | 640 kB | **none (immortal)** |
| retrospective | 54 | 17.5 kB | **946 kB (34 % of all bytes)** | **none** |
| state | 48 | 13.6 kB | 651 kB | 2 cycles |
| plan | 24 | 12.9 kB | 311 kB | 3 |
| decision | 16 | 6.9 kB | 110 kB | none (durable by design) |
| goal | 15 | 5.6 kB | 84 kB | 3 |
| question/code | 2 | — | 22 kB | 3/none |

Writer attribution (audited kb_write calls vs KB rows; curator aux passes are not
audited, so the per-job gap = curator): agents 162 kb_write calls (~155 landed, 0
errors); **curator ≈ 70 notes (~30 % by count, more by bytes)**. By role: scholars 20–21
deliberate writes/job, critics 17–20, **developers only 7–9** (on dev jobs the curator
wrote the majority). Structural drivers besides the curator: (a) **per-job role
boilerplate** — every scholar/critic re-creates its own goal/constraints/criteria/scope
notes (4–5/job) instead of updating loop-level ones (only the cycle-1 scholar did it
right: 4 `kb_update append`); (b) **retrospective firehose** — ~1 per phase transition,
15–32 kB each. **Projection for a 33-job run: ~900 rows / ~730 active / ~9–10 MB, of
which 500+ TTL-exempt** and invisible to convergence.

### Injection quality (F23 — settled: similarity luck, not routing)

Mechanism (graph.py:1292, knowledge_injection.py): every turn, top-5
`hybrid_search(top-pending-todo + "phase N strategic/tactical")`, each note truncated to
500 chars, injected as a fake kb_search result. Block present in 1,414/1,416 requests —
but *which* 5 notes land is pure lexical similarity:

- **Verdict → same-cycle developer: unreliable.** 'verdict' in the block: cycle-1 dev
  ≤16/241 turns (6.6 %); cycle-2 76/299 (25 %); cycle-3 85/248 (34 %) — improves only
  because current-iteration notes accumulate shared vocabulary.
- **DoD/goal note reached 0 of 3 developers in 788 turns** (and 0 for the cycle-3 critic
  and job-10 scholar). Only the first two critics got it (9/55, 24/66). Scholars/critics
  compensated by explicitly `kb_read`-ing it; developers never did.
- **Noise crowds the slots:** ≥1 retrospective in 48–83 % of each job's injections;
  prior-loop content everywhere ('loop 2' in up to 77/98 turns). Smoking gun: cycle-1
  dev's mid-job sample — **all 5 slots** June-27 notes, two of them near-duplicates,
  zero cycle-1 content, no verdict, no DoD.
- Self-serve: scholars 20–51 kb read-ops, critics 34–44, **developers 10–25** — the
  worst-served role compensates least.

→ The pinned always-on loop-state header (loop_optimization Tier 3 #10) is confirmed
necessary; exact contents: **current verdict + DoD + current-iteration status** —
deterministically, every role; top-5 hybrid keeps the long tail.

### Supersede attribution + TTL (F39)

kb_update calls exist for exactly 3 jobs: `1b56f2ba` (5: appends/refresh, no flips),
`7cc65a17` (13: all add_links), `9a504c7a` (15: **8 × status=superseded** + links). Of
the run's 52 status flips: critic-explicit 8; **aux passes (convergence + curator dedup)
~43** (batches with no tool calls anywhere in the audit — e.g. 05:11 ≈ 27 TTL-0
archives/supersedes, 10:12 the cycle-1 verbose proposal set, 13:22 curator superseding
its own loop-10 dups); scholars 0; the 1 `resolved` was a dev blocker note flipped by aux.

**TTL decrement works exactly to spec** (state=2, goal/plan/question=3, minus one per
cycle-wrap — cohort distributions match perfectly). **Survivor refresh does NOT work:**
`last_verified_cycle` is NULL on **all 374 rows** and 38 active notes sit at
remaining ≤ 0 (some −1, decremented past zero across two passes) — `refresh_ttl`
(`src/services/auxiliary.py:1467`) has never landed a row, so the stale queue never
drains via "keep" and the same survivors are re-adjudicated every pass.

### The proposal double-write, solved

**Not an agent retry — the curator racing the agent.** The cycle-1 scholar's audited
kb_writes contain only the compact "L3-nnn" set; the verbose `loop-3-proposal-001..004`
notes appear in **no tool call** — written by the CurateKnowledgeTask aux pass after
phase-4 completion, which "reconstructed" the proposals from plan.md (its retro titles
say "curator reconstruction"). Convergence cleaned the curator's set up 12 h later. The
pattern recurred in job 10 (curator verbose 10:58 → agent canonical 12:17–12:33 → curator
pass superseded the dups 13:22), plus duplicated idea-index and coverage notes. An
injected note in `e1ede3c0` even self-identifies: "**Author: Knowledge Curator (extracted
from iter-9 strategic plan)**".

Fix set (loop_optimization Tier 3 #13): curator stops writing per-phase retrospectives
and deliverable reconstructions to the KB (restrict to linking/updating for plan/state
types); give learning/retrospective a TTL; collapse role boilerplate into loop-level
singletons; fix `refresh_ttl`.

---

## Cross-references

- Findings registry: [`loop_review.md`](../loop_review.md) — F29 (root cause annotated),
  F22 (sharpened: false compliance), F23 (settled), F19 (reranker 404 annotated),
  **F35–F41** (registered from this investigation).
- Fix plan: [`features/loop_optimization.md`](../features/loop_optimization.md) — items
  #2 (no-op guard, elevated by F40), #4 (F29 fix), #5 (mechanical supersede), #7 (phase
  caps, sized), #8 (cache, mechanism), #10 (pinned header, confirmed), #13 (curator diet
  + refresh_ttl).
