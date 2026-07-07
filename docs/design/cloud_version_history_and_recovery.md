---
tags:
  - agent-architecture
  - cloud
  - security
  - design
  - data-recovery
related:
  - "[[agent_action_reversibility]]"
  - "[[cloud_access_unification]]"
  - "[[rclone_cloud_mount]]"
  - "[[main_cloud_abstraction]]"
  - "[[cloud_collaboration_model]]"
  - "[[job_cloud_export]]"
  - "[[vm_snapshots_and_ide]]"
  - "[[cloud_storage_alternatives]]"
  - "[[sudo_approval_gate]]"
aliases:
  - "Cloud Version History"
  - "Cloud Data Recovery"
  - "Agent-Mistake Recovery"
  - "Versioning in Place"
---

# Cloud Data Version History & Agent-Mistake Recovery — design doc

**Status:** brainstorm IN PROGRESS — **paused** pending one decision (§9). Started 2026-06-26, captured 2026-07-07.
**Scope:** the concrete recovery design for the *user's cloud namespace* — i.e. the implementation of [[agent_action_reversibility]] §6, and a resolution of its open questions **#3** (regret → restore UX) and **#4** (where snapshot-before-write lives, un-bypassably). This doc is NOT catastrophe/DR and NOT disclosure/exfiltration — those are separate (§8).

---

## Why this doc exists

The owner won't let an autonomous agent loose on 2–5 TB of personal/family data until there's an "undo any agent action, ever" guarantee. That guarantee is the precondition for daily-driving SRW on real data. This is the reversibility principle ([[agent_action_reversibility]]) made concrete for the cloud slice.

Personal framing that shaped the requirements: extreme data-loss aversion; the realistic worry is a well-meaning agent mangling/deleting files during an autonomous reorg (e.g. "dedupe my notes"), **not** a malicious agent. Storage is not a constraint (≈40 TB usable on garage; the churning set is almost all text and cheap to keep forever). The *source of truth with history* is the thing that's missing.

## 1. Threat model & scope (v1)

- **In scope (v1):** honest agent + *misunderstanding* → unwanted modification/deletion of cloud files. Design for the tail — the worst 1% — per [[agent_action_reversibility]] §2, not the average.
- **Explicitly NOT v1:** a *rogue* agent actively evading capture or deleting the backup store. Deferred hardening (Object Lock — §7).
- **Notes are already solved out-of-band** (Obsidian + a git repo). This doc is about *the rest* of the data (photos, docs, archives) — ~95% static; the 5% that churns is text.

## 2. Verified architecture — how the agent actually touches cloud files

Grounds the whole design. **Verified 2026-06-26 by code sweep** (corrects an earlier, now-stale note that claimed `cloud_*` WebDAV tools):

