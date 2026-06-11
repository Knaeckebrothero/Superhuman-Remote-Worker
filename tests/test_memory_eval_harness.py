"""Offline tests for the memory eval harness (eval/memory/).

Everything here runs without a database, embedding endpoint, or LLM —
infra-touching pieces (stores, manager) are replaced by fakes injected
through HarnessHandles' factory seams. The committed fixture
eval/memory/fixtures/tiny_longmemeval.json uses the real LongMemEval
schema, so the loader tests double as schema documentation.
"""

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval.memory import infra
from eval.memory.arms import ArmSpec, IngestionOptions, _apply_auxiliary_overrides
from eval.memory.datasets import (
    LMESession,
    LMETurn,
    load_longmemeval,
    subset_questions,
)
from eval.memory.ingest import HarnessHandles, ingest_question
from eval.memory.metrics import (
    aggregate,
    collapse_to_sessions,
    coverage_at_k,
    first_hit_rank,
    ndcg_at_k,
    question_metrics,
    recall_at_k,
)
from eval.memory.query import answer_retrieval, session_ranking
from eval.memory.report import render_comparison, render_markdown
from eval.memory.run import _existing_question_ids
from src.services.memory.types import AssembleStats, InjectionBlock, MemoryPayload

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "eval" / "memory" / "fixtures" / "tiny_longmemeval.json"
ARMS_DIR = REPO_ROOT / "eval" / "memory" / "arms"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeDB:
    """Just enough of PostgresDB for provenance + count queries."""

    def __init__(self):
        self.rows = []  # dicts: id, job_id, project_id

    async def fetch(self, query, project):
        return [r for r in self.rows if r["project_id"] == project]

    async def fetchval(self, query, project):
        return len([r for r in self.rows if r["project_id"] == project])


class FakeStore:
    """Records store() kwargs; mirrors rows into the FakeDB."""

    def __init__(self, job_id, project_id, db):
        self.job_id = job_id
        self.project_id = project_id
        self.db = db
        self.stored = []

    async def store(self, **kwargs):
        self.stored.append(kwargs)
        mem_id = uuid.uuid4()
        self.db.rows.append(
            {"id": mem_id, "job_id": self.job_id, "project_id": self.project_id}
        )
        return mem_id


class FakeManager:
    """Records capture()/assemble() traffic; returns a canned payload."""

    def __init__(self, store, job_id, project_id, payload=None):
        self.store = store
        self.job_id = job_id
        self.project_id = project_id
        self.captures = []
        self.assembles = []
        self.payload = payload or MemoryPayload(blocks=[], stats=AssembleStats())

    async def capture(self, event):
        self.captures.append(event)

    async def assemble(self, req):
        self.assembles.append(req)
        return self.payload


def make_handles(db=None, payload=None, run_id="testrun"):
    """HarnessHandles wired entirely with fakes."""
    db = db if db is not None else FakeDB()
    stores = []
    managers = []

    def store_factory(job_id, project_id):
        store = FakeStore(job_id, project_id, db)
        stores.append(store)
        return store

    def manager_factory(store, job_id, project_id):
        manager = FakeManager(store, job_id, project_id, payload=payload)
        managers.append(manager)
        return manager

    handles = HarnessHandles(
        config=SimpleNamespace(llm=SimpleNamespace(model="test-model")),
        run_id=run_id,
        db=db,
        store_factory=store_factory,
        manager_factory=manager_factory,
    )
    return handles, stores, managers


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------


