# Codex brief — integrate origin/develop into feature/stateless-agents

**Goal: merge `origin/develop` (113 commits ahead of our branch point) INTO
`feature/stateless-agents`, resolve all conflicts keeping BOTH sides'
functionality, pass the full gate, and prove the result on k3d — so the branch
is one reviewed merge away from develop.**

Scope boundary, absolute: **do not touch `develop` or `origin/develop`; do not
push anything; do not rebase.** The final merge to develop happens after this
work is reviewed. You produce: one merge commit + any follow-up fix commits on
`feature/stateless-agents`, plus your report.

---

## 0. Before anything: stop Tilt

`tilt down` first and confirm no Tilt process is watching. A mid-merge working
tree deploys to k3d (standing trap — a 10s checkout once left a CrashLooping
pod; a multi-minute merge is worse). Bring Tilt back up only at step 4.
Never `tilt trigger srw` — it uninstalls the release.

## 1. The migration collision — the one way to wedge the whole fleet

`origin/develop` added `orchestrator/database/migrations/app/0115_datasource_tombstones.sql`.
Our branch has `0115_run_queue.sql`. `migrate.py` **rejects duplicate numeric
prefixes at discovery** (`_discover` raises `duplicate migration prefix`), so a
naive merge makes every orchestrator pod refuse to start.

**The fix — exactly this, nothing more:**

1. `git mv orchestrator/database/migrations/app/0115_run_queue.sql orchestrator/database/migrations/app/0115a_run_queue.sql`
   — the runner explicitly supports lettered interstitials (glob
   `[0-9][0-9][0-9][0-9][a-z]_*.sql`; see
   `test_migration_discovery_rejects_duplicate_interstitial_version`).
   Prefix `0115a` ≠ `0115`, so discovery passes. Sort order is correct by
   construction: `_` (0x5F) < `a` (0x61), so
   `0115_datasource_tombstones` < `0115a_run_queue` < `0116_…` — on a fresh
   replay tombstones (datasource domain, independent) runs first, then
   run_queue, then its dependents.
2. **The file content stays byte-identical. Zero edits.** The ledger is
   checksum-tracked; any content change (including "fixing" the
   `-- migration: 0115_run_queue.sql` header comment) changes the checksum and
   wedges k3d with a checksum mismatch. The header comment will lie about its
   own filename — accepted, note it in the merge commit message.
3. **Do NOT edit 0116–0132 either, for the same reason** — several carry
   `depends-on: 0115_run_queue.sql` header comments that will now be slightly
   stale. They are documentation, the runner does not parse them, and every
   one of those files is already applied on k3d under its current checksum.
   Cosmetic staleness beats a fleet wedge.
4. **Do NOT rename develop's `0115_datasource_tombstones.sql`** — the dev
   cluster has it applied by filename; renaming it produces "applied but
   missing on disk" and refuses startup fleet-wide.
5. **k3d ledger repair, while Tilt is still down** (k3d has OUR old filename
   applied):
   ```sql
   UPDATE schema_migrations
      SET filename = '0115a_run_queue.sql'
    WHERE filename = '0115_run_queue.sql';
   ```
   Exactly one row updated. Byte-identical content ⇒ the recorded checksum
   stays valid. Do this BEFORE the merged orchestrator starts, or it wedges on
   "applied but missing on disk".
6. `tests/test_infrastructure_metering_migrations.py` is editable freely (it
   is a test): update any constant referencing `0115_run_queue.sql`, keep BOTH
   sides' entries from the conflict, and the head pin stays
   `0132_jobs_verification_uniq.notx.sql` (still lexicographically last).

## 2. The merge

```
git fetch origin
git merge origin/develop     # into feature/stateless-agents
```

~29 files conflict or overlap. Resolution law, from the last consolidation's
`register_agent` lesson: **keep both sides' functionality — taking either side
alone silently drops the other's fix.** Read the semantic intent of both
changes before resolving; never resolve by picking a side wholesale.

Per-file guidance:

- **`orchestrator/main.py`, `orchestrator/database/postgres.py`** — the
  highest-risk textual merges (our lane fences, Class A write, lane-aware
  verbs; their 113 commits of whatever landed). Both sides' changes coexist;
  verify every `execution_lane` predicate survives (`grep -c "execution_lane"`
  before/after — currently 8+ in postgres.py).
- **`orchestrator/database/schema_current.sql`** — NEVER hand-merge. Take
  either side to close the conflict, then regenerate with
  `scripts/schema-snapshot.sh` from the merged migration set and confirm a
  second regeneration produces no diff. Stage it in the merge commit.
- **`src/api/persistent_session.py` / `persistent_app.py` /
  `src/persistent_graph.py`** — our S2 session work vs their session-side
  changes. Keep both; then run the persistent-session suites.
