# Universal Agent Standard (Exploration)

> **Status:** Exploration / **parked** — no decision made. Captured 2026-06-23 to revisit when there's time/runway.
>
> **The question:** Should the agent↔orchestrator boundary become an open, *runtime-agnostic* standard — so anyone can build their own agent (e.g. with the TypeScript Claude Agent SDK, or in Go/Rust) and slot it into the platform, while the platform provides orchestration, lifecycle management, and resources (workspace, data sources, credentials)?
>
> **TL;DR:** We are already ~80% of the way there. This is **formalization plus a few decoupling seams, not a rewrite.** The real open question is *how far to take it*, which is a positioning/runway call — not an architecture one.

---

## 1. The idea

Rather than treating the agent and the orchestrator as one tightly-coupled unit (and rather than betting effort on a Go rewrite of both), focus the open-source mission on **the schema and the API**: make the agent contract universal enough to represent *any* kind of agent. The platform becomes the substrate — orchestration, lifecycle, workspaces, data sources — and the agent becomes a pluggable component you bring yourself.

The mental model is **"OCI/Kubernetes for agents"**: the platform doesn't care what's inside the agent image, only that it speaks the contract. A likely mechanism is an **agent-image variable** so the platform pulls whatever runtime you point it at, the same way a Pod references a container image.

This framing also settles a recurring question (see [`docs/go_rewrite.md`](../go_rewrite.md) and [`docs/backend_language_decision.md`](../backend_language_decision.md)) **in favour of the schema**: a second runtime (Go, TypeScript) stops being a big-bang rewrite and becomes something you *could* add later precisely because it speaks the same contract. Python and a new runtime can coexist on one orchestrator during any cutover.

Concrete motivating use case the user named: *"Somebody wants to use the Claude SDK (TypeScript) to build their own agent — today the system wouldn't allow that."* Making that possible is the litmus test for the contract being truly language-agnostic.

---

## 2. Key finding — the architecture already anticipates this

The good news, grounded in the current code and the existing design vault:

- **The boundary is already an HTTP/JSON contract** with no shared code, no shared secrets, and no shared database between agent and orchestrator. This is already documented in [`agent_open_source_split.md`](agent_open_source_split.md), which catalogs the contract and the minimal-viable orchestrator surface.
- **The orchestrator-resolved-config work (shipped) is the linchpin.** The orchestrator resolves the *entire* config and hands the agent a single frozen `dict`; the agent is a **pure hydrator** that needs no DB access. The entry point is literally `UniversalAgent.from_resolved()`. The architecture already named the thing we're reaching for. See [`../superpowers/specs/2026-06-17-orchestrator-resolved-config-design.md`](../superpowers/specs/2026-06-17-orchestrator-resolved-config-design.md) and its [plan](../superpowers/plans/2026-06-17-orchestrator-resolved-config.md).
- **Skills already adopt an open standard.** We use the `SKILL.md` format verbatim (portable to/from Claude Code and Codex), so a whole class of agent capability is already expressed in a portable, non-bespoke format. See [`agent_skills.md`](agent_skills.md).
- **The agent already runs in two modes off one image** (worker / persistent), selected by flag/env — the "one substrate, configurable runtime" pattern is established. See [`agent_lifecycle.md`](agent_lifecycle.md) and [`dual_mode_agent.md`](dual_mode_agent.md).

**Implication:** the distance from here to "BYO agent is *possible*" is mostly *packaging and a few seams*, not new architecture.

---

## 3. The contract as it exists today

Concise map of the boundary (cite symbols, not line numbers — these drift; verify before acting):

