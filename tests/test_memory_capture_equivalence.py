"""Phase-1 slice-4 equivalence: capture writers ≡ legacy write-path call sites.

Each legacy extraction/curation call site is reproduced verbatim below
(gates + kwarg shapes + state-advance semantics) and run against the same
spied `extract_and_store_memories` / `assemble_memories` /
`recall_store.store` as the writer driven by CaptureEvents — the await
lists must be identical. Gates use the REAL `_should_extract_memories` /
`_should_assemble_memories` and REAL loader configs (the B1 lesson:
MagicMock-fabricated config attrs are how the persistent extraction bug
hid for months).

Site map and preserved per-mode asymmetries: see the module docstring of
src/services/memory/plugins/legacy_writers.py. The legacy call sites use
asyncio.create_task; the reproductions await directly — argument equality
is unaffected (args are computed at trigger time either way), and the
writers are awaited through capture() by design.
"""

from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import src.services.auxiliary as auxiliary_module
from src.core.loader import (
    AuxiliaryConfig,
    AuxiliaryTaskConfig,
    MemoryConfig,
    MemoryPipelineConfig,
)
from src.services.auxiliary import (
    _should_assemble_memories,
    _should_extract_memories,
)
from src.services.memory import (
    CaptureEvent,
    MemoryManager,
    MemoryRuntime,
    available_memory_plugins,
)
from src.services.memory.plugins.legacy_writers import (
    CompactionMemoryWriter,
    MemoryAssembler,
    PersistentIntervalExtractor,
    PhaseBoundaryExtractor,
    QueuedMemoryWriter,
    TeardownExtractor,
    WorkerIntervalExtractor,
)

EXTRACTION_PROMPT = "EXTRACTION PROMPT"
ASSEMBLER_PROMPT = "ASSEMBLER PROMPT"


@pytest.fixture
def extract_spy(monkeypatch):
    spy = AsyncMock(return_value=3)
    monkeypatch.setattr(auxiliary_module, "extract_and_store_memories", spy)
    return spy


@pytest.fixture
def assemble_spy(monkeypatch):
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(auxiliary_module, "assemble_memories", spy)
    return spy


def make_aux_config(enabled=True, extract=True, assemble=True):
    return AuxiliaryConfig(
        enabled=enabled,
        tasks={
            "extract_memories": AuxiliaryTaskConfig(enabled=extract),
            "assemble_memories": AuxiliaryTaskConfig(enabled=assemble),
            "curate_knowledge": AuxiliaryTaskConfig(enabled=True),
        },
    )


def make_runtime(
    recall_store="store",
    auxiliary_llm="aux-llm",
    observer_interval=5,
    assembler_interval=7,
    aux_enabled=True,
    extract_enabled=True,
    assemble_enabled=True,
    extraction_prompt=EXTRACTION_PROMPT,
    assembler_prompt=ASSEMBLER_PROMPT,
):
    return MemoryRuntime(
        recall_store=recall_store,
        auxiliary_llm=auxiliary_llm,
        memory_config=MemoryConfig(
            enabled=True,
            observer_interval=observer_interval,
            assembler_interval=assembler_interval,
        ),
        auxiliary_config=make_aux_config(
            enabled=aux_enabled, extract=extract_enabled, assemble=assemble_enabled
        ),
        extraction_prompt=extraction_prompt,
        assembler_prompt=assembler_prompt,
    )


def make_messages():
    return [HumanMessage(content="do the thing"), AIMessage(content="done")]


def turn_event(turn, messages, phase=2, injection_text=""):
    return CaptureEvent(
        kind="turn_end",
        messages=messages,
        phase=phase,
        turn_count=turn,
        extra={"current_injection_text": injection_text},
    )


# ---------------------------------------------------------------------------
# Legacy reproductions (verbatim gates + kwarg shapes)
# ---------------------------------------------------------------------------


