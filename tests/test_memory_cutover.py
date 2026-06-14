"""Cutover wiring tests for the MemoryManager seam (Phase 1, slice 5).

The equivalence suites (tests/test_memory_*_equivalence.py) pin the seam's
*logic* against verbatim reproductions of the legacy blocks. This suite pins
the *wiring* that slice 5 added to production code:

- the YAML pipeline defaults parse and every configured name binds,
- both graphs construct the manager behind memory.manager.enabled with the
  right MemoryRuntime (stores, prompts, per-mode retrieval timeout),
- flag-on call sites route through assemble()/capture() with the right
  request/event shapes and skip the legacy direct-store blocks,
- the B11 terminate capture fires for detach-style endings exactly once,
- B10: every injection message the manager produces is recognized by the
  summarization-exclusion predicate (is_workspace_injection_message), so
  compaction keeps stripping injected pairs after the cutover.
"""

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)

from src.core.loader import QueryConfig, load_agent_config
from src.core.workspace import WorkspaceManager
from src.core.workspace_injection import (
    TODOS_INJECTION_CONTENT_PREFIX,
    is_workspace_injection_message,
)
from src.managers import TodoManager, PlanManager
from src.services.memory import (
    AssembleStats,
    Candidate,
    InjectionBlock,
    MemoryManager,
    MemoryPayload,
    MemoryRuntime,
)
from src.tools.context import ToolContext
from tests._fs_backend import FilesystemTestBackend
from tests._memory_fixtures import (
    PROJECT_ID,
    make_kb_mock,
    make_memories,
    make_notes,
    make_recall_mock,
)

REPO_ROOT = Path(__file__).parent.parent
WORKER_CONFIG_PATH = str(REPO_ROOT / "config" / "defaults.yaml")
PERSISTENT_CONFIG_PATH = str(REPO_ROOT / "config" / "persistent_defaults.yaml")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class RecordingManager:
    """Manager stub for wiring tests — records calls, returns a fixed payload.

    The call sites only depend on the seam API (assemble/capture), so a
    recorder proves the wiring without re-testing seam internals.
    """

    def __init__(self, payload: Optional[MemoryPayload] = None) -> None:
        self.payload = payload or MemoryPayload()
        self.assemble_requests: List[Any] = []
        self.captures: List[Any] = []

    async def assemble(self, req: Any) -> MemoryPayload:
        self.assemble_requests.append(req)
        return self.payload

    async def capture(self, event: Any) -> None:
        self.captures.append(event)


def make_payload(
    memory_text: str = "MEMBLOCK", knowledge_text: str = "KBBLOCK"
) -> MemoryPayload:
    """A payload with real injection pairs (the production message shapes)."""
    from src.core.knowledge_injection import create_knowledge_injection_messages
    from src.core.memory_injection import create_memory_injection_messages

    blocks: List[InjectionBlock] = []
    if memory_text:
        blocks.append(
            InjectionBlock(
                kind="memory",
                content=memory_text,
                messages=list(create_memory_injection_messages(memory_text)),
                token_count=26,
                items=[
                    {"record_id": "m1", "token_count": 12},
                    {"record_id": "m2", "token_count": 14},
                ],
            )
        )
    if knowledge_text:
        blocks.append(
            InjectionBlock(
                kind="knowledge",
                content=knowledge_text,
                messages=list(create_knowledge_injection_messages(knowledge_text)),
                token_count=0,
                items=[{"record_id": "k1", "token_count": 0}],
            )
        )
    return MemoryPayload(blocks=blocks, stats=AssembleStats(blocks=len(blocks)))


class FakeContextMgr:
    """Minimal real-typed context manager for driving the execute node.

    Explicit class instead of MagicMock — the node does arithmetic and
    comparisons on these values (the B1 mock-config lesson).
    """

    def __init__(self, ensure_hook=None) -> None:
        self.config = SimpleNamespace(
            compaction_threshold_tokens=100_000,
            summarization_threshold_tokens=100_000,
        )
        self._state = SimpleNamespace(summaries=[])
        self._ensure_hook = ensure_hook

    def set_current_phase(self, phase: str) -> None:
        pass

    def get_token_count(self, messages: List[Any]) -> int:
        return 10

    async def ensure_within_limits(self, messages, *args, **kwargs):
        if self._ensure_hook is not None:
            return self._ensure_hook(messages, self)
        return messages

    def clear_old_tool_results(self, messages):
        return messages


