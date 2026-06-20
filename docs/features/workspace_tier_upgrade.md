---
tags:
  - feature
  - architecture
  - workspace
  - agents-at-scale
  - security
  - sessions
aliases:
  - lite to workspace upgrade
  - virtual to sandbox upgrade
  - live workspace upgrade
  - workspace tier escalation
related:
  - "[[no_workspace_agent_mode]]"
  - "[[vm_backend]]"
  - "[[workspace_pvc_backed_migration]]"
  - "[[workspace_warm_pool_and_async_sessions]]"
  - "[[ephemeral_workspaces]]"
  - "[[tool_permission_tiers]]"
---

# Workspace Tier Upgrade — `virtual`/`none` → `sandbox`/`vm` on demand

**Status:** **Design — not yet scoped or built (2026-06-20).** Proposes a live
upgrade path from the lite workspace tiers to a real container or VM, so a
session (or job) can start cheap and instant and only acquire a workspace pod /
VM when the work actually needs one. Grounded in a codebase trace (what's
reusable vs missing) plus a survey of how cloud-IDE / agent-sandbox platforms
solve the same problem. Sibling to [[no_workspace_agent_mode]] (which shipped
the lite tiers) and reuses the container→VM upgrade precedent from
[[vm_backend]].

**In one paragraph.** Make workspace acquisition lazy: a session (or job) starts
on `virtual` and upgrades to a `sandbox` pod (or `vm`) only when the work needs
one. The recommended path is **sessions first, via the existing in-process
`swap_backend()`** — it's genuinely live (no freeze, no resume) because session
state lives in Postgres and one agent process holds both backends; the only real
code gap is re-deriving the toolset after the swap. Four load-bearing decisions:
(1) **logical checkpoint, not process migration** (no CRIU / live-migration);
(2) **agent-side copy-down seed** from the object-store prefix into the new
workspace; (3) **auto-upgrade `virtual → sandbox` is acceptable** (non-root,
default-deny egress) *only if the trigger is human intent, not ingested
content* — `vm` stays operator-gated; (4) a **warm pool** makes it feel instant
but is an accelerator, not a prerequisite.

---

## 1. Motivation

Today a workspace backend is chosen **once**, at job dispatch or thread create,
and never changes. The lite tiers from [[no_workspace_agent_mode]] (`virtual` =
object-store file ops, `none` = scratch tmpdir) boot with **no workspace pod and
no PVC** — instant, cheap, available to every user. The heavy tiers (`sandbox` =
K8s pod + PVC with a real shell, `vm` = KubeVirt VM with sudo) pay a 5–60 s
cold-start and are gated.

The one existing escalation path is **container → VM only**: an agent in a
hardened container runs `sudo`, the shell layer freezes the job with
`freeze_type: vm_upgrade_required`, an operator approves, and the dispatcher
re-provisions onto a VM. There is **no path from `none`/`virtual` → `sandbox`/`vm`**
— and the lite tiers can't even *trigger* the existing one, because the trigger
is a sudo attempt and lite tiers have no shell.

The target use case:

> A persistent **session** starts on `virtual` — instant, no pod, no cost. Most
> chats never need more. The moment the work needs a real environment (the user
> starts coding, the agent reaches for a shell or git, a build must run), the
> session **upgrades in place** to a `sandbox` container — without dropping the
> conversation — and continues with shell/git tools now available.

This makes "spin up a workspace lazily in the background" the default, instead of
paying for a pod up front on every session. It is also the natural home for a
**warm pool** (see [[workspace_warm_pool_and_async_sessions]]) so the upgrade
feels near-instant.

---

## 2. Current state — what exists, what's missing

### 2.1 The container→VM precedent (the template)

Two **different** mechanisms already exist, and the difference is the single
most important finding for this design:

**Worker jobs — freeze → re-dispatch (heavyweight):**
- Trigger: `sudo` → `RemoteBackend._check_blocked` returns `SUDO_FREEZE_SENTINEL`
  (`src/core/backends/remote.py:731`, `src/tools/shell/shell_manager.py:82`) →
  `_check_sudo_freeze` calls `context.request_freeze({freeze_type:"vm_upgrade_required", ...})`
  (`src/tools/shell/shell_tools.py:194`, `src/tools/context.py:472`).
- Graph writes `freeze_data` + `should_stop=True` (`src/graph.py:3603`).
- Orchestrator routes `vm_upgrade_required → paused` (`orchestrator/services/completion.py:323`),
  records a Cockpit-visible `sudo_approval_requests` row
  (`orchestrator/services/sudo_gate.py:561`).
- Operator approves `POST /api/jobs/{id}/upgrade-to-vm` (`orchestrator/main.py:7606`):
  sets `context.vm.requested=true`, `upgrade_from="container"`, clears
  `freeze_data`, `status='paused'`, `assigned_agent_id=NULL`, `_trigger_dispatch()`.
- Dispatcher provisions the VM, then `_resume_job_on_agent` (`main.py:1741`)
  re-injects the VM connection and the agent resumes from its checkpoint.

**Persistent sessions — live hot-swap (lightweight, the better model):**
- Same `vm_upgrade_required` freeze request, but in a session it fires
  `callbacks.on_vm_upgrade_needed` (`src/persistent_graph.py:309,1508`) →
  broadcast `vm_upgrade.needed` to Cockpit (`src/api/persistent_app.py:3271`).
