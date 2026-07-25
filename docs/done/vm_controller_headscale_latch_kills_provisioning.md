---
tags:
  - issue
  - vm-backend
  - provisioning
  - headscale
  - resilience
---

# vm-controller latches "Headscale unavailable" at init and never retries — after a host reboot every VM boots keyless and provisioning fails 100%

**Status:** RESOLVED 2026-07-25. Recurred that morning (second occurrence),
root cause re-confirmed end-to-end with live evidence, durable fix shipped
in `92662dee` and **deployed to dev in `sha-9710244`** (both vm-controller
and orchestrator). Deploy verified against the running images and by a live
provisioning run — see "Deploy verification" below.
**Severity:** high for the RSI loop — one host reboot with unlucky pod
ordering stalls the entire VM backend until someone manually restarts the
controller; each doomed VM burns a full ~30-min provisioning cycle × 3
attempts before the job fails.
**Component:** `vm/controller/headscale_client.py` (~L55-80),
`docker/agent-vm-base/files/management-daemon.py`,
`orchestrator/services/nats_bridge.py` (`_on_daemon_register`, ~L508)

## Incident

2026-07-17 ~17:55Z onward, Better-Resavio loop (prod/homelab): three
consecutive loop jobs failed with
`VM provisioning failed: provisioning exhausted after 3 attempts (never
reached 'ready'). To retry, clear context.vm and re-queue the job.`

- `94130bf3` (loop iter 40, SCHOLAR)
- `09dda20d` (loop iter 41, CRITIC)
- `8d394124` (loop iter 42, DEVELOPER)

The loop stalled after 18:59Z. Not related to the same-day stage2 packer/CI
failure (VM image pinned `sha-29badb0`; CI only blocks new image publishes).

## Root cause chain (all confirmed in logs/source)

1. Homelab host reboot ~17:10-17:14Z — every pod on both clusters
   (contexts `main` + `vm`) restarted.
2. vm-controller initialized (17:13:59) before Headscale was back
   (~17:22, behind CF tunnel `headscale.h4ll.app`) → `Failed to list
   Headscale users` → **`HeadscaleClient` latches `_available=False`
   forever** (init-once, no retry).
3. Every VM created since gets an empty tailscale auth key ("VM will boot
   without mesh VPN") — cloud-init skips `tailscale up`.
4. Guest boots, mgmt daemon waits 60 s for tailscale, registers with its
   QEMU-NAT IP and `ssh_ready=false` (ssh_ready requires sshd AND a tailnet
   IP).
5. Orchestrator `_on_daemon_register`: `ssh_ready=false` → `ssh_pending`,
   waits for a re-register that never comes → 10-min timeout × 3 →
   park + fail.

## Operational remedy (once Headscale is healthy)

```bash
kubectl --context=vm -n agent-vms rollout restart deploy/srw-vm-vm-controller
# verify: log line "Headscale client initialized"
```
then per failed job: clear `context.vm` + re-queue.

2026-07-18 note: the host rebooted again ~05:50-06:10Z; this time the
controller came up after Headscale and initialized cleanly (06:10:40Z
`Headscale client initialized`) — no re-latch. The failure mode is a boot
race, so it will recur.

## Recurrence: 2026-07-25 (Better-Resavio loop, dev)

Identical chain after the homelab rebooted ~2026-07-24 19:39Z. Three loop
jobs failed with the same park message:

- `413dc55b` (loop iter 1, SCHOLAR) — 07:07→07:38Z
- `0a7d0100` (loop iter 2, CRITIC)
- `5ccfbde2` (loop iter 3, DEVELOPER)

plus three overnight jobs before them (`12ceb1a8`, `92f7dc06`, `acf08407`).
Loop `3ed022a5` then hit `max_consecutive_failures=3` → `status=failed`,
`stop_reason=failures`.

Evidence captured this time (all four layers independently):

1. Controller startup 19:39:30Z: `Failed to list Headscale users:` (empty
   cause) + `Headscale user 'srw' not found`.
2. Headscale itself healthy — queried from *inside* the vm-controller pod:
   `GET /api/v1/user` → 200, user `srw` id=1. The controller simply never
   looked again.
3. Zero `Created Headscale auth key` lines in the whole controller log;
   zero `vm-*` nodes in Headscale since the reboot.
4. Job `context.vm` of every failed job: `ssh_host: "10.0.2.2"` (QEMU NAT,
   not a tailnet IP), `registered_at` and `last_heartbeat` present and
   advancing, `ssh_verified_at: null`, `ssh_probe_error: "daemon reports SSH
   not ready yet (sshd or tailnet IP pending)"`. **The VMs were alive and
   healthy — just unreachable.**

**Correction to the original write-up:** it claimed each doomed VM logs
`"VM will boot without mesh VPN"`. It does **not**. That warning lives
*inside* the `if self.headscale.is_available:` branch, so once `_available`
is latched off the create path is **completely silent** — no per-VM warning,
no telemetry. That silence is why the same outage cost a full diagnosis
cycle twice, and is fixed below.

## Durable fix (IMPLEMENTED 2026-07-25, pending deploy)

1. **No latch** (`vm/controller/headscale_client.py`). `_available` becomes
   `_configured` (URL+key present) and never flips on a runtime failure.
   `_ensure_user_id()` re-resolves the user on demand with exponential
   backoff (`HEADSCALE_RETRY_BASE_S` 5s → `HEADSCALE_RETRY_MAX_S` 60s, reset
   on success); `create_auth_key()` calls it. New `is_ready` is the health
   signal; new `last_error` carries the cause. `_resolve_user_id` now records
   `type(e).__name__` — httpx connect/timeout errors `str()` to `''`, which
   is why the original log line had no cause after the colon.
2. **Never build a doomed VM** (`vm/controller/controller.py`). With
   Headscale configured but no key obtainable, `_do_create` returns
   `{"status": "waiting_headscale", "headscale_error": ...}` **without**
   creating the VM — mirroring the `waiting_golden` deferral. Unconfigured
   deployments (local/k3d, mesh off) still boot keyless as before.
3. **Poll, don't burn attempts** (`orchestrator/services/dispatch_guards.py`,
   `main.py`). New `VM_HEADSCALE_POLL` / `VM_PARK_HEADSCALE` decisions with
   `headscale_wait_started_at` anchoring a `VM_HEADSCALE_WAIT_TIMEOUT_S`
   (default 900s) budget. Polling re-issues create with `fresh=False`, so it
   does **not** consume a provision attempt — no VM is booting. Past budget,
   the job fails with the real cause instead of the misleading "provisioning
   exhausted after N attempts".
