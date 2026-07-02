"""Tests for the spawn_subagent light backend (Phase 3).

Two layers:
- unit: orchestration with acquire/create_llm/run/release mocked — resolves the
  subagent model tier, validates input, wraps the result, releases the env even
  on error, assigns a unique index per call, and bounds concurrency by
  max_parallel.
- integration: a real reader env (git repo + FilesystemTestBackend) driven by a
  fake LLM (create_llm monkeypatched) — the reader runs end-to-end and its
  worktree is cleaned up.
"""

import asyncio
import subprocess
from types import SimpleNamespace

from langchain_core.messages import AIMessage

import pytest

import src.tools.delegation.spawn_subagent as spawn_mod
from src.core.loader import LLMConfig, _parse_phase_override
from src.tools.context import ToolContext
from src.tools.delegation.spawn_subagent import (
    _format_result,
    _resolve_subagent_config,
    create_spawn_subagent_tools,
)

from tests._fs_backend import FilesystemTestBackend


# --- pure helpers -----------------------------------------------------------


class TestResolveSubagentConfig:
    def test_uses_subagent_tier_when_set(self):
        llm = LLMConfig(
            model="opus", subagent=_parse_phase_override({"model": "sonnet"})
        )
        assert _resolve_subagent_config(llm).model == "sonnet"

    def test_falls_back_to_tactical(self):
        llm = LLMConfig(model="base", tactical=_parse_phase_override({"model": "tac"}))
        assert _resolve_subagent_config(llm).model == "tac"

    def test_falls_back_to_base(self):
        llm = LLMConfig(model="base")
        assert _resolve_subagent_config(llm).model == "base"

    def test_none_config(self):
        assert _resolve_subagent_config(None) is None


class TestFormatResult:
    def test_header_with_role(self):
        out = _format_result("reader", "do the thing", "FINDINGS")
        assert out.startswith("[subagent done] — role: reader")
        assert "do the thing" in out
        assert out.endswith("FINDINGS")

    def test_header_without_role(self):
        out = _format_result("", "task", "R")
        assert out.startswith("[subagent done]\ntask: task")


# --- unit orchestration (mocked infra) --------------------------------------


def _light_ctx(**over):
    """A bare light-mode context carrying a real subagent LLM tier."""
    ns = SimpleNamespace(
        config={"delegation": {"mode": "light", "light": {"max_parallel": 2}}},
        _resolved_tool_names=["read_file"],
        _llm_config=LLMConfig(
            model="opus", subagent=_parse_phase_override({"model": "sonnet"})
        ),
        _limits=None,
    )
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


class _FakeEnv:
    def __init__(self, index):
        self.index = index
        self.tools = []
        self.port_block = "PORTS"


class TestLightOrchestration:
    @pytest.fixture
    def wired(self, monkeypatch):
        """Patch acquire/create_llm/run/release; return a record of calls."""
        rec = {"acquired": [], "released": [], "created_with": []}

        async def fake_acquire(context, tool_names, *, index, allow_writes=False):
            rec["acquired"].append(index)
            return _FakeEnv(index)

        async def fake_release(env):
            rec["released"].append(env.index)

        def fake_create_llm(cfg, limits=None):
            rec["created_with"].append(cfg.model)
            return SimpleNamespace(bind_tools=lambda tools: "BOUND_LLM")

        async def fake_run(**kwargs):
            return "READER RESULT"

        monkeypatch.setattr(spawn_mod, "acquire_reader_env", fake_acquire)
        monkeypatch.setattr(spawn_mod, "release_reader_env", fake_release)
        monkeypatch.setattr(spawn_mod, "create_llm", fake_create_llm)
        monkeypatch.setattr(spawn_mod, "run_light_subagent", fake_run)
        return rec

    @pytest.mark.asyncio
    async def test_happy_path_wraps_result(self, wired):
        (tool,) = create_spawn_subagent_tools(_light_ctx())
        out = await tool.coroutine(task_description="read the sources", role="reader")
        assert "READER RESULT" in out
        assert out.startswith("[subagent done] — role: reader")
        assert wired["acquired"] == [0]
        assert wired["released"] == [0]
        # Built the reader LLM from the subagent tier, not the base model.
        assert wired["created_with"] == ["sonnet"]

    @pytest.mark.asyncio
    async def test_release_called_on_run_error(self, wired, monkeypatch):
        async def boom(**kwargs):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(spawn_mod, "run_light_subagent", boom)
        (tool,) = create_spawn_subagent_tools(_light_ctx())
        out = await tool.coroutine(task_description="x")
        assert "Error: subagent failed" in out
        assert "kaboom" in out
        assert wired["released"] == [0]  # env torn down despite the error

    @pytest.mark.asyncio
    async def test_unique_index_per_call(self, wired):
        (tool,) = create_spawn_subagent_tools(_light_ctx())
        await asyncio.gather(
            tool.coroutine(task_description="a"),
            tool.coroutine(task_description="b"),
            tool.coroutine(task_description="c"),
        )
        assert sorted(wired["acquired"]) == [0, 1, 2]  # distinct worktrees/ports

    @pytest.mark.asyncio
    async def test_empty_task_rejected_before_acquire(self, wired):
        (tool,) = create_spawn_subagent_tools(_light_ctx())
        out = await tool.coroutine(task_description="  ")
        assert "task_description is required" in out
        assert wired["acquired"] == []  # never touched infra

    @pytest.mark.asyncio
    async def test_max_parallel_bounds_concurrency(self, monkeypatch):
        active = {"now": 0, "peak": 0}

        async def fake_acquire(context, tool_names, *, index, allow_writes=False):
            return _FakeEnv(index)

        async def fake_release(env):
            pass

        def fake_create_llm(cfg, limits=None):
            return SimpleNamespace(bind_tools=lambda tools: "L")

        async def fake_run(**kwargs):
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
            await asyncio.sleep(0.02)
            active["now"] -= 1
            return "R"

        monkeypatch.setattr(spawn_mod, "acquire_reader_env", fake_acquire)
        monkeypatch.setattr(spawn_mod, "release_reader_env", fake_release)
        monkeypatch.setattr(spawn_mod, "create_llm", fake_create_llm)
        monkeypatch.setattr(spawn_mod, "run_light_subagent", fake_run)

        # max_parallel=2 in the ctx; launch 5 concurrent calls.
        (tool,) = create_spawn_subagent_tools(_light_ctx())
        await asyncio.gather(
            *[tool.coroutine(task_description=str(i)) for i in range(5)]
        )
        assert active["peak"] == 2

    @pytest.mark.asyncio
    async def test_no_llm_config_fails_closed(self, wired):
        ctx = _light_ctx(_llm_config=None)
        (tool,) = create_spawn_subagent_tools(ctx)
        out = await tool.coroutine(task_description="x")
        assert "no LLM config" in out
        assert wired["acquired"] == []