class TestDatasetLoader:
    def test_fixture_loads(self):
        questions = load_longmemeval(str(FIXTURE))
        assert [q.question_id for q in questions] == [
            "tiny_single_1",
            "tiny_multi_1",
            "tiny_cat_1_abs",
        ]
        single = questions[0]
        assert single.question_type == "single-session-user"
        assert len(single.sessions) == 3
        assert single.evidence_session_ids == frozenset({"s2_dog"})
        assert not single.is_abstention
        assert single.sessions[0].date.startswith("2023/05/01")

        multi = questions[1]
        assert multi.evidence_session_ids == frozenset({"s1_trip_book", "s3_trip_move"})

        abstention = questions[2]
        assert abstention.is_abstention
        assert abstention.evidence_session_ids == frozenset()

    def test_has_answer_turn_labels(self):
        questions = load_longmemeval(str(FIXTURE))
        dog_session = questions[0].sessions[1]
        assert dog_session.turns[0].has_answer
        assert not dog_session.turns[2].has_answer

    def test_rounds_pairing(self):
        session = LMESession(
            session_id="s",
            turns=[
                LMETurn(role="assistant", content="hello first"),
                LMETurn(role="user", content="u1"),
                LMETurn(role="assistant", content="a1"),
                LMETurn(role="user", content="trailing"),
            ],
        )
        rounds = list(session.rounds())
        # assistant-first pairs with an empty user; trailing user yields (u, None)
        assert rounds[0][0].content == "" and rounds[0][1].content == "hello first"
        assert rounds[1][0].content == "u1" and rounds[1][1].content == "a1"
        assert rounds[2][0].content == "trailing" and rounds[2][1] is None

    def test_session_count_mismatch_raises(self, tmp_path):
        broken = json.loads(FIXTURE.read_text())
        broken[0]["haystack_session_ids"] = broken[0]["haystack_session_ids"][:-1]
        path = tmp_path / "broken.json"
        path.write_text(json.dumps(broken))
        with pytest.raises(ValueError, match="session ids"):
            load_longmemeval(str(path))


class TestSubset:
    def test_stratified_limit_covers_types(self):
        questions = load_longmemeval(str(FIXTURE))
        picked = subset_questions(questions, limit=2, seed=1)
        assert len(picked) == 2
        assert {q.question_type for q in picked} == {
            "single-session-user",
            "multi-session",
        }

    def test_deterministic(self):
        questions = load_longmemeval(str(FIXTURE))
        a = [q.question_id for q in subset_questions(questions, limit=2, seed=7)]
        b = [q.question_id for q in subset_questions(questions, limit=2, seed=7)]
        assert a == b

    def test_ids_filter(self):
        questions = load_longmemeval(str(FIXTURE))
        picked = subset_questions(questions, ids=["tiny_multi_1"])
        assert [q.question_id for q in picked] == ["tiny_multi_1"]
        with pytest.raises(ValueError, match="not in dataset"):
            subset_questions(questions, ids=["nope"])

    def test_type_filter(self):
        questions = load_longmemeval(str(FIXTURE))
        picked = subset_questions(questions, types=["multi-session"])
        assert [q.question_id for q in picked] == ["tiny_multi_1"]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestMetrics:
    def test_collapse(self):
        assert collapse_to_sessions(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]

    def test_recall(self):
        evidence = frozenset({"b"})
        assert recall_at_k(["a", "b", "c"], evidence, 1) == 0.0
        assert recall_at_k(["a", "b", "c"], evidence, 3) == 1.0
        assert recall_at_k([], evidence, 5) == 0.0
        assert recall_at_k(["a"], frozenset(), 5) is None

    def test_coverage(self):
        evidence = frozenset({"b", "d"})
        assert coverage_at_k(["b", "a", "c"], evidence, 3) == 0.5
        assert coverage_at_k(["b", "a", "d"], evidence, 3) == 1.0

    def test_ndcg(self):
        evidence = frozenset({"b"})
        # hit at rank 2: dcg = 1/log2(3); idcg = 1/log2(2) = 1
        assert ndcg_at_k(["a", "b", "c"], evidence, 3) == pytest.approx(
            0.6309, abs=1e-3
        )
        two = frozenset({"b", "d"})
        # hits at ranks 1+3 vs ideal ranks 1+2
        expected = (1.0 + 1.0 / 2.0) / (1.0 + 0.6309)
        assert ndcg_at_k(["b", "a", "d"], two, 3) == pytest.approx(expected, abs=1e-3)
        assert ndcg_at_k(["a"], frozenset(), 3) is None

    def test_first_hit(self):
        assert first_hit_rank(["a", "b"], frozenset({"b"})) == 2
        assert first_hit_rank(["a"], frozenset({"b"})) is None
        assert first_hit_rank(["a"], frozenset()) is None

    def test_question_metrics_keys(self):
        out = question_metrics(["a", "b"], frozenset({"b"}), ks=[1, 2])
        assert set(out) == {
            "recall@1",
            "coverage@1",
            "ndcg@1",
            "recall@2",
            "coverage@2",
            "ndcg@2",
            "first_hit_rank",
        }

    def test_aggregate(self):
        rows = [
            {
                "question_type": "multi-session",
                "metrics": {"recall@5": 1.0},
                "cost": {"tokens_injected": 100},
            },
            {
                "question_type": "multi-session",
                "metrics": {"recall@5": 0.0},
                "cost": {"tokens_injected": 300},
            },
            {
                "question_type": "single-session-user",
                "is_abstention": True,
                "metrics": {"recall@5": None},
                "cost": {"tokens_injected": 50},
            },
        ]
        summary = aggregate(rows)
        assert summary["questions"] == 3
        assert summary["abstentions"] == 1
        assert summary["overall"]["recall@5"] == 0.5  # None excluded
        assert summary["overall"]["scored_questions"] == 2
        assert summary["cost"]["tokens_injected"] == 150.0  # all rows
        assert summary["by_type"]["multi-session"]["questions"] == 2


