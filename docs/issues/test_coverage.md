# Test Coverage: 33% → 70% Plan

**Date**: 2026-03-26
**Current coverage**: 33% line / unknown branch (30,583 lines total, 20,542 uncovered)
**Target coverage**: ~70% line, ~60% branch
**Test suite**: 1,808 passing, 8 flaky, 37 skipped across 1,856 collected tests

## Problem

33% code coverage is too low, even for a single-developer project. Google's internal guidance considers 60% "acceptable", 75% "commendable", and 90% "exemplary" ([source](https://testing.googleblog.com/2020/08/code-coverage-best-practices.html)). Large sections of the codebase — particularly tool implementations, services, and orchestrator routes — have zero or near-zero coverage. This means regressions from refactors (like the recent creator/validator removal) slip through silently.

### Why "no database" is not an excuse

| Dependency | Testing approach |
|------------|-----------------|
| **PostgreSQL** | **Testcontainers** (`testcontainers[postgres]`) for integration tests — spins up a real Postgres container. For fast unit tests, mock `asyncpg` at the pool/connection level. Avoid SQLite as a substitute — query syntax differences cause false positives. |
| **MongoDB** | **Testcontainers** (`testcontainers[mongodb]`) or `mongomock` for unit tests. |
| **Neo4j** | **Testcontainers** (`testcontainers[neo4j]`) or mock the driver/session. |
| **External APIs** (OpenAI, Tavily, etc.) | Mock HTTP with `respx` (for httpx) or `responses` (for requests). LangChain provides `FakeListChatModel` for deterministic LLM test doubles. |
| **Shell/tmux** | Mock `libtmux` server/session objects. |
| **Filesystem** | Use `tmp_path` pytest fixture (built-in). |

Testcontainers requires Docker/Podman. CI has Docker; locally we use Podman (set `TESTCONTAINERS_RYUK_DISABLED=true` and `DOCKER_HOST`).

### Line coverage vs. branch coverage

- **Line coverage** checks if each line executed. A single pass through a function can hit 100% of lines without testing error paths.
- **Branch coverage** checks if both true and false paths of each conditional were taken. Strictly more comprehensive.
- **Track both.** Use `--cov-branch` with pytest-cov. Set CI gates on line coverage (simpler) but monitor branch coverage in reports to spot untested conditionals.

## Current Coverage by Area

### Well Covered (>70%) — maintain and extend

| Module | Coverage | Notes |
|--------|----------|-------|
| `src/core/loader.py` | 87% | Config loading, matrix resolvers |
| `src/core/context.py` | 84% | Context management, compaction |
| `src/llm/key_ring.py` | 95% | Key rotation |
| `src/llm/reasoning_chat.py` | 80% | LLM wrapper, overflow detection |
| `src/managers/todo_manager.py` | 89% | Todo CRUD |
| `src/managers/plan_manager.py` | 82% | Plan management |
| `src/services/embedding_service.py` | 97% | Embedding |
| `src/services/audio_helper.py` | 91% | Audio transcription |
| `src/tools/research/web.py` | 94% | Web search tools |
| `src/tools/git/git_tools.py` | 100% | Git tools |
| `orchestrator/database/postgres.py` | 85% | DB queries |

### Critically Under-Covered (<30%) — highest impact targets

