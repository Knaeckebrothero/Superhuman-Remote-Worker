# Workspace Tier Upgrade — test coverage map (what's covered vs what isn't)

Companion to `docs/features/workspace_tier_upgrade.md`. Records exactly what is
verified, by which mechanism, and — the point of this file — **what could not be
covered yet**, why, and how to close each gap. Last updated 2026-06-20 (after the
Phase 2 `virtual → vm` build + k3d orchestrator-side smoke).

Feature scope built so far: **session** `virtual → sandbox` (Phase 1) and
**session** `virtual → vm` (Phase 2). Worker jobs (Phase 3) and the `none` tier
are not built.

---

## 1. Covered

### 1.1 Unit tests (run in CI; mocked dependencies)

| Area | File / selector | What it asserts |
|---|---|---|
| Tool re-derivation after swap (S1) | `tests/test_persistent_session.py::TestResetupToolsForBackend` | shell/git readmitted after `swap_backend` + `resetup_tools_for_backend`; `request_workspace_upgrade` exposed on virtual, gone on sandbox |
| Seed copy (S3a) | `tests/test_workspace_seed.py` | `walk()` + `seed_workspace()` copy-down, per-file verify-before-flip |
| Upgrade endpoint logic (S2/S4/Phase 2) | `tests/test_thread_endpoints.py::TestAgentUpgradeToWorkspace`, `::TestAgentUpgradeToVm` | `bogus → 400`; sandbox provisions; idempotency; **vm delegates to the vm path**; **403 when the grant gate denies, on both the delegated and direct endpoints** |
| Grant PDP (Sec-1) | `tests/test_capability_grants.py` | `evaluate({workspace.backend:'vm'})` violates without the `vm_workspace` grant; `sandbox` clean by default; shell-restricted principal refused |
| Agent-offer freeze + callback (S5) | `tests/test_persistent_graph.py` | generalized callback fires on `workspace_upgrade_required`; `on_vm_upgrade_needed` alias promotion |
| Control-tool loading (S5) | `tests/test_tool_registry.py` | `request_workspace_upgrade` loads without a todo/workspace manager (lite session) |
| Cockpit slash + offer | `cockpit/.../persistent-chat.service.spec.ts` | `/upgrade-workspace` → `sandbox`; `/upgrade-workspace vm` → `vm` |

### 1.2 k3d end-to-end (cluster `srw`, ctx `k3d-srw`, ns `srw`)

- **Phase 1 `virtual → sandbox`, MVP (S1+S2+S3)** — verified 2026-06-20 (cockpit
  UI): `virtual` session → `/upgrade-workspace` → pod spawned → 49 files seeded
  virtual→pod over SFTP → backend hot-swapped → 44→55 tools → agent ran a shell
  command reading a seeded marker → conversation never dropped.
- **Phase 2 `virtual → vm`, orchestrator side** — verified 2026-06-20 (curl from
  the orchestrator pod, `X-Internal-Key: $MCP_INTERNAL_KEY`, against a real
  admin-owned thread):
  - `target_tier:bogus` → `400`; `target_tier:vm` → `503` "VM provisioning not
    available" (routed past the 400, gate passed for the admin owner, graceful);
    direct `/upgrade-to-vm` → `503` likewise.
  - kill-switch `system_settings.vm_workspaces={enabled:false}` → `403` "globally
    disabled" on **both** `/upgrade-to-workspace {vm}` and `/upgrade-to-vm`,
    *before* the `503` (fail-closed ordering); sandbox unaffected → `200`;
    removing the row restores `503`.
  - This run **caught + fixed a real bug** (see §3.1).

---

## 2. NOT covered — and why

> The single structural reason most of these are open: **k3d has no KubeVirt and
> no NATS** (`vm_provisioner.is_available` is False — `NATS_URL` and
> `VM_CONTROLLER_URL` are empty, no VM controller, no KubeVirt CRDs). So no real
> VM can boot on k3d; anything downstream of "a VM exists" is unreachable here.

