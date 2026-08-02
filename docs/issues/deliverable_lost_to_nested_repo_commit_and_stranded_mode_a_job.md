---
tags:
  - issue
  - agent
  - workspace
  - git
  - cockpit
  - cloud-export
related:
  - "[[job_cloud_export]]"
  - "[[workspace_and_change_records]]"
  - "[[officer_blind_reads_and_worker_bureaucracy]]"
---

# A correct deliverable never reached its branch, and two path anchors made it undiagnosable

**Filed:** 2026-08-02. Found while asking why job `bbce4bed` had nothing in `output/` on its
branch and no "export to cloud folder" button in the cockpit. Three defects in a chain: the
phase-boundary push never ran, the agent could not diagnose that because `write_file` and
the shell resolve the same relative path against different roots, and the resulting job
state has no exit in the UI.

Reference job: `bbce4bed-79be-4e36-bbb1-9dd12ce43dcf` — project `Better Resavio`
(`68137e29-6b1f-4f1b-a0c1-4e6dc2be3f9a`), config `developer`, model MiniMax-M3, created
2026-08-01T14:42:32Z, sealed `pending_review` 2026-08-02T03:27:59Z after 8 phases and
~13 hours.

## The agent did nothing wrong with the path

Worth stating plainly, because two plausible-sounding theories are both false.

**It is not a hallucinated deliverable.** The agent wrote `output/ui_recovery_report.md`
six times:

| written (UTC) | chars in JSON |
| --- | --- |
| 2026-08-01 14:58:19 | 1 967 |
| 2026-08-01 16:36:12 | 3 428 |
| 2026-08-01 16:59:03 | 3 428 |
| 2026-08-01 22:29:58 | 17 093 |
| 2026-08-01 22:44:24 | 16 647 |
| 2026-08-02 01:43:43 | 14 158 |

The final version is 14 158 *characters* → **14 248 bytes** UTF-8 (the file is full of
German domain nouns — Kurort, Belegung, Kassenabschluss). That is byte-for-byte the number
in its freeze summary, and the recovered file carries exactly the six contracted `##`
headings it claimed.

**Nor did it write to the wrong path.** The job's own task brief contracts exactly
`output/ui_recovery_report.md`, and says so explicitly:

```
## Required Deliverables (Contract)
- `output/ui_recovery_report.md`

Rules:
- Scaffold each deliverable file EARLY (phase 1), even as an outline.
- Update them every phase — each phase boundary commits and pushes
  them, and records contract status in `output/manifest_status.json`.
- Paths are workspace-relative; `repo/` prefix is accepted either way.
```

`normalize_deliverable_path` (`src/core/deliverables.py:42-64`) strips `repo/` — canonical
form is *without* it — and `resolve_workspace_deliverable` accepts either form. So sibling
jobs writing `repo/output/…` and this one writing `output/…` are the same contracted
artifact. The agent wrote the canonical path it was given.

## Defect 1 — the promised phase-boundary commit-and-push never ran

The brief promises *"each phase boundary commits and pushes them"*. Across 8 phases and
13 hours, branch `job/bbce4bed` never moved off
`098bf3fe6dbbdfb463e675db72f433db075412ba`, dated **2026-07-29T15:41:08Z** — three days
before the job started.

Corroborating: `output/manifest_status.json`, which `write_manifest_status` is supposed to
emit at *every* phase boundary (F13), is also absent from the branch, though the agent
demonstrably wrote it. Nothing this job produced reached Gitea.

### The failure is silent by construction

`GitManager.push` (`src/managers/git_manager.py:783`) **returns `False`; it never raises**
for its two most likely failure modes:

```python
if not self.is_active or not self.has_remote(remote):
    return False          # git_manager.py:798-799 — no log line at all
```

That early return emits *nothing* — not even a warning. The `logger.warning` calls further
down only cover the case where git actually ran and exited nonzero.

Now the call sites. There are six; **five discard the return value entirely**:

| site | return checked? |
| --- | --- |
| `phase.py:545`, `:739`, `:925`, `:997`, `:1099` | no — bare `git_mgr.push()` |
| `progress_commit.py:223` | no — bare `git.push()` |
| `phase.py:1146` (`push_evidence_snapshot`) | **yes** — `pushed = git_mgr.push()` |

