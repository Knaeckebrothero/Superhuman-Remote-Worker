# In-VM Daemon: HTTP Transport Migration — 2026-04-29

The same-cluster `vmController.transport=http` mode landed in the Helm chart
covers VM **lifecycle** (create, delete, query) over HTTP. It deliberately
does **not** carry the VM ↔ orchestrator events that the in-VM management
daemon and sudo gate exchange — those still require NATS. This document
records the gaps so they can be closed when same-cluster deployments need
the full feature set without standing up NATS.

Scope: do not implement yet. Capture the work so we don't lose context.

---

## Why these stayed on NATS

The in-VM daemons were written as NATS clients before any HTTP transport
existed. They publish/subscribe directly on NATS subjects; the orchestrator
mirrors them via subscriptions in `orchestrator/services/nats_bridge.py`.
There are no equivalent REST endpoints today, on either side, so the HTTP
transport added in `helm/templates/vm-controller/` cannot plumb these
exchanges end-to-end.

The protocol shapes themselves are not NATS-specific — they're plain
request/response and pub/sub patterns that map cleanly onto HTTP
(`POST` for events, `GET`/long-poll or webhooks for control). The
migration is mostly mechanical, but it touches three components.

---

## Affected exchanges

### 1. Daemon → orchestrator: register / heartbeat / status

**Today (NATS):**
- Daemon publishes `agent.vm.{job_id}.register` once on boot (hostname,
  Tailscale IP, daemon PID).
- Daemon publishes `agent.vm.{job_id}.heartbeat` periodically (CPU/mem/
  disk percent, agent_running flag, code-server connection count).
- Daemon publishes `agent.vm.{job_id}.status` when the agent process
  exits.
- Subscriptions wired in `orchestrator/services/nats_bridge.py:124-128`,
  handlers `_on_daemon_register / _on_daemon_heartbeat / _on_daemon_status`.

**HTTP equivalent:**
- New REST endpoints under `orchestrator/main.py`:
  - `POST /api/internal/vm/{job_id}/register`
  - `POST /api/internal/vm/{job_id}/heartbeat`
  - `POST /api/internal/vm/{job_id}/exit`
- Daemon (Go, in `vm/sudo-daemon/`) replaces its NATS publisher with an
  HTTP client. Needs `ORCHESTRATOR_URL` and a per-VM auth token in
  `/etc/default/management-daemon` (currently writes `NATS_URL` +
  `JOB_ID`).
- Auth: per-VM bearer token minted by the controller at create time and
  baked into cloud-init userData. Orchestrator validates against a token
  table keyed by `job_id`.

### 2. Orchestrator → daemon: control commands (freeze/resume/terminate)

**Today (NATS):**
- Orchestrator publishes `agent.vm.{job_id}.control` with action.
- Daemon subscribes and acts on it.
- Code path: `orchestrator/services/nats_bridge.py:274-295` (publish),
  `orchestrator/services/vm_provisioner.py:391-403` (`send_control`).

**HTTP equivalent (the harder direction):**
The daemon needs a reachable address from the orchestrator. Two options:

- **Option A — Daemon HTTP server inside the VM.** Daemon binds an
  HTTP server on a known port. Requires either (a) a per-VM Service that
  selects the VM's pod (KubeVirt creates a virt-launcher pod per VMI;
  selectors on `vm.kubevirt.io/name` work), or (b) routing via the
  controller using `kubectl exec` / virtctl, with the controller
  proxying the call. Option (a) is the cleaner integration; option
  (b) avoids the per-VM Service explosion but couples lifecycle and
  control through the controller.
- **Option B — Daemon long-polls.** Daemon polls `GET /api/internal/vm/
  {job_id}/control` from the orchestrator (long-poll or SSE). No
  inbound listener inside the VM, no per-VM Service. Simpler, but adds
  baseline traffic and hides freeze/resume latency behind the poll
  interval.

Recommendation: A is the right shape long-term (real push, no polling
overhead), but B is faster to ship and avoids per-VM Services. Both
need the same auth-token mechanism as #1.

### 3. Sudo gate: in-VM `sudo` requests → orchestrator approval

**Today (NATS):**
- `vm/sudo-daemon/` (Go) publishes `sudo.request.>` on a per-request
  subject when a user invokes `sudo` inside the VM.
