"""LongMemEval dataset loading (arXiv:2410.10813, ICLR 2025).

Parses the released JSON files (longmemeval_s.json / _m / _oracle — all
share one schema) into typed records. The files are NOT committed (large;
see README for the download recipe); tests run against the committed
fixture in eval/memory/fixtures/tiny_longmemeval.json, which uses the
same schema.

Schema per instance (LongMemEval repo README):
    question_id          str — "_abs" suffix marks abstention variants
    question_type        single-session-user | single-session-assistant |
                         single-session-preference | temporal-reasoning |
                         knowledge-update | multi-session
    question, answer     str
    question_date        str
    haystack_dates       list[str], one per session
    haystack_session_ids list[str]
    haystack_sessions    list[list[{role, content, has_answer?}]]
    answer_session_ids   list[str] — the evidence sessions (empty/absent
                         for some abstention instances)
"""

import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

QUESTION_TYPES = (
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "temporal-reasoning",
    "knowledge-update",
    "multi-session",
)


@dataclass
class LMETurn:
    """One chat turn inside a haystack session."""

    role: str  # "user" | "assistant"
    content: str
    #: Turn-level evidence label (present only on evidence turns in the
    #: released data) — kept for later finer-grained metrics.
    has_answer: bool = False


@dataclass
class LMESession:
    """One haystack session (a full user-assistant chat)."""

    session_id: str
    date: str = ""
    turns: List[LMETurn] = field(default_factory=list)

    def rounds(self) -> Iterator[Tuple[LMETurn, Optional[LMETurn]]]:
        """Yield (user, assistant) pairs in order.

        Sessions alternate user/assistant in the released data; this is
        tolerant anyway — a user turn with no following assistant turn
        yields (user, None), and stray assistant-first turns are paired
        with an empty user turn so no content is silently dropped.
        """
        pending_user: Optional[LMETurn] = None
        for turn in self.turns:
            if turn.role == "user":
                if pending_user is not None:
                    yield pending_user, None
                pending_user = turn
            else:
                yield (pending_user or LMETurn(role="user", content="")), turn
                pending_user = None
        if pending_user is not None:
            yield pending_user, None


@dataclass
class LMEQuestion:
    """One benchmark instance: a question over a haystack of sessions."""

    question_id: str
    question_type: str
    question: str
    answer: str
    question_date: str = ""
    sessions: List[LMESession] = field(default_factory=list)
    answer_session_ids: List[str] = field(default_factory=list)

    @property
    def is_abstention(self) -> bool:
        """Abstention variants ask about facts NOT in the haystack."""
        return self.question_id.endswith("_abs")

    @property
    def evidence_session_ids(self) -> frozenset:
        return frozenset(self.answer_session_ids)


def load_longmemeval(path: str) -> List[LMEQuestion]:
    """Load a LongMemEval-format JSON file into typed questions.

    Raises:
        FileNotFoundError / json.JSONDecodeError: bad path or file.
        ValueError: instance missing required keys (corrupt download).
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a top-level JSON list of instances")

    questions: List[LMEQuestion] = []
    for i, inst in enumerate(raw):
        try:
            session_ids = inst["haystack_session_ids"]
            session_dates = inst.get("haystack_dates") or [""] * len(session_ids)
            raw_sessions = inst["haystack_sessions"]
        except KeyError as e:
            raise ValueError(f"{path}: instance {i} missing key {e}") from e
        if not (len(session_ids) == len(raw_sessions)):
            raise ValueError(
                f"{path}: instance {i} has {len(session_ids)} session ids "
                f"but {len(raw_sessions)} sessions"
            )

        sessions = [
            LMESession(
                session_id=str(sid),
                date=str(date),
                turns=[
                    LMETurn(
                        role=t.get("role", "user"),
                        content=t.get("content", "") or "",
                        has_answer=bool(t.get("has_answer", False)),
                    )
                    for t in turns
                ],
            )
            for sid, date, turns in zip(session_ids, session_dates, raw_sessions)
        ]

        question = LMEQuestion(
            question_id=str(inst["question_id"]),
            question_type=str(inst.get("question_type", "unknown")),
            question=str(inst["question"]),
            answer=str(inst.get("answer", "")),
            question_date=str(inst.get("question_date", "")),
            sessions=sessions,
            answer_session_ids=[str(s) for s in inst.get("answer_session_ids", [])],
        )

        missing = question.evidence_session_ids - {s.session_id for s in sessions}
        if missing and not question.is_abstention:
            logger.warning(
                "%s: evidence sessions %s not in haystack — retrieval "
                "metrics for it can never hit",
                question.question_id,
                sorted(missing),
            )
        questions.append(question)

    logger.info("Loaded %d questions from %s", len(questions), path)
    return questions


def subset_questions(
    questions: Sequence[LMEQuestion],
    limit: Optional[int] = None,
    types: Optional[Sequence[str]] = None,
    ids: Optional[Sequence[str]] = None,
    seed: int = 0,
) -> List[LMEQuestion]:
    """Deterministic, type-stratified subset for cheap runs.

    ``ids`` filters exactly (and ignores limit/types). Otherwise filter by
    ``types``, shuffle within each type with ``seed``, then round-robin
    across types until ``limit`` — so a --limit 12 run still covers every
    question type instead of whatever sorts first.
    """
    if ids:
        wanted = set(ids)
        picked = [q for q in questions if q.question_id in wanted]
        missing = wanted - {q.question_id for q in picked}
        if missing:
            raise ValueError(f"question ids not in dataset: {sorted(missing)}")
        return picked

    pool = [q for q in questions if not types or q.question_type in types]
    if limit is None or limit >= len(pool):
        return list(pool)

    by_type: dict = {}
    for q in pool:
        by_type.setdefault(q.question_type, []).append(q)
    rng = random.Random(seed)
    for group in by_type.values():
        group.sort(key=lambda q: q.question_id)
        rng.shuffle(group)

    picked = []
    type_order = sorted(by_type)
    while len(picked) < limit:
        progressed = False
        for qtype in type_order:
            if by_type[qtype]:
                picked.append(by_type[qtype].pop(0))
                progressed = True
                if len(picked) >= limit:
                    break
        if not progressed:
            break
    return picked
