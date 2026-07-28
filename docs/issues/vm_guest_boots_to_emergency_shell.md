---
tags:
  - issue
  - vm
  - kubevirt
  - storage
  - root-cause-unknown
---

# Issue — a VM guest boots to an emergency shell and is never noticed

**Status:** Observed 2026-07-26 on dev, VM
`agent-vm-77b3d3e6-6dfc-422e-a8ec-a5848cb8febc` (`agent-vms` ns, `vm` context).
**Root cause NOT determined.** Confirmed **intermittent** on 2026-07-28 — a
later VM booted cleanly from the same golden PVC (see Open question 1). Split
out of
`docs/issues/session_vm_backend_never_attaches.md` (Defect 3) so the three
orchestrator-side defects there can be fixed without waiting on this.

**One line:** The guest's `local-fs.target` fails on a missing
`/dev/disk/by-label/BOOT`, dropping it to an emergency shell before cloud-init
runs — so the VM never joins the tailnet and never registers, while KubeVirt
reports it `Running`/`Ready` and nothing in the system reaps it.

## Evidence

Guest serial console — the only place this is visible
(`kubectl --context vm -n agent-vms logs virt-launcher-<vm> -c guest-console-log`):

```
[ TIME ] Timed out waiting for device dev-disk-by\x2dlabel-BOOT.device
         - /dev/disk/by-label/BOOT.
[DEPEND] Dependency failed for systemd-fsck… /dev/disk/by-label/BOOT
[DEPEND] Dependency failed for boot.mount - /boot.
[DEPEND] Dependency failed for boot-efi.mount - /boot/efi.
[DEPEND] Dependency failed for local-fs.target - Local File Systems.
[  OK  ] Started emergency.service - Emergency Shell.
You are in emergency mode.
```

Consequence chain: `local-fs.target` fails → cloud-init never runs → neither the
`tailscale up --auth-key=…` runcmd nor `management-daemon.service` ever starts →
no `agent.vm.*.register` is published → `nats_bridge._on_daemon_register` never
fires → `threads.metadata.vm` stays `{"status": "created"}` with no `ssh_host`,
forever.

Corroborating: Headscale never saw the node. The three *job* VMs running
concurrently were online at `100.64.0.104/106/113`; `vm-77b3d3e6-…` was never
registered at all.

## Why nothing caught it

**KubeVirt reported this VM as `Running` with `Ready: True` for 45+ minutes.**
Those conditions describe the VMI process, not the guest. `guestOSInfo` is empty
and `interfaces[].infoSource` is `domain`-only — but that is equally true of the
*healthy* job VMs (no qemu-guest-agent in the image), so neither field
discriminates. The serial console is currently the only guest-health signal.

The VM consumed 8 vCPU / 16 GiB on `node8` the entire time, and would have
indefinitely: there is no timeout that reaps a VM which never registers.

## Eliminated causes

- **Not a thread-vs-job template difference.** Rendered cloud-init userData is
  structurally identical to a working job VM's — same image
  (`agent-vm-base:sha-9710244`), same NATS URL, same `ORCHESTRATOR_ID=srw-dev`,
  same tailscale runcmd. The only differences are `agent_config`
  (`session_base` vs `scholar`) and an empty `description`.
- **Not the VM-description YAML-injection bug** (fix unpushed on `develop`; no
  issue doc filed): thread VMs pass `description=""`. That bug is separately live
  here — job `4435994d` died at 11:26 the same morning with
  `yaml.scanner.ScannerError: while scanning a simple key` in the dev
  vm-controller — but it is not this.
- **Not the keyless-VM Headscale latch**
  (`docs/done/vm_controller_headscale_latch_kills_provisioning.md`): a preauth key
  was minted at 14:31:42 and the rendered userData carries it.
- **Not storage divergence.** The DataVolume spec is identical to the working job
  VM's — same golden source PVC `agent-vm-golden-5d0ff629e0e0`, 20Gi,
  `local-path`, `Filesystem`, both `Succeeded 100%`.
- **Not node exhaustion.** `node8` reported `DiskPressure=False` with ~1.8 TB
  ephemeral capacity.

## Open questions

The evidence points to an intermittent clone-or-first-boot failure of the golden
image rather than anything session-specific — but "intermittent" is an inference
from a single occurrence, not a finding. In priority order:

1. ~~**Has a session-on-VM *ever* reached `vm_status = "ready"` on dev?**~~
   **ANSWERED 2026-07-28: yes.** Thread `6e9f7aad-fcef-490e-97be-d570ca3f6a98`
   booted cleanly and attached over SSH (`100.64.0.235`) during the Defect 1/2/4
   live gate — from the **same golden PVC** (`agent-vm-golden-5d0ff629e0e0`) that
   produced the emergency shell on 07-26. So this is **genuinely intermittent**,
   not a permanently broken path, and Defects 1+2 were not masking a dead
   feature. One success isolates the cause no better than one failure did.
   Evidence: `docs/issues/session_vm_backend_never_attaches.md` § Live gate
   result.
2. **Do freshly created *job* VMs boot reliably right now?** The three healthy
   ones were 2-4 h old. Creating one fresh VM is the cheap discriminator between
   "golden image is fine, this was a one-off clone fault" and "the golden image
   regressed recently."
3. **Does `local-path` + concurrent CDI clones from a single golden PVC on one
   node have a truncation failure mode under load?** Four clones landed on
   `node8`.

## Actionable regardless of root cause

Independent of why the guest failed, a VM that never registers should not sit
`Running` and billable forever. A guard — "VM context in `created`/`provisioning`
with no `register` after N minutes → mark `failed`, reap the VM, surface a
truthful error to the session" — closes the resource leak and converts a silent
hang into a real error. This is worth building before the boot bug is understood,
and it is what would have made this incident self-reporting.

## Related

- `docs/issues/session_vm_backend_never_attaches.md` — the parent incident;
  Defects 1/2/4 are orchestrator-side and independently fixable.
- `docs/done/golden_image_cold_import_fails_inflight_vm_jobs.md` — prior
  golden-image failure mode (cold import), distinct from this.