@pytest.fixture
def workspace_manager(tmp_path):
    ws = WorkspaceManager(
        job_id="cutover-job-1",
        base_path=tmp_path,
        backend=FilesystemTestBackend(tmp_path),
    )
    ws.initialize()
    return ws


@pytest.fixture
def worker_config():
    return load_agent_config(WORKER_CONFIG_PATH)


@pytest.fixture
def persistent_config():
    return load_agent_config(PERSISTENT_CONFIG_PATH)


# ---------------------------------------------------------------------------
# 1. YAML pipeline defaults
# ---------------------------------------------------------------------------


class TestPipelineDefaults:
    """The shipped YAML defaults: flag on (cutover), per-mode pipelines bind."""

    def test_worker_defaults(self, worker_config):
        m = worker_config.memory
        assert m.manager_enabled is True  # cutover flag on (Phase-1 closure)
        assert m.pipeline.retrievers == ["recall_two_tier", "kb_notes"]
        assert m.pipeline.writers == [
            "interval_extractor",
            "phase_boundary_extractor",
            "memory_assembler",
            "compaction_memory",
            "queued_memory",
        ]

    def test_persistent_defaults(self, persistent_config):
        m = persistent_config.memory
        assert m.manager_enabled is True
        assert m.pipeline.retrievers == ["recall_two_tier", "kb_notes"]
        assert m.pipeline.writers == [
            "persistent_interval_extractor",
            "teardown_extractor",
        ]

    def test_configured_names_bind(self, worker_config, persistent_config):
        """Registry/YAML drift guard: every shipped name must resolve."""
        for cfg in (worker_config, persistent_config):
            manager = MemoryManager.from_config(cfg.memory, MemoryRuntime())
            summary = manager.pipeline_summary()
            assert summary["retrievers"] == cfg.memory.pipeline.retrievers
            assert summary["writers"] == cfg.memory.pipeline.writers

    def test_manager_flag_survives_dispatch_round_trip(self, worker_config):
        """Dispatch paths re-parse a live config after dataclasses.asdict()
        + deep_merge (job config_override in src/agent.py, session config
        assembly and config.update in persistent_app). asdict emits the
        flat dataclass field name (``manager_enabled``), not the YAML
        nesting (``manager.enabled``) — the parser must accept both or the
        cutover flag silently resets to False on every dispatched
        job/session. Found live on k3d during Phase-1 closure step 1
        (scholar job built its graph with no bind log), 2026-06-11.
        """
        import dataclasses

        from src.core.loader import deep_merge, load_agent_config_from_dict

        worker_config.memory.manager_enabled = True
        base = dataclasses.asdict(worker_config)
        merged = deep_merge(base, {"llm": {"model": "gemma-test"}})
        reparsed = load_agent_config_from_dict(merged)
        assert reparsed.memory.manager_enabled is True
        assert (
            reparsed.memory.pipeline.retrievers
            == worker_config.memory.pipeline.retrievers
        )
        assert reparsed.memory.pipeline.writers == worker_config.memory.pipeline.writers


# ---------------------------------------------------------------------------
# 2. Construction wiring
# ---------------------------------------------------------------------------


