# Session git-versioning push — verification runbook

Manual verification that the **per-turn git push fix** for persistent sessions
works end-to-end: that the version-history UI stays current as the agent works,
and that nothing is left stranded on the workspace pod. Run this after the fix
is built and the dev **agent** image has rolled. Re-run it later as a regression
check.

This is a living document — tick the boxes as you go, add notes for anything
unexpected.

**Target time:** ~15 minutes.

Related:
- Code: `src/persistent_graph.py` (per-turn commit/push block),
  `src/managers/git_manager.py` (`has_unpushed_commits`).
- Unit tests: `tests/test_persistent_graph.py::TestAutoCommitGit`,
  `tests/test_managers_git.py::TestHasUnpushedCommits`.
- Feature doc: [`docs/git.md`](../git.md) (job-centric — the per-turn *session*
  push cadence is not documented there; this runbook is its only coverage).
- Original evidence: session `cafdb9c7` (2026-06-03) had local HEAD at
  `Auto-commit after turn 9` but Gitea `origin/main` only at turn 5 →
  `git status` read `ahead 4`. The four missing turns were committed locally and
  never pushed.

## What was broken / what was fixed

**Bug.** Every tool-using turn commits to the workspace's local git repo on the
workspace pod — that always worked. But the *push* to Gitea (which the
version-history UI reads) was throttled to `turn_count % 5 == 0` **and** nested
inside the `tool_calls_this_turn > 0` gate. So:

- commits from turns between multiples of 5 weren't pushed until the next 5th
  tool-turn, and
- a session ending on a no-tool turn (a final "thanks", "Test", …) never pushed
  its trailing commits at all.

Result: the UI showed stale history. Commits were never lost — just stranded
locally until *some* later push (5th-turn / session detach / idle-timeout /
manual compaction).

**Fix (B + C).**

- Push now runs **every turn** — the `% 5` throttle is gone.
- Push is **decoupled from the tool-call gate** — it fires on no-tool turns too,
  whenever `GitManager.has_unpushed_commits()` is true.
- New `has_unpushed_commits()` compares `origin/<branch>..HEAD` via the local
  remote-tracking ref (no network), so turns with nothing to push skip the
  round-trip.
- Commit/push failures now log at **WARNING** instead of being swallowed at
  debug.

## 0. Preconditions — confirm the fix is actually deployed

The loop runs in the **agent pod** (`srw-agent-j-*`, container `agent`), from
`/app/src/persistent_graph.py`. New sessions attach to an existing long-lived
agent pod, so after building the fix you must confirm the agent pod has rolled
to the post-fix image — otherwise you'll test old code.

```bash
NS=superhuman-remote-worker
kubectl get pods -n $NS | grep srw-agent-j     # pick one
AGENT=srw-agent-j-XXXXXXXX                      # fill in

# Fixed code has has_unpushed_commits and NO "turn_count % 5"
kubectl exec -n $NS $AGENT -c agent -- \
  grep -nE "turn_count % 5|has_unpushed_commits" /app/src/persistent_graph.py
```

- [ ] Output shows **`has_unpushed_commits(...)`** and **no** `turn_count % 5`
      line.
  - If you still see `turn_count % 5 == 0`, the pod is running pre-fix code —
    **stop**; roll the deployment / confirm the image tag includes the fix
    commit. (Reference: the pre-fix code had `turn_count % 5 == 0` at
    `persistent_graph.py:308`.)

## 1. Automated tests (fast gate)

```bash
cd <repo>
source .venv/bin/activate          # local env is Py3.14-noisy but these run; CI is the real gate
python -m pytest \
  tests/test_managers_git.py::TestHasUnpushedCommits \
  tests/test_persistent_graph.py::TestAutoCommitGit -q
```

- [ ] All pass (5 + 8 tests). The load-bearing regression test is
      `test_push_fires_on_no_tool_turn_when_commits_unpushed` — it reproduces the
      turn-10 scenario from session `cafdb9c7`.

## 2. Live test — commits reach Gitea every turn

The decisive check. Start a fresh session and verify `origin/main` advances **on
every file-changing turn**, with nothing left "ahead" — including after a no-tool
final turn.

### Setup

