---
tags:
  - issue
  - vm-backend
  - provisioning
  - headscale
  - resilience
---

# vm-controller latches "Headscale unavailable" at init and never retries — after a host reboot every VM boots keyless and provisioning fails 100%

**Status:** ROOT CAUSE CONFIRMED (logs + source) during the 2026-07-17
outage. Operational remedy known; **durable fix UNBUILT.**
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

## Durable fix (UNBUILT)

- Re-resolve the Headscale user on demand with backoff instead of latching
  `_available=False` at init.
- While Headscale is down, refuse VM creation (or park as
  `waiting_headscale`, like the existing `waiting_golden` park) — a keyless
  VM is doomed and burns a 30-min provisioning cycle for nothing.

## Related

- Memory/topic: `vm-provisioning-headscale-latch-outage`.
- `docs/done/golden_image_cold_import_fails_inflight_vm_jobs.md` — the
  `waiting_golden` park pattern to reuse.
- Separate fallout from the same reboots, still open: ExternalSecrets on
  both clusters go `SecretSyncedError` while Vault is sealed
  (canvas-gateway pods stuck `CreateContainerConfigError` on 2026-07-18) —
  services otherwise run on cached Secrets. USER action: unseal Vault.
