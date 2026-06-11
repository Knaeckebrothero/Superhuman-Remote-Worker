"""Incremental ingestion: haystack sessions → memories, through the seam.

seam mode replays each session against MemoryManager exactly like the
live persistent loop (persistent_graph.py:549-581 read path,
:426-441 turn_end capture, persistent_app teardown session_end):

    per round:  append HumanMessage
                assemble(query=last user msg)        # read_path_per_turn
                append AIMessage
                turn_count += 1
                capture(turn_end, messages, turn_count)
    at end:     capture(session_end, messages)

so TTL decrement, access-resets, the elapsed extraction gate, and the
final teardown extraction all accrue as in production. capture() is
awaited (production fire-and-forgets the turn_end task) — same writer
effects, deterministic completion.

verbatim mode skips the extraction LLM entirely: each user-assistant
round is stored directly with remaining_turns=0, so nothing is ever
TTL-pinned and question-time retrieval is pure hybrid search over a flat
round-granularity index (the cheap smoke arm + published-baseline arm).

Scoping: one question = one project (cross-session sharing), one
haystack session = one job — each memory row's job_id IS its source
session, which is what the retrieval metrics collapse on.
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from .arms import ArmSpec
from .datasets import LMEQuestion, LMESession
from .infra import build_manager, make_recall_store, project_uuid, session_uuid

logger = logging.getLogger(__name__)

#: (job_id, project_id) -> RecallStore-like
StoreFactory = Callable[[uuid.UUID, uuid.UUID], Any]
#: (store, job_id, project_id) -> MemoryManager-like
ManagerFactory = Callable[[Any, uuid.UUID, uuid.UUID], Any]


@dataclass
class HarnessHandles:
    """Shared per-run handles; factories are injectable for offline tests."""

    config: Any
    run_id: str = "dev"
    db: Any = None
    embedding: Any = None
    aux_llm: Any = None
    extraction_prompt: str = ""
    store_factory: Optional[StoreFactory] = None
    manager_factory: Optional[ManagerFactory] = None

    def make_store(self, job_id: uuid.UUID, project_id: uuid.UUID) -> Any:
        if self.store_factory is not None:
            return self.store_factory(job_id, project_id)
        return make_recall_store(
            self.db, self.embedding, self.config, job_id, project_id
        )

    def make_manager(self, store: Any, job_id: uuid.UUID, project_id: uuid.UUID) -> Any:
        if self.manager_factory is not None:
            return self.manager_factory(store, job_id, project_id)
        return build_manager(
            self.config,
            recall_store=store,
            auxiliary_llm=self.aux_llm,
            extraction_prompt=self.extraction_prompt,
            job_id=job_id,
            project_id=project_id,
        )


@dataclass
class IngestResult:
    question_id: str
    sessions: int = 0
    turns: int = 0
    assembles: int = 0
    #: Rows in the question's project scope after ingestion (None when no
    #: real DB handle, i.e. unit tests).
    stored_memories: Optional[int] = None
    #: Contained plugin failures surfaced by the per-turn read path
    #: (AssembleStats.errors). Writer failures log via the manager and
    #: land in the run log; a near-zero stored_memories count is the
    #: companion signal.
    errors: List[str] = field(default_factory=list)


def _first_user_prefix(session: LMESession, arm: ArmSpec) -> str:
    if arm.ingestion.date_prefix and session.date:
        return f"[Session date: {session.date}] "
    return ""


async def _replay_session_seam(
    session: LMESession,
    arm: ArmSpec,
    handles: HarnessHandles,
    manager: Any,
    result: IngestResult,
) -> None:
    from langchain_core.messages import AIMessage, HumanMessage

    from src.services.memory import AssembleRequest, CaptureEvent
    from src.services.memory.plugins.legacy import build_persistent_query_text

    model = getattr(handles.config.llm, "model", None)
    messages: List[Any] = []
    turn_count = 0
    prefix = _first_user_prefix(session, arm)

    for user, assistant in session.rounds():
        content = (prefix + user.content) if not messages else user.content
        messages.append(HumanMessage(content=content))

        if arm.ingestion.read_path_per_turn:
            payload = await manager.assemble(
                AssembleRequest(
                    query_text=build_persistent_query_text(messages),
                    model=model,
                )
            )
            result.assembles += 1
            result.errors.extend(payload.stats.errors)

        if assistant is not None:
            messages.append(AIMessage(content=assistant.content))
        turn_count += 1
        await manager.capture(
            CaptureEvent(
                kind="turn_end",
                messages=list(messages),
                turn_count=turn_count,
            )
        )

    await manager.capture(CaptureEvent(kind="session_end", messages=list(messages)))
    result.turns += turn_count


async def _replay_session_verbatim(
    session: LMESession,
    arm: ArmSpec,
    store: Any,
    result: IngestResult,
) -> None:
    prefix = _first_user_prefix(session, arm)
    for i, (user, assistant) in enumerate(session.rounds()):
        parts = [f"User: {(prefix + user.content) if i == 0 else user.content}"]
        if assistant is not None:
            parts.append(f"Assistant: {assistant.content}")
        await store.store(
            content="\n".join(parts),
            importance=arm.ingestion.verbatim_importance,
            memory_type="factual",
            source="observer",
            source_turn_start=i,
            source_turn_end=i + 1,
            remaining_turns=0,
        )
        result.turns += 1


async def ingest_question(
    question: LMEQuestion,
    arm: ArmSpec,
    handles: HarnessHandles,
) -> IngestResult:
    """Ingest one question's haystack, session by session, in order.

    Sessions are deliberately sequential (recency rank and TTL dynamics
    depend on insertion order, exactly as memories accrue over time in
    production); parallelism belongs at the question level.
    """
    result = IngestResult(question_id=question.question_id)
    project = project_uuid(handles.run_id, question.question_id)

    for session in question.sessions:
        job = session_uuid(handles.run_id, question.question_id, session.session_id)
        store = handles.make_store(job, project)

        if arm.ingestion.mode == "verbatim":
            await _replay_session_verbatim(session, arm, store, result)
        else:
            manager = handles.make_manager(store, job, project)
            await _replay_session_seam(session, arm, handles, manager, result)
        result.sessions += 1

    if handles.db is not None:
        result.stored_memories = await handles.db.fetchval(
            "SELECT COUNT(*) FROM memories WHERE project_id = $1", project
        )
        if arm.ingestion.mode == "seam" and not result.stored_memories:
            logger.warning(
                "%s: seam ingestion stored 0 memories across %d sessions — "
                "extraction is likely failing (check the run log)",
                question.question_id,
                result.sessions,
            )
    return result
