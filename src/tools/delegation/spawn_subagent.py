"""spawn_subagent — the single agent-facing delegation primitive.

One model-facing name (`spawn_subagent`) with the backend selected by operator
config (`delegation.mode`), NOT by the model. The agent sees the same name and
the same flat schema whether a call resolves to a heavy child job or a light
in-process reader.

  heavy — full child jobs (the `delegate_work` path: worktree + dispatch +
          parent freeze/checkpoint/wake + squash-merge). Not yet wired under
          this name; heavy convergence is a documented fast-follow. Until then
          the heavy branch returns a stub pointing at `delegate_work`.
  light — throwaway in-process ReAct readers that read sources and return a
          string, with no freeze/merge.

Light backend (Phase 3): each call spawns ONE reader. The model fans out by
calling this tool N times in a turn; the parent's tool node gathers those calls,
so N readers run concurrently for free (bounded by `delegation.light.max_parallel`
via a shared semaphore, and each given a unique index → unique worktree + port
range). Per call: acquire an isolated reader env (Phase 2) → build the reader LLM
from the `subagents.llm` partial (Phase 0) → run the bounded ReAct loop (Phase 1) →
release the env → return the result string. No freeze, no merge.

See knowledge-base/knowledge/issues/delegation_light_mode_missing.md for the design + plan.
"""

import asyncio
import itertools
import logging
import time
from typing import Any, Dict, List, Optional

from ...core.archiver import archive_llm_request
from ...core.loader import INHERIT_MODEL, _parse_phase_override, create_llm
from ..context import ToolContext
from .light_runner import run_light_subagent
from .reader_env import acquire_reader_env, release_reader_env

logger = logging.getLogger(__name__)


class _MeteredLLM:
    """Wraps a reader's bound chat model so every `ainvoke` folds into the
    PARENT job's usage.

    A light reader is not a job row, so without this its LLM spend would be an
    unmetered leak. Each call is archived via `archive_llm_request(...,
    call_type="subagent")` under the parent `job_id` — the same audit path the
    main loop and AuxiliaryLLM use, which the orchestrator materializes into
    `usage_events`. Fire-and-forget: a metering failure never breaks the reader.
    Keeping this outside `run_light_subagent` preserves the harness's purity —
    metering is a property of the llm handed in, not the loop.
    """

    def __init__(
        self, inner: Any, *, job_id: str, agent_type: str, model: str, index: int
    ):
        self._inner = inner
        self._job_id = job_id
        self._agent_type = agent_type
        self._model = model
        self._index = index

    async def ainvoke(self, messages, *args, **kwargs):
        start = time.monotonic()
        response = await self._inner.ainvoke(messages, *args, **kwargs)
        latency_ms = int((time.monotonic() - start) * 1000)
        if self._job_id:
            try:
                archive_llm_request(
                    job_id=self._job_id,
                    agent_type=self._agent_type,
                    messages=messages,
                    response=response,
                    model=self._model,
                    latency_ms=latency_ms,
                    call_type="subagent",
                    auxiliary_metadata={"subagent_index": self._index},
                )
            except Exception as e:  # never let metering break the reader
                logger.warning("subagent metering failed: %s", e)
        return response

    def __getattr__(self, name):
        # Delegate anything else (e.g. bind_tools) to the wrapped model.
        return getattr(self._inner, name)


SPAWN_SUBAGENT_METADATA: Dict[str, Dict[str, Any]] = {
    "spawn_subagent": {
        "module": "delegation.spawn_subagent",
        "function": "spawn_subagent",
        "description": (
            "Spawn one throwaway subagent to perform a focused, self-contained "
            "subtask (for example: read a cluster of sources and return a "
            "distilled summary) and return its result as text. Cheap and "
            "non-blocking: the subagent runs inline with its own fresh context "
            "and returns its result directly — delegating the reading keeps "
            "your own context small. To run subagents in parallel, call this "
            "tool multiple times in a SINGLE turn — one call per subtask. Each "
            "subagent cannot see this conversation, so 'task_description' must "
            "be fully self-contained. Do not delegate trivial work you can do "
            "yourself."
        ),
        "category": "delegation",
        "short_description": (
            "Spawn one throwaway subagent for a subtask; returns its result string."
        ),
        "phases": ["strategic", "tactical"],
    },
}


def _resolve_subagent_config(
    llm_cfg: Any, subagents_llm: Optional[Dict[str, Any]] = None
) -> Optional[Any]:
    """Resolve the reader's LLM config: the parent's ``llm`` overlaid with the
    roster-wide ``subagents.llm`` partial (model / provider / transport / params).

    U1 replaced the ``llm.subagent`` tier with ``subagents.llm``; the loader's
    compat mapping moves a legacy tier there, so pre-U1 experts and job
    overrides keep pinning a cheaper reader (opus → sonnet). No partial → the
    reader runs the parent's own model (the tactical fallback went with the
    tiers: there is one model).
    """
    if llm_cfg is None:
        return None
    partial = dict(subagents_llm) if isinstance(subagents_llm, dict) else {}
    # `model: inherit` means "the parent's model" — for this overlay that is
    # simply no model override (the roster resolver replaces the sentinel on
    # roster entries; the roster-wide partial keeps it verbatim).
    if partial.get("model") == INHERIT_MODEL:
        partial.pop("model")
    override = _parse_phase_override(partial) if partial else None
    if override is None:
        return llm_cfg
    return llm_cfg.with_override(override)


