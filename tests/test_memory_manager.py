"""Tests for the MemoryManager kernel (memory overhaul Phase 1, slice 1).

Covers the seam's kernel only: registry, binder (from_config), assemble
orchestration + containment + stats, capture dispatch, extensions, and
the MemoryConfig plumbing. The transplanted current-behaviour plugins
get their own equivalence-fixture suites in the follow-up slices.
"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import src.services.memory.registry as registry_module
from src.core.loader import MemoryConfig, MemoryPipelineConfig, _parse_memory_config
from src.services.knowledge_store import KnowledgeRecord
from src.services.recall_store import MemoryRecord
from src.services.memory import (
    AssembleRequest,
    AssembleStats,
    Candidate,
    CaptureEvent,
    InjectionBlock,
    MemoryManager,
    MemoryPayload,
    MemoryRuntime,
    Scored,
    UnknownMemoryPluginError,
    available_memory_plugins,
    register_memory_plugin,
)


@pytest.fixture(autouse=True)
def clean_registry():
    """Snapshot/restore the module-level registry around every test."""
    snapshot = {
        kind: dict(specs)
        for kind, specs in registry_module.MEMORY_PLUGIN_REGISTRY.items()
    }
    yield
    for kind, specs in registry_module.MEMORY_PLUGIN_REGISTRY.items():
        specs.clear()
        specs.update(snapshot[kind])


# ---------------------------------------------------------------------------
# Test plugins
# ---------------------------------------------------------------------------


class FakeRetriever:
    def __init__(self, candidates=None, error=None, log=None, label=""):
        self.candidates = candidates or []
        self.error = error
        self.log = log
        self.label = label

    async def retrieve(self, req):
        if self.log is not None:
            self.log.append(f"retrieve:{self.label}")
        if self.error:
            raise self.error
        return list(self.candidates)


class FakeScorer:
    def __init__(self, score=1.0, error=None, log=None, label=""):
        self.score_value = score
        self.error = error
        self.log = log
        self.label = label

    async def score(self, req, items):
        if self.log is not None:
            self.log.append(f"score:{self.label}")
        if self.error:
            raise self.error
        return [Scored(candidate=s.candidate, score=self.score_value) for s in items]


class FakePolicy:
    def __init__(self, keep=None, error=None, log=None, label=""):
        self.keep = keep  # None = pass through, int = head-truncate
        self.error = error
        self.log = log
        self.label = label

    async def apply(self, req, items):
        if self.log is not None:
            self.log.append(f"apply:{self.label}")
        if self.error:
            raise self.error
        return items if self.keep is None else items[: self.keep]


class FakeWriter:
    def __init__(self, kinds=("turn_end",), error=None):
        self.event_kinds = frozenset(kinds)
        self.error = error
        self.events = []

    async def on_event(self, event):
        if self.error:
            raise self.error
        self.events.append(event)


class FakeExtension:
    def __init__(self, tools=None, error=None):
        self._tools = tools or []
        self.error = error

    def tools(self):
        if self.error:
            raise self.error
        return list(self._tools)


def make_candidate(kind="memory", text="fact", tokens=10, record_id="r1"):
    """Candidate with a real store record so the real renderers work."""
    if kind == "memory":
        record = MemoryRecord(id=record_id, content=text, token_count=tokens)
    elif kind == "knowledge":
        record = KnowledgeRecord(id=record_id, note_id=str(record_id), content=text)
    else:
        record = SimpleNamespace(id=record_id)
    return Candidate(kind=kind, text=text, token_count=tokens, record=record)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_and_resolve(self):
        @register_memory_plugin("retriever", "test_dense", description="probe")
        def _build(runtime):
            return FakeRetriever()

        spec = registry_module.resolve_memory_plugin("retriever", "test_dense")
        assert spec.name == "test_dense"
        assert spec.kind == "retriever"
        assert spec.description == "probe"
        assert spec.factory is _build

    def test_duplicate_registration_raises(self):
        @register_memory_plugin("scorer", "test_dup")
        def _build_a(runtime):
            return FakeScorer()

        with pytest.raises(ValueError, match="already registered"):

            @register_memory_plugin("scorer", "test_dup")
            def _build_b(runtime):
                return FakeScorer()

    def test_same_factory_reregistration_is_idempotent(self):
        def _build(runtime):
            return FakeScorer()

        register_memory_plugin("scorer", "test_idem")(_build)
        # Module re-import re-runs decorators with the same function object.
        register_memory_plugin("scorer", "test_idem")(_build)

    def test_unknown_name_lists_available(self):
        @register_memory_plugin("policy", "test_budget")
        def _build(runtime):
            return FakePolicy()

        with pytest.raises(UnknownMemoryPluginError) as exc_info:
            registry_module.resolve_memory_plugin("policy", "nonexistent")
        assert "test_budget" in str(exc_info.value)
        assert "nonexistent" in str(exc_info.value)

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError, match="kind"):
            register_memory_plugin("frobnicator", "x")
        with pytest.raises(ValueError, match="kind"):
            registry_module.resolve_memory_plugin("frobnicator", "x")
        with pytest.raises(ValueError, match="kind"):
            available_memory_plugins("frobnicator")

    def test_available_filters_by_kind(self):
        @register_memory_plugin("writer", "test_writer")
        def _build(runtime):
            return FakeWriter()

        only_writers = available_memory_plugins("writer")
        assert list(only_writers.keys()) == ["writer"]
        assert "test_writer" in only_writers["writer"]
        everything = available_memory_plugins()
        assert set(everything.keys()) == set(registry_module.PLUGIN_KINDS)


# ---------------------------------------------------------------------------
# Binder (from_config)
# ---------------------------------------------------------------------------


class TestFromConfig:
    def test_binds_named_plugins_with_runtime(self):
        seen_runtimes = []

        @register_memory_plugin("retriever", "test_recall")
        def _build_retriever(runtime):
            seen_runtimes.append(runtime)
            return FakeRetriever()

        @register_memory_plugin("writer", "test_extractor")
        def _build_writer(runtime):
            seen_runtimes.append(runtime)
            return FakeWriter()

        cfg = MemoryConfig(
            pipeline=MemoryPipelineConfig(
                retrievers=["test_recall"], writers=["test_extractor"]
            )
        )
        runtime = MemoryRuntime(recall_store=object())
        manager = MemoryManager.from_config(cfg, runtime)

        assert manager.pipeline_summary() == {
            "retrievers": ["test_recall"],
            "scorers": [],
            "policies": [],
            "writers": ["test_extractor"],
            "extensions": [],
        }
        assert seen_runtimes == [runtime, runtime]

    def test_unknown_pipeline_name_fails_at_bind_time(self):
        cfg = MemoryConfig(pipeline=MemoryPipelineConfig(retrievers=["missing"]))
        with pytest.raises(UnknownMemoryPluginError, match="missing"):
            MemoryManager.from_config(cfg, MemoryRuntime())

    @pytest.mark.asyncio
    async def test_empty_pipeline_binds_noop_manager(self):
        manager = MemoryManager.from_config(MemoryConfig(), MemoryRuntime())
        payload = await manager.assemble(AssembleRequest(query_text="anything"))
        assert payload.blocks == []
        assert payload.stats.errors == []
        assert payload.messages() == []
        # capture is a no-op too — must not raise
        await manager.capture(CaptureEvent(kind="turn_end"))
        assert manager.extension_tools() == []

    def test_runtime_memory_config_backfilled(self):
        cfg = MemoryConfig()
        runtime = MemoryRuntime()
        MemoryManager.from_config(cfg, runtime)
        assert runtime.memory_config is cfg

    def test_runtime_memory_config_not_overwritten(self):
        preset = MemoryConfig(budget_tokens=42)
        runtime = MemoryRuntime(memory_config=preset)
        MemoryManager.from_config(MemoryConfig(), runtime)
        assert runtime.memory_config is preset


# ---------------------------------------------------------------------------
# Assemble orchestration
# ---------------------------------------------------------------------------


class TestAssemble:
    @pytest.mark.asyncio
    async def test_stage_order_retrievers_scorers_policies(self):
        log = []
        manager = MemoryManager(
            MemoryRuntime(),
            retrievers=[
                ("r1", FakeRetriever([make_candidate()], log=log, label="r1")),
                ("r2", FakeRetriever([make_candidate()], log=log, label="r2")),
            ],
            scorers=[("s1", FakeScorer(log=log, label="s1"))],
            policies=[("p1", FakePolicy(log=log, label="p1"))],
        )
        await manager.assemble(AssembleRequest(query_text="q"))
        assert log == ["retrieve:r1", "retrieve:r2", "score:s1", "apply:p1"]

    @pytest.mark.asyncio
    async def test_blocks_grouped_by_kind_with_provenance(self):
        mem = make_candidate(kind="memory", tokens=10, record_id="m1")
        kb1 = make_candidate(kind="knowledge", tokens=7, record_id="k1")
        kb2 = make_candidate(kind="knowledge", tokens=5, record_id="k2")
        manager = MemoryManager(
            MemoryRuntime(),
            retrievers=[
                ("recall", FakeRetriever([mem])),
                ("kb", FakeRetriever([kb1, kb2])),
            ],
        )
        payload = await manager.assemble(AssembleRequest(query_text="q"))

        assert payload.stats.candidates_total == 3
        assert payload.stats.per_retriever == {"recall": 1, "kb": 2}
        assert payload.stats.blocks == 2
        assert payload.stats.injected_total == 3
        assert payload.stats.tokens_injected == 22

        by_kind = {b.kind: b for b in payload.blocks}
        assert by_kind["memory"].token_count == 10
        assert by_kind["knowledge"].token_count == 12
        assert [i["record_id"] for i in by_kind["knowledge"].items] == ["k1", "k2"]
        # retriever name stamped as provenance
        assert by_kind["memory"].items[0]["retriever"] == "recall"

    @pytest.mark.asyncio
    async def test_retriever_failure_contained(self):
        good = make_candidate()
        manager = MemoryManager(
            MemoryRuntime(),
            retrievers=[
                ("bad", FakeRetriever(error=ValueError("boom"))),
                ("good", FakeRetriever([good])),
            ],
        )
        payload = await manager.assemble(AssembleRequest(query_text="q"))
        assert payload.stats.candidates_total == 1
        assert payload.stats.per_retriever == {"bad": 0, "good": 1}
        assert payload.stats.errors == ["retriever:bad: ValueError: boom"]
        assert payload.stats.blocks == 1

    @pytest.mark.asyncio
    async def test_scorer_failure_passes_items_through(self):
        manager = MemoryManager(
            MemoryRuntime(),
            retrievers=[("r", FakeRetriever([make_candidate()]))],
            scorers=[("broken", FakeScorer(error=RuntimeError("nope")))],
        )
        payload = await manager.assemble(AssembleRequest(query_text="q"))
        # containment skips the stage without dropping items
        assert payload.stats.injected_total == 1
        assert payload.stats.errors == ["scorer:broken: RuntimeError: nope"]

    @pytest.mark.asyncio
    async def test_policy_filters_items(self):
        manager = MemoryManager(
            MemoryRuntime(),
            retrievers=[
                (
                    "r",
                    FakeRetriever(
                        [make_candidate(record_id=f"m{i}") for i in range(5)]
                    ),
                )
            ],
            policies=[("head3", FakePolicy(keep=3))],
        )
        payload = await manager.assemble(AssembleRequest(query_text="q"))
        assert payload.stats.candidates_total == 5
        assert payload.stats.injected_total == 3

    @pytest.mark.asyncio
    async def test_unknown_kind_renders_provenance_only_block(self):
        exotic = make_candidate(kind="exotic", record_id="x1")
        manager = MemoryManager(
            MemoryRuntime(), retrievers=[("r", FakeRetriever([exotic]))]
        )
        payload = await manager.assemble(AssembleRequest(query_text="q"))
        assert payload.stats.blocks == 1
        assert payload.blocks[0].kind == "exotic"
        assert payload.blocks[0].messages == []
        assert payload.blocks[0].items[0]["record_id"] == "x1"

    @pytest.mark.asyncio
    async def test_kernel_bug_backstop_returns_empty_payload(self, monkeypatch):
        manager = MemoryManager(
            MemoryRuntime(), retrievers=[("r", FakeRetriever([make_candidate()]))]
        )

        def _explode(req, items):
            raise RuntimeError("kernel bug")

        monkeypatch.setattr(manager, "_render_blocks", _explode)
        payload = await manager.assemble(AssembleRequest(query_text="q"))
        assert payload.blocks == []
        assert payload.stats.errors == ["assemble:kernel: RuntimeError: kernel bug"]

    @pytest.mark.asyncio
    async def test_latency_recorded(self):
        manager = MemoryManager(MemoryRuntime())
        payload = await manager.assemble(AssembleRequest(query_text="q"))
        assert payload.stats.latency_ms >= 0.0

    def test_payload_messages_flatten_in_block_order(self):
        ai = AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "1"}])
        tool = ToolMessage(content="block", tool_call_id="1")
        payload = MemoryPayload(
            blocks=[
                InjectionBlock(kind="memory", messages=[ai, tool]),
                InjectionBlock(kind="knowledge", messages=[]),
            ]
        )
        assert payload.messages() == [ai, tool]

    def test_stats_to_dict_round_trip(self):
        stats = AssembleStats(
            candidates_total=3,
            per_retriever={"r": 3},
            injected_total=2,
            tokens_injected=20,
            blocks=1,
            latency_ms=1.234,
            errors=["scorer:x: ValueError: y"],
        )
        d = stats.to_dict()
        assert d["candidates_total"] == 3
        assert d["per_retriever"] == {"r": 3}
        assert d["latency_ms"] == 1.23
        assert d["errors"] == ["scorer:x: ValueError: y"]
        assert "extra" not in d


# ---------------------------------------------------------------------------
# Capture dispatch
# ---------------------------------------------------------------------------


class TestCapture:
    @pytest.mark.asyncio
    async def test_dispatch_by_event_kind(self):
        turn_writer = FakeWriter(kinds=("turn_end",))
        end_writer = FakeWriter(kinds=("session_end", "idle_archive"))
        manager = MemoryManager(
            MemoryRuntime(),
            writers=[("turns", turn_writer), ("teardown", end_writer)],
        )

        await manager.capture(CaptureEvent(kind="turn_end", phase=2))
        assert len(turn_writer.events) == 1
        assert turn_writer.events[0].phase == 2
        assert end_writer.events == []

        await manager.capture(CaptureEvent(kind="session_end"))
        assert len(turn_writer.events) == 1
        assert len(end_writer.events) == 1

    @pytest.mark.asyncio
    async def test_writer_failure_contained_others_still_run(self):
        broken = FakeWriter(kinds=("turn_end",), error=RuntimeError("aux down"))
        healthy = FakeWriter(kinds=("turn_end",))
        manager = MemoryManager(
            MemoryRuntime(), writers=[("broken", broken), ("healthy", healthy)]
        )
        await manager.capture(CaptureEvent(kind="turn_end"))  # must not raise
        assert len(healthy.events) == 1

    @pytest.mark.asyncio
    async def test_broken_event_kinds_contained(self):
        class BrokenKinds:
            @property
            def event_kinds(self):
                raise AttributeError("misbuilt")

            async def on_event(self, event):
                raise AssertionError("must not be called")

        healthy = FakeWriter(kinds=("turn_end",))
        manager = MemoryManager(
            MemoryRuntime(), writers=[("broken", BrokenKinds()), ("ok", healthy)]
        )
        await manager.capture(CaptureEvent(kind="turn_end"))
        assert len(healthy.events) == 1

    def test_invalid_event_kind_rejected(self):
        with pytest.raises(ValueError, match="bogus"):
            CaptureEvent(kind="bogus")


# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------


class TestExtensions:
    def test_tools_aggregated_in_order(self):
        t1, t2, t3 = object(), object(), object()
        manager = MemoryManager(
            MemoryRuntime(),
            extensions=[("a", FakeExtension([t1, t2])), ("b", FakeExtension([t3]))],
        )
        assert manager.extension_tools() == [t1, t2, t3]

    def test_extension_failure_contained(self):
        t1 = object()
        manager = MemoryManager(
            MemoryRuntime(),
            extensions=[
                ("broken", FakeExtension(error=RuntimeError("no tools"))),
                ("ok", FakeExtension([t1])),
            ],
        )
        assert manager.extension_tools() == [t1]


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------


class TestMemoryConfigPlumbing:
    def test_defaults_keep_manager_off_and_pipeline_empty(self):
        cfg = _parse_memory_config({})
        assert cfg.manager_enabled is False
        assert cfg.pipeline.retrievers == []
        assert cfg.pipeline.scorers == []
        assert cfg.pipeline.policies == []
        assert cfg.pipeline.writers == []
        assert cfg.pipeline.extensions == []

    def test_parse_manager_and_pipeline(self):
        cfg = _parse_memory_config(
            {
                "enabled": True,
                "manager": {"enabled": True},
                "pipeline": {
                    "retrievers": ["recall_two_tier", "kb_notes"],
                    "scorers": [],
                    "policies": ["token_budget"],
                    "writers": ["interval_extractor"],
                },
            }
        )
        assert cfg.manager_enabled is True
        assert cfg.pipeline.retrievers == ["recall_two_tier", "kb_notes"]
        assert cfg.pipeline.policies == ["token_budget"]
        assert cfg.pipeline.writers == ["interval_extractor"]
        assert cfg.pipeline.extensions == []

    def test_parse_tolerates_null_sections(self):
        cfg = _parse_memory_config({"manager": None, "pipeline": None})
        assert cfg.manager_enabled is False
        assert cfg.pipeline.retrievers == []

    def test_parse_manager_bool_shorthand(self):
        assert _parse_memory_config({"manager": True}).manager_enabled is True
        assert _parse_memory_config({"manager": False}).manager_enabled is False

    def test_legacy_fields_unchanged(self):
        cfg = _parse_memory_config({"budget_tokens": 5000, "observer_interval": 3})
        assert cfg.budget_tokens == 5000
        assert cfg.observer_interval == 3
        assert cfg.enabled is False
