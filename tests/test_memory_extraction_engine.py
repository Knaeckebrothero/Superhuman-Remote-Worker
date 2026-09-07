"""MemoryExtractionEngine — chunked, full-coverage extraction (Slice 2).

Proves the properties the design (knowledge-history/done/memory_extraction_before_compaction.md
§6 Slice 2) hangs on: a large history is chunk-planned (not 40-capped), memories
come from the *oldest* messages the single-shot path would have dropped, the
overlap region doesn't double-store facts (in-memory dedup), and a failing chunk
is skipped without aborting the run.
"""

import re
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from shared.runtime.core.llm_retry import NO_RETRY
from shared.runtime.services.auxiliary import ExtractedMemories, ExtractedMemory
from agent.services.memory.extraction_engine import (
    MemoryExtractionEngine,
    _dedup_in_memory,
)


# Deterministic counter: 4 chars per token, no tiktoken.
def char_counter(text: str) -> int:
    return len(text) // 4


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    """No real sleeps between extraction retries in tests."""
    monkeypatch.setattr(
        "agent.services.memory.extraction_engine.EXTRACTION_BACKOFF_SECONDS", (0.0, 0.0)
    )


def _mem(fact: str) -> ExtractedMemory:
    return ExtractedMemory(
        content=fact,
        summary=fact,
        keywords=[fact.lower()],
        importance=0.8,
        type="factual",
        retrieval_messages=[],
    )


class _FakeAux:
    """Aux that 'extracts' one memory per distinct FACT_<n> marker in a chunk.

    Records every chunk text it saw so tests can assert coverage + chunking.
    ``fail_distinct`` holds 1-based *distinct-chunk* ordinals that always raise
    (retries of the same chunk keep failing → deterministic skip), exercising
    the skip-on-failure path without a retry landing on a passing call.
    """

    def __init__(self, max_context_tokens=4000, fail_distinct=()):
        self.max_context_tokens = max_context_tokens
        self.calls = []
        self.retry_policies_seen = []
        self._distinct = []
        self.fail_distinct = set(fail_distinct)

    async def chain(self, task, timeout=None, retry_policy=None):
        # Mirrors AuxiliaryLLM.chain's signature. The engine passes
        # retry_policy=NO_RETRY because it owns its own retry loop below, and a
        # double that silently rejects the kwarg turns every chunk into a
        # skipped chunk rather than a test failure at the seam.
        self.retry_policies_seen.append(retry_policy)
        text = task.build_context()
        self.calls.append(text)
        if text not in self._distinct:
            self._distinct.append(text)
        if (self._distinct.index(text) + 1) in self.fail_distinct:
            raise RuntimeError("aux blip")
        facts = list(dict.fromkeys(re.findall(r"FACT_\d+", text)))  # unique, in order
        return ExtractedMemories(memories=[_mem(f) for f in facts])


class _FakeStore:
    def __init__(self):
        self.stored = []

    async def store(self, **kwargs):
        self.stored.append(kwargs)
        return uuid.uuid4()


def _make_engine(aux, store, **kwargs):
    # Small reserves so a 4k window still leaves a >=1000-token chunk budget.
    return MemoryExtractionEngine(
        aux,
        store,
        extraction_prompt="",
        token_counter=char_counter,
        output_reserve=200,
        **kwargs,
    )


def _history(n=60):
    """n alternating messages, each tagged FACT_i, large enough to force >1 chunk."""
    msgs = []
    for i in range(n):
        body = f"FACT_{i}: " + "word " * 30
        msgs.append(
            HumanMessage(content=body) if i % 2 == 0 else AIMessage(content=body)
        )
    return msgs