4. **Telemetry passthrough** (`vm_provisioner.py`, `nats_bridge.py`):
   `headscale_error` flows to `context.vm`; `_fresh_provision_ctx` resets
   `headscale_wait_started_at`/`headscale_error` so a stale anchor can't cap
   the next provision's patience.

Tests: `test_headscale_client.py` (recovery-after-failed-init regression,
backoff throttle, teardown-after-failed-init), `test_vm_controller.py`
(deferral instead of keyless create), `test_dispatch_guards.py`
(`TestVmHeadscaleWaitDecision`). The two behavioural regressions were
confirmed to FAIL against the pre-fix source. 1107 passed in the filtered
local sweep; ruff clean.

## Deploy verification (2026-07-25 16:23Z)

Shipped as `92662dee` on `develop`; CI built `sha-9710244`, deployed to both
`srw-vm-vm-controller` (context `vm`) and `srw-orchestrator` (context
`main`).

Confirmed *in the running images*, not just in git:

- vm-controller `/app/headscale_client.py`: `_available = False` → **0**
  occurrences (the latch is gone), `_ensure_user_id` → 3.
- vm-controller `/app/controller.py`: `waiting_headscale` present.
- orchestrator: `VM_HEADSCALE_POLL`/`VM_PARK_HEADSCALE` → 4 in `main.py`,
  2 in `services/dispatch_guards.py`.

Live happy-path run on the deployed images (job `5d187e1b`): pre-auth key
minted (`POST /api/v1/preauthkey` 200) → VM created 16:18:19Z → `ready`
16:23:07Z (~4m48s) with `ssh_host 100.64.0.27`, a real tailnet IP. Teardown
was clean: VM deleted and Headscale node `8884` removed, nothing leaked.

The deferral path itself (`waiting_headscale` → poll → park) is covered by
unit tests only; it was not exercised live, since that would mean breaking
Headscale on the dev cluster on purpose.

## Related

- Memory/topic: `vm-provisioning-headscale-latch-outage`.
- `docs/done/golden_image_cold_import_fails_inflight_vm_jobs.md` — the
  `waiting_golden` park pattern to reuse.
- Separate fallout from the same reboots, still open: ExternalSecrets on
  both clusters go `SecretSyncedError` while Vault is sealed
  (canvas-gateway pods stuck `CreateContainerConfigError` on 2026-07-18) —
  services otherwise run on cached Secrets. USER action: unseal Vault.