# ---------------------------------------------------------------------------
# Scope uuids
# ---------------------------------------------------------------------------


class TestScopeUuids:
    def test_deterministic_and_distinct(self):
        assert infra.project_uuid("r1", "q1") == infra.project_uuid("r1", "q1")
        assert infra.project_uuid("r1", "q1") != infra.project_uuid("r2", "q1")
        assert infra.project_uuid("r1", "q1") != infra.project_uuid("r1", "q2")
        s1 = infra.session_uuid("r1", "q1", "sA")
        assert s1 == infra.session_uuid("r1", "q1", "sA")
        assert s1 != infra.session_uuid("r1", "q1", "sB")
        assert s1 != infra.project_uuid("r1", "q1")


# ---------------------------------------------------------------------------
# Ingestion drivers
# ---------------------------------------------------------------------------


class TestSeamIngest:
    @pytest.fixture
    def question(self):
        return load_longmemeval(str(FIXTURE))[0]  # 3 sessions: 2+2+1 rounds

    @pytest.mark.asyncio
    async def test_event_sequence(self, question):
        handles, stores, managers = make_handles()
        arm = ArmSpec(name="t", ingestion=IngestionOptions(mode="seam"))
        result = await ingest_question(question, arm, handles)

        assert result.sessions == 3
        assert result.turns == 5
        assert result.assembles == 5  # read path once per round
        assert len(managers) == 3

        for manager, rounds in zip(managers, (2, 2, 1)):
            kinds = [e.kind for e in manager.captures]
            assert kinds == ["turn_end"] * rounds + ["session_end"]
            turn_counts = [e.turn_count for e in manager.captures[:-1]]
            assert turn_counts == list(range(1, rounds + 1))
            # message lists grow by 2 per round (Human + AI), snapshots stable
            lengths = [len(e.messages) for e in manager.captures[:-1]]
            assert lengths == [2 * (i + 1) for i in range(rounds)]

    @pytest.mark.asyncio
    async def test_read_path_query_is_last_user_message(self, question):
        handles, _, managers = make_handles()
        arm = ArmSpec(name="t", ingestion=IngestionOptions(mode="seam"))
        await ingest_question(question, arm, handles)
        first = managers[0].assembles[0]
        assert first.query_text == question.sessions[0].turns[0].content
        assert first.model == "test-model"

    @pytest.mark.asyncio
    async def test_read_path_off(self, question):
        handles, _, managers = make_handles()
        arm = ArmSpec(
            name="t",
            ingestion=IngestionOptions(mode="seam", read_path_per_turn=False),
        )
        result = await ingest_question(question, arm, handles)
        assert result.assembles == 0
        assert all(not m.assembles for m in managers)

    @pytest.mark.asyncio
    async def test_scoping(self, question):
        handles, _, managers = make_handles()
        arm = ArmSpec(name="t", ingestion=IngestionOptions(mode="seam"))
        await ingest_question(question, arm, handles)
        projects = {m.project_id for m in managers}
        assert len(projects) == 1
        jobs = [m.job_id for m in managers]
        assert len(set(jobs)) == 3
        assert projects.pop() not in jobs

    @pytest.mark.asyncio
    async def test_date_prefix(self, question):
        handles, _, managers = make_handles()
        arm = ArmSpec(
            name="t",
            ingestion=IngestionOptions(mode="seam", date_prefix=True),
        )
        await ingest_question(question, arm, handles)
        first_event = managers[0].captures[0]
        assert first_event.messages[0].content.startswith("[Session date: 2023/05/01")
        # only the session's first user message is prefixed
        second_event = managers[0].captures[1]
        assert second_event.messages[2].content == question.sessions[0].turns[2].content

    @pytest.mark.asyncio
    async def test_assemble_errors_surface(self, question):
        payload = MemoryPayload(
            blocks=[], stats=AssembleStats(errors=["retriever:x: Boom: y"])
        )
        handles, _, _ = make_handles(payload=payload)
        arm = ArmSpec(name="t", ingestion=IngestionOptions(mode="seam"))
        result = await ingest_question(question, arm, handles)
        assert result.errors
        assert "Boom" in result.errors[0]