class TestWorkerConstruction:
    """build_phase_alternation_graph builds the manager behind the flag."""

    def _build(self, config, workspace_manager, tool_context):
        from src.graph import build_phase_alternation_graph

        return build_phase_alternation_graph(
            strategic_llm_with_tools=MagicMock(),
            tactical_llm_with_tools=MagicMock(),
            tools=[],
            config=config,
            workspace=workspace_manager,
            todo_manager=TodoManager(workspace_manager),
            tool_context=tool_context,
        )

    def test_flag_on_constructs_with_worker_runtime(
        self, worker_config, workspace_manager
    ):
        worker_config.memory.manager_enabled = True
        marker_store = object()
        ctx = ToolContext(workspace_manager=workspace_manager)
        ctx.recall_store = marker_store

        with patch(
            "src.services.memory.MemoryManager.from_config",
            return_value=RecordingManager(),
        ) as from_config:
            self._build(worker_config, workspace_manager, ctx)

        assert from_config.call_count == 1
        cfg_arg, runtime = from_config.call_args[0]
        assert cfg_arg is worker_config.memory
        assert runtime.recall_store is marker_store
        # has_knowledge() is False (no graph connection) → same gate as the
        # legacy execute block: no knowledge store in the runtime.
        assert runtime.knowledge_store is None
        assert runtime.retrieval_timeout is None  # worker runs unbounded
        assert runtime.memory_config is worker_config.memory
        assert runtime.auxiliary_config is worker_config.auxiliary
        # Matrix-resolved prompts threaded from the graph factory
        assert isinstance(runtime.extraction_prompt, str)
        assert runtime.extraction_prompt
        assert isinstance(runtime.assembler_prompt, str)
        assert runtime.assembler_prompt
        assert runtime.job_id == workspace_manager.job_id

    def test_flag_off_never_constructs(self, worker_config, workspace_manager):
        worker_config.memory.manager_enabled = False  # rollback-lever state
        ctx = ToolContext(workspace_manager=workspace_manager)

        with patch(
            "src.services.memory.MemoryManager.from_config",
            return_value=RecordingManager(),
        ) as from_config:
            self._build(worker_config, workspace_manager, ctx)

        from_config.assert_not_called()


class TestPersistentConstruction:
    """PersistentSession._setup_memory builds the manager behind the flag."""

    def _make_session(self, persistent_config):
        from src.api.persistent_session import PersistentSession

        return PersistentSession(thread_id=str(uuid.uuid4()), config=persistent_config)

    @pytest.mark.asyncio
    async def test_flag_on_constructs_with_persistent_runtime(self, persistent_config):
        persistent_config.memory.manager_enabled = True
        session = self._make_session(persistent_config)
        aux_marker = object()
        session.auxiliary_llm = aux_marker
        session.memory_extraction_prompt = "EXTRACT-PROMPT"

        recorder = RecordingManager()
        with (
            patch(
                "src.services.embedding_service.get_embedding_service",
                return_value=MagicMock(verify_dimensions=AsyncMock()),
            ),
            patch(
                "src.services.memory.MemoryManager.from_config",
                return_value=recorder,
            ) as from_config,
        ):
            session._setup_memory(None, vector_conn=MagicMock())

        assert from_config.call_count == 1
        cfg_arg, runtime = from_config.call_args[0]
        assert cfg_arg is persistent_config.memory
        assert runtime.recall_store is session.recall_store
        assert runtime.knowledge_store is session.knowledge_store
        assert runtime.auxiliary_llm is aux_marker
        assert runtime.extraction_prompt == "EXTRACT-PROMPT"
        assert runtime.assembler_prompt is None  # persistent: no assembler
        assert runtime.retrieval_timeout == 5.0  # legacy per-call guard
        assert runtime.job_id == session.thread_id
        assert runtime.project_ids == []
        assert session.memory_service is recorder

    @pytest.mark.asyncio
    async def test_flag_off_never_constructs(self, persistent_config):
        persistent_config.memory.manager_enabled = False  # rollback-lever state
        session = self._make_session(persistent_config)

        with (
            patch(
                "src.services.embedding_service.get_embedding_service",
                return_value=MagicMock(verify_dimensions=AsyncMock()),
            ),
            patch("src.services.memory.MemoryManager.from_config") as from_config,
        ):
            session._setup_memory(None, vector_conn=MagicMock())

        from_config.assert_not_called()
        assert session.memory_service is None


# ---------------------------------------------------------------------------
# 3. Worker execute node wiring (read swap + turn_end + compaction)
# ---------------------------------------------------------------------------


