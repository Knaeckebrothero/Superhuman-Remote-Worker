# Open IDE on a reviewed job routes pod snapshots to a KubeVirt VM restore — which fails 100% of the time (120s wait vs ~3.5min boot, no orchestrator→tailnet route, reaper race) while the cockpit swallows the error; fix: restore snapshots into the in-cluster IDE pod

**Status:** IMPLEMENTED 2026-07-11 (all of P0+P1+P2), commits `c63e1d2b`
(P0 snapshot→IDE-pod routing + gitea clone port fix + P1 cockpit error
surfacing/poll cap) and `49aac739` (P2 reaper exemption via
`ide_session_status` metadata, 420s VM wait, topology gate) + a review pass
(uncommitted at time of writing): snapshot→gitea fallback resets the session
to `restoring` so the cockpit poll doesn't report a transient `failed`;
`get_session_status` maps a session-level `unavailable` (topology verdict)
to a terminal error response instead of falling through to `available` and
re-offering a doomed retry; `start_session` propagates the topology error in
its idempotent return; ruff format. Unit-verified: 95 tests green in
`tests/test_ide_session.py` + `tests/test_lifecycle_vm_manager.py`, ruff
clean. **Live k3d/dev smoke NOT yet run** — the verification plan below is
the remaining gate; re-test "Open IDE" on job `7e45c299` (or any
pod-snapshot pending_review job) after the next dev rollout.
**Review notes:** the daemon-side `code_server_connections` heartbeat field
is still aspirational (`management-daemon.py` never sends it), so the
P2 reaper exemption cannot be triggered by phantom heartbeat-written
session statuses today — but if the daemon ever starts reporting it, the
heartbeat handler (`nats_bridge.py:716-728`) must first be guarded (see
"Contributing smell" below) or every heartbeating VM becomes unreapable.
**Motivating incident:** user clicked "Open IDE" on job
`7e45c299-435c-4fff-b9ef-f7706e7ce0d4` (pending_review since 2026-07-10, its
workspace pod long since reaped). Cockpit spinner ran ~2 minutes, then stopped
with no error, no tab, nothing. Same outcome earlier the same morning on job
`5e4c117a-a9ba-4f9f-b056-35149685ddd5`. Dev cluster `main` / namespace
`superhuman-remote-worker`, orchestrator replicas
`srw-orchestrator-67858fb7d7-{rtm2l,s9xhv}`.

## TL;DR

When a reviewed job's workspace is gone but an S3 environment snapshot exists,
`IdeSessionService.start_session` routes **every** snapshot restore to the
full KubeVirt-VM path (`ide_session.py:224` keys only on
`snapshot.status == "available"`; `_restore_session` at `ide_session.py:408`
sends only `source == "gitea"` to the container path). This is wrong for
`source_type: "pod"` snapshots — jobs that ran on in-cluster workspace pods —
and on the dev topology the VM path cannot succeed **at all**, for three
independent reasons:

1. **Timeout vs reality:** `_wait_for_vm_ready(job_id, timeout=120)`
   (`ide_session.py:475`) gives up after 2 minutes; VM boot on this infra
   takes ~3.5 min (golden-image path; measured 3m17s in the incident).
   Guaranteed `"VM did not become ready within timeout"`.
2. **No route anyway:** the orchestrator pod has no route to tailnet VMs
   (known issue, `vm_ssh_readiness_probe_unroutable_from_orchestrator.md`;
   the orchestrator itself logs "no route to tailnet targets" when it skips
   IDE config seeding, `nats_bridge.py:644`). Snapshot extraction
   (`_extract_snapshot_to_vm`, SSH **from the orchestrator**) and the IDE
   reverse proxy (also from the orchestrator) would both black-hole even if
   the VM boot won the race.
