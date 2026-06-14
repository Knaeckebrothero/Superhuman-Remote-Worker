"""Ingestion verdict service + wiring (overhaul Phase 4, slice 2).

Covers IngestionVerdictService.adjudicate (success / fail-safe), the
maybe_attach wiring helper, and the IngestionVerdictTask context formatting.
The store-side ADD/NOOP/UPDATE/MERGE behaviour is in test_recall_store.py.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.core.loader import IngestionConfig, _parse_memory_config
from src.services.auxiliary import IngestionVerdict, IngestionVerdictTask
from src.services.memory.ingestion import (
    IngestionVerdictService,
    maybe_attach_ingestion_verdict,
)


class _FakeAux:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.tasks = []

    async def chain(self, task, timeout=None):
        self.tasks.append(task)
        if self.exc:
            raise self.exc
        return self.result


def _rec(content="old fact", sim=0.8, created=None):
    return SimpleNamespace(content=content, similarity=sim, created_at=created)


class TestAdjudicate:
    @pytest.mark.asyncio
    async def test_returns_aux_verdict(self):
        verdict = IngestionVerdict(action="UPDATE", target_indices=[1], reason="x")
        aux = _FakeAux(result=verdict)
        svc = IngestionVerdictService(aux, "PROMPT", IngestionConfig(enabled=True))
        out = await svc.adjudicate(
            content="new", summary=None, keywords=[], similar=[_rec()]
        )
        assert out is verdict
        # the task saw the neighbour content + similarity
        task = aux.tasks[0]
        assert isinstance(task, IngestionVerdictTask)
        ctx = task.build_context()
        assert "new" in ctx and "old fact" in ctx and "similarity 0.80" in ctx

    @pytest.mark.asyncio
    async def test_includes_age_when_created_at_present(self):
        aux = _FakeAux(result=IngestionVerdict(action="ADD", reason="r"))
        svc = IngestionVerdictService(aux, "P", IngestionConfig(enabled=True))
        created = datetime(2026, 6, 1, tzinfo=timezone.utc)
        await svc.adjudicate(
            content="c", summary=None, keywords=None, similar=[_rec(created=created)]
        )
        assert "2026-06-01" in aux.tasks[0].build_context()

    @pytest.mark.asyncio
    async def test_aux_exception_falls_back_to_add(self):
        aux = _FakeAux(exc=RuntimeError("aux down"))
        svc = IngestionVerdictService(aux, "P", IngestionConfig(enabled=True))
        out = await svc.adjudicate(
            content="c", summary=None, keywords=None, similar=[_rec()]
        )
        assert out.action == "ADD"  # never lose a write to a verdict failure

    @pytest.mark.asyncio
    async def test_wrong_shape_falls_back_to_add(self):
        aux = _FakeAux(result="not a verdict")
        svc = IngestionVerdictService(aux, "P", IngestionConfig(enabled=True))
        out = await svc.adjudicate(
            content="c", summary=None, keywords=None, similar=[_rec()]
        )
        assert out.action == "ADD"


class TestMaybeAttach:
    def test_attaches_when_enabled(self):
        store = SimpleNamespace(ingestion_verdict=None)
        aux = _FakeAux()
        cfg = SimpleNamespace(
            ingestion=IngestionConfig(enabled=True, verdict_top_k=8, review_floor=0.7)
        )
        svc = maybe_attach_ingestion_verdict(store, aux, cfg)
        assert svc is not None
        assert store.ingestion_verdict is svc
        assert svc.top_k == 8 and svc.review_floor == 0.7

    def test_noop_when_disabled(self):
        store = SimpleNamespace(ingestion_verdict=None)
        cfg = SimpleNamespace(ingestion=IngestionConfig(enabled=False))
        assert maybe_attach_ingestion_verdict(store, _FakeAux(), cfg) is None
        assert store.ingestion_verdict is None

    def test_noop_without_aux(self):
        store = SimpleNamespace(ingestion_verdict=None)
        cfg = SimpleNamespace(ingestion=IngestionConfig(enabled=True))
        assert maybe_attach_ingestion_verdict(store, None, cfg) is None
        assert store.ingestion_verdict is None

    def test_noop_without_store(self):
        cfg = SimpleNamespace(ingestion=IngestionConfig(enabled=True))
        assert maybe_attach_ingestion_verdict(None, _FakeAux(), cfg) is None


class TestIngestionConfigParse:
    def test_defaults_off(self):
        mc = _parse_memory_config({})
        assert mc.ingestion.enabled is False
        assert mc.ingestion.verdict_top_k == 5
        assert mc.ingestion.review_floor == 0.6

    def test_parses_knobs(self):
        mc = _parse_memory_config(
            {"ingestion": {"enabled": True, "verdict_top_k": 9, "review_floor": 0.5}}
        )
        assert mc.ingestion.enabled is True
        assert mc.ingestion.verdict_top_k == 9
        assert mc.ingestion.review_floor == 0.5

    def test_bool_shorthand(self):
        assert _parse_memory_config({"ingestion": True}).ingestion.enabled is True


class TestExtractionConfigParse:
    def test_write_gate_defaults_on(self):
        assert _parse_memory_config({}).extraction.write_gate is True

    def test_write_gate_off(self):
        mc = _parse_memory_config({"extraction": {"write_gate": False}})
        assert mc.extraction.write_gate is False