| Concern | Mechanism | Where |
|---|---|---|
| **Transport** | Plain HTTP/JSON both directions; auth via `X-Internal-Key`. No NATS/WebSocket on the job path. | `src/api/orchestrator_client.py`, `src/api/app.py` |
| **Registration** | Agent → `POST /api/agents/register` with `AgentRegistration` (`config_name`, `pod_ip/port`, `hostname`, `agent_mode`, `thread_id?`, `build_sha?`). Returns `agent_id` + heartbeat interval. | `AgentRegistration` in `orchestrator/main.py`; `register()` in `orchestrator_client.py` |
| **Lifecycle / heartbeat** | Agent → `POST /api/agents/{id}/heartbeat` with `status` + `metrics`; response carries `intents` (drain, upgrade). States: `booting, available, ready, working, session, draining, completed, failed, offline`. | `AgentHeartbeat` in `orchestrator/main.py`; heartbeat loop in `orchestrator_client.py` |
| **Job dispatch** | Orchestrator → `POST {agent}/job/start` with `JobStartRequest`. Key field: **`resolved_config`** (full, frozen, credential-free blob). Falls back to `config_name + config_override` when DB experts are off. | `JobStartRequest` in `orchestrator/main.py` and `src/api/models.py`; dispatch in `orchestrator/main.py` |
| **Config resolution** | `resolve_config(...)` merges bundled base → expert → project → DB → user → request, strips secrets, emits the blob; credentials injected into the *delivery copy* only. | `orchestrator/services/config_resolver.py`; `serialize_resolved_config` in `src/core/loader.py` |
| **Workspace** | Orchestrator provisions and injects `workspace.{backend, remote}` — `backend ∈ {sandbox, vm, virtual, none}`; remote is **SSH** (`host/port/username/key_path/workspace_path`). | dispatch path in `orchestrator/main.py`; `WorkspaceConfig` in `src/core/loader.py` |
| **Data sources** | Resolved by orchestrator, delivered as a generic `datasources: [{id, name, type, credentials, ...}]` list. | `resolve_datasources_for_job` in `orchestrator/main.py` |
| **Result reporting** | Agent → `POST /api/jobs/{id}/complete` with `JobCompleteRequest` (`should_stop`, `goal_achieved`, `error`, `freeze_data`). | `JobCompleteRequest` in `orchestrator/main.py` |

---

## 4. Already abstracted vs. still coupled

**Already runtime-agnostic (no change needed):** registration, heartbeat + status enum, job-dispatch JSON, `config_name` selection, workspace backend enum + SSH remote, data-source delivery, LLM endpoint (model/provider/base_url/api_key), task description, intents, metrics.

**Still coupled to the Python/LangChain runtime:**
- **Entrypoint** baked into the image: `python agent.py --config ... --port ...` (`docker/Dockerfile.agent`).
- **Config fallback path** assumes the agent can load YAML from `config/` locally (lossy vs. the resolved blob).
- **Tool loading** is dynamic Python import from `src/tools/` (orchestrator only selects *which* are enabled).
- **Agent state/checkpointing** uses LangGraph's `AsyncPostgresSaver` (`src/agent.py`) — opaque to the orchestrator today, but registration implicitly assumes it.
- **`freeze_data`** in the completion payload is shaped around the Python agent's graph (summary/deliverables/confidence).

---

## 5. Seams to close for true runtime-agnosticism

Bounded list. None of these is a rewrite; each is a clean, isolated change.

