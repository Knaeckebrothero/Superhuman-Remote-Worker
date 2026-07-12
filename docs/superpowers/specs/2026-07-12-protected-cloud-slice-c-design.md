# Protected Cloud Mode — Phase 1 Slice C: staging → review → apply + toggle

**Status:** design approved by owner 2026-07-12 (this doc formalizes that conversation).
**Master design:** `docs/design/cloud_access_unification.md` (§3.4 review seam, §8.1 settled decisions, §9 open questions — several resolved here).
**Builds on:** Slice A (`docs/superpowers/plans/2026-07-09-protected-cloud-mode-phase1-slice-a-cloud-plumbing.md` — RO reader/grant/probe/engage, `cloud_ro_mounts`, etag baseline) and Slice B (`docs/superpowers/plans/2026-07-11-protected-cloud-mode-phase1-slice-b-mount-stack.md` — capture overlay stack, guards, snapshot xattrs, fail-closed wiring; live-gate PASSED 2026-07-12 on k3d vs real Nextcloud 31.0.14).

## 1. Goal

Deliver the session-facing surface of protected cloud mode: a user can create a **protected** session (agent's cloud writes are captured in an overlay upperdir, never written live), see a **"Cloud changes (N)"** badge as the agent works, open a **review panel** (file tree + side-by-side diff), and **Apply** the whole staged diff to the real cloud folder or **Reject** it — including after the workspace pod is gone. The agent is prompted to describe its cloud writes honestly as staged. Plus the six hardening items Slice B explicitly deferred.

Nextcloud-only (§9.2: OpenCloud dropped from protected mode). Jobs stay on Mode A; the `DiffSource` seam introduced here is what lets Phase 2 swap their diff source later.

## 2. Decisions settled with the owner (2026-07-12)

| Question | Decision |
|---|---|
| Accept/Reject granularity (§9.1 vs Slice A outline conflict) | **Whole-diff apply**: review per-file, Accept applies ALL staged changes, Reject discards ALL — exactly Mode A's job behavior. Per-file selection deferred until usage demands it. |
| Agent `submit_cloud_changes` tool (§9.1 residual) | **No tool in v1.** Badge updates at every turn-end stage push; honesty copy instructs the agent to tell the user in chat when work is ready to review. |
| Stage-push transport | **Orchestrator pulls.** Agent's turn-end hook calls a tiny internal endpoint; the orchestrator SSH-streams the upperdir tar from the workspace pod and pushes to S3 (snapshot_service's streaming pattern). No S3 credentials on workspace or agent pods. |
| Review TOCTOU (§9.6) | **Epoch-pinned apply.** Apply carries the reviewed `staged_epoch`; mismatch with the DB row → 409, UI reloads. You apply exactly what you reviewed. |
| Concurrent stagings over one folder (§9.5) | **No lease in v1.** The second apply's external-mods conflict gate catches the drift and surfaces it. |
| Staging history | **Latest-epoch-only.** S3 objects overwritten per push; no epoch history in v1. |
| Default projects | **Excluded in v1.** They mount the user-home Space; the §8.1.3 etag baseline is validated only at project-folder scale. Checkbox hidden for default projects. |

## 3. Scope

**In:** toggle UI + badge, turn-end staging pipeline (internal endpoint → SSH tar stream → S3 + manifest), `DiffSource` protocol with `GiteaDiffSource` (behavior-preserving refactor) and `UpperdirDiffSource`, thread-scoped review/apply/reject/restage endpoints, Cockpit review panel reusing `job-diff-review`, whole-diff apply engine with conflict gate + post-apply refresh, agent honesty prompt copy, migration 0057, and the six Slice B deferrals (§9 below).

**Out (explicitly):** per-file accept/reject; agent-invoked submit tool; mid-session toggle flip; staging history/epoch retention; default-project (user-home) protection; OpenCloud/GDrive/MS365 protected paths; jobs adopting the mount (Phase 2); apply lease for concurrent stagings; retiring the Mode A Gitea pipeline.

## 4. Toggle UI

