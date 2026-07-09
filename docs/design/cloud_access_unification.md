---
tags:
  - cloud
  - agent-architecture
  - design
  - unification
  - security
  - tech-debt
related:
  - "[[agent_action_reversibility]]"
  - "[[cloud_version_history_and_recovery]]"
  - "[[rclone_cloud_mount]]"
  - "[[job_cloud_export]]"
  - "[[main_cloud_abstraction]]"
  - "[[cloud_collaboration_model]]"
  - "[[webdav_datasource_tools]]"
  - "[[guardrails_matrix]]"
aliases:
  - "Cloud Connection Unification"
  - "Cloud Access Unification"
  - "Protected Cloud Mode"
  - "Capture Overlay"
---

# Cloud Access Unification — landscape + direction

**Status:** direction doc (landscape + agreed direction, **not** an implementation spec) — 2026-07-07. **Research round 2026-07-09 (§10):** 12-agent codebase+web sweep, verdict **viable with changes** — §3.1/§3.3/§3.4 corrected in place, §6 reduced to the genuinely hands-on spike set, §8.1 records the pre-spec design decisions (settled with the owner 2026-07-09). Next artifact: the implementation plan.
**Scope:** how the **agent** reaches cloud files/folders — the agent-facing data plane. Explicitly NOT the control plane (`MainCloudBackend` provisioning — healthy, see [[main_cloud_abstraction]] and `docs/issues/main_cloud.md`), NOT recovery/undo ([[cloud_version_history_and_recovery]] — complementary, §5), and NOT backend-native byte APIs (Phase 5, still deferred).

---

## 0. Why this doc

A 2026-06-26 investigation ("how does the cloud diff view actually gate writes?") revealed that even *we* had conflated the access paths: the owner believed jobs reached the project folder via an rclone mount + cloud tools; a prior architecture note claimed `cloud_*` WebDAV tools that no longer exist. The truth — jobs = Gitea-staged Mode A, sessions = live rclone mount, datasources = a third thing — took a multi-agent code sweep to establish. When the people who built the system can't hold its access model in their heads, that's the signal: **too many parallel mechanisms**. This doc catalogs all of them and records the agreed direction for collapsing the agent-facing ones into a single system.

The owner's seed notes for the direction:

> - Protected cloud modus toggle
> - If on we make the cloud rclone read only (for executing commands)
> - And the edits CRUD are only allowed through the tool so we can save the diff!

§3 keeps the intent (read path stays a mount; writes are captured as a reviewable diff) but strengthens the mechanism: a **capture overlay** journals *all* writes — shell included — so the tool-only restriction becomes unnecessary (§3.2).

## 1. The landscape — every way cloud bytes reach or leave an agent today

Verified against code 2026-06-26 → 2026-07-07 (develop).

| # | Path | Used by | Mechanism | Write semantics | Review gate | Code |
|---|------|---------|-----------|-----------------|-------------|------|
| 1 | **rclone FUSE mount** at `/cloud/*` (symlinked as `workspace/cloud`) | sessions only | lazy read-through mount, Keycloak bearer / WebDAV | **live** — standard file tools + shell write straight to the cloud | none | `src/services/cloud_mount/`, built at `_build_agent_cloud_mount` (`orchestrator/main.py:15538`, injected only via `/api/agents/threads/{id}/workspace`), [[rclone_cloud_mount]] |
| 2 | **WebDAV sync fallback** (`cloud_sync`) | sessions only, when the mount is inactive | `pull_all()` before turn / `push_all()` after turn | live at turn boundaries | none | `src/services/cloud_sync/` (`WorkspaceSyncBase`), `_build_agent_cloud_sync` (`main.py:15320`) |
| 3 | **Mode A Gitea seed + diff review** | project-attached jobs | orchestrator seeds cloud→Gitea at dispatch; agent edits plain workspace files; tree-diff at completion → `pending_review` | **staged** — zero cloud writes during execution | **yes** — Cockpit diff view, Accept applies / Reject discards | `orchestrator/services/job_cloud_baseline.py`, REST `main.py:10981–11268`, Cockpit `job-diff-review` |
| 4 | **Mode B export** | loose jobs | user-triggered walk of `output/` → PUT to a new shared folder | staged (export-on-click) | implicit (user clicks) | `main.py` Mode-B export, [[job_cloud_export]] |
| 5 | **`webdav_*` datasource tools** | jobs + sessions, only when a webdav **datasource** is attached | raw `webdav3` client per call | live | none | `src/tools/webdav/tools.py`; deliberately datasource-scoped since [[webdav_datasource_tools]] (2026-06-07) |
| 6 | *(beneath 3/4)* control-plane byte methods | orchestrator only | `list/get/put/delete_project_folder_file_bytes` — used at seed, apply, export time | n/a (orchestrator is the writer) | n/a | `orchestrator/services/cloud/{opencloud,nextcloud}.py` |
| 7 | *(beneath everything)* S3/MinIO object store backing the cloud | nobody directly (agent has no S3 key) | planned: bucket versioning + lifecycle | n/a | n/a — **recovery**, not access | [[cloud_version_history_and_recovery]] (paused on its §9) |