class TestChunkingBypassesFortyCap:
    @pytest.mark.asyncio
    async def test_multichunk_covers_oldest_messages(self):
        aux, store = _FakeAux(), _FakeStore()
        engine = _make_engine(aux, store)
        messages = _history(60)

        stored = await engine.run(messages, phase=3)

        # More than one chunk → the 40-cap is bypassed by construction.
        assert len(aux.calls) > 1
        contents = {s["content"] for s in store.stored}
        # The oldest messages (FACT_0..FACT_19) fall outside messages[-40:] —
        # the single-shot path would have dropped them. Here they're stored.
        assert "FACT_0" in contents
        assert "FACT_1" in contents
        # Full coverage, every fact stored exactly once (dedup, next test).
        assert contents == {f"FACT_{i}" for i in range(60)}
        assert stored == 60
        # phase propagated to the store contract.
        assert all(s["source_phase"] == 3 for s in store.stored)
        assert all(s["source"] == "observer" for s in store.stored)

    @pytest.mark.asyncio
    async def test_overlap_does_not_double_store(self):
        """Facts re-extracted in the overlap region collapse to one store call."""
        aux, store = _FakeAux(), _FakeStore()
        engine = _make_engine(aux, store, overlap_ratio=0.15)
        messages = _history(60)

        await engine.run(messages)

        # A fact in the overlap seed is extracted by two adjacent chunks, but
        # in-memory dedup means store() is called once per unique fact.
        keys = [s["content"] for s in store.stored]
        assert len(keys) == len(set(keys))  # no duplicate stores


class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_failing_chunk_is_skipped_not_fatal(self):
        # No overlap so the second chunk's facts are exclusive: skipping it
        # must lose them (and only them), not abort the whole run.
        aux = _FakeAux(fail_distinct=(2,))  # 2nd distinct chunk always errors
        store = _FakeStore()
        engine = _make_engine(aux, store, overlap_ratio=0.0)
        messages = _history(60)

        stored = await engine.run(messages)

        # Run completed with a partial result rather than aborting: the skipped
        # chunk's exclusive facts are gone, the rest survived.
        assert 0 < stored < 60
        assert len(store.stored) == stored
        contents = {s["content"] for s in store.stored}
        assert "FACT_0" in contents  # a surviving chunk's facts are present
        missing = {f"FACT_{i}" for i in range(60)} - contents
        assert missing  # the skipped chunk left a real gap

    @pytest.mark.asyncio
    async def test_engine_opts_out_of_the_aux_layer_retry(self):
        # Retry belongs at exactly one layer per call path. This engine owns its
        # own loop (MAX_EXTRACTION_ATTEMPTS + EXTRACTION_BACKOFF_SECONDS), so it
        # must tell AuxiliaryLLM not to add a second one — otherwise a transient
        # costs 2x the provider calls and the inner failure never reaches this
        # loop's per-chunk attempt accounting.
        aux, store = _FakeAux(), _FakeStore()
        engine = _make_engine(aux, store)

        await engine.run(_history(20))

        assert aux.retry_policies_seen  # the engine actually called through
        assert all(p is NO_RETRY for p in aux.retry_policies_seen)

    @pytest.mark.asyncio
    async def test_empty_history_stores_nothing(self):
        aux, store = _FakeAux(), _FakeStore()
        engine = _make_engine(aux, store)
        assert await engine.run([]) == 0
        assert store.stored == []

    @pytest.mark.asyncio
    async def test_none_store_is_safe(self):
        engine = _make_engine(_FakeAux(), None)
        assert await engine.run(_history(4)) == 0


class TestDedupInMemory:
    def test_collapses_whitespace_and_case_variants(self):
        mems = [_mem("The DB uses soft deletes"), _mem("the   db uses SOFT deletes")]
        out = _dedup_in_memory(mems)
        assert len(out) == 1

    def test_keeps_higher_importance_on_collision(self):
        low = ExtractedMemory(
            content="same fact",
            summary="s",
            keywords=[],
            importance=0.4,
            type="factual",
        )
        high = ExtractedMemory(
            content="same fact",
            summary="s",
            keywords=[],
            importance=0.9,
            type="factual",
        )
        out = _dedup_in_memory([low, high])
        assert len(out) == 1
        assert out[0].importance == 0.9

    def test_skips_blank_content(self):
        assert _dedup_in_memory([_mem("   ")]) == []
