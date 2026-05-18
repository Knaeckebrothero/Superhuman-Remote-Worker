# Orchestrator — `main.py` has become a 19k-line monolith

## Symptom (observed 2026-05-18, `develop` at `9b5ff74`)

`orchestrator/main.py` is now **19,032 lines** and contains:

- **241 HTTP endpoints**, all decorated with `@app.<method>` (zero `APIRouter` usage)
- **367 top-level function definitions** (handlers + helpers, mixed freely)
- Six handlers/helpers over 300 lines each
- 19 raw psycopg cursor references inside route handlers (SQL embedded in HTTP layer)
- Module-level lookup tables at lines 15674, 16080, 16097 (provider/cloud field maps)

CLAUDE.md still describes this file as "~11500 lines" — it has grown ~65 % since that line was written and no one updated the doc, which is itself a smell.

## Endpoint distribution

Routes are spread across **30 distinct `/api/<domain>` prefixes**, plus four outside the `/api/` namespace (`/magic/*`, `/ws/persistent/*`):

| Prefix         | Count | Plausible router module |
|---------------|------:|--------------------------|
| `/api/jobs`         | 57 | `routers/jobs.py` |
| `/api/projects`     | 35 | `routers/projects.py` |
| `/api/admin`        | 30 | `routers/admin.py` |
| `/api/agents`       | 14 | `routers/agents.py` (internal) |
| `/api/persistent`   | 13 | `routers/persistent.py` |
| `/api/settings`     | 11 | `routers/settings.py` |
| `/api/sudo`         | 10 | `routers/sudo.py` |
| `/api/datasources`  |  6 | `routers/datasources.py` |
| `/api/codex`        |  6 | `routers/codex.py` |
| `/api/users`        |  5 | `routers/users.py` |
| `/api/builder`      |  5 | `routers/builder.py` |
| `/api/vms`          |  4 | `routers/vms.py` |
| `/api/stats`        |  4 | `routers/stats.py` |
| `/api/api-keys`     |  4 | `routers/api_keys.py` |
| `/api/tables`       |  3 | `routers/tables.py` |
| `/api/notifications`|  3 | `routers/notifications.py` |
| `/api/mcp-tokens`   |  3 | (merge with auth) |
| `/api/experts`      |  3 | `routers/experts.py` |
| (15 more, 1–2 each) | 19 | grouped into the above |
| `/magic/*`          |  3 | `routers/magic.py` |
| `/ws/persistent/*`  |  1 | `routers/persistent.py` |
| **Total**           | **241** | |

Top five domains alone (jobs / projects / admin / agents / persistent) carry **149 of 241 endpoints (62 %)**.

## The largest functions in the file

| Lines | Function | Notes |
|------:|----------|-------|
| 718 | `_trigger_dispatch()` (line 2324) | Auto-assign dispatcher core; this one function alone is bigger than most well-designed Python modules |
| 476 | `create_thread()` | Single POST handler |
| 354 | `_try_dispatch_pending_jobs()` (line 1947) | Dispatcher polling loop body |
| 347 | `create_job()` | Single POST handler |
| 336 | `lifespan()` | App startup/shutdown |
| 319 | `persistent_ws_proxy()` | WebSocket proxy to agent |
| 271 | `complete_job()` | Single POST handler |
| 270 | `_inject_dispatch_credentials()` | Credential injection helper |
| 269 | `send_builder_message()` | Single POST handler |
| 226 | `_trigger_verification_on_complete()` | Background subjob spawn |
| 226 | `resume_job()` | Single POST handler |
| 218 | `_dispatch_job_to_agent()` | HTTP POST to agent pod IP |
| 199 | `promote_job()` | Single POST handler |

A handler with 270+ lines of inline business logic, often containing direct SQL via asyncpg, is not a thin route — it's a service masquerading as one.

## Root cause

Three habits compound:

1. **No `APIRouter` was ever introduced.** Every endpoint added since the project started has gone on `@app.…` in the same file, by inertia. There is no "where should this go" decision to make — there is only the one place.
2. **Handlers contain the business logic.** Service modules (`orchestrator/services/completion.py`, etc.) exist and are used in places, but the dominant pattern is `@app.post(...)` → 200 lines of asyncpg + branching + side effects → response shape. The service layer is incomplete, not absent.
3. **Background workers live in the same file.** `_trigger_dispatch`, `_try_dispatch_pending_jobs`, `_dispatch_job_to_agent`, `_inject_dispatch_credentials`, `_inject_model_credentials`, `_inject_env_key_credentials`, `_trigger_verification_on_complete`, `_trigger_curation_final_pass` — roughly 1700+ lines of dispatcher/credentials code is glued to the HTTP app. A separate `orchestrator/dispatch/` package is begging to be born.

## Why this is bad

- **Editing pain.** Loading the file in any tool is slow; `grep` results blur; you cannot tell whether you're looking at a handler, a helper, or a background loop without scrolling.
- **Merge-conflict surface.** Every PR that adds an endpoint touches the same file. Two devs adding to `/api/jobs` and `/api/projects` collide for no structural reason.
- **Hidden coupling.** Helpers like `_inject_dispatch_credentials` are reachable from anywhere in the file. Refactors are scary because you can't easily prove no other handler depends on a global.
- **Test ergonomics.** There is no way to test "the projects API" in isolation — you import the entire FastAPI app and its lifespan. New tests inherit the import cost of the whole monolith.
- **Onboarding tax.** A new contributor sees one 19 k-line file and gives up on building a mental model. The actual architecture (jobs subsystem, dispatcher, persistent-session layer, admin tooling) is invisible.
- **Documentation rot.** CLAUDE.md says 11.5 k; reality is 19 k. The doc that's supposed to help Claude navigate the codebase is wrong by 65 %, because the file's size makes it embarrassing to mention out loud.

## Why the agent side is fine and this one isn't

For reference, the agent (`src/`, 60 k lines total) is **123 files** organized into `core/`, `tools/{12 domains}/`, `managers/`, `llm/`, `services/`, `api/`, `database/`. The biggest single file (`src/graph.py`, 3804 lines) is a state machine with 10 nodes — its size is intrinsic to its job. There is nothing comparable to a 19 k-line FastAPI app on the agent side.

Two large agent files do deserve a separate look later (`src/api/persistent_app.py` at 3349 lines and `src/core/loader.py` at 3459 lines) but they are an order of magnitude smaller than the issue here.

## Proposed fix

A two-axis refactor done in slices, no single big-bang PR.

### Axis 1 — split routes into `APIRouter` modules

Target structure:

```
orchestrator/
  main.py                       # ~300 lines: app construction, middleware, lifespan, router includes
  routers/
    __init__.py
    jobs.py                     # 57 endpoints
    projects.py                 # 35
    admin.py                    # 30
    agents.py                   # 14 (internal-only, mounted with require_internal)
    persistent.py               # 13 + ws_proxy
    settings.py                 # 11
    sudo.py                     # 10
    datasources.py              # 6
    codex.py                    # 6
    users.py                    # 5
    builder.py                  # 5
    vms.py                      # 4
    stats.py                    # 4
    api_keys.py                 # 4 + mcp-tokens merge
    misc.py                     # everything ≤3 endpoints, grouped sensibly
    magic.py                    # 3 magic-link endpoints
```