| Module | Coverage | Lines | Approach |
|--------|----------|-------|----------|
| `src/tools/knowledge/knowledge_tools.py` | 4% | 300 | Mock vector DB, test CRUD operations |
| `src/tools/sql/postgresql.py` | 7% | 120 | Mock asyncpg, test query building |
| `src/tools/mongodb/mongo.py` | 8% | 150 | Testcontainers or mongomock |
| `src/tools/workspace/filesystem.py` | 15% | 264 | `tmp_path`, test file operations |
| `src/tools/document/processing.py` | 11% | 89 | Mock document renderer |
| `src/services/document_renderer.py` | 15% | 172 | Mock poppler/PIL subprocess |
| `src/services/knowledge_graph.py` | 0% | 166 | Mock Neo4j driver |
| `src/services/knowledge_store.py` | 0% | 120 | Mock vector DB |
| `src/services/assembler_tools.py` | 0% | 60 | Pure logic, straightforward |
| `src/tools/coding/claude_code.py` | 0% | 96 | Mock subprocess/CLI |
| `src/utils/document_processor.py` | 14% | 401 | Mock file I/O, test each parser |
| `src/utils/pdf.py` | 14% | 106 | Mock poppler |
| `src/tools/citation/sources.py` | 18% | 387 | Mock CitationEngine |
| `src/tools/communication/messaging.py` | 12% | 104 | Mock NATS client |
| `orchestrator/main.py` | 35% | ~800 | `httpx.AsyncClient` + dependency overrides |
| `orchestrator/services/formatters.py` | 68% | ~200 | Pure functions, easy wins |
| `orchestrator/services/builder_dispatch.py` | 30% | ~150 | Mock tool calls |
| `orchestrator/services/builder_tools.py` | 25% | ~200 | Mock orchestrator client |
| `orchestrator/services/vm_provisioner.py` | 0% | ~300 | Mock K8s/NATS clients |
| `orchestrator/services/nats_bridge.py` | 0% | ~400 | Mock NATS client |
| `orchestrator/services/sudo_gate.py` | 0% | ~250 | Mock DB + NATS |

### Zero-Coverage Entry Points

| Module | Lines | Approach |
|--------|-------|----------|
| `src/agent.py` | 1,879 | Mock graph + DB, test lifecycle methods |
| `src/init.py` | 443 | Mock DB connections, test init sequence |
| `src/core/workspace.py` | 671 | `tmp_path`, test workspace CRUD |
| `src/core/workspace_injection.py` | 149 | Unit test injection logic |
| `src/core/phase_snapshot.py` | 505 | `tmp_path` + mock DB |

## Priority Order

Work that gets to 70% fastest, ordered by impact-to-effort ratio:

### Phase 1: Low-Hanging Fruit (33% → 45%)

Pure/near-pure logic, minimal mocking needed:

1. **`orchestrator/services/formatters.py`** — pure formatting functions, just add more cases
2. **`src/services/assembler_tools.py`** — pure logic, 60 lines, zero deps
3. **`src/tools/workspace/filesystem.py`** — use `tmp_path`, no DB needed
4. **`src/tools/workspace/files.py`** — use `tmp_path`, mock workspace manager
5. **`src/core/workspace.py`** — use `tmp_path`, test CRUD
6. **`src/core/workspace_injection.py`** — small module, test injection formatting
7. **`src/utils/config.py`** — pure config helpers
8. **`src/utils/citation_utils.py`** — pure utility functions
9. **`src/tools/core/todo.py`** — mock TodoManager, test tool wrappers
10. **`src/tools/core/job.py`** — mock DB, test job tools

### Phase 2: Mock Database Layer (45% → 55%)

Create shared test fixtures for mocked DB connections, then cover:

1. **`orchestrator/main.py`** — `httpx.AsyncClient` + FastAPI dependency overrides
2. **`src/tools/sql/postgresql.py`** — mock asyncpg connection, test query building
3. **`src/tools/mongodb/mongo.py`** — testcontainers or mongomock, test all CRUD ops
4. **`src/tools/knowledge/knowledge_tools.py`** — mock vector DB, test store/retrieve
5. **`src/services/knowledge_graph.py`** — mock Neo4j driver, test Cypher queries
6. **`src/services/knowledge_store.py`** — mock vector DB, test similarity search
7. **`src/database/postgres_db.py`** (remaining gaps) — more edge cases
8. **`orchestrator/services/builder_dispatch.py`** — mock tool calls
9. **`orchestrator/services/builder_tools.py`** — mock client responses

### Phase 3: Mock External Services (55% → 65%)