- **No `cloud_*` tools exist.** `src/tools/cloud/` is empty. There is nothing to "hook" at a dedicated cloud tool.
- **Sessions** mount the cloud as an **rclone FUSE folder at `/workspace/cloud/*`** ([[rclone_cloud_mount]]; `src/services/cloud_mount/`, mounted in `src/api/persistent_session.py`). The agent edits it with the **standard file tools** (`read_file`/`write_file`/`edit_file` in `src/tools/workspace/files.py`) **and the shell** — writes go **live** to Nextcloud/OpenCloud through rclone. Auth is a **Keycloak bearer / WebDAV token to the cloud** — the agent never holds the MinIO S3 key.
- **Sync fallback** (WebDAV pull-before-turn / push-after-turn) applies **only when the mount is inactive**. Not viable for large scopes (can't pull TBs into the workspace) → the live mount is mandatory for "sort my whole cloud."
- **Jobs** don't mount the cloud; they use the Mode A baseline/diff/accept ([[job_cloud_export]]).

**Consequence (resolves [[agent_action_reversibility]] open-Q4):** a live mount + a shell means *any* in-SRW hook (tool wrapper, datasource adapter, workspace backend) is bypassable by a shell command (`rm`, `>`, `sed -i`, or a direct `curl`). The only un-bypassable capture point is **below the mount, at the object store** — where the agent has no credentials.

## 3. Options considered & rejected (so we don't re-litigate)

| Option | Verdict | Why |
|---|---|---|
| Git monorepo of the cloud | ✗ | Scale + binaries; git handles many-file/large/binary namespaces poorly. ([[agent_action_reversibility]] §6 already says: don't mirror.) |
| SRW-native byte store (intercept cloud write tools) | ✗ | No `cloud_*` tools to intercept; would itself be a mirror; misses shell + non-SRW paths. |
| rclone live hook / plugin | ✗ | rclone has **no** loadable-plugin / op-hook system (compile-time backends only). `--backup-dir`+`--suffix` work on *sync/copy/move*, **not** a live mount. A custom versioning wrapper backend = a maintained rclone fork (community-requested, unbuilt). |
| Orchestrator full before/after-job diff | ✗ (as primary) | The "copy 500 GB per job" fear was a **misread** — snapshot tools are incremental (genesis once, then deltas; never a re-copy). But a full-tree walk over WebDAV per boundary is still costly and doesn't fit the live-mount session model. |
| restic/kopia snapshot **mirror** | ✗ (as the versioning layer) | It's a **second ~full copy** of the cloud — redundant (owner's key insight: "we'd be mirroring the cloud"). Retained ONLY for the separate **off-site DR** copy (§8), where a second copy legitimately belongs. |
| **S3 versioning in-place on Nextcloud's bucket + thin restore UI** | ✓ **converged** | See §4–5. |

## 4. Converged design

**Version in place; don't mirror.** Enable **S3 versioning + a noncurrent-version lifecycle** on the MinIO bucket that already backs Nextcloud. Then:

- One store + only the deltas (95% static → tiny overhead), *not* a second copy.
- Every write through the mount — **tool or shell** — is versioned automatically, below the agent, with zero interception.
- Deletions become S3 **delete-markers** with the prior version retained → the long-tail "deleted in 2023, noticed in 2026" case is covered. (Native Nextcloud versions + trash are **not** enough — both expire and both live inside the live system. **This is the refinement to [[agent_action_reversibility]] §6**, which had said "lean on native versions + trash.")

**The SRW build shrinks to the restore UX** — the "regret → restore" of [[agent_action_reversibility]] open-Q3:

- A **"browse versions / restore by path"** view (in Cockpit, or a simple standalone UI — this *is* the owner's "S3 + a simple UI," just pointed at Nextcloud's bucket instead of replacing Nextcloud).
- **Provenance:** tie a restore point to the audited agent action that produced it (the audit trail already exists).

## 5. Why this design

- **No duplication.** Versioning-in-place = 1 copy + deltas; a restic mirror = 2 copies + deltas.
- **Trust boundary is intrinsic, for free.** The agent mounts *Nextcloud* (bearer/WebDAV), not MinIO. It never holds the S3 key, so it **cannot disable versioning or purge versions** — the recovery layer sits one level *below* where the agent operates. This alone covers the honest-agent threat and answers open-Q4 ("impossible for a tool to bypass it").
- **Don't rebuild Nextcloud.** "S3 + simple UI *as the actual cloud*" throws away sync clients, mobile apps, sharing, and document editing — the whole reason for leaving Proton, and needed for *family* data. Nextcloud already runs in the homelab; this is a bucket setting, not a migration. (Same "someone already built this" logic the owner applied to git-for-clouds; also respects the feature-freeze / ship-and-earn posture.)

## 6. The real (small) work — two caveats

1. **Opaque object keys.** Nextcloud stores S3 objects as `urn:oid:<fileid>`, so S3 versions are **not path-browsable** at the bucket layer. The restore UI must map **path → fileid → S3 version** via Nextcloud's DB / `occ`. *This mapping is the bulk of the SRW build.*
2. **Object Lock — deferred.** Not needed for v1 (the agent has no S3 key anyway). It's the *rogue-agent* hardening, and it may conflict with Nextcloud's own object housekeeping (delete / version-expiry). Defer it, and its compatibility question, until the rogue case is in scope.

## 7. Phasing

- **v1:** bucket versioning + lifecycle (ops) + a basic path→version restore (script or minimal UI). Makes daily-driving SRW safe.
- **v2:** the nice Cockpit "browse / restore + provenance" UX; per-action grouping via the audit trail.
- **Deferred:** Object Lock / rogue-agent hardening; the DR workstream (§8).

## 8. Explicitly separate — do NOT fold in

- **Catastrophe / DR.** Versioning-in-place lives in the *same* MinIO, so it dies with that cluster. The off-site copy is the homelab DR chain (headscale HA → garage `remote` zone → Longhorn-to-garage) — and that copy also backs up the versions. A periodic **restic to off-site is the right tool _here_**, not as the versioning layer.
- **Key-escrow / LUKS / test-restores.** "A backup you've never restored isn't a backup." Its own small task.
- **Disclosure / exfiltration.** Undo does not reach it ([[agent_action_reversibility]] §3 last row, §6). Out of scope.

## 9. OPEN QUESTION (the blocker) — resolve to resume

**Is the AI operating on the _same_ data the owner daily-drives from phone/clients, or a _separable_ corpus?**

- **Same data** (expected, per the Proton→Nextcloud plan): keep Nextcloud, version its bucket in place, build the restore UI. ← **strong recommendation.**
- **Separable corpus** (never needs sync/sharing/mobile): a standalone versioned-S3 store + simple UI for *that corpus* becomes reasonable, with Nextcloud staying for personal use.

## 10. Next steps (when we resume)

1. Answer §9.
2. Verify in the homelab: does enabling versioning + lifecycle on the Nextcloud MinIO bucket behave cleanly? Confirm the `urn:oid:<fileid>` scheme and how to resolve path → fileid via `occ` / the Nextcloud DB.
3. Then: implementation spec/plan for v1 → hand to writing-plans.

## References

**Internal:** [[agent_action_reversibility]] (parent principle — this doc is its §6 + resolves Q3/Q4), [[rclone_cloud_mount]], [[main_cloud_abstraction]], [[cloud_collaboration_model]], [[job_cloud_export]], [[vm_snapshots_and_ide]], [[cloud_storage_alternatives]], [[sudo_approval_gate]].

**External anchors:**
- rclone `--backup-dir` / `--suffix` — versioned sync (moves the about-to-be-overwritten/deleted file aside first); applies to *sync/copy/move*, not a live FUSE mount.
- rclone has **no** op-interception plugin system (backends are compile-time; a versioning wrapper backend has been requested and remains unbuilt).
- rclone `--s3-versions` — read/restore old object versions (makes rclone a good *restore* tool over a versioned bucket).
- S3 **bucket versioning** + `NonCurrentVersionExpiration` lifecycle + **Object Lock** (supported by MinIO) — the "keep every version, deletions recoverable, tunable retention" primitive this design leans on.
