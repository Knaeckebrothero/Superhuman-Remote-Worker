---
tags:
  - feature
  - documentation
  - api
  - developer-experience
  - orchestrator
  - agent
aliases:
  - swagger documentation
  - api reference
  - openapi spec
related:
  - "[[shared_browser]]"
  - "[[cli_design]]"
---

# Feature: OpenAPI / API Reference Documentation

Design document for producing complete, trustworthy, browsable API documentation across every HTTP surface in the system, and publishing it as a single aggregated reference site.

**Status:** Design phase.

## Motivation

The system exposes four HTTP surfaces (orchestrator, agent worker, agent persistent, MCP server) and a rapidly growing number of endpoints — the orchestrator alone has ~183 routes. There is currently no first-class API documentation:

- **For us.** When wiring a new cockpit feature, the only way to know what an endpoint returns is to read the orchestrator handler. There are ~183 of them in a single 13,000-line file. The mental tax compounds across every change.
- **For Claude Code / agents.** Agents that need to call our APIs (via MCP or directly) have no spec to consume. They guess from URL patterns and frequently call the wrong shape.
- **For external integrators.** The MCP server is the only sanctioned external surface today, but the orchestrator API is the substrate everything sits on. Without a published reference, third parties cannot build on it.
- **For our future selves.** Documentation rot is real, but generated documentation rots in lockstep with the code — a discrepancy means the code is wrong, not the docs. That feedback loop is what we want.

FastAPI gives us OpenAPI essentially for free at `/docs` and `/redoc` — and all three FastAPI servers in the repo already expose those routes. The reason this isn't a one-line task is that the *quality* of the generated spec is poor:

1. The orchestrator declares 1 `response_model` across 183 endpoints. Swagger UI shows the routes but says nothing useful about what they return.
2. Auth is implemented by *calling* `get_current_user(request, db)` from inside route bodies (`orchestrator/security/auth.py:22`) rather than via FastAPI's `Security(...)` dependency injection. As a result, the OpenAPI document declares no `securitySchemes` and Swagger UI has no Authorize button — every "Try it out" returns 401.
3. Many handlers return `dict[str, Any]` or hand-built `JSONResponse` payloads with no type backing, so even if we did add `response_model` declarations, they would be lying half the time until we type the underlying code.
4. The MCP server is FastMCP, not FastAPI — it speaks Model Context Protocol, not REST. Its ~100 tools need their own documentation pipeline.
5. WebSocket endpoints (`/ws/persistent/{thread_id}`, `/ws/chat`, `/api/ide/.../proxy/...`) cannot be expressed in OpenAPI 3.0 at all. They are part of our public surface and need to be documented in a parallel track.

The decision driving this doc: rather than ship an "OK-ish" cleanup that leaves half the surface untyped and the auth still invisible, do the full pass in one go and publish it as an aggregated, versioned reference site that lives alongside the cockpit. The pipeline is already large enough that one more build step is not the cost driver.

## Current State Survey

Snapshot of every HTTP surface, gathered from the codebase:

| Server | Entry point | Port | Endpoints | Tags | response_model coverage | Auth wired for OpenAPI | `/docs` exposed | WebSockets |
|--------|-------------|------|-----------|------|-------------------------|------------------------|-----------------|------------|
| **Orchestrator** | `orchestrator/main.py:2463` | 8085 | ~183 | None on `@app.*`, only on the 2 `APIRouter` includes (`graph`, `Uploads`) | 1 / 183 | **No** — manual `get_current_user(request, db)` calls inside route bodies | Yes (default) | 2 (`/ws/persistent/{thread_id}`, `/api/ide/{job_id}/proxy/{path:path}`) |
| **Agent Worker** | `src/api/app.py:569` | 8080 | 11 | Yes (`Health`, `Orchestrator`, `Monitoring`) | 11 / 11 | None needed (internal) | Yes (default) | 0 |
| **Agent Persistent** | `src/api/persistent_app.py:642` | 8001 | 6 | None | 0 / 6 (raw `JSONResponse`) | None needed (internal) | Yes (default) | 1 (`/ws/chat`) |
| **MCP Server** | `orchestrator/mcp/run.py:43` | 8055 | N/A — ~100 `@mcp.tool` decorators in `orchestrator/mcp/server.py` | N/A | N/A — uses MCP schema, not OpenAPI | Bearer + optional Keycloak OIDC | `/health` only | 0 |

**Key file refs:**
- `orchestrator/main.py:2463` — `FastAPI(title="Debug Cockpit API", description=..., version="0.1.0")` — title is a placeholder, description is a stub
- `orchestrator/security/auth.py:22` — `async def get_current_user(request: Request, db) -> dict` — called manually, not a `Depends(...)`
- `orchestrator/security/auth.py:132` — `async def require_approved_user(request: Request, db) -> dict` — same pattern
- `src/api/app.py:569` — agent worker FastAPI construction (already a model citizen)
- `src/api/persistent_app.py:642` — persistent agent FastAPI construction
- `orchestrator/mcp/run.py:43` — `mcp.run(transport="streamable-http", ...)` — FastMCP, not FastAPI