And the protection wrapped around two of them guards against an exception the function is
written never to throw:

```python
try:
    git_mgr.push()
except Exception as e:
    logger.warning(f"[{job_id}] Final git push failed: {e}")   # phase.py:927, :999
```

So if `is_active` was false (no `.git` at the manager's resolved `_remote_cwd`) or
`has_remote("origin")` was false, every phase-boundary push across 8 phases returned `False`
and **nothing anywhere recorded it** — no exception, no warning, no log line. That matches
the evidence exactly: an 83-line log archive containing zero git lines, and a branch that
never moved.

Which of the two conditions fired is not determinable post-hoc — the workspace pod is gone
with no PVC (86 `pvc-workspace-*` exist in the namespace, none for this job). But the defect
does not depend on knowing: **a push that cannot land must not be able to seal a job
silently.**

The deliverable gate then did its job correctly: it checks the branch HEAD in Gitea, found
the contracted artifact missing, and bounced twice before sealing —
`actions=['deliverable gate: bounce cap reached (2) with 1 still missing — sealing as
pending_review with report', ...]`.

## Defect 2 — `write_file` and the shell resolve the same relative path differently

This is why the agent could not diagnose Defect 1, and why it argued with a gate that was
telling the truth.

| tool | anchor for a relative path | mutable? |
| --- | --- | --- |
| `write_file` / `read_file` / `file_exists` | the workspace root, `/home/agent-host/workspace` | no |
| `shell_execute` | the tmux tab's cwd | **yes** — any `cd` moves it |

The agent had `cd /home/agent-host/workspace/repo` at the top of its shell blocks (that is
where the product tree lives, and where pytest/ruff must run). From then on the string
`output/ui_recovery_report.md` denoted **two different files**:

- via `write_file`/`read_file` → `/home/agent-host/workspace/output/ui_recovery_report.md` — exists
- via the shell → `/home/agent-host/workspace/repo/output/ui_recovery_report.md` — does not

Both tools answered honestly about different files. The agent's corrective ritual —
`cd repo` then `git add output/ui_recovery_report.md` — matched nothing, `git commit`
printed "nothing to commit", and **the block exited 0**. `repo/output/` exists and is
populated (5 files plus `repros/`), so nothing looked wrong. It concluded the gate was
anchored to a stale commit ("merge-base 098bf3fe6dbb was the historical bounce-check
anchor") and re-sealed. `098bf3fe` was not historical; it was — and remains — the live tip.

Two things make this invisible from the model's seat:

- **The tool contract never names the anchor.** `write_file`'s docstring says only
  *"path: Relative path for the file (e.g. `research.md`)"* — relative to what is never
  stated (`src/tools/workspace/files.py:905`).
- **The result echoes the input.** `write_file` returns `f"Written: {path}"`
  (`files.py:999`) — the same string it was handed, revealing nothing about where it landed.
  `read_file` behaves the same. Meanwhile `shell_execute` *does* report its absolute
  location (`CWD: …`), added by `f41970ae` *"fix(shell): anchor working directories and
  report cwd"* on 2026-08-01, ~2h before this job started. The shell was instrumented
  because cwd drift had bitten there; the file tools were assumed immune because they are
  root-anchored. True, and exactly the blind spot — immune to drift, but silent about which
  root.

Related gap in that same fix: the cwd restore only fires on `if working_dir and
self._sandbox_cwd:` (`src/core/backends/remote.py:1389`), i.e. when the caller passes a
`working_dir` argument. An in-body `cd` — what models actually write — does not trip it.

### This is a compliance failure, not a documentation gap

Uncomfortable but important: `f41970ae` *also* added this to `shell_execute`'s docstring
(`src/tools/shell/shell_tools.py:524-528`):

> Always set `working_dir` for commands. Do not use `cd` unless absolutely necessary — when
> `working_dir` is omitted, an inline `cd` persists in that tab for the rest of the job. A
> supplied `working_dir` is resolved relative to the workspace root and the root is restored
> when the command finishes.

So the agent had an explicit prohibition, a purpose-built `working_dir` parameter, and
`CWD: /home/agent-host/workspace/repo` printed in every result. It used an inline `cd` in
every shell block anyway. Guidance the model reliably ignores is a comment, not a control —
which is the argument for enforcing the anchor in the tool rather than documenting it
harder. Note this cuts only one way: `write_file`'s docstring still never names its anchor,
so there is a real documentation gap *and* a compliance failure, in different tools.

## Defect 3 — a Mode A job that pushes nothing has no exit

Because `projects.main_cloud_folder_handle` is set for `Better Resavio`,
`_with_cloud_review_mode` (`orchestrator/main.py:8378`) computes
`cloud_review_mode = 'diff'` — Mode A. Every export affordance is gated on Mode B
`'open_folder'`:

| site | gate |
| --- | --- |
| `cockpit/.../job-review.component.ts:186` | `cloud_review_mode === 'open_folder'` |
| `cockpit/.../job-list.component.ts:268` | `status === 'completed' && cloud_review_mode === 'open_folder' && !exported_at` |
| `cockpit/.../job-list.component.ts:375` | `status === 'completed' && cloud_review_mode === 'open_folder'` |
| `POST /api/jobs/{id}/export-to-cloud` | 409s when `project_has_cloud_folder` (`main.py:16947`) |

That is deliberate — Mode A jobs are meant to use the diff accept/reject flow instead. But
that flow renders only when `diff_status === 'pending'` (`job-review.component.ts:91`), and
`capture_job_diff` leaves `diff_status` NULL:

```python
head = await _read_head_commit(gitea_client, repo_name, branch)
...
if head == baseline:
    return False                      # job_cloud_baseline.py:415
```

Branch HEAD never moved, so `head == baseline`, so no diff was captured, so `diff_status` is
NULL. The job falls through into the generic review-content branch, whose only cloud
affordance is the Mode B button it can never satisfy.

**Net:** `pending_review` + Mode A + `diff_status IS NULL` = no diff UI, no export button,
no API path, no way to move the output anywhere from the cockpit. Verified DB state:

```
status       | pending_review
diff_status  | (null)
exported_at  | (null)
```

## Recovering the content

Unpushed `write_file` content survives in the audit store. The arg key is **`path`**, not
`file_path` — the obvious query silently returns nothing:

```sql
SELECT tc->'args'->>'content'
FROM llm_requests r, LATERAL jsonb_array_elements(r.response->'tool_calls') tc
WHERE r.job_id = '<uuid>'
  AND tc->>'name' = 'write_file'
  AND tc->'args'->>'path' = 'output/ui_recovery_report.md'
ORDER BY r.timestamp DESC LIMIT 1;
```

Use `llm_requests`, not `agent_audit` — the latter truncates.

## Investigation status (k3d)

Isolating each defect on the local cluster, since the production evidence is exhausted (pod
gone, no PVC, log archive truncated to 83 tail lines).

### E1 — shell cwd persists across separate calls · **CONFIRMED 2026-08-02**

Run against real tmux in a live workspace pod (`workspace-3adc5d1b-789`, k3d), driving
`tmux send-keys` the way `RemoteBackend` does:

| step | command sent | observed cwd |
| --- | --- | --- |
| 1 | `pwd` | `/home/agent-host/workspace` |
| 2 | `cd /tmp/repo` (in-body, no `working_dir`) | — |
| 3 | `pwd` — **separate invocation** | `/tmp/repo` |

Step 3 never mentioned the directory. This is the live-tmux behavioural gate that CLAUDE.md
records as never having been exercised; it now has been, and it reproduces. Combined with
`write_file` resolving against the workspace root unconditionally, Defect 2's mechanism is
fully accounted for.

### E2 — why did the phase-boundary push not land? · **H1/H2 REFUTED · BLOCKED ON LLM KEYS**

**Instrumentation landed** (`d7e05c39`): both early returns in `GitManager.push` now log at
WARNING, and `_inactive_reason()` separates a missing git binary from a missing `.git`. The
next occurrence is diagnosable from the log alone. Deployed to k3d as agent image
`tilt-60a46ecb9ec21cca` and verified present in the image.

**Both original hypotheses refuted against live workspaces** (2026-08-02):

| workspace | `.git` | `origin` | branch | push dry-run |
| --- | --- | --- | --- | --- |
| k3d `workspace-3adc5d1b-789` | present | configured | `job/3adc5d1b` | — |
| dev `workspace-cd3bfe52-c01` (job running) | present | configured | `main` | **succeeds** (`3d041bd..96abd59`) |

So the push mechanism itself works: credentials, permissions and remote are all fine, and a
dry run lands. H1 and H2 do not reproduce on a healthy job.

**Note two distinct repo layouts** — bbce4bed used a project-wide repo
(`project-68137e29-jobs`, branch `job/bbce4bed`); `cd3bfe52` uses a per-job repo
(`job-cd3bfe52`, branch `main`). Any fix must hold for both.

**Confirmed the file is genuinely gone**, not merely misfiled: no `job-bbce4bed` repo exists
in Gitea; `main` of `project-68137e29-jobs` is at the same `098bf3fe` as the job branch; and
`git log --all -- output/ui_recovery_report.md` is empty across every ref.

**What remains unknowable post-hoc:** every git command bbce4bed ran began with
`cd /home/agent-host/workspace/repo`, so the workspace root's branch was never captured in
the audit. We know only that the root had HEAD `4d1a23fe` at freeze time — a commit absent
from Gitea, i.e. real local commits that never pushed. A third hypothesis is now open:

- **H3** — the root repo was on a branch other than `job/bbce4bed` (a `subjob/…` ref, cf.
  [[project_resume_inherits_subjob_branch]]). `push()` auto-detects via
  `git branch --show-current` (`git_manager.py:804`), so it would have pushed that branch and
  left `job/bbce4bed` untouched. `workspace-cd3bfe52-c01` carries a local
  `subjob/09c3f309/critic` branch, so the ref does exist in live workspaces.

**Blocked:** running a real multi-phase job on k3d needs LLM credentials, and every key in
`deployment/values-local.yaml` is empty (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`GROQ_API_KEY`, `OPENROUTER_API_KEY` all `""`); the `models` catalog is correspondingly
empty. Needs a key before E2 can run.

### E2 (original hypotheses, retained for the record)

Static analysis has already reduced this to two candidates, both of which produce exactly
the observed silence (see "The failure is silent by construction"):

- **H1** — `is_active` false: no `.git` at the manager's resolved `_remote_cwd`.
- **H2** — `has_remote("origin")` false: no remote configured on the job repo.

Both return `False` with no log output, and five of six call sites discard the return. So
the k3d run is no longer a fishing trip — instrument `push()` to log *which* early return
fired, run a multi-phase job, and read it directly. k3d is decisive here because agent pod
logs are live rather than the truncated S3 archive that hid this in production.

Assertions for the run: branch tip advances at each phase boundary; `output/manifest_status.json`
appears on the branch; `push()` returns `True`.

### E3 — Mode A dead zone · **CONFIRMED 2026-08-02 · WORSE THAN FILED**

Verified end-to-end on k3d against **unmodified existing data** — project
`7ceb84dc` (`e2e-scholar-clone-fix`), which has a cloud folder. No state was fabricated.

Every completed job in it is stranded:

| job | status | `cloud_review_mode` | `diff_status` | `cloud_diff_baseline_commit` |
| --- | --- | --- | --- | --- |
| `d23cf1b5` | completed | `diff` | **NULL** | absent |
| `22fd993c` | completed | `diff` | **NULL** | absent |
| `1f7bb260` | completed | `diff` | **NULL** | absent |
| `77bbb5bd` | completed | `diff` | **NULL** | `37be759cbd1f` |
| `d029762b` | failed | — | NULL | `37be759cbd1f` |

And the export endpoint refuses, as designed:

```
POST /api/jobs/d23cf1b5-.../export-to-shared-folder
HTTP 409 {"detail":"Job's project has a cloud folder — use the diff-review
          (accept/reject) flow instead of shared-folder export."}
```

**This is not an edge case triggered by a failed push.** It is the *default* outcome for
Mode A jobs on this cluster — 4 of 4 completed jobs have no diff UI and no export
affordance. Two distinct routes reach it:

- **No baseline seeded** (3 of 5): `capture_job_diff` bails at
  `if not baseline: return False` (`job_cloud_baseline.py:401`) before it looks at anything.
- **Baseline seeded but diff empty** (`77bbb5bd`): bails at `head == baseline` (`:415`) or at
  the empty project-folder file list (`:423`).

Either way `diff_status` stays NULL, the Mode A review flow never renders, and the Mode B
button it falls through to is unreachable by construction. bbce4bed was not unlucky; it hit
the normal path.

#### Production blast radius (dev cluster, 2026-08-02)

| Mode A jobs | count | `diff_status IS NULL` | no baseline | ever exported |
| --- | --- | --- | --- | --- |
| completed | 211 | **210** | 30 | **0** |
| pending_review | 9 | **9** | 4 | **0** |

219 of 220 Mode A jobs are stranded, and the export path has never once succeeded. Note only
30 lack a baseline — so the dominant failure is *not* missing seeding.

#### The `projects/` filter is correct — it is a cloud-folder mirror, not a noise filter

Read the accept path before touching it. `projects/<slug>/` is a **two-way mirror of the
user's cloud folder**, not an arbitrary scope:

- `seed_project_folder_baseline` walks the project's cloud folder and pushes its files into
  Gitea under `projects/<slug>` (`job_cloud_baseline.py:105`).
- The agent edits them there.
- Accept "writes/deletes each diff path back via the cloud backend" (`main.py:16738`),
  mapping `projects/<slug>/sub/file.md` → `sub/file.md` (`:484-489`).

So **dropping the filter would be actively destructive**, not merely noisy: accept would try
to write `output/`, `archive/`, `tmp/`, and — in `project-68137e29-jobs` — `repo/.venv/` and
`repo/.coverage` into the root of the user's cloud storage, and would issue **deletions**
there for every `deleted` diff entry. Paths outside the prefix have no cloud counterpart and
`_strip_prefix` would not even map them.

Files outside `projects/` are not "shadow changes" being hidden. The Mode A diff answers
"what will be written to your cloud folder", not "what did this job change".

#### Actual root cause: the cloud folders are empty, so the mirror never exists

The seed reports success while finding nothing. Across Mode A jobs: **247 `state=ready`**,
28 `failed`, 45 unset — yet every `ready` job recorded **zero entries**:

```
job       seed_state  entries  n_files
f0e20d10  ready       object   0
bbce4bed  ready       object   0
4268052c  ready       object   0
d1894a91  ready       object   0
```

The projects' cloud folders are **empty**, so the seed walks them, pushes nothing, stamps a
baseline and reports `ready`. `projects/<slug>/` therefore never exists — confirmed on job
`58027ee7`: 0 files under `projects/` at its branch head *and* at its baseline `098bf3fe6d`,
while 50 other files changed.

The full chain:

1. Project cloud folder is empty → seed pushes 0 files, reports `ready`.
2. `projects/<slug>/` never exists, so the agent has nothing to edit there — and its
   deliverable contract points at `output/` anyway.
3. Diff scoped to `projects/` finds nothing → `diff_status` stays NULL.
4. Mode A review never renders; Mode B export 409s *because* the project has a cloud folder.

**The real gap is a missing feature, not a wrong filter.** Mode A is an *in-place
cloud-folder editing* flow: it assumes the folder already holds documents the agent edits.
Mode B is the *publish-deliverables* flow. A project with a cloud folder is routed to Mode A
and thereby locked out of Mode B — so when its jobs produce deliverables in `output/`,
**nothing publishes them anywhere**. That is precisely the missing button.

## Suggested fixes

**Defect 1 (the one that lost the file).** Three changes, none large:

- Make `push()`'s early return say why. `if not self.is_active or not self.has_remote(remote)`
  (`git_manager.py:798`) should log which condition fired, at WARNING. Today it is the only
  failure path in the function that emits nothing.
- **Check the return value.** Five of six call sites discard it. A phase boundary whose push
  returned `False` should not be treated as a completed boundary.
- A job that never advanced its branch must not seal clean — surface it into `freeze_data`
  so the orchestrator can refuse, rather than logging into an archive that is truncated by
  the time anyone looks. `has_unpushed_commits()` (`git_manager.py:832`) already exists and
  needs no network round-trip, so the seal path can cheaply assert "nothing left behind".

**Defect 2 (the one that made it undiagnosable).** Do not change resolution semantics —
making the file tools cwd-relative would put writes at the mercy of mutable shell state and
reintroduce the drift class `f41970ae` just closed. Instead:

- Return the **resolved absolute path**: `Written: /home/agent-host/workspace/output/ui_recovery_report.md (14248 bytes)`.
  The root is on hand as `workspace.workspace_path` (`src/core/workspace.py:390`). Then the
  write result and the shell's `CWD:` line sit in the same transcript and the gap is
  readable. Same for `read_file` and `file_exists`.
- State the anchor in the tool docstrings: *relative to the workspace root, independent of
  the shell's working directory*.
- Fire the cwd restore for in-body `cd`, not only for the `working_dir` argument
  (`remote.py:1389`).

**Defect 3 (now the most severe — the feature has never worked).**

- **Do NOT widen the `projects/` filter.** It is the cloud-folder mirror prefix; accept
  writes those paths back into the user's storage. Widening it would publish `output/`,
  `archive/`, `tmp/` and `.venv/` into their cloud root and issue deletions there.
- **DECIDED 2026-08-02: wait for §6.3. No interim.** Removing the
  `project_has_cloud_folder` refusal at `main.py:16947` would restore the export button
  today, but publishes without a review step and adds a line to delete later. Since 219 of
  220 Mode A jobs are stranded and there have been **zero** exports ever, nothing depends on
  this path — so there is no user to unblock, and the interim buys little. Revisit only if
  someone actually needs output published before §6.3 lands.
- **Retire Mode A rather than repair it.** Once the cloud folder is a change-capable
  datasource, the mirror/diff/accept flow is a parallel implementation of the same concept.
  Keeping both is how two subsystems end up with incompatible conventions — which is exactly
  how bbce4bed's deliverable landed where nothing publishes from. The zero-exports figure
  makes retirement cheap.
- **Do NOT extend Mode A's mirror to cover deliverables.** An earlier revision of this doc
  proposed widening the diff scope to `projects/<slug>/` ∪ declared deliverables and
  extending the writeback to match. That would invest in a mechanism
  `workspace_and_change_records.md` is replacing: under that design the cloud folder becomes
  a **change-capable datasource** the agent writes to directly (§6.3), with a `kind`-tagged
  change record merging to `main` — not a folder mirrored into git, edited there, and
  written back. Mode A's mirror is the older shape of the same idea. Build §6.3's cloud
  destination rather than deepening the mirror.
  Note §6.3 is gated on write-scoped per-datasource credentials and branch policy, which is
  an open decision (`writable datasources`), so the 409 removal above is the interim.
- **Fix the seed's success reporting.** Walking an empty cloud folder and recording
  `state=ready` with zero entries is indistinguishable from real success. It should record
  "seeded 0 files — nothing to mirror", which would have made this visible immediately.
- **Then give the empty-diff case an explicit state** rather than a fall-through: when
  `cloud_review_mode === 'diff'` and `diff_status IS NULL`, render "no changes were pushed to
  this job's branch" with the branch ref and tip — so a genuine empty diff is legible instead
  of silently landing in a pane with no applicable action.
- **Alarm on the invariant.** 219 of 220 Mode A jobs stranded and 0 exports ever should not
  have needed a human to notice. A counter on "Mode A jobs sealed with `diff_status IS NULL`"
  would have surfaced this the first week.
- Consider surfacing the gate's seal reason too, so the user reads "sealed after 2 bounces,
  1 deliverable still missing" instead of reconstructing it from the freeze summary.

## Verification

For Defect 1: run a multi-phase job and assert the branch tip advances at each phase
boundary; assert `output/manifest_status.json` is present on the branch. Both fail today.

For Defect 2: from a shell `cd`'d into a subdirectory, `write_file("output/x.md")` then
`shell_execute("cat output/x.md")`. They disagree today, and nothing in either result
explains why.

For Defect 3: freeze any job on a cloud-folder project `job_complete` without pushing and
open it in the cockpit. There should be an explanatory empty state; today there is a review
pane with no applicable action.
