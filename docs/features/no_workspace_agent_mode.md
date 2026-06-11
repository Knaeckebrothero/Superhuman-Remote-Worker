---
tags:
  - feature
  - architecture
  - workspace
  - agents-at-scale
  - security
aliases:
  - no-workspace mode
  - lite agents
  - virtual workspace backend
related:
  - "[[rclone_cloud_mount]]"
  - "[[main_cloud_abstraction]]"
  - "[[browser_workspace_executor]]"
  - "[[builder_to_sessions_consolidation]]"
  - "[[job_cloud_export]]"
---

# No-Workspace Agent Modes — `virtual` and `none` workspace tiers

**Status:** **v1 + v1.1 implemented and k3d-validated; committed on `develop`,
not yet pushed/deployed to dev (2026-06-11).** Design agreed 2026-06-10;
per-slice implementation + validation detail follows.
Prerequisite hardening complete (serial order: prereqs before S1/S2 —
building the lite backends first would arm the very fallbacks the prereqs
remove): §9.2 browser fallback **removed 2026-06-11** (+133/−1431, agent
image drops Playwright/Chromium entirely; also completed
[[browser_workspace_executor]] Phase 4). §9.3 libtmux hard-off **done
2026-06-11** (ShellManager is delegation-only and raises without a
shell-capable backend; agent image drops tmux/libtmux). §9.4 clone audit
**done 2026-06-11** — finding: the worker path WAS reachable (unfiltered
datasources, clone into agent CWD); local branches removed, unified
backend-only helper (§7). §9.1 egress NetworkPolicy (S4) **implemented
2026-06-11, ships default-off** — policy + values + enablement checklist
(`agent_egress_networkpolicy_enablement.md`); per-deployment enablement
(verify LLM/Keycloak egress, add carve-outs, stage on dev) is the open
follow-up. All four §9 prerequisites now landed in code.

**S1 (agent side) implemented 2026-06-11** — 177 unit tests green: the
`ObjectStore` seam + `InMemoryObjectStore` (`src/core/backends/object_store.py`),
`VirtualWorkspaceBackend` (prefix-math object-store file ops, `virtual.py`),
`RcloneObjectStore` (the §5 rclone transport, behind the swappable seam,
`rclone.py`), `ScratchBackend` (`none` mode, `scratch.py`), a backend factory
keyed on `workspace.backend` (`factory.py`), config support (`virtual`/`none`
+ `mounts` in loader `WorkspaceConfig`, resume-safe via `asdict`), the
worker + session bootstrap seams (lite branch, git forced off per §8), and
rclone in the agent image. The full WorkspaceBackend contract is exercised
over the in-memory store.

**S2 (orchestrator side) implemented 2026-06-11** — the dispatch/session seams
now skip workspace provisioning for `virtual`/`none` (`_job_needs_sandbox`
short-circuits; the two eager session `create_workspace` sites and
`ensure_session_workspace` skip lite threads), emit the §4 `mounts` payload (one
shared `_inject_lite_workspace_config` helper drives both job dispatch and
`_send_session_attach`; credentials sourced from deployment env, in-flight only,
never persisted), reject `repository` datasources on a lite tier (HTTP 400 at
`create_job`, fail-job at dispatch), and carry Helm values for the object store
(`virtualWorkspace.rclone.*` → `VIRTUAL_WORKSPACE_RCLONE_TYPE/ROOT/CONFIG`;
type/root via ConfigMap, the credential JSON via the bundled Secret). A
contract-roundtrip test feeds the orchestrator-emitted payload straight into the
agent's `create_lite_backend` and round-trips a file, so S1 and S2 are proven to
agree without a cluster. **Local-dev story decided** (§13): dev/k3d uses the
`memory` object store (set in `deployment/values-tilt.yaml`; non-durable,
single-pod, no MinIO dependency); production points `virtualWorkspace.rclone`
at MinIO S3. **Validated on `k3d-srw` 2026-06-11** (§12): `virtual` and `none`
jobs each run as a single agent pod with no workspace pod/PVC, the agent boots
the lite backend from the live dispatch payload (`Lite workspace backend ready
(backend=virtual, no workspace pod)`), a `virtual` job wrote `notes/plan.md`
into its `jobs/<id>/` object-store prefix and completed, and a
`virtual`+`repository`-datasource job was rejected at creation (HTTP 400).