def _make_execute_node(config, workspace_manager, todo_manager, ctx, service, mgr):
    from src.graph import create_execute_node

    return create_execute_node(
        strategic_llm_with_tools=MagicMock(),
        tactical_llm_with_tools=mgr["llm"],
        todo_manager=todo_manager,
        memory_manager=MagicMock(),  # vestigial workspace.md manager
        workspace_manager=workspace_manager,
        config=config,
        context_mgr=mgr["context"],
        retry_manager=MagicMock(),
        auxiliary_llm=MagicMock(),
        summarization_prompt="summarize",
        memory_extraction_prompt="extract",
        memory_assembler_prompt="assemble",
        tool_context=ctx,
        tool_names=None,
        memory_service=service,
    )


def _worker_state():
    return {
        "job_id": "cutover-job-1",
        "iteration": 0,
        "messages": [HumanMessage(content="start")],
        "is_strategic_phase": False,
        "phase_number": 2,
        "turn_count": 0,
        "metadata": {},
    }


@pytest.fixture
def execute_env(worker_config, workspace_manager):
    """Real config/managers, mock LLM, recording manager, legacy-store spies."""
    todo_manager = TodoManager(workspace_manager)
    todo_manager.add("Do the task")

    ctx = ToolContext(workspace_manager=workspace_manager)
    # Legacy-store spies: the flag-on path must never touch them directly.
    ctx.recall_store = AsyncMock()
    ctx.knowledge_store = None

    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=AIMessage(content="done"))

    service = RecordingManager(payload=make_payload())
    return {
        "config": worker_config,
        "workspace": workspace_manager,
        "todo": todo_manager,
        "ctx": ctx,
        "llm": llm,
        "service": service,
        "context": FakeContextMgr(),
    }


class TestWorkerExecuteWiring:
    @pytest.mark.asyncio
    async def test_flag_on_read_write_wiring(self, execute_env):
        env = execute_env
        auditor = MagicMock()
        node = _make_execute_node(
            env["config"],
            env["workspace"],
            env["todo"],
            env["ctx"],
            env["service"],
            {"llm": env["llm"], "context": env["context"]},
        )

        with (
            patch("src.graph.get_archiver", return_value=auditor),
            # The real prompt templates aren't under test (their Jinja
            # blocks trip get_phase_system_prompt's .format with the
            # bare default model) — the wiring is.
            patch("src.graph.get_phase_system_prompt", return_value="SYS"),
        ):
            result = await node(_worker_state())
        await asyncio.sleep(0)  # let the fire-and-forget capture task run

        service = env["service"]

        # -- assemble request: legacy worker query formation
        assert len(service.assemble_requests) == 1
        req = service.assemble_requests[0]
        assert req.query_text == "Do the task phase 2 tactical"
        assert req.task_frame.top_todo == "Do the task"
        assert req.task_frame.phase_number == 2
        assert req.task_frame.is_strategic is False
        assert req.model == env["config"].llm.model

        # -- payload spliced right after the todos injection message
        prepared = env["llm"].ainvoke.call_args[0][0]
        todo_idx = next(
            i
            for i, m in enumerate(prepared)
            if isinstance(m, HumanMessage)
            and str(m.content).startswith(TODOS_INJECTION_CONTENT_PREFIX)
        )
        payload_msgs = service.payload.messages()
        assert prepared[todo_idx + 1 : todo_idx + 1 + len(payload_msgs)] == (
            payload_msgs
        )

        # -- legacy direct-store path skipped entirely
        env["ctx"].recall_store.decrement_ttl.assert_not_called()
        env["ctx"].recall_store.retrieve.assert_not_called()

        # -- audit fed from the payload's memory block (+ stats tap)
        audit_calls = [
            c
            for c in auditor.audit_step.call_args_list
            if c.kwargs.get("step_type") == "memory_inject"
        ]
        assert len(audit_calls) == 1
        data = audit_calls[0].kwargs["data"]
        assert data["count"] == 2
        assert data["total_tokens"] == 26
        assert "stats" in data

        # -- one turn_end capture with the legacy fields
        assert [e.kind for e in service.captures] == ["turn_end"]
        event = service.captures[0]
        assert event.turn_count == 1
        assert event.phase == 2
        assert event.extra["current_injection_text"] == "MEMBLOCK"

        # -- interval state stays writer-internal (no checkpointed keys)
        assert "last_observed_turn" not in result
        assert "last_assembled_turn" not in result
        assert result["turn_count"] == 1

    @pytest.mark.asyncio
    async def test_flag_on_compaction_capture(self, execute_env):
        env = execute_env

        def compact(messages, mgr):
            mgr._state.summaries.append("COMPACTION-SUMMARY")
            return [RemoveMessage(id="rm-1")] + list(messages)

        env["context"] = FakeContextMgr(ensure_hook=compact)
        node = _make_execute_node(
            env["config"],
            env["workspace"],
            env["todo"],
            env["ctx"],
            env["service"],
            {"llm": env["llm"], "context": env["context"]},
        )

        with (
            patch("src.graph.get_archiver", return_value=None),
            patch("src.graph.get_phase_system_prompt", return_value="SYS"),
        ):
            await node(_worker_state())
        await asyncio.sleep(0)

        kinds = [e.kind for e in env["service"].captures]
        assert kinds == ["compaction", "turn_end"]
        event = env["service"].captures[0]
        assert event.extra["summary"] == "COMPACTION-SUMMARY"
        assert event.phase == 2
        # Legacy inline store skipped
        env["ctx"].recall_store.store.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Worker archive + tools node wiring
