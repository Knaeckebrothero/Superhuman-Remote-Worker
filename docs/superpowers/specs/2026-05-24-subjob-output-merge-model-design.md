---
tags:
  - design
  - orchestration
  - git
  - subjob
  - delegation
status: draft
phase: 1
created: 2026-05-24
related:
  - "[[subjob_branch_merge_model]]"
  - "[[subjob_merge_clobbers_parent_deliverables]]"
  - "[[repo_resolution]]"
  - "[[subagent_delegation]]"
---

# Phase 1 — Safe, expert-aware subjob output model (design spec)

## 1. Problem & context

Subjobs (scholar, critic, delegation children) currently fork the parent's branch and are
**squash-merged back into it**. The merge runs a destructive pre-merge "cleanup" that deletes
paths from the subjob branch; because the subjob merges *into* the parent, those deletions
propagate onto the parent and **destroyed real deliverables** (job `227329ed` lost its 34-PDF
corpus). A partial immediate fix landed (critics no longer merged; `documents`/`reference`
removed from the cleanup list), but the underlying *merge-the-whole-branch* model is unsafe and
inconsistent — see [[subjob_branch_merge_model]] for the full current-state map and
[[subjob_merge_clobbers_parent_deliverables]] for the incident.

This spec defines the **proper** model for what a subjob contributes to its parent and how that
contribution lands — Phase 1 of the three-phase roadmap in [[subjob_branch_merge_model]].

## 2. Goal & scope

**Goal:** a subjob can never modify or delete its parent's content, two subjobs can never
collide, and each subjob's contribution is a single, attributable, recency-ordered folder.

**In scope (Phase 1):**
- Replace the whole-branch squash-merge with an **extract-and-graft** of the subjob's `output/`.
- Apply it **uniformly** to all subjob types (orchestrator-side), retiring the agent-driven
  merge path used by delegation today.
- Remove the destructive pre-merge cleanup and the now-dead conflict machinery.

**Out of scope (later):** recursive/nested subjobs and depth policy (Phase 3); the broader
delegation depth/endpoint wiring (Phase 3); recovery of already-clobbered jobs (separate);
persistent-subjob bidirectional branch sync (only noted here).

## 3. Decisions (locked)

1. **Deliverable boundary = the subjob's `output/` folder, nothing else.** The only thing that
   propagates from a subjob to its parent is `output/`. Everything else the subjob did (plan,
   scratch, edits to the forked tree) stays on the subjob branch.
2. **Namespaced, recency-numbered target.** `output/` is grafted onto the parent as
   `outputs/<n>-<config>-<short_id>/` (e.g. `outputs/023-scholar-a1b2c3d4/`). `<n>` is a
   per-repo counter, zero-padded to 3 digits, assigned at graft time.
3. **It is an extract-and-graft, not a merge.** Pure addition under a unique path → no PR, no
   squash, no cleanup, no conflicts.
4. **Uniform & orchestrator-side.** One graft path for every subjob type. The agent-driven
   merge (`git_merge_squash`/`git_worktree_cleanup`, `subagent/N` assumptions) is retired; the
   parent agent reads `outputs/*` instead of performing git merges.
5. **Critic contributes nothing** to the branch; its verdict stays in the DB (`freeze_data`).

## 4. The model

### 4.1 Subjob lifecycle

```
parent (owns its branch)
  └─ subjob created → forks parent branch (full tree, for READ context)
        └─ subjob works on subjob/<short_id>/<config> (own plan, scratch, output/)
        └─ subjob completes
              └─ orchestrator grafts subjob:output/ → parent:outputs/<n>-<config>-<short_id>/
                 (one commit, additive)
              └─ subjob branch retained, untouched (full history viewable)
  └─ parent reads outputs/<n>-…/ (and integrates into its own work if needed)
```