1. **Entrypoint → `AGENT_IMAGE`.** Stop baking `python agent.py`; let the platform pull/run an arbitrary agent image that exposes the inbound HTTP surface (`/job/start`, `/job/cancel|pause|resume`, `/health`, `/ready`). *(The user's container-image instinct.)* — *small*
2. **Deprecate the config-fallback path.** Always ship `resolved_config`; remove the "load YAML locally" branch so no agent needs our `config/` layout or `src/core/loader`. — *small/medium*
3. **Tools as data, not imports.** Deliver enabled tools as JSON tool-schemas (OpenAI-style function defs) inside the blob, so a non-Python agent can honor them without importing our modules. — *medium*
4. **Make agent state opaque.** Let the agent declare its checkpoint/state type at registration; orchestrator treats it as a black box (it mostly already does). — *small*
5. **Standardize the result.** Replace/augment `freeze_data` with a minimal runtime-neutral result (`status`, `output`, `error`, optional structured extras). — *medium*
6. **Capability declaration + version negotiation.** Registration advertises `{supported_tools, workspace_backends, agent_version, min_orchestrator_version}`; orchestrator validates an agent can run a job's config before dispatch, and degrades gracefully otherwise. — *medium*
7. **Publish the contract.** Extract the Pydantic models into a versioned, standalone spec: **OpenAPI** for the HTTP surfaces + **JSON Schema** for `resolved_config` / `JobStartRequest` / `JobCompleteRequest`. Today it's Python-implied only. See [`openapi_documentation.md`](openapi_documentation.md). — *medium, mostly packaging*

---

## 6. Scope options — the actual open decision

The architecture distance is small; the **commitment distance** is large. Three honest tiers:

| | **Option 1 — Formalize what exists** | **Option 2 — Enable 3rd-party agents** | **Option 3 — "Kubernetes for agents"** |
|---|---|---|---|
| **Goal** | Open-source-ready; can't-lock-ourselves-in | Others actually build agents on us | Become *the* substrate for running agents |
| **Work** | Seams 1–2, 4, 7 (publish spec, AGENT_IMAGE, drop fallback) | + seams 3, 5, 6; **a 2nd reference agent (thin TS + Claude Agent SDK)**; authoring guide | + agent-image registry/marketplace; SDKs (py/ts/go) + conformance suite; spec governance; partner motion |
| **Cost** | ~weeks, little new building | ~a quarter, real building | multi-quarter, company-defining |
| **Risk** | Low. Standard exists on paper; adoption unproven | Medium. The TS agent *is* the proof the contract is language-agnostic | High. Standards plays need adoption we don't yet have |
| **Runway/feature-freeze fit** | Good | Tension with revenue work | A bet-the-company pivot, not a feature |

A **published, versioned spec is already a standard** even with a single reference implementation. Adoption can follow without us building the whole ecosystem up front.

---

## 7. Recommendation (a lean, not a decision)

Given the current feature-freeze stance and runway constraint, **Option 1 is the high-leverage, low-regret move**: it captures the "global standard" positioning cheaply, makes BYO-agent genuinely possible, and keeps the door open to Options 2–3 without committing to them. Pull Option 2 forward only when there's runway *or* a concrete partner pulling for it — and let that partner's use case (very likely the TS Claude-Agent-SDK agent) be the forcing function that proves the contract. Treat Option 3 as a separate, deliberate strategic bet, not a default.

This is a lean for when we revisit — **not a decision.** No work is committed by this document.

---

## 8. Open questions for when we pick this back up

- **What's the actual win** — open-source hygiene, developer adoption, a moat, or a positioning story? (Determines the tier above.)
- **Licensing** of both the code and the *standard itself* (Apache/MIT vs AGPL vs BSL; trademark/governance for the spec). Unresolved in `agent_open_source_split.md`.
- **Where does the moat live** if the agent is open and pluggable? (Working assumption: orchestrator, multi-tenant auth, Cockpit, knowledge graph, deployment automation — see the open-source-split doc.)
- **Data portability** — can a user export job results / session history / workspace and move to another orchestrator? Not yet designed.
- **Versioning policy** for the agent↔orchestrator contract (backward/forward-compat guarantees).
- **How opaque can agent state really be** before lifecycle features (drain, resume, snapshot) need to know its shape?

---

## 9. Related docs

- [`agent_open_source_split.md`](agent_open_source_split.md) — the closest sibling: HTTP contract, minimal-viable orchestrator, friction points, licensing options.
- [`agent_lifecycle.md`](agent_lifecycle.md) / [`dual_mode_agent.md`](dual_mode_agent.md) — modes, lifecycle states, one-image-two-modes pattern.
- [`agent_skills.md`](agent_skills.md) — open `SKILL.md` standard already adopted (portability precedent).
- [`openapi_documentation.md`](openapi_documentation.md) — existing thinking on publishing API specs.
- [`no_workspace_agent_mode.md`](no_workspace_agent_mode.md) — workspace as an *optional* resource (relevant to "platform provides resources").
- [`../superpowers/specs/2026-06-17-orchestrator-resolved-config-design.md`](../superpowers/specs/2026-06-17-orchestrator-resolved-config-design.md) + [plan](../superpowers/plans/2026-06-17-orchestrator-resolved-config.md) — the resolved-config blob that makes the agent a pure hydrator.
- [`../go_rewrite.md`](../go_rewrite.md) / [`../backend_language_decision.md`](../backend_language_decision.md) — the rewrite question this reframes.
- [`../saas_roadmap.md`](../saas_roadmap.md) / [`../financing.md`](../financing.md) — business context for the scope decision.