# ---------------------------------------------------------------------------


class TestArchiveNodeWiring:
    @pytest.mark.asyncio
    async def test_phase_boundary_capture(self, workspace_manager):
        from src.graph import create_archive_phase_node

        todo_manager = TodoManager(workspace_manager)
        plan_manager = PlanManager(workspace_manager)
        plan_manager.write("## Phase 1: Test\n\n- [x] Task 1")

        config = MagicMock()
        config.agent_id = "test-agent"
        config.extra = {}
        config.context_management.compact_on_archive = False
        config.llm.reasoning_level = None

        service = RecordingManager()
        recall_spy = AsyncMock()
        node = create_archive_phase_node(
            todo_manager,
            plan_manager,
            config,
            MagicMock(),
            MagicMock(),
            "summarize",
            recall_store=recall_spy,
            memory_service=service,
        )

        messages = [HumanMessage(content="work happened")]
        state = {
            "job_id": "cutover-job-1",
            "messages": messages,
            "phase_number": 3,
            "is_strategic_phase": False,
        }
        await node(state)
        await asyncio.sleep(0)

        assert [e.kind for e in service.captures] == ["phase_boundary"]
        event = service.captures[0]
        assert event.phase == 3
        assert event.messages is messages
        # Legacy direct extraction skipped
        recall_spy.assert_not_called()


