# Workspace Tier Upgrade — test coverage map (what's covered vs what isn't)

Companion to `docs/features/workspace_tier_upgrade.md`. Records exactly what is
verified, by which mechanism, and — the point of this file — **what could not be
covered yet**, why, and how to close each gap. Last updated 2026-06-20 (after the
Phase 3 worker `virtual → sandbox` build + k3d end-to-end smoke).

Feature scope built so far: **session** `virtual → sandbox` (Phase 1),
**session** `virtual → vm` (Phase 2), and **worker job** `virtual → sandbox`
(Phase 3, in-process — unit-green + **k3d end-to-end verified**). The `none` tier
and the worker `* → vm` path (Phase 3 W3) are not built.

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
| Worker endpoints + gate (W2) | `tests/test_job_workspace_upgrade.py::TestProvisionJobWorkspace`, `::TestGetJobWorkspaceStatus`, `::TestJobGrantExtraction` | provision routing (`vm`/unknown → 400, missing → 404, gate-deny → 403 **before** provisioning, unavailable → 503, idempotent on in-flight, sandbox provisions for the **`job`** owner kind); `workspace-status` `port`→`pod_port` mapping + `none` default; the **job-wrapper extraction** (top-level `config_override` column, JSON-string tolerant) vs the real `evaluate` (sandbox passes, shell-restricted owner refused) |
| Worker in-process upgrade (W1) | `tests/test_job_workspace_upgrade.py::TestAgentJobWorkspacePoller`, `::TestAgentInprocessUpgradeGuards`, `::TestStreamingInterception` | `_poll_job_workspace_ready` (ready→remote block / failed→None / none→None / pending→creating→ready poll-through); upgrade guards (already-shell→noop True, no-client→False, provision-refused→False, poll-timeout→False before any backend build); the **streaming-interception decision** (normal completion + non-matching freeze → no upgrade & states unchanged; `workspace_upgrade_required`/sandbox → upgrade once + resume primed; upgrade-failure → freeze surfaced, no resume) |
| W1 trigger exposure | `tests/test_job_workspace_upgrade.py::TestW1TriggerExposure` | `request_workspace_upgrade` is a registered `core` tool; `_setup_job_tools` injects it **lite-only** (survives `filter_tools_by_backend` on a no-shell backend; absent on a shell-capable one). It's in no config — without this injection a lite worker can't trigger an upgrade |
| Failure-path routing | `tests/test_completion_endpoint.py::TestDetermineJobStatus::test_workspace_upgrade_required_paused` | a surfaced (failed-upgrade) `workspace_upgrade_required` freeze → `paused` |

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
- **Phase 3 `virtual → sandbox`, WORKER job — full end-to-end** — verified
  2026-06-20 (closes G7). Two parts:
  - *Orchestrator curls* (from the orchestrator pod, against a real admin job):
    `provision-workspace {vm}` → `400`; missing job → `404`; `workspace-status`
    before → `none`; `provision-workspace {sandbox}` → `200` provisioning (the
    live-DB job-wrapper gate passed — exercised `get_user` UUID-coercion +
    `config_override` read with no asyncpg crash) → pod spawned →
    `workspace-status` reached `ready` with `pod_ip`/`pod_port` (the `port` →
    `pod_port` mapping).
  - *Full agent flow* (real virtual job `fa144821`, gemini-3.5-flash): booted
    lite (capability gate dropped `run_command`/git/browser, `request_workspace_upgrade`
    exposed) → the agent called the tool → freeze intercepted **in process**
    (same agent throughout; the job never left `processing` for `paused` — no
    re-dispatch, no `/job/resume`) → `Seeded 49 file(s) from
    VirtualWorkspaceBackend to RemoteBackend` → `run_command` re-derived on the
    new backend → "Workspace upgraded to sandbox; graph rebuilt … resuming in
    process" → the resumed graph ran `echo SMOKE_OK_PHASE3 && uname -n` on the
    sandbox. **Rigorous proof:** the marker file read off the sandbox pod =
    `SMOKE_OK_PHASE3` + the sandbox hostname (so the shell ran on the upgraded
    pod, not virtual), and `notes/task_rules.md` (created pre-upgrade) survived
    the seed. `job_complete` → `reviewing` (goal achieved).

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
| ~~G7~~ | ~~Worker in-process `virtual → sandbox` full happy path (W1+W2)~~ | ✅ **CLOSED 2026-06-20** — k3d end-to-end smoke ran the whole path on a real virtual job (49 files seeded, upgraded on the same pod with no re-dispatch, ran a shell command on the sandbox; marker proven). See §1.2 and §4.1 | — |
| G7b | **Worker `* → vm` (Phase 3 W3)** | not built (deferred) — composes W1 (in-process `virtual → sandbox`) + the existing operator-gated sudo→VM re-dispatch; no lite-checkpoint re-dispatch needed | n/a (design only); plus the still-◻️ `_job_needs_sandbox` / `_resume_job_on_agent` sandbox-injection items |
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

## 4. How to close the gaps

### 4.1 Worker in-process sandbox smoke (closed G7 ✅ 2026-06-20) — runs on **k3d**

k3d has container provisioning, so the worker `virtual → sandbox` happy path is
fully verifiable here (no dev cluster needed). **Ran green 2026-06-20** (see
§1.2); keep this as the regression recipe. The aim: prove a running lite worker
job upgrades **in the same agent pod** with no re-dispatch.

1. Submit a worker job whose resolved config has `workspace.backend: virtual`
   (a lite job). Steer the task so the agent reaches for a shell — or, simplest,
   give instructions that explicitly tell it to call `request_workspace_upgrade`
   once it needs a real environment.
2. Watch the agent pod logs (`kubectl --context=k3d-srw -n srw logs -l
   srw/managed-by=agent-provisioner -f`). Assert, in order:
   - the agent posts `POST /api/jobs/{id}/provision-workspace` and a
     `workspace-<id>` pod spawns (`kubectl … get pods -l srw/job-id=<id>`);
     the job stays `processing` (no row flip to `paused`, **no `/job/resume`**);
   - "Seeded N file(s) into upgraded workspace" → "Workspace upgraded to sandbox;
     graph rebuilt with shell/git tools — resuming in process";
   - the job continues and a subsequent tactical step runs a shell command on the
     pod (e.g. reads a file seeded from the virtual prefix), then completes.
3. Negative/fallback: with the container provisioner unavailable (or a
   shell-restricted owner), assert the upgrade request returns `503`/`403`, the
   agent surfaces the freeze, and `completion.py` routes the job → `paused`
   (not `failed`).

Infra-independent regression curls (any cluster, from the orchestrator pod):

```sh
J=<job_id>; B=http://localhost:8085/api/jobs/$J
# routing: vm → 400 (operator-gated elsewhere); sandbox → provisioning (or 503 w/o K8s)
curl -s -o- -w '\n%{http_code}\n' -XPOST $B/provision-workspace \
  -H "X-Internal-Key: $MCP_INTERNAL_KEY" -H 'Content-Type: application/json' \
  -d '{"target_tier":"vm"}'        # expect 400
curl -s -o- -w '\n%{http_code}\n' $B/workspace-status \
  -H "X-Internal-Key: $MCP_INTERNAL_KEY"   # status + pod_ip/pod_port once ready
```

> Restore state afterward: if the smoke provisioned a `workspace-<id>` pod, delete
> it and clear the job's `context.workspace_container`.

### 4.2 Dev-cluster VM boot smoke (closes G1, G2, partially G4)

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