1. In the cockpit, start a **new persistent session**. Note the thread short-id
   (first 8 chars of the UUID), e.g. `abcd1234`.
2. Find its workspace pod and define a helper that prints local-vs-pushed state:

```bash
NS=superhuman-remote-worker
TID=abcd1234                                     # your thread short-id
WS=$(kubectl get pods -n $NS -o name | grep "ws-thread-$TID" | head -1)
WS=${WS#pod/}

gitstate() {
  kubectl exec -n $NS "$WS" -- sh -c '
    cd /home/agent-host/workspace
    G="git -c safe.directory=*"
    echo "--- status ---";                 $G status -sb
    echo "--- local HEAD ---";             $G log --oneline -6
    echo "--- origin/main (Gitea / UI) ---"; $G log --oneline -6 origin/main 2>&1'
}
```

### Steps (wait for each turn to fully finish before running `gitstate`)

- [ ] **Turn 1 (tool turn):** *"Create a file notes.md with a few lines."* →
      `gitstate`
  - **Expected:** `status` shows `## main...origin/main` with **no `[ahead N]`**;
    `origin/main` includes **`Auto-commit after turn 1`**.
  - *Old/buggy behavior, for contrast: `origin/main` would NOT have turn 1, and
    `status` would read `[ahead 1]`.*
- [ ] **Turn 2 (tool turn):** *"Add another section to notes.md."* → `gitstate`
  - **Expected:** `origin/main` includes `Auto-commit after turn 2`; `ahead 0`.
- [ ] **Turn 3 (no-tool turn):** a pure-chat message, e.g. *"danke!"* →
      `gitstate`
  - **Expected:** no new commit (nothing changed); still `ahead 0`. Nothing
    stranded.
- [ ] **Turn 4 (tool turn), then Turn 5 (no-tool turn):** make one more file
      change, then end on a plain message (*"das war's"*). → `gitstate`
  - **Expected (the core regression):** `origin/main` includes
    `Auto-commit after turn 4`, and `status` is `ahead 0` even though the session
    ended on a no-tool turn. Under the old code this left turn 4 stranded
    (`[ahead 1]`+).

### Cross-check in the UI

- [ ] Open the session's version-history panel in the cockpit — it should list
      the latest turn's commit with no lag. (Direct Gitea view:
      `https://git.superhuman-remote-worker.com/srw/thread-<TID>`.)

## 3. (Optional) Surfacing push failures — the "C" half

Verify a failed push is logged, not silently swallowed.

1. Copy the real remote URL first (it carries the push token), then point the
   remote at a bad URL to force a failure:

```bash
kubectl exec -n $NS "$WS" -- sh -c '
  cd /home/agent-host/workspace
  git -c safe.directory="*" remote -v                      # copy the origin URL
  git -c safe.directory="*" remote set-url origin \
    http://invalid:invalid@srw-gitea:3000/srw/nonexistent.git'
```

2. Do a file-changing turn in the session, then check the agent pod logs:

```bash
kubectl logs -n $NS $AGENT -c agent --tail=300 | grep -i "push failed"
```

- [ ] A **WARNING** line appears:
      `Turn N: workspace git push failed — unpushed commits remain only on the
      workspace pod …`

3. Restore the remote (or just discard this throwaway session):

```bash
kubectl exec -n $NS "$WS" -- sh -c '
  cd /home/agent-host/workspace
  git -c safe.directory="*" remote set-url origin <the-url-you-copied>'
```

## Pass / fail summary

| Check | Pass criteria |
|------|---------------|
| Precondition | Agent pod code has `has_unpushed_commits`, no `% 5` |
| Unit tests | `TestHasUnpushedCommits` + `TestAutoCommitGit` green |
| Every tool turn | `origin/main` advances the same turn; `status` not `[ahead]` |
| No-tool final turn | `status` = `ahead 0`; last file-changing turn's commit is in Gitea |
| Clean turns | No needless push, no error spam in agent logs |
| Push failure (opt) | WARNING logged in agent pod, not silent |

## Cleanup

- [ ] End / delete the throwaway test session(s) so the workspace pod is GC'd.
- [ ] If you ran step 3, make sure that session is discarded (don't leave a
      broken remote behind).

## Notes / observations

_(Record date, agent image tag, and anything surprising here on each run.)_
