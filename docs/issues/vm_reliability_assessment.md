# VM Workspace Reliability — Full Assessment

**Date:** 2026-07-30 · **Branch:** `develop` · **Environment audited:** dev
(`--context main` = orchestrator, `--context vm` = `agent-vms`)

Evidence base: 8 parallel audit lanes over the docs corpus, the current implementation, the
live dev VM cluster, and the orchestrator Postgres. Every claim below is tagged:

- **OBSERVED** — measured from live cluster or DB output
- **CONFIRMED** — read in current source at the cited line
- **INFERRED** — reasoning from the above; not directly proven

---

## 1. Where we stand

The VM tier is **4.7 months old** (first commit 2026-03-11) and **has not stabilized**.
Commits touching VM core by month: 11 (Mar) → 6 (Apr) → 13 (May) → 21 (Jun) → **24 (Jul, month
incomplete)**. The split is 26 `feat` to 8 `fix` — most VM effort is still adding capability
on an unstable base rather than hardening it. Eleven distinct VM defects were filed in the last
22 days, three on 2026-07-27 alone. [OBSERVED]

The single most important number:

> **VM-backed jobs fail for infrastructure reasons at 2.2× the container rate**
> (10.8% vs 5.0%, n=241 VM / 342 container, window 2026-06-10 → 2026-07-30). [OBSERVED]

The naive headline rate is a trap and must not be quoted: raw failure is VM 17.8% vs container
29.5%, which makes VM look *better*. The container bucket is inflated by ~100 unreviewed cron
jobs sitting in `pending_review` and by mostly non-workspace failures. Filtering both sides to
workspace/infrastructure signatures produces the 2.2× figure. Selection bias was bounded: of 23
failed non-`context.vm` jobs carrying VM-ish error text, 18 are genuinely container — the string
`"VM workspace connection lost"` is a code-level misnomer emitted for every backend. Worst-case
re-attribution still yields ≥2.2×. [OBSERVED]

And the dominant loss mechanism is **not** provisioning failure. It is workspace destruction on
teardown, running at scale, today.

### What is genuinely working — do not re-litigate these

| Area | Evidence |
|---|---|
| **Headscale latch fix** | `provisioning exhausted after 3 attempts` last seen **2026-07-25**; 20 VM jobs since with **zero** recurrence. QEMU-NAT `ssh_host=10.0.2.2` signature confined to exactly 07-17 → 07-25. [OBSERVED] |
| **Description YAML injection** | Escaping present and **byte-identical** across both render sites (AST-compared); `MAX_DESCRIPTION_LEN=200` in both; real-chart regression test in place. One YAML failure in all history (06-24), pre-fix. [CONFIRMED + OBSERVED] |
| **Session VM attach** | Both arms fixed. VM arm at `session_provisioner.py:107-124`; the `require_vm` block at `persistent_app.py:6657-6689` ends in `continue` and *cannot* fall through to the container branch. [CONFIRMED] |
| **Suspend/restore ordering** | Restore trigger at `session_provisioner.py:81-93` sits above the backend read at `:95`; kept-disk restore returns at the create. [CONFIRMED] |
| **`_resolve_ssh_port`** | Fixed; threads pass `is_vm`, jobs deliberately omit. `workspace_suspension.py:68-87`. [CONFIRMED] |
| **Reap "snapshot captured" lie** | Fixed in `ed26ebfa` — now logs `"VM snapshot SKIPPED … deleting anyway"`. [CONFIRMED] |
| **Cluster health & capacity** | Zero ERROR/WARN/Traceback in the full 13.4h controller log; 1.8 TiB free (5% used), all pressure conditions False, KubeVirt + CDI `Deployed`. Goldens are **bounded** (`VM_GOLDEN_KEEP=3`, count == keep) — not a leak. [OBSERVED] |
| **Deployment coherence** | No VM version skew. Every VM commit is pushed and present in both deployed images. Both `VM_PERSISTENT_ROOTDISK` flags ON in the safe order. NATS mode confirmed on both replicas. [OBSERVED] |
| **Test suite** | 874 passed, 0 failed (473 VM files + 401 adjacent). Only Py-3.14 mock noise. [OBSERVED] |