**Verdict:**
- Agent worker (8080) is already shippable. Zero-line refactor for that surface.
- Agent persistent (8001) needs ~6 endpoints typed and tagged. Maybe a half day.
- Orchestrator (8085) is the elephant. The auth refactor (turning `get_current_user` into a real dependency) is mechanical but touches every route. Adding `response_model` declarations is the bulk of the work — call it 1–2 days of focused mechanical typing once the Pydantic models exist, plus several days of writing the missing models.
- MCP needs a separate, parallel documentation pipeline. Not OpenAPI-shaped, but doable.
- WebSockets need a parallel track (AsyncAPI or hand-written reference pages).

## Industry Context

### How Others Document FastAPI / Multi-Service APIs

| System | Approach | Notes |
|--------|----------|-------|
| **Stripe** | Hand-curated reference site, OpenAPI generated from code | Reference is the gold standard. Aggressively versioned. Reference pages render code samples in 7+ languages from a single OpenAPI source |
| **GitHub** | OpenAPI 3.1 spec published in [github/rest-api-description](https://github.com/github/rest-api-description), rendered as docs site | Spec lives in its own repo, generated from internal definitions. Multiple consumers (docs, SDKs, CLI) all read the same spec |
| **Linear** | GraphQL schema + hand-written guides | Not a fit for our REST surface, but their developer docs IA is worth borrowing |
| **Supabase** | Auto-generated from OpenAPI per project + hand-written guides | Their generated reference uses Redoc; their guides use Nextra |
| **PostHog** | Generated from FastAPI OpenAPI, rendered with their own docs site | Closest analogue to us — also Python+FastAPI. Their docs site is Next.js + Redoc |
| **Sentry** | Generated from OpenAPI, rendered with Redocly | The "aggregated multi-product reference" pattern we're aiming for |

### Aggregated Renderers

The choices for "render an OpenAPI spec as a docs site":

| Renderer | License | Look | Notes |
|----------|---------|------|-------|
| **Swagger UI** | OSS | Dated | The default. Functional, ugly. What FastAPI's `/docs` already serves |
| **Redoc** (open source) | OSS (MIT) | Clean, three-column, print-friendly | The traditional "looks like Stripe" choice. Single static HTML file. Battle-tested |
| **Redocly** | Commercial layer on top of Redoc | Same look + bundling, linting, CI tooling | Their CLI (`redocly`) does spec linting and bundling. Free for OSS, paid for hosted features |
| **Scalar** | OSS (MIT) | Modern, sleek, "what Stripe would build today" | Newest entrant. Live "Try it out" works without a separate sandbox. Excellent UX. Single-file embed |
| **Stoplight Elements** | OSS (Apache 2.0) | Polished, three-column | Embeddable web component. Less momentum than Scalar lately |
| **Bump.sh** | Commercial SaaS | Polished | Hosted, paid. Good diffing across versions. Out of scope — we want self-hosted |
| **ReadMe** | Commercial SaaS | Polished | Same — paid SaaS, not self-hosted |

### Documenting Non-OpenAPI Surfaces

| Surface | Spec format | Renderer |
|---------|-------------|----------|
| **WebSockets** | [AsyncAPI](https://www.asyncapi.com/) | AsyncAPI Studio (web) or `@asyncapi/html-template` (static) |
| **MCP tools** | MCP protocol's own `tools/list` (introspectable at runtime) | Generate markdown from `@mcp.tool` decorators at build time |
| **GraphQL** | (n/a — we don't have any) | — |

### Key Takeaways

1. **OpenAPI is the lingua franca, but the renderer matters.** Swagger UI is the default and the worst-looking option. Scalar and Redoc are both miles ahead and free. Whichever we pick defines the visual identity of our API for everyone who reads the docs.
2. **Generated > written.** Every team that has tried hand-writing an API reference has watched it rot. The win condition is: source of truth lives in the code, the spec is generated, the docs site renders the spec. No manual sync.
3. **The aggregation layer is the value-add.** Each FastAPI service exposes its own `/openapi.json`. The aggregated docs site reads all four (or three, post-dropping MCP from the OpenAPI track) and presents them under one navigation. This is how Sentry, Supabase, and PostHog all do it.
4. **Spec linting is non-optional.** Once you have an aggregated docs site, malformed `operationId`s, missing descriptions, and untagged endpoints become visible. `redocly lint` (or `spectral`) catches these in CI before they ship.
5. **AsyncAPI for WebSockets is a lateral commitment.** It's a real spec with real tooling, but it adds a second source of truth. Realistic alternative: a dedicated `docs/api/websockets.md` page that lives in the same docs site, hand-maintained but linted by CI for "every WS route in the code is mentioned in the doc."
6. **MCP is its own world.** FastMCP tools are introspectable at runtime via the MCP `tools/list` request. The cleanest approach: at build time, start the MCP server in a stub mode, fetch `tools/list`, and render to markdown. No new source of truth; the tools' docstrings remain canonical.
7. **Versioning matters less than we think early on.** Stripe's API versioning is famous because their customers stayed on old versions for years. We don't have that problem yet. v1 of this feature can be "single current version, the spec moves with main." Versioning can be added later when we have external integrators who need it.

## Design

### Approach: Generated Specs + Aggregated Renderer + Spec Linting in CI

Three changes, in this order:

1. **Make each FastAPI server's OpenAPI output high-quality** by typing responses, declaring auth as `Security(...)` dependencies, and adding tags / summaries / descriptions. The orchestrator is most of this work.
2. **Aggregate the specs** at build time into a single docs site. The build pulls `/openapi.json` from each service (or imports the FastAPI app and generates the spec offline — preferred, since it doesn't require running services in CI), bundles them with the chosen renderer, and outputs static HTML.
3. **Lint the specs in CI** using `redocly lint` or `spectral`. New endpoints without tags, summaries, or response models break the build.

```
                            ┌──────────────────────────┐
  Source of truth           │  FastAPI route handlers  │
  ─────────────             │  + Pydantic models       │
                            │  + Security() deps       │
                            └────────────┬─────────────┘
                                         │  fastapi.openapi.utils.get_openapi()
                                         │  (offline, no running server)
                                         ▼
                            ┌──────────────────────────┐
  Build artifacts           │  openapi.orchestrator.json│
                            │  openapi.agent.json       │
                            │  openapi.persistent.json  │
                            │  mcp_tools.md             │
                            │  websockets.md            │
                            └────────────┬─────────────┘
                                         │  redocly lint  →  CI gate
                                         │  scalar/redoc bundle
                                         ▼
                            ┌──────────────────────────┐
  Published docs site       │  docs.srw.local/api      │
                            │  - Orchestrator          │
                            │  - Agent (Worker)        │
                            │  - Agent (Persistent)    │
                            │  - MCP Tools             │
                            │  - WebSockets            │
                            └──────────────────────────┘
```

### Renderer Choice: Scalar (Primary), Redoc (Fallback)

| Concern | Scalar | Redoc | Swagger UI |
|---------|--------|-------|------------|
| License | MIT (OSS) | MIT (OSS) | Apache 2.0 (OSS) |
| Visual quality | Best in class | Clean, dated-but-good | Functional, dated |
| Live "Try it out" | Yes, in-browser | Limited (paid Redocly tier for full) | Yes |
| Multi-spec aggregation | Yes (`sources` config) | Yes (`spec-url` array via Redocly CLI) | No (one spec per instance) |
| Static-site output | Yes | Yes | Yes |
| Bundle size | ~200 KB | ~900 KB | ~1.5 MB |
| Maintenance momentum | High (active 2024–2025) | Steady | Steady |
| Community familiarity | Lower (newer) | Highest (years of FastAPI default) | Highest |

**Decision: Scalar.** Best UX, smallest bundle, native multi-spec aggregation, MIT licensed. The lower community familiarity is not a real cost — we are the only people who will maintain the docs build, and the renderer is downstream of the spec, not load-bearing. If Scalar's project momentum stalls, swapping to Redoc is a build-config change, not a content change — the specs themselves remain identical.

**Per-service `/docs` endpoints stay.** FastAPI's built-in Swagger UI on each server is useful for in-development "what does this endpoint look like right now" checks. The aggregated Scalar site is the *published* reference; the per-service `/docs` pages are dev tools. Both live, no contradiction.

### The Orchestrator Cleanup (The Big One)

This is where most of the work lives. Three sub-tasks, roughly independent:

#### 1. Refactor `get_current_user` into a real `Depends(...)` chain

Currently:

```python
# orchestrator/main.py — every protected route looks like this
@app.get("/api/jobs")
async def list_jobs(request: Request):
    user = await require_approved_user(request, db)
    # ...
```

Target:

```python
# orchestrator/security/auth.py
http_bearer = HTTPBearer(auto_error=False, description="Keycloak access token")

async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Security(http_bearer),
    db = Depends(get_db),
) -> User:
    ...

async def require_approved_user(
    user: User = Depends(get_current_user),
) -> User:
    ...

# orchestrator/main.py
@app.get("/api/jobs", response_model=JobListResponse, tags=["Jobs"])
async def list_jobs(user: User = Depends(require_approved_user)):
    ...
```

**Why this matters:** `Security(HTTPBearer())` is what makes FastAPI emit a `securitySchemes` block in the OpenAPI document. Once that block exists, Scalar (and Swagger UI) renders an "Authorize" button and every "Try it out" carries the token. Without it, the docs are a read-only catalogue.

The MCP-header fallback (`X-MCP-User-Id` + `X-Internal-Key`) becomes a second `APIKeyHeader` security scheme declared in the same dependency, and the OpenAPI spec lists both as alternatives.

**Migration strategy:** This is mechanical. Touch every `@app.*` decorator in `orchestrator/main.py` (~183 of them). The change is `request: Request` → `user: User = Depends(require_approved_user)`, then delete the inline `await require_approved_user(...)` line at the top of each handler. No behavior change. Land it in one PR with a focused review.

#### 2. Add `response_model` declarations and tags

Every endpoint gets:
- A `tags=["..."]` list — there are roughly 12 logical groups (Jobs, Agents, Projects, Users, Datasources, Knowledge, Memory, Audit, MCP, IDE, System, Auth)
- A `response_model=...` — either an existing Pydantic model or one we add to a new `orchestrator/api/schemas.py` module
- A `summary="..."` and (where the behavior isn't obvious) `description="..."`
- An `operationId` only if the auto-generated one is ugly

The Pydantic models do not need to be perfect — we can start with permissive models (`Config: extra = "allow"`) and tighten over time. A loose typed model is infinitely better than `dict[str, Any]`.

**Where to put the schemas.** A new `orchestrator/api/schemas.py` module (or split into `schemas/jobs.py`, `schemas/agents.py`, etc. if it grows past ~500 lines). Keep them out of `database/postgres.py` so the persistence layer stays decoupled from the API surface.

**How to find the gaps quickly.** Run `redocly lint` against the generated spec — it will list every endpoint without a description, every untyped response, every missing tag. Fix until the list is empty. Use this as the iteration loop instead of trying to audit by reading code.

#### 3. Split `orchestrator/main.py` into APIRouters (optional, but recommended)

`main.py` is 13,000+ lines. It works, but the typing pass is an ideal moment to split it into per-domain `APIRouter` modules:

```
orchestrator/
├── main.py                    # FastAPI app construction, lifespan, middleware
├── api/
│   ├── __init__.py
│   ├── schemas.py             # or schemas/ package
│   ├── routes/
│   │   ├── jobs.py            # /api/jobs/*
│   │   ├── agents.py          # /api/agents/*
│   │   ├── projects.py
│   │   ├── users.py
│   │   ├── datasources.py
│   │   ├── knowledge.py
│   │   ├── memory.py
│   │   ├── audit.py
│   │   ├── ide.py             # IDE proxy + WS
│   │   ├── auth.py            # /api/auth/me, login, etc.
│   │   └── system.py          # /api/health, metrics
```

Each router has its own `tags=[...]` default, which auto-applies to every route inside it. This is also the opportunity to lift WebSocket routes into their own module (`api/routes/websockets.py`) so they're isolated from the OpenAPI-relevant routes.

**Optionality:** This is a refactor, not a docs feature. If it makes the typing pass too big to land in one PR, we can do the typing pass first and the split second. The end-state is the same. Recommend doing them together because the typing work touches every line anyway and the split is "free" at that point.

### Agent Worker (8080) — No Work

`src/api/app.py:569` is already in the target shape: tags on every endpoint, full `response_model` coverage, complete docstrings. The aggregated docs build picks up its `/openapi.json` (or imports the app offline) and ships it as-is. No PR needed — confirm-by-inspection only.

### Agent Persistent (8001) — Small Cleanup

`src/api/persistent_app.py:642` has 6 endpoints. Tasks:
1. Add `tags=[...]` to each (proposed: `Health`, `Session`, `WebSocket`)
2. Replace inline `JSONResponse({"status": "ok"})` returns with proper Pydantic models in `src/api/schemas/persistent.py`
3. Add `summary=` to each route
4. Document the `/ws/chat` WebSocket in the parallel WebSocket reference (see below)

Estimated half a day of focused work.

### MCP Tools — Generate Markdown From Decorators

`orchestrator/mcp/server.py` has ~100 `@mcp.tool` decorators. Each tool function has a docstring describing its purpose, parameters, and return shape (the existing docstrings are good — they're the source of truth the MCP protocol already advertises via `tools/list`).

**Build-time generator:**

```python
# scripts/generate_mcp_docs.py
"""Render MCP tool reference to docs/api/mcp_tools.md.

Imports orchestrator.mcp.server, walks the FastMCP `tools` registry, and
emits one section per tool with name, description, parameter table, and
return type. No live server required.
"""
```

The output is a single `mcp_tools.md` markdown file that the docs site renders as a normal page in the navigation. Since the source of truth is still the decorator docstrings in `server.py`, there is no manual sync. Lint rule: every `@mcp.tool` must have a non-empty docstring or CI fails.

This approach keeps MCP out of the OpenAPI track entirely, which is the right call — pretending MCP tools are REST endpoints would distort both specs.

### WebSocket Endpoints — Parallel Reference Page

OpenAPI 3.0 cannot describe WebSockets. OpenAPI 3.1 has partial support but renderer adoption is patchy. Realistic options:

1. **Hand-written `docs/api/websockets.md`.** Cheap, immediate, easy to read. Risk: rots.
2. **AsyncAPI spec.** Real spec, real tooling. Cost: a second source of truth and a second linter to maintain.
3. **In-code annotation + extractor.** Decorate WebSocket routes with a custom marker that a build script reads and renders to markdown. Best of both — single source of truth, generated output.

**Decision: option 1 for v1, with a CI lint that asserts every `@app.websocket(...)` route in the codebase has a corresponding heading in `websockets.md`.** The lint is the rot-prevention. If the lint becomes painful, we revisit option 3. AsyncAPI is overkill for our current ~3 WebSocket routes.

**The current WebSocket routes:**

| Route | Server | Purpose |
|-------|--------|---------|
| `/ws/persistent/{thread_id}` | Orchestrator (8085) | Persistent thread tunnel — bidirectional chat with persistent agents |
| `/api/ide/{job_id}/proxy/{path:path}` | Orchestrator (8085) | Web IDE WebSocket proxy (terminal, LSP) |
| `/ws/chat` | Persistent agent (8001) | Direct WebSocket transport for interactive sessions |

Each gets a section in `websockets.md` covering: subprotocol, auth (token in `Sec-WebSocket-Protocol` or query string), inbound message schema, outbound message schema, lifecycle, and example exchange.

### The Aggregated Docs Site

A new `docs-site/` subdirectory at the repo root:

```
docs-site/
├── package.json              # Just for the build, not for runtime
├── scalar.config.ts          # Scalar configuration
├── build.sh                  # Generates specs offline + bundles
├── content/
│   ├── index.md              # Landing page
│   ├── getting-started.md
│   ├── auth.md               # How to get a token
│   └── api/
│       ├── websockets.md     # Generated/linted
│       └── mcp_tools.md      # Generated from server.py
└── dist/                     # Build output (gitignored)
```

**The build script:**

```bash
#!/usr/bin/env bash
# docs-site/build.sh
set -euo pipefail

# 1. Generate OpenAPI specs offline (no running services)
python scripts/generate_openapi.py orchestrator --out docs-site/openapi/orchestrator.json
python scripts/generate_openapi.py agent_worker  --out docs-site/openapi/agent_worker.json
python scripts/generate_openapi.py persistent    --out docs-site/openapi/persistent.json

# 2. Generate MCP markdown
python scripts/generate_mcp_docs.py --out docs-site/content/api/mcp_tools.md

# 3. Lint the specs
npx -y @redocly/cli@latest lint docs-site/openapi/*.json

# 4. Lint the WebSocket reference (every WS route exists in markdown)
python scripts/lint_websocket_docs.py

# 5. Bundle with Scalar (or fallback to redocly bundle)
npx -y @scalar/cli@latest build docs-site/
```

`scripts/generate_openapi.py` imports the FastAPI app and calls `app.openapi()` to dump the spec, *without ever starting an HTTP server*. This is the right way to do it in CI — no port juggling, no service startup, no flaky test infrastructure.

### Where the Docs Site Is Served

Two viable hosts:

1. **As a route on the cockpit.** `cockpit.srw.local/api-docs` — the cockpit serves the static bundle from `docs-site/dist/` as a sub-path. Pros: same domain, same auth boundary, zero new infra. Cons: couples docs build to cockpit deploy.
2. **As a sibling service in the deployment.** `docs.srw.local` — its own pod, its own ingress, nginx serving the bundle. Pros: independent lifecycle, easy to publish to public internet later. Cons: one more deployment.

**Decision: option 1 for v1.** Ship it as a cockpit route. We can always extract it to a sibling service later if we want a public URL — the bundle is just static files. Until we have external integrators reading the docs, the cockpit route is the right home.

### CI Integration

A new GitHub Actions step in `.github/workflows/production.yml`, sequenced after the lint step and before the build step:

```yaml
- name: Generate and lint API docs
  run: |
    pip install -e .  # ensure orchestrator + agent imports work
    bash docs-site/build.sh
- name: Upload docs bundle
  uses: actions/upload-artifact@v4
  with:
    name: api-docs-${{ github.sha }}
    path: docs-site/dist/
```

The bundle is then either copied into the cockpit image (`docker/cockpit/Dockerfile` adds a `COPY --from=docs-bundle docs-site/dist/ /usr/share/nginx/html/api-docs/`) or published as a separate artifact, depending on the hosting decision above.

### What Gets Linted

`redocly lint` rules (configured in `docs-site/.redocly.yaml`):

- `operation-summary` — every endpoint must have a summary
- `operation-description` — every endpoint must have a description (we can start with `recommended` and tighten to `error`)
- `operation-tag-defined` — tags must be declared in the spec's top-level `tags` list
- `operation-operationId-unique` — no duplicate operation IDs
- `no-empty-servers` — at least one server URL declared
- `parameter-description` — every parameter has a description
- Custom rule: every endpoint that uses `Security(http_bearer)` must declare `responses: 401: ...` and `responses: 403: ...`

The custom WebSocket lint (`scripts/lint_websocket_docs.py`) walks every `@app.websocket(...)` and `@router.websocket(...)` decorator in the codebase, and asserts each route appears in `docs-site/content/api/websockets.md`.

The custom MCP docstring lint (in `scripts/generate_mcp_docs.py` itself) raises if any `@mcp.tool` has an empty docstring.

## What Could Go Wrong

| Risk | Mitigation |
|------|-----------|
| Auth refactor breaks every endpoint at once | The change is mechanical and behavior-preserving. Land it in a single PR with the existing test suite as the safety net. Spot-check one endpoint per route group manually. The new dependency raises the same `HTTPException(401)` the old inline call did |
| Pydantic models drift from actual response shapes | Start with `Config: extra = "allow"` so fields the model doesn't know about pass through silently. Tighten over time. The cockpit's TypeScript types — generated from the spec — will surface drift fast |
| Lint rules so strict that adding a new endpoint is painful | Phase the rules in. Start with `operation-summary` and `operation-tag-defined` as errors, everything else as warnings. Promote to errors once the existing surface is clean |
| Generated spec is huge (~10k lines) and slow to render | Scalar handles 1000-endpoint specs without trouble; orchestrator at ~183 endpoints is small by comparison. Not a real concern |
| Cockpit-hosted docs route conflicts with Angular routing | Serve the docs bundle from a separate sub-path (`/api-docs`) with nginx SSI / Angular's `excludeRoutes` so Angular doesn't claim it. Standard pattern, well-trodden |
| MCP tool generator imports break because `orchestrator.mcp.server` has runtime side effects | The generator imports the module in a "stub mode" where database connections are mocked. If that's hard, fall back to running the MCP server with `MCP_STUB_MODE=1` and hitting `tools/list` over HTTP. Either works |
| `app.openapi()` called offline raises because lifespan hasn't run | `get_openapi(...)` is a pure function over the app's route table — it doesn't need lifespan. Tested in FastAPI's own test suite. Should Just Work; if it doesn't, fall back to running the server in a subprocess for the spec dump |
| Two security schemes (Bearer + MCP header) confuse the renderer | OpenAPI supports `security: [{Bearer: []}, {MCPHeader: []}]` as alternative requirements. Both Scalar and Redoc render this correctly as "either of these works." Verified pattern |
| CI build time grows noticeably | The docs build is fast (spec generation + lint + bundle is single-digit seconds). Not material on a multi-minute pipeline |
| WebSocket lint becomes a maintenance chore | If true, that's the signal to upgrade to option 3 (in-code annotation + extractor). The lint is cheap to write and cheap to delete |
| Scalar project loses momentum / archives | Specs remain valid OpenAPI; swap to Redoc by changing one build script line. Renderer is swappable; specs are durable |
| Orchestrator schema modules become a parallel source of truth that drifts from `database/postgres.py` | Keep API schemas separate from DB models on purpose. Cockpit never sees DB models. Drift between API and DB is normal and intentional — they evolve independently with explicit translation in the route layer |
| `response_model` strips fields the cockpit was relying on | This is the most likely real bug. Mitigation: cockpit consumes the generated TypeScript types post-refactor and the type checker catches the drop at build time. Until then, run with `response_model_exclude_unset=True` and permissive models |

## Implementation Plan

Phased to land in reviewable chunks. Each phase is independently shippable.

### Phase 1 — Foundation (no orchestrator changes yet)

Establish the docs build pipeline against the *currently* clean surfaces (agent worker, agent persistent) so the pipeline is proven before we touch the orchestrator.

#### Files to create

| File | Purpose |
|------|---------|
| `docs-site/package.json` | Scalar + Redocly CLI dependencies |
| `docs-site/scalar.config.ts` | Scalar configuration (sources, theme) |
| `docs-site/build.sh` | The pipeline entry point |
| `docs-site/.redocly.yaml` | Lint config |
| `docs-site/content/index.md` | Landing page |
| `docs-site/content/getting-started.md` | How to authenticate, where to find tokens |
| `docs-site/content/api/websockets.md` | Hand-written WebSocket reference (3 routes for v1) |
| `docs-site/content/api/mcp_tools.md` | Generated — committed initially, regenerated in CI |
| `scripts/generate_openapi.py` | Imports each FastAPI app, dumps `app.openapi()` to JSON |
| `scripts/generate_mcp_docs.py` | Walks `orchestrator/mcp/server.py` `@mcp.tool` registry, emits markdown |
| `scripts/lint_websocket_docs.py` | Asserts every `@app.websocket(...)` is mentioned in `websockets.md` |
| `tests/test_openapi_generation.py` | Smoke test: `app.openapi()` succeeds for each FastAPI app |

#### Files to modify

| File | Change |
|------|--------|
| `src/api/persistent_app.py` | Add tags + Pydantic response models to all 6 endpoints |
| `src/api/schemas/persistent.py` | New file — Pydantic models for the persistent endpoints |
| `.github/workflows/production.yml` | Add the docs generation + lint step after the existing lint job |
| `.gitignore` | `docs-site/dist/`, `docs-site/openapi/*.json` |

**Deliverable:** A docs site that documents the agent worker, agent persistent, MCP tools, and WebSockets. The orchestrator section is empty (or links to its raw `/docs`) until Phase 2. CI is gated.

### Phase 2 — Orchestrator Auth Refactor

The mechanical refactor that makes the orchestrator's OpenAPI useful. No new endpoints, no behavior changes.

#### Files to modify

| File | Change |
|------|--------|
| `orchestrator/security/auth.py` | Convert `get_current_user` and `require_approved_user` into proper FastAPI dependencies. Add `HTTPBearer` and `APIKeyHeader` security schemes |
| `orchestrator/main.py` | Replace every inline `await require_approved_user(request, db)` with `Depends(require_approved_user)`. ~183 routes. Mechanical |
| `tests/test_auth.py` | Add coverage for the dependency form (the existing tests cover the manual call form) |

**Deliverable:** Orchestrator's `/openapi.json` declares `securitySchemes` with both Bearer and MCP header. Swagger UI's "Authorize" button works. No content changes — the spec is still untyped, but auth is now documentable.

### Phase 3 — Orchestrator Typing Pass

The bulk of the work. Add response models, tags, summaries.

#### Files to create

| File | Purpose |
|------|---------|
| `orchestrator/api/__init__.py` | New package |
| `orchestrator/api/schemas/jobs.py` | Pydantic models for job-related endpoints |
| `orchestrator/api/schemas/agents.py` | Agent endpoints |
| `orchestrator/api/schemas/projects.py` | Project endpoints |
| `orchestrator/api/schemas/users.py` | User + auth endpoints |
| `orchestrator/api/schemas/datasources.py` | Datasource endpoints |
| `orchestrator/api/schemas/knowledge.py` | Knowledge / Neo4j endpoints |
| `orchestrator/api/schemas/memory.py` | Memory endpoints |
| `orchestrator/api/schemas/audit.py` | Audit / chat history / LLM request endpoints |
| `orchestrator/api/schemas/system.py` | Health / metrics / system endpoints |

#### Files to modify

| File | Change |
|------|--------|
| `orchestrator/main.py` | Add `response_model`, `tags`, `summary` to every `@app.*` route. Group routes by tag visually for ease of review |
| `docs-site/.redocly.yaml` | Promote `operation-summary` and `operation-tag-defined` from warnings to errors |

**Deliverable:** Orchestrator's spec has typed responses, tags, and summaries. Docs site shows the orchestrator section in full. Lint gates the new rules.

### Phase 4 — Optional Orchestrator Router Split

The optional refactor — turning `orchestrator/main.py` into per-domain `APIRouter`s. Skip if Phase 3 is too large; do it if a clean opportunity presents itself during Phase 3.

#### Files to create

| File | Purpose |
|------|---------|
| `orchestrator/api/routes/jobs.py` | `/api/jobs/*` |
| `orchestrator/api/routes/agents.py` | `/api/agents/*` |
| `orchestrator/api/routes/projects.py` | etc. |
| `orchestrator/api/routes/users.py` | |
| `orchestrator/api/routes/datasources.py` | |
| `orchestrator/api/routes/knowledge.py` | |
| `orchestrator/api/routes/memory.py` | |
| `orchestrator/api/routes/audit.py` | |
| `orchestrator/api/routes/ide.py` | IDE proxy + WS |
| `orchestrator/api/routes/auth.py` | Login / me / token endpoints |
| `orchestrator/api/routes/system.py` | Health / metrics |
| `orchestrator/api/routes/websockets.py` | All WebSocket endpoints |

#### Files to modify

| File | Change |
|------|--------|
| `orchestrator/main.py` | Reduces to lifespan + middleware + `app.include_router(...)` calls |

**Deliverable:** A 13,000-line `main.py` becomes a 200-line composer. Routing is unchanged.

### Phase 5 — Cockpit Integration

Wire the docs site into the cockpit and ship a "Docs" link in the cockpit nav.

#### Files to modify

| File | Change |
|------|--------|
| `docker/cockpit/Dockerfile` | `COPY` the docs bundle into the nginx image at `/usr/share/nginx/html/api-docs/` |
| `cockpit/nginx.conf` (or equivalent) | Serve `/api-docs/*` as static files; ensure Angular doesn't claim that path |
| `cockpit/src/app/layout/header.component.ts` | Add a "API Docs" link |

**Deliverable:** `cockpit.srw.local/api-docs` serves the aggregated reference. Linked from the cockpit header.

## Open Questions

1. **Schemas split or single file?** `orchestrator/api/schemas.py` (one big file) vs `orchestrator/api/schemas/jobs.py` (per-domain). Probably per-domain — even at 12 modules averaging 100 lines each it's more navigable than a single 1200-line file. But "single file first, split when it hurts" is a defensible alternative.

2. **Versioning.** Do we declare `version: 0.1.0` (matches the current `FastAPI(version=...)`) and never bump it, or do we tie the spec version to the git tag of the release? The latter is the right answer eventually but is unnecessary friction now. Probably: leave at `0.1.0` until we have an external consumer who cares.

3. **Generated TypeScript types for the cockpit.** Once we have a clean OpenAPI spec, `openapi-typescript` can generate `cockpit/src/types/api.ts` with full request/response types. This is a big quality-of-life win for cockpit development but adds yet another build step. Worth a separate feature doc rather than scoping it into this one.

4. **MCP "endpoints" tab in the docs site.** Should the MCP section be a sibling of the OpenAPI sections in the navigation, or a clearly-labeled separate area? Probably sibling, with an explanatory note at the top of the page that says "These are MCP tools, not REST endpoints — they're called via the MCP protocol, not HTTP."

5. **Public publishing.** When (if ever) does this docs site become public-facing at `docs.srw.io` or similar? Out of scope for the implementation, but the choice of "static bundle, no auth" makes the eventual extraction trivial.

6. **AsyncAPI for WebSockets.** Worth revisiting if we add a 4th or 5th WebSocket route. With 3 routes the markdown approach is fine; past ~6 it gets fragile.

7. **Spec diffing in PRs.** Tools like `redocly diff` or `oasdiff` can post API change summaries on PRs ("this PR adds 3 endpoints, removes 1, changes the response shape of 2"). Useful if external integrators ever appear; nice-to-have until then.

## Future Extensions

- **Generated TypeScript types for the cockpit** — `openapi-typescript` against the orchestrator spec produces `cockpit/src/types/api.ts`. End of "the cockpit guesses what the orchestrator returns."
- **Generated Python client** — `openapi-python-client` against the orchestrator spec produces a typed client library for use in scripts and tests. Currently we hand-write `requests.get(...)` calls.
- **Spec diffing in PRs** — `oasdiff base.json head.json` posted as a PR comment. Catches accidental breaking changes before merge.
- **Public docs site** — Extract `docs-site/dist/` to a standalone deployment behind a public URL once we have external integrators.
- **AsyncAPI for WebSockets** — Upgrade from hand-written markdown to a real spec when WebSocket count justifies it.
- **Code samples in multiple languages** — Scalar supports multi-language examples generated from the spec. Worth turning on once the surface is stable.
- **Versioned API + spec** — Declare `version` from the git tag, archive each release's spec under `docs-site/openapi/v0.1.0.json`, etc. Becomes important when external integrators ask "what changed in v0.4?"
- **Spec-first endpoint design** — Once the typing pass is done, future endpoints can be designed by writing the Pydantic model first and the route second. Closer to spec-first development without going full OpenAPI-as-source-of-truth.
- **MCP tools rendered with the same UX as REST endpoints** — A custom Scalar plugin (or a separate static page using the same theme) that presents MCP tools as if they were endpoints, including a fake "Try it out" that constructs the MCP protocol message. Big lift, big payoff for MCP adoption.

## References

- [FastAPI — OpenAPI customization](https://fastapi.tiangolo.com/how-to/extending-openapi/)
- [FastAPI — Security schemes via dependencies](https://fastapi.tiangolo.com/tutorial/security/)
- [Scalar — open source API reference](https://github.com/scalar/scalar)
- [Redocly CLI — lint, bundle, build](https://redocly.com/docs/cli/)
- [Spectral — alternative OpenAPI linter](https://github.com/stoplightio/spectral)
- [openapi-typescript — generate TS types from OpenAPI](https://github.com/openapi-ts/openapi-typescript)
- [oasdiff — OpenAPI breaking-change detection](https://github.com/oasdiff/oasdiff)
- [AsyncAPI Initiative — WebSocket / event-driven API spec](https://www.asyncapi.com/)
- [Browserbase docs](https://docs.browserbase.com/) — example of a Scalar-rendered modern API reference
- [Stripe API reference](https://docs.stripe.com/api) — the gold standard, hand-curated wrapper around generated OpenAPI
- [GitHub REST API description](https://github.com/github/rest-api-description) — example of "spec lives in its own repo, multiple consumers"
