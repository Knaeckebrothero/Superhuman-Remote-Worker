# Workspace durability model — snapshot-primary, PVC-as-cache

## Status (2026-08-08, rev. 2 — research-backed)

**Design decision record + implementation spec.** Ratifies how the workspace-durability
mechanisms relate, and specifies the work that makes them compose. It introduces no new
subsystem — the pieces (tar→S3 snapshot, PVC "Branch (a)", per-thread Gitea repo, Postgres
`thread_messages`) all exist and the PVC layer is live on dev. Rev. 2 folds in a six-agent
research pass (three codebase implementation specs + three web prior-art/best-practice
sweeps); every non-trivial claim below is anchored to code (`file:line`) or a cited source.

**Thesis in one line:** the app-owned **tar→S3 snapshot is the portable durability floor**;
the **Kubernetes PVC is an opt-in hot cache** in front of it (`workspace.pvcEnabled`); storage
provisioning (Longhorn/EBS/…) is the **operator's** concern, never the chart's.

**External validation of the thesis:** Gitpod shipped essentially this exact model in
production — a daemonSet that tars `/workspace` to per-user object-storage buckets on stop and
restores on start — and their *"We're leaving Kubernetes"* post-mortem is direct evidence for
keeping the PVC optional and non-load-bearing: cross-node PVC attach was *"unpredictable"* and
*"impractical,"* and per-node disk-attach limits capped their scheduler
(https://ona.com/stories/we-are-leaving-kubernetes). Coder's product is our "optional PVC"
already generalized: persistence is a template toggle, compute is ephemeral (`start_count=0`),
the volume survives keyed by an immutable id
(https://coder.com/docs/admin/templates/extending-templates/resource-persistence).

| Piece | State | Rev. 2 change |
|---|---|---|
| tar→S3 snapshot capture/restore | BUILT (`snapshot_service`, `workspace_suspension`) | Elevate to portable floor; **make capture verifiable** (§C) |
| PVC "Branch (a)", jobs + sessions | BUILT + live on dev (`pvcEnabled: true`) | Reframe as opt-in hot cache; adopt K8s-lifecycle refinements (§D) |
| PVC-loss recovery | clones from Gitea only | **Change to extract → fetch → `git reset`** (§B) |
| Session checkout gitlink bug | OPEN (`b1758f38`) | **Fix F1** (§A) |
| Reclaim-on-idle | not built (retain-on-idle today) | Deferred policy knob, gated on §C |

Prior art this builds on — do not re-derive:
[`workspace_pvc_branch_a_implementation.md`](workspace_pvc_branch_a_implementation.md),
[`workspace_pvc_backed_migration.md`](workspace_pvc_backed_migration.md), and issue
[`../issues/session_restore_drops_repo_checkouts.md`](../issues/session_restore_drops_repo_checkouts.md).

---

## Shipped (2026-08-08) — implemented, reviewed, k3d-validated

The honest-exit-code + verifiable-capture + reclaim-on-idle work is BUILT on `develop` (unpushed), each
task TDD'd, per-task reviewed (Opus for the shell/data-destructive ones), and validated on the k3d dev
cluster:

| Piece | Commits | k3d validation |
|---|---|---|
| **F1** checkout gitignore cure | `8e7998d0` | deployed helper + real `git`: baseline gitlink reproduced, fix prevents it |
| **C1a** IDE-VM extract honest rc | `7a9bd5d8` | unit |
| **C1b** capture honest accept (PIPESTATUS) | `2f747a5f`, `2761d9bd` | runtime bash 5.2 matrix + real `tar`/`zstd` capture/restore round-trip |
| **C1c** extract `set -o pipefail` | `9b7dd366` | runtime bash 5.2 matrix + real corrupt-archive reject |
| **C1d** `ide_settings` capture+extract | `f8a6b297` | runtime bash 5.2 collapse-verdict matrix |
| **C2** `verify_snapshot` (fail-safe) | `06b3ab6c`, `b6519630`, `3a945619` | **real garage S3**: good→`ok`; sha/size/missing→reject |
| **reclaim-on-idle** (flag + verify gated) | `0a63a091` | flag-off default in deployed env; verify vs real garage |
| **C3** no-clobber upload (staging→verify→promote, keep-N) | `85b2fafe`, `bf2def54` | **real garage**: bad upload can't clobber canonical; good promotes + verifies |

**Reclaim-on-idle** (§D3) is the capacity fix: opt-in `WORKSPACE_RECLAIM_ON_IDLE` (default **off**); on
idle-suspend, once `verify_snapshot` confirms the archive restorable, the workspace PVC is deleted
(fail-safe — a verify failure keeps it). Enable it as a soak on the real (Longhorn) cluster before broad
rollout.

**F3 (quota)** — the per-class `ResourceQuota` mechanism already exists (default-off); with reclaim-on-idle
now the primary bound, F3 is a fail-closed *backstop* the operator enables with a cluster-specific cap when
desired.

**Owed follow-ups** (all non-blocking): also reclaim the session **agent PVC** (`pvc-agent-s-*` —
`AgentProvisioner` needs a delete method); clear the write-only `volume_reclaimed` marker;
`_soft_delete_snapshot` still uses single-part `copy_object` on `env.tar.zst` (>5 GB cap); the F1 cleanup
migration for already-poisoned threads; and a full session-level reclaim E2E (live suspend→reclaim→resume)
as a homelab soak. **Push held** (owner's call).

**Dev validation runbook:** [`../../tests/reclaim_on_idle_dev_validation.md`](../../tests/reclaim_on_idle_dev_validation.md)
— the step-by-step for validating the reclaim cycle on the real (Longhorn) cluster: confirm the flag-off
deploy, check `verify_snapshot` against real dev S3, then arm the flag and drive suspend→reclaim→resume on a
**throwaway test session**, asserting a gitignored real-work file survives the round-trip.

**Still design-only (NOT built) — proposals in the sections below, not shipped:** the **§B** layered
PVC-loss recovery (extract → `git fetch` → `git reset` for node loss), the **§D** PVC-lifecycle hardening
(the `crossNodeReattach` operator flag, finalizer discipline, `WaitForFirstConsumer`), and **F3** quota
*enablement* (the mechanism exists; turning it on with a cap is an operator decision).

---

## Prior art & rationale (why the decisions below are the right ones)

Distilled from a survey of Codespaces, Gitpod, Coder, E2B, Daytona, Modal, Replit, CodeSandbox,
Devpod (sources at the end), plus Kubernetes storage docs and backup-integrity literature.

**Patterns we adopt:**

1. **App-owned archive to object storage is a validated floor.** Gitpod (tar→S3 daemonSet, per-user
   bucket) and Daytona (*"archive moves a stopped sandbox's filesystem to object storage and frees
   disk quota"*) both run it in production. It depends only on an S3 API — the object-storage analog
   of our StorageClass-only constraint. We adopt Gitpod's **per-owner bucket/prefix layout**.
2. **Persistence as a config toggle; compute ephemeral, volume the only persistent piece** (Coder).
   Maps 1:1 to `pvcEnabled` + our owner-keyed PVC name. Rule inherited: **key the volume by an
   immutable owner id, never a mutable field**, or a reconcile recreates it and loses data.
3. **Two restore paths: a fast warm resume and a slower cold restore, the cold one always sufficient
   alone** (E2B `keepMemory:false`, CodeSandbox memory-snap vs cold archive, Daytona stopped vs
   un-archive). Our PVC reattach is the warm path; the snapshot floor is the cold path. Restore must
   **prefer** the cache and **degrade** to the floor — never require the cache.
4. **Explicit multi-timer tiering** (Daytona `auto-stop`/`auto-archive`/`auto-delete`; Gitpod hard
   8h/36h lifetime caps). Separate the cost dimensions: one timer idle→snapshot+release (bounds
   compute + hot cache), another TTL-GCs the cold copy (bounds object storage). A hard max-lifetime
   cap kills zombie sessions.

**Anti-patterns we design against (all documented by the above):**

- **Cross-node block-volume reattach is slow/unreliable and caps scheduling** (Gitpod). → PVC stays
  optional and non-load-bearing; the floor never assumes reattach.
- **Decompress/restore I/O storms** — Gitpod needed *"fixed IO bandwidth limits per environment"*
  because decompression *"consumes most available CPU on a node"* and starves neighbors. → rate-limit
  snapshot/restore I/O+CPU per workspace (§Idle policy).
- **Only a blessed subset of the filesystem persists → silent data loss** (Gitpod `/workspace`-only,
  Codespaces `/workspaces`-only). → **document exactly what our snapshot captures** (§Architecture)
  and test it.
- **Repeated-resume corruption** (E2B issue #884: files stop persisting after the 2nd resume). → make
  writes idempotent and **checksum the archive** (§C); test across many suspend/resume cycles.
- **Hot layer silently expires with no fallback** (Modal memory-snap hard 7-day TTL). → TTL the PVC/hot
  cache, but the S3 snapshot must outlive it.
- **Restore = a new clone, identity must be re-wired** (Modal). → a restored workspace rebinds to the
  same session/DB row + orchestrator/NATS ids (already true for us; keep it).
- **In-cluster object store shares the cluster's failure domain** (Gitpod self-hosted). → the
  object-storage floor should be able to live independently of the workspace cluster.

**Rejected alternative — object-store-backed POSIX as the hot layer** (JuiceFS, SeaweedFS, s3fs/goofys,
CSI-S3): every option becomes a *bundled storage dependency*, violating D1. JuiceFS needs a separate
metadata engine **and** an object store (https://github.com/juicedata/juicefs); SeaweedFS needs its own
server cluster; FUSE-over-S3 has weak POSIX semantics and still mandates an endpoint; CSI-S3 is itself a
mandated driver. Our tar→S3 + optional-PVC achieves the same result with *less* mandated infra.

---

## The decisions

### D1 — Depend on the StorageClass abstraction, never chart a storage provider

The product depends on *"a StorageClass that can provision RWO PVCs"* (`WORKSPACE_STORAGE_CLASS`,
default `longhorn-ephemeral`) and nothing more. Longhorn/any CSI provider is **out of scope for the
chart**: it is cluster infra (privileged daemonset, own control plane, iscsi deps), and running it on
top of an operator's existing cloud storage is the double-provisioning anti-pattern.

**Consequence:** node-loss durability *in the product* comes from the tar→S3 snapshot, not from
storage-layer backups. Operator-side storage DR (a global Longhorn→S3 target, EBS snapshots) is
orthogonal and outside the chart.

> **Rejected:** Longhorn-native `VolumeSnapshot`/backup — absent on-cluster (no backup target, no
> `VolumeSnapshotClass`, no external-snapshotter CRDs) **and** already deliberately rejected earlier
> (*"CSI VolumeSnapshots are a dead end on our stack"*,
> `docs/done/vm_upgrade_pause_workspace_reaped_before_approval.md`). Also breaks D2 portability.

### D2 — PVC is opt-in; the snapshot is the always-on floor (`pvcEnabled` contract)

A PVC only delivers value if it **reattaches to a pod rescheduled onto another node**. `local-path`/
hostPath and some managed tiers cannot (data is physically node-pinned —
https://github.com/rancher/local-path-provisioner). So the PVC must be optional and the snapshot must
stand alone.

| Mode (`workspace.pvcEnabled`) | Hot path | Durability floor | Where it runs |
|---|---|---|---|
| **false** (default) | emptyDir | tar→S3 snapshot | **everywhere** — the correctness floor |
| **true** | PVC reattach (`pvc-ws-thread-*`) | same tar→S3 snapshot underneath | reattach-capable storage only |

**Contract:** the snapshot path is fully functional and independent of the PVC path. Enabling PVC must
not disable capture; disabling PVC must leave a working system. Already true — capture tars the pod
filesystem regardless of backing; restore branches reattach-skip vs extract (`restore_thread_workspace`,
the `volume_reattached` skip). §B keeps it true by wiring the failure path to actually *use* the floor.

### D3 — Retain-on-idle now; reclaim-on-idle is the real capacity fix (gated on §C)

Today: **retain-on-idle** — suspend deletes the pod but keeps the PVC; a session's volumes are reclaimed
only on `threads`-row hard-delete (`_is_volume_reclaimable` → `bound_row_missing`,
`lifecycle/workspace_manager.py:448-488`). Safe, but PVCs accumulate with every retained thread — the
unbounded growth F3's quota exists to *bound in the interim*.

**Reclaim-on-idle** (on idle/graceful suspend, after a **verified** snapshot, delete the PVC; the next
touch extracts from S3 via §B's existing path) is **the actual solution to that growth**, not a mere knob:
it bounds live PVCs to *currently-active + recently-crashed* workspaces instead of *every thread ever*. In
this snapshot-primary framing it is not a new subsystem — the extract path already exists — but it is
**gated on §C** (verifiable capture): you cannot safely delete a PVC against an archive you can't confirm,
so C1b + C2–C4 are its hard prerequisite. Two things it deliberately does **not** cover:
- **Crashed / abandoned sessions keep their PVC** (the D4 invariant — never delete on a crash signal). A
  session that crashes and is never resumed holds its volume until a **conservative time-based reaper**
  (row hard-deleted, or idle-age *and* already-archived) reclaims it. Reclaim-on-idle handles the
  *graceful* path; this reaper handles the crashed tail.
- A **quota backstop (F3)** stays worth keeping as a cheap fail-closed safety net against a runaway.

So the durable bound is **reclaim-on-idle (graceful) + abandoned-crashed reaper (tail) + F3 quota
(backstop)** — F3 alone is only the interim stopgap while reclaim-on-idle isn't built.

### D4 — The PVC-deletion safety invariant (codified — keep it)

> A PVC is deleted **only** on the deliberate suspend/terminal path, gated on a reclaim signal —
> **never** on a crash/pod-gone heuristic.

`delete_workspace` is pod-only; PVC deletion is separate and gated by `_is_volume_reclaimable`. The one
knowing exception (the node-loss fresh-fallback) is resolved by §B — it discards **and recovers**,
rather than discarding into an empty volume.

---

## Architecture — three layers, and exactly what each covers

```
  PVC hot cache        (opt-in; survives pod crash + reschedule on reattach-capable storage)
        │  reattach → skip extract
        ▼
  tar→S3 snapshot      (PORTABLE FLOOR; survives pod loss, node loss, PVC-less substrates)
        │  extract → fetch → git reset   (§B)
        ▼
  Git (tracked files, last push) + Postgres thread_messages (conversation)
                       (semantic truth; push every turn; survives everything)
```

**What the snapshot tar actually contains matters and is easy to get wrong.** Capture deliberately
**excludes** `.git/objects`, `*/repos/*`, and `*/node_modules/*` (`snapshot_service.py:449-452`).
Therefore, per layer:

| State | Captured by | Recovered on node-loss by | Notes |
|---|---|---|---|
| Tracked files | git (every-turn push) | `git fetch`+`reset` (§B) | freshest = last push |
| `.git/objects` | **neither** git-floor nor snapshot | `git fetch` from Gitea | this is **why** §B must fetch before reset |
| Gitignored real-work outside `repos/`,`node_modules/` (SQLite `*.db`, logs, uploads) | **snapshot only** | snapshot extract (§B) | the non-reproducible data the snapshot exists for |
| `repos/<slug>` project checkout | **neither** (excluded from snapshot; gitignored by F1) | **re-run `checkout_project_repository`** | reproducible; PVC-durable across crash; F1 stops it corrupting the git tree |
| `node_modules/`, venvs, build output | neither | rebuild (`npm i`, etc.) | reproducible |
| Conversation / thread state | Postgres `thread_messages` (NOT LangGraph `checkpoint_blobs` — that is the job layer) | Postgres | survives everything |

So a session PVC's *marginal* value over git+Postgres is narrow and specific: **gitignored real-work +
unpushed/uncommitted work + live process state, across crash/reschedule only.** It adds nothing for node
loss (git+snapshot are the floor there). This is the honest scope to document, per the "blessed subset"
anti-pattern.

---

## §A — F1: session checkout `.gitignore` cure (fixes `b1758f38`)

**Bug (confirmed):** `checkout_project_repository` (`src/tools/orchestrator/repositories.py:250-347`)
clones a project repo as a nested git repo; the per-turn `git add -A` (`src/persistent_graph.py:1019-1022`)
then stages it as a contentless **gitlink** (mode 160000) and pushes it to `thread-<id>.git`; a fresh-pod
re-clone materializes it as an **empty directory**. The job path already cured this
(`src/core/workspace.py:800-813`, appends `repos/` to `.gitignore` and commits). The session tool never
does — and a fresh session workspace has **no `.gitignore` at all** (it's cloned, not `init`-ed, so
`GitManager.DEFAULT_IGNORE_PATTERNS` at `git_manager.py:157-163` never runs).

**Fix:** a failure-isolated helper on the session path, writing an **anchored, per-path** entry derived
from the actual `target_path` (which is caller-configurable — not always `repos/`), before the tool
returns (so it precedes the turn's `add -A`). **Do not commit in the tool** — rely on the guaranteed
end-of-turn auto-commit (a checkout is a tool-call turn; the `.gitignore` change makes
`has_uncommitted_changes()` true). This is strictly safe: if the push later fails, the commit that would
have carried the gitlink also never forms.

```python
# repositories.py, new helper near _safe_checkout_path (:130); called after the clone/branch, before return (:335)
def _ensure_checkout_path_ignored(backend, checkout_path: str) -> None:
    entry = f"/{checkout_path}/"          # leading '/' anchors to root .gitignore; can't be read as '#'/'!'
    header = "# Cloned project repositories (working-tree only; never versioned)"
    try:
        if backend.exists(".gitignore"):
            content = backend.read_file(".gitignore")
            if isinstance(content, bytes): content = content.decode("utf-8", "replace")
            if entry in {l.strip() for l in content.splitlines()}: return   # idempotent
            backend.append_file(".gitignore", f"\n{entry}\n")
        else:
            backend.write_file(".gitignore", f"{header}\n{entry}\n")
    except Exception as e:
        logger.warning("Failed to gitignore checkout path %s: %s", checkout_path, e)   # never fail the checkout
```

- **Per-path, not hardcoded `repos/`:** a custom `target_path=vendor/lib` → `/vendor/lib/`; the job-path
  hardcode would miss it. Idempotent via line-membership + the existing `backend.exists(checkout_path)`
  early-return (`:313-317`).
- **Interaction with the snapshot exclude (important):** the default checkout `repos/<slug>` is *also*
  excluded from the snapshot, so on node-loss the checkout is neither in git nor in the tar. That is
  acceptable **because a project-repo checkout is reproducible** — the correct recovery is to re-run the
  checkout, and F1's win is that the path is now a *clean absence* (re-checkoutable) instead of a
  corrupt empty gitlink that breaks Canvas and confuses the agent. Non-reproducible real-work belongs
  outside `repos/` and is snapshot-covered.
- **Test plan:** (A) mock-backend wiring tests — asserts `.gitignore` written/appended with the anchored
  entry, absent-file and existing-file branches, idempotency, custom-path verbatim, failure-isolation;
  (B) real-`git` proof — build a root repo with a real nested repo, show `add -A` produces a `160000`
  entry (reproduces the bug), then with the `.gitignore` entry assert `ls-tree -r HEAD` has **no
  `160000`** and `check-ignore` passes.
- **Migration (follow-up):** the fix is preventive; threads whose repo already contains a committed
  gitlink (including `b1758f38`) won't self-heal — a one-time cleanup (rm the gitlink, add the ignore,
  commit) is needed for already-poisoned live threads.

---

## §B — Layered PVC-loss recovery: extract → fetch → `git reset` (resolves F2)

**Gap:** when a PVC is lost (node death, or the `fresh=True` fallback), recovery today either extracts
the snapshot **or** clones from Gitea, never both — dropping gitignored/unpushed work. And gating the
fresh-fallback off for sessions is *not* a fix: a gated-off session falls to the `else` branch → status
`creating` → stuck pod (`container_provisioner.py:493-500`), worse than today. So the safe fix is to
make the discard path **recover**, and it's the same helper both restore paths call.

**The sequence must be extract → FETCH → reset, not extract → reset.** The snapshot excludes
`.git/objects` (`snapshot_service.py:449`), so an extracted workspace has `.git/{config,refs,index,HEAD}`
and the full working tree but **no object DB** — `git reset` alone would fail; `git fetch` first
repopulates objects from Gitea. (Precedent: `ide_session.py:1046 _repair_git_after_snapshot` already
fetches-after-extract for the IDE path.) `git reset --hard` only touches superproject-*tracked* paths, so
the tar's untracked/gitignored/nested content survives — **the absence of `git clean` is the entire
guarantee.**

**Command sequence** (on the pod, `agent-host@<pod_ip>:30022`, one round-trip after extract):
```sh
set -o pipefail; set -e
cd /home/agent-host/workspace 2>/dev/null || exit 3
[ -d .git ] || exit 3
BR="$(git symbolic-ref --short -q HEAD || echo main)"   # sessions default to main; works without objects
[ -n "$REMOTE_URL" ] && { git remote set-url origin "$REMOTE_URL" || git remote add origin "$REMOTE_URL" || true; }
git fetch --no-tags origin "$BR"
git reset --hard "origin/$BR"
# NO `git clean` — preserves tar-restored untracked / gitignored / nested content
```

**Hook points:**
- **Path A — `restore_thread_workspace` (workspace_suspension.py):** replace the bare extract at
  `:913-928` with the layered helper; extend the skip guard at `:906` so it also skips when
  `create_workspace` already recovered this call (dedup via a `workspace_reset_at` nonce read at `:793`).
  The job twin `restore_workspace:464` has the identical seam (out of scope; same swap with
  `entity_type="jobs"` + job branch).
- **Path B — `create_workspace(fresh=True)` (container_provisioner.py:452-492):** after the recursive
  fresh create (`:488`) the fresh pod is ready with a fresh empty PVC; re-read context, then call the
  helper; change the stamp at `:491` from `{"workspace_reset": True}` to also write `workspace_reset_at`
  (the dedup nonce). **Leave the `else`→`creating` at `:493-500` untouched** (that's the stuck-pod trap
  we avoid by keeping the fallback ON and making it recover).
- **Agent side — no change.** `_attach_existing_workspace` (`persistent_session.py:573-655`) already
  probes the pod root and returns `"reattach"` when `.git` is present (no clone, no `rm -rf`). Invariant:
  **orchestrator recovery first, agent attach second** — holds on both resume triggers (`main.py:30438`
  HTTP, `:31493` SSE, both `await` restore before dispatch) and is protected by that probe.

**Shared helper** — `recover_workspace_layered(...) -> LayeredRecovery` in a new
`orchestrator/services/workspace_recovery.py`, plus an SSH primitive `stream_git_reset_to_origin(...)`
beside `stream_extract_snapshot` in `ssh_helpers.py`:
```
LayeredRecovery = RECOVERED | EXTRACTED_NO_RESET | NO_SNAPSHOT | EXTRACT_FAILED
```
Body: `_object_exists` (disambiguate missing-vs-error, which `download_snapshot:740` collapses today) →
download → `stream_extract_snapshot` (rc≠0 → EXTRACT_FAILED) → `stream_git_reset_to_origin` (rc 0 →
RECOVERED; rc 3/≠0 → EXTRACTED_NO_RESET). Refactor `_extract_snapshot` to delegate to the same primitives
so they never drift.

**Degradation matrix:**

| Situation | Result | Path A | Path B |
|---|---|---|---|
| snapshot + `.git` + fetch/reset ok | RECOVERED | `ready` | stamp; agent attaches in place |
| snapshot, extract ok, fetch/reset fails or no `.git` | EXTRACTED_NO_RESET | `ready` (snapshot-as-of-capture; no worse than today) | stamp; agent attaches |
| **no snapshot** (pre-first-suspend) | NO_SNAPSHOT | `failed` (a suspended thread must have one) | leave empty → agent **clones from Gitea** (today's behavior) |
| object exists, tar rc≠0 | EXTRACT_FAILED | `failed` | log → agent clones from Gitea (degrade, don't fail the provision) |

The asymmetry is deliberate: for Path A the snapshot is a precondition; for Path B (node loss, maybe
pre-first-suspend) its absence is expected and the Gitea clone is the honest floor.

**Test plan:** unit — assert extract issued **before** git, git string contains `fetch` + `reset --hard
origin/` and **not** `git clean`; branch resolution; the four `LayeredRecovery` outcomes; the restore→
wedge→fresh dedup. Integration (k3d) — create a tracked file (push) + a gitignored `secret.env` **outside
`repos/`**, snapshot, force-delete pod+PVC+Service, resume, assert `secret.env` survives byte-for-byte and
`git status` shows it ignored (not deleted); negative — delete the S3 object too → `NO_SNAPSHOT` → agent
clones.

---

## §C — Verifiable capture (prerequisite for trusting the floor + reclaim-on-idle)

Both §B (trusting the snapshot on node loss) and reclaim-on-idle (deleting a PVC against a snapshot)
require capture to be **confirmable**. Two independent agents (one code-side, one best-practices) reached
the same conclusions; merged here.

**What already exists:** a SHA-256 of the `.tar.zst` **is** computed at capture and stored as
`manifest.checksum_sha256` (`snapshot_service.py:170-171`), and `size_compressed_bytes` is recorded
(`:554`). The gaps are: it's never checked on read; nothing verifies before a destructive reclaim; a
single overwritten key with no versioning; and a truncated tar is accepted.

**C1 — Honest exit codes.** Two sub-parts.

**C1a (restore-side masking) — DONE (commit `7a9bd5d8`, 2026-08-08).** `ide_session._extract_snapshot_to_vm`
was `-> None` and only logged on `rc≠0`; `restore_snapshot_for_resume` then unconditionally returned
`True`. Changed the former to `-> bool` (mirrors the twin `workspace_suspension._extract_snapshot:506-562`)
and gated the resume path — it now returns `False` and suppresses the "Snapshot restored" success log when
the extract fails. The IDE **browse** path (`_extract_snapshot_to_k8s_pod`) keeps its soft "populated-probe"
policy and is deliberately NOT used by any reclaim/resume path; the best-effort IDE-*start* VM caller
(`ide_session.py:580`) intentionally ignores the new `bool`.

**C1b (capture-side honest capture accept) — DONE (commits `2f747a5f` + `2761d9bd`, 2026-08-08); the corrected rule below is what shipped.** The
remote pipelines have **no `set -o pipefail`**, so a failing `tar`/`zstd` upstream is masked by the
downstream stage's `0` (capture `tar_cmd` `snapshot_service.py:460-464`, accept gate `:525` currently fails
only when `rc≠0 AND bytes==0`; extract `EXTRACT_REMOTE_CMD` `ssh_helpers.py:79`, `EXTRACT_HOME_REMOTE_CMD`
`:88`).

> **Correction — do NOT "reject any nonzero tar rc".** `tar` exit **1** = *"file changed as we read it"* —
> a **warning, not a failure**: the archive is complete and usable, and it happens **routinely** when
> tarring a *live* workspace (the agent is actively writing files). Exit **2** is fatal
> (truncated/unreadable). The honest capture rule is therefore: **accept tar rc ∈ {0, 1}; reject tar rc ≥ 2;
> reject any `zstd` failure.** A shell pipe collapses to one exit code, and `pipefail` alone can't tell you
> *which* stage failed or that it was the harmless `1` — so capture **each stage's code separately** via
> bash `PIPESTATUS` (e.g. run the pipeline then emit `RC:${PIPESTATUS[@]}` on a side channel / stderr, or
> restructure) and let the Python accept-logic see `tar_rc` and `zstd_rc` independently. On the **extract**
> (restore) side, `pipefail` is a clean win — it catches a masked `zstd -d` decompression failure on a
> corrupt archive (extract into a fresh target rarely yields a spurious tar rc==1). This changes **live dev
> capture behavior**, so gate it behind the discriminating test (rc=2 rejected, rc=1 accepted, zstd-fail
> rejected) and validate on a busy workspace before rollout.

*Portability guard:* `pipefail`/`PIPESTATUS` are bash/ksh/zsh, not POSIX `sh`/`dash` — verify the agent-host
login shell in the workspace/VM images; wrap in `bash -c` if any target is dash.

**Implementation status.**
- **C1b (capture-side)** — DONE (`2f747a5f` + `2761d9bd`): a `PIPESTATUS`-discriminated remote exit code
  (tar rc 0/1 accept, ≥2 or any `zstd` failure reject) + the accept gate, in `snapshot_service.py`.
- **C1c (extract-side)** — DONE (`9b7dd366`): plain `set -o pipefail` on both EXTRACT commands in
  `ssh_helpers.py`. **Corrected from the earlier plan** — the extract side needs only `pipefail`, NOT
  PIPESTATUS and **no consumer changes**: `tar` is the pipeline's *last* stage so its rc already passes
  through (a benign full-extract `tar` rc==2 on image-provided `/usr/local` stays unchanged, per
  `ssh_helpers.py:82-88`); `pipefail` only surfaces an otherwise-masked `zstd -d` decompression failure.
- **C1d** — DONE (commit `f8a6b297`, k3d-verified on bash 5.2). `ide_settings.py` had **two** unwrapped
  sites — capture `_ssh_tar_to_file:728` (`tar | zstd`) **and** extract `_ssh_untar_from_file:889`
  (`zstd -d | tar`). Both now `bash -c`-wrapped with a `PIPESTATUS` **collapse-to-bool** verdict (accept
  `tar` rc≤1 + upstream stage OK → exit 0; reject → exit 1), matching the two callers' `rc == 0` contract.
  (Collapse, not plain `pipefail`, because these best-effort callers want a bool and benefit from the
  tar-rc==1 tolerance.)

**C2 — Verify integrity + completeness before trusting.** Server/S3 checksums only prove *bytes-received
== bytes-hashed*, **not** that the archive is complete (a truncated tar hashes clean). So the verify has
two parts: (a) **integrity** — `HeadObject` size match, and a streamed re-hash of the S3 object compared
to `manifest.checksum_sha256` (O(1) memory, mirroring the fd-streamed extract); (b) **completeness** —
stream the object back through `zstd -t | tar -tf - >/dev/null` and confirm exit 0 + expected member
count. Expose one `SnapshotService.verify_snapshot(entity_id, *, deep) -> (ok, reason)` that every reclaim
gate calls. **Multipart-ETag caveat:** boto3 uploads these (500 MB–2 GB) as multipart, so the ETag is
`md5(concat(part-md5s))-N`, **not** the object hash — never compare ETag to the sha; our stored sha256
(rehash on GET) is the oracle. Optional `ChecksumAlgorithm=SHA256` on PUT is complementary hardening
(yields a *composite* checksum for multipart), and Garage support is uneven — keep our own rehash
authoritative. Also add `verify=True` opt-in to `download_snapshot` (cheap: bytes already local).

**C3 — No-clobber (portable across MinIO *and* Garage).** The overwrite-protection primitives are **not
portable**: Garage supports **none** of bucket versioning, Object-Lock, or conditional writes
(https://garagehq.deuxfleurs.fr/documentation/reference-manual/s3-compatibility/), and MinIO lacks the
`If-None-Match: *` create-if-absent form. So the design must not depend on any of them. The portable
pattern is **write-new → verify → promote; never overwrite the current-good with an unverified stream:**
- **Recommended (lower churn, keeps the restore path stable):** upload to a staging key
  `…/env.tar.zst.staging-<uuid>`, run C2 verify, then **server-side `copy_object`** (atomic per-key, works
  on Garage) staging → canonical `env.tar.zst` **and** → `history/<created_at>/`, then delete staging;
  **keep last N** generations (`SNAPSHOT_KEEP_GENERATIONS`, default 3). Restore/`download_snapshot` keep
  reading the canonical key unchanged. A failed capture leaves a bad staging object that fails verify →
  canonical untouched.
- **Alternative (purer immutability, higher churn):** unique key per snapshot + a `latest.json` pointer
  updated only after verify; restore reads the pointer. Adopt only if we later want strictly
  never-overwrite semantics.
- Manifest (v2, additive) records: `checksum_sha256`, `size_compressed_bytes`, `tar_member_count`,
  `capture_rc`, `capture_complete`, tool versions (`tar_impl`/`zstd`), `source_id`, `created_at`,
  `verified`/`verified_at`. `run_gc`/`_soft_delete_snapshot` are already prefix-recursive → sweep
  `history/` automatically; exclude `history/` from the `get_storage_stats` snapshot count.

**C4 — Gate reclaim on verify.** Two reclaim gates, both currently capture-then-destroy:
`release_workspace` (`container_provisioner.py:624/643-645`, *"deleting anyway"*) and
`WorkspaceLifecycleManager.delete` (`workspace_manager.py:784-788`). Insert `verify_snapshot` between the
pod delete and the **irreversible** `delete_workspace_pvc`, **hard-blocking for resumable work**
(fail-safe keeps the volume) and **advisory for terminal jobs** (`verify_required = not _is_terminal`) so
a legitimately-snapshotless terminal job doesn't leak its PVC. Also set `snapshot_status="available"`
only after verify passes, which upgrades the existing emptyDir-suspend handoff for free. `reap_orphans`
(row-gone orphans) is untouched — nothing to restore into.

**C5 — tar-implementation pinning (restore-correctness hazard).** GNU tar stores xattrs/ACLs under
`SCHILY.xattr.*`, libarchive under `LIBARCHIVE.xattr.*`, and they **don't cross-restore**
(https://mgorny.pl/articles/portability-of-tar-features.html). Pin the **same tar implementation** on
capture and restore and pass `--numeric-owner` (UID/GID won't match across pods). `zstd -T0` is free (no
ratio change, just speed).

**Test plan:** capture-failure leaves canonical untouched; `verify_snapshot` truth table (missing/size/
deep-hash mismatch/no-checksum→unverifiable/ok); pipefail present in all three remote command strings;
tar rc=2 with bytes>0 now fails; `restore_snapshot_for_resume` returns False on extract failure (the
masking regression); reclaim gates block PVC delete on unverified (resumable) and proceed for terminal
jobs; keep-last-N prune. Live gate (k3d, real MinIO/Garage, object >8 MB so multipart is exercised):
truncate the S3 object out-of-band → deep verify catches it → PVC survives; clean capture → verify → reclaim
→ fresh pod restores cleanly.

---

## §D — PVC lifecycle & portability (hot-cache hardening)

From the Kubernetes-storage sweep; aligns the live PVC path to D4 and the portability constraint.

- **Bare app-owned PVC, not a StatefulSet `volumeClaimTemplate`.** An STS gives free naming/reattach/
  retention but its controller aggressively recreates pods and manages PVC owner-refs, **fighting** our
  orchestrator that owns `ensure_workspace`/reconcile (a documented failure mode,
  kubernetes/kubernetes#134357). Own the PVC directly; borrow the STS *ideas* only.
- **Naming/reattach:** deterministic, owner-keyed (`pvc-ws-thread-<id>` — already so). PVC is a separate
  object from the pod, so crash/reschedule never touches it; the `pvc-protection` finalizer keeps the PV.
- **Reclaim policy = the StorageClass default (usually `Delete`); do NOT force `Retain`.** Durability is
  the snapshot's job; `Retain` just leaks `Released` PVs. (k8s Persistent Volumes docs.)
- **GC (there is no built-in GC for bare PVCs):** two mechanisms, defense-in-depth — (1) an
  `ownerReference` on the PVC → a per-workspace owner object, so deleting the owner cascades (how STS
  auto-deletion works internally); (2) the existing idle-sweep also sweeps PVCs whose workspace row is
  terminal/absent (our `reap_orphans`, already present). Keep the orchestrator the **sole** deletion
  authority.
- **Finalizer discipline:** a PVC stuck `Terminating` with `pvc-protection` is *working as designed* — a
  Pod still references it. **Delete the pod first, then the finalizer clears.** Never routinely patch
  `finalizers: null` (orphans the PV); reserve manual stripping for the explicit node-loss discard.
- **Portability = operator-set flag, not runtime autodetection.** CSI exposes no capability bit for
  cross-node reattach (https://kubernetes-csi.github.io/docs/topology.html). Add
  `hotCache.crossNodeReattach: true|false` per StorageClass, **default false** (safe on k3d/local-path).
  Optional one-way assist: after first bind, if `PV.spec.nodeAffinity` pins a single node, downgrade —
  this safely detects "pinned," never falsely claims "cross-node." Template the pod so the cache mount is
  **optional at the same path** (PVC-on → mount PVC; PVC-off/unsupported → `emptyDir` at the identical
  path) so app code is byte-identical and always treats the cache as best-effort.
- **Binding mode `WaitForFirstConsumer`** (not `Immediate`) so the volume is provisioned where the pod
  lands (avoids the wrong-zone-unschedulable trap); it only helps first placement — reschedule is still
  bound by the baked PV topology. **Access mode `ReadWriteOnce`** as the portable baseline;
  `ReadWriteOncePod` as opt-in hardening where the CSI driver supports it (not universal, so not a hard
  requirement) — single-attach discipline is the real guard.
- **Node-loss = discard-then-restore, don't wait out force-detach.** An RWO PVC on a dead node blocks the
  replacement with a Multi-Attach error for ~6 min before force-detach, and single-replica Longhorn goes
  *faulted* (often needing manual salvage). So on node `NotReady` beyond a short threshold: force-delete
  the pod, delete the wedged PVC (the deliberate discard, distinct from crash), recreate fresh on a
  healthy node, and **rehydrate via §B**. The `node.kubernetes.io/out-of-service` taint is the
  **operator** remedy to reclaim the leaked PV (manual + dangerous — only after confirming the node is
  truly off), documented as such, not automated. **Split-brain guard:** fence the writer on the workspace
  id (a lease, or RWOP) so a resurrected old node can't run a second writer; make restore idempotent,
  last-writer-wins.

---

## §E — F3: capacity guard + idle/tiering policy

- **F3 (do now):** turn on the already-built per-class `ResourceQuota` (`helm/templates/workspace-resourcequota.yaml`,
  `workspace.resourceQuota`, default-off) and/or a thread-pruning sweep. Sessions now hold **two** PVCs
  each under retain-on-idle on single-replica `longhorn-ephemeral`; the dev overlay itself warns of a
  *"capacity incident."* Pick the cap from live cluster state and size with headroom — at the cap, new work
  fails **closed** (bad UX for a session).
- **Idle/tiering (from prior art):** run **two independent timers** — (a) idle → snapshot + release pod
  (and, once §C lands, optionally the PVC) to bound compute + hot-cache cost; (b) a **retention TTL** that
  GCs old cold snapshots (already `SNAPSHOT_RETENTION_DAYS`) to bound object storage. Add a **hard
  max-lifetime cap** (Gitpod-style) to kill zombie sessions. **Rate-limit snapshot/restore I/O+CPU per
  workspace** (Gitpod's decompress-storm lesson) — a cgroup IO/CPU bound so one restore doesn't starve
  neighbors.

---

## Sequencing

1. **Now (small, independent, de-risk the live PVC):**
   - **F1** (§A) — closes `b1758f38` for all paths; ~1h design + TDD then mechanical.
   - **F3** (§E) — quota on + a cap number from the cluster.
   - **C1** (§C, pipefail + honest rc) — a few lines, stops the active "truncated tar accepted / restore
     masks failure" data-loss risk; independent of the rest.
2. **Next (unify the layers — coupled):**
   - **C2–C4** (verify + no-clobber + reclaim gate) and **§B** (layered recovery) and **F2** (session
     node-loss recovers via §B, not an empty volume). §B *trusts* the snapshot, so it should not ship
     ahead of C2. Together these make the floor trustworthy and node-loss non-lossy.
   - **§D** hardening (finalizer discipline, `crossNodeReattach` flag, WFC, fencing) alongside.
3. **Later (only if capacity demands):** reclaim-on-idle (D3) — a small policy change on top of (2).
4. **Roadmap (deliberate, not a prerequisite):** replace the bespoke tar path with **restic or kopia**
   (default restic for the "push to S3, no server, one `check`" model; kopia if we want no write-lock +
   auto-verify). Content-addressing makes integrity the storage model, adds dedup + incremental (repeated
   captures upload only changed chunks) + snapshot history + self-verifying restore, in a single static Go
   binary (dependency-light). Rule out borg (S3 support only just arriving, chatty). Harden first (C1–C4);
   migrate later. Note: the restore-OOM restic would also relieve is **already fixed** for us (the extract
   is fd-streamed, `ssh_helpers.py:171`), so OOM is not a driver.

---

## Non-goals

- Charting Longhorn or any storage provider (D1).
- CSI `VolumeSnapshot`/Longhorn-native backups (rejected; breaks portability).
- Object-store-backed POSIX (JuiceFS/SeaweedFS/s3fs/CSI-S3) as the hot layer — each is a bundled storage
  dependency (D1 violation); our tar→S3 + optional-PVC already achieves it with less infra.
- In-product node-loss durability *beyond* the snapshot floor — that is operator-side storage DR.

---

## Code anchors

Line numbers drift (this area moved ~100 lines in two weeks) — treat as pointers, grep the symbol.

- **Toggle:** `helm/values.yaml` `workspace.pvcEnabled` (default false), `helm/templates/configmap.yaml`
  `WORKSPACE_PVC_ENABLED`, `deployment/values-experimental.yaml` (`true` on dev).
- **F1 (§A):** fix `src/tools/orchestrator/repositories.py:250-347` (helper near `:130`, call after
  `:335`); precedent `src/core/workspace.py:800-813`; seeding gap `src/managers/git_manager.py:157-163,
  :206-216` vs clone `:1080-1141`; ordering `src/persistent_graph.py:1019-1022`.
- **Layered recovery (§B):** `orchestrator/services/workspace_suspension.py` (Path A extract `:913-928`,
  skip guard `:906`, dedup baseline `:793`; job twin `:464`); `orchestrator/services/container_provisioner.py:452-492`
  (Path B; stamp `:491`); `orchestrator/services/ssh_helpers.py` (`stream_extract_snapshot:171`,
  `EXTRACT_REMOTE_CMD:79`, `:88`); agent probe `src/api/persistent_session.py:573-655`; git-over-SSH
  precedent `orchestrator/services/ide_session.py:1046,:1069-1083`; snapshot excludes
  `orchestrator/services/snapshot_service.py:449-452`.
- **Verifiable capture (§C):** `snapshot_service.py` (sha `:170-171`, size `:554`, manifest `:547-564`,
  capture accept `:525`, tar_cmd `:460-464`, upload `:191-204`, download `:706-742`, `run_gc`/soft-delete
  `:915,:1051-1093`); rc masking `ide_session.py:1171-1208,:1276-1309`; strict twin
  `workspace_suspension._extract_snapshot:506,:552-562`; reclaim gates
  `container_provisioner.py:585-647`, `lifecycle/workspace_manager.py:718-804`.
- **PVC lifecycle (§D):** `lifecycle/workspace_manager.py` (`_is_volume_reclaimable:448-488`,
  `reap_orphans:806+`); `container_provisioner.py` (`_pvc_name_for:64-80`, `_create_pvc`, fresh-fallback
  `:452-492`).
- **Semantic floor:** per-turn commit+push `src/persistent_graph.py` (~`:1019-1051`); session conversation
  Postgres `thread_messages` (NOT `checkpoint_blobs`).

## References

- Design/bug: [`workspace_pvc_branch_a_implementation.md`](workspace_pvc_branch_a_implementation.md),
  [`workspace_pvc_backed_migration.md`](workspace_pvc_backed_migration.md),
  [`../issues/session_restore_drops_repo_checkouts.md`](../issues/session_restore_drops_repo_checkouts.md),
  [`../issues/failed_job_pvc_reclaimed_without_grace_period.md`](../issues/failed_job_pvc_reclaimed_without_grace_period.md).
- **Prior art (products):** Gitpod "leaving Kubernetes" https://ona.com/stories/we-are-leaving-kubernetes ·
  Gitpod lifecycle/backup https://ona.com/docs/configure/workspaces/workspace-lifecycle ·
  Coder persistence https://coder.com/docs/admin/templates/extending-templates/resource-persistence ·
  Codespaces lifecycle https://docs.github.com/en/codespaces/getting-started/understanding-the-codespace-lifecycle ·
  E2B persistence https://e2b.dev/docs/sandbox/persistence (corruption issue https://github.com/e2b-dev/E2B/issues/884) ·
  Daytona sandboxes https://www.daytona.io/docs/en/sandboxes/ · Modal snapshots https://modal.com/docs/guide/sandbox-snapshots ·
  CodeSandbox https://codesandbox.io/blog/how-we-clone-a-running-vm-in-2-seconds.
- **Backup integrity / S3:** integrity/checksums https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity.html ·
  multipart-ETag https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html ·
  strong consistency https://aws.amazon.com/s3/consistency/ · conditional writes https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html ·
  Garage S3-compat https://garagehq.deuxfleurs.fr/documentation/reference-manual/s3-compatibility/ ·
  MinIO retention https://github.com/minio/minio/blob/master/docs/bucket/retention/README.md ·
  restic design https://github.com/restic/restic/blob/master/doc/design.rst · kopia https://kopia.io/docs/advanced/architecture/ ·
  tar xattr portability https://mgorny.pl/articles/portability-of-tar-features.html.
- **Kubernetes storage:** Persistent Volumes https://kubernetes.io/docs/concepts/storage/persistent-volumes/ ·
  STS PVC retention https://kubernetes.io/blog/2023/05/04/kubernetes-1-27-statefulset-pvc-auto-deletion-beta/ ·
  CSI topology https://kubernetes-csi.github.io/docs/topology.html · non-graceful shutdown / out-of-service taint
  https://kubernetes.io/blog/2023/08/16/kubernetes-1-28-non-graceful-node-shutdown-ga/ · RWOP GA
  https://kubernetes.io/blog/2023/12/18/read-write-once-pod-access-mode-ga/ · local-path
  https://github.com/rancher/local-path-provisioner · JuiceFS https://github.com/juicedata/juicefs.
