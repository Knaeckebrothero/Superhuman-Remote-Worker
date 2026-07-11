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

**Status:** direction doc (landscape + agreed direction, **not** an implementation spec) — 2026-07-07. **Research round 2026-07-09 (§10):** 12-agent codebase+web sweep, verdict **viable with changes** — §3.1/§3.3/§3.4 corrected in place, §6 reduced to the genuinely hands-on spike set, §8.1 records the pre-spec design decisions (settled with the owner 2026-07-09). **Phase 0 spike complete 2026-07-09 (§11):** hands-on matrix + measured costs — verdict **GO**; §6 items 1–3/5 resolved-with-numbers, item 4 (live RO probe) converted into a bounded Phase-1 work item; §3.1 mechanism + §3.2 "metadata-only" corrected in place. **Phase 1 Slice A complete + live-validated 2026-07-10 (§11.4):** orchestrator RO-reader plumbing built/tested; Nextcloud RO guarantee proven against a real backend; six integration bugs caught+fixed. **Phase 1 Slice B complete 2026-07-11:** the workspace mount stack is implemented and merge-approved on `develop` (plan `docs/superpowers/plans/2026-07-11-protected-cloud-mode-phase1-slice-b-mount-stack.md` — canary cure with real probe refs, `OverlayMountManager` + refresh/heal methods, delete/quota guards, snapshot `--xattrs`/`--acls`, fail-closed orchestrator+agent wiring end to end; ENOTCONN monitor *wiring* and workdir-epoch hardening explicitly deferred to Slice C per the plan's fix-wave section). Remaining before protected mode can engage anywhere: the live k3d validation gate (§Post-Slice-B in the plan; k3d NC groupfolders 19.1.18 < 20.1.2 floor must be bumped first) and Slice C (toggle UI, review/apply, S3 staging). **DECISION 2026-07-11 (§9.2): OpenCloud is dropped from protected mode (live-mode only).** OpenCloud has no clean machine credential for a scoped reader (its read-only is a per-Space *role* needing a reader account, but oCIS offers no app-password and every alternative needs an infra/policy concession); Nextcloud gives self-hosters the same capability natively, and Google Drive / MS365 get read-only from an OAuth *scope* on the user's own token (a different, tractable model). See §9.2 for the full rationale. Next artifact: the Phase 1 Slice B/C plan (Nextcloud + the OAuth-cloud scoped-token model), not OpenCloud.
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

**Whiteout enumeration is engine-ambiguous by design:** fuse-overlayfs *also* prefers char(0,0) device whiteouts (on an emptyDir/PVC upper — the deciding factor is the upperdir's **backing filesystem**, not process caps: the uid-1000 process runs with CAP_MKNOD *absent* yet still mknods char(0,0) on emptyDir per kernel ≥5.8; **mechanism corrected 2026-07-09, §11.1**), falling back to `.wh.<name>` files on an overlayfs-backed upper; opaque dirs are marked by any of `trusted.overlay.opaque` / `user.overlay.opaque` / `user.fuseoverlayfs.opaque` xattrs or a `.wh..wh..opq` sentinel — and both styles can coexist in one upperdir. The diff enumerator must therefore handle **all forms from day one** (and ignore `user.fuseoverlayfs.*` metadata xattrs); done once, it works under either engine and keeps them swappable.

### 3.2 Why an overlay instead of "CRUD only through a tool"

The owner's seed note scoped writes to a diff-capturing tool. The earlier objection (recorded in [[cloud_version_history_and_recovery]] §2) was that with a live mount **plus a shell**, any tool-level hook is bypassable (`rm`, `sed -i`, `>`, `curl`). The overlay dissolves the dilemma rather than picking a side:

- The **shell keeps working** — `grep`, `sed -i`, build scripts, `rm -rf` — full local-directory ergonomics.
- **Every write is captured anyway**, because writes physically cannot reach the lower layer; they land in the upperdir (copy-up for modifications, whiteout markers for deletions).
- `rm -rf huge-dir/` is the showcase: the overlay records **whiteouts** (deletion markers) in the upperdir; the cloud is untouched; the diff view lists N deletions; **Reject = delete the upperdir** and nothing ever happened. **(Corrected 2026-07-09 — §11.1: this is NOT metadata-only on fuse-overlayfs-over-rclone; whiteouting a *cold* lower file opens it → ~1 backend round-trip per file, and deleting an unread file downloads its full body first. The Reject/diff *UX* is unaffected; the bulk-delete *cost* is a priced-in Phase-1 design input — §11.6.)**
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

1. ✅ **The FUSE-on-FUSE prototype** — no authoritative source specifically blesses *rclone* as a lowerdir; community precedent is 2017-2020 read-heavy media workloads. Mount fuse-overlayfs over the actual rclone WebDAV mount on the real workspace image + VM tier and run the matrix: (a) `rm -rf` whiteout storm + enumeration fidelity (incl. opaque-dir renames, binaries), (b) copy-up timing on a ~100 MB file, (c) external WebDAV edit mid-session → refresh op → verify upperdir intact, (d) readdir/merge latency on a 10k-file tree, (e) build-like write workload (fuse-overlayfs write-path overhead is claimed ~2-7x kernel — writes are scratch-local so likely acceptable; measure). **→ §11.1: RESOLVED, GO** — copy-up 100 MB 2.24 s, cold readdir +5%, writes 1.14×; enumerator faithful; **bulk-delete = O(files) backend round-trips (§3.2 corrected)** and added dirs surface as opaque DELETE+PUT (Phase-1 inputs).
2. ✅ **Refresh/heal with a live agent** — the §3.4 refresh op and rclone-mount-death recovery (no health monitor exists today: `src/services/cloud_mount/__init__.py` has unmount scripts only) against open FDs on the merged view; define ordering (unmount overlay → restart/flush rclone → remount overlay) and what the agent experiences mid-turn. **→ §11.2: RESOLVED** — refresh = quiesce → plain unmount → `vfs/refresh` (never `vfs/forget`) → remount; held FDs get silent stale reads (quiesce mandatory); rclone-death detected only by an ENOTCONN read-probe, heal = overlay-first `-uz`.
3. ✅ **Snapshot sequencing** — capture the upperdir **without traversing the merged mountpoint** (a naive tar of the merged view would pull the whole cloud tree through rclone); quiesce-vs-torn-copy-up rules; restore ordering on resume (upperdir → rclone mount → overlay) so staged changes survive pod churn (`snapshot_service.py:371-386` already scopes `/home/agent-host/`, so upperdir at `/home/agent-host/.overlay/{upperdir,workdir}` is captured by placement). **→ §11.3: RESOLVED** — upper/work inside scope, merged + raw lower OUTSIDE; verified 9 members / 0 cloud bytes / 1.15 s; add `--xattrs`/`--acls` to the tar.
4. ⏳ **Live RO probe** — run the §3.3 fail-closed probe against real Nextcloud (≥28.0.3) and OpenCloud instances; verify every mutating verb 403s for the dedicated RO identity. **→ §11.4: OPEN — NOT RUN** (no spike plan task covered it); the probe module + 19 tests exist, but a live run needs the Phase-1 RO identity + a canary-fixture cure (synthetic ids land inconclusive → refuse) and live NC/OpenCloud validation.
5. ✅ **Etag-baseline walk cost** — measure the mount-time PROPFIND enumeration at target tree sizes (project folders now; user-home scale later) to size the §8.1 baseline-capture decision. **→ §11.5: RESOLVED** — cost ∝ **directory** count (~2.4 s/PROPFIND dev NC); sequential BFS fails past ~50–100 dirs ⇒ **`Depth: infinity`-first + concurrent-BFS fallback** required; fix the `_propfind` double-subdir bug before building baselines.

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
2. **OpenCloud RO reader — RESOLVED 2026-07-11: DROP OpenCloud from protected mode (live-mode only).** OpenCloud stays a supported **live-mode** backend (today's rw session mount + the §5 recovery floor); it does **not** get the RO-reader/protected path. Self-hosters who want protected mode use **Nextcloud**.

   *Rationale (from live dev-cluster oCIS validation + web research, 2026-07-10/11).* Protected mode needs, per backend, (1) a read-only reader identity on the folder and (2) a **credential** that identity can authenticate with. OpenCloud does (1) fine — reader user + Space **Viewer** role, both live-verified — but has no clean answer for (2): oCIS is OIDC/Keycloak-only with no Nextcloud-style app-password, and every credential path needs an infra/policy concession — **app-tokens** work but only via `PROXY_ENABLE_BASIC_AUTH` (oCIS docs: dev/test only); **ROPC** needs a Keycloak direct-grant client (none exist; unproven oCIS accepts the token); **token-exchange** is rejected by prod's shared KC ([[srw-prod-private-cloud-sync-token-exchange]]). The three backends split into two working models plus OpenCloud stuck between them: **Nextcloud** = reader account + app-password (native, proven); **Google Drive / MS365** = read-only **scope** on the user's *own* OAuth token, no reader account (`drive.readonly`; MS Graph `Files.Read[.All]`, enforced at the app-registration/token layer); **OpenCloud** = per-Space role (needs a reader account, like NC) but no app-password and no read-only OIDC scope (unlike either), so it gets the worst of both.

   *Consequences.* (a) The Slice A OpenCloud `SupportsRoReader` code (`ensure_ro_reader`/`mint_ro_grant`/…) is **validated but left unwired** — vestigial; keep as a documented dev-only escape hatch (flip `PROXY_ENABLE_BASIC_AUTH=true` on a dev oCIS + the confirmed `POST /auth-app/tokens?userId=` on-behalf mint), or remove in a later cleanup. (b) Protected mode's credential layer has **two shapes**: the reader-account model (`SupportsRoReader`, Nextcloud) and a future **read-only-scoped-token** model for the OAuth clouds (Google/MS365) — the abstraction must not over-fit the reader-account model. (c) §11.4's OpenCloud canary/live-RO item and §8.1.4's OC-reader path are **closed as won't-do**.
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

## 11. Phase 0 spike results — 2026-07-09

The Phase 0 spike (§8 Phase 0; plan `docs/superpowers/plans/2026-07-09-protected-cloud-mode-phase0-spike.md`) ran the §6 hands-on set plus the two permanent artifacts. **Verdict: GO** (§11.8) — no feasibility blocker; every §6 unknown either resolved-with-numbers or (item 4) converted into a bounded Phase-1 work item. Two permanent modules landed on `develop` and import into Phase 1 unchanged: the dual-format whiteout→diff enumerator `src/services/cloud_overlay/whiteout.py` (10 tests; hardened across the spike — malformed-marker `ValueError`, phantom `.wh..opq` char-node skip from live data, and a documented opaque-added-dir contract + `Raises`) and the fail-closed RO probe `orchestrator/services/cloud/ro_probe.py` (19 tests; a mandatory **positive read control** — an authenticated `PROPFIND Depth:0` must return 2xx before any "read-only verified" verdict, so a dead/misconfigured credential cannot pass by returning 401 everywhere; rejection set narrowed to **403/405 only**). **Code:** commits `7bd12f3c..891ca0cf` on `develop` (spike + two final-review fix rounds; unpushed as of 2026-07-09). **Read every number below against §11.7's environment caveats:** all hands-on cells ran on local k3d against a manually-built Path-B pod over **local Nextcloud 31 (sqlite)** WebDAV — not the provisioned mount path, not OpenCloud, not the VM tier. Absolute latencies are dev-pessimistic; the durable results are the mechanisms, overhead *ratios*, and cost *shapes*.

### 11.1 FUSE-on-FUSE prototype (§6.1) — ✅ RESOLVED · GO

fuse-overlayfs 1.13 mounts cleanly over a live **read-only** rclone 1.74.3 WebDAV lower **as uid 1000** (setuid `fusermount3`; `/dev/fuse` only, no CAP_SYS_ADMIN in the calling process), zero warnings — confirming the §3.1 privilege argument end-to-end. Matrix (428-object tree):

- **Copy-up** (1-byte edit → full 100 MB pulled lower→upper): **2.24 s** cold, 0.153 s warm — transfer-dominated, matching §7's copy-up caveat.
- **Cold readdir overhead ≈ +5%** over the raw rclone lower (11.22 s vs 10.67 s, true cold-vs-cold with remount between passes); warm listings single-digit **ms**. readdir cost is cloud PROPFIND latency, not the overlay.
- **Build-like write workload 1.14× local disk** (3.69 s vs 3.23 s, 1,552 files) — far below the "~2–7×" fuse-overlayfs claim §6.1(e) flagged; upper writes are scratch-local.
- **`enumerate_diff` fidelity: faithful.** The Task-2 enumerator read real fuse-overlayfs-over-rclone upperdirs with zero errors, mapping char(0,0) / `.wh.` / opaque-dir markers correctly. It was hardened three times across the spike — fail loudly (`ValueError`) on malformed empty-remainder markers (Task-2 review), skip phantom `.wh..opq` char bookkeeping nodes (Task-4 live data), and document the opaque-added-dir contract + a `Raises` section so a Phase-1 caller does not misread an added directory as a deletion (final review) — all now permanent, the first two with tests (10 total).

**§3.1 whiteout mechanism — CONFIRMED, mechanism corrected.** The whiteout *form* depends on the **upperdir's backing filesystem**, not process capabilities. The uid-1000 fuse-overlayfs process runs with **CapEff=0x0 (CAP_MKNOD absent)** yet still `mknod`s a char(0,0) node on the emptyDir (kernel ≥5.8 permits unprivileged overlay whiteouts); on an overlayfs-backed upper the same `mknod` returns EPERM and fuse-overlayfs 1.13 `create_whiteout()` (source-verified: mknod-first) falls back to `.wh.<name>`. **Production emptyDir upperdirs (`/home/agent-host`) will emit char(0,0); ephemeral/rootfs uppers emit `.wh.`** — both occur in realistic deployments, so the §3.1 dual-format enumerator is vindicated, not optional. (This corrects §3.1's "containers hold CAP_MKNOD by default" reasoning; the conclusion is unchanged.)

**§3.2 correction — `rm -rf` is NOT metadata-only on this stack.** §3.2's showcase claim ("the operation is metadata-only (no downloads)") is **falsified** for fuse-overlayfs-over-rclone: whiteouting a lower file opens it, so a cold `rm -rf` costs **≈ 1 backend round-trip per file** (225 files = **2m26s cold / 0.24 s warm**), and deleting an *unread* file **downloads its full content first** (100 MiB transferred through rclone to `unlink` one 100 MB file — `rclone core/stats` byte-verified). A directory rename is per-file copy-up (15-file dir ≈ 9.5 s; warming the *directory listing* does not help — only reading *file contents* does). The diff/apply **UX is unaffected** (Reject still just discards the upperdir), but **bulk-delete cost is a real Phase-1 design input** (VFS warm-cache priming, size-aware guards, or backend-side bulk delete instead of per-file overlay whiteout) — §11.6.

**New design input — added directories surface as DELETE+PUT.** fuse-overlayfs marks **every** directory created through the merged view opaque at creation, so a freshly *added* directory tree appears in `enumerate_diff` as an opaque-dir "deleted" entry (for a dir that never existed in the lower) followed by its "present" children. Phase-1 apply must treat opaque-dir deletes as **lower-existence-aware no-ops** (404-tolerant), and the review UI must **not render a newly added folder as a deletion/replacement**.

*Sub-items §6.1(a)–(e) all measured; go/no-go = **GO**.*

### 11.2 Refresh / heal with a live agent (§6.2) — ✅ RESOLVED

**Refresh op (the §3.4 first-class refresh) — sequence settled:** **quiesce the agent** (stop I/O, drop cloud FDs at a turn/tool boundary) → **plain** `fusermount3 -u` the overlay → `rclone rc vfs/refresh recursive=true` → remount overlay → resume. Two hard facts forced quiesce-first:

- **`vfs/forget` does not flush already-read file content; `vfs/refresh` does** (re-PROPFINDs, detects changed mtime/size, invalidates). This closes the Task-4 open item — the refresh op's flush step must be `vfs/refresh` (or a full rclone remount), never `vfs/forget`.
- **Held FDs across a lazy-unmount refresh get SILENT STALE READS.** Proven with a mid-window backend edit + positive control: a reader holding an FD returned **60/60 old-byte reads, zero errors, ~25 s past a *completed* `vfs/refresh`** while fresh opens saw the new bytes. No error signal exists — so quiescing the agent is **mandatory, not advisory**; the plain `-u` (which EBUSYs while FDs are held) is the guard that the agent actually let go. If forced to lazy `-uz`, the agent MUST reopen FDs afterward.

**rclone-mount death under a live overlay — detection + heal:** `/proc/mounts` and `mountpoint -q` both **lie** (both report "mounted" over a dead endpoint; `mountpoint -q` on the merged view even exits 0). The only reliable signal is a **read/readdir probe returning ENOTCONN**. Heal ordering (executed + verified): **unmount the overlay FIRST with `-uz`** → lazy-unmount the dead rclone lower → remount rclone → remount overlay; agents reopen FDs after. Lazy `-uz` is safe *here* (unlike the refresh path) precisely because the dead lower makes every held-FD read fail loudly with ENOTCONN — no silent-staleness window exists. This is the §6.2 ordering, now evidenced; Phase 1 builds the missing health monitor (`src/services/cloud_mount/__init__.py` has unmount scripts only) around the ENOTCONN probe.

**INFERRED hazard (label as such):** a lazy-unmount refresh transiently leaves **two fuse-overlayfs instances sharing one workdir** (fuse-overlayfs does not refuse the second mount). No corruption seen in short tests, but concurrent workdir use is unsupported — Phase 1 must drain the old instance or give the new mount a fresh workdir.

### 11.3 Snapshot sequencing (§6.3) — ✅ RESOLVED

**Placement rule (hard):** upperdir + workdir **inside** the snapshot scope at `/home/agent-host/.overlay/{upper,work}`; the **merged mountpoint AND the raw rclone lower both OUTSIDE it** (e.g. `/workspace-cloud/merged`, `/cloud/home`). Verified against the real product tar shape (`snapshot_service.py:371-391`): with correct placement the snapshot captured **9 upperdir members (whiteouts + opaque sentinel included), 0 cloud bytes** (`rclone core/stats` identical before/after), **1.15 s**, no FUSE stall. With the merged mount **inside** scope, the same tar streams the cloud through rclone (observed 65 objects pulled at the 90 s cap before timing out mid-walk; full-download cost **extrapolated**, mechanism = §11.1's O(files) cold reads). §6.3's placement guess is confirmed correct.

**Upperdir survives an overlay remount byte-identical** — whiteouts, opaque `user.fuseoverlayfs.opaque` xattr, and `.wh..wh..opq` sentinel all intact across unmount/remount (upper/work are plain files on the emptyDir; remount never touches them).

**Restore fidelity — one hardening item.** The product tar has **no `--xattrs`**: char(0,0) whiteouts round-trip faithfully onto an emptyDir (restore call site code-verified — `EXTRACT_REMOTE_CMD = "zstd -d | tar -xf - -C /"`, `orchestrator/services/ssh_helpers.py:28`, lands upper members at `/home/agent-host/...`), and opaque-dir semantics survive via the `.wh..wh..opq` sentinel alone even with the xattr dropped. **But** extraction onto an overlayfs-backed rootfs as uid 1000 **fails** ("Cannot mknod") — so Phase 1 should add **`--xattrs`/`--acls`** to the snapshot tar (cheap belt-and-suspenders) and ensure restore untars onto the non-overlayfs emptyDir/PVC. The "`/home/agent-host` is emptyDir/PVC everywhere" premise matches the provisioner context but was not audited across all runtime variants — **asserted-plausible**.

### 11.4 Live RO probe (§6.4) — ⏳ OPEN (WIRED + FIRST LIVE RUN done on Nextcloud; canary + groupfolders bump remain)

**First live run — Nextcloud 31.0.14 on k3d (2026-07-09).** Slice A was validated end-to-end against the real local Nextcloud (in-pod, tilt-synced). **The RO guarantee HOLDS against a real backend:** a per-mount RO reader minted by `mint_ro_grant` is denied every write at the Nextcloud ACL layer — `PUT`/`DELETE`/`MKCOL` → 403, `PROPPATCH` inner-status → 403, `COPY` (with Destination) → 403 — while `PROPFIND` reads succeed (207) and `revoke_ro_grant` drops access (404). Provisioning → grant → revoke, `capture_etag_baseline` (real etag), and the `_propfind` fix all worked against real NC. The live run also surfaced **three real `ro_probe` bugs** (all fail-closed/over-refuse, now fixed — commit `2e93ebe8`): (1) `check_version_floors` read `capabilities.groupfolders.version` but real NC 31 exposes `appVersion`; (2) the PROPPATCH check treated the 207 multistatus *envelope* as an open write instead of reading the inner propstat status; (3) the COPY check sent no `Destination` header, so NC returned 400 (masking the real 403). **Two things still gate a green live engage:** (a) dev's **groupfolders app is 19.1.18, below the 20.1.2 CVE floor** — a real deployment bump, not a code issue; (b) the canary side channels still use synthetic ids (versions/trash MOVE → 409, tus → 404 = inconclusive), so `engage_ro_mount` correctly REFUSES until the canary supplies real version/trash ids. OpenCloud was NOT validated (not deployed on k3d; still §9.2/live-blocked).

**OpenCloud live run — real oCIS on the main dev cluster (2026-07-10).** The dev SRW deployment runs OpenCloud (`srw-opencloud`), so the OC RO-reader *provisioning* path was validated against real oCIS (in-pod, code overlaid + restored). **Confirmed working:** `capture_etag_baseline` (Depth:1 BFS — live-confirms §11.5's "oCIS rejects `Depth: infinity`" claim, previously code-read-only), `ensure_ro_reader` (LibreGraph user create), `mint_ro_grant` (Space **Viewer** role actually assigned on the drive — verified via the drive permissions list), and `revoke_ro_grant` (permission delete). **One real bug caught + fixed (`7f70f35e`):** oCIS requires `onPremisesSamAccountName` on `POST /graph/v1.0/users` (400 without it); A6 only set it conditionally, so it 400'd live while the permissive fake passed — the §11.7 "unverified live" gap. **Still OPEN for OC:** the RO-write-*denial* as the reader is unproven because the reader must authenticate (a bearer), which needs the reader's **Keycloak identity** — the §9.2 item Slice A does not yet implement. So OC provisioning + Viewer-assignment are now live-verified; the OC RO *guarantee* still depends on §9.2.

**Prior Slice A wiring status:** the probe module is WIRED into a fail-closed engage gate — `orchestrator/services/cloud/ro_engage.py::engage_ro_mount` (unit-tested: persist-on-ok, revoke-on-open-write, refuse-on-dead-credential, refuse-below-version-floor). It provisions the per-user reader + per-mount grant (`SupportsRoReader` on both backends, §8.1.4), seeds a canary fixture via the write identity, runs `check_version_floors` + `probe_read_only` **as the reader**, and only persists the `cloud_ro_mounts` row when `RoProbeResult.ok`. The **remaining** open item is the canary **live status-code tuning**: the canary's `version_ref`/`trash_ref` are still `None`, so the CVE side channels stay `inconclusive`.

**Phase-1 Slice A update (2026-07-09):** the probe module is now **wired** into a fail-closed engage gate — `orchestrator/services/cloud/ro_engage.py::engage_ro_mount` (unit-tested: persist-on-ok, revoke-on-open-write, refuse-on-dead-credential, refuse-below-version-floor). It provisions the per-user reader + per-mount grant (`SupportsRoReader` on both backends, §8.1.4), seeds a canary fixture via the write identity, runs `check_version_floors` + `probe_read_only` **as the reader**, and only persists the `cloud_ro_mounts` row when `RoProbeResult.ok`. The **remaining** open item is narrowed to the **live status-code tuning**: the canary's `version_ref`/`trash_ref` are still `None` (real NC/OC version+trash id discovery), so the CVE side channels stay `inconclusive` and — correctly, under the strict gate — a live run REFUSES until tuned against real Nextcloud ≥28.0.3 AND OpenCloud. That tuning is the one genuinely-open item; the provisioning + gate wiring it depended on now exist.

**Not run (original).** The §6.4 live probe against real Nextcloud (≥28.0.3) and OpenCloud was covered by **no spike plan task** (the plan's self-review mislabeled §6.4 as "whiteout formats"), and running it needs the dedicated RO identity + real fixture ids that are themselves Phase-1 provisioning work (§8.1.4). **What exists and is done:** the fail-closed probe module `orchestrator/services/cloud/ro_probe.py` (19 tests) with strict engage-gate semantics — `ok` is a property that **refuses on any failure, skipped, OR inconclusive** check. A mandatory **positive read control** runs first: an authenticated `PROPFIND Depth:0` on the target must return 2xx before any rejection verdict counts, so a dead/misconfigured RO credential that returns 401 on every verb can no longer masquerade as "read-only verified" (the rejection set is narrowed to **403/405 only**; a 401 now counts as *not* rejected). Plus version floors via the OCS capabilities endpoint (§3.3: NC server ≥ 28.0.3, groupfolders ≥ 20.1.2) and **real** CVE side-channel request builders (versions-restore / trashbin-restore MOVEs, chunked/TUS finalize; the two earlier "fake" same-URL POSTs were removed). **Honest consequence:** a live run *today* with synthetic ids will land **inconclusive** and, under the strict gate, **REFUSE** — which is correct, not a bug. The cure is Phase-1 work: the orchestrator's write identity seeds a **canary fixture** (create → version → trash a real file), the RO identity attempts restore of those **real** ids, and the status-code map is validated/tuned against **live Nextcloud ≥28.0.3 AND OpenCloud** before protected mode may engage on either backend. Until then §6.4 remains the one genuinely-open item — but it is a provisioning/validation task, not a physics unknown.

### 11.5 Etag-baseline walk cost (§6.5) — ✅ RESOLVED (with a scoping consequence)

Measured through the **real product path** (`main_cloud_router` → `NextcloudBackend.list_project_folder`, real Group Folders via two API-provisioned projects). **The cost scales with DIRECTORY count, not file count** — walk ≈ (1 + #dirs) × per-PROPFIND latency; files are near-free:

- 50 files / 0 dirs: **1.15–2.44 s** (1 request).
- 300 files / 10 dirs: **~26 s / 11 requests** (~2.4 s per PROPFIND on dev NC/sqlite — write-pessimized, reads less so).
- Extrapolation (two-point-calibrated; sqlite per-request growth **unverified** — treat as order-of-magnitude): 10k files ≈ **13.4 min** dev / **~50 s** at optimistic prod latency; 100k files ≈ **2.2 h** dev.

**Consequence for §8.1.3 (full-tree PROPFIND at mount):** the sequential depth-1 BFS lean does **not** hold past ~50–100 directories (a 100-dir project ≈ 4 min dev / ~15 s optimistic-prod, on the mount path before the user can work). It survives **only** if implemented **`Depth: infinity`-first** — on this Nextcloud one infinity PROPFIND returned the whole 310-entry tree with all etags in **1.2–2.4 s** (OpenCloud 400s `Depth: infinity` per the `opencloud.py:763` code comment — **not tested live**, so this is per-backend) — **with a bounded-concurrency BFS fallback**. If Phase 1 ships the sequential walk as-is, touched-paths scoping becomes necessary as soon as real projects exceed ~50–100 directories, not "later."

**Product bug found (pre-existing, NOT fixed by the spike):** `NextcloudBackend.list_project_folder` returns every subdirectory **twice** — `parse_propfind_entries` drops the self-href only at the walk root (`orchestrator/services/cloud/_propfind.py`). It does not distort this bench (request-gated, dict-keyed) but **must be fixed before the §3.4 etag baseline builds path→etag maps from it**, or the conflict gate double-processes dirs.

### 11.6 Design amendments Phase 1 must ingest

The spike confirms the direction and *prices in* these costs — the Phase-1 plan must carry them:

1. **Bulk-delete cost** (§11.1): `rm -rf` / large deletes are O(files) cold backend round-trips and can download full file bodies to whiteout them; add VFS warm-cache priming and/or size-aware guards, or route bulk deletes to backend-side ops rather than per-file overlay whiteout.
2. **Added-dirs-as-opaque apply semantics** (§11.1): opaque-dir "deletes" for never-in-lower dirs ⇒ apply must be lower-existence-aware / 404-tolerant; review UI must not render added folders as deletions.
3. **Refresh/quiesce sequence** (§11.2): quiesce → plain unmount → `vfs/refresh` → remount; never `vfs/forget`; ENOTCONN-probe health check; avoid dual-instance workdir sharing.
4. **Infinity-first etag walk + the `_propfind` fix** (§11.5): probe-and-use `Depth: infinity` with a concurrent-BFS fallback; fix the double-subdir bug before building baselines.
5. **RO-probe fixture cure + live validation** (§11.4): canary-fixture real-id probing, validated against live NC ≥28.0.3 and OpenCloud, before protected mode engages anywhere.
6. **Snapshot placement rule + tar xattrs hardening** (§11.3): merged/lower outside the snapshot scope, upper/work inside; add `--xattrs`/`--acls`; restore onto non-overlayfs emptyDir/PVC.

### 11.7 Environment caveats — read the numbers accordingly

- **Backend:** local **Nextcloud 31 (sqlite)** over WebDAV, not OpenCloud (in-chart OC is config-broken locally — crash-loops on service-account config, no chart env seam). All OpenCloud-specific claims (`Depth: infinity` 400, Space Viewer RO identity) remain **code-read only, unverified live**.
- **Mount path:** a manually-built **Path-B** pod (privileged, `/dev/fuse`, k3d-default `WORKSPACE_FUSE_PRIVILEGED=true`), replicating the provisioner's FUSE context + product rclone flags — **not** the orchestrator-provisioned workspace path. The tighter **SYS_ADMIN-only (non-privileged) pod variant is untested**.
- **VM tier:** fuse-overlayfs is installed in `docker/agent-vm-base/scripts/provision-stage1.sh` but verified by **inspection + `bash -n` only** (no VM built this spike); the container workspace image is the build-verified one.
- **Tree sizes** capped (428 objects matrix / 350 bench) by dev-NC fragility — an over-aggressive first seed crash-looped Nextcloud once. **Absolute latencies are dev-pessimistic; shapes and mechanisms are the durable findings.**

### 11.8 Verdict — **GO**

**Direction confirmed: proceed to the Phase 1 implementation plan.** The FUSE-on-FUSE capture overlay is feasible and behaves correctly — no feasibility blocker survived the spike. Every §6 unknown is resolved-with-numbers (items 1–3, 5) or converted into a bounded Phase-1 work item (item 4, the live RO probe — a provisioning/validation task, not a physics unknown). GO is **conditional on the Phase-1 plan ingesting the §11.6 amendments**: bulk-delete cost, added-dirs-as-opaque apply semantics, the refresh/quiesce sequence, the infinity-first etag walk + `_propfind` fix, the RO-probe fixture cure + live validation, and the snapshot placement rule + tar xattrs hardening. These are the priced-in costs of the chosen design, not open feasibility questions.

## References

**Internal:** [[agent_action_reversibility]] (parent principle; this resolves its open-Q4 for the cloud slice), [[cloud_version_history_and_recovery]] (the recovery floor this layers over), [[rclone_cloud_mount]] (the session mount this builds on), [[job_cloud_export]] (Mode A — the review/apply machinery this generalizes), [[main_cloud_abstraction]] + `docs/issues/main_cloud.md` (control plane, resolution seam), [[webdav_datasource_tools]] (why datasource tools are separate), [[cloud_collaboration_model]], [[guardrails_matrix]].

**External anchors:** kernel overlayfs (upperdir/lowerdir/whiteouts/opaque dirs — the container-layer model); fuse-overlayfs (rootless overlay, `/dev/fuse` only); `docker diff` (upperdir-as-changeset precedent); YoloFS arXiv 2604.13536 (staged agent FS with review gate); arXiv 2606.22721 (review habituation); rclone `--read-only` + VFS cache (read path) and rclone#1710 (no xattrs); Nextcloud GHSA-5mq8-738w-5942 / GHSA-2vrq-fhmf-c49m (RO-bypass CVE class → version floors); note that rclone union / mergerfs are **not** substitutes (no copy-up, no whiteouts — deletes/modifies of RO-branch files fail instead of staging).