class TestAuditedToolsWiring:
    @pytest.mark.asyncio
    async def test_todo_complete_capture_drains_queue(
        self, worker_config, workspace_manager
    ):
        from src.graph import create_audited_tool_node

        ctx = ToolContext(workspace_manager=workspace_manager)
        ctx.queue_memory(content="Queued insight", importance=0.7, source="todo")
        recall_spy = AsyncMock()
        service = RecordingManager()

        fake_tool = MagicMock()
        fake_tool.name = "fake_tool"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            mock_tn.ainvoke = AsyncMock(
                return_value={
                    "messages": [
                        ToolMessage(content="ok", tool_call_id="c1", name="fake_tool")
                    ]
                }
            )
            MockToolNode.return_value = mock_tn

            node = create_audited_tool_node(
                [fake_tool],
                worker_config,
                recall_store=recall_spy,
                tool_context=ctx,
                memory_service=service,
            )
            state = {
                "job_id": "cutover-job-1",
                "iteration": 1,
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[{"name": "fake_tool", "id": "c1", "args": {}}],
                    )
                ],
                "is_strategic_phase": False,
                "phase_number": 2,
                "metadata": {},
            }
            with patch("src.graph.get_archiver", return_value=None):
                await node(state)

        assert [e.kind for e in service.captures] == ["todo_complete"]
        queued = service.captures[0].extra["queued_memories"]
        assert len(queued) == 1
        assert queued[0]["content"] == "Queued insight"
        # Queue drained at the call site; store untouched directly
        assert ctx._pending_memories == []
        recall_spy.store.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_queue_emits_nothing(self, worker_config, workspace_manager):
        from src.graph import create_audited_tool_node

        ctx = ToolContext(workspace_manager=workspace_manager)
        service = RecordingManager()
        fake_tool = MagicMock()
        fake_tool.name = "fake_tool"

        with patch("src.graph.ToolNode") as MockToolNode:
            mock_tn = AsyncMock()
            mock_tn.ainvoke = AsyncMock(
                return_value={
                    "messages": [
                        ToolMessage(content="ok", tool_call_id="c1", name="fake_tool")
                    ]
                }
            )
            MockToolNode.return_value = mock_tn

            node = create_audited_tool_node(
                [fake_tool],
                worker_config,
                recall_store=None,
                tool_context=ctx,
                memory_service=service,
            )
            state = {
                "job_id": "cutover-job-1",
                "iteration": 1,
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[{"name": "fake_tool", "id": "c1", "args": {}}],
                    )
                ],
                "is_strategic_phase": False,
                "phase_number": 2,
                "metadata": {},
            }
            with patch("src.graph.get_archiver", return_value=None):
                await node(state)

        assert service.captures == []


# ---------------------------------------------------------------------------
# 5. Persistent turn wiring (read swap + insertion position)
# ---------------------------------------------------------------------------


def _persistent_callbacks():
    from src.persistent_graph import PersistentLoopCallbacks

    return PersistentLoopCallbacks(
        get_user_input=AsyncMock(return_value="hello"),
        on_token=AsyncMock(),
        on_thinking=AsyncMock(),
        on_tool_start=AsyncMock(),
        on_tool_result=AsyncMock(),
        permission_check=AsyncMock(return_value=True),
        on_turn_start=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_error=AsyncMock(),
        check_interrupt=lambda: None,
    )