**S3 (capability-gated tools) implemented + validated 2026-06-11** (§11/§12):
tool binding is gated by backend capability — `registry.filter_tools_by_backend()`
drops shell/browser/git when `not supports_shell` and the file tools when
`not supports_file_tools` (a new ScratchBackend flag) — so on plain
`config:default` a `virtual` job dropped all 16 shell/git/browser tools (keeping
files + web) and a `none` job dropped those **plus** all file tools (§12 #4/#6,
verified live). This replaces the originally sketched "lite presets" with
enforcement-by-construction.

**v1.1 Cockpit tier picker implemented + k3d-validated 2026-06-11** (detail in
§10): the Advanced-settings backend selector now offers `virtual`/`none` and
greys the dependent controls to match the S3 gate (git-versioning, the whole
Shell section, browser headless/vision; file-size limits too for `none`; proxy
stays — web egress), with an explanatory hint; `getOverrides()` drops the gated
fragments so the emitted `config_override` stays clean. Validated by `npm run
build` (AOT-clean), vitest (9/9), and a live Playwright drive on k3d (dropdown +
hint + full disable matrix for both tiers). Remaining — all optional, none
blocks shipping: §12 #8 session-view affordance-hiding (code-server/workspace
links), #3 web+SQL smoke, the UI→`config_override`→dispatch round-trip through
the real form, and an optional lite instruction variant.

## 1. Goal

Add two lightweight workspace tiers so agents can run **without a workspace
container**: the agent pod is the only pod.

- **`virtual`** — the agent gets file tools backed by a virtual filesystem
  (rclone/S3 explicit operations, no FUSE, no mount). Files are durable in
  object storage under a per-job/per-thread prefix.
- **`none`** — the agent gets no file tools at all. Deliverables are its final
  output (`freeze_data` / chat), knowledge-base writes, and datasource writes.

Both tiers keep: web research (Tavily-backed), database/datasource tools
(SQL, graph, MongoDB, WebDAV), knowledge base, memory, todos, communication.
Neither tier has: shell, browser, git, or repository datasources.

This is the "simple version" of the agent framework: many agents — RAG
chatbots over a customer database, research/summarize agents, light file
management — need basic file IO and occasional queries, not a POSIX machine
they can install packages on.

## 2. Motivation

- **Cost/scale.** Today every job/session costs an agent pod **plus** a
  workspace pod **plus** a PVC, and the default workspace profile is
  privileged for FUSE (rclone mount). A lite agent is one unprivileged pod
  with no PVC. That is the "run agents at scale" story.
- **Product tiering.** The builder→sessions consolidation
  ([[builder_to_sessions_consolidation]]) explicitly wants a lightweight
  session tier (cloud-only file IO + later a python sandbox).
- **Simplicity.** For workloads that never execute code, provisioning and
  reaping a workspace container is pure overhead and failure surface.

## 3. Design principles

1. **The agent pod stays a control plane.** Typed, fixed-destination clients
   in-pod are fine (S3/rclone file ops, SQL/graph/Mongo clients, Tavily,
   LLM endpoints — all existing precedent). Anything that *executes code* or
   *interprets attacker-influenceable content* (shell, browser, python) does
   not run in the agent pod: the pod holds internal credentials
   (`config_override.env_keys`) and currently has **no NetworkPolicy**.
2. **Capability, not inference.** Tools and managers gate on what the backend
   *declares* (`supports_shell`, future `supports_change_tracking`), never on
   duck-typing like `backend.host is None`. The local-browser fallback removal
   (`docs/issues/remove_local_browser_fallback.md`) and the ShellManager
   libtmux hard-off are part of this feature's prerequisite hardening.
3. **Universal by construction.** One new `WorkspaceBackend` implementation
   behind the existing seam; one dispatch payload variant; both graphs
   (worker `graph.py` and `persistent_graph.py`) are untouched. Tool sets are
   config profiles, as today.

## 4. Modes and configuration

Extend the **existing** selector `config_override.workspace.backend`
(read in `orchestrator/main.py` `_job_needs_sandbox()`, ~:1756-1791):

| Value | Meaning | Provisioning | Agent-side backend |
|---|---|---|---|
| `sandbox` | container workspace (default) | k8s pod + PVC | `RemoteBackend` (SSH) |
| `vm` | VM workspace | NATS/KubeVirt VM | `RemoteBackend` (SSH) |
| `virtual` | **new** — virtual FS | none (S3 prefix only) | `VirtualWorkspaceBackend` |
| `none` | **new** — no FS | none | `ScratchBackend` (internal only) |