1. **`src/tools/research/browser.py`** — mock Playwright, test extraction logic
2. **`src/tools/citation/sources.py`** — mock CitationEngine, test wrapper logic
3. **`src/services/document_renderer.py`** — mock poppler subprocess, test rendering pipeline
4. **`src/services/vision_helper.py`** — mock OpenAI vision API with `respx`
5. **`src/utils/document_processor.py`** — mock file I/O, test each format parser
6. **`src/utils/pdf.py`** — mock poppler, test text extraction
7. **`src/tools/coding/shell_manager.py`** (remaining gaps) — mock libtmux
8. **`src/tools/coding/claude_code.py`** — mock subprocess
9. **`src/tools/communication/messaging.py`** — mock NATS
10. **`src/tools/research/papers.py`** — mock arxiv/unpaywall clients

### Phase 4: Integration & Entry Points (65% → 70%)

1. **`src/agent.py`** — mock graph execution, test lifecycle (init, resume, phase transitions)
2. **`src/init.py`** — mock DB connections, test initialization sequence
3. **`src/core/phase_snapshot.py`** — `tmp_path` + mock DB, test snapshot save/restore
4. **`orchestrator/services/vm_provisioner.py`** — mock K8s client
5. **`orchestrator/services/nats_bridge.py`** — mock NATS client
6. **`orchestrator/services/sudo_gate.py`** — mock DB + NATS
7. **`src/tools/registry.py`** (remaining gaps) — test phase filtering edge cases

## Testing LangGraph Agents

The agent system uses LangGraph's state machine pattern. Testing strategy should work at two levels:

### Node-Level Unit Tests (primary)

Call graph node functions directly without the LangGraph runtime. Pass in a mock state dict, assert the returned state updates. Fast, deterministic, no LLM calls.

```python
# Test the execute node in isolation
def test_execute_node_calls_tool():
    state = {"messages": [...], "phase": "tactical", "todos": [...]}
    result = execute_node(state)
    assert result["messages"][-1].tool_calls[0]["name"] == "expected_tool"
```

Nodes to cover: `init_workspace`, `execute`, `check_todos`, `handle_transition`, `check_goal`, `archive_phase`.

### Graph-Level Integration Tests (secondary)

Use LangChain's `FakeListChatModel` for deterministic LLM responses + in-memory checkpointer:

```python
from langchain_community.chat_models.fake import FakeListChatModel

chat = FakeListChatModel(responses=[
    "I'll analyze the document.",
    '{"tool": "read_file", "args": {"path": "doc.pdf"}}',
    "Analysis complete.",
])
```

This allows testing full phase transitions (strategic → tactical → strategic) without hitting real LLM APIs. Assert on the trajectory (sequence of tool calls and state transitions), not on output text quality.

Output quality evaluation belongs in a separate evaluation pipeline (e.g., LangSmith AgentEvals), not in unit tests.

## Shared Test Infrastructure

### `pyproject.toml` Configuration

Add to project root:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = [
    "slow: marks tests as slow (deselect with '-m \"not slow\"')",
    "integration: requires external services (containers, APIs)",
]

[tool.coverage.run]
source = ["src", "orchestrator"]
branch = true
omit = [
    "*/tests/*",
    "*/migrations/*",
    "*/__pycache__/*",
]

[tool.coverage.report]
fail_under = 33
show_missing = true
exclude_lines = [
    "pragma: no cover",
    "if __name__ == .__main__.",
    "if TYPE_CHECKING:",
    "raise NotImplementedError",
]
```

Setting `asyncio_mode = "auto"` prevents a dangerous pitfall: without it, an `async def test_*` function missing the `@pytest.mark.asyncio` decorator **silently passes without running**.

### `conftest.py` Fixtures

Organize by scope — put fixtures in the narrowest `conftest.py` that covers their users:

```
tests/
    conftest.py              # Shared: temp dirs, env vars, fake configs, singleton resets
    tools/
        conftest.py          # Tool-specific: mock LLM (FakeListChatModel), mock state
        research/
            conftest.py      # Research-specific: mock HTTP (respx)
    orchestrator/
        conftest.py          # API: AsyncClient, mock DB, dependency overrides