Measured latency, replacing remembered numbers: **median time-to-usable VM ≈ 3m39s**
(p50 boot→register 205.1s, p90 281.7s, n=94 since 07-01); p50 dispatch 13.7s. A persistent-rootdisk
reattach skips the clone entirely. [OBSERVED]

---

## 2. Priority list

Ranked by **(impact × how often it fires × confidence)**. "Firing" means observed in live data;
"Latent" means confirmed in code but not yet observed to fire.

### P0 — losing work right now

**P0-1 · VM workspace snapshots fail 89.7% of the time; last success was 20 days ago**

| tier | attempted | capture_failed | capture_skipped | success |
|---|---|---|---|---|
| **VM** | 145 | 71 | **59** | **10.3%** |
| container | 271 | 13 | 0 | 94.8% |

**130 of 145 VM snapshot attempts lost the workspace.** Last successful VM snapshot:
**2026-07-10**. All 60 `capture_skipped` rows (59 jobs + 1 thread, 07-09 → **07-30, today**)
carry `"unroutable tailnet target from orchestrator"`; the earlier 71 `capture_failed` say
`SSH tar failed (rc=255)` — same root cause, before the code learned to pre-check. Two of the
five most recent skips are `completed` jobs, including an **8h22m success whose entire workspace
was discarded**. [OBSERVED]

Root cause: capture SSHes *from the orchestrator*, which is not a tailnet node; a VM workspace
has only a `100.64.0.0/10` address. `capture_vm_snapshot` hits the F4 guard and returns False
(`snapshot_service.py:398-421`). [CONFIRMED]

Status nuance that matters: the *logging lie* is fixed (`ed26ebfa`), so this is no longer
**silent**. It is still **loss**. Persistent rootdisk was adopted specifically to make this
capture unnecessary — but `rootdisk` is **null on 238 of 243 rows** (kept 0, purged 5). The
designated remedy is live and has engaged on ~2% of the population while the loss continues
daily. [OBSERVED]

*Fix locus:* either make the rootdisk path actually cover the population, or build the VM-side
push (daemon captures + uploads to S3, triggered over NATS — the VM already speaks NATS and needs
no inbound route). Do **not** put a tailscale sidecar on the orchestrator; a prior incident
deliberately narrowed that blast radius.

---

**P0-2 · The `infra_transient` fix never runs on the path that matters**

A sub-second DB blip still hard-fails the job **and destroys its VM**. The 2026-07-27 incident
(~46h of work lost) would recur unchanged today.

`src/agent.py:1058-1069` and `:1237-1248` still do binary `workspace_unavailable` / `job_error`
classification and never call `completion_error_payload`. Commit `256f8213` did not touch
`agent.py`. The app-layer path that *could* reach it (`app.py:643/1194`) is unreachable because
`_process_job_streaming` catches `Exception` itself and **yields** the error state, so the
`except` never fires. Result: `job_error` → terminal → `main.py:15361` → VM destroyed.
[CONFIRMED]

The orchestrator arm (`main.py:14652-14748`) and the reaper carve-out (`vm_manager.py:223,272`)
both landed correctly and *do* preserve the VM — they are simply never reached. **This also
explains why the live gate could never reproduce the failure with `pg_terminate_backend`:** the
code path is not exercised at all. [CONFIRMED]

*Fix locus:* `src/agent.py` classification sites. Small change, high value.

---

**P0-3 · A VM session has no re-drive, no crash recovery, and no reap guards**