Legacy aliases `container`→`sandbox` and `remote`→`vm` already exist; the two
new values follow the same pattern. (`vm` vs `sandbox` is invisible to the
agent — both arrive as SSH endpoints. `virtual`/`none` are new payload
shapes.)

Orchestrator changes (both code paths):

- **Job dispatcher**: for `virtual`/`none`, skip workspace provisioning and
  readiness-wait entirely; emit the lite workspace payload in the dispatch
  request.
- **Session prepare**: same skip; sessions spawn only the session agent pod.
- **Validation**: reject jobs/sessions that combine `virtual`/`none` with a
  `repository` datasource (HTTP 400 with a clear message). Coding workloads
  want a real workspace; that is the tier boundary, not a missing feature.

Payload sketch:

```json
{
  "workspace": {
    "mode": "virtual",
    "mounts": [
      {
        "name": "workspace",
        "rclone_spec": { "type": "s3", "config": { "...": "..." } },
        "prefix": "jobs/<job_id>/",
        "access": "read_write"
      }
    ]
  }
}
```

`rclone_spec` reuses the `RcloneMountSpec` provider contract from
[[rclone_cloud_mount]] / [[main_cloud_abstraction]] — the same shape that
describes FUSE mounts for full workspaces describes explicit-op remotes here.
For `none`, the payload is just `{"workspace": {"mode": "none"}}`.

## 5. `VirtualWorkspaceBackend`

A new `WorkspaceBackend` implementation (the ABC in
`src/core/workspace_backend.py` already treats shell as optional;
`tests/_fs_backend.py` proves the file-op seam works without SSH).

- Implements the abstract surface (read/write/append/exists/list/search/
  mkdir/delete/move/copy/stat/resolve_path/connect) as **explicit rclone
  operations** (`lsjson`, `cat`, `rcat`, `copyto`, `deletefile`, `mkdir`, …)
  against the spec'd remote — rclone as subprocess in the agent pod. A
  boto3-direct implementation is an acceptable alternative if benchmarks
  favor it; the interface is identical and `boto3` is already used by the
  orchestrator. **Resolved (S1): rclone**, built behind a swappable
  `ObjectStore` seam (`src/core/backends/object_store.py`) so the backend's
  logic + contract tests are transport-agnostic and boto3 stays a drop-in
  alternative for the v1.1 latency revisit (§10). Tests run over an
  `InMemoryObjectStore`; production credentials come from the `rclone_spec`
  via `RCLONE_CONFIG_*` env (out of argv).
- **No FUSE, no mount, no privileged pod.** This sidesteps the entire
  `/dev/fuse` + `SYS_ADMIN` + seccomp problem from [[rclone_cloud_mount]]
  §12, and the POSIX-semantics objection to S3 mounts (§11) — explicit ops
  don't need POSIX.
- `supports_shell = False`; no `host` attribute games — see principle 2.
- `resolve_path` is prefix math (escape-proof by construction, same pattern
  as the test backend).
- Internal state files (`plan.md`, `todos.yaml` archives, `datasources.md`,
  `notes/`) flow through the backend like any file → they land in the S3
  prefix, durable and inspectable.
- Workspaces survive pod death by default; GC is a bucket lifecycle policy
  instead of PVC reaping; the cockpit can browse outputs through the same
  credentials later.

Known S3-isms to handle: empty directories don't persist (tolerate, or drop
`.keep` files during structure init); `edit_file` is read-modify-write
without atomicity (single writer per prefix — acceptable); `search_files`
is name/path search first, content search only bounded (this is the
hydration lesson from [[rclone_cloud_mount]] §8 in miniature); single-file
size guard on reads.

**Storage endpoint reality:** prod-private already runs MinIO
(`minio.minio.svc`, used by the cloud object-store wiring in
`helm/values.yaml`), so S3 exists where it matters. Local k3d has no MinIO;
v1 options are a dev-only MinIO (small, optional) or pointing the spec at
any rclone-supported remote (e.g. the bundled cloud via WebDAV) — it's
config, not code. Decide in S2 when wiring values.