- Checkbox in `cockpit/src/app/views/session-create/` — label "Protected cloud — agent writes are staged for your review". Shown/enabled only when: the selected project is **not** a default project, its `main_cloud_backend == "nextcloud"`, and the orchestrator reports the feature flag on (`PROTECTED_CLOUD_MODE_ENABLED`, helm `agent.protectedCloudModeEnabled` — dev ON / prod OFF; surfaced to Cockpit via the existing config/capabilities payload the create view already reads, or added to it).
- Sets `ThreadCreateRequest.protected_cloud` (exists since Slice B task B8). **Immutable** for the thread's life; no mid-session flip in v1 (§9.10).
- `persistent-chat` header: "Cloud changes (N)" badge for protected threads — count from `staged_summary`, tooltip shows last-staged timestamp and, for multi-mount threads, **which** mount is protected (the v1 single-protected-mount signal, deferral #5). Click opens the review panel.
- Engage failure surfacing already exists from Slice B (`protected_cloud_error` on the thread); the create view keeps its current behavior (fail-closed: no live fallback).

## 5. Staging pipeline (turn end → S3)

**Trigger.** `_loop_on_turn_complete` (`src/api/persistent_app.py:3431`) fires a fire-and-forget `POST /internal/threads/{thread_id}/cloud-stage` with `X-Internal-Key`. The orchestrator handler: no-op unless the thread is protected with an active `cloud_ro_mounts` row; debounce (one in-flight stage per thread, coalesce bursts); skip when the upperdir is unchanged since the last epoch (cheap remote check — upperdir content signature — before any tar).

**Transport.** Orchestrator SSHes to the workspace pod (existing `ssh_helpers` channel) and streams `tar --xattrs --xattrs-include='*' --acls -C /home/agent-host/.overlay upper` to S3 — same streaming pattern as `snapshot_service.py` capture (never buffer the whole tar in RAM; the restore-side OOM lesson). S3 objects, in the snapshot bucket (`S3_BUCKET`, default `srw-snapshots`, `S3_ENDPOINT` creds — orchestrator-only):

```
cloud-staging/<thread_id>/upper.tar      # full upperdir, overwritten per epoch
cloud-staging/<thread_id>/manifest.json  # derived diff manifest, overwritten per epoch
```

**Manifest derivation (orchestrator-side).** Parsed from the tar stream + the mount's persisted etag baseline:

- char(0,0) device member → `deleted` (whiteout);
- directory member with an overlay opaque xattr (`trusted.overlay.opaque` / `user.overlay.opaque` / `user.fuseoverlayfs.opaque` = `y`) or containing the `.wh..wh..opq` sentinel → opaque dir, **expanded to per-path entries** (the apply engine is per-file): `deleted` for every etag-baseline path under the dir that is not shadowed by a staged member, staged members classified as usual. A dir with no baseline paths under it produces no deletes at all (amendment #2: opaque "deletes" of never-in-lower dirs are no-ops);
- regular file member → `modified` if the path is in the etag baseline, else `added`;
- overlay-reserved names (`.wh..wh..opq`, `.wh.`-prefixed bookkeeping char devices) skipped per `src/services/cloud_overlay/whiteout.py` rules.

The classification rules are shared with `whiteout.py` (stdlib-pure). The plan pins whether the orchestrator image ships `src/services/cloud_overlay/` (podman-verify imports — the orchestrator-image lesson) or gets a tar-walking sibling module sharing the same constants; either way the constants are defined once.

Manifest shape: `{epoch, staged_at, entries: [{path, status, size, binary}], counts: {added, modified, deleted}, skipped: [{path, kind}]}` (skipped = non-regular members WebDAV cannot represent — review finding 2026-07-12). `binary` = null-byte sniff of the first 8 KiB of the member.

**Bookkeeping.** Migration `0057`: four columns on `cloud_ro_mounts` — `etag_baseline JSONB` (the path→etag map from `capture_etag_baseline`, captured at engage and re-captured after each apply; **nothing persists this today** — code exploration 2026-07-12 confirmed `capture_etag_baseline` has no orchestrator persistence caller, so Slice C adds both the column and the engage-time capture), `staged_epoch INTEGER NOT NULL DEFAULT 0` (monotonic — bumped on every successful stage push, apply, and reject), `staged_at TIMESTAMPTZ`, `staged_summary JSONB` (manifest `counts` + content signature only — entry lists live in the S3 manifest, not the DB row). `schema_current.sql` regenerated via `scripts/schema-snapshot.sh app`, never hand-edited.

**Cadence.** Every turn end (debounced) + once at thread teardown (before the workspace snapshot, best-effort) + on-demand via the `restage` endpoint (review panel's Refresh button). Empty upperdir → delete both S3 objects, zero the summary (badge shows 0).

## 6. Review surface (`DiffSource`)

**Protocol** (new module `orchestrator/services/diff_source.py`):

```python
class DiffSource(Protocol):
    async def summary(self) -> DiffSummary            # entries [{path, status, binary}], epoch/baseline identity
    async def file(self, path: str) -> DiffFile       # old_content: str|None, new_content: str|None,
                                                      # old_binary/new_binary flags, sizes
```

- `GiteaDiffSource`: today's Mode A behavior (`_diff_files_by_tree` + Gitea content reads) extracted behavior-preserving; the existing `/api/jobs/{job_id}/diff*` endpoints (`orchestrator/main.py:14048/14086`) route through it unchanged.
- `UpperdirDiffSource`: `summary()` from `manifest.json`; `file()` new bytes from the `upper.tar` member, old bytes from a live cloud GET via the project's `MainCloudBackend` (agent-service creds, orchestrator-side — old-side TOCTOU is acceptable; the apply conflict gate is the guarantee). Binary members (either side) return content `None` + the binary flag; the UI renders "binary file (N bytes)" without a Monaco diff. Apply remains byte-true — protected mode drops Mode A's text-only limitation.
- The `projects/<slug>/` path-prefix assumption is Gitea-source-specific and stays inside `GiteaDiffSource`; `UpperdirDiffSource` paths are already folder-relative.

**Endpoints** (thread-scoped, session owner + admin):

```
GET  /api/agents/threads/{thread_id}/cloud-diff              # summary + epoch + staged_at + conflict preview
GET  /api/agents/threads/{thread_id}/cloud-diff/{path:path}  # per-file old/new
POST /api/agents/threads/{thread_id}/cloud-diff/apply        # body: {epoch}
POST /api/agents/threads/{thread_id}/cloud-diff/reject       # body: {epoch}
POST /api/agents/threads/{thread_id}/cloud-diff/restage      # manual refresh (pod alive only)
```

**Cockpit.** Reuse the `job-diff-review` component (`cockpit/src/app/views/job-diff-review/` — `JobDiffFileEntry`/`JobDiffFile` types are already source-agnostic; `baseline_commit`/`head_commit` become optional metadata). The session panel + badge get their own scss (the 32 kB `persistent-chat.scss` budget stays untouched). Whole-diff Accept/Reject buttons mirror the job review's all-or-nothing contract.

## 7. Apply / reject (whole-diff, epoch-pinned)

**Preconditions.** Request `epoch` must equal the row's `staged_epoch` (else 409 `epoch_stale`, UI reloads the diff). Conflict gate: `detect_external_mods` (`orchestrator/services/job_cloud_baseline.py:495`) generalized so the baseline source can be a thread's `cloud_ro_mounts.etag_baseline` instead of job context — same divergence kinds (`etag_mismatch` / `missing_at_cloud` / `unexpected_at_cloud`), scoped to the diff's touched paths, same surfacing contract as Mode A's accept endpoint (the plan pins Mode A's exact current block/force behavior at `orchestrator/main.py:14237` and mirrors it).

**Engine** (generalized `apply_diff_to_cloud` pulling bytes from a `DiffSource` instead of Gitea):

- Order: **deletes before creates** (whiteout-before-create, §9.8 — handles opaque-dir renames).
- Sequential, fail-soft, idempotent: PUT overwrites, DELETE `if_exists=True`; parents auto-created.
- **Partial failure:** upperdir + S3 epoch retained, per-file errors returned (`{applied, deleted, errors[]}` like Mode A); retry = re-apply the same epoch (idempotency makes that safe). No "partially applied" DB state — the staged set simply remains staged until a fully clean apply.
- **Full success:** (1) clear the upperdir **via the agent process** — the overlay scripts live in the agent-side `OverlayMountManager`, so the orchestrator calls a small agent-app endpoint (`POST /cloud-overlay/reset` on `persistent_app`, reached the same way the SSE proxy reaches the agent pod) which unmounts the overlay (`fusermount3 -u`), wipes `upper` + `work` **and creates a fresh workdir for the new epoch** (deferral #2 lands here), refreshes the lower, and remounts; (2) re-capture the etag baseline (`capture_etag_baseline`) and persist it; (3) `rclone rc vfs/refresh recursive=true` so the merged view equals the just-applied cloud; (4) delete the S3 epoch objects, zero `staged_summary`, increment `staged_epoch`. Pod already dead → steps run S3/DB-only (apply from S3 with orchestrator creds — "review at your leisure" holds; the mount is gone, nothing to refresh).

**Reject:** same epoch pin; discard = clear upperdir + fresh workdir (pod alive), delete S3 objects, zero summary, increment epoch. Cloud untouched; baseline left as-is.

## 8. Agent honesty

Conditional prompt block injected via the orchestrator-resolved config overlay (`config_resolver.py` path, same mechanism as other resolved prompt fragments) when the thread is protected, appended to the `Workspace:` section of `config/prompts/systemprompt_interactive.txt` and its per-family variants:

> Your cloud folder is in **protected mode**: everything you write there is **staged for the user's review** — it is NOT visible to anyone else and has NOT been saved to the cloud until the user approves it in the review panel. Say "staged for your review", never "saved to your cloud". When a piece of work is ready, tell the user so they can review and apply it.

Slice B's delete-guard and quota-guard messages are already mode-honest (LIVE vs STAGED); no tool-copy change needed beyond the prompt.

## 9. Hardening — the six Slice B deferrals (plan MUST carry each as a task)

1. **ENOTCONN monitor wiring:** the existing `OverlayMountManager.health_check`/`heal` methods get a production caller — periodic health probe on the session's mount-keepalive path; heal on ENOTCONN requires a new `RcloneMountManager.restart_mount`.
2. **Fresh-workdir-per-epoch:** folded into apply/reject clearing (§7) AND the heal/lazy-remount path (never remount an overlay onto a workdir a previous instance may still hold — §11.2 dual-instance hazard).
3. **Quota-guard read alignment:** the shell upperdir-quota guard currently blocks *reads* at cap; align with the write-only rationale (reads always allowed at cap).
4. **`grant.reader_id` into engage `http_client_factory`:** stop re-deriving the reader identity inside the factory; pass it from the grant (naming currently duplicated).
5. **Multi-mount signal:** protected threads with multiple cloud mounts protect only the first NC mount in v1 — the badge tooltip (§4) names it, and the engage path records which mount is protected.
6. ~~Replica-test re-pins~~ — **struck 2026-07-12**: verified against the Slice B plan's fix-wave section; the deferral list has exactly five items and no replica-test item exists (it was a memory-index artifact). The only test re-pins Slice B called for (snapshot `EXTRACT_REMOTE_CMD` assertions) were done inside Slice B itself.

## 10. Security invariants (unchanged, restated as acceptance criteria)

- A protected thread NEVER reaches a live cloud write path: apply writes happen orchestrator-side with orchestrator-held creds, only on explicit user action.
- No S3 credentials on workspace or agent pods; the stage trigger endpoint takes `X-Internal-Key` and a thread id, and stages only that thread's own mount.
- The review/apply endpoints authorize as session owner (or admin); the agent cannot call them (no grant, and they live outside the internal-key surface).
- The RO reader credential in the workspace still grants nothing beyond RO on that one folder (Slices A/B invariant; nothing here widens it).
- Fail-closed posture inherited: engage failure = no cloud dir at all, never a live fallback.

## 11. Failure posture

- **Stage push fails** (SSH dead, S3 down, tar error): never blocks the agent's turn — log, keep the previous epoch, badge shows staleness via `staged_at`; next turn retries. The teardown snapshot remains the durable backstop (upperdir is inside snapshot scope since Slice B).
- **Apply partial failure:** §7 — staged set persists, errors listed, retry safe.
- **Conflict detected:** apply blocked with the diverged-path list (Mode A contract); user re-stages/reviews or rejects.
- **Pod death mid-review:** diff + apply served from S3; workspace-side steps skipped.
- **Reconciler interplay:** the Slice A RO-grant reconciler revokes ended threads' grants — it must NOT delete `cloud-staging/` S3 objects or the staged summary; staged-but-unreviewed diffs of ended threads stay reviewable. (S3 lifecycle/retention for abandoned stagings is out of scope for v1.)

## 12. Testing

CI (Py3.12, no /dev/fuse, no live cloud, all mocked — the standing gate):

- Manifest derivation from **synthetic tars** (whiteout char devices, opaque-dir xattrs via pax headers, never-in-lower opaque no-op, binary sniff, reserved-name skips).
- `DiffSource` contract tests run against both implementations (same assertions, two fixtures).
- Apply/reject engine: httpx MockTransport WebDAV fakes + the snapshot-service S3 fake pattern; epoch-409, conflict gate, deletes-before-creates order, partial-failure retention, full-success sequence (clear script text asserted via the established `FakeRemoteBackend` pattern).
- Endpoint auth tests (owner/admin/agent-denied; internal-key on the stage trigger).
- Cockpit vitest: checkbox gating (backend/flag/default-project), badge count + staleness, panel rendering incl. binary entries.
- Live k3d gate (manual, end of slice): create protected session → agent writes → badge N → review in Cockpit → Apply → bytes verified in NC + upperdir cleared + baseline re-captured → second run Reject → post-pod-death review/apply.

## 13. Rollout

Behind the existing `PROTECTED_CLOUD_MODE_ENABLED` flag (dev ON / prod OFF). Prod stays OFF until prod Nextcloud's groupfolders is on a patched branch (the `GROUPFOLDERS_PATCHED` floor enforces it regardless). No new helm values beyond what Slice B added; the S3 prefix rides the existing snapshot bucket. Mode A jobs unaffected (GiteaDiffSource refactor is behavior-preserving; its existing endpoint tests are the regression net).
