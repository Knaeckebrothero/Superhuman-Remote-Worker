---
tags:
  - feature
  - architecture
  - orchestrator
  - agent
  - strategy
  - platform
  - memory
related:
  - "[[agent_open_source_split]]"
  - "[[mcp]]"
  - "[[ephemeral_workspaces]]"
  - "[[no_workspace_agent_mode]]"
  - "[[agent_memory_overhaul]]"
  - "[[memory_light]]"
  - "[[dual_mode_agent]]"
  - "[[agent_lifecycle]]"
aliases:
  - platform for agents
  - agent platform
  - foreign harness hosting
  - services MCP
---

# Platform for Agents — running foreign harnesses on SRW

> SRW could host *foreign* agent runtimes (Claude Code, Claude SDK, Hermes, OpenClaw) on its orchestrator + workspace provisioning + services, instead of only its own built-in agent. This is the dual of [[agent_open_source_split]] (our agent on someone else's control plane); here it's someone else's agent on *our* control plane. The play sidesteps the "everything-app" breadth trap — we stop competing on coding UX and become the substrate other harnesses sit on — and it turns cross-domain memory capture into a byproduct of being that substrate. The isolation needed to do it safely is already at the environment boundary and is harness-agnostic. The hard parts are **not** the tools; they're the lifecycle-contract decoupling, a stateful-services surface that doesn't exist yet, multi-tenant security, and adoption.

**Status:** Concept / feasibility exploration. Filed 2026-06-23. Grounded in a code-archaeology pass (file:line anchors below); no implementation.

---

## 1. The idea

From a working note:

> We could become a platform for agents. We provide the services for agents (OpenClaw, Claude SDK, Hermes). Everyone just needs a container that supports the orchestrator API. We just need to provide a universal standard that everyone can hook into.

Decomposed, "platform for agents" is three separable layers, each at a different feasibility:

1. **Compute / sandbox-as-a-service** — SRW provisions an isolated workspace; the foreign harness runs inside it. (e2b / Daytona / Modal shape.)
2. **Orchestration / lifecycle** — the orchestrator *drives* the foreign harness through a job lifecycle (dispatch → run → complete → freeze → heartbeat).
3. **Stateful services** — the foreign harness calls SRW's memory / KB / graph / cloud as network services (this is the differentiated layer, and the one that feeds the cross-domain-memory thesis in §2).

The note's framing collapses these into one and assumes the bottleneck is the **tool layer** (e.g. "`write_file()` has a lot of protection logic"). The code says otherwise — see §3.3.

## 2. Strategic context — why this, and why now

**Background — the tension that motivates this.** The recurring worry behind this whole line of thinking: system engineering eats the calendar and starves work on the individual agent. The useful split is **moat-infra** (workspace isolation, orchestration, multi-tenancy, scale — the "offer scalability" half of the goal, and a largely one-time architectural cost) versus **toil-infra** (operational papercuts and prod-grade ceremony — on a thesis / small-team risk surface, defer or cut ruthlessly). The failure mode is toil-infra masquerading as moat-infra and quietly eating the week; platform-for-agents is attractive partly because it keeps effort on moat-infra (the substrate) instead of on UX surfaces a small team can't win. (The thread began by comparing SRW against Hermes — Nous Research's single-process, self-improving agent harness — and concluding it isn't a competitor but a reference for the single-agent inner loop.)

The motivating thesis (see [[agent_memory_overhaul]], [[memory_light]]): **the durable, un-commoditized moat is cross-domain memory** — an agent that knows not just one repo but your habits, colleagues, company, and your *other* projects. Memory *algorithms* are table stakes and hit diminishing returns fast; the unserved gap is that context is siloed **across tools** (coding here, notes there, quick questions on mobile), and "the agent simply didn't have the fact" is where the 20–30% wins are. Those wins are not ML-gated.

Corollary on where ML actually pays: generic learned-harness policies — hallucination / context-rot classifiers, "when to compact," "which memory to inject" — race the base-model curve and usually lose (the next model release absorbs them). The defensible ML target is **domain-specific verification on proprietary data** we have and the base model does not — citation correctness, FINIUS financial-document grounding — not generic detectors. Heuristics first (the [[agent_memory_overhaul]] reranker + relative gate already moved R@5 0.2→1.0 with no training); reach for trained policies only where a heuristic visibly plateaus *and* the model isn't closing the gap on its own.

The strategic problem is **capture**: to accumulate cross-domain memory you must have presence in each domain, and HCI reality is brutal (users won't wire up integrations, won't read tutorials, won't switch off a tool that already works). Three ways to get there, with their failure modes:

- **Substrate behind one tool** (e.g. an MCP memory server behind Claude Code only) — low friction, but a **capture ceiling**: you only ever see coding context, not the note or the mobile question. Doesn't deliver *cross-domain*.
- **Everything-app** (one harness good at coding + notes + questions + mobile) — captures broadly, but the **breadth trap**: a solo/small team cannot be best-in-class on every surface, and HCI brutality means lazy users punish your *weakest* surface and bounce back to the incumbent. Laziness is *stickiness*, and stickiness favours the incumbent you're trying to replace.
- **Platform for agents** (this doc) — you don't build the UXes; you host the harnesses that already won them. Cross-domain capture becomes a **byproduct** of being the substrate multiple harnesses sit on. This is the truest form of the thesis: it resolves the breadth trap by not competing on UX at all.

The distinction that makes these options coherent rather than contradictory: **you don't need to win the full *task* in every domain — only *capture* in every domain, and full-task in exactly one wedge.** Those are very different bars. A frictionless mobile quick-capture client can beat "re-open Gemini" *at capture* without beating it at chat; full-task best-in-class is reserved for the one domain where incumbents are weakest and our edge is real (research / knowledge-work / citations — not coding, where Claude Code is too strong). Platform-for-agents is the limiting case: capture happens through *every* harness that runs on us, so we never have to win a full-task UX at all.

The trade is real: platform-for-agents swaps the breadth-trap risk for (a) a multi-tenant code-execution + ops burden and (b) an **adoption** problem — "it runs fine on my laptop" is the competitor. See §6.

**Relationship to [[agent_open_source_split]].** That doc is the same agent↔orchestrator boundary viewed from the opposite side: open-source *our* agent so a third party drives it with a minimal orchestrator. Its claim — "the boundary is already an HTTP contract with no shared code/secrets/DB, mostly documentation not refactoring" — is true *when both sides are SRW's own components*. It does **not** contradict §3.1's finding that a *foreign* agent faces SRW-specific payload semantics: our agent is a generic execution engine drivable by a thin control plane, but a foreign agent still has to learn (or have stubbed) our payload schema. The two findings together are the full picture.

## 3. Grounded findings — how coupled are we today?

From a code-archaeology pass over the dispatch path, the MCP server, and the tool layer.

### 3.1 The dispatch / lifecycle contract (the keystone)

The **transport** is clean HTTP, but the **payload semantics are deeply SRW-shaped**.

- Dispatch: the auto-assign dispatcher builds a `JobStartRequest` and POSTs it to the agent pod at `http://{pod_ip}:{pod_port}/job/start` (`orchestrator/main.py` ~2063–2088; model in `src/api/models.py` ~243–328, with an orchestrator-side twin at `orchestrator/main.py` ~4278–4318). Agent receiver: `src/api/app.py` `start_job_from_orchestrator` (~800–869), returns `202 Accepted` and runs the job on a background task.
- The payload carries SRW-internal concepts a foreign runtime cannot use without adaptation:
  - `resolved_config` — SRW's resolved YAML (phases, tools, model/LLM config, templates).
  - `datasources` — resolved connection blobs typed to SRW's datasource kinds (Postgres / Mongo / Neo4j / WebDAV).
  - `context` — SRW keys like `graft_output_path`, `vm`, `queued_replies`, `verification_target`.
  - `repositories` — Gitea-shaped `{role: jobs|source|reference, name}`.
- Completion (agent → orchestrator): `POST /api/jobs/{id}/complete` (`orchestrator/main.py` ~9352) with `JobCompleteRequest{should_stop, goal_achieved, error, freeze_data}` (~4320–4328). `freeze_data` *types* (`job_complete`, `budget_exceeded`, `vm_upgrade_required`) are meaningful only to the orchestrator's own features (hardcap, sudo gate, VM provisioning).
- Heartbeat: `POST /api/agents/{id}/heartbeat` (~14101) with a `metrics` dict that includes SRW-specifics (e.g. `aux` auxiliary-LLM health). Readiness via `/ready`; agents self-register on first heartbeat or are pre-registered by the provisioner.

**Assessment:** a foreign runtime can speak the HTTP, but would stub ~80% of the semantics, and loses the rich features (datasources, freeze/sudo, VM upgrade) unless it integrates with SRW concepts. Driving a foreign harness *through* the job lifecycle needs a **minimal-contract mode** (orchestrator strips SRW assumptions) or a **per-harness adapter**. This is the bulk of the work and is larger than the tool question.

### 3.2 The existing MCP surface is control-plane only

`orchestrator/mcp/server.py` exposes a **read/action proxy to the orchestrator REST API**, not a stateful-services gateway:

- Read: `list_jobs`, `get_job`, `get_audit_trail`/`get_audit_bulk`, `get_chat_history`, `get_todos`, `get_graph_changes`, `get_job_summary`, `search_audit`, git tools (`list_job_commits`, `get_job_diff`, `get_job_file`…), `list_agents`, `list_models`, `test_datasource`.
- Action: `approve_job`, `resume_job_with_feedback`, `cancel_job`, `pause_job`, `create_job`, `delete_job`, `assign_job`.
- Auth: `X-MCP-Token` (`McpTokenVerifier`) + optional OAuth bridge (see [[mcp_oauth_bridge]]); token scope (`user` / `project:<uuid>` / `all`) is passed through and enforced orchestrator-side. Default port 8055.

**Not exposed:** memory / RecallStore, KB mutations (`kb_write`/`kb_read`), graph mutations, workspace file ops. Those live **in-process behind `ToolContext`**, reachable only by an agent running *inside* SRW's graph. So "a foreign agent hooks into our cross-domain memory" is **not partially built — it does not exist yet.** This is the gap, and it is the *right* gap (it's the un-commoditized layer).

### 3.3 Where tool protection actually lives (the `write_file` correction)

Protection is **not** in the tool implementations:

- `write_file` (`src/tools/workspace/files.py`) delegates to `workspace.write_file()` with almost no validation — no path allowlist, no intrinsic guardrail.
- `run_command` (`src/tools/shell/shell_tools.py`) has only **advisory** checks (cloud-mount guard, sudo-freeze sentinel, error-pattern scan) and sends the command verbatim.

The real protection is at three *other* layers:

1. **The SSH / container boundary — the real isolation.** The agent never touches a filesystem directly; every workspace is a separate pod/VM reached over SSH/SFTP (`remote` backend), credentials injected at dispatch. Path confinement is implicit in the sandbox, not a tool check. **This boundary is already environment-level and harness-agnostic** — a foreign harness handed SSH creds to an SRW-provisioned workspace is contained by exactly the same boundary our own agent is.
2. **The graph's `audited_tool_node`** (`src/graph.py` ~3247–3394) — phase gating (defense-in-depth behind the primary LLM schema binding), stateful loop detection, per-phase hard caps, progress tracking. These are stateful closures **tightly coupled to graph state** (phases, todos, job_id) and are **not** liftable as standalone functions.
3. **The orchestrator** — the sudo/VM-upgrade freeze is meaningful only because the orchestrator persists `sudo_approval_requests` and lets an operator approve.

**Consequence for the platform play:** you do **not** port tool guardrails to foreign harnesses, because (a) the isolation was never in the tools — it's the boundary, already generic — and (b) the graph-coupled governance (loop detection, caps) is something foreign harnesses **bring their own of** (Claude Code has its own loop detection and permission model). The tool layer the note flagged as hardest is the part that's already done and already harness-agnostic.

## 4. Feasibility verdict — close vs. far, in layers

- **Sandbox-as-a-service — close.** Workspaces are already provisioned on demand (K8s pods/PVCs, VMs — see [[ephemeral_workspaces]], [[no_workspace_agent_mode]]) and the agent relates to them purely as an SSH endpoint. Handing that workspace to a foreign runtime is a real but bounded step; the isolation is already there (§3.3).
- **Lifecycle contract — far (the keystone).** "Everyone just needs a container that supports the orchestrator API" breaks on §3.1: the contract is HTTP but SRW-shaped. Needs a minimal-contract mode or per-harness adapter.
- **Stateful services — net-new, and the actual prize.** §3.2: the services that matter (memory/KB/graph) are not exposed as network APIs at all. Net-new work, but it's the one layer that is genuinely ours and not commoditized.

## 5. The hardest parts (recalibrated)

Not the tools. In rough order:

1. **Decouple the lifecycle contract** from the UniversalAgent (minimal-contract mode / adapter) — §3.1.
2. **Build the stateful-services surface** (memory/KB/graph as authenticated, scoped MCP/REST) — §3.2. This is the differentiator.
3. **Multi-tenant security** for running *foreign / arbitrary* code in our sandboxes — a materially bigger commitment than running our own trusted agent (egress policy, resource limits, isolation, abuse). The egress-policy groundwork from [[no_workspace_agent_mode]] §9.1 is a prerequisite.
4. **Adoption / distribution** — the non-technical keystone (§6).
5. **Capture normalization** — if we host a black-box harness we only see FS diffs; structured cross-domain memory requires the harness to *write through our memory API* (MCP). That makes "use our memory MCP → your agent gets cross-domain recall" the actual hook, not a side feature.

## 6. Competitive landscape & risk

- **Pure sandbox-hosting is a crowded, capital-heavy race** (e2b, Daytona, Modal, Cloudflare). Competing there on infra alone is not where a solo/small team wins. The differentiation is the **stateful brain on top** (cross-domain memory + KB + graph), not the sandbox.
- **"It runs fine on my laptop"** is the adoption competitor. The reason to run on SRW must be the cross-domain memory / team-org sharing / managed persistence — compelling enough to overcome zero-friction local use.
- **Don't invent a standard.** The note's "we just need to provide a universal standard everyone can hook into" — that "just" is a decade of ecosystem work. For tools/services the de-facto standard already exists (**MCP**, which we already serve at §3.2), and for compute it's OCI/containers. Our value is the *implementation*, not a new protocol.

## 7. Recommendation — smallest first step

**Start with the services MCP, not the hosting layer.** It is net-new, small, ours, and it tests the entire thesis cheaply:

1. Extend `orchestrator/mcp/` to expose **memory / KB / graph** as MCP tools (lift them out from behind `ToolContext` into a scoped, authenticated surface — §3.2).
2. Point our **own** Claude Code at it across the ~50 real projects that overlap.
3. **Measure** whether cross-domain recall actually buys the 20–30%.

If it does, the moat is validated *before* committing to heavy multi-tenant hosting infra — and it's done by adopting the standard we already serve rather than inventing one. The sandbox-hosting layer (§4) can follow for harnesses that want managed execution; the lifecycle-contract decoupling (§5.1) is only needed once we want the orchestrator to *drive* foreign harnesses rather than merely *serve* them.

## 8. Open questions

- Minimal-contract mode vs. per-harness adapters — which foreign runtimes first (Claude Code / Claude SDK are the obvious wedge given MCP-native tooling)?
- Memory write-path: do foreign harnesses write memory explicitly via an MCP tool, or do we observe+extract from traces/FS diffs (and at what fidelity)?
- Scoping/permission model for cross-project, cross-org shared memory (ties to the permission-boundary work the thesis depends on).
- Where the services MCP runs and how it authenticates a non-Cockpit, non-SRW-agent client at scale (extend `McpTokenVerifier` / OAuth bridge).