**Considered alternative — shared filesystem gateway service.** The original
sketch had a shared backend container handling the virtual filesystems for
all lite agents. Rejected for the file layer in v1: it adds a network hop
and a new SPOF, concentrates every tenant's storage credentials in one
process (a per-agent typed client matches the existing trust model, where
each agent process holds only its own job's keys), and provides nothing a
typed S3 client doesn't. Shared services stay reserved for where they
genuinely pay off — the pooled browser service, the python executor, and
the search/index layer (§10) — which are heavy, stateful, or
isolation-critical in ways plain file IO is not.

## 6. `ScratchBackend` (`none` mode)

The graph's internal consumers (PlanManager, TodoManager archive, optional
`task_brief.md` read, `datasources.md`) expect *a* backend. Rather than
auditing every call site for None-safety, `none` mode constructs an
ephemeral scratch backend — the `tests/_fs_backend.py` pattern promoted into
`src/core/backends/` — rooted in a private tmpdir on the agent pod, with
**no file tools registered over it**.

Precedent: the LangGraph checkpoint (AsyncSqliteSaver) already lives on
agent-local disk, and phase archives are mirrored to the MongoDB audit
trail. Scratch state is disposable by design; durable outputs in `none` mode
are `freeze_data`, KB writes, and datasource writes. This does not violate
the "agent never uses its own filesystem as a workspace" rule in spirit:
nothing agent-driven can touch it — there are no file tools.

## 7. Tool profile (v1)

**Included** (all already workspace-independent, zero new code):

- Web research: `web_search`, `extract_webpage`, `crawl_website`,
  `map_website` — Tavily-transported; the page fetch happens on Tavily's
  infrastructure, so these are not an in-pod SSRF surface. Accepted as the
  v1 web story until the pooled browser service exists.
- Database/datasource tools: SQL, graph (Neo4j), MongoDB, WebDAV — these are
  auto-injected per attached datasource type (see the datasource→tool map in
  `persistent_app.py` ~:900) and run as in-process typed clients.
  **Including them is the no-op; excluding them would be extra code.** This
  is the RAG-chatbot tier: lite agent + attached SQL/graph datasource.
- Core: todos, KB (`kb_write`/`kb_search`), memory, communication,
  delegation (subjobs inherit their own mode).
- File tools (`read_file`, `write_file`, `edit_file`, `list_files`,
  `search_files`, `delete/move/copy/mkdir/stat`) — **`virtual` mode only**.

**Excluded**, enforced by **backend capability** — not by per-config tool
lists (§3.2). `registry.filter_tools_by_backend()` runs at both bind seams
(worker `agent.py`, session `persistent_session.py`) right after
`get_all_tool_names`, and drops — by the backend's declared flags — whatever a
lite tier can't support. So *any* config dispatched onto `virtual`/`none` gets
the right toolset by construction; no lite-specific preset has to remember to
omit anything.

- **Shell** — dropped when `not backend.supports_shell` (False on both lite
  backends); the ShellManager local-libtmux fallback is also hard-disabled
  (§9.3). `run_command`/`shell_read` are simply absent on a lite tier.
- **Browser** — `browser_direct` dropped on the same `supports_shell` gate
  (browser-exec needs the workspace pod); the in-pod fallback was removed
  entirely (§9.2).
- **Git** — `git` tools dropped on the same gate; `git_versioning` is also
  forced off for lite (S1, §8). No git binary or repo exists in this tier.
- **File tools** — dropped for `none` via `not backend.supports_file_tools`
  (a new ABC flag, False on ScratchBackend; §6). `virtual` keeps them.
- **Repository datasources** — rejected at dispatch (§4).
  **Audit done 2026-06-11, finding: the worker path COULD reach the local
  branch** — `agent.py` passed job datasources unfiltered into
  `process_datasources()`, whose subprocess `git clone` branch wrote SSH
  keys/tokens into the agent pod's home and cloned into the agent process
  CWD (`workspace_dir` fell back to `os.getcwd()`; `WorkspaceManager` has
  no such attribute). Fixed: the local branch is deleted; both paths now
  share `clone_repository_datasources()` (datasource_setup.py), which
  requires a shell-capable backend and runs all auth + clone operations on
  the workspace (`write_home_file` + `GitManager.clone(backend=...)`) —
  no local fallback, shell-less backends skip with an error. The session
  path's own local SSH-key else-branch and a dead duplicate
  (`Agent._setup_repository_datasource`) were removed with it. Residuals
  filed: `GitManager`'s own local-subprocess fallback (job-repo flows)
  in [[gitmanager_local_git_fallback]], and the dead legacy
  datasource/proxy code the audit surfaced in
  [[datasource_legacy_dead_code]].