Jobs get a full VM state machine — PROVISION / WAIT / RECYCLE / PARK_EXHAUSTED / GOLDEN_POLL /
HEADSCALE_POLL with attempt and timeout budgets (`dispatch_guards.py:93-143`,
`main.py:5711-5940`). Sessions get **one fire-and-forget `create_thread_vm`** and nothing ever
re-drives it. Jobs have four reap guards (`vm_manager.py:255-274`); sessions have
`thread_status in {"ended"}` and zero guards. Jobs crash-recover with a kept disk and re-dispatch
(`main.py:14790-14820`); a VM session's dead VM has **no recovery path** at all —
`_exit_workspace_not_ready` exits and `ensure_session_workspace` declines. [CONFIRMED]

Compounding: **session VMs have no `waiting_golden` / `waiting_headscale` handling.**
`create_thread_vm:1024` has no `fresh` param and `_create_http:839` returns **True** on a
deferral, so `main.py:22455` believes a VM is coming and nothing polls — a silent permanent hang
for ~30 min after any image bump. Live confirmation that this arm has never worked: `waiting_*`
statuses have **never fired**, and `golden_wait_started_at` / `headscale_wait_started_at` /
`headscale_error` are **null on all 243 rows**. [CONFIRMED + OBSERVED]

---

### P1 — confirmed, will fire

**P1-4 · `deleting` has no exit.** `dispatch_guards.py:127` returns WAIT *before* the timeout
check at `:139`, and `vm_manager.py:78` skips `deleting`. Both the delete request and the
controller's answer are core-NATS fire-and-forget (at-most-once, no JetStream). **One lost
message wedges a job forever, silently.** [CONFIRMED]

**P1-5 · `delete_failed` → unbounded recycle, job never terminal.** `PARK_EXHAUSTED` requires
status absent-or-`deleted`, so it is unreachable from `delete_failed`; the loop runs
delete → `deleting` → `delete_failed` → RECYCLE every 30s forever.
Controller writes it at `controller.py:1023`. [CONFIRMED]

**P1-6 · `VM_RECYCLE` purges a deliberately-kept rootdisk.** `main.py:5981` passes no
`purge_disk`, so it defaults True. Deterministic in HTTP mode, where `vm.status` rests at
`suspended` and `dispatch_guards.py:139-143` classifies it as "stuck short of ready".
[CONFIRMED]

**P1-7 · Session rootdisks and Headscale nodes are never reclaimed.**
`_archive_and_cleanup_workspace`'s thread-VM guard is an **allowlist**
(`status in ("provisioning","created","ready")`, `main.py:5009`) while the jobs branch two lines
below at `:5022` is a **denylist**. After any keep-delete the status is
`deleting`/`deleted`/`suspended`, so `release_thread_vm` **never runs**. Kept-disk GC is
jobs-only (`vm_manager.py:583-591`); the controller orphan backstop ships **off**
(`VM_ROOTDISK_GC_ENABLED=false`, live). Each leak is a 20 GiB DataVolume **plus** a Headscale
node (`delete_node` lives only inside the purge branch, `controller.py:462-470`). ~50 sessions
per TB, nothing bounds it. [CONFIRMED]

Currently **zero** leaked disks exist — the surface is latent, not active. Corroborating live
evidence that it is real: **2 threads sit `ended` with `vm.status='suspended'`** while the cluster
holds zero VMs (`accfbc56`, 6d17h; `77b3d3e6`, 3d17h). Terminal, so nothing will ever reconcile
them. [OBSERVED]

**P1-8 · IDE sessions collide with job VMs on entity id.** `ide_session.py:525` calls
`create_vm(job_id=job_id)` and `:1254` calls `delete_vm(job_id, purge_disk=True)`. `start_session`
guards only on `ide_session.status` (`:236`); `main.py:15505` has no job-status guard. So opening
an IDE against a running job wipes the live VM's `ssh_host` (via `_fresh_provision_ctx`) and
closing it purges the job's rootdisk. The reconciler knows about IDE (`vm_manager.py:207,257`);
**the dispatcher does not.** [CONFIRMED — found independently by two lanes]

**P1-9 · Thread VM deletes are written into the `jobs` table.**
`nats_bridge.request_vm_delete` (`:340`) has no `entity_type` and routes through `_set_vm_context`
(`:935`) unconditionally to jobs; `request_vm_create` branches correctly at `:290/294`. A thread
row therefore keeps `vm.status='ready'` after its VM is deleted, until the async callback lands —
or forever if it is lost. [CONFIRMED]

