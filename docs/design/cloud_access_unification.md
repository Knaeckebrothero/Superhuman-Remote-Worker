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

**Status:** direction doc (landscape + agreed direction, **not** an implementation spec) — 2026-07-07. Written under the feature-freeze: this records the convergence so the build can be scheduled deliberately. Gate to graduate into a spec: the §6 spike.
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
| 1 | **rclone FUSE mount** at `/workspace/cloud/*` | sessions only | lazy read-through mount, Keycloak bearer / WebDAV | **live** — standard file tools + shell write straight to the cloud | none | `src/services/cloud_mount/`, built at `_build_agent_cloud_mount` (`orchestrator/main.py:15538`, injected only via `/api/agents/threads/{id}/workspace`), [[rclone_cloud_mount]] |
| 2 | **WebDAV sync fallback** (`cloud_sync`) | sessions only, when the mount is inactive | `pull_all()` before turn / `push_all()` after turn | live at turn boundaries | none | `src/services/cloud_sync/` (`WorkspaceSyncBase`), `_build_agent_cloud_sync` (`main.py:15320`) |
| 3 | **Mode A Gitea seed + diff review** | project-attached jobs | orchestrator seeds cloud→Gitea at dispatch; agent edits plain workspace files; tree-diff at completion → `pending_review` | **staged** — zero cloud writes during execution | **yes** — Cockpit diff view, Accept applies / Reject discards | `orchestrator/services/job_cloud_baseline.py`, REST `main.py:10981–11268`, Cockpit `job-diff-review` |
| 4 | **Mode B export** | loose jobs | user-triggered walk of `output/` → PUT to a new shared folder | staged (export-on-click) | implicit (user clicks) | `main.py` Mode-B export, [[job_cloud_export]] |
| 5 | **`webdav_*` datasource tools** | jobs + sessions, only when a webdav **datasource** is attached | raw `webdav3` client per call | live | none | `src/tools/webdav/tools.py`; deliberately datasource-scoped since [[webdav_datasource_tools]] (2026-06-07) |
| 6 | *(beneath 3/4)* control-plane byte methods | orchestrator only | `list/get/put/delete_project_folder_file_bytes` — used at seed, apply, export time | n/a (orchestrator is the writer) | n/a | `orchestrator/services/cloud/{opencloud,nextcloud}.py` |
| 7 | *(beneath everything)* S3/MinIO object store backing the cloud | nobody directly (agent has no S3 key) | planned: bucket versioning + lifecycle | n/a | n/a — **recovery**, not access | [[cloud_version_history_and_recovery]] (paused on its §9) |

**What the split costs us:**

- **Three separate project-folder implementations** (1, 2, 3) over the same WebDAV byte layer — every fix/feature lands N times or drifts.
- **Mode A limitations** baked in by the Gitea intermediary: text/UTF-8 files only (binaries silently skipped from seed *and* diff), seed latency at dispatch proportional to folder size, dispatcher gated on `cloud_baseline.state`.
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
agent sees:   /workspace/cloud            ← overlay mount (writable, normal dir semantics)
                 upperdir: local scratch      ← ALL writes land here (the staged diff)
                 workdir:  local scratch
                 lowerdir: rclone FUSE mount  ← READ-ONLY, read-only-scoped credentials