Prompts: with the toolset enforced by capability (above), the default
instructions still *mention* shell/git/workspace conventions a lite tier
lacks — harmless (those tools are absent) but slightly off-key. A lite
instruction/persona variant that omits them is optional follow-up polish
(`config/templates/`, resolved per the existing matrix machinery) — not
required for v1, and deliberately not a parallel set of "lite presets".

## 8. Git: deferred — and why that's safe

Decision (2026-06-10): **no git tools in v1.**

- Git-off is already a first-class, shipped state: every git tool
  runtime-checks `git_mgr.is_active` (`src/tools/git/git_tools.py`
  :104-212), every `GitManager` method guards on it, the phase-boundary
  commits in `src/core/phase.py` (:538, :727, :871) are wrapped in
  `if git_mgr and git_mgr.is_active`, registration comes from config tool
  lists, and `git_versioning: false` (loader) prevents initialization. The
  lite profile sets two config keys and exercises an existing degraded path.
- "Basic git tools" in a tier with no git binary, no repo, and no POSIX
  filesystem would actually mean building a change-tracking subsystem now —
  the only part of v1 that wouldn't be assembling existing pieces.
- `none` mode has no files at all, so v1 strategic review must already work
  without diffs: phase archives (`archive/phase_N_*.yaml` todo outcomes +
  notes), `plan.md`, KB — plus re-reading current files in `virtual` mode.

**Successor (planned, not v1): backend change tracking.** In `virtual` mode
every mutation flows through the backend *by construction* (no shell means
no side-channel writes), so the backend can maintain a complete change
journal: record `(phase, op, path, before/after ref)` on each write, stash
prior versions via S3 object versioning or a `.history/` copy-on-write
prefix, checkpoint markers at phase boundaries. (rclone/S3 is only the
transport here — no rclone feature provides tracking; the tracker is this
journal plus the version stash, owned by us.) Exposed as a
`supports_change_tracking` capability backing the same review-tool names
(status/log/diff); git remains the implementation for full workspaces.

This capability has a **second customer**: the user-facing "what did the
agent change in my cloud" diff view with approve/revert — the journal
provides the changed-path list and the prior-version blobs (revert source),
and the Cockpit already has the Monaco diff-review surface from
[[job_cloud_export]]. Scoping note: that covers `virtual`-mode surfaces,
where the backend mediates writes. Full-mode FUSE mounts bypass the backend
(shell writes straight to the mount), so user-cloud diff/revert for mounted
clouds needs a mount-side mechanism (`rclone --backup-dir`, provider
versioning) — separate design when we get there. A cheap intermediate if
review pressure appears early: an op-log without diffs (`workspace_changes`
read-only tool) is hours of work on top of the backend.

## 9. Security prerequisites and hardening

Ship with (or before) v1:

1. **Agent-pod egress NetworkPolicy** — today the chart's only NetworkPolicy
   is `workspace-network-policy.yaml`; agent pods have none. Same
   `ipBlock`-except pattern as the workspace hardening: allow orchestrator/
   DBs/NATS/object store by selector, kube-dns, explicit carve-outs for LAN
   LLM endpoints (e.g. the `ai.h4ll.app` router), then internet-except-
   RFC1918/link-local/metadata. Valuable independent of lite mode; lite mode
   makes it a prerequisite. BYO datasources pointing at private ranges remain
   a per-tenant tiering question (multi-tenancy M1.D), unchanged.
   **IMPLEMENTED 2026-06-11, ships default-off** (S4):
   `helm/templates/agent/network-policy.yaml` + `agent.networkPolicy` values.
   Single policy (agents are control plane, not a tenant boundary — no
   tiers), selecting both `srw-agent`/`srw-persistent-agent` via
   `matchExpressions In`. Allows the in-cluster deps by podSelector
   (orchestrator 8085, NATS 4222, pg/pgvector 5432, neo4j 7688, mongo 27017,
   nextcloud 80 / opencloud 9200, workspace SSH 30022 + CDP 9222), kube-dns,
   and internet 80/443/22 minus the `except` CIDRs; `extraEgress` carries
   per-deployment RFC1918 LLM/Keycloak/cloud carve-outs. External
   LLM/Keycloak/Tavily work over the 443 wildcard with no config. Default-off
   because flipping it on against a live cluster with an RFC1918 LLM endpoint
   would break model access — **enablement is a per-deployment checklist**:
   `docs/issues/agent_egress_networkpolicy_enablement.md` (discovery recipe +
   stage-on-dev-first). Helm-lints clean in both CI scenarios.
