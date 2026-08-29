# Reclaim-on-idle + verifiable-capture — dev-cluster validation runbook

**Purpose.** Validate the workspace-durability work (F1, C1a–d, C2, C3, reclaim-on-idle) on the **dev
cluster**, focused on the one path that could NOT be exercised end-to-end on local k3d: the full
**reclaim-on-idle cycle** (idle-suspend → snapshot verify → PVC reclaim → resume → **extract from S3**).
Everything else is already validated on k3d (see `knowledge-base/knowledge/features/workspace_durability_tiering.md` §Shipped);
this runbook re-confirms the deploy and drives the destructive-but-fail-safe cycle safely.

> **Dev E2E status (2026-08-08).** This runbook was run manually against the dev cluster and it did its job
> — it caught **two blocking bugs** that k3d missed (both involve `tar` as the unprivileged `agent-host`
> user over a real ext4 PVC), now fixed in commit **`91f68129`** (tests included):
> 1. **Capture** failed rc=2 on the ext4 `lost+found` at the PVC root → fixed by `--exclude=*/lost+found`.
> 2. **Pod restore** failed rc=2 extracting root-owned `/usr/local` as `agent-host` → fixed by using the
>    home-only `EXTRACT_HOME_REMOTE_CMD` for pod (non-VM) restores.
>
> **Phase 2 automated — PASSED (2026-08-08, image `sha-5086d48`).** With the fixes deployed, the full
> integrated cycle was driven on a sandbox session over a real ext4 PVC: capture succeeded past `lost+found`
> (Fix 1) → snapshot verified → **PVC reclaimed** (`volume_reclaimed=True`) → resume re-provisioned a fresh
> PVC and **extracted from S3** (Fix 2) → seeded content round-tripped byte-for-byte. Reclaim-on-idle works
> end to end; enabling it is now just the operator flag `WORKSPACE_RECLAIM_ON_IDLE=true`.
>
> Two gotchas worth keeping: (1) new dev sessions default to the **`virtual` (lite) tier** — no workspace
> PVC, *not* subject to reclaim; create a **sandbox**-backend session
> (`config_override.workspace.backend = "sandbox"` via `POST /api/persistent/threads`) to exercise this
> path. (2) To fire the integrated reclaim branch without arming the cluster-wide flag (which would also
> expose other idle sessions to the sweeper), run `suspend_thread_workspace` in an **isolated in-pod
> process** that sets `os.environ["WORKSPACE_RECLAIM_ON_IDLE"]="true"` and replicates the startup
> `.connect()` wiring — the live orchestrator keeps the flag off, so only the test session is affected.

**Feature under test.** `WORKSPACE_RECLAIM_ON_IDLE` (default **off**): on idle-suspend of a session, once
`verify_snapshot` confirms the S3 archive is restorable, the workspace PVC (`pvc-ws-thread-<id>`) is
deleted; on the next touch, restore extracts from S3 (no reattach). Design + commit list:
`knowledge-base/knowledge/features/workspace_durability_tiering.md`.

**Environment.** Dev cluster, context `main`, namespace `superhuman-remote-worker` (adjust if different).
Prereqs: the durability commits deployed (image built from `develop`), `WORKSPACE_PVC_ENABLED=true`,
`WORKSPACE_STORAGE_CLASS=longhorn-ephemeral` (single-replica — reclaim deletes a real Longhorn volume).

```sh
export KCTX="--context=k3d-srw"; export NS="-n srw"   # substitute your own context/namespace
ORCH=$(kubectl $KCTX $NS get pods -l app=srw-orchestrator -o jsonpath='{.items[0].metadata.name}')
```

**The one real risk to watch.** Reclaim is verify-gated, so it will not delete a PVC unless a
confirmed-good snapshot exists (data moves to S3, it is not destroyed). The residual risk is the session
**extract-from-S3-after-reclaim** path — under retain-on-idle today, sessions *reattach* their PVC on
resume, so this restore branch is job-tested but rarely fires for sessions live. Phase 2 exercises it on a
**throwaway test session** before any broad exposure. **Rollback is instant:** set
`WORKSPACE_RECLAIM_ON_IDLE=false` and redeploy → new suspends keep the PVC (already-reclaimed sessions are
unaffected — their data is in S3).

---

## Phase 0 — confirm the deploy (flag OFF, zero behavior change)