- Orchestrator's `sudo_gate` subscribes (`nats_bridge.py:130-134`),
  surfaces an approval request to the operator, replies with allow/deny.
- The sudo plugin (`vm/sudo-plugin/`) waits on a Unix socket served by
  the daemon, which gates execution on the NATS reply.

**HTTP equivalent:**
- `POST /api/internal/vm/{job_id}/sudo` from daemon, returning `{decision,
  reason}` once the operator approves/denies. Effectively a synchronous
  request — the existing sudo gate already has approval timeout logic.
- Orchestrator-side glue lives in `orchestrator/services/sudo_gate.py`
  today. Needs an HTTP variant that resolves on the same approval queue.
- Sudo daemon (Go) replaces its NATS roundtrip with an HTTP call. Auth
  reuses the same per-VM token from #1.

### 4. Code-server activity tracking (IDE session idle detection)

Currently piggy-backs on heartbeat (`code_server_connections` field in
`_on_daemon_heartbeat`, `nats_bridge.py:414-426`). Falls out of #1
automatically once heartbeat moves to HTTP.

---

## Required changes (scope sketch)

**Daemon (`vm/sudo-daemon/`):**
- Replace `internal/gate/server.go` NATS publish with HTTP call to
  orchestrator sudo endpoint (#3).
- Replace heartbeat NATS publisher (currently in the management-daemon —
  separate binary, may live elsewhere) with HTTP client (#1).
- Add HTTP server for control commands if option A (#2A), or long-poll
  client if option B (#2B).
- Read `ORCHESTRATOR_URL` and `VM_AUTH_TOKEN` from
  `/etc/default/management-daemon` instead of `NATS_URL`.

**Orchestrator (`orchestrator/main.py` + services):**
- New routes under `/api/internal/vm/...` for register/heartbeat/exit/
  sudo/control.
- Auth middleware that validates the per-VM token against a table.
- Token issuance at VM-create time (returns the token to the controller
  so it can bake into cloud-init).
- `nats_bridge.py` handlers stay as-is for the cross-cluster path; new
  handlers live alongside.
- `sudo_gate.py` gains an HTTP entrypoint that shares the approval queue
  with the existing NATS path.

**VM Controller (`vm/controller/`):**
- Mints per-VM tokens during `_do_create` and substitutes them into the
  template alongside `${JOB_ID}` etc.
- Optionally creates the per-VM Service for option 2A, scoped to its
  namespace via the existing RBAC (already has `pods` get/list, would
  need `services` create/delete added).

**VM template (`helm/templates/vm-controller/configmap.yaml` and
`deployment-vms/srw-vm-controller/vm-controller.yaml`):**
- cloud-init writes `ORCHESTRATOR_URL` and `VM_AUTH_TOKEN` into the
  daemon env files.
- Drops `NATS_URL` from those files when transport is HTTP-only.

**Helm chart (`helm/`):**
- `vmController.transport=http` becomes a real full-feature mode (today
  it's lifecycle-only with a documented caveat).
- README + values.example.yaml: remove the "use `transport=both` if you
  need daemon events" caveat.

---

## Open questions

- Per-VM Services vs. controller-mediated control: pick one before
  starting #2. Affects RBAC scope and KubeVirt addressing.
- Token rotation: bake-once-at-create is simplest; if VMs are long-lived
  (persistent threads), revisit.
- Should the HTTP variant share the same `agent.vm.*` URL prefix style
  on the orchestrator (e.g. `/api/internal/agent/vm/{id}/...`) so the
  routing maps 1:1 with NATS subjects? Probably yes for grep-ability.
- Backwards compatibility: keep NATS path indefinitely (for cross-
  cluster deployments) or eventually unify on HTTP everywhere with NATS
  as a transport adapter? Cross-cluster HTTP needs an exposed
  orchestrator endpoint reachable from the VM cluster, which may or may
  not be desirable.

---

## References

- Lifecycle HTTP work that landed: `vm/controller/controller.py`,
  `orchestrator/services/vm_provisioner.py` (HTTP backend),
  `helm/templates/vm-controller/`.
- NATS subjects + handlers: `orchestrator/services/nats_bridge.py`,
  `vm/sudo-daemon/internal/gate/`.
- Original VM design: `docs/features/vm.md`,
  `docs/features/vm_backend.md`.