2. **Local-browser fallback removal** —
   `docs/issues/remove_local_browser_fallback.md` (filed 2026-06-10).
   **DONE 2026-06-11**: local path deleted, `browser_exec` errors loudly
   without a workspace, agent image ships no Chromium, regression guard
   (no `browser_use` import under `src/`) in the test suite.
3. **ShellManager libtmux hard-off** — lite backends declare
   `supports_shell=False` and the local-tmux degradation must not be
   reachable (the bare-metal dev posture it served is deprecated with the
   Compose stack).
   **DONE 2026-06-11**: the local libtmux path is deleted — ShellManager is
   pure delegation and raises without a shell-capable backend; call sites
   gate on `supports_shell` (no `which tmux` fallback); sudo/blocked-command
   gating stays agent-side, ahead of delegation; libtmux + tmux dropped from
   the agent image; regression guard (no `libtmux` import under `src/`) in
   the test suite. The shared sentinel/stall helpers remain in
   `shell_manager.py` for `RemoteBackend`.
4. **`datasource_setup` local-clone audit** — §7.
   **DONE 2026-06-11** — worker path was reachable (see §7 finding);
   local-clone branches removed, unified backend-only
   `clone_repository_datasources()` helper + capability gate + tests.

## 10. Roadmap beyond v1

Dependency-ordered; v3 and v4 are independent of each other and can swap on
observed demand.

- **v1.1 — polish (hours-to-days, demand-driven):**
  - `workspace_changes` op-log tool (changed paths per phase, no diffs) if
    phase-review pressure appears before v2 lands.
  - **Cockpit tier picker — DONE 2026-06-11.** The Advanced-settings accordion's
    workspace `backend` selector (`advanced-accordion.component.ts`) now offers
    `virtual` and `none` alongside `sandbox`/`vm`. Selecting a lite tier greys
    the dependent controls — git-versioning, the whole Shell section, browser
    headless/vision; `none` also greys the file-size limits — and shows an
    explanatory hint, and `getOverrides()` stops emitting the gated
    `shell`/`browser`/`git_versioning` fragments so the emitted `config_override`
    stays clean. This mirrors the server-side capability gate (S3) in the UI
    (no permission gating — lite tiers are lighter than `sandbox`, available to
    all). en/de i18n + 4 spec tests; `npm run build` AOT-clean. **Validated live
    on k3d via Playwright 2026-06-11** (logged into the running cockpit pod over
    the live-synced `ng serve`): Create-Job → Advanced → Workspace lists all four
    backends with resolved labels; selecting `virtual` greys git-versioning + the
    whole Shell section + browser headless/vision and shows the hint, while file
    limits + proxy stay enabled; `none` additionally greys the file limits and
    switches the hint. Remaining Cockpit piece is §12 #8 (session-view affordance-
    hiding: code-server/workspace links) — and the UI→`config_override`→dispatch
    round-trip through the real form (form behaviour verified; submit not yet driven).
  - Revisit rclone-subprocess vs boto3 with production latency numbers.
- **v2 — change tracking & cloud diff review (~1 week):** the §8 successor.
  `supports_change_tracking` journal + version stash; status/log/diff
  review tools for strategic phases in `virtual` mode; user-facing diff +
  approve/revert for virtual surfaces reusing the [[job_cloud_export]]
  Monaco review UI. (Diff/revert for full-mode FUSE-mounted clouds stays a
  separate design — those writes bypass the backend.)
- **v3 — python executor (~1-1.5 weeks):** the "occasional python tools"
  from the original pitch; completes the lite tier for data/RAG workloads.
  Never in the agent pod (§3.1). Warm-pool sandbox pods: claim → stage
  referenced files from the job prefix → execute → write outputs back →
  recycle; ephemeral one-shot pods are the simpler fallback shape.
