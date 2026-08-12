# Session workspace restore drops project-repo checkouts and the shell grant — agent left flailing on a half-restored workspace

> **CORRECTION 2026-08-08 (verified by a 5-agent codebase sweep — read before the body below).**
> Two premises this doc was written on have since changed, and one section is now partly superseded:
>
> - **Sessions are no longer emptyDir on dev.** PVC-backed workspaces (Branch (a),
>   `workspace.pvcEnabled`) shipped for jobs 2026-06-30 and were **extended to sessions
>   2026-08-04**; `deployment/values-experimental.yaml` sets `pvcEnabled: true`. The
>   clone-on-attach wipe that made sessions unsafe was fixed 2026-08-06
>   (`docs/done/session_workspace_wiped_by_agent_clone_on_attach.md`). So a session's
>   workspace pod now reattaches `pvc-ws-thread-<id>` on recycle, and
>   `restore_thread_workspace` already skips the S3 extract on a genuine reattach.
> - **Strand 1 is therefore MITIGATED, not cured.** The reattached PVC keeps the
>   `repos/<slug>` checkout across a pod crash/reschedule. But the root cause is
>   unchanged: the checkout is committed to `thread-<id>.git` as a contentless **gitlink**,
>   so on any Gitea-fallback path (permanent node loss, the `fresh=True` PVC discard, or
>   `pvcEnabled` off) it still restores EMPTY. **The real fix is tiny and has a precedent:**
>   the *job* workspace path appends `repos/` to `.gitignore` and commits it
>   (`src/core/workspace.py:800-813`, commit "Add repos/ to .gitignore") so nested clones
>   never become gitlinks; the *session* tool `checkout_project_repository`
>   (`src/tools/orchestrator/repositories.py`) never does this — porting those lines cures
>   strand 1 for all recovery paths, PVC or not. Prefer this over the "restore-time re-clone"
>   in Proposed fix 1.
> - **The resume-OOM cited in § Restore layering is already fixed in code**
>   (`stream_extract_snapshot` hands the tar to `ssh` stdin as an fd, no RAM buffer —
>   `orchestrator/services/ssh_helpers.py`), and that section's lifecycle citations
>   (`check_idle_threads`, idle sweeper gating on `status='ended'`) are now **dead code** —
>   idle-suspend moved to the 60s `lifecycle_reconciler_loop`. The layering *reasoning*
>   still holds, but treat those specific anchors as historical; the PVC hot-tier + the
>   `_is_volume_reclaimable` retain-on-idle invariant it argues toward are **already
>   implemented** (see `docs/features/workspace_pvc_branch_a_implementation.md`).
> - **Strand 2 (shell-grant loss on re-provision) was NOT re-investigated in this sweep** —
>   treat it as still open pending a fresh check.
>
> **UPDATE 2026-08-08 (durability track).** Strand 1's recommended cure — porting the job
> path's `.gitignore repos/` to the session `checkout_project_repository` — **shipped** as
> F1 (`_ensure_checkout_path_ignored` in `src/tools/orchestrator/repositories.py`), so new
> sessions no longer commit the checkout as a contentless gitlink and it survives every
> recovery path (PVC or Gitea-fallback). Preventive only: threads whose repo already holds a
> committed gitlink still need a one-time cleanup migration. **This issue stays OPEN** for
> **strand 2** (shell-grant persistence, un-investigated) and **strand 3** (model degeneration
> guards, not built) — do not move to `done/` until those are closed.