class TestVerbatimIngest:
    @pytest.mark.asyncio
    async def test_store_kwargs(self):
        question = load_longmemeval(str(FIXTURE))[0]
        handles, stores, managers = make_handles()
        arm = ArmSpec(
            name="t",
            ingestion=IngestionOptions(mode="verbatim", verbatim_importance=0.7),
        )
        result = await ingest_question(question, arm, handles)

        assert not managers  # no manager in verbatim mode
        assert result.turns == 5
        assert result.stored_memories == 5  # FakeDB count

        first = stores[0].stored[0]
        assert first["remaining_turns"] == 0
        assert first["importance"] == 0.7
        assert first["source"] == "observer"
        assert "User: Can you help me plan" in first["content"]
        assert "Assistant: Sure!" in first["content"]
        assert first["source_turn_start"] == 0
        assert first["source_turn_end"] == 1

    @pytest.mark.asyncio
    async def test_date_prefix(self):
        question = load_longmemeval(str(FIXTURE))[0]
        handles, stores, _ = make_handles()
        arm = ArmSpec(
            name="t",
            ingestion=IngestionOptions(mode="verbatim", date_prefix=True),
        )
        await ingest_question(question, arm, handles)
        assert "[Session date: 2023/05/01" in stores[0].stored[0]["content"]
        assert "[Session date" not in stores[0].stored[1]["content"]


# ---------------------------------------------------------------------------
# Query / provenance
# ---------------------------------------------------------------------------


def _payload_for(records):
    """MemoryPayload with one memory block whose items carry record ids."""
    return MemoryPayload(
        blocks=[
            InjectionBlock(
                kind="memory",
                items=[{"record_id": str(rid), "token_count": 10} for rid in records],
            )
        ],
        stats=AssembleStats(tokens_injected=30, candidates_total=3, latency_ms=1.5),
    )


class TestSessionRanking:
    def test_collapse_and_skip_unresolvable(self):
        r1, r2, r3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        job_a, job_b = uuid.uuid4(), uuid.uuid4()
        payload = _payload_for([r1, r2, r3, uuid.uuid4()])
        record_to_job = {str(r1): job_a, str(r2): job_b, str(r3): job_a}
        ranked = session_ranking(payload, record_to_job, {job_a: "sA", job_b: "sB"})
        assert ranked == ["sA", "sB"]


class TestAnswerRetrieval:
    @pytest.mark.asyncio
    async def test_end_to_end_with_fakes(self):
        question = load_longmemeval(str(FIXTURE))[0]  # evidence: s2_dog
        run_id = "testrun"
        job_workout = infra.session_uuid(run_id, question.question_id, "s3_workout")
        job_dog = infra.session_uuid(run_id, question.question_id, "s2_dog")
        project = infra.project_uuid(run_id, question.question_id)

        rec_workout, rec_dog = uuid.uuid4(), uuid.uuid4()
        db = FakeDB()
        db.rows = [
            {"id": rec_workout, "job_id": job_workout, "project_id": project},
            {"id": rec_dog, "job_id": job_dog, "project_id": project},
        ]
        payload = _payload_for([rec_workout, rec_dog])
        handles, _, managers = make_handles(db=db, payload=payload, run_id=run_id)
        arm = ArmSpec(name="t", ks=[1, 3])

        row = await answer_retrieval(question, arm, handles)

        assert managers[-1].assembles[0].query_text == question.question
        assert row["ranked_sessions"] == ["s3_workout", "s2_dog"]
        assert row["metrics"]["recall@1"] == 0.0
        assert row["metrics"]["recall@3"] == 1.0
        assert row["metrics"]["first_hit_rank"] == 2.0
        assert row["cost"]["memories_injected"] == 2
        assert row["cost"]["tokens_injected"] == 30
        assert row["items"][1]["session"] == "s2_dog"

    @pytest.mark.asyncio
    async def test_abstention_metrics_are_none(self):
        question = load_longmemeval(str(FIXTURE))[2]
        handles, _, _ = make_handles()
        arm = ArmSpec(name="t", ks=[5])
        row = await answer_retrieval(question, arm, handles)
        assert row["is_abstention"]
        assert row["metrics"]["recall@5"] is None


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------