- **v4 — pooled browser service (~1 week):** the workspace `browser-exec`
  daemon deployed as a claimable shared pool with workspace-grade
  NetworkPolicy; gives lite agents real interactive browsing beyond Tavily
  extract. Interacts with the egress/IP-reputation work
  (`docs/issues/egress_proxy_pool.md`).
- **v5 — cloud surfaces for lite agents (~2-3 days):** attach the user's
  cloud (Nextcloud/OpenCloud/Drive) as additional explicit-op remotes
  through the same `RcloneMountSpec` payload, with hydration-guard limits.
  Pairs naturally with v2 (diff/approve on exactly those surfaces).
- **Search/index layer (own track):** shared service serving lite and
  full-mode mounts alike ([[rclone_cloud_mount]] §10); scoped separately.

## 11. Implementation slices

- **S1 — agent side (~1 day): DONE 2026-06-11.** `VirtualWorkspaceBackend`
  (+ contract tests over the in-memory store, the same contract the FS test
  backend satisfies), `ScratchBackend`, an `ObjectStore` seam +
  `RcloneObjectStore` transport, a backend factory keyed on
  `workspace.backend` (`src/core/backends/factory.py`), config (`virtual`/
  `none` + `mounts`), the worker (`agent.py`) and session
  (`persistent_session.py`) bootstrap seams (lite branch; git forced off per
  §8), rclone in the agent image, libtmux hard-off (done in §9.3). 177 unit
  tests; not yet exercised end-to-end (needs S2 + a real object store).
- **S2 — orchestrator side (~0.5-1 day): DONE 2026-06-11.** `workspace.backend`
  enum extension (shared `LITE_BACKENDS`, imported not re-declared),
  skip-provisioning branches in job dispatch (`_job_needs_sandbox`) + session
  prepare (eager `create_workspace` sites + `ensure_session_workspace`), payload
  emission (per-owner prefix + creds from deployment config, in-flight only) via
  the shared `_inject_lite_workspace_config`, repository-datasource validation
  (400 at create, fail-job at dispatch), Helm values for the object-store
  endpoint (`virtualWorkspace.rclone.*`). Local-dev story decided: `memory`
  store in dev/k3d (`values-tilt.yaml`), MinIO S3 in prod. Unit + contract-
  roundtrip tests green; **validated on k3d 2026-06-11** (§12 — #1/#2/#5/#7
  pass; #4/#6 pass once S3's capability gate landed).
- **S3 — capability-gated tools (~0.5 day): DONE 2026-06-11.** Reframed from
  the originally sketched "lite presets" (which would have relied on every
  preset author trimming the tool lists): tool binding is now gated by
  *backend capability*. `registry.filter_tools_by_backend()` at both bind
  seams drops `shell`/`browser_direct`/`git` when `not supports_shell`, and the
  `workspace` file tools when `not supports_file_tools` (a new `WorkspaceBackend`
  flag, False on ScratchBackend); `git_versioning` is already forced off for
  lite (S1). Plus the `workspace.backend` schema enum (`virtual`/`none`) and
  unit tests (`tests/test_lite_tool_gating.py`). Enforcement-by-construction:
  any config on a lite tier gets the right toolset. **Validated on k3d
  2026-06-11** (§12 #4/#6). Deferred (optional polish): a lite instruction
  variant whose prose matches the trimmed toolset.
- **S4 — egress NetworkPolicy (~0.5 day, parallel):** independent PR,
  protects full mode too. **DONE 2026-06-11 (ships default-off)** — see §9.1;
  per-deployment enablement tracked in
  `agent_egress_networkpolicy_enablement.md`.

~2-3 days to a k3d-verified slice (S1-S3); S4 parallel.

Ordering: S1 and S2 can proceed in parallel once the §4 payload contract is
frozen; S3 lands last; S4 and the local-browser fallback removal
(`docs/issues/remove_local_browser_fallback.md`) are independent and can
start immediately. v1 requires no Cockpit changes — the tier is selected via
`config_override`/expert preset; the picker is v1.1 (§10).

## 12. Acceptance criteria (k3d smoke)

**Smoke run on `k3d-srw` 2026-06-11 — S1–S3 validated end-to-end** (driven via
the orchestrator internal API; dev `memory` object store): ✅ **#1** (both
`virtual` and `none` ran as a single agent pod, no workspace pod/PVC; the agent
booted the lite backend from the live dispatch payload — `Lite workspace backend
ready (backend=virtual, no workspace pod)` — with files under the `jobs/<id>/`
prefix), ✅ **#2** (a `virtual` job wrote `task_brief.md` + `notes/plan.md`
through `VirtualWorkspaceBackend` and completed; deliverable enumerated),
✅ **#4** (the S3 capability gate, on plain `config:default`, dropped all 16
shell/git/browser tools on `virtual` and all 29 shell/git/browser **+ file**
tools on `none` — `Backend capability gate dropped N tool(s)`; web + datasource
+ file (virtual) tools kept), ✅ **#5** (HTTP 400 at creation with the
actionable message), ✅ **#6** (`none` ran on `ScratchBackend`, completed, with
**zero** file tools bound), ✅ **#7** (the auto-spawned scholar — a `sandbox`
job — still got its workspace pod). ⛔ **#3** (web + SQL datasource) not run;
⛔ **#8** (Cockpit affordances) is v1.1 — the workspace **tier picker** (the
`virtual`/`none` selector + dependent-control greying) landed 2026-06-11 (§10);
the session-view affordance-hiding (code-server/workspace links) remains.