**P1-10 · Chart drift breaks VM boot on the `helm/` path.** Three defects, all proved by
rendering the real charts:
1. **`ORCHESTRATOR_ID` is absent from `helm/`'s VM template** — `helm-vm-cluster/` writes it into
   `/etc/default/management-daemon`, `job-config.json` and `/etc/default/sudo-gated`; `helm/`
   writes none. `management-daemon.py:64-69` **hard-requires** it → `sys.exit(1)` → the VM boots
   and **never registers**. Cause: `4d7616cf` touched only the vm-cluster copy.
2. **`helm/values.yaml:1940` defaults `vmSshPublicKey: ""`** and renders an **empty**
   `authorized_keys` silently; `helm-vm-cluster/` hard-fails the same case.
3. **`${SSH_AUTHORIZED_KEY}` is never substituted** — documented as controller-filled
   (`configmap.yaml:20-24`) but `rg SSH_AUTHORIZED_KEY vm/controller/` returns **0 hits**.
[CONFIRMED — two lanes independently]

`helm-vm-cluster/` has **no `ci/` directory** and is never linted, rendered, or kubeconform'd —
only packaged. That is why this drift survived. [CONFIRMED]

---

### P2 — structural; fixing these stops the bleeding at source

**P2-11 · No VM test substrate — the root cause of nearly everything above.**
Quantified: **~70% of VM failure modes are statically catchable on a laptop today**; only ~8%
(1 of 13) genuinely need a booting guest; ~15% more need a real API server for *admission only*.
**Every high-cost VM incident on record — the description newline, the headscale latch,
ssh-ready, and all three chart defects in P1-10 — was statically catchable.** [CONFIRMED]

Recommended, in order:
- **Tier 1 — rendered-manifest contract test. ~0.5–1 day, ~150 LOC.** Shell out to `helm template`
  (as `test_canvas_slice3_infra.py` already does) instead of regex-extracting, then assert:
  placeholder closure both ways against each renderer's `replacements` dict (catches defect 3);
  cross-chart `write_files`/`runcmd` parity (catches defect 1); `/etc/default/management-daemon`
  carries exactly the keys `management-daemon.py:64-69` requires; non-empty `authorized_keys` at
  defaults (catches defect 2); move the 2048-byte budget onto real helm output; add
  `helm-vm-cluster/ci/*-values.yaml`.
- **Tier 2 — `kubectl apply --dry-run=server`, ~1–2 days, nightly.** The only cheap thing that
  catches the CDI webhook class (the 422 `spec.source.pvc.namespace` that killed every create when
  the rootdisk flag was first flipped).
- **Tier 3 — one-VM boot smoke on the real `vm` cluster, ~1 week, nightly not PR-gate.** The only
  thing that catches guest-boot failure outright.
- **Do NOT build a fake KubeVirt API.** It would be a hand-written stand-in for the artifact under
  test — this audit's central defect — at 10× the size of the existing fixtures.

**P2-12 · The test suite contains counter-pins and copies of the code.**
- `tests/test_vm_upgrade_endpoint.py` is **516 lines testing a copy of the code**. It imports
  `json, sys, Path, pytest` and nothing else; `_job_needs_vm` is commented *"Replicate
  _job_needs_vm from orchestrator/main.py"*, with the SQL as a string literal. ~30 tests that
  cannot fail when production changes. The stated import obstacle is **false** — three sibling
  files import `main` fine.
- `test_vm_controller.py:419` asserts descriptions substitute **verbatim** while `controller.py:220`
  JSON-*escapes* them. It passes only because its payload has nothing escapable. **A regression
  back to verbatim turns this test green and the real escaping test red.**
- `SAMPLE_VM_TEMPLATE` asserts on `spec.config.*` fields **that do not exist**, and verifies
  `${MEMORY}` against `domain.resources.requests.memory` when the real field is `domain.memory.guest`.