```

Key fixtures needed:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

# Mocked async PostgreSQL pool
@pytest.fixture
def mock_pg_pool():
    """Returns a mock asyncpg pool with configurable query results."""
    pool = AsyncMock()
    pool.acquire.return_value.__aenter__ = AsyncMock()
    pool.acquire.return_value.__aexit__ = AsyncMock()
    return pool

# Mocked MongoDB client (or use mongomock)
@pytest.fixture
def mock_mongo_client():
    """Returns a mocked PyMongo/Motor client."""
    client = MagicMock()
    client.server_info.return_value = {"version": "7.0"}
    return client

# Mocked Neo4j driver
@pytest.fixture
def mock_neo4j_driver():
    """Returns a mock Neo4j driver with session context manager."""
    driver = MagicMock()
    session = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session)
    driver.session.return_value.__exit__ = MagicMock()
    return driver

# FastAPI test client with mocked dependencies
@pytest.fixture
async def orchestrator_client():
    """Returns an httpx AsyncClient with all DB deps mocked."""
    from httpx import ASGITransport, AsyncClient
    from orchestrator.main import app
    app.dependency_overrides[get_db] = lambda: mock_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()

# Temporary workspace directory
@pytest.fixture
def workspace(tmp_path):
    """Creates a realistic workspace directory structure."""
    (tmp_path / "workspace.md").write_text("# Workspace")
    (tmp_path / "plan.md").write_text("# Plan")
    (tmp_path / "todos.yaml").write_text("todos: []")
    (tmp_path / "archive").mkdir()
    (tmp_path / "documents").mkdir()
    (tmp_path / "tools").mkdir()
    return tmp_path
```

### Test Dependencies

Add to a `requirements-dev.txt`:

```
pytest>=9.0
pytest-asyncio>=1.0
pytest-cov>=7.0
pytest-timeout>=2.3
pytest-randomly>=3.16
pytest-xdist>=3.5
respx>=0.22
mongomock>=4.3
testcontainers>=4.2
hypothesis>=6.120
diff-cover>=9.2
```

## FastAPI Testing Pattern

Use `httpx.AsyncClient` with `ASGITransport` for async endpoint testing (the orchestrator is fully async). Use FastAPI's dependency injection overrides to swap real DB connections for mocks:

```python
@pytest.fixture(autouse=True)
def override_deps():
    app.dependency_overrides[get_db] = lambda: mock_db
    yield
    app.dependency_overrides.clear()  # always clean up

@pytest.mark.asyncio
async def test_list_jobs():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/jobs")
        assert response.status_code == 200
        assert isinstance(response.json(), list)
```

For mocking HTTP calls the orchestrator makes to agents, use `respx`:

```python
import respx

@respx.mock
async def test_agent_heartbeat():
    respx.post("http://agent:8001/heartbeat").respond(200, json={"status": "ok"})
    # ... test code that triggers the heartbeat call ...
```

## Property-Based Testing (Hypothesis)

Not needed everywhere, but high value for specific areas where edge cases are hard to enumerate:

| Area | Property to test |
|------|-----------------|
| Config deep merge (`src/core/loader.py`) | Merging any two valid configs produces a valid config; `null` always clears; arrays always replace |
| Token counting (`src/core/context.py`) | Count is always non-negative; compaction always reduces message count |
| Todo YAML roundtrip (`src/managers/todo.py`) | `parse(serialize(todos)) == todos` |
| KeyRing rotation (`src/llm/key_ring.py`) | After N consecutive failures with N keys, all keys have been tried |
| Matrix resolution (`src/core/loader.py`) | Resolution always returns a file path or raises; 4-level fallback is deterministic |