The image layout maps `orchestrator/` → `/app`, `src/` → `/app/src`.

```sh
# 0.1 all four changes present in the running image
kubectl $KCTX $NS exec $ORCH -c orchestrator -- sh -c '
  grep -c _ensure_checkout_path_ignored /app/src/tools/orchestrator/repositories.py   # F1  -> 2
  grep -c PIPESTATUS                      /app/services/snapshot_service.py            # C1b -> >0
  grep -c "set -o pipefail"               /app/services/ssh_helpers.py                 # C1c -> 3
  grep -c "bash -c" /app/services/ide_settings.py                                      # C1d -> >0
  grep -c "def verify_snapshot"           /app/services/snapshot_service.py            # C2  -> 1
  grep -c _reclaim_on_idle_enabled        /app/services/workspace_suspension.py        # reclaim -> >0
  grep -c "staging-" /app/services/snapshot_service.py'                                # C3  -> >0

# 0.2 SAFETY: flag must be OFF by default
kubectl $KCTX $NS exec $ORCH -c orchestrator -- sh -c 'echo "RECLAIM=[${WORKSPACE_RECLAIM_ON_IDLE:-<unset>}]"'   # expect false/<unset>
```
**PASS** = all markers present AND `WORKSPACE_RECLAIM_ON_IDLE` is false/unset. Leaving it here is a
no-behavior-change deploy — safe indefinitely.

---

## Phase 1 — non-destructive checks (flag still OFF)

### 1a. `verify_snapshot` against the dev cluster's real S3
Deletes/creates only throwaway `threads/verifytest-*` keys.
```sh
kubectl $KCTX $NS exec -i $ORCH -c orchestrator -- python - <<'PY'
import asyncio, hashlib, json, sys; sys.path.insert(0, "/app")
from services.snapshot_service import SnapshotService
async def main():
    svc = SnapshotService(); await svc.connect(None)
    assert svc._available, "S3 unavailable"
    s3, b = svc._s3, svc._bucket; tid="verifytest-dev-1"; p=f"threads/{tid}"; k=f"{p}/env.tar.zst"
    blob=b"HELLO-"*2000; sha=hashlib.sha256(blob).hexdigest(); sz=len(blob)
    mk=lambda s,z: s3.put_object(Bucket=b, Key=f"{p}/manifest.json", Body=json.dumps({"checksum_sha256":s,"size_compressed_bytes":z}).encode())
    s3.put_object(Bucket=b, Key=k, Body=blob); mk(sha, sz)
    print("GOOD    ->", await svc.verify_snapshot(tid, entity_type="threads"))   # (True,'ok')
    mk("de"*32, sz);  print("BAD_SHA ->", await svc.verify_snapshot(tid, entity_type="threads"))  # (False,'sha256 mismatch')
    mk(sha, sz+9);    print("BAD_SIZE->", await svc.verify_snapshot(tid, entity_type="threads"))  # (False,'size mismatch...')
    mk(sha, sz); s3.delete_object(Bucket=b, Key=k)
    print("MISSING ->", await svc.verify_snapshot(tid, entity_type="threads"))   # (False,'object missing')
    for o in s3.list_objects_v2(Bucket=b, Prefix=p).get("Contents", []): s3.delete_object(Bucket=b, Key=o["Key"])
    print("cleaned")
asyncio.run(main())
PY
```
**PASS** = GOOD→`(True,'ok')`, others→`(False, <reason>)`.

### 1b. flag-OFF no-op (regression guarantee)
Suspend an existing/throwaway ENDED session (idle-sweeper or explicit suspend) and confirm its
`pvc-ws-thread-<id>` **still exists** afterward (retain-on-idle preserved). If you can't easily drive a
suspend, this is also covered by the unit tests; the k3d review verified flag-off is byte-identical to
today.

---

## Phase 2 — arm + validate the reclaim cycle (the load-bearing test)

### 2.1 Arm the flag
Set it in the dev overlay and redeploy (GitOps), or patch for a quick soak:
```sh
# GitOps (durable): set workspace.reclaimOnIdle: true in deployment/values-experimental.yaml, push, sync.
# OR quick/manual (revertible): patch the configmap + restart, then remember to revert.
```
Confirm live: `kubectl $KCTX $NS exec $ORCH -c orchestrator -- sh -c 'echo $WORKSPACE_RECLAIM_ON_IDLE'` → `true`.