The fork is unchanged (subjobs still need the parent's tree to read). What changes is the
**return path**: only `output/` travels back, relocated under a unique namespaced path.

### 4.2 The namespaced output contract

- A subjob's *entire* contribution is `output/`. Subjob authors keep writing to `output/` as
  today — **no agent-side convention change.** The relocation to `outputs/<n>-…/` happens at
  graft time.
- A subjob that needs to produce code destined for the codebase writes it under `output/`; the
  **parent integrates it** from `outputs/<n>-…/` with full project context. Subjobs never edit
  shared source directly (any such edits stay on their branch and are not propagated). This
  constraint is what makes collisions impossible and matches how subagents are actually used
  (parallel research/exploration, not parallel editing).
- The whole `output/` subtree is grafted, including its small process-control JSONs
  (`completion.json`, `job_frozen.json`, `critic_verdict.json`, …) — harmless under the
  namespace and useful as provenance.

### 4.3 The graft operation

New orchestrator function `_graft_subjob_output(job_id)` (replaces `_squash_merge_subjob`):

```
job = get_job(job_id)
guard: job exists, has parent_job_id, has branch_name + repo_name, gitea initialized
if job.context.verification_target:           # critic → contributes nothing
    set merge_status="skipped"; return {"status":"skipped","reason":"critic-not-merged"}

base_branch = parent.branch_name or "main"
src_tree   = gitea.read_tree(repo, ref=subjob_branch, path="output")   # recursive
if src_tree is empty:
    set merge_status="skipped"; return {"status":"skipped","reason":"no-output"}

n        = _next_output_ordinal(repo, base_branch)          # zero-padded, e.g. "023"
dest     = f"outputs/{n}-{config}-{short_id}"
gitea.write_files(repo, base_branch, {dest + "/" + p: bytes for p in src_tree},
                  message=f"Graft {dest} from subjob {short_id}")   # ONE commit
set merge_status="grafted"
return {"status":"grafted","base_branch":base_branch,"output_path":dest,"ordinal":n}
```

- **Mechanic:** pure additive write of files under a brand-new path. Done via the Gitea HTTP
  API (batch "change files" in one commit where available, else per-file). This reuses the
  same "read a branch's `output/` tree and copy it file-by-file, bytes-faithful" logic the
  Mode-B export already implements (`export_job_to_shared_folder`), so no local checkout and no
  new infra. No PR, no squash, no merge — collisions are structurally impossible.
- **Branch retention:** the subjob branch is never modified or deleted.

`_next_output_ordinal(repo, base_branch)`:

```
entries = gitea.list_contents(repo, "outputs", ref=base_branch) or []
nums = [int(m.group(1)) for e in entries
        if (m := re.match(r"(\d+)-", e["name"])) and e["type"]=="dir"]
return f"{(max(nums) + 1) if nums else 1:03d}"
```

Grafts are sequential (no async subjobs; delegation siblings graft in `creation_order`), so
"max existing + 1" is race-free. No schema change needed.

### 4.4 Per subjob type

- **scholar** (`_handle_scholar_completion`): graft → `outputs/<n>-scholar-…/`; the orchestrator
  sets the parent's `scholar_output_dir` (and kickoff/context) to that path so the main job
  reads the research from there.
- **delegation children** (`_handle_delegation_child_completion`): each child grafts on
  completion. When all siblings are terminal, the parent resumes; the injected
  `delegation_results` reference each child's `outputs/<n>-…/` path (instead of branch
  diffs/summaries). The parent reads them and integrates as needed. **No agent git-merge step.**
- **critic** (`_handle_critic_verdict_on_complete`): no graft; verdict consumed from DB.

## 5. Components & interfaces

| Unit | Responsibility | Depends on |
|---|---|---|
| `_graft_subjob_output(job_id)` | Extract subjob `output/`, write under namespaced path on parent, one commit. Critic/no-output → skip. | `gitea_client` (read tree, write files, list contents), `postgres_db` |
| `_next_output_ordinal(repo, base_branch)` | Per-repo recency counter from existing `outputs/<NNN>-…` dirs. | `gitea_client.list_contents` |
| graft trigger | On any subjob completion (`parent_job_id` set), call the graft. Replaces the `creation_order is None` gate that previously excluded delegation children. | completion handlers |
| delegation resume injection (`_format_delegation_results`, `agent.py`) | Point the parent at children's `outputs/<n>-…/` paths; drop branch-diff/merge instructions. | child graft results |

Trigger sites to convert from `_squash_merge_subjob` → `_graft_subjob_output`: the main
completion handler (`orchestrator/main.py:~7548`), the `approve_job` path (`~6204`), and the
`subjob-merge` endpoint (`~4200`). The delegation-child completion path must now also trigger a
graft (previously it deliberately skipped the merge).

## 6. What's removed

- `_squash_merge_subjob` and its PR + squash flow for subjobs.
- `SUBJOB_CLEANUP_FILES` / `SUBJOB_CLEANUP_DIRS` and the pre-merge delete loops.
- The `merge_status="conflict"` path (conflicts are impossible).
- Agent-side `git_merge_squash` / `git_worktree_cleanup` tools and the `subagent/N` branch +
  worktree assumptions in the delegation tooling/instructions. (`merge_pr`/`create_pr` remain on
  `gitea_client` for any non-subjob use, but are no longer called for subjob contribution.)

## 7. Invariants & guarantees