Skip Hypothesis for LLM responses, database queries, and API endpoints — those are better served by `parametrize` and mocks.

## Mutation Testing

**Not yet.** At 33% coverage, mutation testing (`mutmut`) generates overwhelming numbers of surviving mutants. It becomes valuable at **75%+ coverage** to verify that tests contain meaningful assertions, not just line execution.

When to add: once coverage exceeds 70%, run `mutmut` on critical modules (`src/core/context.py`, `src/managers/todo.py`, `src/tools/registry.py`) to identify tests that execute code but don't assert behavior.

## CI Enforcement

### Current State

CI runs `pytest tests/ -x -q --tb=short` with no coverage tracking. `ruff check` + `ruff format --check` are the only quality gates.

### Phased CI Improvement

**Step 1 — Add coverage reporting (now):**

```yaml
- name: Run tests with coverage
  run: |
    pytest tests/ -x -q --tb=short \
      --cov=src --cov=orchestrator \
      --cov-branch \
      --cov-report=xml:coverage.xml \
      --cov-report=term-missing \
      --cov-fail-under=33
```

**Step 2 — Add diff-cover for PR quality (high impact):**

```yaml
- name: Check coverage on changed files
  run: |
    diff-cover coverage.xml --compare-branch=origin/main --fail-under=80
```

This enforces that **new/changed code** in a PR must have 80% coverage, even while overall coverage is lower. This is the most effective ratcheting mechanism — overall coverage increases organically because every PR raises the bar.

**Step 3 — Ratchet the floor:**

Store `fail_under` in `pyproject.toml`. Every time coverage improves by 5 points, commit the new floor. CI ensures it never decreases.

**Step 4 — PR coverage comments (optional):**

Use the [Pytest Coverage Comment](https://github.com/marketplace/actions/pytest-coverage-comment) GitHub Action to post before/after coverage diffs on PRs.

## Flaky Tests to Fix

8 tests fail intermittently in full-suite runs but pass in isolation:

| Test | Root Cause | Fix |
|------|-----------|-----|
| `test_audio_helper.py::test_init_with_openai_key` | Singleton retains state from earlier test | `autouse` fixture that resets singleton |
| `test_embedding_service.py::test_init_with_openai_key` | Same singleton issue | Same fix |
| `test_embedding_service.py::test_local_provider_*` (3) | Singleton state pollution | Same fix |
| `test_database_phase1.py::TestMongoDB::*` (3) | Motor client import side-effects | Isolate with `monkeypatch` |

**Detection**: Add `pytest-randomly` to CI — randomizes test order, exposes hidden ordering dependencies. Run suite 3x with different seeds to catch intermittent failures.

**Band-aid vs. fix**: `pytest-rerunfailures` can auto-retry flaky tests but masks root causes. Fix the singletons instead.

## Recommended Plugin Stack

| Plugin | Purpose |
|--------|---------|
| `pytest-cov` | Coverage reporting + branch coverage |
| `pytest-asyncio` | Async test support (use `asyncio_mode = "auto"`) |
| `pytest-xdist` | Parallel test execution (`-n auto`) |
| `pytest-timeout` | Kill hanging tests (`--timeout=30`) |
| `pytest-randomly` | Randomize order to detect state leaks |
| `respx` | Mock httpx HTTP calls |
| `mongomock` | In-memory MongoDB for unit tests |
| `testcontainers` | Real DB containers for integration tests |
| `hypothesis` | Property-based testing for edge cases |
| `diff-cover` | PR-level coverage enforcement |

## Notes

- See also `docs/issues/tests.md` for an older detailed per-module gap analysis
- The cockpit (Angular) has 101 tests, all passing — coverage tracking not yet set up
- Async testing: pytest-asyncio 1.0+ creates a fresh event loop per test automatically — the old `event_loop` fixture pattern is removed
- Testcontainer fixtures should use `scope="session"` to avoid per-test container startup overhead
