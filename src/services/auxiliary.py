"""AuxiliaryLLM — Unified support task system.

Provides two execution modes for background/support LLM tasks:

- **Chain mode** (`chain()`): Single LLM call with structured output.
  System prompt + context → Pydantic model. No tools, no loop.
  For tasks that just need reasoning over provided context.

- **Agent mode** (`agent()`): Short-lived tool loop with structured output.
  The LLM can make tool calls (search KB, read files, write notes),
  then a final structured-output call produces the result.
  Capped iterations. Not a full job — no workspace, no todos, no phases.

All tasks use `with_structured_output()` for reliable structured returns.

See docs/features/auxiliary.md for the full design document.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# =============================================================================
# Output schemas (Pydantic models for with_structured_output)
# =============================================================================


class ExtractedMemory(BaseModel):
    """A single extracted memory from conversation."""

    content: str = Field(description="The insight (1-3 sentences, self-contained)")
    summary: str = Field(description="One-line summary (under 100 chars)")
    keywords: List[str] = Field(description="Relevant terms for search (3-8 keywords)")
    importance: float = Field(
        description="How useful is this for future work? (0.0-1.0)",
        ge=0.0,
        le=1.0,
    )
    type: str = Field(
        description="One of: factual, procedural, error_solution, vocabulary, relational"
    )


class ExtractedMemories(BaseModel):
    """Structured output for memory extraction."""

    memories: List[ExtractedMemory] = Field(
        description="List of extracted memories. Empty list if nothing noteworthy."
    )


class CurationResult(BaseModel):
    """Structured output for knowledge curation (agent mode)."""

    notes_created: int = Field(description="Number of new knowledge notes created")
    notes_updated: int = Field(description="Number of existing notes updated")
    summary: str = Field(description="Brief summary of what was curated")


# NOTE: ConversationSummary (the summarization output schema) lives in
# src/core/context.py because it's tightly coupled with the compaction
# formatting logic there. SummarizeTask imports it from there.


# =============================================================================
# Task base classes
# =============================================================================


class AuxTask(ABC):
    """Base class for chain-mode tasks.

    Subclasses define the system prompt, context assembly, and output schema.
    The AuxiliaryLLM handles execution via with_structured_output().
    """

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """System prompt for the LLM call."""
        ...

    @abstractmethod
    def build_context(self) -> str:
        """Build the user message content from task inputs."""
        ...

    @property
    @abstractmethod
    def output_schema(self) -> Type[BaseModel]:
        """Pydantic model class for structured output."""
        ...


class AuxAgentTask(AuxTask):
    """Base class for agent-mode tasks (adds tool access).

    Agent tasks run a short tool loop, then a final structured-output call
    produces the result. The output_schema defines what the final call returns.
    """

    @abstractmethod
    def get_tools(self) -> list:
        """Return the list of LangChain tools available to this task."""
        ...


# =============================================================================
# Task implementations
# =============================================================================


MEMORY_EXTRACTION_PROMPT = """\
You are a memory extraction system. Given a segment of agent conversation, \
extract noteworthy information that would be useful in future turns.

For each memory, provide:
- content: The insight (1-3 sentences, self-contained, no references to "the conversation")
- summary: One-line summary (under 100 chars)
- keywords: Relevant terms for search (3-8 keywords)
- importance: How useful is this for future work? (0.0-1.0)
- type: One of: factual, procedural, error_solution, vocabulary, relational

