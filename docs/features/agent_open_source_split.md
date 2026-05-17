---
tags:
  - feature
  - agent
  - orchestrator
  - architecture
  - licensing
  - open-source
aliases:
  - OSS agent split
  - agent open-source
  - oss core
  - minimal orchestrator
related:
  - "[[agent_lifecycle]]"
  - "[[unified_instance_lifecycle]]"
  - "[[dual_mode_agent]]"
  - "[[headless_persistent_sessions]]"
  - "[[ephemeral_workspaces]]"
  - "[[sessions]]"
---

# Agent Open-Source Split

> The agent↔orchestrator boundary is already an HTTP contract with no shared code, no shared secrets, and no shared database. Open-sourcing the agent while keeping the orchestrator and Cockpit closed is a viable product split — the architecture is set up for it. The work is mostly documentation and a reference orchestrator, not refactoring.

**Status:** Concept. Filed 2026-05-16.
**Filed:** 2026-05-16
**Last updated:** 2026-05-16

## Motivation

We want to evaluate publishing the agent (`src/`, `agent.py`, `config/`, related Docker images) under an open-source license while keeping the orchestrator (`orchestrator/`), Cockpit (`cockpit/`), and the multi-tenant deployment stack closed. The thesis: the *agent process* is a generic execution engine; the *orchestrator + UI* is the differentiated product.

For this split to make sense, two things must be true:

1. A third party with their own SSH-accessible workspace and a key can drive the agent without our orchestrator — by writing their own "minimal orchestrator" or wiring up a small UI.
2. The split is a clean one-time slice, not an ongoing maintenance tax (no twin-API to keep in sync, no shim layer, no compatibility flags).

Both are true today, with one caveat: the agent currently *requires* a control plane on `localhost:8085` at startup or it sits idle. That's a deliberate property, but it makes the OSS unboxing experience bad unless we ship a reference orchestrator alongside.

## Why this split is clean

The coupling between agent and orchestrator is unusually narrow. From a code-archaeology pass:

- **No cross-package imports.** `grep -rn "from orchestrator" src/` returns nothing. The agent never imports orchestrator code.
- **No shared database.** The agent does not connect to Postgres, Mongo, or Neo4j directly. All persistence flows over HTTP through the orchestrator. (Agent-internal SQLite checkpoints live in the workspace, not in shared infra.)
- **No auth between them.** `/api/agents/*` endpoints in `orchestrator/main.py` are explicitly unauthenticated — comments in the source say "no auth required." The trust model is "this lives on a private network." There are no API keys, JWTs, or mTLS to provision. For OSS, this is a feature: nothing to license-gate.
- **HTTP surface is Pydantic-typed.** `src/api/models.py` defines the request/response shapes formally. `src/api/orchestrator_client.py` is the only outbound coupling point — a single ~1100-line module that wraps every call the agent makes.
- **Workspace isolation already enforces a clean boundary.** `src/agent.py:1065-1080` *requires* `workspace.backend in ("sandbox", "vm")` with SSH credentials injected from outside. The agent has no privileged access to "our" infrastructure — it operates on whatever SSH endpoint it is pointed at. A homelab user can give it `ssh://localhost:2222`; our cluster gives it a Kubernetes pod IP. Same code path either way.

The agent is, in effect, already a stand-alone daemon. The orchestrator is just one possible driver.

## What a minimal orchestrator must implement

A third party drives the agent by speaking two protocols: the agent's inbound HTTP API (which they call), and the orchestrator's HTTP API (which the agent calls back into).

### Hard requirements (block core function)

These must exist or the agent cannot do useful work.

| Endpoint | Direction | Purpose | Notes |
|---|---|---|---|
| `POST /api/agents/register` | agent → orch | Returns `{agent_id, heartbeat_interval_seconds}` | Without it, agent retries silently in the heartbeat loop but never gets an ID |
| `POST /api/agents/{id}/heartbeat` | agent → orch | Liveness, every ~60s | Can no-op return 200; payload can be ignored |
| `POST /job/start` | orch → agent | Submit a job; payload is `JobStartRequest` (`src/api/models.py:243`) | Must include `config_override.workspace.remote = {host, port, username, key_path}` |
| Workspace container | infra | An SSH-reachable container/VM the agent can `paramiko`-connect to | Trivial: `openssh-server` in a Dockerfile is enough |

### Soft requirements (degrade gracefully)

The agent handles 404s on these, so a minimal driver can omit them and lose features without breaking.