> Environment caveat (not the feature): on the dev cluster only a *chat* model
> was configured, so *summarization/auxiliary* LLM calls fall back to
> `OPENAI_API_KEY=not-needed` → 401 and the small model loops; jobs still
> complete. Configure the summarization + auxiliary models to run cleanly.

1. A `virtual` session/job starts with **exactly one pod** (agent; no
   workspace pod, no PVC) and reaches ready.
2. `write_file` → `read_file` → `list_files` round-trip lands under the
   job's object-store prefix (verify with `rclone lsjson` / `mc ls` against
   the bucket).
3. `web_search` and an attached SQL datasource query both succeed in the
   same lite session.
4. Shell, browser, and git tools are absent from the bound toolset in both
   phases; forcing the dispatch path proves the libtmux/browser fallbacks
   are unreachable (clear error, no local execution).
5. A job submitted with `workspace.backend: virtual` + a `repository`
   datasource is rejected at creation with an actionable message.
6. A `none` job completes end-to-end with deliverables in `freeze_data`/KB;
   no file tools were bound.
7. Regression: a default `sandbox` job and a session on the same deploy
   behave unchanged.
8. Cockpit: a lite session shows no workspace-pod affordances (code-server /
   workspace links absent or hidden) and otherwise renders normally.

## 13. Open questions

- **Credential scoping:** v1 = internal bucket + per-job prefix with a
  deployment-level key held in the agent process (matches the existing
  internal-creds trust model), vs per-job STS/scoped credentials (cleaner
  tenancy, more machinery — ties into multi-tenancy M1). Lean: prefix +
  shared internal key for v1, STS when MinIO/IAM is wired for it.
- ~~**rclone subprocess vs boto3** for the backend's op layer~~ — **resolved
  (S1): rclone**, behind the swappable `ObjectStore` seam (§5). boto3 stays a
  drop-in alternative if the v1.1 latency revisit favors it.
- ~~**Local k3d object store:** dev-only MinIO vs WebDAV remote against the
  bundled cloud (S2).~~ — **resolved (S2): the `memory` object store** in
  dev/k3d (`deployment/values-tilt.yaml` sets `virtualWorkspace.rclone.type:
  memory`). It's a non-durable, in-process store: a `virtual` job/session runs
  as a single pod and round-trips files within the agent's lifetime, with no
  MinIO dependency — enough for the §12 smoke. A dev MinIO or WebDAV-against-
  bundled-cloud remains a drop-in (`type`+`config`) for when durability or
  external inspection (`rclone lsjson`/`mc ls`) is wanted. Production uses
  `type: s3` against MinIO.

## 14. Decision summary

Two new tiers behind the existing `workspace.backend` selector: `virtual`
(explicit-op rclone/S3 file tools, no FUSE, no workspace pod) and `none` (no
file tools at all). Web + database + KB/memory/todo tools come along
unchanged; shell/browser/git/repository-datasources are excluded with
capability-based enforcement, not just configuration. Git review returns
later as backend change tracking, which also powers the user-facing cloud
diff/approve/revert. Prerequisites: agent-pod egress NetworkPolicy and
removal of the local execution fallbacks.