Type definitions:
- factual: A discovered fact about the codebase, data, or domain
- procedural: A process, pattern, or sequence that works (or doesn't)
- error_solution: A problem encountered and how it was resolved
- vocabulary: Domain-specific terminology or naming conventions
- relational: How things connect (A depends on B, X requires Y)

Focus on:
- Decisions made and why
- Facts discovered about the codebase/data/domain
- Mistakes made and corrections applied
- Patterns that worked or failed
- Constraints and requirements discovered

Do NOT extract:
- Routine tool calls with no insight
- Repetitive information already captured
- Raw data or file contents (summarize instead)

If nothing noteworthy, return an empty memories list."""


class ExtractMemoriesTask(AuxTask):
    """Extract memories from a conversation segment.

    Chain mode task that replaces MemoryObserver.extract_memories().
    """

    def __init__(self, messages: List[BaseMessage], phase: int = 0):
        self.messages = messages
        self.phase = phase

    @property
    def system_prompt(self) -> str:
        return MEMORY_EXTRACTION_PROMPT

    def build_context(self) -> str:
        return _format_messages_for_extraction(self.messages)

    @property
    def output_schema(self) -> Type[BaseModel]:
        return ExtractedMemories


# Default summarization prompt used when no template is provided
_DEFAULT_SUMMARIZATION_PROMPT = """\
You are compressing a conversation for handoff to your future self. After this summary,
the raw history will be gone — this summary is the ONLY context you will have to continue working.

EXCLUSIONS: Do NOT summarize the content of workspace.md or plan.md.
These files are persistent and will be re-injected. Only note ACTIONS TAKEN
on these files (e.g., "Updated workspace.md with new blockers").

Preservation priority (when space is limited):
  User corrections and constraints > Errors and failed approaches > Active work > Completed work

What to preserve (critical):
- Completed work: what was actually done, not what was planned
- Key decisions: choices made and WHY (the reasoning matters)
- Discovered information: entity IDs, file paths, version numbers, exact error messages
- Current state: where the agent is in the plan, what comes next
- Errors and blockers: exact error text, what was tried, what is still blocked
- Failed approaches: what did NOT work, so the agent does not retry

What to omit:
- Routine tool calls that succeeded without notable results
- Verbose outputs saved in files (just reference the file path)
- Debugging back-and-forth that led to a solution (just note the solution)
- Planning discussions captured in the plan file
- Pleasantries, acknowledgments, filler"""


class SummarizeTask(AuxTask):
    """Summarize a conversation segment into structured fields.

    Chain mode task that replaces the inline LLM call in
    ContextManager._single_pass_summarize().

    The output schema is ConversationSummary from src/core/context.py.
    """

    def __init__(
        self,
        conversation_text: str,
        summarization_prompt: Optional[str] = None,
        max_summary_length: int = 10000,
    ):
        self.conversation_text = conversation_text
        self._summarization_prompt = summarization_prompt
        self.max_summary_length = max_summary_length

    @property
    def system_prompt(self) -> str:
        if self._summarization_prompt:
            # The template has {conversation} and {max_summary_length} placeholders.
            # Strip the conversation placeholder — it goes in build_context().
            # Render max_summary_length into the instructions.
            from collections import defaultdict

            rendered = self._summarization_prompt.format_map(
                defaultdict(
                    str,
                    conversation="",  # Will be sent as HumanMessage
                    max_summary_length=str(self.max_summary_length),
                )
            )
            # Clean up the empty "Conversation:" section left by the placeholder
            rendered = rendered.replace("\nConversation:\n\n\n", "\n")
            return rendered.strip()
        return _DEFAULT_SUMMARIZATION_PROMPT

    def build_context(self) -> str:
        return (
            f"Summarize the following conversation. "
            f"Keep the total summary under {self.max_summary_length} tokens. "
            f"Weight recent messages more heavily — the end of the transcript "
            f"is the active context.\n\n"
            f"{self.conversation_text}"
        )

    @property
    def output_schema(self) -> Type[BaseModel]:
        from src.core.context import ConversationSummary

        return ConversationSummary


CURATION_SYSTEM_PROMPT = """\
You are the knowledge curator for a project. Your job is to read an agent's \
work artifacts and extract structured, reusable knowledge notes into the \
project knowledge base. You do NOT do the work yourself — you read what was \
done and distill it into knowledge that future jobs can use.

## What You Extract

Every job produces knowledge. Find it and write it as typed notes:

| Note Type | What to Look For |
|-----------|-----------------|
| decision | Architecture choices, technology picks, trade-off analysis |
| learning | What worked, what didn't, debugging insights, error-solution pairs |
| code | Key patterns, conventions, API designs, module responsibilities |
| goal | Project objectives, success criteria, definition of done |
| plan | Roadmap items, milestones, priorities |
| state | Current project status, what's done/in-progress/blocked |
| question | Open items, unresolved decisions, things to investigate |
| source | Documents, URLs, conversations that informed decisions |
| retrospective | Phase reviews, what went well vs. poorly |

## Editorial Judgment

DO extract:
- Decisions with reasoning (even small ones)
- Error-solution pairs (debugging gold for future jobs)
- Architecture patterns and conventions
- Things that didn't work and why
- Open questions and uncertainties

DO NOT extract:
- Implementation details obvious from reading code
- Temporary state only relevant to this job's execution
- Redundant information already in the knowledge base (check with kb_search first)
- Trivial observations ("we created a file called X")

## Retrieval Messages

For every note you write, generate 2-4 retrieval messages — synthetic queries \
describing when this note should surface. Think: "What question would someone \
ask when they need this knowledge?"

## Linking

Use the links parameter on kb_write to create relationships between notes. \
Always search for related existing notes with kb_search before writing. \
Link types: REFERENCES, DERIVED_FROM, SUPPORTS, CONTRADICTS, ANSWERS, \
DEPENDS_ON, SUPERSEDES, IMPLEMENTS.

## Confidence

- high: explicit decision with documented reasoning, verified result
- medium: reasonable inference, partially verified
- low: uncertain, limited evidence, needs verification

When you are done curating, report how many notes you created and updated."""


class CurateKnowledgeTask(AuxAgentTask):
    """Extract knowledge notes from phase artifacts.

    Agent mode task that replaces the curator subjob.
    Uses kb_search, kb_write, kb_update, kb_read tools.
    """

    def __init__(
        self,
        phase_data: str,
        workspace_md: str,
        plan_md: str,
        existing_notes: List[str],
        kb_tools: list,
    ):
        self.phase_data = phase_data
        self.workspace_md = workspace_md
        self.plan_md = plan_md
        self.existing_notes = existing_notes
        self._kb_tools = kb_tools

    @property
    def system_prompt(self) -> str:
        return CURATION_SYSTEM_PROMPT

    def build_context(self) -> str:
        parts = [
            "## Phase Artifacts",
            self.phase_data,
            "",
            "## Current Workspace",
            self.workspace_md,
            "",
            "## Current Plan",
            self.plan_md,
        ]
        if self.existing_notes:
            parts.extend([
                "",
                "## Existing Knowledge (check before writing duplicates)",
                "\n".join(self.existing_notes),
            ])
        return "\n".join(parts)

    @property
    def output_schema(self) -> Type[BaseModel]:
        return CurationResult

    def get_tools(self) -> list:
        return self._kb_tools


# =============================================================================
# AuxiliaryLLM — the unified executor
# =============================================================================


class AuxiliaryLLM:
    """Unified support task execution with chain and agent modes.

    All tasks use with_structured_output() for reliable structured returns.

    Args:
        llm: The support model (e.g. gpt-oss-120b, or main LLM as fallback)
        config: AuxiliaryConfig dataclass (from loader.py)
    """

    def __init__(
        self,
        llm: BaseChatModel,
        max_iterations: int = 15,
        timeout: float = 120.0,
    ):
        self.llm = llm
        self.max_iterations = max_iterations
        self.timeout = timeout

    async def chain(self, task: AuxTask) -> BaseModel:
        """Single LLM call: system prompt + context -> structured output.

        For tasks that need reasoning but no tool access.

        Args:
            task: AuxTask instance with system_prompt, build_context(), output_schema

        Returns:
            Pydantic model instance matching task.output_schema

        Raises:
            asyncio.TimeoutError: If the LLM call exceeds timeout
        """
        structured_llm = self.llm.with_structured_output(task.output_schema)
        messages = [
            SystemMessage(content=task.system_prompt),
            HumanMessage(content=task.build_context()),
        ]

        result = await asyncio.wait_for(
            structured_llm.ainvoke(messages),
            timeout=self.timeout,
        )

        logger.debug(
            f"AuxiliaryLLM.chain completed: {task.__class__.__name__} -> "
            f"{type(result).__name__}"
        )
        return result

    async def agent(self, task: AuxAgentTask) -> BaseModel:
        """Short-lived agent loop: system prompt + tools -> structured result.

        Runs a tool loop capped at max_iterations, then makes one final
        structured-output call to produce the result.

        Args:
            task: AuxAgentTask with system_prompt, build_context(),
                  output_schema, get_tools()

        Returns:
            Pydantic model instance matching task.output_schema

        Raises:
            asyncio.TimeoutError: If any individual LLM call exceeds timeout
        """
        tools = task.get_tools()
        llm_with_tools = self.llm.bind_tools(tools)
        tool_map = {t.name: t for t in tools}

        messages: List[BaseMessage] = [
            SystemMessage(content=task.system_prompt),
            HumanMessage(content=task.build_context()),
        ]

        tool_calls_made = 0
        for iteration in range(self.max_iterations):
            response = await asyncio.wait_for(
                llm_with_tools.ainvoke(messages),
                timeout=self.timeout,
            )
            messages.append(response)

            if not response.tool_calls:
                # LLM is done with tool calls
                break

            # Execute tool calls
            for tool_call in response.tool_calls:
                tool_name = tool_call.get("name", "unknown")
                tool_args = tool_call.get("args", {})
                tool_id = tool_call.get("id", "")

                try:
                    tool_fn = tool_map.get(tool_name)
                    if tool_fn is None:
                        result = f"Error: Unknown tool '{tool_name}'"
                    else:
                        result = await tool_fn.ainvoke(tool_args)
                        if not isinstance(result, str):
                            result = str(result)
                    tool_calls_made += 1
                except Exception as e:
                    result = f"Error executing {tool_name}: {e}"
                    logger.warning(
                        f"AuxiliaryLLM.agent: tool {tool_name} failed: {e}"
                    )

                messages.append(ToolMessage(
                    content=result,
                    tool_call_id=tool_id,
                ))

        logger.info(
            f"AuxiliaryLLM.agent completed: {task.__class__.__name__}, "
            f"{tool_calls_made} tool calls in {iteration + 1} iterations"
        )

        # Final structured-output call to get the result
        structured_llm = self.llm.with_structured_output(task.output_schema)
        messages.append(HumanMessage(
            content="Summarize what you accomplished in the required output format."
        ))

        result = await asyncio.wait_for(
            structured_llm.ainvoke(messages),
            timeout=self.timeout,
        )
        return result


# =============================================================================
# Helpers
# =============================================================================


def _format_messages_for_extraction(messages: List[BaseMessage]) -> str:
    """Format messages into readable text for the extraction LLM.

    Filters out injection messages (workspace, memory, instruction)
    to focus on actual conversation content.
    """
    from src.core.workspace_injection import is_workspace_injection_message

    lines = []
    for msg in messages:
        if is_workspace_injection_message(msg):
            continue

        role = _get_message_role(msg)
        content = msg.content if hasattr(msg, "content") else ""

        if not content or not content.strip():
            continue

        # Truncate very long tool results
        if isinstance(msg, ToolMessage) and len(content) > 1000:
            content = content[:1000] + "... [truncated]"

        lines.append(f"[{role}] {content}")

    return "\n\n".join(lines)


def _get_message_role(msg: BaseMessage) -> str:
    """Get a human-readable role label for a message."""
    if isinstance(msg, HumanMessage):
        return "User"
    elif isinstance(msg, AIMessage):
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            tool_names = ", ".join(tc.get("name", "?") for tc in msg.tool_calls)
            return f"Agent (calls: {tool_names})"
        return "Agent"
    elif isinstance(msg, ToolMessage):
        return "Tool Result"
    return "System"