| Endpoint | What you lose if omitted |
|---|---|
| `GET /api/uploads/{id}` + `GET /api/uploads/{id}/files/{name}` | File-upload-driven jobs (config/docs by upload ID). Inline config still works. |
| `POST /api/jobs/{id}/complete` | Server-side completion bookkeeping. Agent falls back to local handling. |
| `POST /api/jobs` | The `delegate_work` tool is unavailable; the rest of the agent runs fine. |
| `PUT /api/jobs/{id}/agent-release` | Graceful release on shutdown; orphan detection still cleans up. |
| `POST /api/jobs/{id}/subjob-merge` | Delegation result merge bookkeeping. |
| `GET /api/jobs/{id}/delegation-depth` | Assumed `depth=0` if absent — fine for non-delegating jobs. |

### Required for interactive sessions

Persistent sessions add their own surface. If the OSS product only ships batch jobs, you can skip all of these.

| Endpoint | Notes |
|---|---|
| `POST /api/agents/threads` | Create thread, return `{thread_id}` |
| `GET /api/agents/threads/{id}/workspace` | Return SSH coordinates. Polling-readiness pattern means returning `{"status": "pending"}` until provisioned is fine; agent polls up to 120s (`src/api/persistent_app.py:457`) |
| `GET /api/agents/threads/{id}/lifecycle` | Return `{status: created\|active\|ended}` |
| `PATCH /api/agents/threads/{id}/config` | Return resolved `config_override`; for endpoint-LLM models, this is where `base_url` and `api_key` are injected |
| `POST /api/agents/threads/{id}/messages` | Append message records; fire-and-forget from agent |
| `PUT /api/agents/threads/{id}/status` | Mark thread `active`/`ended` |
| `POST /api/agents/threads/{id}/release-agent` | Cleanup if attach fails |

### Can omit entirely

These have no impact on the agent if they don't exist:

- MCP server / OAuth bridge (Keycloak integration)
- Citation tracking (Neo4j graph)
- Knowledge base (Postgres notes schema)
- Permission approval workflows (sudo gating)
- Critic / verification subjobs
- Curator archival
- Cloud sync (OpenCloud / Nextcloud)
- Datasource registry

**Estimated effort for a minimal orchestrator:** 1–2 weeks for a developer comfortable with FastAPI. The reference implementation should sit in the OSS repo as a working example, not as production infrastructure.

## Response shapes the agent expects

Beyond the endpoint list, the agent expects specific JSON shapes back from the orchestrator. These are not formally versioned — a third party reads them from `src/api/orchestrator_client.py`.

The notable ones:

```jsonc
// POST /api/agents/register → required
{ "agent_id": "uuid", "heartbeat_interval_seconds": 60 }

// GET /api/agents/threads/{id}/workspace → required for sessions
{
  "status": "ready",            // or "pending" while provisioning
  "pod_ip": "10.0.0.5",         // becomes workspace.remote.host
  "pod_port": 22,
  "pod_name": "...",            // optional
  "namespace": "...",           // optional
  "cloud_sync": { ... },        // optional, OpenCloud config
  "config_override": { ... },   // optional, injected into agent config
  "git_remote_url": "..."       // optional
}

// PATCH /api/agents/threads/{id}/config → required when using endpoint LLMs
{
  "config_override": {
    "llm": {
      "model": "...",
      "base_url": "...",        // for endpoint-based models
      "api_key": "..."          // resolved server-side
    }
  }
}
```

These should be documented in an OSS-grade protocol spec rather than left implicit.

## Friction points to fix before open-sourcing

These are blockers for adoption, not blockers for correctness:

1. **The `/ws/chat` protocol has no spec.** The session WebSocket uses a `{method, params}` envelope with ~12 client methods (`message`, `approve`, `deny`, `interrupt`, `mode.set`, `config.update`, `narration.set`, `compact`, `archive`, `upgrade-to-vm`, `undo`, etc.) and many more server-side message types. All documented only in `src/api/persistent_app.py:1342-1678`. Need `docs/protocol/websocket.md` or auto-generated docs from Pydantic models — otherwise every third-party UI reverse-engineers the same protocol.

2. **The "agent boots, then sits idle" UX is bad.** Currently `python agent.py --loop` starts up and nothing happens until something POSTs `/job/start`. Newcomers will think it's broken. Mitigation: ship `docker-compose.oss.yaml` with:
   - The agent image
   - An `openssh-server` workspace container
   - A ~300-line reference orchestrator (Python/FastAPI)
   - A minimal HTML page that opens a session and renders messages

   Target experience: `git clone && docker compose up && open localhost:8080`.