async def run_legacy_worker_turn(
    state,
    *,
    recall_store,
    auxiliary_llm,
    config_auxiliary,
    memory_config,
    messages,
    phase,
    memory_block_text="",
):
    """graph.py:1528-1606 — one execute-node turn's extraction + assembly."""
    new_turn_count = state.get("turn_count", 0) + 1
    extraction_triggered = False
    assembly_triggered = False

    if (
        recall_store
        and config_auxiliary.enabled
        and config_auxiliary.tasks.get("extract_memories", None)
        and config_auxiliary.tasks["extract_memories"].enabled
    ):
        last_observed = state.get("last_observed_turn", 0)
        if _should_extract_memories(
            new_turn_count, memory_config.observer_interval, last_observed
        ):
            extraction_triggered = True
            await auxiliary_module.extract_and_store_memories(
                auxiliary_llm=auxiliary_llm,
                recall_store=recall_store,
                messages=messages,
                memory_extraction_prompt=EXTRACTION_PROMPT,
                phase=phase,
                source_turn_start=last_observed,
                source_turn_end=new_turn_count,
            )

    if (
        recall_store
        and config_auxiliary.enabled
        and config_auxiliary.tasks.get("assemble_memories", None)
        and config_auxiliary.tasks["assemble_memories"].enabled
        and ASSEMBLER_PROMPT
    ):
        last_assembled = state.get("last_assembled_turn", 0)
        if _should_assemble_memories(
            new_turn_count, memory_config.assembler_interval, last_assembled
        ):
            assembly_triggered = True
            await auxiliary_module.assemble_memories(
                auxiliary_llm=auxiliary_llm,
                recall_store=recall_store,
                messages=messages,
                current_injection_text=memory_block_text,
                memory_assembler_prompt=ASSEMBLER_PROMPT,
            )

    state["turn_count"] = new_turn_count
    if extraction_triggered:
        state["last_observed_turn"] = new_turn_count
    if assembly_triggered:
        state["last_assembled_turn"] = new_turn_count


async def run_legacy_persistent_turn(
    loop_state, *, recall_store, auxiliary_llm, extraction_interval, messages
):
    """persistent_graph.py:420-443 — one loop turn's extraction check."""
    turn_count = loop_state["turn_count"] + 1
    loop_state["turn_count"] = turn_count

    if (
        recall_store
        and auxiliary_llm
        and extraction_interval > 0
        and (turn_count - loop_state["last_extraction_turn"]) >= extraction_interval
    ):
        loop_state["last_extraction_turn"] = turn_count
        await auxiliary_module.extract_and_store_memories(
            auxiliary_llm=auxiliary_llm,
            recall_store=recall_store,
            messages=messages,
            memory_extraction_prompt=EXTRACTION_PROMPT,
            source_turn_start=turn_count - extraction_interval,
            source_turn_end=turn_count,
        )


async def run_legacy_phase_boundary(
    *, recall_store, auxiliary_llm, config_auxiliary, messages, phase
):
    """graph.py:2037-2054 — archive_phase extraction."""
    if (
        recall_store
        and config_auxiliary.enabled
        and config_auxiliary.tasks.get("extract_memories", None)
        and config_auxiliary.tasks["extract_memories"].enabled
    ):
        await auxiliary_module.extract_and_store_memories(
            auxiliary_llm=auxiliary_llm,
            recall_store=recall_store,
            messages=messages,
            memory_extraction_prompt=EXTRACTION_PROMPT,
            phase=phase,
        )