class TestPersistentTurnWiring:
    @pytest.mark.asyncio
    async def test_flag_on_assemble_and_insertion(self):
        from src.persistent_graph import _execute_turn

        captured_prepared: List[List[Any]] = []

        async def _astream(msgs, **kwargs):
            captured_prepared.append(list(msgs))
            yield AIMessage(content="ok")

        llm = AsyncMock()
        llm.astream = _astream

        config = MagicMock()
        config.llm.timeout = 600
        config.llm.model = "test-model"
        config.context_management.max_summary_length = 10000
        # Real QueryConfig — a bare MagicMock fabricates a truthy
        # memory.query.digest and routes the turn into the digest branch.
        config.memory.query = QueryConfig()

        service = RecordingManager(payload=make_payload())
        recall_spy = AsyncMock()
        kb_spy = AsyncMock()
        messages = [SystemMessage(content="sys"), HumanMessage(content="user query")]

        result = await _execute_turn(
            llm_with_tools=llm,
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=_persistent_callbacks(),
            llm_timeout=600,
            auxiliary_llm=None,
            config=config,
            recall_store=recall_spy,
            knowledge_store=kb_spy,
            project_id=str(PROJECT_ID),
            memory_service=service,
        )
        assert result.interrupted is False

        # -- assemble request: latest HumanMessage, main model threaded
        assert len(service.assemble_requests) == 1
        req = service.assemble_requests[0]
        assert req.query_text == "user query"
        assert req.model == "test-model"

        # -- payload inserted immediately after the first HumanMessage, so the
        # synthetic function-call pairs always follow a real user turn (Gemini's
        # native API 400s on a leading functionCall — see _injection_anchor_index).
        prepared = captured_prepared[0]
        payload_msgs = service.payload.messages()
        assert isinstance(prepared[0], SystemMessage)
        assert prepared[1].content == "user query"
        assert prepared[2 : 2 + len(payload_msgs)] == payload_msgs

        # -- legacy direct-store blocks skipped
        recall_spy.decrement_ttl.assert_not_called()
        recall_spy.retrieve.assert_not_called()
        kb_spy.hybrid_search.assert_not_called()

    @pytest.mark.asyncio
    async def test_digest_flag_switches_query_formation(self):
        from src.persistent_graph import _execute_turn

        async def _astream(msgs, **kwargs):
            yield AIMessage(content="ok")

        llm = AsyncMock()
        llm.astream = _astream

        config = MagicMock()
        config.llm.timeout = 600
        config.llm.model = "test-model"
        config.context_management.max_summary_length = 10000
        config.memory.query = QueryConfig(digest=True, digest_window=4)

        service = RecordingManager(payload=make_payload())
        messages = [
            SystemMessage(content="sys"),
            HumanMessage(content="first ask"),
            AIMessage(content="did things"),
            HumanMessage(content="follow-up"),
        ]

        await _execute_turn(
            llm_with_tools=llm,
            tool_map={},
            context_manager=AsyncMock(
                ensure_within_limits=AsyncMock(side_effect=lambda m, *a, **kw: m)
            ),
            messages=messages,
            callbacks=_persistent_callbacks(),
            llm_timeout=600,
            auxiliary_llm=None,
            config=config,
            recall_store=AsyncMock(),
            knowledge_store=AsyncMock(),
            project_id=str(PROJECT_ID),
            memory_service=service,
        )

        # Digest = windowed conversation, not the bare last user message
        # (which is what the legacy builder would have produced).
        req = service.assemble_requests[0]
        assert req.query_text == "first ask\ndid things\nfollow-up"


# ---------------------------------------------------------------------------
# 6. Persistent teardown + B11 terminate wiring
# ---------------------------------------------------------------------------


def _archive_session(service: RecordingManager) -> MagicMock:
    session = MagicMock()
    session.memory_service = service
    session.final_memory_extracted = False
    session.messages = [HumanMessage(content="hi")]
    session.tool_context.recall_store = AsyncMock()
    session.auxiliary_llm = None
    session.postgres_conn = None
    session.workspace_sync = None
    return session


class TestTeardownWiring:
    @pytest.mark.asyncio
    async def test_archive_captures_session_end(self):
        from src.api.persistent_app import _handle_archive

        service = RecordingManager()
        session = _archive_session(service)
        ws = AsyncMock()

        with (
            patch("src.api.persistent_app._session", session),
            patch("src.api.persistent_app._thread_id", "tid"),
        ):
            await _handle_archive(ws)

        assert [e.kind for e in service.captures] == ["session_end"]
        assert service.captures[0].messages is session.messages
        assert session.final_memory_extracted is True
        ended = [
            c[0][0]
            for c in ws.send_json.call_args_list
            if c[0][0].get("method") == "session.ended"
        ]
        assert len(ended) == 1

    @pytest.mark.asyncio
    async def test_idle_archive_captures_idle_kind(self):
        from src.api.persistent_app import _handle_idle_archive

        service = RecordingManager()
        session = _archive_session(service)
        session.workspace_manager = None

        with (
            patch("src.api.persistent_app._session", session),
            patch("src.api.persistent_app._thread_id", "tid"),
            patch("src.api.persistent_app._update_thread_status", new=AsyncMock()),
        ):
            await _handle_idle_archive(None)

        assert [e.kind for e in service.captures] == ["idle_archive"]
        assert session.final_memory_extracted is True

    @pytest.mark.asyncio
    async def test_terminate_captures_session_end_once(self):
        """B11: detach-style endings now capture — exactly once."""
        from src.api import persistent_app

        service = RecordingManager()
        session = MagicMock()
        session.memory_service = service
        session.final_memory_extracted = False
        session.messages = [HumanMessage(content="hi")]
        session.workspace_sync = None
        session.workspace_manager = None
        session.cleanup = AsyncMock()
        persistent_app._session = session
        persistent_app._thread_id = "tid-b11-1"
        persistent_app._terminating = False
        persistent_app._loop_task = None
        persistent_app._max_sessions_per_process = 0

        with (
            patch.object(persistent_app, "_update_thread_status", new=AsyncMock()),
            patch.object(persistent_app, "_stop_watchdogs"),
        ):
            await persistent_app._terminate_session("rest_detach")

        assert [e.kind for e in service.captures] == ["session_end"]
        assert persistent_app._session is None

    @pytest.mark.asyncio
    async def test_terminate_skips_when_archive_already_captured(self):
        """B11 guard: archive → terminate must not double-extract."""
        from src.api import persistent_app
        from src.api.persistent_app import _handle_archive

        service = RecordingManager()
        session = _archive_session(service)
        session.workspace_manager = None
        session.cleanup = AsyncMock()
        ws = AsyncMock()

        persistent_app._session = session
        persistent_app._thread_id = "tid-b11-2"
        persistent_app._terminating = False
        persistent_app._loop_task = None
        persistent_app._max_sessions_per_process = 0

        with (
            patch.object(persistent_app, "_update_thread_status", new=AsyncMock()),
            patch.object(persistent_app, "_stop_watchdogs"),
        ):
            # _handle_archive reads the patched-in module globals directly
            with patch("src.api.persistent_app._thread_id", "tid-b11-2"):
                await _handle_archive(ws)
            await persistent_app._terminate_session("loop_complete")

        # One capture total: archive's. Terminate honoured the guard flag.
        assert [e.kind for e in service.captures] == ["session_end"]
        assert persistent_app._session is None