# --- integration: real reader env + fake LLM --------------------------------


class _ScriptedChat:
    """Fake chat model: bind_tools returns self; ainvoke replays responses."""

    def __init__(self, responses):
        self._responses = list(responses)

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        if self._responses:
            return self._responses.pop(0)
        return AIMessage(content="[default]")


@pytest.fixture
def light_parent_ctx(tmp_path):
    from src.core.workspace import WorkspaceManager, WorkspaceManagerConfig
    from src.managers.git_manager import GitManager

    ws = tmp_path / "workspace"
    ws.mkdir()

    def _git(*a):
        subprocess.run(["git", *a], cwd=ws, capture_output=True)

    _git("init")
    _git("config", "user.email", "t@t.com")
    _git("config", "user.name", "T")
    (ws / "README.md").write_text("# parent")
    _git("add", ".")
    _git("commit", "-m", "init")

    parent_ws = WorkspaceManager(
        job_id="parent-job",
        config=WorkspaceManagerConfig(structure=[], base_path=str(ws)),
        backend=FilesystemTestBackend(ws),
    )
    parent_ws._initialized = True
    parent_ws._git_manager = GitManager(ws)

    ctx = ToolContext(
        workspace_manager=parent_ws,
        _job_metadata={"job_id": "parent-job"},
        config={"delegation": {"mode": "light", "light": {}}, "shell": {}},
    )
    ctx._llm_config = LLMConfig(model="base")
    ctx._resolved_tool_names = ["read_file"]
    return ctx, ws


class TestLightIntegration:
    @pytest.mark.asyncio
    async def test_reader_runs_end_to_end_and_cleans_up(
        self, light_parent_ctx, monkeypatch
    ):
        ctx, ws = light_parent_ctx

        # A reader that answers immediately (no tool calls).
        def fake_create_llm(cfg, limits=None):
            return _ScriptedChat([AIMessage(content="DISTILLED FINDINGS")])

        monkeypatch.setattr(spawn_mod, "create_llm", fake_create_llm)

        (tool,) = create_spawn_subagent_tools(ctx)
        out = await tool.coroutine(
            task_description="Read the sources and summarize.", role="reader"
        )

        assert "DISTILLED FINDINGS" in out
        assert out.startswith("[subagent done] — role: reader")
        # Worktree created during the call and removed on teardown.
        assert not (ws / ".worktrees" / "sub_0").exists()


# --- metering (Phase 4) -----------------------------------------------------


class _Inner:
    """Minimal chat model: records ainvoke calls, returns scripted responses."""

    def __init__(self, responses=None):
        self._responses = list(responses or [AIMessage(content="ok")])
        self.calls = 0

    async def ainvoke(self, messages, *a, **k):
        self.calls += 1
        return self._responses.pop(0) if self._responses else AIMessage(content="ok")