- User clicks → WS `upgrade-to-vm` → `_handle_vm_upgrade`
  (`persistent_app.py:4500`): provision VM, build a new `RemoteBackend`, call
  **`_session.swap_backend(new_backend)`** (`src/api/persistent_session.py:867`).
  **The session never drops — no freeze, no re-dispatch, no resume.**

### 2.2 What's hardcoded to container→VM (must generalize)

| Assumption | Where | Breaks for lite→sandbox because… |
|---|---|---|
| Trigger is a **sudo attempt** | `shell_tools.py:194` | lite has **no shell** — the trigger can never fire |
| `freeze_type == "vm_upgrade_required"` literal | `completion.py:323`, `main.py:7668` | need a tier-parameterized type |
| `upgrade_from = "container"` literal | `main.py:7692` | source is `virtual`/`none` |
| Target gated on `vm_provisioner.is_available` | `main.py:7676`, `persistent_app.py:4500` | sandbox target uses `container_provisioner` |
| `context.vm.requested` only | `_job_needs_vm` `main.py:2367` | `_job_needs_sandbox` (`main.py:2401`) has **no `requested` escape hatch** and short-circuits lite (`:2435`) |
| `sudo_action="allow"` on target | `persistent_app.py:4500` | sandbox keeps `"freeze"`; only VM allows sudo |
| Sandbox connection injected only on **fresh dispatch**, not resume | container inject `main.py:1501` exists; `_resume_job_on_agent` injects VM+lite but **not sandbox** (`main.py:1818`) | concrete gap |
| `swap_backend` rebuilds shell but **doesn't re-derive the toolset** | `persistent_session.py:867` | upgraded session would keep lite-filtered tools (no shell/git) |

### 2.3 State portability — the deciding constraint

| | Conversation / graph state | Workspace files |
|---|---|---|
| **Session** | **Portable** — lives in Postgres `thread_messages` (`persistent_app.py` resume). No LangGraph checkpointer at all. | `virtual` files are **already canonical in S3** under the thread prefix. |
| **Worker job** | **Not portable** — `AsyncSqliteSaver` SQLite on the agent pod's `emptyDir` (`agent.py:2623`, `workspace.py:126`). Cross-pod survival is faked via phase snapshots (also `emptyDir`) + Gitea re-clone — **both unavailable to lite tiers**. | `virtual` files canonical in S3 (`jobs/<id>/`); `none` files are pod-local tmpdir, **not durable**. |

Two consequences that shape the whole design:

1. **Sessions are dramatically easier than jobs.** The same long-lived agent
   process does the swap, so the conversation never moves, the file "migration"
   is a local copy between two backends the process already holds, there is
   **only one writer** (no split-brain), and there is **no resume**. This is why
   the session path is recommended first.
2. **No code copies the virtual S3 prefix into a fresh pod/PVC/VM today.** The
   virtual loose-object layout (`jobs/<id>/<file>`) and the snapshot-tarball
   layout (`srw-snapshots/.../env.tar.zst`, `snapshot_service.py:283`) are
   different schemes; `restore_workspace` only understands the latter. A new
   **seed step** is required either way.

---

## 3. Industry prior art (what the survey found)

Seven-platform survey (Codespaces, Gitpod, Coder, Replit, StackBlitz
WebContainers, e2b, Daytona, Modal, Cloud9/CodeCatalyst) plus the state-migration
and agent-security literature. The load-bearing lessons:

1. **Almost nobody does a true live "grow the running box."** Machine-type
   change is universally **stop → reprovision → resume**; what survives is
   whatever sits on a **persistent volume**, not the running process. The only
   genuine live-resize primitive is K8s in-place Pod resize (1.33+, CPU/mem
   only, same node) — and Daytona's live CPU/mem *increase*. Neither helps a
   *tier jump* from "no pod" to "pod". → **Design for freeze→reprovision→resume,
   hidden behind a warm pool. Don't build process-level migration.**

2. **Do a logical checkpoint, not a process checkpoint.** Every
   process-preserving technique (CRIU container checkpoint, KubeVirt live
   migration, Firecracker snapshot) either can't cross tiers turnkey, needs
   shared/identical storage/hardware, or **breaks exactly the sockets the agent
   must re-establish anyway** (DB, LLM API, SSH). For small file state + a
   connection-heavy Python agent, the clean pattern is: **quiesce → make the
   object store the source of truth → provision the new tier → copy the prefix
   down → resume from a logical checkpoint**, fenced by a monotonic token so the
   old generation can't zombie-write. (Refs: K8s forensic-checkpointing is
   alpha and not restorable in-cluster; KubeVirt restore requires stopping the
   VM; Firecracker resume breaks guest networking and shares clone entropy.)

3. **Copy-down beats lazy S3 mount for small state.** S3-as-a-filesystem
   (mountpoint-s3 / s3fs / rclone mount) has sharp POSIX edges — sequential
   writes only, no rename, no in-place append, no locks. For tens of files
   (`plan.md`, `todos.yaml`, `notes/`, code) an init-container/agent-side
   **copy-down** gives real POSIX and a hard "state is local and consistent"
   boundary. Reserve lazy mounts for large read-mostly datasets. S3's
   strong-read-after-write (since 2020) makes the "PUT then GET from the new
   env" handoff safe at the API layer.