- **Zero `spec=` / `autospec=` in the entire VM suite** (`rg` → 0 hits). Nothing validates any
  manifest body.
[CONFIRMED]

**P2-13 · No CAS on any VM state transition.** Every `context.vm` write is a blind key-wise merge
(`postgres.py:2269`). The one CAS helper, `merge_vm_context_if_current` (`postgres.py:2352`), has
**zero production callers** — only test mocks — despite `ssh_registration_id` being written
specifically to enable it. The safety mechanism was built and never wired up. [CONFIRMED]

**P2-14 · The whole VM lifecycle path is not leader-gated.** Both orchestrator replicas subscribe
to `vm.lifecycle.*`; `_on_vm_lifecycle_status` and `_on_daemon_heartbeat` use no queue group, so
both write. Conversely `_on_daemon_register` **is** leader-gated (`nats_bridge.py:576`), so during
a leadership handover (10s poll, up to 30s backoff) **no replica accepts a register** — core NATS
has no redelivery and the daemon never retries. [CONFIRMED live: both replicas logged
`VM provisioner ready: NATS mode`, `Workspace suspension enabled (backends=k8s,vm)`, idle sweeper
and snapshot GC — OBSERVED]

**P2-15 · The VM sudo gate ships permanently fail-open.** *(New — no issue doc exists.)*
`provision-stage2.sh:214` states "cloud-init switches to `fail_mode=deny` at boot". **Nothing
does** — an exhaustive search finds no code anywhere that rewrites `sudo.conf` or `fail_mode` at
boot. Line 219 writes `fail_mode=open` and line 221 makes the file immutable (`chattr +i`). A
*correct* config (`files/sudo-gate.conf`, `fail_mode=deny`) is uploaded to the guest by Packer and
then ignored in favour of the inline wrong line. [CONFIRMED]

Calibrated: this is **not** a containment breach — the VM tier exists precisely to give the agent
root. What is lost is the **approval and audit path**. `orchestrator/main.py:2128-2129` sets
`sudo_action="allow"` for VM jobs ("VM has its own sudo gate"), disabling the agent-side gate; the
VM-side gate is socket-activated and fail-open, so any daemon fault silently degrades to "always
allow" with the operator never prompted. Neither gate enforces. It also interacts with the open
emergency-shell bug: the hardening step was assumed to live in cloud-init, which is exactly what
does not run in that failure mode.

Note the earlier finding this supersedes: `vm_workspace_missing_browser_exec.md:384` (filed 07-17)
reports the gate binaries absent from the image. **That was fixed the next day** by `9011352f`
(07-18, artifact-path fix). Residual: the download step keeps `continue-on-error: true` and the
`touch`-empty-placeholder fallback, so a transient *download* failure still ships a gate-less image
with a green build — the hard guard only covers `build-sudo-gate` *failing*.

**P2-16 · Nothing reaps a VM that never registers.** `registered_at` is written
(`nats_bridge.py:631`) and **read by nothing**. There is no "never registered → reap" guard, which
is precisely why an emergency-shell VM holds 8 vCPU / 16 GiB indefinitely. [CONFIRMED]

---

### P3 — hygiene, observability, dead code

- **There is no VM lifecycle event log anywhere.** `thread_events` holds zero VM kinds; the audit
  DB stores agent-side tool calls only; the controller never emits VMI phase. Every timeline in
  this report had to be reconstructed from `context.vm` field archaeology. [OBSERVED]
- **`get_stuck_jobs` only scans `status='processing'`** — structurally blind to the 3 VM jobs
  paused for 16–46 days. [OBSERVED]
- **`failed_at` is populated on only 3 of 44 failed VM jobs.** [OBSERVED]
- **Dead code:** `suspend_idle_workspaces` has no callers → job-VM suspend/restore never runs;
  `vm_image` is never written to `context.vm` → VM drift detection always computes `version=None`;
  `pending` is read at `main.py:26103` and never written. [CONFIRMED]