- **`orchestrator/services/workspace_suspension.py`, `snapshot_service.py`,
  `ssh_helpers.py`** — **semantic overlap, not just textual**: their side
  changed suspension/snapshot behavior while our S2 changed shell ownership
  and teardown semantics on the same objects. After textual resolution, run
  the workspace/suspension suites AND do a live k3d suspension + cross-pod
  handoff smoke (step 4). If their suspension change and our incarnation
  authority genuinely conflict in design (not just in text), STOP and report —
  that is a real design decision, not a merge resolution.
- **`helm/values.yaml`, `helm/templates/configmap.yaml`,
  `helm/templates/orchestrator/deployment.yaml`** — union: our stateless
  block + their additions. `agent.stateless.enabled` and
  `agent.stateless.worker.enabled` stay **false** in `helm/values.yaml`
  defaults; the Tilt overlay keeps them on.
- **Cockpit (`persistent-chat.service.ts`, `turn-reducer.ts`, specs, i18n)** —
  keep both; `npm run test` + production build + the i18n parity check.
- **`docs/issues/BACKLOG.md`, `docs/security/endpoint_inventory.txt`, i18n
  JSONs** — mechanical union.
- **`docs/features/stateless_agents.md`** — if it conflicts at all, keep OUR
  side wholesale; §5.4.5 stays byte-identical (it is being rewritten in a
  parallel session against `gate3_adversarial_review.md`).
- **`requirements.txt`** — union; our `langgraph-checkpoint-postgres` 3.x pin
  and strict-msgpack env are load-bearing (CVE-2025-64439), do not lose them.

Post-merge sanity: `git rev-list --count HEAD..origin/develop` must be **0**;
`HomeLab/` stays untracked and untouched.

## 3. Gate (all of it, on the merged tree)

- Full `pytest tests/ -q` at the known **11 environment-failure baseline** —
  any NEW failure must reproduce on a clean `origin/develop` checkout worktree
  before you chase it here (note: a fresh worktree fails 19 helm tests
  spuriously for missing gitignored deps — baseline helm tests in the main
  tree only).
- `ruff check` + `ruff format --check`; both helm lint value sets; squawk on
  lint-covered migrations.
- **Fresh schema replay from zero** — this is what proves the 0115a ordering:
  tombstones → run_queue → dependents, no discovery error, snapshot matches.
- Cockpit: full vitest + production build + i18n parity.
- `git diff --check`.

## 4. k3d proof (bring Tilt back up only now)

1. Ledger repair from step 1.5 already applied; `tilt up`; watch the
   orchestrator start: it must apply **exactly one** new migration
   (`0115_datasource_tombstones`) and come up Ready. Any
   `duplicate migration prefix`, `applied but missing on disk`, or
   `checksum changed` line = stop and report, do not improvise repairs beyond
   the one prescribed UPDATE.
2. Pinned smoke: the README path (login, session, job to completion with
   `completed_at` set, approve, delete).
3. Stateless session smoke: one sandbox-tier session, multi-turn, forced
   cross-pod handoff — shell state and files intact (your own S2 acceptance,
   abbreviated).
4. Worker driver smoke (Tilt overlay has worker admission on): one opted-in
   job through claim → one rotation (assert zero `/complete` calls in the
   rotation window) → terminal completion.
5. Suspension/handoff interaction smoke for the `workspace_suspension.py`
   semantic overlap: suspend + resume a session across the merge's combined
   behavior.

## 5. Traps (standing, all have drawn blood)

- Tilt ships partially-edited images — `kubectl exec <pod> -- grep` a string
  you just wrote on EVERY pod before trusting a run.
- `kill -9 1` in a container does nothing; use
  `kubectl delete pod --force --grace-period=0`.
- admin-cli: use the `id_token`; ~15 min silent-401 lifetime.
- Never `git add -A` (HomeLab/ stays untracked); never manual
  `helm upgrade`/`install`; never `tilt trigger srw`.
- The 11-failure pytest baseline is py3.14 env noise; `-x` hides the backlog.

## 6. Stop rule

Stop only if: the suspension-domain overlap is a genuine design conflict
(§2), the k3d migration chain errors in any way other than the prescribed
repair, or a conflict resolution would require choosing between two sides'
functionality with no way to keep both. Everything else: absorb, fix, record
in `docs/research/stateless_agents/implementation_log.md`.

## 7. Report back with

The conflict-by-conflict resolution for the risky files (main.py, postgres.py,
persistent_*, workspace_suspension/snapshot/ssh_helpers, helm, metering test);
full gate numbers; the k3d evidence (ledger state, the one-migration apply
line, all four smokes); the `rev-list` zero-drift proof; anything in the
suspension overlap that smelled like a design conflict even if you resolved
it; and what remains unverified.