4. **Daytona is the closest north star.** Its RUNNING → STOPPED (disk only) →
   ARCHIVED (filesystem → object storage) gradient is exactly this tiering. Our
   `virtual` tier is essentially Daytona's ARCHIVED-but-never-yet-hydrated as
   the *default start state*. Note its documented penalty: object-storage
   rehydration scales with size — so **size-cap the lite tier**.

5. **Warm pool is the latency hider, and Coder's prebuilt-workspaces is the
   blueprint.** A reconciliation loop keeps N pre-built pods of common shapes
   running; the upgrade **claims** one (re-parent + rename) in ~30 s instead of
   cold-provisioning in minutes. EC2 warm pools are the VM analog. Pitfall:
   idle pools cost money — pool only the 1–2 most common shapes. This is an
   accelerator, **not** a prerequisite.

6. **Security: auto-upgrade is defensible only for a tier that is itself a real
   isolation boundary.** Industry consensus is a two-axis model (filesystem
   scope × network egress) with escalation gated by a human or a classifier —
   **never by the model itself** (GitHub Copilot CVE-2025-53773: indirect prompt
   injection wrote `autoApprove:true` into the agent's own config → RCE; Meta's
   "Agents Rule of Two" and OWASP LLM08 say the same). Mapped to our three tiers
   as a concrete design decision in §4.4.

---

## 4. Proposed design

### 4.1 Core model

Adopt the **logical-checkpoint** pattern (§3.2) uniformly, with two flow shapes:

- **Sessions → live in-process swap** (no freeze, no resume). Recommended v1.
- **Worker jobs → freeze → re-dispatch** (the `upgrade-to-vm` template,
  generalized). v2.

Generalize the existing container→VM machinery rather than forking it:

- New freeze type **`workspace_upgrade_required`** carrying
  `target_tier: "sandbox" | "vm"` and `from_tier`, superseding the
  VM-specific `vm_upgrade_required` (keep the old type as an alias for the
  sudo→VM path during migration).
- New context namespace **`context.workspace_upgrade = {requested, target_tier, from_tier}`**,
  parallel to `context.vm` but tier-parameterized. **Never** rewrite
  `jobs.config_override` / `resolved_config` (frozen at first dispatch,
  `postgres.py:1370`); drive the backend flip through a `context.*` flag +
  the dispatch-time `config_override` deep-merge the agent already applies on
  resume (`agent.py:1043`). This is exactly how container→VM works.

**State-survival contract** (decide it and surface it — the research's #1 source
of user surprise): across an upgrade, *files* survive (the seeded prefix) and the
*conversation* survives (sessions: live in-process + Postgres `thread_messages`;
worker: the logical checkpoint). What does **not** survive is a tool process
mid-call — so the cutover happens at a turn/phase boundary where nothing is in
flight. For a session it's seamless: a brief "upgrading workspace" state, then
the user keeps chatting with shell/git now available.

### 4.2 Session flow (v1, the headline)

The same agent process owns both backends during the swap, which collapses most
of the hard problems: the conversation never moves (it's live in-process +
Postgres), the file copy is local between two backends the one process holds,
there is a single writer (so **no fencing token is needed** — the agent quiesces
itself between turns), and there is no resume.

**Runtime sequence (happy path).** `virtual` session → upgrade requested (user
clicks "Upgrade workspace", or the agent calls `request_workspace_upgrade`) →
grant check → orchestrator provisions a sandbox pod → agent builds + connects a
`RemoteBackend` and seeds it from the virtual prefix (both backends live) →
`swap_backend()` → `resetup_tools_for_backend()` → persist the new tier → emit
`workspace_upgrade.complete`. The next turn has shell/git.

The work breaks into five slices. **S1 + S2 + S3 are the MVP** — a user-triggered
upgrade working end-to-end; S4 adds the grant gate, S5 the agent-initiated offer.
S1 and S2 are independent and can land in parallel.

**S1 — `resetup_tools_for_backend()` (the unblocker).** Re-derive the toolset
after a swap so the new tier's tools appear, without clobbering live session
state. `swap_backend` (`persistent_session.py:867`) rebuilds `ShellManager` but
leaves `self.tools` / `llm_with_tools` and the now-stale
`tool_context.shell_manager` (set at `:552`) pointing at the old, lite-filtered
set. New method that (a) refreshes `tool_context.shell_manager =
self.shell_manager`; (b) recomputes `tool_names` → `filter_tools_by_backend(new
backend)` (`registry.py:149`) → `load_tools` → `apply_description_overrides` /
`apply_instruction_enforcement` → `_bind_tools()`. **Do not** simply re-call
`_setup_tools` — it resets `session_task_manager` (`:539`) and rebuilds
`tool_context`, dropping in-flight state; instead factor the shared
name-resolution + load block (`:566-632`) out of `_setup_tools` so both call it.
The per-turn `get_current_tools()` re-read (`persistent_graph.py:537`) then
exposes the new tools on the next turn with zero further plumbing.
- *Verify:* unit — a session on a fake `supports_shell=False` backend has no
  shell/git tools; after `swap_backend(fake_shell_backend)` +
  `resetup_tools_for_backend()`, shell/git appear in `self.tools` /
  `llm_with_tools` and `tool_context.shell_manager` is the new one.
- *Deps:* none. Independently shippable + unit-testable.