cloud sees:   nothing until approval; then the orchestrator applies the diff
```

This is the container-image layering model (kernel overlayfs / fuse-overlayfs), not invented tech. The mount is never written; the **upperdir *is* the diff**, materialized on local disk by construction.

### 3.2 Why an overlay instead of "CRUD only through a tool"

The owner's seed note scoped writes to a diff-capturing tool. The earlier objection (recorded in [[cloud_version_history_and_recovery]] §2) was that with a live mount **plus a shell**, any tool-level hook is bypassable (`rm`, `sed -i`, `>`, `curl`). The overlay dissolves the dilemma rather than picking a side:

- The **shell keeps working** — `grep`, `sed -i`, build scripts, `rm -rf` — full local-directory ergonomics.
- **Every write is captured anyway**, because writes physically cannot reach the lower layer; they land in the upperdir (copy-up for modifications, whiteout markers for deletions).
- `rm -rf huge-dir/` is the showcase: the kernel records **whiteouts** (deletion markers) in the upperdir; the cloud is untouched; the operation is metadata-only (no downloads); the diff view lists N deletions; **Reject = delete the upperdir** and nothing ever happened.
- Modifications get old/new for free: old = lower (pristine in the cloud), new = upper. **Binary files work** — no Gitea in the middle — which retires Mode A's text-only limitation.

Note what was rejected before and stays rejected: *intercepting writes inside the mount* (rclone has no hook/plugin system). The overlay doesn't intercept — it layers above a mount that is read-only.

### 3.3 Trust model — Mode A generalized

Read-only must be **credential-level**, not an rclone flag: in protected mode the orchestrator provisions the mount with a **read-only-scoped identity** (Nextcloud: RO share/ACL grant to the mount identity; OpenCloud: viewer-role share — exact mechanism per backend is a spike item, §6). Then even extracted creds + `curl` cannot write. Write credentials exist **only orchestrator-side, used once, at apply time, after approval** — which is exactly Mode A's trust model today (agent stages, orchestrator is the sole cloud writer). Protected sessions become "Mode A with a live lazy view instead of a Gitea seed."

### 3.4 Review + apply — reuse, don't rebuild

- **Diff extraction:** walk the upperdir → `{path, status: added|modified|deleted}` (whiteouts = deleted). Same shape Mode A's summary already serves.
- **Review UI:** the Cockpit `job-diff-review` component (file tree + Monaco side-by-side + Accept/Reject) generalizes; sessions get a "cloud changes (N)" surface backed by the same endpoints.
- **Apply:** `apply_diff_to_cloud()` walks the diff and PUTs/DELETEs via the control-plane byte methods — source changes from "Gitea at HEAD" to "upperdir", the rest is shared.
- **Conflicts:** `detect_external_mods()` (etag map at mount time vs fresh listing at apply) carries over unchanged; 409 on divergence.
- **After apply in a continuing session:** clear the upperdir + refresh the lower view (likely a remount at the approval boundary — spike item, overlayfs dislikes the lower changing underneath it).

### 3.5 Live mode (toggle off)

Today's session mount, unchanged: writable rclone mount, live writes, user sees edits in the cloud immediately. The recovery floor below (§5) is what makes live mode sane.

## 4. What this eliminates / what it keeps

**Eliminates (over the phasing in §8):**
- The Mode A **Gitea seed** (path 3's capture mechanism) — and with it the text-only limitation, the seed latency, and the dispatch gate. Gitea remains the *workspace/repo* layer; it stops being the *cloud staging* layer.
- The **`cloud_sync` WebDAV fallback** (path 2) — once the mount is the only session mechanism, the fallback's job disappears (workspaces that can't FUSE-mount need a decision: keep sync as a legacy tier or require mount-capable runtimes).
- The **three-implementations debt** — one mount codepath, one review UX, one apply path for every agent kind.

**Keeps:**
- The **control plane** (`MainCloudBackend`, router seam) — untouched; it gains the "provision RO identity" duty.
- The **review/apply machinery** — generalized, not replaced.
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

This doc does **not** reopen [[cloud_version_history_and_recovery]]'s convergence (version-in-place on the MinIO bucket; paused on its §9 same-data-vs-separable-corpus question). That stays the always-on floor and the answer for the *rogue/residual* tail. Protected mode addresses [[agent_action_reversibility]]'s "staged change awaiting commit" class and — by making the lower read-only at the credential level — resolves its open-Q4 ("where does write-capture live such that a tool can't bypass it?") for the cloud slice: **below the shell, above the cloud, with no write creds in the workspace at all.**

## 6. Feasibility gate — the spike (before any spec)

1. **Overlay over a FUSE lower.** Kernel overlayfs over rclone-FUSE works in practice but has kernel-version quirks; **fuse-overlayfs** (rootless Podman's engine) is the tolerant fallback and needs only `/dev/fuse` — which the workspace runtime already has for rclone. Verify both on the workspace image and the VM tier; pick one.
2. **RO credential minting per backend.** Nextcloud: RO share/ACL to a mount identity (app passwords are not permission-scoped — the scoping must come from the share). OpenCloud: viewer-role share / token-exchange scope. Must be enforced server-side, verified by attempting a direct WebDAV PUT with the mount's creds.
3. **Apply → lower refresh.** Confirm a clean remount (or upperdir purge + cache invalidation) at the approval boundary in a continuing session.
4. **Whiteout/diff extraction.** Confirm whiteout representation (char-device vs xattr-marked, kernel vs fuse-overlayfs) and that the upperdir walk yields a faithful `{path,status}` set, including opaque-dir renames.
5. **Snapshot integration.** The upperdir is local scratch — confirm the existing snapshot service can include it so staged-but-unreviewed changes survive pod teardown.

## 7. Honest costs — it is *not* "a local filesystem with no downsides"

- **Cold reads stream from the cloud.** `grep -r` over the namespace downloads what it reads (rclone VFS cache mitigates re-reads). Inherent to laziness; unchanged from today's mount. The existing cache guardrails (`src/services/cloud_mount/guardrails.py`) compose with the overlay.
- **Copy-up cost.** First write to a file pulls the whole file into the upper layer — trivial for text, real for a 10 GB binary. (The live mount's VFS write cache has essentially the same cost; this is not a regression, just not magic.)
- **Staleness + conflicts.** The lower view is as-of mount time; external edits during a long session surface as 409s at apply (same trade Mode A makes today). Long-lived protected sessions may want a manual "refresh view" action (discards or rebases staged changes — v2 question).
- **Semantics change under protection.** Live-collab sessions lose "user sees agent edits immediately" — that's *why* it's a toggle, not a global switch.
- **Review fatigue risk for sessions.** Jobs have a natural review point (completion); sessions don't. Getting the session review UX right (badge + apply-anytime vs end-of-session vs agent-requested apply) is a real design task (§9), and getting it wrong recreates the approval fatigue [[agent_action_reversibility]] warns about.
- **Upperdir durability.** Staged changes live on pod-local scratch until applied — same risk class as today's uncommitted working tree; §6.5 mitigates.

## 8. Phasing sketch (not committed work)

- **Phase 0 — spike** (§6). Cheap, decides everything. Output: go/no-go + which overlay engine.
- **Phase 1 — protected sessions.** The toggle, RO-identity provisioning, overlay mount, session diff review surface (reusing job-diff-review), apply/reject. Live mode untouched. *This is the piece that unblocks daily-driving autonomous runs on real data, together with the recovery floor.*
- **Phase 2 — jobs adopt the mount.** Project-attached jobs get the same RO mount + overlay instead of the Gitea seed; Mode A's review/apply endpoints stay, their diff source swaps. Retires seed latency + text-only. Gitea reverts to being only the workspace/repo layer.
- **Phase 3 — consolidation.** Retire the `cloud_sync` fallback (or fence it as a legacy tier); decide datasource absorption (§9); revisit Mode B.

**Fallback if the spike disappoints:** the owner's original shape — RO mount + tool-only CRUD (shell writes to the cloud path hard-fail with a guardrail nudge) — ships with zero new moving parts. Weaker ergonomics, same trust model, same review/apply reuse. It is the degraded v1, not a different architecture.

## 9. Open questions

1. **Session review UX granularity** — persistent "pending changes" badge with apply-anytime? end-of-session gate? agent-invoked `submit_cloud_changes`? (Lean: badge + apply-anytime; all-or-nothing apply in v1, selective apply v2.)
2. **RO credential mechanism per backend** (§6.2) — and does the shared prod Keycloak constrain the OpenCloud variant the way token-exchange did ([[srw-prod-private-cloud-sync-token-exchange]] class of problem)?
3. **Datasource absorption** — do `webdav_*` datasources eventually become additional (RO-or-RW) mounts under the same overlay policy, retiring path 5? Attractive (one model for *everything*), but datasources are third-party clouds with their own auth quirks.
4. **Mode B / loose jobs** — home-folder mount under protection instead of export-on-click?
5. **Concurrent writers** — two agents (or agent + user via a second session) staging over the same folder: last-apply-wins with 409s, or lease/lock?
6. **Upperdir size governance** — cap staged-change volume? (An agent that rewrites 500 GB into the upper layer fills the pod's scratch disk.)

## References

**Internal:** [[agent_action_reversibility]] (parent principle; this resolves its open-Q4 for the cloud slice), [[cloud_version_history_and_recovery]] (the recovery floor this layers over), [[rclone_cloud_mount]] (the session mount this builds on), [[job_cloud_export]] (Mode A — the review/apply machinery this generalizes), [[main_cloud_abstraction]] + `docs/issues/main_cloud.md` (control plane, resolution seam), [[webdav_datasource_tools]] (why datasource tools are separate), [[cloud_collaboration_model]], [[guardrails_matrix]].

**External anchors:** kernel overlayfs (upperdir/lowerdir/whiteouts/opaque dirs — the container-layer model); fuse-overlayfs (rootless overlay, `/dev/fuse` only — rootless Podman's engine); rclone `--read-only` + VFS cache (read path); note that rclone union / mergerfs are **not** substitutes (no copy-up, no whiteouts — deletes/modifies of RO-branch files fail instead of staging).