3. **Reaper race:** the lifecycle reconciler has zero IDE-session awareness.
   `VmInstanceManager.is_reapable` (`vm_manager.py:223`) gates only on
   job/thread status, and an IDE VM is by construction bound to a
   `pending_review`/terminal job — i.e. always reapable. In the incident the
   reaper force-deleted the VM **14 seconds after it finally became ready**
   (dirty + unreachable + snapshot attempts exhausted,
   `reconciler.py:287`). Even a hypothetically reachable, fast-booting IDE
   VM gets torn down out from under an active session on a later tick.

Meanwhile the cockpit swallows the failure: `pollIdeSession` on
`failed`/`unavailable` clears the interval and stops the spinner without
displaying `result.error` (`job-review.component.ts:756-759`; same silent
drop on the start-response branch at `722-727`). Hence "spun a while and
nothing happened."

The DB shows this path has **never worked**: every `vm`/`snapshot` IDE
restore attempt since 2026-06-12 failed with the identical error, while the
`gitea`/`k8s_container` fallback sessions from the same period reached
`active` and later `expired` normally (i.e. the container path works).

**The fix (agreed):** stop detouring pod snapshots through VMs. The
"lightweight IDE pod" already **is** the workspace pod — `create_ide_pod`
(`container_provisioner.py:612`) launches `self._workspace_image` (line 651,
sshd + code-server on 38080 included) with smaller resources and the label
`srw/component: ide-session` (line 661), which is precisely what keeps the
workspace reaper's hands off and hands lifecycle to the IDE TTL sweeper.
Route snapshot restores there and extract the S3 tarball into the pod over
in-cluster SSH instead of git-cloning. The agent was never in the workspace
pod (it SSHes in from its own pod), so "bypassing the agent" costs nothing.

## Evidence (live dev, 2026-07-11)

### Incident timeline, job `7e45c299`