**S2 — Orchestrator sandbox-upgrade endpoint.** Provision a sandbox pod for an
existing lite thread, idempotently. New internal `POST
/api/agents/threads/{thread_id}/upgrade-to-workspace {target_tier}` mirroring
`agent_upgrade_thread_to_vm` (`main.py:12849`): for `sandbox`, assert
`container_provisioner.is_available`, short-circuit if
`metadata.workspace_container` is already provisioning/ready, else
`container_provisioner.create_workspace(WorkspaceOwner.session(thread_id))` (the
eager-session pattern at `main.py:13300`) and record
`threads.metadata.workspace_container`. Client method
`request_thread_workspace_upgrade(thread_id, target_tier)` in
`orchestrator_client.py` (mirror `request_thread_vm_upgrade`).
- *Verify (k3d):* `curl -XPOST .../upgrade-to-workspace` from the orchestrator
  pod → a `ws-thread-<id>` pod + PVC spawns → `_poll_workspace_ready` returns a
  `{"backend":"sandbox","remote":{…}}` config (already implemented,
  `persistent_app.py:4467`).
- *Deps:* none (parallel with S1).

**S3 — Agent handler: seed + swap + retool + persist (the MVP).**
`_handle_workspace_upgrade(ws, target_tier)` generalizing `_handle_vm_upgrade`
(`persistent_app.py:4500`): `request_thread_workspace_upgrade` (S2) →
`_poll_workspace_ready` → build `RemoteBackend` from the returned `remote` block
with **`sudo_action="freeze"`** (not VM's `"allow"` — the sandbox keeps its sudo
gate, which preserves the existing sandbox→VM escalation for free) → **seed**
(S3a) → `swap_backend` → `resetup_tools_for_backend` (S1) → **persist tier**
(S3b) → emit `.started`/`.complete`/`.failed` like the VM path. Wire WS dispatch
`method == "upgrade-to-workspace"` → this handler (mirror the `upgrade-to-vm`
dispatch at `persistent_app.py:2292`).
- **S3a — seed helper.** Shared `seed_workspace(src_backend, dst_backend)`:
  recursively walk src, `write_file`/`mkdir` into dst, verify count/size before
  returning. `list_dir` is one level only (`virtual.py:238`), so add a
  `WorkspaceBackend.walk()` (default = recursive `list_dir`;
  `VirtualWorkspaceBackend` overrides to the flat `ObjectStore.list(prefix)`).
  Reusable by the worker path. The agent already holds the object-store creds
  (per [[no_workspace_agent_mode]] internal creds never leave the agent
  process), so this is a pure in-process copy — no orchestrator transfer.
- **S3b — persist tier.** Write `metadata.config_override.workspace.backend =
  "sandbox"` so `ensure_session_workspace` (`session_provisioner.py:49`) stops
  no-op'ing on this thread and the suspend/resume/reconcile lifecycle engages.
- Minimal Cockpit: an "Upgrade workspace" action that sends the WS message + a
  provisioning/complete/failed indicator (reuse the vm_upgrade toast pattern).
- *Verify (k3d):* from a running virtual session, trigger the upgrade and assert
  (1) a pod spawns, (2) a file written in `virtual` (`notes/plan.md`) exists in
  the new pod workspace, (3) the next turn the agent can run a shell command,
  (4) the WebSocket/conversation never dropped. Watch agent logs for "Backend
  swapped" + "Loaded N tools".
- *Deps:* S1 + S2.

**S4 — Grant enforcement.** Run the shared upgrade-authorization gate (§4.4
Sec-1) before provisioning: `capability_grants.evaluate` on the post-upgrade
config + the kill-switch, fail-closed, mirroring `_enforce_dispatch_grants`. For
`sandbox` this passes by default (the PDP gates only `vm` and explicitly declared
tool flags); refuse with `workspace_upgrade.failed {reason}` only when the PDP
actually violates (e.g. a `tools.shell` cap on this user/expert) or the
kill-switch is off.
- *Verify:* a `sandbox` upgrade for a default user succeeds (ungated); a user
  with an admin `tools.shell` restriction is refused with a clear reason; a
  `vm`-target upgrade without `vm_workspace` is refused.
- *Deps:* S3; the shared gate is §4.4 Sec-1.

**S5 — Agent-initiated offer (HITL trigger).** A lite agent has no shell tool to
"attempt", so give it an explicit request path. (a) **Freeze vocab:** add
`workspace_upgrade_required` (carries `target_tier`, `reason`); generalize the
consume check (`persistent_graph.py:1513`) to fire on both it and
`vm_upgrade_required`; rename the callback to `on_workspace_upgrade_needed` (keep
`on_vm_upgrade_needed` as an alias); `_loop_on_*` (`persistent_app.py:3271`)
broadcasts `workspace_upgrade.needed {target_tier, reason}`; the nats_bridge map
(`nats_bridge.py:643`) gains `workspace_upgrade.needed → session.workspace_upgrade`.
(b) **New tool** `request_workspace_upgrade(reason)` in a non-execution category
(`core`) so it survives `filter_tools_by_backend` on lite tiers; it calls
`context.request_freeze({freeze_type:"workspace_upgrade_required",
target_tier:"sandbox", reason})` — it *requests*, never flips the tier. Register
in `TOOL_REGISTRY`, expose only on lite tiers. (c) **Cockpit** offer banner on
`workspace_upgrade.needed` whose accept sends the S3 `upgrade-to-workspace`
message.
- *Security (§4.4):* this is the HITL *offer* — the agent requests, a human
  approves. The trigger is the agent's explicit tool call, not ingested content
  flipping the tier, so it stays clear of the CVE-2025-53773 self-escalation
  class. Auto-grant (no click) is deferred to Phase 4.