class TestMeteredLLM:
    @pytest.mark.asyncio
    async def test_each_ainvoke_archives_under_parent_job(self, monkeypatch):
        rows = []
        monkeypatch.setattr(
            spawn_mod, "archive_llm_request", lambda **kw: rows.append(kw)
        )
        llm = spawn_mod._MeteredLLM(
            _Inner(), job_id="parent-job", agent_type="scholar", model="sonnet", index=2
        )
        await llm.ainvoke(["m1"])
        await llm.ainvoke(["m2"])
        assert len(rows) == 2
        assert all(r["job_id"] == "parent-job" for r in rows)
        assert all(r["call_type"] == "subagent" for r in rows)
        assert all(r["model"] == "sonnet" for r in rows)
        assert rows[0]["auxiliary_metadata"] == {"subagent_index": 2}

    @pytest.mark.asyncio
    async def test_metering_failure_does_not_break_reader(self, monkeypatch):
        def boom(**kw):
            raise RuntimeError("audit db down")

        monkeypatch.setattr(spawn_mod, "archive_llm_request", boom)
        inner = _Inner([AIMessage(content="still works")])
        llm = spawn_mod._MeteredLLM(
            inner, job_id="j", agent_type="a", model="m", index=0
        )
        resp = await llm.ainvoke(["m"])
        assert resp.content == "still works"  # invoke returns despite metering error

    @pytest.mark.asyncio
    async def test_no_job_id_skips_metering(self, monkeypatch):
        rows = []
        monkeypatch.setattr(
            spawn_mod, "archive_llm_request", lambda **kw: rows.append(kw)
        )
        llm = spawn_mod._MeteredLLM(
            _Inner(), job_id="", agent_type="a", model="m", index=0
        )
        await llm.ainvoke(["m"])
        assert rows == []  # nothing to attribute to → no row

    def test_delegates_unknown_attrs(self):
        inner = _Inner()
        inner.bind_tools = lambda tools: "BOUND"
        llm = spawn_mod._MeteredLLM(
            inner, job_id="j", agent_type="a", model="m", index=0
        )
        assert llm.bind_tools([]) == "BOUND"

    @pytest.mark.asyncio
    async def test_light_tool_meters_end_to_end(self, light_parent_ctx, monkeypatch):
        """A real light-tool call archives the reader's LLM turn under the parent."""
        ctx, _ = light_parent_ctx
        rows = []
        monkeypatch.setattr(
            spawn_mod, "archive_llm_request", lambda **kw: rows.append(kw)
        )
        monkeypatch.setattr(
            spawn_mod,
            "create_llm",
            lambda cfg, limits=None: _ScriptedChat([AIMessage(content="R")]),
        )
        (tool,) = create_spawn_subagent_tools(ctx)
        await tool.coroutine(task_description="summarize the sources")
        assert len(rows) == 1
        assert rows[0]["job_id"] == "parent-job"
        assert rows[0]["call_type"] == "subagent"
        assert rows[0]["model"] == "base"  # ctx has no subagent tier → base fallback


# --- config plumbing (Phase 5 regression) ------------------------------------


class TestDelegationConfigPlumbing:
    """`delegation` is a parsed/known config field, so it is NOT in config.extra.

    agent.py therefore injects `asdict(config.delegation)` into tool_config
    explicitly. Before that injection existed, the factory saw no `delegation`
    key at all and silently fell back to the heavy stub even for mode: light
    experts (and delegate_work's call-time `enabled` check could never pass).
    These tests pin the whole chain on the real expert configs.
    """

    @staticmethod
    def _load(expert):
        from src.core.loader import load_agent_config, resolve_config_path

        path, deployment_dir = resolve_config_path(expert)
        return load_agent_config(path, deployment_dir)

    def test_defaults_stay_heavy(self):
        cfg = self._load("defaults")
        assert cfg.delegation.mode == "heavy"

    @pytest.mark.parametrize("expert", ["scholar", "critic"])
    def test_expert_resolves_light_with_spawn_subagent_grant(self, expert):
        cfg = self._load(expert)
        assert cfg.delegation.mode == "light"
        # light knobs deep-merge in from defaults under the expert's override
        assert cfg.delegation.light.get("max_parallel") == 3
        assert cfg.tools.delegation == ["spawn_subagent"]

    @pytest.mark.parametrize("expert", ["scholar", "critic"])
    def test_delegation_absent_from_extra(self, expert):
        # The premise for the explicit tool_config injection: known fields are
        # stripped from extra. If this flips, the injection becomes redundant
        # (harmless) — update agent.py/persistent_session.py accordingly.
        cfg = self._load(expert)
        assert "delegation" not in cfg.extra

    @pytest.mark.asyncio
    @pytest.mark.parametrize("expert", ["scholar", "critic"])
    async def test_agent_tool_config_dispatches_light_backend(self, expert):
        """Reproduce agent.py's tool_config construction end-to-end."""
        from dataclasses import asdict

        cfg = self._load(expert)
        tool_config = {
            **cfg.extra,
            "agent_id": cfg.agent_id,
            "delegation": asdict(cfg.delegation),
        }
        ctx = ToolContext(config=tool_config)
        (tool,) = create_spawn_subagent_tools(ctx)
        reply = await tool.coroutine(task_description="")
        # Light backend validates input; heavy stub errors about delegate_work.
        assert "task_description is required" in reply
        assert "delegate_work" not in reply