### 2.2 Create a THROWAWAY test session with distinctive content
Use a NEW session (never a pilot's). Give it a shell workspace and seed:
- a **tracked** file, committed + pushed (so it's in the thread Gitea repo), e.g. `report.md`;
- a **gitignored real-work** file the snapshot must carry, e.g. `scratch.db` (or under a non-`repos/`
  path) — this is the file git can't restore, so it proves the snapshot round-trips.
Record the thread id `TID` and note its `pvc-ws-thread-${TID:0:12}` claim exists:
```sh
kubectl $KCTX $NS get pvc | grep "pvc-ws-thread-${TID:0:12}"   # present before suspend
```

### 2.3 Trigger idle-suspend
Let the session go idle past `WORKSPACE_IDLE_TIMEOUT` (the sweeper suspends `ended` threads), or drive an
explicit suspend. Then verify the RECLAIM fired:
```sh
kubectl $KCTX $NS get pvc | grep "pvc-ws-thread-${TID:0:12}" || echo "PVC RECLAIMED (expected)"
kubectl $KCTX $NS logs $ORCH -c orchestrator | grep -i "reclaim-on-idle" | tail   # "snapshot verified, PVC reclaimed"
# thread workspace context should show volume_reclaimed:true and status:suspended (DB / MCP get_workspace_overview)
```
**PASS** = `pvc-ws-thread-*` gone, log shows "snapshot verified, PVC reclaimed", status `suspended`.
(If the log instead says "snapshot unverified … keeping PVC", the PVC is intentionally retained — investigate
the capture, but that is the fail-safe working, not a failure of the feature.)

### 2.4 Resume and prove the S3 restore round-trips (THE key assertion)
Touch/resume the test session (send it a message). Then:
```sh
kubectl $KCTX $NS get pvc | grep "pvc-ws-thread-${TID:0:12}"    # a FRESH claim exists again
kubectl $KCTX $NS logs $ORCH -c orchestrator | grep -iE "restore|extract" | tail  # extract-from-S3 path, NOT reattach
```
In the resumed workspace, confirm **both** files are back byte-for-byte: the tracked `report.md` AND the
**gitignored `scratch.db`** (the latter proves the snapshot restored data git alone could not).
**PASS** = fresh PVC + restore via extract + `scratch.db` intact.

### 2.5 Cleanup
Delete the test thread (permanent) so its PVC + snapshot are GC'd. Decide: leave the flag on for a soak
(Phase 3) or set it back off.

---

## Phase 3 — soak + rollback (optional, once 2.4 passes)

Leave `WORKSPACE_RECLAIM_ON_IDLE=true` and monitor over a day:
```sh
kubectl $KCTX $NS get pvc | grep -c pvc-ws-thread    # should stop growing / trend down as idle sessions reclaim
kubectl $KCTX $NS logs $ORCH -c orchestrator | grep -iE "reclaim-on-idle|snapshot restore FAILED|extract"
```
Watch for: any resumed session that restored empty/failed (the risk); any "keeping PVC" (fail-safe, benign).
**Rollback anytime:** set `WORKSPACE_RECLAIM_ON_IDLE=false` + redeploy → new suspends keep the PVC.

---

## Success criteria (summary)
1. Phase 0: all markers deployed; flag off by default.
2. Phase 1a: `verify_snapshot` accepts good / rejects sha·size·missing against real dev S3.
3. Phase 2.3: idle test session → snapshot **verified** → `pvc-ws-thread-*` **deleted**.
4. Phase 2.4: resume → fresh PVC → **extract from S3** → tracked **and gitignored** content intact.
5. No session loses data; verify-fail keeps the PVC; flag-off rollback is instant.

## Known non-blocking gaps (from the shipped work; see design doc §Shipped)
- Session **agent** PVC (`pvc-agent-s-*`) is NOT reclaimed (AgentProvisioner has no delete method) — bounded
  by the terminal reaper; a follow-up should add it.
- `volume_reclaimed` context marker is write-only (nothing reads it; benign).
- `_soft_delete_snapshot` still uses single-part `copy_object` on `env.tar.zst` (>5 GB cap; pre-existing).
- F1 is preventive; threads whose repo already holds a committed gitlink need a one-time cleanup migration.