| # | Gap | Why uncovered | Where it lives (unexercised code) |
|---|---|---|---|
| G1 | **Full `virtual → vm` boot** (provision VM → seed → swap → sudo shell, conversation intact) | k3d has no VM infra; needs the **dev cluster** (KubeVirt + NATS present there) | end-to-end |
| G2 | **Agent-side `_handle_workspace_upgrade` vm branch** | only runs once a VM is provisioned (G1) — the `vm` poll/build/seed/swap path never executes on k3d | `src/api/persistent_app.py::_handle_workspace_upgrade` (the `target_tier=="vm"` branch: `_poll_vm_ready` → `remote` block mapping; `sudo_action="allow"`; post-swap `shell_manager.sudo_action="allow"`) |
| G3 | **Real grant gate on a live thread** is only covered by the k3d smoke, **not** by unit tests | `orchestrator/main.py` isn't importable under the test deps, so `test_thread_endpoints.py` exercises a **replicated** endpoint with an **injected** `grant_gate` stand-in — the real `_enforce_workspace_upgrade_grants` (incl. the asyncpg-UUID handling) is never unit-run | `orchestrator/main.py::_enforce_workspace_upgrade_grants` |
| G4 | **vm-tier session resume / suspend / reconcile** (S3b persists `metadata.config_override.workspace.backend=vm`) | no live vm session has existed to suspend/resume; orthogonal to the in-place upgrade (which never drops the conversation) | session lifecycle (`session_provisioner.py`, suspension/reconcile) for a `vm`-tier thread |
| G5 | **`none → sandbox` and `none → vm`** | only `virtual → *` was built + tested; the request tool is *exposed* on `none` and the lite-boot fix covers `none`, but the path is unverified | same handlers, `none`/ScratchBackend source |
| G6 | **S5 agent-offer round-trip on a cluster** (agent calls `request_workspace_upgrade` → HITL offer → accept → shell appears) | Phase-1 leftover; unit-tested in pieces but never walked end-to-end live | S5 chain (tool → freeze → `workspace_upgrade.needed` → cockpit accept) |
| G7 | **Worker-job upgrades (Phase 3, W1–W3)** | not built | n/a (design only) |
| G8 | **Sandbox S4 refusal path live** (a shell-restricted principal refused `403` on a sandbox upgrade) | the k3d smoke only exercised the *vm* refusal (kill-switch) and the *sandbox pass*; the sandbox-refusal branch is unit-only | `_enforce_workspace_upgrade_grants` sandbox + a `tools.shell`-capped owner |
| G9 | **Egress posture on an upgraded vm/sandbox pod** (Sec-2: `srw.io/network-tier` label bound by NetworkPolicy) | not asserted in any run | `container_provisioner` labelling + `helm/templates/workspace-network-policy.yaml` |

---

## 3. Findings from the smoke that shaped coverage

### 3.1 Bug caught: grant gate crashed on a live thread `user_id`

`_enforce_workspace_upgrade_grants` passed `thread["user_id"]` — an
`asyncpg.pgproto.pgproto.UUID` object — straight into `postgres_db.get_user`,
which does `UUID(user_id)` (catching only `ValueError`). Result:
`AttributeError: 'asyncpg...UUID' object has no attribute 'replace'` → HTTP `500`
on **both** the vm path and the (never-live-tested) **Phase-1 sandbox S4** path.
Fixed by coercing `owner_id = str(owner_id)` in the gate (matches the
`str(user["id"])` convention elsewhere).

**Coverage lesson (→ G3):** the mocked unit helpers return a *string* `user_id`,
so they structurally cannot catch an asyncpg-type mismatch. Only a live DB
returns the UUID type. Until `orchestrator/main.py` is importable for tests (or
the gate is extracted into an importable module), **the k3d/dev smoke is the only
regression guard for the real gate** — re-run §4 after any change to the gate or
to `get_user`.

---

## 4. How to close the gaps — dev-cluster VM boot smoke (closes G1, G2, partially G4)

Run on the **dev cluster** (VM provisioner available there — see memory
`reference_srw_prod_private_deploy` / `VM backend on dev — WORKING`; ctx `main`,
ns `superhuman-remote-worker`). The orchestrator-side curl checks from §1.2 are
infra-independent and can be re-run on any cluster as a regression for G3/G8.

1. Create (or pick) a `virtual` session owned by a user with `can_use_vm=true`
   (or an admin), with the global `vm_workspaces` switch enabled.
2. In the session, send `/upgrade-workspace vm`.
3. Assert, in order:
   - orchestrator delegates to the VM path and provisions a VM (`metadata.vm`
     status progresses; a KubeVirt VM appears);
   - the agent's `_handle_workspace_upgrade` vm branch polls `_poll_vm_ready`,
     builds the `RemoteBackend`, **seeds** the virtual files into the VM, and
     `swap_backend`s — watch agent logs for "Backend swapped" + "Re-derived N
     tools";
   - `workspace_upgrade.complete` arrives; the conversation never dropped;
   - the next turn the agent can run a shell command **and `sudo` is allowed**
     (the post-swap `shell_manager.sudo_action="allow"` — i.e. sudo does *not*
     freeze, unlike sandbox).
4. (G4, optional) suspend → resume the session; assert it re-resolves the `vm`
   tier from `metadata.config_override.workspace.backend`.

Regression curls for G3/G8 (any cluster), from the orchestrator pod:

```sh
T=<thread_id>; B=http://localhost:8085/api/agents/threads/$T
# routing: bogus → 400, vm → 503 (or, with VM infra, provisioning)
curl -s -o- -w '\n%{http_code}\n' -XPOST $B/upgrade-to-workspace \
  -H "X-Internal-Key: $MCP_INTERNAL_KEY" -H 'Content-Type: application/json' \
  -d '{"target_tier":"vm"}'
# grant gate fail-closed: set system_settings.vm_workspaces={"enabled":false}
#   → expect 403 BEFORE any availability/provisioning; then DELETE the row to restore.
```

> When running the kill-switch test, **restore state afterward**: delete the
> `vm_workspaces` row, and if a sandbox test provisioned a `ws-thread-*` pod,
> delete the pod and clear the thread's `metadata.workspace_container`.