async def run_legacy_teardown(*, recall_store, auxiliary_llm, messages):
    """persistent_app.py _handle_archive / _handle_idle_archive extraction."""
    if recall_store and auxiliary_llm and messages:
        try:
            await auxiliary_module.extract_and_store_memories(
                auxiliary_llm=auxiliary_llm,
                recall_store=recall_store,
                messages=messages,
                memory_extraction_prompt=EXTRACTION_PROMPT,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Worker in-loop parity
# ---------------------------------------------------------------------------


class TestWorkerIntervalParity:
    @pytest.mark.asyncio
    async def test_call_sequence_matches_legacy_over_twelve_turns(
        self, extract_spy, assemble_spy
    ):
        messages = make_messages()
        runtime = make_runtime()

        state = {"turn_count": 0}
        for _ in range(12):
            await run_legacy_worker_turn(
                state,
                recall_store=runtime.recall_store,
                auxiliary_llm=runtime.auxiliary_llm,
                config_auxiliary=runtime.auxiliary_config,
                memory_config=runtime.memory_config,
                messages=messages,
                phase=2,
            )
        legacy_calls = list(extract_spy.await_args_list)
        extract_spy.reset_mock()

        writer = WorkerIntervalExtractor(make_runtime())
        for turn in range(1, 13):
            await writer.on_event(turn_event(turn, messages))
        assert extract_spy.await_args_list == legacy_calls

        # and the sequence itself is what the legacy semantics promise:
        # modulo fires at 5 and 10 with gap windows
        assert len(legacy_calls) == 2
        assert legacy_calls[0].kwargs["source_turn_start"] == 0
        assert legacy_calls[0].kwargs["source_turn_end"] == 5
        assert legacy_calls[1].kwargs["source_turn_start"] == 5
        assert legacy_calls[1].kwargs["source_turn_end"] == 10
        assert legacy_calls[0].kwargs["phase"] == 2
        assert legacy_calls[0].kwargs["memory_extraction_prompt"] == EXTRACTION_PROMPT

    @pytest.mark.asyncio
    async def test_no_calls_when_task_disabled(self, extract_spy):
        writer = WorkerIntervalExtractor(make_runtime(extract_enabled=False))
        for turn in range(1, 11):
            await writer.on_event(turn_event(turn, make_messages()))
        extract_spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_calls_when_auxiliary_disabled(self, extract_spy):
        writer = WorkerIntervalExtractor(make_runtime(aux_enabled=False))
        await writer.on_event(turn_event(5, make_messages()))
        extract_spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_calls_without_store(self, extract_spy):
        writer = WorkerIntervalExtractor(make_runtime(recall_store=None))
        await writer.on_event(turn_event(5, make_messages()))
        extract_spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_state_advances_at_trigger_even_if_extraction_fails(
        self, extract_spy
    ):
        extract_spy.side_effect = RuntimeError("aux down")
        writer = WorkerIntervalExtractor(make_runtime())
        with pytest.raises(RuntimeError):
            await writer.on_event(turn_event(5, make_messages()))
        # B1 follow-up semantics: the trigger still advanced
        assert writer._last_observed_turn == 5


# ---------------------------------------------------------------------------
# Persistent in-loop parity
# ---------------------------------------------------------------------------


class TestPersistentIntervalParity:
    @pytest.mark.asyncio
    async def test_call_sequence_matches_legacy_over_twelve_turns(self, extract_spy):
        messages = make_messages()
        loop_state = {"turn_count": 0, "last_extraction_turn": 0}
        for _ in range(12):
            await run_legacy_persistent_turn(
                loop_state,
                recall_store="store",
                auxiliary_llm="aux-llm",
                extraction_interval=5,
                messages=messages,
            )
        legacy_calls = list(extract_spy.await_args_list)
        extract_spy.reset_mock()

        writer = PersistentIntervalExtractor(make_runtime())
        for turn in range(1, 13):
            await writer.on_event(turn_event(turn, messages))
        assert extract_spy.await_args_list == legacy_calls

        # elapsed gate fires at 5 and 10 with fixed-width windows
        assert len(legacy_calls) == 2
        assert legacy_calls[0].kwargs["source_turn_start"] == 0
        assert legacy_calls[0].kwargs["source_turn_end"] == 5
        assert legacy_calls[1].kwargs["source_turn_start"] == 5
        assert legacy_calls[1].kwargs["source_turn_end"] == 10
        # the legacy persistent call passes NO phase kwarg
        assert "phase" not in legacy_calls[0].kwargs

    @pytest.mark.asyncio
    async def test_no_calls_without_aux_llm_or_interval(self, extract_spy):
        writer = PersistentIntervalExtractor(make_runtime(auxiliary_llm=None))
        await writer.on_event(turn_event(5, make_messages()))
        writer_zero = PersistentIntervalExtractor(make_runtime(observer_interval=0))
        await writer_zero.on_event(turn_event(5, make_messages()))
        extract_spy.assert_not_awaited()


# ---------------------------------------------------------------------------
# Phase boundary parity
# ---------------------------------------------------------------------------


class TestPhaseBoundaryParity:
    @pytest.mark.asyncio
    async def test_call_matches_legacy(self, extract_spy):
        messages = make_messages()
        runtime = make_runtime()
        await run_legacy_phase_boundary(
            recall_store=runtime.recall_store,
            auxiliary_llm=runtime.auxiliary_llm,
            config_auxiliary=runtime.auxiliary_config,
            messages=messages,
            phase=4,
        )
        legacy_calls = list(extract_spy.await_args_list)
        extract_spy.reset_mock()

        writer = PhaseBoundaryExtractor(runtime)
        await writer.on_event(
            CaptureEvent(kind="phase_boundary", messages=messages, phase=4)
        )
        assert extract_spy.await_args_list == legacy_calls
        # no turn window on the boundary call — kwarg-shape pin
        kwargs = legacy_calls[0].kwargs
        assert kwargs["phase"] == 4
        assert "source_turn_start" not in kwargs
        assert "source_turn_end" not in kwargs

    @pytest.mark.asyncio
    async def test_disabled_task_no_call(self, extract_spy):
        writer = PhaseBoundaryExtractor(make_runtime(extract_enabled=False))
        await writer.on_event(CaptureEvent(kind="phase_boundary", phase=4))
        extract_spy.assert_not_awaited()


# ---------------------------------------------------------------------------
# Teardown parity (session_end / idle_archive)
# ---------------------------------------------------------------------------


class TestTeardownParity:
    @pytest.mark.asyncio
    async def test_session_end_matches_legacy(self, extract_spy, caplog):
        messages = make_messages()
        await run_legacy_teardown(
            recall_store="store", auxiliary_llm="aux-llm", messages=messages
        )
        legacy_calls = list(extract_spy.await_args_list)
        extract_spy.reset_mock()

        writer = TeardownExtractor(make_runtime())
        with caplog.at_level("INFO"):
            await writer.on_event(CaptureEvent(kind="session_end", messages=messages))
        assert extract_spy.await_args_list == legacy_calls
        # exactly the four legacy kwargs — no phase, no window
        assert sorted(legacy_calls[0].kwargs) == [
            "auxiliary_llm",
            "memory_extraction_prompt",
            "messages",
            "recall_store",
        ]
        # legacy log text preserved (the k3d B1 verification signal)
        assert "Final memory extraction complete" in caplog.text

    @pytest.mark.asyncio
    async def test_idle_archive_fires_with_legacy_log(self, extract_spy, caplog):
        writer = TeardownExtractor(make_runtime())
        with caplog.at_level("INFO"):
            await writer.on_event(
                CaptureEvent(kind="idle_archive", messages=make_messages())
            )
        extract_spy.assert_awaited_once()
        assert "Idle archive: memory extraction complete" in caplog.text

    @pytest.mark.asyncio
    async def test_empty_messages_or_missing_deps_skip(self, extract_spy):
        writer = TeardownExtractor(make_runtime())
        await writer.on_event(CaptureEvent(kind="session_end", messages=[]))
        writer_no_aux = TeardownExtractor(make_runtime(auxiliary_llm=None))
        await writer_no_aux.on_event(
            CaptureEvent(kind="session_end", messages=make_messages())
        )
        extract_spy.assert_not_awaited()


# ---------------------------------------------------------------------------
# Assembler parity (combined worker turn loop)
# ---------------------------------------------------------------------------


class TestAssemblerParity:
    @pytest.mark.asyncio
    async def test_combined_worker_loop_matches_legacy(self, extract_spy, assemble_spy):
        messages = make_messages()
        runtime = make_runtime()

        state = {"turn_count": 0}
        for turn in range(1, 15):
            await run_legacy_worker_turn(
                state,
                recall_store=runtime.recall_store,
                auxiliary_llm=runtime.auxiliary_llm,
                config_auxiliary=runtime.auxiliary_config,
                memory_config=runtime.memory_config,
                messages=messages,
                phase=2,
                memory_block_text=f"block@{turn}",
            )
        legacy_extracts = list(extract_spy.await_args_list)
        legacy_assembles = list(assemble_spy.await_args_list)
        extract_spy.reset_mock()
        assemble_spy.reset_mock()

        shared_runtime = make_runtime()
        extractor = WorkerIntervalExtractor(shared_runtime)
        assembler = MemoryAssembler(shared_runtime)
        for turn in range(1, 15):
            event = turn_event(turn, messages, injection_text=f"block@{turn}")
            await extractor.on_event(event)
            await assembler.on_event(event)

        assert extract_spy.await_args_list == legacy_extracts
        assert assemble_spy.await_args_list == legacy_assembles

        # assembler fires at 7 and 14 with the per-turn injection text
        assert len(legacy_assembles) == 2
        assert legacy_assembles[0].kwargs["current_injection_text"] == "block@7"
        assert legacy_assembles[1].kwargs["current_injection_text"] == "block@14"
        assert legacy_assembles[0].kwargs["memory_assembler_prompt"] == ASSEMBLER_PROMPT

    @pytest.mark.asyncio
    async def test_no_prompt_or_disabled_task_never_fires(self, assemble_spy):
        writer = MemoryAssembler(make_runtime(assembler_prompt=""))
        await writer.on_event(turn_event(7, make_messages()))
        writer_disabled = MemoryAssembler(make_runtime(assemble_enabled=False))
        await writer_disabled.on_event(turn_event(7, make_messages()))
        assemble_spy.assert_not_awaited()


# ---------------------------------------------------------------------------
# Compaction + queued-memory writers (direct store sites)
# ---------------------------------------------------------------------------


class TestCompactionWriter:
    @pytest.mark.asyncio
    async def test_stores_truncated_summary_with_legacy_kwargs(self):
        store = AsyncMock()
        runtime = make_runtime(recall_store=store)
        writer = CompactionMemoryWriter(runtime)
        summary = "x" * 2500
        await writer.on_event(
            CaptureEvent(kind="compaction", phase=3, extra={"summary": summary})
        )
        store.store.assert_awaited_once_with(
            content=summary[:2000],
            summary="Context compaction summary",
            importance=0.6,
            source="compaction",
            memory_type="factual",
            source_phase=3,
        )

    @pytest.mark.asyncio
    async def test_no_summary_no_store(self):
        store = AsyncMock()
        writer = CompactionMemoryWriter(make_runtime(recall_store=store))
        await writer.on_event(CaptureEvent(kind="compaction", phase=3))
        store.store.assert_not_awaited()


class TestQueuedMemoryWriter:
    @pytest.mark.asyncio
    async def test_stores_each_queued_memory(self):
        store = AsyncMock()
        writer = QueuedMemoryWriter(make_runtime(recall_store=store))
        mems = [
            {
                "content": "Completed: fix the bug\nOutcome: patched",
                "importance": 0.7,
                "source": "todo",
                "memory_type": "procedural",
                "source_phase": 2,
            },
            {"content": "another", "source": "todo"},
        ]
        await writer.on_event(
            CaptureEvent(kind="todo_complete", extra={"queued_memories": mems})
        )
        assert store.store.await_count == 2
        assert store.store.await_args_list[0].kwargs == mems[0]
        assert store.store.await_args_list[1].kwargs == mems[1]

    @pytest.mark.asyncio
    async def test_per_item_containment_matches_legacy_drain(self):
        store = AsyncMock()
        store.store.side_effect = [RuntimeError("dup"), None]
        writer = QueuedMemoryWriter(make_runtime(recall_store=store))
        await writer.on_event(
            CaptureEvent(
                kind="todo_complete",
                extra={"queued_memories": [{"content": "a"}, {"content": "b"}]},
            )
        )
        # first failure contained, second still stored
        assert store.store.await_count == 2


# ---------------------------------------------------------------------------
# Registration + manager dispatch integration
# ---------------------------------------------------------------------------


class TestRegistrationAndDispatch:
    def test_all_legacy_writers_registered(self):
        writers = available_memory_plugins("writer")["writer"]
        for name in (
            "interval_extractor",
            "persistent_interval_extractor",
            "phase_boundary_extractor",
            "teardown_extractor",
            "memory_assembler",
            "compaction_memory",
            "queued_memory",
        ):
            assert name in writers

    @pytest.mark.asyncio
    async def test_manager_capture_routes_by_kind(self, extract_spy, assemble_spy):
        cfg = MemoryConfig(
            enabled=True,
            observer_interval=5,
            assembler_interval=7,
            pipeline=MemoryPipelineConfig(
                writers=["interval_extractor", "memory_assembler"]
            ),
        )
        runtime = make_runtime()
        runtime.memory_config = cfg
        manager = MemoryManager.from_config(cfg, runtime)

        await manager.capture(turn_event(5, make_messages()))
        extract_spy.assert_awaited_once()
        assemble_spy.assert_not_awaited()

        # phase_boundary reaches neither of these two writers
        extract_spy.reset_mock()
        await manager.capture(
            CaptureEvent(kind="phase_boundary", messages=make_messages(), phase=1)
        )
        extract_spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_writer_failure_contained_by_manager(self, extract_spy):
        extract_spy.side_effect = RuntimeError("aux exploded")
        cfg = MemoryConfig(
            enabled=True,
            observer_interval=5,
            pipeline=MemoryPipelineConfig(writers=["interval_extractor"]),
        )
        runtime = make_runtime()
        runtime.memory_config = cfg
        manager = MemoryManager.from_config(cfg, runtime)
        # must not raise
        await manager.capture(turn_event(5, make_messages()))
