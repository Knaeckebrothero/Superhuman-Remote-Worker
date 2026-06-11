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

**Status:** Draft. Design discussed and v1 scope agreed 2026-06-10. Not started.

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
  orchestrator. Decision deferred to S1 implementation; default to rclone
  for provider continuity with `RcloneMountSpec`.
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

**Excluded**, with enforcement:

- **Shell** — not registered, *and* the ShellManager local-libtmux fallback
  is hard-disabled (capability check, not config absence).
- **Browser** — not registered; the local in-pod fallback is removed
  entirely per `docs/issues/remove_local_browser_fallback.md`.
- **Git** — deferred, see §8.
- **Repository datasources** — rejected at dispatch (§4). Note while
  verifying: `process_datasources()` contains a *local subprocess* `git
  clone` branch (`src/core/datasource_setup.py` ~:617-691); the session path
  filters repository datasources out before calling it
  (`persistent_app.py` ~:890-895) and clones via
  `GitManager.clone(backend=...)` on the remote workspace instead. Audit
  whether the worker path can reach the local branch and fold it into the
  capability-not-inference cleanup.

Prompts: a lite instruction/persona variant that doesn't reference shell,
git, or workspace conventions that don't exist in this tier
(`config/templates/`, resolved per the existing matrix machinery).

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
prefix, checkpoint markers at phase boundaries. Exposed as a
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
2. **Local-browser fallback removal** —
   `docs/issues/remove_local_browser_fallback.md` (filed 2026-06-10).
3. **ShellManager libtmux hard-off** — lite backends declare
   `supports_shell=False` and the local-tmux degradation must not be
   reachable (the bare-metal dev posture it served is deprecated with the
   Compose stack).
4. **`datasource_setup` local-clone audit** — §7.

## 10. Later phases (out of v1 scope)

- **Change tracking / diff review** (§8 successor) — phase review + cloud
  approve/revert.
- **Pooled browser service** — the workspace `browser-exec` daemon deployed
  as a claimable shared pool with workspace-grade NetworkPolicy; gives lite
  agents real browsing. See also `docs/issues/egress_proxy_pool.md`.
- **Python executor** — never in the agent pod (credentials, principle 1).
  Warm-pool sandbox pods (claim → stage referenced files from the prefix →
  execute → write outputs back → recycle) or ephemeral one-shot pods.
- **Cloud surfaces for lite agents** — attaching the user's cloud
  (Nextcloud/Drive) as additional explicit-op remotes through the same
  `RcloneMountSpec` payload, with the hydration-guard thinking applied.
- **Search/index layer** — shared service, also serves full-mode mounts
  ([[rclone_cloud_mount]] §10).

## 11. Implementation slices

- **S1 — agent side (~1 day):** `VirtualWorkspaceBackend` (+ unit tests
  against the same contract the FS test backend satisfies),
  `ScratchBackend`, backend factory keyed on the payload `workspace.mode` at
  the shared bootstrap seam (`app.py` / `dual_app.py` /
  `persistent_session.py`), libtmux hard-off.
- **S2 — orchestrator side (~0.5-1 day):** `workspace.backend` enum
  extension, skip-provisioning branches in job dispatch + session prepare,
  payload emission (S3 prefix + creds from deployment config),
  repository-datasource validation, Helm values for the object-store
  endpoint (decide local-dev story here).
- **S3 — config/profile (~0.5 day):** lite expert/profile config (tool
  lists, `git_versioning: false`), lite instruction template, docs.
- **S4 — egress NetworkPolicy (~0.5 day, parallel):** independent PR,
  protects full mode too.

~2-3 days to a k3d-verified slice (S1-S3); S4 parallel.

## 12. Acceptance criteria (k3d smoke)

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

## 13. Open questions

- **Credential scoping:** v1 = internal bucket + per-job prefix with a
  deployment-level key held in the agent process (matches the existing
  internal-creds trust model), vs per-job STS/scoped credentials (cleaner
  tenancy, more machinery — ties into multi-tenancy M1). Lean: prefix +
  shared internal key for v1, STS when MinIO/IAM is wired for it.
- **rclone subprocess vs boto3** for the backend's op layer (S1 bench;
  interface identical either way).
- **Local k3d object store:** dev-only MinIO vs WebDAV remote against the
  bundled cloud (S2).

## 14. Decision summary

Two new tiers behind the existing `workspace.backend` selector: `virtual`
(explicit-op rclone/S3 file tools, no FUSE, no workspace pod) and `none` (no
file tools at all). Web + database + KB/memory/todo tools come along
unchanged; shell/browser/git/repository-datasources are excluded with
capability-based enforcement, not just configuration. Git review returns
later as backend change tracking, which also powers the user-facing cloud
diff/approve/revert. Prerequisites: agent-pod egress NetworkPolicy and
removal of the local execution fallbacks.