- *Verify:* steer a virtual session toward needing a shell → agent calls
  `request_workspace_upgrade` → offer appears → accept → shell appears.
- *Deps:* S3 (+ S4 for the gate).

**Out of these slices** (later phases): auto-grant without a click + warm-pool
claim + `none`-tier (Phase 4); `virtual → vm`, which reuses the S3 handler with
`target_tier="vm"`, the existing VM provisioner, and the operator-approval gate
(Phase 2).

### 4.3 Worker-job flow (v2)

Worker jobs are harder than sessions for one reason: the LangGraph checkpoint is
**pod-local and non-portable** (AsyncSqliteSaver at `WORKSPACE_PATH/checkpoints/
job_<id>.db` on the agent pod's emptyDir, `agent.py:546`), and resume rebuilds
todos/phase/messages *from that checkpoint* (`route_entry` keys on `initialized`,
then `restore_todo_state`, `graph.py:2821/2842`). A resume on a different pod
finds an empty emptyDir and re-routes to `init_workspace` — i.e. restarts. The
two fallbacks that carry a normal job across pods (Gitea re-clone, SSH phase
snapshot) are **both unavailable to a lite job** (git off, no SSH target). So a
naive freeze→re-dispatch would lose all progress.

**Key insight — do it in-process, like the session.** The agent process running a
lite job is alive (`--loop`) and holds *both* the live `virtual` backend and the
local checkpoint, and it rebuilds tools + graph per job-run (`agent.py:543-577`).
If the agent upgrades **within the same process** — provision a sandbox, seed it
from the still-attached virtual backend, swap the `WorkspaceManager` backend, and
re-`ainvoke` from the local checkpoint — nothing crosses a pod boundary. No
re-dispatch, no checkpoint move, no fencing, no `_resume_job_on_agent` gap. This
is the worker analogue of `swap_backend()`.

**Strategy by target tier:**
- **`virtual → sandbox` → in-process** (auto-grant; W1 + W2). Recommended v1.
- **`* → vm` → freeze → operator-approve → re-dispatch** — VM is operator-gated,
  so it *must* pause for approval and can't stay in-process; this is the existing
  `upgrade_job_to_vm` machinery (W3).
- **`virtual → vm` → compose**: `virtual → sandbox` in-process (W1), then the
  existing `sandbox → vm` sudo path. Avoids a direct lite→vm re-dispatch and its
  non-portable-checkpoint problem entirely.

**W1 — In-process `virtual → sandbox` upgrade (the worker MVP).** In the agent's
job handler, intercept a `workspace_upgrade_required` freeze (target `sandbox`)
returned by `ainvoke` — instead of reporting `paused`, run an in-process upgrade:
provision (W2) → poll → build a sandbox `RemoteBackend` (`sudo_action="freeze"`)
→ `seed_workspace(virtual_backend, sandbox_backend)` (§4.2 S3a, both live) → swap
`self._workspace_manager` backend + `_setup_job_tools()` (re-derives tools for
the new backend, `agent.py:543`) → rebuild graph → re-`ainvoke` from the same
local checkpoint (`route_entry` → `restore_todo_state` continues, now with
shell). On provision failure / grant denial, fall back to reporting `paused`.
Reuses the freeze vocab + `request_workspace_upgrade` tool (§4.2 S5) and the seed
helper (§4.2 S3a).
- *Touches:* `src/agent.py` (freeze interception + the in-process upgrade
  sub-sequence; `_setup_job_tools` + graph rebuild already exist).
- *Verify (k3d):* a `virtual` worker job calls `request_workspace_upgrade`;
  assert no re-dispatch, a `workspace-<id>` pod spawns, files are seeded, the job
  continues **in the same agent pod** with a shell, and completes. Logs show
  "Backend swapped" with no `/job/resume`.
- *Deps:* §4.2 S3a + §4.2 S5 + W2.

**W2 — Orchestrator: provision-workspace-for-a-running-job (gated).** New `POST
/api/jobs/{id}/provision-workspace {target_tier}`: grant check (fail-closed,
mirror `_check_vm_permission` `main.py:2574` + `_enforce_dispatch_grants`),
`container_provisioner.create_workspace(WorkspaceOwner.job(job_id))`, record
`context.workspace_container`. Status stays `processing` (no pause). The agent
polls `get_job_workspace` for readiness (the job-side analogue of
`_poll_workspace_ready`).
- *Verify:* call for a virtual job → pod spawns, `context.workspace_container`
  recorded, status unchanged.
- *Deps:* none (parallel with W1).

**W3 — Re-dispatch path for operator-gated `* → vm` (existing machinery,
generalized; deferred).** VM targets must pause for operator approval, so they
use freeze→approve→re-dispatch, not W1. **Prefer the composition** (`virtual →
sandbox` in-process, then the existing `sandbox → vm` sudo →
`vm_upgrade_required` path at `main.py:7606`) — it reuses two working paths and
never re-dispatches a lite checkpoint. A *direct* lite→vm re-dispatch, if ever
needed, additionally requires: generalize `upgrade_job_to_vm` to a
tier-parameterized endpoint; teach `_job_needs_sandbox` (`main.py:2435`) to honor
`context.workspace_upgrade.requested`; **fill the `_resume_job_on_agent`
sandbox/lite-source injection gap** (`main.py:1818` injects VM + lite, no sandbox
block); survive the non-portable lite checkpoint via **same-pod pinning** (record
the original `agent_id` on the freeze, dispatcher prefers it) or phase-boundary
re-init; and **fence** with a monotonic token. Caveat: a lite-origin job upgraded
to sandbox in-process has git off, so enabling git/Gitea at the sandbox-upgrade
step is what lets a subsequent `sandbox → vm` cross-pod resume reconstruct.
- *Verify:* virtual job → in-process sandbox (W1) → sudo attempt → existing
  `vm_upgrade` approve → VM, checkpoint intact.
- *Deps:* W1 (composition path) + the existing container→VM machinery.

**Worker MVP = W1 + W2.** It mirrors the session path (in-process swap, seed from
the live backend) and inherits none of the re-dispatch / checkpoint / fencing
complexity — which is confined to the operator-gated VM path (W3).

### 4.4 Capability & security model

Auto-upgrade is only safe to a tier that is *itself* a strong isolation
boundary. Mapping the two-axis model (§3.6) to our tiers:

| Tier | Filesystem | Egress | Privilege | Grant |
|---|---|---|---|---|
| `virtual` | object-store ops only | LLM endpoint only | none | always (ungated) |
| `sandbox` (pod) | workspace dir + tmp; deny agent's own config | **default-deny allowlist** | **non-root** | **auto-upgrade on coding intent — OK** |
| `vm` | broad | broad | **sudo/root** | **keep `can_use_vm` + operator approval** |

Decisions:
- **`virtual → sandbox` may auto-grant; `virtual → vm` stays operator-gated.**
  The sandbox tier is a real boundary (non-root + default-deny egress), so
  *starting* it is a reversible, contained capability grant — not a privileged
  action. VM adds root + broad egress (all three legs of the lethal trifecta)
  and keeps the existing `can_use_vm` + operator-approval gate + global
  kill-switch.
- **The upgrade trigger must derive from *human* intent, never from ingested
  content.** A tool result or fetched file that can flip the tier is a
  self-escalation exploit (CVE-2025-53773). Tool-derived signals may only
  *offer* an upgrade (HITL banner); auto-grant is reserved for user-message /
  explicit-control signals.
- **Egress stays default-deny in the new sandbox** (cuts the exfiltration leg).
  Reuse the agent-egress NetworkPolicy from [[no_workspace_agent_mode]] §9.1.
- **Re-run `capability_grants.evaluate`** on the post-upgrade config, exactly as
  dispatch does: `vm` requires the `vm_workspace` grant (+ operator approval);
  `sandbox` is **not** gated by the PDP (it gates only `vm` and explicitly
  declared tool flags like `tools.shell`), so it inherits dispatch-time semantics
  with no new rule (`src/core/capability_grants.py:135`).
- **Protect the tier-control surface from agent writes**, and make the
  **kill-switch a live downgrade** (tear down the pod/VM, revert to `virtual`),
  not just a block on new upgrades.

#### Implementation slices

**v1 security = Sec-1 + Sec-2 + Sec-4** (authorize, inherit egress, protect the
control surface) — the minimum for Phase 1–3 to ship safely with HITL approval.
Sec-3 (kill-switch downgrade) is a fast-follow; Sec-5 (auto-grant) is Phase 4.

**Sec-1 — Shared upgrade-authorization gate (server-side, fail-closed).** One
helper that **both** the session endpoint (§4.2 S4) and the worker
`provision-workspace` endpoint (§4.3 W2) call before provisioning, consolidating
their grant checks. Run `capability_grants.evaluate` on the **post-upgrade merged
config**, exactly as `_enforce_dispatch_grants` does at dispatch
(`capability_grants.py:123`), plus the kill-switch (Sec-3):
- `target=vm`: the fragment's `workspace.backend='vm'` trips the `vm_workspace`
  requirement (`evaluate` line 135); `vm` also keeps `_check_vm_permission` +
  operator approval (`main.py:2574`).
- `target=sandbox`: the PDP does **not** gate `backend='sandbox'` — it gates only
  `vm` and explicitly declared tool flags (`tools.shell` / `browser` /
  `delegation`, lines 135–150, fired only when the config *declares* them). So a
  sandbox upgrade **passes by default**, matching "sandbox is the ungated default
  tier", unless the user/expert carries a restriction (e.g. a `tools.shell` cap)
  that the upgraded config would also trip at dispatch. **No new grant key, no
  sandbox-specific rule** — identical to dispatch-time enforcement, re-run at
  upgrade time.
- *Verify:* unit — `evaluate({workspace:{backend:'vm'}}, grants_without_vm)`
  violates; `evaluate({workspace:{backend:'sandbox'}}, default_grants)` is clean.
  Endpoint 403 for `vm` without `vm_workspace`; `sandbox` succeeds by default.
- *Deps:* replaces the grant-check stubs in §4.2 S4 and §4.3 W2.

**Sec-2 — Egress posture on the upgraded pod (inherit + confirm).** The on-demand
sandbox pod created by an upgrade goes through `container_provisioner.create_workspace`,
which labels it `srw.io/network-tier` via `_resolve_network_tier(work_id, kind)`
(`container_provisioner.py:679`) → the per-project egress NetworkPolicy
(`helm/templates/workspace-network-policy.yaml`) binds it automatically, default
tier strictest. So an upgraded pod **inherits the same egress posture as a native
sandbox by construction — no new policy code.** Work: (a) verify a
`virtual → sandbox` upgraded pod actually carries the tier label and is bound by
the policy; (b) for the Phase-4 auto-upgrade case, ensure the deployment's
*default* tier is genuinely default-deny — the open enablement follow-up from
[[no_workspace_agent_mode]] §9.1 (`agent_egress_networkpolicy_enablement.md`).
- *Verify (k3d):* upgraded pod has `srw.io/network-tier`; from inside it the
  LLM/registry/git endpoints are reachable and arbitrary egress is blocked.
- *Deps:* none (inherits the existing system).

**Sec-3 — Upgrade kill-switch + live downgrade.** Add a `workspace_upgrade`
`system_settings` kill-switch mirroring `vm_workspaces` (`get_system_setting`,
fail-open on a missing row, blocks everyone incl. admins), enforced inside Sec-1.
The *live downgrade* half — flipping it off reverts already-upgraded entities:
sessions `swap_backend` back to a virtual backend + `resetup_tools_for_backend`
(§4.2 S1) + `release_workspace` the pod; jobs pause + `release_workspace`
(re-dispatch as `virtual`). Reuse `release_workspace` (`container_provisioner.py:349`),
`suspend_thread_workspace` (`workspace_suspension.py:446`),
`_archive_and_cleanup_workspace` (`main.py:2612`).
- *Verify:* flip the switch → new upgrades 403; an existing upgraded session
  loses its pod and reverts to `virtual` (tools re-filter to lite) with the
  conversation intact.
- *Note:* session downgrade is clean (swap back); an in-flight **job** downgrade
  is necessarily coarse (pause, not a mid-turn revert).
- *Deps:* Sec-1; session swap/retool (§4.2 S1/S3); worker (§4.3 W1).

**Sec-4 — Tier-control-surface protection (invariant + guard test).** Realize
decision #5 + the CVE-2025-53773 lesson as a *verifiable invariant*: the agent
can only **request** an upgrade — the `request_workspace_upgrade` tool (§4.2 S5)
sets `freeze_data`, never writes `workspace.backend` / `context.workspace_upgrade`
/ grants / the kill-switch; `config_override` stays frozen (`postgres.py:1370`)
and tier/grants/kill-switch live in DB / `system_settings` the agent can't reach;
the seed copy writes only into the workspace tree, never a path the sandbox reads
as config at boot (config comes from the dispatch payload, not workspace files).
- *Verify:* guard test — a job whose agent writes a `config`-looking file into
  its workspace still resolves the same backend/grants on upgrade; the request
  tool's only effect is one `freeze_data` entry. HITL approval (v1) means a
  malicious request still needs a human click.
- *Deps:* audits §4.2 S5's blast radius.

**Sec-5 — Auto-grant with provenance (Phase 4, deferred).** Only after Sec-1–4:
let `virtual → sandbox` upgrade *without* a human click when the trigger is
provably human-authored. Tag each upgrade signal with provenance
(`user_message` | `explicit_control` | `tool_result`) and auto-grant only the
first two; `tool_result`-derived requests stay HITL-offer-only (the CVE class).
Optional classifier as defense-in-depth (not the boundary — Anthropic's auto-mode
classifier misses ~17% of overeager actions). Maps to open question #1.
- *Verify:* a `tool_result`-derived request only offers (no auto-grant); a
  `user_message`-derived one auto-grants (sandbox only, never vm).
- *Deps:* Sec-1–4; the trigger plumbing (§4.2 S5 / §4.3 W1).

### 4.5 What stays out of scope

- **Process-level migration** (CRIU / Firecracker snapshot of the running
  agent) — explicitly rejected per §3.2.
- **`none` → heavy** — `none`/ScratchBackend has no durable anchor (tmpdir dies
  with the pod), so it would require exfiltrating files from the live pod first.
  Defer; v1 supports `virtual → sandbox/vm`. (Easy follow-on: in-process
  agent-side copy works for `none` sessions too, since the agent holds the
  tmpdir — only worker `none` jobs are genuinely blocked.)
- **Downgrade** (sandbox → virtual) beyond the kill-switch teardown — not needed
  for v1.
- **`virtual → vm` auto-grant** — VM stays operator-gated (§4.4).

---

## 5. Integration points

**Reuse as-is:**
- `swap_backend()` runtime hot-swap — `persistent_session.py:867`
- `_handle_vm_upgrade` orchestration + WS `started`/`complete`/`failed` events — `persistent_app.py:4500`
- `request_freeze`/`consume_freeze_request` + freeze→state plumbing — `context.py:472`, `graph.py:3603`
- `on_vm_upgrade_needed` → broadcast signal chain — `persistent_graph.py:1508`, `persistent_app.py:3271`, `nats_bridge.py:643`
- `get_current_tools()` per-turn re-read — `persistent_graph.py:537`
- `filter_tools_by_backend` + `supports_shell`/`supports_file_tools` gate — `registry.py:149`, `workspace_backend.py:445`
- `capability_grants.evaluate` PDP — `capability_grants.py:123`
- `upgrade_job_to_vm` control-flow template + dispatcher provision-wait-resume loop — `main.py:7606`, `:3144`
- `_poll_workspace_ready`, `container_provisioner.create_workspace(WorkspaceOwner.session)` — `persistent_app.py:4417`, `main.py:13300`
- `VirtualWorkspaceBackend` / `ObjectStore` read+list (for the seed copy) — `virtual.py:88`, `object_store.py:49`
- Agent egress NetworkPolicy — [[no_workspace_agent_mode]] §9.1

**New code:**
1. `resetup_tools_for_backend()` on `PersistentSession` (the ~10-line blocker).
2. Generalized `workspace_upgrade_required` freeze type + `context.workspace_upgrade` namespace; `completion.py` routing + `_format_freeze_notification` case.
3. Agent-side `_handle_workspace_upgrade(target_tier)` generalizing `_handle_vm_upgrade` for the sandbox target.
4. Orchestrator `request_thread_workspace_upgrade` + `POST /api/jobs/{id}/upgrade-workspace`.
5. Agent-side **seed copy** (virtual prefix → new backend) with verify-before-flip.
6. Teach `_job_needs_sandbox` to honor `context.workspace_upgrade.requested`; add the **sandbox injection to `_resume_job_on_agent`**.
7. Grant re-check on upgrade; persist new tier to `threads.metadata`.
8. Cockpit: upgrade offer banner + explicit "Upgrade workspace" control + provisioning state.
9. *(v2)* Worker in-process `virtual → sandbox` upgrade (agent-side freeze interception + re-`ainvoke`, §4.3 W1) + orchestrator `provision-workspace` endpoint (W2). Re-dispatch / fencing only for the operator-gated VM path (W3).
10. *(v3)* Warm-pool claim (depends on [[workspace_warm_pool_and_async_sessions]]).

---

## 6. Phasing

- **Phase 1 — Sessions, `virtual → sandbox`, HITL.** §4.2 slices S1–S5 (MVP =
  S1 + S2 + S3; S4 grant gate; S5 agent-initiated offer). Explicit-user-action /
  agent-offer trigger, no auto-grant yet. Delivers the headline use case. Lowest
  risk: in-process, portable state, single writer.
- **Phase 2 — Sessions, `virtual → vm`.** Add the VM target to
  `_handle_workspace_upgrade` (reuses `request_thread_vm_upgrade`), keep the
  operator-approval gate. Mostly wiring once Phase 1 exists.
- **Phase 3 — Worker jobs.** §4.3 slices W1–W3 (MVP = W1 + W2: in-process
  `virtual → sandbox` swap mirroring the session — no re-dispatch, no checkpoint
  move). The operator-gated VM path (W3) reuses the existing container→VM
  machinery; `virtual → vm` composes W1 + the existing sudo→VM path, so no
  lite-checkpoint re-dispatch is ever required.
- **Phase 4 — Accelerate + automate.** Warm pool for ~instant sandbox upgrades;
  **auto-upgrade `virtual → sandbox` on coding intent** — only after the trigger
  is provably human-authored (not ingested content), egress stays default-deny,
  and the kill-switch downgrades live sessions. `none`-tier support.

---

## 7. Open questions

1. **Auto-upgrade trigger provenance.** How do we prove the "start coding" signal
   is human intent and not injected content before allowing an *auto*-grant
   (vs an offer)? Likely: only user-message-derived or explicit-control signals
   auto-grant; tool-result-derived signals can only *offer*.
2. **Size cap on the lite tier.** Daytona's rehydration penalty scales with
   size — what's the max prefix size we'll copy-down synchronously before the
   upgrade feels slow? Above it, fall back to a progress UI / lazy mount.
3. **Worker in-process re-invoke robustness** (§4.3 W1). Re-`ainvoke`-ing the
   graph in-process from the local checkpoint after a backend swap: re-entrancy
   hazards (freeze-marker cleanup, in-flight aux tasks, reusing the checkpoint
   `thread_config`), and orphan recovery if the agent pod dies *mid-upgrade*
   (job `processing` with a half-provisioned `workspace_container`) — does the
   existing orphaned-job auto-pause cover it, or does the partial pod leak?
4. **Warm-pool shapes.** Which 1–2 sandbox shapes are common enough to pre-warm
   without burning idle cost?
5. **Datasource re-materialization.** Repository datasources are *rejected* on
   lite tiers ([[no_workspace_agent_mode]] §7). On upgrade, do we now clone the
   repos the user attached but couldn't use, and where in the flow?
6. **Cockpit UX for the in-flight wait** — reuse the `vm_upgrade` banner pattern
   and the async-session "type while infra spins up" buffering from
   [[workspace_warm_pool_and_async_sessions]]?

---

## 8. References

- Codebase precedent: [[vm_backend]] (container→VM), [[no_workspace_agent_mode]]
  (lite tiers + egress policy + capability gate), [[workspace_pvc_backed_migration]]
  (harness/execution pod split; "brain in Postgres"), [[ephemeral_workspaces]]
  (snapshot S3 layout), [[tool_permission_tiers]].
- External (load-bearing): Daytona sandbox lifecycle (cold/hot tiering);
  Coder prebuilt-workspaces (warm-pool claim); GitHub Codespaces lifecycle
  (logical hibernate/wake); K8s in-place pod resize 1.33 (live resize limits);
  mountpoint-s3 SEMANTICS (S3-FUSE caveats); Kleppmann fencing tokens
  (split-brain); Meta "Agents Rule of Two" + OWASP LLM08 Excessive Agency
  (escalation gating); GitHub Copilot CVE-2025-53773 (self-reconfiguration RCE);
  Anthropic Claude Code permission modes + sandboxing (two-axis model,
  protected paths, classifier 17% false-negative). Full source URLs in the
  research notes that produced this doc.