- The parent tree is **never modified or deleted** by a subjob graft (only new files under
  `outputs/<n>-…/` are added). The clobber class is eliminated by construction.
- Two subjobs **never collide** (unique namespaced path per subjob).
- The subjob branch is **retained** with full per-phase history.
- The parent receives **exactly one commit per subjob**.
- `outputs/` listing **sorts by recency** (zero-padded ordinal).

## 8. Error handling & edge cases

- **Conflict:** impossible. Defensive: if `dest` already exists (it shouldn't — ordinal is
  max+1), log an error and bump the ordinal rather than overwrite.
- **Empty / missing `output/`:** skip with `status="skipped", reason="no-output"`; the subjob
  branch still holds whatever it produced.
- **Gitea read/write failure mid-graft:** log, mark a non-terminal failure status, do **not**
  crash the parent; the subjob's `output/` is intact on its branch and the graft is retryable
  (idempotent under a fresh ordinal, or re-detect the partial dest and resume).
- **Critic:** never grafts.
- **Root/main job:** unaffected — it owns its branch and writes deliverables directly; it is not
  a subjob and never grafts into anything.
- **Multi-round subjobs** (critic returns → parent resumes): critic doesn't graft, so no issue
  in Phase 1. Persistent-subjob bidirectional branch sync is deferred (noted in the issue doc).

## 9. Testing strategy

Build on the existing `tests/test_per_job_repo.py` and its stateful fake-Gitea (extend it to
model `read_tree`/`write_files` and an `outputs/` tree). TDD; each test fails first.

- `test_subjob_output_grafted_to_namespaced_dir` — a scholar's `output/x.md` lands at
  `outputs/001-scholar-<id>/x.md`; nothing else from the branch appears on the parent.
- `test_parent_tree_untouched_by_graft` — a pre-existing parent file (e.g. `documents/corpus.pdf`,
  `src/app.py`) is byte-identical after the graft (regression for the clobber bug).
- `test_ordinal_increments_per_repo` — second graft into a repo with `001-…` yields `002-…`;
  zero-padded; recency-sortable.
- `test_critic_not_grafted` — a `verification_target` subjob grafts nothing (subsumes the
  current `TestSquashMergeDoesNotClobberParent::test_critic_subjob_is_not_merged`).
- `test_empty_output_skipped` — subjob with no `output/` → skipped, no commit.
- `test_delegation_child_grafted_and_referenced` — a delegation child grafts; the parent's
  resume injection references `outputs/<n>-…/` (not a branch diff).
- Remove/replace the old `TestSquashMergeSubjob` delete-count and cleanup-constant assertions
  (they encode the retired behavior).

Full suite must stay green (`pytest tests/ -q`; pre-existing SFTP/cloud_sync collection errors
in this venv are unrelated).

## 10. Migration & compatibility

- **No schema migration** (ordinal is derived, not stored).
- **Behavior change only:** new completions use the graft; existing repos are untouched (we
  simply stop performing destructive merges). In-flight subjobs graft on their next completion.
- Already-merged/clobbered repos are not retro-fixed here (recovery is tracked separately in
  [[subjob_merge_clobbers_parent_deliverables]]).

## 11. Out of scope / follow-ons

- **Phase 2 — unify the two worlds (remainder):** fully align delegation branch naming/creation
  with the orchestrator (`subjob/<short_id>/<config>`), retire remaining World-B assumptions not
  already removed here.
- **Phase 3 — recursion to user-defined depth:** nested grafting (a child's `outputs/*` rolling
  up through intermediate subjobs), the missing `delegation-depth` endpoint, stop force-disabling
  child delegation, depth policy (lifecycle links depth-transparent), cycle guard.
- Persistent-subjob bidirectional branch sync; already-clobbered recovery.

## 12. References

- Issue / current state: [[subjob_branch_merge_model]], [[subjob_merge_clobbers_parent_deliverables]]
- Canon: [[repo_resolution]], [[subagent_delegation]], [[subjob_worktree_sharing]]
- Code: `orchestrator/main.py` (`_squash_merge_subjob` ~360-470 → replace; `SUBJOB_CLEANUP_*`
  319-337 → remove; triggers ~7548, ~6204, ~4200; `_handle_delegation_child_completion`
  ~6677-6781; `_handle_scholar_completion`; `_handle_critic_verdict_on_complete`),
  `orchestrator/services/gitea.py` (tree read / file write / `list_contents`; export copy logic
  in `export_job_to_shared_folder`), `src/tools/git/git_tools.py` (retire `git_merge_squash` /
  `git_worktree_cleanup`), `src/agent.py` (`_format_delegation_results`),
  `tests/test_per_job_repo.py`.
