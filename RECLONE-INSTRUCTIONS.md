# Re-clone required — code repo history was rewritten (2026-08-18)

**Audience:** any agent or human on a machine holding a clone of
`Knaeckebrothero/Superhuman-Remote-Worker` (possibly under its old remote name
`Uni-Projekt-Graph-RAG`). Follow this before doing ANY work in that clone.

**Do not commit this file into the code repo.** It belongs next to the repo or in the
private vault, never in the public tree.

## Why

On 2026-08-18 the repository's entire history was rewritten with `git filter-repo`
(sensitive personal content removed from the public history; full record in the private
vault: `knowledge-base/knowledge/operations/strategy_docs_history_purge.md`). All 11
branches and all 23 tags were force-pushed. **Every commit hash changed.** Your clone's
history and the remote's history now share no commits.

**The failure mode this file exists to prevent:** running `git pull` (or fetch+merge, or
rebase) in an old clone merges the old lineage back into the new one. The next `git push`
then **re-publishes every object that was purged** — undoing the entire cleanup in one
command. `git status` will not warn you. The merge will look normal.

## Am I affected? (10-second test)

```bash
cd <repo>  &&  git cat-file -e 22a3072d6cae 2>/dev/null && echo "OLD CLONE — re-clone required" || echo "clean (new lineage)"
```

`22a3072d6cae` is the pre-rewrite `develop` head. If your clone knows that object, it is
an old clone. **From this moment: no `git pull`, no `git fetch`, no `git push`, no
`git rebase` in it.** Read-only git commands are fine.

**⚠ The head probe alone can false-negative — it did, on the desktop (2026-08-18).** That
clone did not know `22a3072d6cae` (the final pre-rewrite CI commit was never fetched
there) and reported "clean", while local `develop` was in fact 3,586 commits of pure old
lineage sharing *no* ancestor with the remote. Any clone that stopped fetching before the
last pre-rewrite commit fails this test. The robust probe is the **root commits**, which
every old clone knows no matter how stale:

```bash
{ git cat-file -e 3dc99864e9a2cfa9afb57943164a90b53fa00d1a 2>/dev/null \
  || git cat-file -e e73f65cafe627768c5b819d78dcfd816708613de 2>/dev/null; } \
  && echo "OLD CLONE — re-clone required" || echo "clean (new lineage)"
```

A second confirmation that needs no hashes: `git merge-base develop origin/develop` — if
it prints nothing, the histories are disjoint and the clone is old.

Also look past the checked-out branch: on the desktop the old `main` head survived inside
a **stash**, and three Claude **worktrees** carried uncommitted work. Check `git stash
list` and `git worktree list` even when `develop` itself looks clean.

## Procedure (keeps the same path, preserves nested repos and untracked files)