**What the split costs us:**

- **Three separate project-folder implementations** (1, 2, 3) over the same WebDAV byte layer — every fix/feature lands N times or drifts.
- **Mode A limitations** baked in by the Gitea intermediary: text/UTF-8 files only (binaries silently skipped from seed *and* diff), seed latency at dispatch proportional to folder size, dispatcher gated on `cloud_baseline.state` (`orchestrator/database/postgres.py:3153-3155`).
- **Sessions write live with no gate and (today) no recovery floor** — the exact "agent mangles 2 TB" fear that blocks daily-driving ([[cloud_version_history_and_recovery]] §0).
- **Asymmetric UX:** jobs get a PR-style review; sessions get nothing; Mode B gets a one-shot export button. Three mental models for "the agent changed my files."
- **Nobody can hold the model in their head** (§0) — including us.

## 2. The goal

**One agent-facing mechanism:** the cloud namespace appears as a **plain directory** in the workspace — lazy (no 2 TB clone), identical for workers and sessions, edited with the standard file tools and the shell. Whether writes land **live** or are **staged for review** becomes a *policy toggle* on that one mechanism ("protected cloud mode"), not a separate code path per agent kind.

Unify the plumbing; keep the semantics divergent *on purpose*:

- **Jobs** (async, unsupervised) default to **protected** — they keep review-before-apply, which is the product value of Mode A.
- **Sessions** (live, supervised) default per user choice: **live** for collaborative editing (today's behavior, user sees agent edits in the cloud immediately), **protected** for autonomous/high-stakes runs ("reorganize my archive").

## 3. Design direction: protected cloud mode via a capture overlay

### 3.1 The stack

```
agent sees:   workspace/cloud            ← overlay mount (writable, normal dir semantics)
                 upperdir: local scratch      ← ALL writes land here (the staged diff)
                 workdir:  local scratch
                 lowerdir: rclone FUSE mount  ← READ-ONLY, dedicated read-only identity
cloud sees:   nothing until approval; then the orchestrator applies the diff
```

This is the container-image layering model, not invented tech — `docker diff` has derived Added/Changed/Deleted changesets from the overlay upperdir for a decade. The mount is never written; the **upperdir *is* the diff**, materialized on local disk by construction.

**Engine (settled by research, §10): fuse-overlayfs primary, kernel overlayfs as opportunistic fast path.** The decisive argument: the agent runs as uid 1000 (`agent-host`), and kernel overlayfs `mount(2)` requires CAP_SYS_ADMIN *in the calling process* — a privileged pod grants that to root, not to the agent uid. fuse-overlayfs mounts via the setuid `fusermount3` helper and needs only `/dev/fuse` — which the workspace runtime already exposes for the rclone mount, so the privilege delta is **zero** (`container_provisioner.py:1304-1412`; helm notes privileged is already required for FUSE on k3d/containerd). fuse-overlayfs is actively maintained (v1.17 June 2026; Rust 2.0 rewrite in progress) and explicitly accommodates high-latency network lowers (`static_nlink`). Kernel overlayfs over a FUSE lower is officially in-scope too (lower-fs requirements are minimal; the strict d_type/xattr rules apply to the *upper* only, and rclone's missing xattr support — rclone#1710 — is harmless for a lower) with long-running community precedent (plexdrive/rclone-era overlay setups). If used: `redirect_dir=off,metacopy=off,index=off,xino=off,nfs_export=off`.

**Whiteout enumeration is engine-ambiguous by design:** fuse-overlayfs *also* prefers char(0,0) device whiteouts (containers hold CAP_MKNOD by default), falling back to `.wh.<name>` files; opaque dirs are marked by any of `trusted.overlay.opaque` / `user.overlay.opaque` / `user.fuseoverlayfs.opaque` xattrs or a `.wh..wh..opq` sentinel — and both styles can coexist in one upperdir. The diff enumerator must therefore handle **all forms from day one** (and ignore `user.fuseoverlayfs.*` metadata xattrs); done once, it works under either engine and keeps them swappable.

### 3.2 Why an overlay instead of "CRUD only through a tool"

The owner's seed note scoped writes to a diff-capturing tool. The earlier objection (recorded in [[cloud_version_history_and_recovery]] §2) was that with a live mount **plus a shell**, any tool-level hook is bypassable (`rm`, `sed -i`, `>`, `curl`). The overlay dissolves the dilemma rather than picking a side:

- The **shell keeps working** — `grep`, `sed -i`, build scripts, `rm -rf` — full local-directory ergonomics.
- **Every write is captured anyway**, because writes physically cannot reach the lower layer; they land in the upperdir (copy-up for modifications, whiteout markers for deletions).
- `rm -rf huge-dir/` is the showcase: the kernel records **whiteouts** (deletion markers) in the upperdir; the cloud is untouched; the operation is metadata-only (no downloads); the diff view lists N deletions; **Reject = delete the upperdir** and nothing ever happened.
- Modifications get old/new for free: old = lower (pristine in the cloud), new = upper. **Binary files work** — no Gitea in the middle — which retires Mode A's text-only limitation.

Note what was rejected before and stays rejected: *intercepting writes inside the mount* (rclone has no hook/plugin system). The overlay doesn't intercept — it layers above a mount that is read-only.

This choice now has direct academic validation: **YoloFS** (arXiv 2604.13536, 2026) is an agent-native filesystem staging ALL mutations — shell included — at the FS layer with a user review/commit gate, evaluated to *fewer* user interactions at unchanged task success. FS-level interception over tool-level journaling is the emerging consensus, not just our reasoning.

### 3.3 Trust model — Mode A generalized (mechanism settled 2026-07-09)

Read-only must be **credential-level**, and research settled how: on both backends the RO guarantee comes from the **share/role layer**, not from tokens — Nextcloud app passwords cannot be permission-scoped (open feature requests #6030/#30331), and oCIS/OpenCloud token-exchange/impersonation carries the user's full rights (and prod's shared Keycloak rejects `requested_subject` anyway — [[srw-prod-private-cloud-sync-token-exchange]]).

- **The mount identity must be a dedicated low-privilege account, NOT `agent-service`.** `agent-service` already holds editor grants elsewhere (`_SESSION_SHARE_PERMISSIONS=15`, group-folder `_ALL_PERMISSIONS` — `nextcloud.py:588,619`), so "RO share + agent-service creds" is theater: extracted creds could write *other* folders. Instead: **Nextcloud** — a dedicated secondary account (optionally via the Guests app) that receives *only* read-only shares / RO Group Folder ACL entries, mounted via an app password for that account. **OpenCloud** — a dedicated regular Keycloak machine user (e.g. `srw-reader`) granted the **Viewer** role on the target Space via LibreGraph permissions; explicitly NOT oCIS "service accounts" (inter-service-only, can stat all spaces) and NOT token-exchange.
- **Fail-closed RO probe at mount-provision time:** as the RO identity, attempt `PUT`/`DELETE`/`MKCOL`/`MOVE`/`PROPPATCH` **plus versions-restore, trash-restore, and chunked-upload/TUS finalize** — all must 403/405 or protected mode refuses to engage. The extended verb list is not paranoia: both backends had *real, patched* RO-bypass CVEs in exactly those side channels (Nextcloud versions-restore GHSA-5mq8-738w-5942 fixed 28.0.3; groupfolders trash-restore GHSA-2vrq-fhmf-c49m fixed 20.1.2). **Version floors: Nextcloud server ≥ 28.0.3, groupfolders ≥ 20.1.2.** The probe converts version-assumption risk into a runtime check.
- The orchestrator owns the RO-share lifecycle (create/verify before mount, revoke on teardown — see §9.7 on GC).

Write credentials exist **only orchestrator-side, used once, at apply time, after approval** — which is exactly Mode A's trust model today (agent stages, orchestrator is the sole cloud writer). Protected sessions become "Mode A with a live lazy view instead of a Gitea seed."

### 3.4 Review + apply — reuse, don't rebuild (with two corrections)

- **Diff extraction:** walk the upperdir → `{path, status: added|modified|deleted}` (whiteouts = deleted, per the §3.1 dual-format enumerator; extract from the upperdir path directly, never through the merged view). Same shape Mode A's summary already serves.
- **Review UI:** the Cockpit `job-diff-review` component (file tree + Monaco side-by-side + Accept/Reject) generalizes — its types (`JobDiffFileEntry`/`JobDiffFile`) are already source-agnostic; `baseline_commit`/`head_commit` become optional metadata. Sessions get a "cloud changes (N)" badge/panel backed by mirrored endpoints. The refactor seam is a **`DiffSource`** protocol (summary + per-file old/new content) with `GiteaDiffSource` (today) and `UpperdirDiffSource` (new) implementations; endpoints and Cockpit stay shared.
- **Apply:** `apply_diff_to_cloud()` walks the diff and PUTs/DELETEs via the control-plane byte methods — source swaps from "Gitea at HEAD" to "upperdir", the rest is shared.
- **Conflicts — corrected 2026-07-09:** `detect_external_mods()` the *function* is source-agnostic, but its *input* (the path→etag map in `context.cloud_baseline.entries`) is produced **only by the Mode-A seed** (`job_cloud_baseline.py:190-195,516`). Overlay mode has no seed, so a **mount-time etag baseline capture** must be added (a metadata-only PROPFIND walk — cost/scoping is a §8.1 decision), and re-captured after each apply. Without it the 409 conflict gate is a silent no-op. Apply-time validation always runs against **live cloud etags**, never the (minutes-stale) mounted view.
- **After apply in a continuing session:** clear the upperdir + refresh the lower view. Both engines declare a *changing lower* undefined behavior, so the design commits to **frozen-lower-per-epoch** semantics with a first-class **refresh op** (unmount overlay → flush rclone dir cache / remount → remount overlay; the upperdir survives untouched) at apply boundaries and on detected external mods — engineered for open FDs held by a live agent (§6.2), not ad-hoc.

### 3.5 Live mode (toggle off)

Today's session mount, unchanged: writable rclone mount, live writes, user sees edits in the cloud immediately. The recovery floor below (§5) is what makes live mode sane. (Whether live mode stays a direct rw mount or becomes overlay-with-auto-apply is a §8.1 decision; the lean is: direct rw mount — "one mechanism" means one mount plumbing with policy layered on top, not one write path.)

## 4. What this eliminates / what it keeps

**Eliminates (over the phasing in §8):**
- The Mode A **Gitea seed** (path 3's capture mechanism) — and with it the text-only limitation, the seed latency, and the dispatch gate. Gitea remains the *workspace/repo* layer; it stops being the *cloud staging* layer.
- The **`cloud_sync` WebDAV fallback** (path 2) — once the mount is the only session mechanism, the fallback's job disappears (workspaces that can't FUSE-mount need a decision: keep sync as a legacy tier or require mount-capable runtimes).
- The **three-implementations debt** — one mount codepath, one review UX, one apply path for every agent kind.

**Keeps:**
- The **control plane** (`MainCloudBackend`, router seam) — untouched; it gains the "provision RO identity + RO share lifecycle" duty.
- The **review/apply machinery** — generalized behind `DiffSource`, not replaced.
- The **S3 recovery floor** (§5) — complementary, not competing.
- The **`webdav_*` datasource tools** — BYO datasources stay a separate, deliberate concern ([[webdav_datasource_tools]]). Absorbing datasources as *additional mounts* is attractive but is an open question (§9), not a v1 claim.
- **Mode B** export for loose jobs — until/unless loose jobs get a home-folder mount (open question).

## 5. Layering with recovery — complementary, not competing

```
protected mode  (opt-in)   → PREVENT:  review-before-apply, staged diff, reject discards
────────────────────────────────────────────────────────────────────────────
S3 versioning   (always-on) → RECOVER:  undo-after-the-fact, below the mount,
                                        covers live mode, the apply step itself,
                                        non-SRW clients, and anything else
```

This doc does **not** reopen [[cloud_version_history_and_recovery]]'s convergence (version-in-place on the MinIO bucket; paused on its §9 same-data-vs-separable-corpus question). That stays the always-on floor and the answer for the *rogue/residual* tail. Protected mode addresses [[agent_action_reversibility]]'s "staged change awaiting commit" class and — by making the lower read-only at the credential level — resolves its open-Q4 ("where does write-capture live such that a tool can't bypass it?") for the cloud slice: **below the shell, above the cloud, with no write creds in the workspace at all.** Notably, the industry default for cloud-drive agents is "write live + version-history undo" — i.e. *only* the floor; the staged layer above it is (per the §10 survey) genuine whitespace.

## 6. Feasibility gate — the remaining hands-on spike (rescoped 2026-07-09)

Desk research **answered** former items 2 (RO mechanism — §3.3) and 4 (whiteout formats/diff walk — §3.1; `docker diff` is the prior art), and answered item 1 *in principle* (FUSE lower is officially supported with real-world precedent). What remains genuinely hands-on:

1. **The FUSE-on-FUSE prototype** — no authoritative source specifically blesses *rclone* as a lowerdir; community precedent is 2017-2020 read-heavy media workloads. Mount fuse-overlayfs over the actual rclone WebDAV mount on the real workspace image + VM tier and run the matrix: (a) `rm -rf` whiteout storm + enumeration fidelity (incl. opaque-dir renames, binaries), (b) copy-up timing on a ~100 MB file, (c) external WebDAV edit mid-session → refresh op → verify upperdir intact, (d) readdir/merge latency on a 10k-file tree, (e) build-like write workload (fuse-overlayfs write-path overhead is claimed ~2-7x kernel — writes are scratch-local so likely acceptable; measure).
2. **Refresh/heal with a live agent** — the §3.4 refresh op and rclone-mount-death recovery (no health monitor exists today: `src/services/cloud_mount/__init__.py` has unmount scripts only) against open FDs on the merged view; define ordering (unmount overlay → restart/flush rclone → remount overlay) and what the agent experiences mid-turn.
3. **Snapshot sequencing** — capture the upperdir **without traversing the merged mountpoint** (a naive tar of the merged view would pull the whole cloud tree through rclone); quiesce-vs-torn-copy-up rules; restore ordering on resume (upperdir → rclone mount → overlay) so staged changes survive pod churn (`snapshot_service.py:371-386` already scopes `/home/agent-host/`, so upperdir at `/home/agent-host/.overlay/{upperdir,workdir}` is captured by placement).
4. **Live RO probe** — run the §3.3 fail-closed probe against real Nextcloud (≥28.0.3) and OpenCloud instances; verify every mutating verb 403s for the dedicated RO identity.
5. **Etag-baseline walk cost** — measure the mount-time PROPFIND enumeration at target tree sizes (project folders now; user-home scale later) to size the §8.1 baseline-capture decision.

## 7. Honest costs — it is *not* "a local filesystem with no downsides"

- **Cold reads stream from the cloud.** `grep -r` over the namespace downloads what it reads (rclone VFS cache mitigates re-reads). Inherent to laziness; unchanged from today's mount. The existing cache guardrails (`src/services/cloud_mount/guardrails.py`) compose with the overlay.
- **Copy-up cost.** First write to a file pulls the whole file into the upper layer — trivial for text, real for a 10 GB binary. (The live mount's VFS write cache has essentially the same cost; this is not a regression, just not magic.) Needs size-aware guards so one `touch` of a huge file doesn't stall a turn.
- **Staleness + conflicts.** The lower view is as-of mount time (rclone dir-cache; WebDAV has no change-polling) and the overlay *requires* a frozen lower; external edits surface as 409s at apply via the etag baseline (§3.4). Fine for short jobs; long-lived protected sessions need the refresh op as a first-class UX action.
- **Semantics change under protection.** Live-collab sessions lose "user sees agent edits immediately" — that's *why* it's a toggle, not a global switch. The agent must also *know* writes are staged (prompt/tool copy), or it will tell the user "saved to your cloud" falsely (§9.10).
- **Review fatigue risk for sessions.** Settled direction from the evidence (§10): **batched review, badge + apply-anytime** (Word-Copilot-style), per-file accept/reject *within* one review surface, and **no per-write confirmations** — the 11k-review habituation study shows high-frequency gates decay into rubber-stamping.
- **Staged-diff data-loss window.** The upperdir is pod-local emptyDir; snapshots fire at teardown/idle — and snapshot-before-delete SSH failures are a *known* dev failure mode ([[dev_workspace_gc_gap_leaked_pods]] class). Between staging and the next snapshot, a crash loses staged work — for a feature whose promise is "nothing is lost until you approve," this needs a snapshot-on-stage cadence or PVC decision before Phase 1 ships (§8.1).
- **Disk governance.** Upperdir + rclone VFS cache + workspace share one ~10Gi emptyDir, and breaching an emptyDir sizeLimit **evicts the pod** — i.e. the failure mode of over-staging is losing the session *and* the staged changes. Needs an upperdir quota + defined at-cap behavior (§9.9).
- **Renames apply as DELETE+PUT** — destroying cloud-side version history and share links on the old path. Acceptable v1 cost; document it in the review UI.

## 8. Phasing sketch (not committed work)

- **Phase 0 — spike** (§6). Cheap, decides everything. Output: go/no-go + engine + measured costs.
- **Phase 1 — protected sessions.** The toggle, RO-identity provisioning + probe, overlay mount, etag baseline, session diff review surface (reusing job-diff-review via `DiffSource`), apply/reject + refresh op. Live mode untouched. *This is the piece that unblocks daily-driving autonomous runs on real data, together with the recovery floor.*
- **Phase 2 — jobs adopt the mount.** Project-attached jobs get the same RO mount + overlay instead of the Gitea seed; Mode A's review/apply endpoints stay, their diff source swaps (per-job `DiffSource` selection during coexistence). Retires seed latency + text-only + the dispatcher gate. The plumbing delta is known and modest: a `job_mounts` table (or polymorphic `thread_mounts`), mount config on the dispatch path or job workspace-status endpoint, `RcloneMountManager` genericized (thread_id is only naming — `cloud_mount/__init__.py:159-167`), `_setup_cloud_mount` in the job setup path.
- **Phase 3 — consolidation.** Retire the `cloud_sync` fallback (or fence it as a legacy tier); decide datasource absorption (§9); revisit Mode B.

**Fallback (demoted 2026-07-09):** the RO mount + tool-only CRUD shape remains documented, but research says it is unlikely to be needed for feasibility reasons — and YoloFS argues tool-level journaling + EROFS shell semantics is strictly worse. Keep it only as the degraded path for runtimes without `/dev/fuse`.

### 8.1 Pre-spec design decisions — **settled with the owner 2026-07-09**

1. **Staged-bytes transport: upperdir→S3 push at stage boundaries.** The orchestrator reads the staged diff (a small tar of deltas + whiteouts) from the snapshot S3 bucket, not from the pod. One mechanism solves three problems: orchestrator access to old/new bytes, review/apply surviving pod death ("review at your leisure"), and the §7 staged-diff data-loss window. Apply reads from S3 with orchestrator creds.
2. **Protected-OFF: live mode stays today's rw mount.** Unification = one mount plumbing with policy layered on top, not literally one write path; the recovery floor ([[cloud_version_history_and_recovery]]) covers live mode. No overlay-with-auto-apply.
3. **Etag-baseline: full-tree PROPFIND at mount** for project-folder scale in v1 (simple, correct); measure the walk cost (spike item 5) before extending to user-home scale. Re-capture after each apply.
4. **RO identity: per-user readers, per-mount grants, per-provision credentials.** Identity `srw-reader-<user>` JIT-provisioned at first protected mount — keyed by `user_id` from day one (the same "add the key to the seam now" move as the [[main_cloud_abstraction]] router seam, and the same scoped-credential pattern as the bucket-scoped virtual-workspace S3 key). Grants (NC RO share / OC Space Viewer) minted **per mount**, revoked at teardown **plus a periodic reconciler sweep** ([[srw_agent_headscale_ephemeral_leak]] lesson: never trust revoke-on-teardown alone). Credentials rotated **per provision** (NC app password created/deleted with the mount; OC bearer is short-TTL anyway). Security invariant: **a credential present in a workspace must grant no capability beyond that workspace's legitimate scope** — per-user readers make extracted RO creds worth ≈ nothing marginal. (Context: this is strictly better than the status quo, where `agent-service` is a cross-tenant read+**write** key in every session workspace.) Backend nuance: NC readers are local/guest accounts via the NC admin API (no Keycloak dependency — works on prod-private's shared KC); OC readers need KC admin to create users (fine on dev's own KC; prod verification is §9.2).

## 9. Open questions

1. **Session review UX granularity** — settled in direction by §10 evidence: badge + apply-anytime, one batched review surface, per-file accept/reject within it; remaining question is agent-invoked `submit_cloud_changes` yes/no.
2. **RO identity on prod's shared Keycloak** — creating the `srw-reader` machine user needs KC admin capability prod-private may not have; verify.
3. **Datasource absorption** — do `webdav_*` datasources eventually become additional (RO-or-RW) mounts under the same overlay policy, retiring path 5? Attractive (one model for *everything*), but datasources are third-party clouds with their own auth quirks.
4. **Mode B / loose jobs** — home-folder mount under protection instead of export-on-click?
5. **Concurrent writers** — two protected stagings over the same folder have independent upperdirs; first Accept invalidates the second's baseline (409 storm vs apply lease?). Agent+user concurrent edits: what does the review UI offer when the staged diff is against a stale base (rebase/discard)?
6. **Review TOCTOU** — the agent can keep writing the upperdir between the user loading the diff and clicking Accept. Does Accept apply the *reviewed snapshot* or the *live upperdir*? (Freezing the upperdir during review, or hashing the reviewed state and 409ing on drift, are the candidate answers.)
7. **RO identity blast radius + GC — DECIDED, see §8.1.4** (per-user readers / per-mount grants / per-provision creds + reconciler sweep). Residual for the plan: reader-account GC on user deletion, sweeper cadence, and whether NC readers are Guests-app accounts or plain local accounts.
8. **Apply partial failure** — `apply_diff_to_cloud` is sequential and fail-soft; define idempotent resume (per-file clearing vs all-or-nothing), the Cockpit "partially applied" state, and whiteout-before-create ordering for opaque-dir renames.
9. **Upperdir size governance** — quota level, at-cap behavior (EDQUOT to the agent? pause + notify?), interplay with the existing cloud-mount cache guardrails, and the emptyDir-eviction cliff (§7).
10. **Toggle ownership + agent honesty** — where the protected flag lives (thread vs project vs job context vs user default), who may flip it, what a mid-session flip does with a non-empty upperdir, and the prompt/tool-copy changes so the agent describes staged writes truthfully.
11. **Rollout mechanics** — helm feature flag (dev-ON/prod-OFF, `promptDbOverridesEnabled` pattern); fuse-overlayfs baked into the workspace image *and* agent-vm-base (VM controller is unmanaged/hand-upgraded — v0.0.21 lesson); Mode A coexistence window.
12. **Testability on k3d/CI** — no hermetic cloud backend exists locally (tilt reaches the shared dev OpenCloud); the RO probe needs a real Nextcloud ≥28.0.3 and OpenCloud; overlay tests need `/dev/fuse` in CI. A test-harness story is a spec prerequisite.

## 10. Research record — 2026-07-09

12-agent fan-out (5 codebase explorers, 5 web researchers, 2 adversarial critics; ~710k tokens). **Verdict: overlay-target viable with changes** — no feasibility blocker found; the changes are §3.1 (engine lean), §3.3 (RO identity mechanism), §3.4 (etag baseline gap), §8.1 (three undesigned seams). Corrections the critics caught: "kernel overlayfs = char-device whiteouts, fuse-overlayfs = xattr" is wrong (both prefer char(0,0); dual-format enumerator required); "detect_external_mods carries over unchanged" was wrong (input only exists via Mode-A seed); "RO share on agent-service creds" violates the threat model (agent-service holds editor grants elsewhere).

**Prior art worth citing:**
- **YoloFS** (arXiv 2604.13536, 2026) — agent filesystem staging all mutations at the FS layer with review/commit gate; validates the exact design, argues against the tool-only fallback.
- **`docker diff`** — decade-old production precedent for upperdir-walk-as-changeset, including whiteout/opaque-dir parsing.
- **Word Copilot Track Changes** — mainstream "agent stages edits to user documents, human accepts at leisure" on non-git content; supports badge + apply-anytime.
- **Habituation at the Gate** (arXiv 2606.22721) — 7-month/11,429-review study: approval rates rose while scrutiny fell with reviewer experience; high-frequency gates decay into rubber-stamping → batch the review, never per-write.
- **Confirmed negative results:** mergerfs ("does NOT support CoW or whiteouts" — maintainer README) and rclone union (action policies filter out `:ro` upstreams; issue #4929) cannot stage deletes/modifies — the doc's §3.1 engine set is the complete option space. No shipping product does staged writes over cloud drives (whitespace).

**Key primary sources:** kernel overlayfs docs (lower-fs requirements, "changes to underlying filesystems… undefined", userxattr); fuse-overlayfs repo (v1.17, `src/whiteout.rs`, issue #306 cross-engine whiteout incompatibility); rclone #1710 (no xattr — lower-safe, upper-disqualifying); Nextcloud GHSA-5mq8-738w-5942 + GHSA-2vrq-fhmf-c49m (RO-bypass CVEs → version floors); Nextcloud #6030/#30331 (no token scoping); oCIS service-account ADR ("all service users can stat all files on all spaces").

## References

**Internal:** [[agent_action_reversibility]] (parent principle; this resolves its open-Q4 for the cloud slice), [[cloud_version_history_and_recovery]] (the recovery floor this layers over), [[rclone_cloud_mount]] (the session mount this builds on), [[job_cloud_export]] (Mode A — the review/apply machinery this generalizes), [[main_cloud_abstraction]] + `docs/issues/main_cloud.md` (control plane, resolution seam), [[webdav_datasource_tools]] (why datasource tools are separate), [[cloud_collaboration_model]], [[guardrails_matrix]].

**External anchors:** kernel overlayfs (upperdir/lowerdir/whiteouts/opaque dirs — the container-layer model); fuse-overlayfs (rootless overlay, `/dev/fuse` only); `docker diff` (upperdir-as-changeset precedent); YoloFS arXiv 2604.13536 (staged agent FS with review gate); arXiv 2606.22721 (review habituation); rclone `--read-only` + VFS cache (read path) and rclone#1710 (no xattrs); Nextcloud GHSA-5mq8-738w-5942 / GHSA-2vrq-fhmf-c49m (RO-bypass CVE class → version floors); note that rclone union / mergerfs are **not** substitutes (no copy-up, no whiteouts — deletes/modifies of RO-branch files fail instead of staging).
