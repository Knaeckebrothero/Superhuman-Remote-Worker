"""Knowledge ingestion verdict service + builder (OKF KB slice 2 PR2).

Covers KnowledgeVerdictService.adjudicate (cost guard, content-hash pre-filter,
success, fail-safe) and the build_knowledge_verdict_service factory. The KB
analog of test_memory_ingestion.py.
"""

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from shared.runtime.services.auxiliary import KnowledgeVerdict, KnowledgeVerdictTask
from agent.services.knowledge.ingestion import (
    KnowledgeVerdictService,
    build_knowledge_verdict_service,
    gate_candidate,
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


def _hash(content):
    return hashlib.sha256(content.encode()).hexdigest()


def _neighbour(content="old fact", sim=0.8, title="Old", created=None, chash=None):
    return SimpleNamespace(
        content=content,
        similarity=sim,
        title=title,
        created_at=created,
        content_hash=chash if chash is not None else _hash(content),
    )


def _cfg(verdict=True, top_k=5, floor=0.6):
    return SimpleNamespace(verdict=verdict, verdict_top_k=top_k, review_floor=floor)


_PROMPT = "Adjudicate the note."


class TestAdjudicate:
    @pytest.mark.asyncio
    async def test_cost_guard_empty_neighbours_adds_without_llm(self):
        aux = _FakeAux()
        svc = KnowledgeVerdictService(aux, _cfg())
        out = await svc.adjudicate(content="brand new", neighbours=[], prompt=_PROMPT)
        assert out.action == "ADD"
        assert aux.tasks == []  # no LLM call for a genuinely new note

    @pytest.mark.asyncio
    async def test_content_hash_prefilter_discards_exact_dup(self):
        aux = _FakeAux()
        svc = KnowledgeVerdictService(aux, _cfg())
        dup = _neighbour(content="identical text")
        out = await svc.adjudicate(
            content="identical text", neighbours=[dup], prompt=_PROMPT
        )
        assert out.action == "DISCARD"
        assert out.target_indices == [1]
        assert aux.tasks == []  # exact dup never reaches the LLM

    @pytest.mark.asyncio
    async def test_returns_aux_verdict_and_formats_neighbours(self):
        verdict = KnowledgeVerdict(
            action="SUPERSEDE", target_indices=[1], reason="stale"
        )
        aux = _FakeAux(result=verdict)
        svc = KnowledgeVerdictService(aux, _cfg())
        out = await svc.adjudicate(
            content="new fact",
            neighbours=[_neighbour(content="old fact", sim=0.82, title="Auth")],
            prompt=_PROMPT,
        )
        assert out is verdict
        task = aux.tasks[0]
        assert isinstance(task, KnowledgeVerdictTask)
        ctx = task.build_context()
        assert "new fact" in ctx and "old fact" in ctx
        assert "similarity 0.82" in ctx and "Auth" in ctx

    @pytest.mark.asyncio
    async def test_includes_age_when_created_at_present(self):
        aux = _FakeAux(result=KnowledgeVerdict(action="ADD", reason="r"))
        svc = KnowledgeVerdictService(aux, _cfg())
        created = datetime(2026, 6, 1, tzinfo=timezone.utc)
        await svc.adjudicate(
            content="c",
            neighbours=[_neighbour(created=created)],
            prompt=_PROMPT,
        )
        assert "2026-06-01" in aux.tasks[0].build_context()

    @pytest.mark.asyncio
    async def test_aux_exception_falls_back_to_add(self):
        aux = _FakeAux(exc=RuntimeError("aux down"))
        svc = KnowledgeVerdictService(aux, _cfg())
        out = await svc.adjudicate(
            content="c", neighbours=[_neighbour()], prompt=_PROMPT
        )
        assert out.action == "ADD"  # never lose a write to a verdict failure

    @pytest.mark.asyncio
    async def test_wrong_shape_falls_back_to_add(self):
        aux = _FakeAux(result="not a verdict")
        svc = KnowledgeVerdictService(aux, _cfg())
        out = await svc.adjudicate(
            content="c", neighbours=[_neighbour()], prompt=_PROMPT
        )
        assert out.action == "ADD"


class _FakeStore:
    """Minimal KnowledgeStore stand-in for gate_candidate."""

    def __init__(self, neighbours):
        self._neighbours = neighbours
        self.embedding_service = SimpleNamespace(embed=self._embed)
        self.calls = {}

    async def _embed(self, text):
        self.calls["embed"] = text
        return [0.1, 0.2, 0.3]

    async def find_similar_many(self, project_id, embedding, k=5, min_similarity=0.6):
        self.calls["find_similar_many"] = {
            "project_id": project_id,
            "embedding": embedding,
            "k": k,
            "min_similarity": min_similarity,
        }
        return self._neighbours


class TestGateCandidate:
    @pytest.mark.asyncio
    async def test_empty_neighbours_adds_and_uses_knobs(self):
        aux = _FakeAux()
        svc = KnowledgeVerdictService(aux, _cfg(top_k=8, floor=0.7))
        store = _FakeStore(neighbours=[])
        decision = await gate_candidate(
            svc, store, "pid", content="brand new", prompt=_PROMPT
        )
        assert decision.verdict.action == "ADD"
        assert decision.targets == []
        # embedding + neighbour fetch used the service knobs
        assert store.calls["embed"] == "brand new"
        assert store.calls["find_similar_many"]["k"] == 8
        assert store.calls["find_similar_many"]["min_similarity"] == 0.7
        assert aux.tasks == []  # cost guard: no LLM

    @pytest.mark.asyncio
    async def test_resolves_target_indices_to_notes(self):
        n1 = _neighbour(content="a")
        n1.note_id = "note-a"
        n2 = _neighbour(content="b")
        n2.note_id = "note-b"
        aux = _FakeAux(
            result=KnowledgeVerdict(action="SUPERSEDE", target_indices=[2], reason="x")
        )
        svc = KnowledgeVerdictService(aux, _cfg())
        store = _FakeStore(neighbours=[n1, n2])
        decision = await gate_candidate(
            svc, store, "pid", content="new", prompt=_PROMPT
        )
        assert decision.verdict.action == "SUPERSEDE"
        assert [t.note_id for t in decision.targets] == ["note-b"]

    @pytest.mark.asyncio
    async def test_out_of_range_index_ignored(self):
        n1 = _neighbour(content="a")
        n1.note_id = "note-a"
        aux = _FakeAux(
            result=KnowledgeVerdict(action="UPDATE", target_indices=[5], reason="x")
        )
        svc = KnowledgeVerdictService(aux, _cfg())
        store = _FakeStore(neighbours=[n1])
        decision = await gate_candidate(
            svc, store, "pid", content="new", prompt=_PROMPT
        )
        assert decision.targets == []


class TestBuildService:
    def test_builds_when_enabled(self):
        aux = _FakeAux()
        svc = build_knowledge_verdict_service(aux, _cfg(top_k=8, floor=0.7))
        assert svc is not None
        assert svc.top_k == 8 and svc.review_floor == 0.7

    def test_none_when_verdict_disabled(self):
        assert build_knowledge_verdict_service(_FakeAux(), _cfg(verdict=False)) is None

    def test_none_without_aux(self):
        assert build_knowledge_verdict_service(None, _cfg(verdict=True)) is None

    def test_none_without_config(self):
        assert build_knowledge_verdict_service(_FakeAux(), None) is None