```bash
cd <parent-of-repo>                       # e.g. ~/Repositories
# 0. Stop anything that touches the repo: agent sessions, tilt, watchers, cron.
#    Check for worktrees tied to this clone — remove them first:
git -C Superhuman-Remote-Worker worktree list
# (git -C Superhuman-Remote-Worker worktree remove <path> for each extra one)

# 1. Check for unpushed work BEFORE anything else (uses the stale origin ref — correct):
git -C Superhuman-Remote-Worker status --short
git -C Superhuman-Remote-Worker log --oneline origin/develop..HEAD
git -C Superhuman-Remote-Worker stash list
#    Nothing? Continue. Something? See "Salvaging unpushed work" below FIRST.

# 2. Move the old clone aside (do NOT delete yet):
mv Superhuman-Remote-Worker Superhuman-Remote-Worker.OLD

# 3. Fresh clone under the SAME name (absolute paths, venvs, session state keep working):
git clone https://github.com/Knaeckebrothero/Superhuman-Remote-Worker.git
cd Superhuman-Remote-Worker && git checkout develop

# 4. Carry over the nested repos and local-only state (untracked/ignored, so the old
#    clone's history cannot leak through them):
for d in knowledge-base knowledge-history HomeLab .venv cockpit/node_modules; do
  [ -e "../Superhuman-Remote-Worker.OLD/$d" ] && mv "../Superhuman-Remote-Worker.OLD/$d" "$d"
done

# 4b. If the vaults were NOT in the old clone (machines set up before the 2026-08-17
#     vault migration won't have them), clone them fresh — they are PRIVATE repos in
#     the superhuman-remote-worker org, so git must be authenticated for that org
#     (gh auth status / a PAT with read access):
[ -d knowledge-base ]    || git clone https://github.com/superhuman-remote-worker/knowledge-base.git
[ -d knowledge-history ] || git clone https://github.com/superhuman-remote-worker/knowledge-history.git
#     Both are covered by the repo's committed .gitignore (never committable into the
#     public repo) and .ignore (searchable by ripgrep) — nothing to configure.

# 4c. Repo-local git config does NOT survive a re-clone. If the old clone committed
#     under a per-repo identity (this project's commits use `srw-agent`) or carries
#     other local settings, re-apply them:
git -C ../Superhuman-Remote-Worker.OLD config --local --list | grep -vE '^(core|remote|branch)\.'
#     ^ re-apply anything you recognize (typically user.name / user.email) with:
#       git config user.name  "..."   &&   git config user.email "..."

# 5. Look for anything else worth keeping, then delete the old clone:
git -C ../Superhuman-Remote-Worker.OLD status --short | head -30   # untracked leftovers
rm -rf ../Superhuman-Remote-Worker.OLD
```

## Salvaging unpushed work (only if step 1 found commits)

Never push or merge them. Export by **content**, which survives the rehash:

```bash
mkdir -p /tmp/salvage
git -C Superhuman-Remote-Worker.OLD format-patch origin/develop..HEAD -o /tmp/salvage/
git -C Superhuman-Remote-Worker.OLD stash show -p > /tmp/salvage/stash.diff   # if stashes exist
# after the fresh clone:
cd Superhuman-Remote-Worker && git am /tmp/salvage/*.patch
```

If `git am` conflicts, apply with `git apply --3way` per patch. The patches contain your
changes only — nothing from the old lineage's history.

## Verify (all three must hold)

```bash
git cat-file -e 3dc99864e9a2cfa9afb57943164a90b53fa00d1a 2>/dev/null && echo "FAIL: old objects present" || echo "OK"
git merge-base --is-ancestor bfb11e968ad4 HEAD && echo "OK: on rewritten lineage"
git status --short          # empty (plus your carried-over untracked dirs)
ls -d knowledge-base knowledge-history   # both vaults present
```

## Rules going forward

- The vault repos (`knowledge-base`, `knowledge-history`) were **not** rewritten —
  `git pull` inside them is safe and encouraged (the updated runbook lives there).
- If you ever find another old clone or an old backup of the repo directory: do not
  fetch or push in it. Repeat this procedure.
- Old-lineage SHAs in notes/docs/memory are now dangling references; resolve commits by
  message, not hash.

## Reference — pre-rewrite → rewritten heads (2026-08-18)

| branch | old head | new head |
|---|---|---|
| develop | `22a3072d` | `bfb11e96` |
| main | `9d8e9b29` | `1a4fa92f` |
| feature/stateless-agents | `006a852a` | `094b3176` |
| etl-pipeline | `e93d2da7` | `21f73316` |
| feature/ui-cleanup | `f7f63d3f` | `fa62d665` |
| finaler-stand-finius | `746b3f0f` | `4bf92c39` |
| fix/ide-bff-happy-path | `c0c02bee` | `cb86e953` |
| fix/shell-stall-detection | `eb54e928` | `57cc6aea` |
| main-backup-31-03-26 | `19c55a22` | `5fc0a24e` |
| multi-agent-system-rework | `58e15f85` | `fd288d52` |
| revert-82-develop | `6984e816` | `2c98b098` |

(`develop`/`main` move on as development continues; the **old** column is what must be
unknown to your clone.)