| time (UTC) | event | source |
|---|---|---|
| 07:09:24.600 | `POST /api/jobs/7e45c299/ide` 200 → session `restoring` | rtm2l log |
| 07:09:24.603 | `Published vm.lifecycle.create.srw-dev` | rtm2l log |
| 07:09:24.952 | VM lifecycle status: `created` | both replicas |
| 07:09:27 – 07:11:27 | cockpit polls `GET /ide` every 3s (user's spinner) | rtm2l log |
| ~07:11:24 | `_wait_for_vm_ready` 120s deadline → session `failed: "VM did not become ready within timeout"` | ctx + code |
| 07:12:41.086 | `VM SSH ready … (100.64.24.95:22, evidence: daemon)` — **77s too late** | s9xhv log |
| 07:12:41.086 | `Skipping IDE config seed … orchestrator has no route to tailnet targets` | s9xhv log |
| 07:12:55.161 | `Published vm.lifecycle.delete.srw-dev` | s9xhv log |
| 07:12:55.167 | `Lifecycle reaper force-deleted dirty unreachable instance kind=vm id=agent-vm-7e45c299-… — state not captured (snapshot attempts exhausted)` | s9xhv log |
| 07:12:56.110 | VM lifecycle status: `deleted` | both replicas |

Job context after the incident (`jobs.context`, abridged):

```
vm:           status=deleted, ssh_host=100.64.24.95, registered_at=07:12:41,
              snapshot_attempts=3
ide_session:  status=failed, error="VM did not become ready within timeout",
              source=snapshot, snapshot_type=pod, restore_type=vm,
              started_at=07:09:24, estimated_seconds=60,
              vm_name=ide-7e45c299-435   # cosmetic mismatch: actual VM is
                                         # agent-vm-{full job id}; deletes go
                                         # by job_id so it still works
snapshot:     status=available, source_type=pod,
              size_compressed_bytes=99758950, checksum recorded
```

Note `snapshot_type: pod` — the job ran on an in-cluster workspace pod; the
100MB S3 snapshot is intact. Nothing is lost; the restore route is just
wrong.

### Failure history — the vm/snapshot path has a 100% failure rate

`SELECT … FROM jobs WHERE context ? 'ide_session'` ordered by
`ide_session.started_at`:

| job | started | session status | error | restore_type/source |
|---|---|---|---|---|
| `7e45c299` | 2026-07-11 07:09 | failed | VM did not become ready within timeout | vm / snapshot |
| `5e4c117a` | 2026-07-11 06:13 | failed | VM did not become ready within timeout | vm / snapshot |
| `8384b8a9` | 2026-06-24 | failed | VM did not become ready within timeout | vm / snapshot |
| `cb0a9128` | 2026-06-14 | failed | VM did not become ready within timeout | vm / snapshot |
| `67ab2595` | 2026-06-14 | failed | VM did not become ready within timeout | vm / snapshot |
| `99824850` | 2026-06-12 | failed | VM did not become ready within timeout | vm / snapshot |
| `44574c5d` | 2026-06-12 | failed | VM did not become ready within timeout | vm / snapshot |
| `692f00d5` | 2026-06-10 | **expired** (was active) | — | k8s_container / gitea |
| `12e6da83` | 2026-05-18 | **expired** (was active) | — | k8s_container / gitea |
| `815b803e` | 2026-05-18 | failed | VM provisioner not available | — / snapshot |

Every snapshot→VM attempt: failed. The gitea→pod attempts reached
`active`/`expired`, proving the pod + proxy + TTL-sweeper chain works — but
see "Latent defect in the gitea clone" below: the clone step itself likely
never populated files, so those sessions may have opened empty workspaces.

The `5e4c117a` attempt at 06:13 shows the same create→(timeout)→ready→reap
arc: VM `created` 06:13:23, `deleted` 06:17:28.

## Root cause detail

### 1. Routing: snapshots unconditionally take the VM path

`start_session` (`ide_session.py:224-231`): `snapshot.status == "available"`
→ `source = "snapshot"`, else `repo_name` → `source = "gitea"`.
`_restore_session` (`ide_session.py:408-413`): `gitea` → container path,
**everything else** → `_restore_vm_session`. `source_type` (`"pod"` vs
`"vm"`) is stored in the snapshot ctx and even copied into the session ctx —
but never consulted for routing.

### 2. `_wait_for_vm_ready` timeout is a third of the real boot time

`ide_session.py:475` hardcodes `timeout=120`. Golden-image VM bring-up is
~3.5–5.5 min (see `project_vm_golden_image` work: ~5m20s; this incident:
3m17s to daemon registration). The session is declared failed while the VM
is still mid-boot, every time.

### 3. Orchestrator cannot reach the VM it just built

Even past the timeout, `_extract_snapshot_to_vm` (`ide_session.py:825`)
SSHes from the orchestrator pod to the VM's tailnet address, and
`ide_proxy_http`/`ide_proxy_ws` proxy from the orchestrator to code-server on
the VM. Both are black holes on this topology — the codebase already knows
this (`nats_bridge.py:640-652` skips IDE config seeding for exactly this
reason, referencing
`docs/issues/vm_ssh_readiness_probe_unroutable_from_orchestrator.md`).

### 4. The lifecycle reaper has no concept of IDE sessions

`VmInstanceManager.is_reapable` (`vm_manager.py:223-255`) exempts live
shared children (`has_live_shared_child`, the critic-on-parent's-VM case)
and dispatchable jobs, but has no exemption for
`context.ide_session.status in (restoring, active, idle)`. A VM bound to a
`pending_review` job is in `_REAPABLE_JOB_STATUSES` by design ("genuinely
idle"). Reap flow for a dirty+unreachable instance
(`reconciler.py:254-297`): 3 recorded snapshot attempts, then
`give_up` = force-delete. The IDE VM accumulated its 3 attempts during boot
and was force-deleted 14s after registering.

### 5. Cockpit swallows every failure mode

`openIde` (`job-review.component.ts:701`) and `pollIdeSession` (`:742`)
treat `failed`/`unavailable` as "stop the spinner" (`:722-727`, `:756-759`).
`get_session_status` returns the error string for failed sessions
(`ide_session.py:153-158`) — the UI just never shows it.

### Latent defect in the gitea clone path (fix alongside P0)

`_restore_k8s_ide_container` hand-rolls its SSH argv
(`ide_session.py:614-625`) with **no `-p` flag** — it targets port 22, but
workspace/IDE pods run sshd on **30022**
(`container_provisioner.py:1316` containerPort; `workspace_suspension.py:26-34`
even carries a comment that the port-22 default "silently broke pod
snapshots"). The clone therefore gets connection-refused, `rc != 0` is only
logged as a warning, and the session is marked `active` anyway — code-server
opens an **empty workspace**. This is why the "working" gitea sessions in
the history table prove transport/proxy/lifecycle but not file delivery.
`_clone_gitea_to_vm` (`ide_session.py:883-896`) does pass `-p`, correctly.
The new snapshot-extract step must use `build_agent_ssh_cmd(...)`
(`ssh_helpers.py`, which handles `-p`, `agent-host@`, key resolution) with
port 30022 — and the gitea clone should be switched to the same helper in
the same PR.

### Contributing smell (not load-bearing here)

`_on_daemon_heartbeat` (`nats_bridge.py:716-728`) overwrites
`ide_session.status` to `active`/`idle` from `code_server_connections` on
**any** VM heartbeat, including for sessions currently `failed` or
`restoring` — a second writer to the session state machine. Didn't matter in
this incident (VM died 14s after ready) but will produce confusing states
once VMs live longer. Worth a guard (only promote from
`restoring`/`active`/`idle`, never resurrect `failed`/`expired`).

## Fix design

### P0 — restore pod snapshots into the in-cluster IDE pod (the actual fix)

The IDE pod is already the right runtime: same `WORKSPACE_IMAGE` as job
workspaces (sshd + code-server:38080 in the entrypoint), smaller requests
(250m/512Mi), label `srw/component: ide-session` so the workspace reaper
ignores it and the IDE TTL sweeper (`ide_session_ttl_sweeper`,
`main.py:850`; idle 30min / max 4h) owns teardown. The gitea path proves the
whole chain (pod → clone → proxy → active → expired) works on dev.

Changes, all in `orchestrator/services/ide_session.py`:

1. **Routing** (`start_session` + `_restore_session`): when
   `snapshot.status == "available"` and `source_type == "pod"` and
   `self._container_provisioner.is_available` → new
   `_restore_snapshot_container` path. Keep `_restore_vm_session` only for
   `source_type == "vm"` (see P2 for its remaining problems).
   `estimated_seconds` for the pod path: ~30 (pod ready ~15s + extract
   ~10s for a 100MB tarball).
2. **`_restore_snapshot_container(job_id, job)`** — clone of
   `_restore_k8s_ide_container` with the clone step swapped for snapshot
   extraction:
   - `pod_ip = await self._container_provisioner.create_ide_pod(job_id)`
     (unchanged, including seed-configmap/extension seeding).
   - Download: `await self._snapshot_service.download_snapshot(job_id,
     tmp_path)` (same tempfile pattern as `_extract_snapshot_to_vm`,
     `ide_session.py:834`).
   - Extract: `stream_extract_snapshot(pod_ip, 30022, tmp_path)` — the same
     helper the VM path uses, except the target is an in-cluster pod IP the
     orchestrator *can* reach. **Port is 30022**, not 22 (pod sshd,
     `container_provisioner.py:1316`); the helper's `build_agent_ssh_cmd`
     handles `-p`, `agent-host@`, and key resolution. Extraction is
     `zstd -d | tar -xf - -C /` — absolute paths, symmetric with how
     `workspace_suspension.py` captured the pod, so files land back at
     `/home/agent-host/…` where code-server expects them. Note the helper
     fail-fasts (rc 255) on tailnet targets via `orchestrator_can_reach` —
     harmless here (pod IPs aren't in 100.64.0.0/10) and further proof the
     VM path's extraction was doomed regardless of the boot timeout.
   - Session ctx: `restore_type: "k8s_container"`, `pod_ip`, `status:
     "active"`, `code_server_url` via `_build_code_server_url(job_id)`.
     `stop_session` and the TTL sweeper already handle
     `k8s_container` teardown (`_delete_ide_container` →
     `container_provisioner.delete_ide_pod`).
3. **Fix the gitea clone port** in the same PR (see "Latent defect" above):
   replace the hand-rolled SSH argv in `_restore_k8s_ide_container` with
   `build_agent_ssh_cmd(pod_ip, 30022, clone_cmd)`, and treat a non-zero
   clone rc as session `failed` (not warn-and-mark-active) — an empty
   workspace presented as success is this bug's UX all over again.
4. **Fallback order** when the pod-snapshot restore fails: fall through to
   the gitea path if `repo_name` exists (workspace files minus
   environment beat nothing), else mark failed with the real error.

Optional refinement (separate slice, not required): if the job's workspace
**PVC** still exists (PVCs can outlive pods until the orphan sweep), mount
it into the IDE pod instead of extracting from S3 — exact pre-teardown state,
zero extraction time. S3 stays the durable fallback.

### P1 — cockpit: surface the error

`job-review.component.ts`: on `failed`/`unavailable` (both the
start-response branch and the poll branch), surface `result.error` via the
existing `resultMessage`/`resultIsError` signals (or a toast) instead of
silently stopping the spinner. Also cap `pollIdeSession` at ~5 min so a
stuck `restoring` session doesn't poll forever.

### P2 — the remaining VM leg (only relevant for `source_type == "vm"`)

Ordered by value; all three are needed before the VM restore path can be
called supported on any topology:

1. Exempt VMs with `ide_session.status in (restoring, active, idle)` in
   `VmInstanceManager.is_reapable`/`is_idle` (same shape as
   `has_live_shared_child`), so the reaper stops racing IDE sessions.
2. Raise `_wait_for_vm_ready` to 360–480s and keep the cockpit informed via
   `estimated_seconds`.
3. Topology gate: if the orchestrator can't reach VM targets (probe with
   the existing `orchestrator_can_reach()` / honour
   `ORCHESTRATOR_HAS_TAILNET_ROUTE`, `ssh_helpers.py:50-63`), return
   `{"status": "unavailable", "error": "VM restore not supported on this
   topology"}` up front instead of burning 4 minutes to fail. VM-source
   snapshots on such topologies could alternatively be restored into an IDE
   pod too (the tarball is just files, extracted `-C /`), accepting that the
   VM-only environment (systemd, sudo state) won't be faithful — good
   enough for review/browse.

Until P2 lands, `source_type == "vm"` snapshots on dev keep failing — but
after P0 that no longer affects the common case (pod-backed jobs), and P1
makes any remaining failure visible instead of silent.

## Verification plan (k3d)

1. Run a job to `pending_review` on the k8s workspace backend; wait for (or
   force) workspace teardown with snapshot upload → `context.snapshot =
   {status: available, source_type: pod}`.
2. Cockpit → job review → Open IDE. Expect: spinner ≤ ~30s, new tab with
   code-server showing the restored workspace (check a file written by the
   agent mid-job, i.e. state that isn't in Gitea).
3. `kubectl get pod ide-<jobid12> -o jsonpath='{.metadata.labels}'` →
   `srw/component: ide-session`; confirm the reconciler tick logs no reap
   attempt against it.
4. Failure surfacing: delete the S3 object, click Open IDE → cockpit shows
   the error (no silent stop); session ctx `status: failed` with a real
   message.
5. TTL: leave the session idle past `IDE_SESSION_IDLE_TIMEOUT` → sweeper
   expires it, pod deleted, `status: expired`; clicking Open IDE again
   restores fresh.
6. Gitea path: a gitea-only job (no snapshot) takes the clone path AND the
   cloned files are actually present in code-server (this was silently
   broken before — port 22 vs 30022; don't accept `status: active` alone as
   proof).
7. Regression: `pytest tests/ -k ide` + `ruff check src/ orchestrator/
   tests/`.