- **`vm_manager.job_dispatchable` ≠ `get_dispatchable_jobs`** despite a docstring claiming an exact
  mirror — it omits the recursive ancestor cascade guard and the `cloud_baseline='seeding'` term.
  A paused job under a failed parent is un-reapable *and* un-dispatchable → permanently leaked VM.
  [CONFIRMED]
- **Failure reasons are discarded at 9 sites.** The worst: `persistent_app.py:834-858` logs the
  exception then `os._exit(0)` with no upstream report, so every VM session failure collapses to
  `"agent /ready timeout"` — or to nothing, since the client timeout is swallowed at
  `persistent-chat.service.ts:1587-1596`. `metadata.vm.status` is exposed nowhere in the UI.
  [CONFIRMED]
- **Golden GC's in-use scan reads `spec.dataVolumeTemplates`, which `_ensure_rootdisk:768` pops.**
  Under the live config the scan sees nothing in use — it could delete an in-use golden. GC also
  only fires on a successful create and no-ops at `len <= KEEP`, so it has **never executed**.
  [CONFIRMED]
- **userData headroom is 111 bytes, not the ~350 the comments claim** — measured against the live
  template at a 200-char description (1737 B empty / 1937 B full, 2048 B ceiling). [CONFIRMED]
- **Infrastructure fragility:** the VM cluster is a **single node** (`node8`, control-plane + etcd +
  workload, no workers). On **2026-07-24T19:22:34Z** eight pods across four namespaces died at the
  identical second with `exit 255` — nats-leaf, virt-handler, virt-controller, virt-operator,
  cdi-apiserver, cdi-uploadproxy, local-path-provisioner, coredns. Partial repeat 2026-07-28T13:14:37Z.
  Node-level, not app flapping; on one node it kills every VM *and* the NATS leaf that would report
  it. NATS was hub-disconnected for **16m28s** in that window. [OBSERVED]
- **Deploy churn:** vm-controller Deployment generation **51**, Helm revision **104**, 10 ReplicaSets.
  On 07-29: five rollouts in 100 minutes, **three inside five minutes**, with two RS pairs sharing an
  identical image tag (i.e. the pod spec was being edited live). The only unused preauth key in 14
  days was minted inside that window. Mitigating: running VMs survive a controller restart; only
  in-flight requests are lost. [OBSERVED]

---

## 3. Open items this audit could not close

- **Guest boots to emergency shell** (`vm_guest_boots_to_emergency_shell.md`) — root cause remains
  **unknown**. Disk ordering and labels are clean; no repo code touches fstab/labels/grub. The
  leading hypothesis is a dropped or zero-filled sparse range in the cloned `disk.img` (CDI copies
  with **no checksum**), fitting the observed asymmetry: dense `/` mounted, mostly-sparse `/boot`
  had no by-label node. **Discriminating test:** `cmp` the golden's `disk.img` against a clone on
  `node8`. One competing theory (reattach skips cloud-init) was **refuted** — instance-id changes
  per VMI, so cloud-init re-runs. [INFERRED]
- **A kept rootdisk vanished with no delete logged.** Job `a1240add`: disk created 19:06:55,
  reattached `Succeeded` at 19:35:15 and 19:50:04, final delete 19:52:30 with `rootdisk=keep`, **no
  purge ever logged**, and no rootdisk DV exists today. Ruled out from the cluster side: ownerRef
  cascade, controller rootdisk GC (disabled), golden GC (different selector). Prime suspect is
  orchestrator Layer-2 reclaim (`vm_manager.py:559-608`) — but that routes over NATS and would have
  logged a purge, which is absent. **Unresolved.** [OBSERVED + INFERRED]
- **Two-replica disagreement** — the structural hazard is confirmed (P2-14), but the specific
  "same event logged as thread on one replica, job on the other" disagreement is **UNPROVEN**: both
  replicas restarted 23 min before sampling, no `--previous`, and the VM cluster was idle throughout,
  so only startup banners were available. [OBSERVED, insufficient]
