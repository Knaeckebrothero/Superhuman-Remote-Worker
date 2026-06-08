# AGENTS.md

This is Codex's repository guidance file. `CLAUDE.md` also exists for Claude Code;
keep behavior-critical guidance in sync between the two when changing project
norms.

## Project Summary

Superhuman Remote Worker is a multi-tier AI agent orchestration system.

- `orchestrator/`: FastAPI backend on port 8085. It owns job/session APIs,
  dispatch, auth, migrations, persistence, and the MCP server.
- `agent.py` + `src/`: LangGraph-based agent runtime. The same codebase runs
  batch jobs and persistent interactive sessions via YAML expert configs.
- `cockpit/`: Angular 21 web UI with standalone components, signals, Transloco
  i18n, REST/WebSocket/SSE data flows, and Vitest tests.
- `helm/`, `deployment/`, `docker/`, `vm/`: Kubernetes, GitOps, container, and
  workspace/VM infrastructure.
- `docs/`: design notes, feature plans, issue analyses, and operational runbooks.
- `Advanced-LLM-Chat/`, `CitationEngine/`, and `HomeLab/` are nested/external
  projects in this checkout. Treat them as separate scopes unless the task
  explicitly targets them.

## Working Agreements

- Prefer narrowly scoped edits that follow the existing architecture. This repo
  is already broad; avoid opportunistic refactors.
- Read the relevant docs under `docs/features/`, `docs/issues/`, or
  `docs/done/` before changing behavior that has an existing design trail.
- For larger features, create or update a plan/design note under `docs/features/`
  or `docs/issues/` before implementation.
- Do not print, commit, or summarize secret values from `.env`, local overlays,
  kube secrets, or private deployment files. Use `.env.example` and
  `deployment/values-local.example.yaml` as public references.
- Expect a dirty worktree and nested repositories. Do not clean up untracked
  directories or revert unrelated changes.

## Common Commands

Python setup and local services:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
podman-compose -f docker-compose.dev.yaml up -d
python init.py
```

Run backend and agent locally:

```bash
uvicorn orchestrator.main:app --reload --port 8085
python agent.py --port 8001 --loop
python agent.py --config scholar --port 8001 --loop
python agent.py --mode worker --port 8001 --loop
python agent.py --mode persistent --port 8002 --loop
```

Cockpit:

```bash
cd cockpit
npm ci
npm start
npm test
npm run i18n:check
npm run build
```

Python verification:

```bash
pytest tests/ -x -q --tb=short
pytest tests/test_<area>.py -x -q --tb=short
ruff check src/ orchestrator/ tests/
ruff format --check src/ orchestrator/ tests/
ruff format src/ orchestrator/ tests/
```

Helm verification for chart changes:

```bash
helm lint helm/ -f helm/ci/test-values.yaml
helm lint helm/ -f helm/ci/customer-external-values.yaml
```

Local k3d/Tilt workflow:

```bash
./scripts/local-dev-tilt-up.sh
k3d cluster start srw
tilt up
tilt down
k3d cluster stop srw
```

## Architecture Rules

- Orchestrator is the authority for final job status. Agent/graph code should set
  `freeze_data`, `should_stop`, and completion payloads; it should not directly
  decide or persist final job state.
- Job status flow is `created -> processing -> completed | failed |
  pending_review | paused`.
- `orchestrator/main.py` is still a large route module. Keep endpoint edits near
  related routes and move shared behavior into `orchestrator/services/` when it
  reduces real duplication.
- Use `orchestrator/database/migrations/app/` and
  `orchestrator/database/migrations/vector/` for schema changes. Do not edit
  `orchestrator/database/schema.sql` or `vector_schema.sql`; they are reference
  snapshots. Use `.notx.sql` for non-transactional migrations such as
  `CREATE INDEX CONCURRENTLY`.
- Use existing JSONB merge helpers, especially for `jobs.context`. Avoid direct
  read-modify-write assignments that can race with dispatch/session updates.
- MongoDB, Neo4j, NATS, and other optional services should degrade gracefully
  unless the feature explicitly requires them.
- Agents operate through isolated workspaces, normally SSH/SFTP-backed. Tests may
  use test-only filesystem backends from `tests/`; do not import test backends
  into production `src/`.
- Tool registration lives in `src/tools/registry.py`. Respect strategic/tactical
  phase restrictions and runtime gates.
- Do not manually patch Kubernetes deployments as a normal fix path. Helm/Fleet
  values and chart templates are the source of truth.
- `deployment/legacy/` is reference-only unless the task specifically asks for
  legacy deployment updates.

## Testing Notes

- Pytest async tests need `@pytest.mark.asyncio`.
- Use `AsyncMock` for awaitable collaborators.
- Keep `config.extra` as a real dict in tests; using `MagicMock()` there can
  break YAML/config handling.
- tmux-dependent tests may auto-skip when tmux is unavailable.
- Cockpit tests use Vitest with jsdom and `cockpit/src/test-setup.ts`.
- Use `vi.fn()` for Cockpit mocks and `of()` for RxJS observables.
- Angular signal mocks need callable values with `.set()` / `.update()` when the
  production signal exposes those methods.
- When adding UI copy, update both `cockpit/src/assets/i18n/en.json` and
  `cockpit/src/assets/i18n/de-DE.json`, then run `npm run i18n:check`.

## Frontend Notes

- Cockpit is Angular 21 with standalone components and signal-based state. Do not
  introduce NgRx or a new state framework without explicit direction.
- Keep UI changes aligned with the existing Cockpit design system. Useful
  references include `docs/design_framework.md`, `docs/cockpit_ds.md`, and
  `cockpit/src/styles/README.md`.
- Prefer existing shared services/components over one-off implementations.
- For user-visible workflows, verify in the running app when practical, not only
  through unit tests.

## CI Notes

- `.github/workflows/main.yml` is the full blocking pipeline for `main`.
- `.github/workflows/develop.yml` is change-based and auto-formats Python on
  pushes to `develop`; still run local checks before pushing.
- CI uses Python 3.12, Node 22, npm for Cockpit, and Ruff 0.14.10.
- The Playwright version is pinned in `.playwright-version` and must match the
  `playwright==...` pin in `requirements.txt`.