3. **Config inheritance via `$extends` and the model-family matrix is undocumented for outsiders.** Lives in CLAUDE.md and tribal knowledge. OSS users need `docs/config.md` covering: `defaults.yaml` → expert → model-family overrides → per-job override resolution; the prompt/instruction matrix; how to add a new model family.

4. **LLM provider key resolution assumes server-side injection.** The agent expects keys to come back from `PATCH /api/agents/threads/{id}/config`. For OSS, allow an inline fallback: if `config.llm.api_key` is set directly in YAML or env, use it without round-tripping through an orchestrator. Small change in `src/api/orchestrator_client.py` and `src/agent.py`. (Worth checking whether [[custom_llm_endpoints]] already covers this.)

5. **The agent registration loop logs warnings forever if no orchestrator is reachable.** Cosmetic but confusing. Add a config flag like `agent.standalone: true` that suppresses the loop and the warnings when running against a minimal driver that doesn't implement registration.

## Strategic considerations

### What this split actually protects

The agent is a generic execution engine — anyone could write one. The differentiated product is everything *around* it:

- Cockpit (Angular SPA, ~years of UX work)
- Multi-tenant auth via Keycloak + RBAC
- k8s/VM workspace provisioning (`container_provisioner.py`, `vm_provisioner.py`, NATS bridge)
- MCP server for Claude Code integration
- Knowledge graph + citation pipelines
- Fleet/Vault-managed deployment
- The cluster operations playbook itself

None of that is in `src/`. Open-sourcing the agent leaks none of the moat.

### What this split exposes

The boundary you're drawing is exactly the boundary a competitor would also draw. Someone can fork the agent, build a 70%-good orchestrator in a couple of weeks, and ship a competing product. This is the standard OSS-core risk — mitigated, not eliminated, by the closed shell moving faster than a one-person fork.

The shift in framing: the agent's "no orchestrator → fails" behavior stops being an oversight and becomes a deliberate design choice. The README would lean into it: "the agent expects a control plane on port 8085. Run our reference orchestrator, build your own, or use the closed-source enterprise one."

### Licensing posture

With zero secrets at the boundary, license enforcement is purely a legal question, not a technical one. Options:

- **Apache 2.0 / MIT** — maximal adoption, zero protection.
- **AGPL** — protects against SaaS competitors who'd need to release their orchestrator code; allows internal/private use.
- **BSL with a time-delayed Apache fallback** — common for OSS-core companies (HashiCorp pattern, recently controversial).

The choice depends on whether the goal is community-building (permissive) or competitive protection (copyleft). This doc doesn't recommend one; it just notes the technical work is the same either way.

### One thing worth deciding early

If the OSS agent diverges from the internal one (e.g., we add a feature internally that depends on a closed orchestrator service), we have to either ship a stub for OSS or feature-gate it. Decide now whether the internal and OSS agent are *the same binary* or *two branches*. Recommendation: same binary. Feature gates are cheap; maintaining a divergent fork is not.

## Scope

### v1 (the OSS launch)
- Reference minimal orchestrator (~300 LOC FastAPI)
- `docker-compose.oss.yaml` with agent + workspace + reference orchestrator + tiny HTML client
- `docs/protocol/http.md` — agent's inbound API
- `docs/protocol/websocket.md` — `/ws/chat` message types
- `docs/protocol/orchestrator-callbacks.md` — what the agent calls back into
- `docs/config.md` — YAML config system
- Standalone mode flag + LLM-key inline fallback
- License decision + LICENSE file
- README that sets expectations: "this is an execution engine, you need a control plane"

### Not v1
- Web-based reference UI beyond the trivial HTML page (let the community build that)
- Helm chart for OSS deployment (compose is enough to start)
- Compatibility guarantees between agent and orchestrator versions (just pin both)

## Open questions

- **Branding.** Does the OSS agent ship under the same name as the product, a sub-name, or a separate brand? Affects search-result hygiene and confusion if support questions come in for the closed product.
- **Contribution policy.** CLA, DCO, or neither? Affects ability to relicense later.
- **Issue triage burden.** OSS users will file issues against the agent that are really about our closed orchestrator. Need a clear "report issues against the OSS repo only if reproducible with the reference orchestrator" rule.
- **Telemetry.** The agent currently logs to stdout only; no phone-home. Confirm we want to keep it that way for OSS (we probably do).
- **Trademark.** If the project name is trademarked, decide what third parties can call their forks.

## Out of scope

- Refactoring the agent itself. The split is doable with the current code.
- Open-sourcing the orchestrator, Cockpit, or deployment manifests.
- A formal SDK in any language. Documenting the HTTP/WS protocols is enough; SDKs are community work.