# ---------------------------------------------------------------------------
# 7. B10 — strip recognition for everything the manager injects
# ---------------------------------------------------------------------------


class _ExoticRetriever:
    """A future-shaped retriever with an unrenderable kind."""

    async def retrieve(self, req):
        return [Candidate(kind="graph_paths", text="A→B", record=object())]


class TestStripRecognitionB10:
    @pytest.mark.asyncio
    async def test_all_injected_messages_are_strip_recognized(self):
        """Every message assemble() emits must be recognized by the
        summarization-exclusion predicate — compaction strips injected
        pairs and re-injects fresh ones, so an unrecognized injection
        would survive into summaries and duplicate forever."""
        runtime = MemoryRuntime(
            recall_store=make_recall_mock(make_memories()),
            knowledge_store=make_kb_mock(make_notes()),
            project_id=str(PROJECT_ID),
        )
        manager = MemoryManager(
            runtime,
            retrievers=[
                ("recall_two_tier", _make_recall_retriever(runtime)),
                ("kb_notes", _make_kb_retriever(runtime)),
            ],
        )
        from src.services.memory import AssembleRequest

        payload = await manager.assemble(
            AssembleRequest(query_text="anything", model=None)
        )

        msgs = payload.messages()
        assert len(msgs) == 4  # memory pair + knowledge pair
        for msg in msgs:
            assert is_workspace_injection_message(msg), (
                f"Injection message not strip-recognized: {msg!r}"
            )

    @pytest.mark.asyncio
    async def test_unrenderable_kinds_contribute_no_messages(self):
        """An exotic candidate kind yields a provenance-only block — the
        payload still contains only strip-recognized messages."""
        manager = MemoryManager(
            MemoryRuntime(), retrievers=[("exotic", _ExoticRetriever())]
        )
        from src.services.memory import AssembleRequest

        payload = await manager.assemble(AssembleRequest(query_text="q"))
        assert payload.stats.candidates_total == 1
        assert payload.messages() == []


def _make_recall_retriever(runtime):
    from src.services.memory.plugins.legacy import RecallTwoTierRetriever

    return RecallTwoTierRetriever(runtime.recall_store)


def _make_kb_retriever(runtime):
    from src.services.memory.plugins.legacy import KbNotesRetriever

    return KbNotesRetriever(runtime.knowledge_store, project_id=runtime.project_id)