def _format_result(role: str, task: str, result: str, *, failed: bool = False) -> str:
    """Re-state the task/role so the parent can attribute N parallel results.

    ``failed`` flips the header. A failure used to be announced as
    "[subagent done]" followed by an error body, so the parent model read
    "done" and had to notice the contradiction further down — exactly when it
    is deciding whether to adapt or re-delegate. The audit row was already
    marked FAIL; only the text the LLM sees was misleading.
    """
    head = "[subagent failed]" if failed else "[subagent done]"
    if role:
        head += f" — role: {role}"
    return f"{head}\ntask: {task.strip()[:200]}\n\n{result}"


def _make_light_spawn(context: ToolContext, light_config: Dict[str, Any]):
    """Build the light-backend coroutine (one call = one throwaway reader)."""
    counter = itertools.count()
    max_parallel = int(light_config.get("max_parallel", 3) or 3)
    sem = asyncio.Semaphore(max_parallel)
    max_iterations = int(light_config.get("max_iterations", 10) or 10)
    max_tokens = int(light_config.get("max_tokens", 40000) or 40000)
    timeout_seconds = float(light_config.get("timeout_seconds", 240) or 0)
    allow_writes = bool(light_config.get("allow_writes", False))

    async def _spawn(
        task_description: str,
        role: str = "",
        config: str = "",  # v1: advisory; readers inherit the parent config
        expected_return_format: str = "",
    ) -> str:
        if not task_description or not task_description.strip():
            return "Error: task_description is required and must be non-empty."

        parent_tool_names = list(getattr(context, "_resolved_tool_names", []) or [])
        # Roster-wide reader partial: tool_config["subagents"] is
        # asdict(config.subagents) — {"default", "llm", "roster"} — injected
        # by agent.py / persistent_session.py next to "delegation" (a parsed
        # field, so never in config.extra). A legacy `llm.subagent` tier is
        # already mapped to subagents.llm by the loader.
        subagents_llm = (
            (getattr(context, "config", None) or {}).get("subagents") or {}
        ).get("llm") or {}
        subagent_cfg = _resolve_subagent_config(
            getattr(context, "_llm_config", None), subagents_llm
        )
        if subagent_cfg is None:
            return "Error: spawn_subagent has no LLM config to build a reader."
        limits = getattr(context, "_limits", None)
        parent_job_id = (getattr(context, "_job_metadata", None) or {}).get(
            "job_id", ""
        )
        agent_type = (getattr(context, "config", None) or {}).get(
            "agent_id", "subagent"
        )

        index = next(counter)
        failed = False
        async with sem:
            env = None
            try:
                env = await acquire_reader_env(
                    context,
                    parent_tool_names,
                    index=index,
                    allow_writes=allow_writes,
                )
                # Metering: fold the reader's LLM spend into the parent job.
                reader_llm = _MeteredLLM(
                    create_llm(subagent_cfg, limits=limits).bind_tools(env.tools),
                    job_id=parent_job_id,
                    agent_type=agent_type,
                    model=subagent_cfg.model,
                    index=index,
                )
                result = await run_light_subagent(
                    task=task_description,
                    context="",
                    tools=env.tools,
                    llm=reader_llm,
                    max_iterations=max_iterations,
                    max_tokens=max_tokens,
                    timeout_seconds=timeout_seconds,
                    port_block=env.port_block,
                    role=role,
                    expected_return_format=expected_return_format,
                )
            except Exception as e:
                logger.error("spawn_subagent (light) failed: %s", e, exc_info=True)
                failed = True
                result = f"Error: subagent failed — {e}"
            finally:
                if env is not None:
                    try:
                        await release_reader_env(env)
                    except Exception as e:
                        logger.warning("spawn_subagent teardown failed: %s", e)

        return _format_result(role, task_description, result, failed=failed)

    return _spawn


def _make_heavy_stub():
    """Heavy backend under this name is a fast-follow; stub until then."""

    async def _spawn(
        task_description: str,
        role: str = "",
        config: str = "",
        expected_return_format: str = "",
    ) -> str:
        logger.info("spawn_subagent (heavy) called — routing to delegate_work stub")
        return (
            "Error: the spawn_subagent heavy backend is not yet available under "
            "this name. Use delegate_work to spawn child jobs."
        )

    return _spawn


def create_spawn_subagent_tools(context: ToolContext) -> List[Any]:
    """Create the `spawn_subagent` tool with the backend selected by config.

    The returned tool is mode-agnostic to the model: identical name and schema
    regardless of `delegation.mode`. The factory reads `delegation.mode` once at
    build time and wires the matching backend.
    """
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field

    delegation_config = (context.config or {}).get("delegation", {})
    mode = delegation_config.get("mode", "heavy")
    light_config = delegation_config.get("light", {}) or {}

    class SpawnSubagentInput(BaseModel):
        task_description: str = Field(
            description=(
                "The complete, self-contained instruction for the subagent. It "
                "has no access to this conversation, so include everything it "
                "needs: what to read or do, and exactly what to return."
            )
        )
        role: str = Field(
            default="",
            description=(
                "Optional role hint for the subagent (e.g. 'source reader', "
                "'fact checker'). Advisory — steers tone/focus only."
            ),
        )
        config: str = Field(
            default="",
            description=(
                "Optional expert preset the subagent should adopt (e.g. "
                "'scholar'). Leave empty to inherit the parent's config."
            ),
        )
        expected_return_format: str = Field(
            default="",
            description=(
                "Optional natural-language description of the shape of the "
                "string you want back (e.g. 'a bulleted list of findings, each "
                "with a citation')."
            ),
        )

    coroutine = (
        _make_light_spawn(context, light_config)
        if mode == "light"
        else _make_heavy_stub()
    )

    spawn_tool = StructuredTool.from_function(
        coroutine=coroutine,
        name="spawn_subagent",
        description=SPAWN_SUBAGENT_METADATA["spawn_subagent"]["description"],
        args_schema=SpawnSubagentInput,
    )

    return [spawn_tool]