class TestArms:
    def test_committed_arm_files_parse(self):
        flat = ArmSpec.from_file(str(ARMS_DIR / "flat_verbatim.yaml"))
        assert flat.ingestion.mode == "verbatim"
        assert flat.config_overrides["memory"]["dedup_threshold"] == 1.01

        current = ArmSpec.from_file(str(ARMS_DIR / "persistent_current.yaml"))
        assert current.ingestion.mode == "seam"
        assert current.ingestion.read_path_per_turn
        assert current.ks == [1, 3, 5, 10]

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="ingestion.mode"):
            IngestionOptions(mode="bogus")

    def test_build_agent_config_flat(self):
        from eval.memory.arms import build_agent_config

        arm = ArmSpec.from_file(str(ARMS_DIR / "flat_verbatim.yaml"))
        config = build_agent_config(arm)
        assert config.memory.enabled
        assert config.memory.manager_enabled
        assert config.memory.dedup_threshold == 1.01
        assert "recall_two_tier" in config.memory.pipeline.retrievers

    def test_build_agent_config_persistent_writers(self):
        from eval.memory.arms import build_agent_config

        arm = ArmSpec.from_file(str(ARMS_DIR / "persistent_current.yaml"))
        config = build_agent_config(arm)
        assert config.memory.pipeline.writers == [
            "persistent_interval_extractor",
            "teardown_extractor",
        ]

    def test_seam_arm_with_no_writers_raises(self):
        from eval.memory.arms import build_agent_config

        arm = ArmSpec(
            name="t",
            config_overrides={"memory": {"pipeline": {"writers": []}}},
            ingestion=IngestionOptions(mode="seam"),
        )
        with pytest.raises(ValueError, match="store nothing"):
            build_agent_config(arm)

    def test_auxiliary_overrides(self, monkeypatch):
        config = SimpleNamespace(
            auxiliary=SimpleNamespace(
                model="m0", base_url=None, api_key=None, timeout=120
            )
        )
        monkeypatch.setenv("EVAL_TEST_KEY", "sk-test")
        _apply_auxiliary_overrides(
            config,
            {"model": "m1", "base_url": "http://x", "api_key_env": "EVAL_TEST_KEY"},
        )
        assert config.auxiliary.model == "m1"
        assert config.auxiliary.base_url == "http://x"
        assert config.auxiliary.api_key == "sk-test"

    def test_auxiliary_missing_env_raises(self, monkeypatch):
        monkeypatch.delenv("EVAL_TEST_KEY_MISSING", raising=False)
        config = SimpleNamespace(auxiliary=SimpleNamespace(api_key=None))
        with pytest.raises(ValueError, match="EVAL_TEST_KEY_MISSING"):
            _apply_auxiliary_overrides(config, {"api_key_env": "EVAL_TEST_KEY_MISSING"})


# ---------------------------------------------------------------------------
# Runner plumbing + reports
# ---------------------------------------------------------------------------


class TestRunnerPlumbing:
    def test_existing_question_ids(self, tmp_path):
        path = tmp_path / "results.jsonl"
        path.write_text(
            json.dumps({"question_id": "a"})
            + "\n"
            + "not json\n"
            + json.dumps({"question_id": "b"})
            + "\n"
        )
        assert _existing_question_ids(path) == {"a", "b"}

    def test_existing_question_ids_missing_file(self, tmp_path):
        assert _existing_question_ids(tmp_path / "nope.jsonl") == set()


class TestReport:
    def _summary(self, recall):
        return {
            "questions": 4,
            "abstentions": 1,
            "overall": {"recall@5": recall, "ndcg@5": 0.5, "scored_questions": 3},
            "cost": {"tokens_injected": 123.0},
            "by_type": {
                "multi-session": {"recall@5": recall, "ndcg@5": 0.5, "questions": 4}
            },
            "arm": {"name": "armX"},
        }

    def test_render_markdown(self):
        text = render_markdown(self._summary(0.75))
        assert "**overall**" in text
        assert "0.750" in text
        assert "multi-session" in text
        assert "tokens_injected: 123.0" in text

    def test_render_comparison(self):
        text = render_comparison(self._summary(0.5), self._summary(0.75))
        assert "+0.250" in text
        assert "armX" in text