`main.py` keeps:
- FastAPI app construction
- CORS, middleware, `require_internal` wiring
- `lifespan` (or extract to `lifecycle.py` if it doesn't shrink)
- All `app.include_router(...)` calls

### Axis 2 — extract dispatch and credential injection

The 1700+ lines starting at line 749 (`_inject_dispatch_credentials`) and ending around 2324 (`_trigger_dispatch`) form a coherent subsystem that has nothing to do with HTTP. Move to:

```
orchestrator/
  dispatch/
    __init__.py
    dispatcher.py               # _trigger_dispatch, _try_dispatch_pending_jobs
    agent_client.py             # _dispatch_job_to_agent (POST to agent pod IP)
    credentials.py              # _inject_dispatch_credentials, _inject_model_credentials,
                                # _inject_env_key_credentials
    triggers.py                 # _trigger_verification_on_complete, _trigger_curation_final_pass
```

Routes that *kick* dispatch (e.g. `create_job`, `resume_job`) call into `orchestrator.dispatch.dispatcher.trigger(...)` instead of having the logic inline.

### Axis 3 — push SQL out of handlers (opportunistic, not blocking)

Each time a router file is created, lift the asyncpg cursor blocks inside its handlers into `orchestrator/services/<domain>.py`. Routes become 5–20 lines: validate input → call service → shape response. Don't gate the router split on this; it can happen per-domain as touched.

## Suggested sequencing

Each step is its own PR, mergeable independently, no behavior change expected:

1. **Introduce `APIRouter` scaffolding** — create `orchestrator/routers/__init__.py`, move one small router first (e.g. `stats.py`, 4 endpoints) end-to-end. Validates the pattern, tests, OpenAPI schema unchanged. ~1 day.
2. **Migrate top-5 domains** — jobs, projects, admin, agents, persistent. Largest payoff. One PR each, ~1–1.5 days per domain, in that order (jobs first reveals the most shared helpers).
3. **Migrate the long tail** — remaining 15 small domains batched into 2–3 PRs, ~1 day total.
4. **Extract dispatch subsystem** — separate PR, no route changes. ~2 days. Includes moving `_trigger_dispatch` and friends, fixing imports, adding `orchestrator/dispatch/__init__.py` exports.
5. **Service-layer extraction (ongoing)** — opportunistic per domain. Not part of the headline split PR series; let it bleed in naturally during feature work.

## Acceptance criteria

- [ ] `orchestrator/main.py` under **1000 lines** (was 19 032)
- [ ] No domain router file exceeds **2500 lines**; flag for further split if it does
- [ ] Zero `@app.<method>` decorators outside `main.py` (everything is on a router)
- [ ] All endpoint paths preserved exactly (`/api/jobs/...` etc.) — OpenAPI schema diff is empty
- [ ] `_trigger_dispatch` and the credential-injection helpers live in `orchestrator/dispatch/`, not `main.py`
- [ ] CI green: `ruff check`, `ruff format --check`, full pytest suite
- [ ] CLAUDE.md updated with the new layout — no more "11500 lines" lie

## Effort estimate

- Scaffolding + first small router: ~1 day
- Top-5 domain migrations: ~6 days (1–1.5 each)
- Long-tail migrations: ~2 days
- Dispatch extraction: ~2 days
- Docs (CLAUDE.md, new README in `routers/`, `dispatch/`): ~0.5 day

**Total: ~2 engineering weeks**, parallelizable since per-domain PRs touch disjoint code once the scaffolding is in.

## Related

- `docs/features/orchestrator_ha_scaling.md` — the active-active rework relies on isolating singleton loops (advisory locks, `LISTEN/NOTIFY`); having dispatch as a real package makes that work tractable.
- `docs/features/auth_bff_and_api_tokens.md` — touches 5+ scattered auth handlers; would be far easier to land against a `routers/auth.py` than against `main.py`.
- `docs/features/agent_open_source_split.md` — orchestrator stays closed-source, but a clean module layout makes the agent↔orchestrator HTTP contract easier to document for downstream users.
- Agent-side parallels worth checking next: `src/api/persistent_app.py` (3349 lines, same FastAPI-monolith shape on a smaller scale) and `src/core/loader.py` (3459 lines, config deep-merge that tends to grow tendrils).