- **Prod exposure** — `develop` is **1444 commits ahead of `main`**. Every fix in this report is
  dev-only. Whether prod exposes the VM tier at all is unconfirmed; if it does, it is running
  March/April-era VM code with none of these fixes. [OBSERVED + INFERRED]

---

## 4. The doc trail is not trustworthy — read this before planning from it

Of the VM issue corpus, **only 4 docs assert a full adversarial live gate**
(`session_vm_backend_never_attaches`, `job_description_newline_breaks_vm_template_render`,
`workspace_reattach_ephemeral_ip_reconnect_churn`, `workspace_upgrade_drops_cloud_mount`).

**Nine claim FIXED/DEPLOYED while the doc itself admits no gate was run**, several with fixes
uncommitted for 21 days — including `vm_ssh_readiness_probe_unroutable_from_orchestrator`,
`vm_upgrade_pause_workspace_reaped_before_approval`, `vm_workspace_missing_browser_exec`, and
`transient_db_error_hard_fails_job_and_destroys_vm` (whose ungated arm is exactly P0-2). Two docs
are stale in the *other* direction, describing as unbuilt work that has since landed.

Structural patterns worth naming:
- **One defect, filed six times.** "The orchestrator has no tailnet route" appears as six separate
  issues (SSH probe, snapshot-unreachable, IDE restore §3, cold-import Finding C, VM-upgrade fix 1.3,
  `vm_snapshots_and_ide` status note).
- **`is_reapable` has five bespoke consumer exemptions** rather than one coherent rule — and P3
  shows a sixth is already needed.
- **Tier inferred from metadata shape: three instances in two weeks**, with a fourth reader still
  unfixed.
- **Recurrences:** the headscale latch fired twice (07-17, 07-25) plus a near-miss; "reaper kills a
  workspace a live child needs" fired twice and the trigger got *wider* between them.
- **`srw-vm-dispatcher-reconciler-churn` has no doc anywhere** — it is referenced by name in another
  doc's Related line and exists only in session memory.

---

## 5. Recommended sequence

1. **P0-2 (`agent.py` classification)** — smallest change, stops active work destruction, and
   un-blocks the live gate that has been owed for days.
2. **P2-11 Tier 1 (rendered-manifest contract test, ~0.5–1 day)** — immediately catches all three
   P1-10 chart defects and prevents the next one. Highest leverage per hour in the whole list.
3. **P0-1 (snapshot loss)** — decide the mechanism (rootdisk coverage vs VM-side NATS push) before
   writing code; this is a design decision, not a patch.
4. **P1-4, P1-5, P1-6** — three small, independent, high-confidence state-machine fixes.
5. **P0-3 / P1-7 (session parity)** — the largest chunk; sessions need the guards, budgets, and
   reclaim paths jobs already have. Scope as its own slice.
6. **P2-15 (sudo fail-mode)** — one line plus a verification hook; file it as an issue first since
   nothing tracks it.

Items P2-12, P2-13, P2-14, P3 are real but should follow, not lead.

---

## 6. Method and limits

Eight parallel lanes: docs inventory · provisioning/boot trace · lifecycle/persistence ·
dispatcher/state machine · session tier · test coverage · live VM cluster · live orchestrator+DB.
Plus a lead lane covering deployment state and engineering history. All cluster and DB access was
strictly read-only.

**Known limits of the evidence:**
- The vm-controller log retains only **13.4h** (bounded by pod age); the 07-29 redeploy storm and
  both mass-restart events fall outside it and cannot be reconstructed.
- Orchestrator logs span **~28 min** (both replicas restarted before sampling), with no `--previous`.
- The VM cluster was **idle for the entire audit** — zero VMs — so no live provisioning was observed
  end to end. All lifecycle timelines are reconstructed from DB field archaeology.
- Failure-rate denominators are stated inline; the 2.2× figure depends on the infra-attribution
  filter described in §1 and should be re-derived if that filter is disputed.