**Filed:** 2026-07-24, from a live investigation of persistent thread `b1758f38` ("Hotel
Rheinland ERP Job Status", project `68137e29` Better Resavio), turn 8 on 2026-07-23
22:36–22:54+ UTC. Full tool-call evidence is in the thread transcript
(`get_persistent_thread_messages`, offsets ~58–441) and the memory note
`srw_session_restore_drops_repo_checkouts.md`.

**Severity:** High. After any idle gap long enough to recycle the workspace pod,
"show me those files again" breaks: the artifacts the user was looking at are gone from
the workspace, the Canvas flips to `unavailable`, and the session agent — also silently
stripped of its shell grant — either flails for tens of minutes or rebuilds a lookalike
from KB notes instead of showing the real files. The originals are safe in Gitea the
whole time; the product just can't get back to them.

Three strands, one incident. Strand 1 is the core defect, strand 2 is an independent
regression that removes the escape hatch, strand 3 is the (model-side) failure to route
around both.

---

## Recommendation (TL;DR)

1. **Re-clone managed checkouts on restore.** `checkout_project_repository` clones a
   repo the orchestrator already knows (`Managed: True`, binding row with repo id/URL/
   branch). Record the checkout (repo id + target path + branch) in the thread's
   workspace context, and have the restore/reconcile path re-run the clone after the
   thread repo is restored — same shape as the re-seed-on-reconnect fix for unseeded
   workspace roots. Until then, at minimum stop the auto-commit from recording nested
   repos as bare gitlinks (see fix 1b) so restores fail *clean* instead of *poisoned*.
2. **Make the sandbox/shell grant survive re-provision.** The 07-17 approved workspace
   upgrade did not carry to the 07-23 rebuilt pod: the regenerated `tools/README.md` on
   the new pod dropped `run_command`/`shell_read`. Verify the persisted-tier read path
   in the reconcile/recreate flow (tier is persisted on upgrade — `persistent_app.py`
   S3b, ~`:6559` — but something between recreate and retool loses shell exposure).
3. **Loop-degeneration guard.** The agent burned ~25 min on
   `create_directory → file_exists → delete_directory` cycles and anonymous Gitea
   browsing with the git password pasted into URLs. Repeated create/delete of empty
   dirs is mechanically detectable; also treat credential-bearing URLs in browser tools
   as a redaction bug.

---

## Symptom

A session that had checked out a project repo (and had a Canvas open on a file inside
it) goes idle; the workspace pod is torn down and later re-provisioned. On the next
user request:

- the checkout path exists but is an **empty directory** (0 files);
- `get_canvas` reports `status: unavailable` (source file gone);
- shell tools (`run_command`, `shell_read`) are **no longer in the tool roster**;
- the agent cannot recover: `checkout_project_repository` would refuse without shell
  (`repositories.py:267`) *and* would refuse anyway because the empty restored
  directory makes it say "already present" (`repositories.py:295`);
- the turn degenerates into filesystem probing, KB archaeology, and anonymous Gitea
  browsing (private repos 404 without auth; API says "token is required"; ROOT_URL
  mismatch warning), all dead ends.

**Empirical proof (thread `b1758f38`):**

- **07-17 turn 4/5:** `checkout_project_repository` clones `project-68137e29-jobs`
  (turn 4 → `repos/…`, turn 5 → `output/project-68137e29-jobs`, ~53 s), agent runs
  `run_command` to check out `job/4eba7f2f`, `set_canvas` on
  `output/project-68137e29-jobs/mockups/today_front_desk_dashboard.html` → `ready`.
- **07-17 20:55 auto-commit `729d591`** ("Auto-commit after turn 5") records the
  checkout as `output/project-68137e29-jobs` **mode 160000, gitlink → `eb32f6ce`** —
  no `.gitmodules`, no content.
- **07-23 ~22:33:** fresh pod; reflog shows `clone: from
  http://srw-gitea:3000/srw/thread-b1758f38.git`. The gitlink materializes as an
  empty dir (`list_files` → "No files found in: output/project-68137e29-jobs").
- **07-23 22:36–22:54+:** Canvas `unavailable`; working tree shows `tools/README.md`
  regenerated **minus** the `run_command`/`shell_read` entries (+ new
  `read_product_guide.md`, changed `set_canvas.md`); the agent deletes the empty
  gitlink dir, then loops (`repo_checkout`, `output/hr-mockups`,
  `checkouts/project_jobs`, `jobsrepo`, `output/mockups`, `testdir`, …), polls
  `get_canvas`, `git_show`s a commit that only exists in the jobs repo, and browses
  `http://srw:<password>@srw-gitea:3000/…` anonymously. It reads
  `tools/checkout_project_repository.md` **twice** but never calls the tool.
- **Downstream (separate session, 07-24):** the agent eventually rebuilt a lookalike
  gallery from KB records (`output/hotel-rheinland-mockup-gallery.html`) instead of
  the real worker files — the user-visible cost of the lost checkout.

---

## Root cause

### 1. Nested repo checkouts are persisted as bare gitlinks and restored as empty dirs

Session workspace durability is the thread git repo (`thread-<id[:8]>.git`, created at
thread creation — `orchestrator/main.py:18171`). Every turn/idle/compaction checkpoint
commits with `git add -A` (`src/managers/git_manager.py:182,:222` via
`persistent_graph.py:766`, `persistent_app.py:6145,:5369`).

`checkout_project_repository` (`src/tools/orchestrator/repositories.py:242-329`)
clones a project repo **inside** the workspace tree via `GitManager.clone`. To git,
that nested work tree is a foreign repo: `add -A` records it as a **gitlink (mode
160000)** — a bare commit pointer, with no `.gitmodules` and no object content pushed
to the thread repo. On re-provision, the fresh clone of the thread repo reproduces the
gitlink as an **empty directory**. The checkout's content was never persisted anywhere
the restore can reach — by design of git, not by anyone's decision.

Two aggravators:

- **The empty dir poisons re-checkout.** `repositories.py:295` returns "Repository …
  is already present at `<path>`. Use shell/git tools there" when the target path
  exists — precisely the restored empty gitlink dir — and the suggested shell tools
  were also gone (strand 2).
- **The orchestrator knows everything needed to re-clone** — the binding is
  `Managed: True` with repo id, URL, role, branch — but no restore-time reconciler
  re-runs the clone.

### 2. The approved shell/sandbox grant does not survive pod re-provision

The 07-17 `request_workspace_upgrade` was human-approved and shell/git worked for
turns 4–7. The 07-23 rebuilt pod came back **without** shell: the harness-regenerated
`tools/README.md` diff (visible in the thread's own `git_diff` output) deletes the
`run_command`/`shell_read` entries, and in 25+ minutes the agent never had a shell
call succeed or even attempt one (it probed `file_exists("run_command")` instead).

Mechanism not yet pinned down — the upgrade handler *does* persist the tier
("Persist the new tier (S3b) so the suspend/resume/reconcile …",
`src/api/persistent_app.py` ~`:6559`), and the session reconcile path
(`ensure_workspace` drift probe, comment at `persistent_app.py:1030-1040`) recreates
the pod and rebinds a fresh agent. Candidates: the recreate path provisions the
default tier ignoring the persisted one; or the rebound agent's
`_load_tools_for_backend` never re-exposes shell for the restored backend; or an
agent-image change between 07-17 and 07-23 regressed shell exposure on re-attach.
**Investigation task:** check the `threads` workspace context row for `b1758f38`
(persisted tier) against the recreated pod spec and the agent's tool-registration log.

Knock-on: `checkout_project_repository` hard-requires `backend.supports_shell`
(`repositories.py:267`) — so losing the grant also removes the one tool that repairs
strand 1, with an error message that tells the agent to request an upgrade it already
had approved.

### 3. Model degeneration instead of recovery (gpt-5.6-sol via codex proxy)

Dropped into this half-restored state — post-compaction, with the compaction summary
explicitly naming `checkout_project_repository` as the next step — the agent instead:

- ran dozens of `create_directory → file_exists → delete_directory` cycles over
  guessed paths (a stuck "verify writability" heuristic);
- polled `get_canvas` repeatedly (status can't change by watching it);
- browsed Gitea with basic-auth credentials from `.git/config` pasted into URLs.
  Gitea's web UI/API ignore URL basic-auth without a 401 challenge, so every request
  was anonymous: private repos → 404, `/api/v1/user/repos` → "token is required",
  plus the ROOT_URL mismatch banner (`https://git.srw.works` vs in-cluster
  `srw-gitea:3000`). The password is now rendered in transcript text and screenshots
  — a credential-hygiene bug independent of the loop;
- never once called `checkout_project_repository`.

This strand is a consequence, not a cause — but it turns a recoverable gap into a
long, token-burning, user-visible failure, and it will recur whenever strands 1/2 put
a session in this state.

---

## Scope / blast radius

- Any session that used `checkout_project_repository` and outlives one pod recycle
  loses the checkout (strand 1) — deterministic, not a race.
- Any session whose approved upgrade predates a pod recycle loses shell (strand 2) —
  observed once; frequency depends on the unconfirmed mechanism.
- Canvas presentations sourced from files inside a checkout break with it
  (`status: unavailable`) — the user-facing face of strand 1.
- Worker jobs are **not** affected (their jobs-repo clone is provisioning-time and
  freshly re-cloned per dispatch; their clone failures were the separate, now-fixed
  timeout bug — see docs/issues/jobs_repo_clone_timeout_abandons_healthy_transfer.md).

Adjacent-but-different existing docs (none cover this):
`snapshot_restore_dead_for_jobs.md` (job-side restore never fires; sessions restore
fine — *too* fine, they faithfully restore the gitlink), `workspace_upgrade_drops_cloud_mount.md`
(RESOLVED; upgrade loses the cloud mount — same "lifecycle transition drops session
state" family), `stuck_thread_workspace_pods.md` / `persistant_shell.md` (pod GC and
interactive shell, no checkout/grant loss).

---

## Proposed fix

1. **Strand 1 — restore-time re-clone of managed checkouts.**
   - a. On successful `checkout_project_repository`, persist `{repo_id, target_path,
     branch, head}` into the thread workspace context (alongside `git_remote_url` /
     `repo_name`, `orchestrator/main.py:18174-18177`).
   - b. Stop committing gitlinks: add the checkout path to the workspace `.gitignore`
     at checkout time (the `DEFAULT_IGNORE_PATTERNS` seam, `git_manager.py:119-126`)
     so `add -A` skips it entirely. Restores then produce a *missing* dir (clean
     signal, and `repositories.py:295` no longer traps) instead of an empty one.
   - c. In the session attach/restore path, after the thread repo clone, re-run the
     clone for each recorded checkout (re-use the `checkout_project_repository`
     internals; refuse gracefully if the repo binding is gone). Canvas sources under
     re-cloned paths come back without any agent action.
2. **Strand 2 — grant persistence.** Pin down the mechanism (investigation task
   above), then make the reconcile/recreate path provision the persisted tier and
   re-expose the matching toolset. Regression test: approve upgrade → force pod
   recycle → assert `run_command` present and `checkout_project_repository` succeeds.
3. **Strand 3 — guards.**
   - Degenerate-loop detector: N consecutive create/delete cycles on empty paths (or
     N identical `get_canvas` polls) → inject a corrective system nudge or end the
     turn with a structured "stuck" status instead of burning tokens.
   - Redact `user:password@` userinfo from browser-tool URLs (request and rendered
     transcript/screenshot) — the credential belongs to the git remote, not the
     browser.

---

## Test plan (acceptance)

1. Session on sandbox tier: `checkout_project_repository` → verify checkout content;
   force idle teardown + re-provision (idle-sweeper or pod delete) → on next turn the
   checkout path has **content at the recorded branch/head**, without any agent tool
   call; a Canvas sourced from a file inside it re-renders (`status: ready`).
2. Same cycle, upgrade-approved session: after re-provision, `run_command` works and
   the tool docs list shell tools.
3. Thread repo hygiene: after checkout + auto-commit, `git ls-files -s` in the thread
   repo shows **no** mode-160000 entries.
4. Negative: delete the repo binding, recycle → restore logs a clear one-line warning
   naming the unrecoverable checkout instead of silently leaving an empty dir.

---

## References

- Evidence: thread `b1758f38-6e5b-4c0f-bf70-e3ea7eb4dbb3` turn 8 transcript; memory
  note `srw_session_restore_drops_repo_checkouts.md`; auto-commit `729d591` (gitlink),
  restore reflog (`clone: from …/thread-b1758f38.git`).
- Checkout tool: `src/tools/orchestrator/repositories.py:242-329` (shell gate `:267`,
  already-present trap `:295`, clone `:304-309`).
- Workspace versioning: `src/managers/git_manager.py` (`add -A` `:182,:222`,
  `DEFAULT_IGNORE_PATTERNS` `:119-126`); commit sites `src/persistent_graph.py:766`,
  `src/api/persistent_app.py:5369,:6139-6148`; thread repo creation
  `orchestrator/main.py:18168-18177`.
- Tier persistence / reconcile: `src/api/persistent_app.py` (~`:6559` S3b persist,
  `:1030-1040` ensure_workspace drift-probe comment).
- Related: docs/issues/jobs_repo_clone_timeout_abandons_healthy_transfer.md (worker-side
  clone failures, fixed + deployed sha-f131079 07-23), docs/issues/snapshot_restore_dead_for_jobs.md,
  docs/issues/workspace_upgrade_drops_cloud_mount.md, docs/issues/stuck_thread_workspace_pods.md.
- Canvas rendering issues found in the same incident (sanitizer strips CSS; srcdoc
  fragment links navigate to Cockpit) are a **separate** bug pair, diagnosed in the
  Cockpit renderer — file separately if not already tracked.
