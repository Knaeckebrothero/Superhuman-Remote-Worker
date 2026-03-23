"""
Universal Agent - Phase Alternation Graph.

Implements a single ReAct loop with phase alternation between:
- Strategic mode: Planning, memory updates, todo creation
- Tactical mode: Domain-specific execution

Graph Structure:
```
╔═══════════════════════════════════════════════════════════════════════════╗
║                         INITIALIZATION (runs once)                        ║
║                                                                           ║
║   init_workspace → init_strategic_todos (predefined todos)                ║
║                                                                           ║
╠═══════════════════════════════════════════════════════════════════════════╣
║                         SINGLE REACT LOOP                                 ║
║                                                                           ║
║   ┌─────────────────────────────────────────────────────────────────┐     ║
║   │         ↓                                        │              │     ║
║   │      execute ─→ check_todos ─→ todos done? ──no──┘              │     ║
║   │      (ReAct)           │                                        │     ║
║   │                       yes                                       │     ║
║   │                        ↓                                        │     ║
║   │                  archive_phase                                  │     ║
║   │                        ↓                                        │     ║
║   │                handle_transition                                │     ║
║   │       (strategic↔tactical, clears messages, loads todos)        │     ║
║   └─────────────────────────────────────────────────────────────────┘     ║
║                                    ↓                                      ║
║                               check_goal                                  ║
║                              ↓          ↓                                 ║
║                       continue        done                                ║
║                              ↓          ↓                                 ║
║                       back to LOOP     END                                ║
║                                                                           ║
╚═══════════════════════════════════════════════════════════════════════════╝
```

Phase Alternation:
- Strategic phases use predefined todos for planning/reflection
- Tactical phases use todos.yaml written by the strategic agent
- Messages are cleared at each phase transition
- workspace.md persists across phases for long-term memory
"""

import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Literal, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import StateGraph, END
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.base import BaseCheckpointSaver

from .core.state import UniversalAgentState
from .core.loader import (
    AgentConfig,
    load_summarization_prompt,
    load_auxiliary_prompt,
    get_phase_system_prompt,
)
from .core.workspace import WorkspaceManager
from .core.archiver import get_archiver
from .core.context import ContextManager, ContextConfig, ToolRetryManager, sanitize_message_history
from .core.phase_snapshot import PhaseSnapshotManager
from .core.phase import (
    handle_phase_transition,
    get_initial_strategic_todos,
    get_transition_strategic_todos,
    get_resume_strategic_todos,
)
from .managers import TodoManager, TodoStatus, PlanManager, MemoryManager
from .llm.exceptions import ContextOverflowError
from .tools.context import ToolContext
from .core.response_validator import validate_response

logger = logging.getLogger(__name__)


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def _extract_rate_limit_delay(error: Exception) -> Optional[float]:
    """Extract retry-after delay from rate limit errors.

    Checks the exception and its chain for rate limit indicators (HTTP 429)
    and extracts the retry-after value from headers or error messages.

    Args:
        error: The exception to inspect

    Returns:
        Delay in seconds if rate limit detected, None otherwise
    """
    error_str = str(error)

    # Check if this is a rate limit error
    is_rate_limit = (
        "429" in error_str
        or "rate limit" in error_str.lower()
        or "too many requests" in error_str.lower()
    )
    if not is_rate_limit:
        return None

    # Try to extract retry-after from the exception chain
    # Anthropic/OpenAI SDK exceptions may have response headers
    current = error
    while current is not None:
        # Check for response attribute with headers (httpx/SDK exceptions)
        response = getattr(current, "response", None)
        if response is not None:
            headers = getattr(response, "headers", {})
            retry_after = headers.get("retry-after")
            if retry_after:
                try:
                    return float(retry_after) + 5.0  # Add buffer
                except (ValueError, TypeError):
                    pass

        current = current.__cause__ if current.__cause__ != current else None

    # Fallback: try to extract retry-after from error message text
    match = re.search(r"retry.?after['\"]?\s*[:=]\s*['\"]?(\d+)", error_str, re.IGNORECASE)
    if match:
        return float(match.group(1)) + 5.0

    # Rate limit detected but no retry-after found — use conservative default
    return 90.0


def _extract_tool_use_failed(error: Exception) -> Optional[str]:
    """Extract failed_generation from Groq's tool_use_failed error.

    When a Groq model exceeds its max completion tokens, the output is truncated
    mid-tool-call and Groq returns a 400 error with code 'tool_use_failed' instead
    of a truncated response. This function detects that error and extracts the
    truncated output so we can give the model actionable feedback.

    Args:
        error: The exception to inspect (walks __cause__ chain)

    Returns:
        The failed_generation text if this is a tool_use_failed error, None otherwise
    """
    # Walk the exception chain (LangChain may wrap the original error)
    current: Optional[BaseException] = error
    while current is not None:
        # Primary: check structured body attribute on Groq's BadRequestError
        body = getattr(current, "body", None)
        if isinstance(body, dict):
            inner_error = body.get("error", {})
            if isinstance(inner_error, dict) and inner_error.get("code") == "tool_use_failed":
                return inner_error.get("failed_generation", "")

        # Move up the cause chain
        cause = getattr(current, "__cause__", None)
        current = cause if cause is not current else None

    # Fallback: regex on string representation
    error_str = str(error)
    if "tool_use_failed" in error_str:
        match = re.search(r'"failed_generation"\s*:\s*"(.*?)"', error_str, re.DOTALL)
        if match:
            return match.group(1)
        # Error identified but can't extract generation text
        return ""

    return None


def _build_tool_use_failed_feedback(failed_generation: str) -> str:
    """Build a feedback message from a truncated tool call output.

    Creates a message explaining what happened and showing a preview of the
    truncated output so the model can retry with smaller chunks.

    Args:
        failed_generation: The truncated output from the failed tool call

    Returns:
        Formatted feedback message for the model
    """
    # Build preview: first 100 words + last 100 words
    words = failed_generation.split()
    if len(words) <= 200:
        preview = failed_generation
    else:
        first_100 = " ".join(words[:100])
        last_100 = " ".join(words[-100:])
        preview = f"{first_100}\n\n[... {len(words) - 200} words truncated ...]\n\n{last_100}"

    return (
        "YOUR PREVIOUS TOOL CALL FAILED: Your output exceeded the maximum completion token "
        "limit and was truncated mid-tool-call. The tool call was never executed — no file "
        "was written and no action was taken.\n\n"
        "PREVIEW OF TRUNCATED OUTPUT:\n"
        f"```\n{preview}\n```\n\n"
        "TO FIX THIS: Split your content into multiple smaller tool calls. For example, "
        "use multiple `write_file` calls to write sections of a document separately, or "
        "break large operations into smaller steps. Each individual tool call must produce "
        "less output than the model's completion token limit."
    )


def _is_tool_error(content: str) -> bool:
    """Check if tool message content indicates an error.

    Args:
        content: Tool result content to check

    Returns:
        True if content appears to indicate an error
    """
    if not content:
        return False
    content_lower = content.lower()
    error_indicators = ["error:", "failed:", "exception:", "traceback"]
    return any(indicator in content_lower for indicator in error_indicators)


def _extract_markdown_content(content: str) -> str:
    """Extract clean markdown content from LLM response.

    LLMs sometimes wrap their output in markdown code blocks or add file headers like:
        **File: `plan.md`**
        ```markdown
        ...actual content...
        ```

    This function strips those wrappers to get the actual content.

    Args:
        content: Raw LLM response content

    Returns:
        Cleaned markdown content
    """
    import re

    if not content:
        return content

    result = content.strip()

    # Remove file header patterns like "**File: `filename.md`**" or "**File: filename.md**"
    result = re.sub(r'^\*\*File:\s*`?[^`\n]+`?\*\*\s*\n*', '', result, flags=re.IGNORECASE)

    # Check if the content is wrapped in a markdown code block
    # Pattern: ```markdown or ``` at start, ``` at end
    code_block_pattern = r'^```(?:markdown|md)?\s*\n(.*?)\n```\s*$'
    match = re.match(code_block_pattern, result, re.DOTALL | re.IGNORECASE)
    if match:
        result = match.group(1)

    return result.strip()


# =============================================================================
# INITIALIZATION NODES
# =============================================================================


def create_init_workspace_node(
    memory_manager: MemoryManager,
    workspace_template: str,
    config: AgentConfig,
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the init_workspace node.

    This node performs workspace initialization audit. workspace.md is no longer
    created or injected — the project knowledge base and memory system replace it.
    """

    def init_workspace(state: UniversalAgentState) -> Dict[str, Any]:
        """Initialize workspace (audit only, workspace.md no longer used)."""
        job_id = state.get("job_id", "unknown")
        logger.info(f"[{job_id}] Initializing workspace")

        # Audit workspace initialization
        auditor = get_archiver()
        if auditor:
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="initialize",
                node_name="init_workspace",
                iteration=0,
                data={"workspace": {"created": False}},
                metadata=state.get("metadata"),
                phase="strategic",
                phase_number=0,
            )

        return {
            "workspace_memory": "",
        }

    return init_workspace


def create_init_strategic_todos_node(
    workspace: WorkspaceManager,
    todo_manager: TodoManager,
    config: AgentConfig,
    tool_names: Optional[List[str]] = None,
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the init_strategic_todos node for phase alternation.

    This node initializes the job with predefined strategic todos,
    enabling the agent to use tools for planning rather than relying
    on a toolless LLM.

    Used when use_phase_alternation=True.
    """

    def init_strategic_todos(state: UniversalAgentState) -> Dict[str, Any]:
        """Load predefined strategic todos and instructions context."""
        job_id = state.get("job_id", "unknown")
        logger.info(f"[{job_id}] Initializing strategic todos for phase alternation")

        # Read task brief for context
        try:
            task_brief = workspace.read_file("task_brief.md")
        except FileNotFoundError:
            task_brief = ""

        # Read instructions for context (backward compat — removed in Phase 0)
        try:
            instructions = workspace.read_file("instructions.md")
        except FileNotFoundError:
            instructions = ""
            logger.warning(f"[{job_id}] instructions.md not found")

        # Load predefined strategic todos from config template
        strategic_todos = get_initial_strategic_todos(config, tool_names=tool_names)
        todo_list = [todo.to_dict() for todo in strategic_todos]
        todo_manager.set_todos_from_list(todo_list)

        # Initialize phase state on TodoManager for tool access
        todo_manager.is_strategic_phase = True
        todo_manager.phase_number = state.get("phase_number", 1)

        logger.info(
            f"[{job_id}] Loaded {len(strategic_todos)} predefined strategic todos"
        )

        # Audit initialization
        auditor = get_archiver()
        if auditor:
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="initialize",
                node_name="init_strategic_todos",
                iteration=0,
                data={
                    "phase_alternation": True,
                    "strategic_todos": len(strategic_todos),
                    "task_brief_length": len(task_brief),
                    "instructions_length": len(instructions),
                },
                metadata=state.get("metadata"),
                phase="strategic",
                phase_number=0,
            )

        # Compose initial HumanMessage: task brief + instructions
        content_parts = []
        if task_brief:
            content_parts.append(task_brief)
        if instructions:
            content_parts.append(f"## Task Instructions\n\n{instructions}")
        content_parts.append(
            "You are starting in strategic mode. Work through the predefined todos "
            "to understand the task, create a plan, and prepare todos for execution.\n\n"
            "Your task brief is saved to `task_brief.md` in your workspace for reference."
        )
        message = HumanMessage(content="\n\n".join(content_parts))

        return {
            "messages": [message],
            "initialized": True,
            "is_strategic_phase": True,
            "phase_number": 0,
            "phase_complete": False,
            "goal_achieved": False,
        }

    return init_strategic_todos


# =============================================================================
# EXECUTE PHASE NODES (ReAct Loop)
# =============================================================================


def create_execute_node(
    strategic_llm_with_tools: BaseChatModel,
    tactical_llm_with_tools: BaseChatModel,
    todo_manager: TodoManager,
    memory_manager: MemoryManager,
    workspace_manager: WorkspaceManager,
    config: AgentConfig,
    context_mgr: ContextManager,
    retry_manager: ToolRetryManager,
    auxiliary_llm,
    summarization_prompt: str,
    memory_extraction_prompt: str = "",
    memory_assembler_prompt: str = "",
    tool_context: Optional[ToolContext] = None,
    tool_names: Optional[List[str]] = None,
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the execute node with phase-specific LLM selection.

    This is the main ReAct execution node that processes todos.
    Selects strategic or tactical LLM based on current phase.

    Args:
        strategic_llm_with_tools: LLM for strategic (planning) phases
        tactical_llm_with_tools: LLM for tactical (execution) phases
        todo_manager: TodoManager for task tracking
        memory_manager: MemoryManager for workspace.md access
        workspace_manager: WorkspaceManager for file operations
        config: Agent configuration
        context_mgr: ContextManager for context window management
        retry_manager: ToolRetryManager for LLM call retry logic
        auxiliary_llm: AuxiliaryLLM instance for summarization
        summarization_prompt: Prompt template for summarization
        memory_extraction_prompt: Prompt for memory extraction task
        memory_assembler_prompt: Prompt for memory assembler task
        tool_names: List of loaded tool names for system prompt conditionals
    """

    # Extract tool schemas from bound LLMs once at creation time for archiving
    def _extract_tool_schemas(bound_llm: BaseChatModel) -> Optional[List[Dict[str, Any]]]:
        """Extract OpenAI-format tool schemas from a bound LLM."""
        if hasattr(bound_llm, 'kwargs'):
            return bound_llm.kwargs.get('tools')
        return None

    strategic_tool_schemas = _extract_tool_schemas(strategic_llm_with_tools)
    tactical_tool_schemas = _extract_tool_schemas(tactical_llm_with_tools)

    # Extract model kwargs (temperature, etc.) for archiving
    def _extract_model_kwargs(bound_llm: BaseChatModel) -> Dict[str, Any]:
        """Extract model configuration from LLM."""
        kwargs: Dict[str, Any] = {}
        for attr in ('temperature', 'model_name', 'max_tokens'):
            if hasattr(bound_llm, attr):
                val = getattr(bound_llm, attr)
                if val is not None:
                    kwargs[attr] = val
        # For bound LLMs, check the underlying LLM too
        if hasattr(bound_llm, 'bound'):
            for attr in ('temperature', 'model_name', 'max_tokens'):
                if hasattr(bound_llm.bound, attr):
                    val = getattr(bound_llm.bound, attr)
                    if val is not None:
                        kwargs[attr] = val
        return kwargs

    strategic_model_kwargs = _extract_model_kwargs(strategic_llm_with_tools)
    tactical_model_kwargs = _extract_model_kwargs(tactical_llm_with_tools)

    # Track consecutive tool_use_failed errors (mutable container for closure access)
    _tool_use_failed_streak = [0]
    # Track consecutive response degeneration events
    _degeneration_streak = [0]

    async def execute(state: UniversalAgentState) -> Dict[str, Any]:
        """Execute current todo using ReAct pattern."""
        job_id = state.get("job_id", "unknown")
        iteration = state.get("iteration", 0)
        messages = state.get("messages", [])
        is_strategic = state.get("is_strategic_phase", True)

        # Select LLM based on current phase
        llm_with_tools = strategic_llm_with_tools if is_strategic else tactical_llm_with_tools

        # Update tool context for phase-aware behavior (e.g., multimodal override)
        if tool_context is not None:
            tool_context.set_current_phase("strategic" if is_strategic else "tactical")

        logger.debug(f"[{job_id}] Execute iteration {iteration}")

        # Debug: log message types in state
        msg_types = {}
        for msg in messages:
            msg_type = type(msg).__name__
            msg_types[msg_type] = msg_types.get(msg_type, 0) + 1
        logger.debug(f"[{job_id}] State messages: {len(messages)} total, types: {msg_types}")

        # Build messages for LLM
        prepared_messages = []

        # Get phase-aware system prompt (workspace.md and todos are injected as transient messages below)
        phase_number = state.get("phase_number", 0)
        phase_name = "strategic" if is_strategic else "tactical"
        phase_llm_config = config.llm.get_phase_config(phase_name)
        full_system = get_phase_system_prompt(
            config=config,
            is_strategic=is_strategic,
            phase_number=phase_number,
            model=phase_llm_config.model,
            tool_names=tool_names,
        )
        context_mgr.set_current_phase(phase_name)
        logger.debug(
            f"[{job_id}] Using {phase_name} LLM and prompt for phase {phase_number}"
        )
        prepared_messages.append(SystemMessage(content=full_system))

        # Estimate transient injection overhead (system prompt + todos + memory + knowledge)
        # so compaction thresholds account for messages that will be added AFTER compaction
        injection_overhead_tokens = context_mgr.get_token_count([prepared_messages[0]])  # system prompt
        injection_overhead_tokens += len(todo_manager.format_for_injection()) // 4  # approximate

        # Add memory injection budget overhead
        recall_store = tool_context.recall_store if tool_context else None
        if recall_store:
            injection_overhead_tokens += config.memory.budget_tokens

        # Add knowledge injection budget overhead (~2500 tokens for 5 notes)
        if tool_context and tool_context.has_knowledge() and tool_context.project_id:
            injection_overhead_tokens += 2500

        # Temporarily lower compaction thresholds to account for injection overhead
        # Floor at 50% of original to avoid over-triggering
        original_compaction_threshold = context_mgr.config.compaction_threshold_tokens
        original_summarization_threshold = context_mgr.config.summarization_threshold_tokens
        context_mgr.config.compaction_threshold_tokens = max(
            original_compaction_threshold // 2,
            original_compaction_threshold - injection_overhead_tokens,
        )
        context_mgr.config.summarization_threshold_tokens = max(
            original_summarization_threshold // 2,
            original_summarization_threshold - injection_overhead_tokens,
        )

        # Ensure context is within limits before LLM call
        original_message_count = len(messages)
        summaries_count_before = len(context_mgr._state.summaries) if hasattr(context_mgr, '_state') else 0
        try:
            messages = await context_mgr.ensure_within_limits(
                messages,
                auxiliary_llm,
                summarization_prompt,
                max_summary_length=config.context_management.max_summary_length,
            )
        finally:
            # Restore original thresholds
            context_mgr.config.compaction_threshold_tokens = original_compaction_threshold
            context_mgr.config.summarization_threshold_tokens = original_summarization_threshold
        # Separate RemoveMessage markers from actual messages
        # RemoveMessage markers must NOT be sent to LLM - they're only for state update
        remove_markers = [m for m in messages if isinstance(m, RemoveMessage)]
        messages = [m for m in messages if not isinstance(m, RemoveMessage)]
        context_was_compacted = len(remove_markers) > 0
        if context_was_compacted:
            logger.info(
                f"[{job_id}] Context compacted in execute: {original_message_count} -> {len(messages)} messages "
                f"(removing {len(remove_markers)} old messages)"
            )

        # Memory Light: store compaction summary as free-source memory
        if recall_store and context_was_compacted:
            summaries_count_after = len(context_mgr._state.summaries) if hasattr(context_mgr, '_state') else 0
            if summaries_count_after > summaries_count_before:
                try:
                    await recall_store.store(
                        content=context_mgr._state.summaries[-1][:2000],
                        summary="Context compaction summary",
                        importance=0.6,
                        source="compaction",
                        memory_type="factual",
                        source_phase=state.get("phase_number", 0),
                    )
                except Exception as e:
                    logger.warning(f"[{job_id}] Failed to store compaction memory: {e}")

        # Always clear old tool results, keep last 10
        messages = context_mgr.clear_old_tool_results(messages)

        # Sanitize message history to remove orphaned ToolMessages
        # (can occur from improper context compaction or checkpoint corruption)
        messages = sanitize_message_history(messages)

        # Add full conversation history in specific order:
        # 1. Summary SystemMessages first (context from before compaction)
        # 1.5. Todo injection (full todo list as transient HumanMessage)
        # 2. Workspace injection (fake tool call - current workspace state)
        # 3. Rest of conversation (excluding regular SystemMessages)

        # Step 1: Add summaries first
        for msg in messages:
            if isinstance(msg, SystemMessage):
                if "[Summary of prior work]" in msg.content:
                    prepared_messages.append(msg)

        # Helper: inject all transient messages (todos, memories, knowledge, instruction files)
        # Used both in normal path and safety rebuild to avoid code duplication
        from src.core.workspace_injection import (
            create_todos_human_message,
            create_instruction_tool_messages,
        )

        todos_injection_content = todo_manager.format_for_injection()

        # Memory Light: decrement TTLs then retrieve relevant memories for injection
        _memory_block = [""]  # mutable container for closure access
        if recall_store:
            try:
                await recall_store.decrement_ttl()
            except Exception as e:
                logger.warning(f"[{job_id}] TTL decrement failed (non-fatal): {e}")

            try:
                # Build retrieval context from current todo + phase info
                pending_todos = todo_manager.list_pending()
                context_parts = []
                if pending_todos:
                    context_parts.append(pending_todos[0].content)
                context_parts.append(f"phase {phase_number} {'strategic' if is_strategic else 'tactical'}")
                context_text = " ".join(context_parts)

                memories = await recall_store.retrieve(context_text)
                if memories:
                    from src.services.recall_store import RecallStore as _RS
                    _memory_block[0] = _RS.assemble_memory_block(memories)
                    logger.debug(
                        f"[{job_id}] Memory injection: {len(memories)} memories retrieved"
                    )
                    # Audit memory injection
                    inject_auditor = get_archiver()
                    if inject_auditor:
                        inject_auditor.audit_step(
                            job_id=job_id,
                            agent_type=config.agent_id,
                            step_type="memory_inject",
                            node_name="execute",
                            iteration=iteration,
                            data={
                                "count": len(memories),
                                "total_tokens": sum(m.token_count for m in memories),
                            },
                            metadata=state.get("metadata"),
                            phase="strategic" if is_strategic else "tactical",
                            phase_number=phase_number,
                        )
            except Exception as e:
                logger.warning(f"[{job_id}] Memory retrieval failed (non-fatal): {e}")

        # Knowledge Base: retrieve relevant project knowledge for injection
        _knowledge_block = [""]  # mutable container for closure access
        knowledge_store = tool_context.knowledge_store if tool_context and tool_context.has_knowledge() else None
        if knowledge_store and tool_context.project_id:
            try:
                import uuid as _uuid
                project_uuid = _uuid.UUID(tool_context.project_id) if isinstance(tool_context.project_id, str) else tool_context.project_id

                # Build retrieval context from current todo + phase info
                pending_todos = todo_manager.list_pending()
                kb_context_parts = []
                if pending_todos:
                    kb_context_parts.append(pending_todos[0].content)
                kb_context_parts.append(f"phase {phase_number} {'strategic' if is_strategic else 'tactical'}")
                kb_context_text = " ".join(kb_context_parts)

                from src.services.knowledge_store import KnowledgeStore as _KS
                kb_notes = await knowledge_store.hybrid_search(
                    project_id=project_uuid,
                    query=kb_context_text,
                    match_count=5,
                )
                if kb_notes:
                    _knowledge_block[0] = _KS.assemble_knowledge_block(kb_notes)
                    logger.debug(
                        f"[{job_id}] Knowledge injection: {len(kb_notes)} notes retrieved"
                    )
            except Exception as e:
                logger.warning(f"[{job_id}] Knowledge retrieval failed (non-fatal): {e}")

        def _inject_transient_messages(target_messages: list) -> None:
            """Append transient injection messages (todos, memories, knowledge, instruction files)."""
            # Todo list as transient HumanMessage
            target_messages.append(create_todos_human_message(todos_injection_content))

            # Memory Light: inject recalled memories
            if _memory_block[0]:
                from src.core.memory_injection import create_memory_injection_messages
                mem_ai, mem_tool = create_memory_injection_messages(_memory_block[0])
                target_messages.append(mem_ai)
                target_messages.append(mem_tool)

            # Knowledge Base: inject relevant project knowledge after memories
            if _knowledge_block[0]:
                from src.core.knowledge_injection import create_knowledge_injection_messages
                kb_ai, kb_tool = create_knowledge_injection_messages(_knowledge_block[0])
                target_messages.append(kb_ai)
                target_messages.append(kb_tool)

            # Phase-triggered instruction files (active injection)
            if tool_context and hasattr(tool_context, 'get_phase_instruction_files'):
                _phase_name = "strategic" if is_strategic else "tactical"
                phase_entries = tool_context.get_phase_instruction_files(_phase_name)
                if phase_entries:
                    for entry in phase_entries:
                        try:
                            instr_content = workspace_manager.read_file(entry.file)
                            instr_ai, instr_tool = create_instruction_tool_messages(
                                entry.file, instr_content
                            )
                            target_messages.append(instr_ai)
                            target_messages.append(instr_tool)
                            logger.debug(
                                f"[{job_id}] Injected instruction file: {entry.file}"
                            )
                        except FileNotFoundError:
                            logger.warning(
                                f"[{job_id}] Phase instruction file not found: {entry.file}"
                            )

        # Inject transient messages (todos, workspace.md, instruction files)
        _inject_transient_messages(prepared_messages)

        # Step 3: Add rest of conversation (excluding all SystemMessages)
        for msg in messages:
            if not isinstance(msg, SystemMessage):
                prepared_messages.append(msg)

        # Todo reminders are injected post-LLM-response (see below) so they
        # persist in conversation history and survive context compaction.

        # LAYER 1 SAFETY CHECK: Ensure we don't exceed model context limit
        # This catches bad configs and edge cases that slip through normal compaction
        total_tokens = context_mgr.get_token_count(prepared_messages)
        model_max = phase_llm_config.model_max_context_tokens or config.limits.model_max_context_tokens

        if total_tokens > model_max:
            logger.warning(
                f"[{job_id}] Pre-request safety triggered: {total_tokens} tokens exceeds "
                f"{model_max} limit. Forcing summarization."
            )

            # Force summarization on conversation messages (not system prompt)
            messages = await context_mgr.ensure_within_limits(
                messages,
                auxiliary_llm,
                summarization_prompt,
                max_summary_length=config.context_management.max_summary_length,
                force=True,
            )

            # Separate RemoveMessage markers from actual messages
            safety_remove_markers = [m for m in messages if isinstance(m, RemoveMessage)]
            messages = [m for m in messages if not isinstance(m, RemoveMessage)]

            # Rebuild prepared_messages with compacted history
            # Keep system prompt, re-inject ALL transient messages, replace conversation
            system_msg = prepared_messages[0] if prepared_messages else None
            prepared_messages = []
            if system_msg and isinstance(system_msg, SystemMessage):
                prepared_messages.append(system_msg)

            # Add compacted conversation (including summary SystemMessages)
            for msg in messages:
                if isinstance(msg, SystemMessage):
                    if "[Summary of prior work]" in msg.content:
                        prepared_messages.append(msg)

            # Re-inject ALL transient messages (todos + workspace.md + instruction files)
            _inject_transient_messages(prepared_messages)
            logger.debug(
                f"[{job_id}] Re-injected transient messages after safety compaction"
            )

            for msg in messages:
                if not isinstance(msg, SystemMessage):
                    prepared_messages.append(msg)

            # Merge remove markers if compaction occurred
            if safety_remove_markers:
                remove_markers = safety_remove_markers + remove_markers
                context_was_compacted = True

            # Re-check - if still over limit, something is very wrong
            total_tokens = context_mgr.get_token_count(prepared_messages)
            if total_tokens > model_max:
                raise RuntimeError(
                    f"[{job_id}] Context still at {total_tokens} tokens after forced summarization. "
                    f"Model limit is {model_max}. System prompt may be too large "
                    f"({context_mgr.get_token_count([prepared_messages[0]]) if prepared_messages else 0} tokens)."
                )

            logger.info(
                f"[{job_id}] Safety compaction complete: now at {total_tokens} tokens"
            )

        # Audit LLM call (will be updated with response via update_llm_response)
        auditor = get_archiver()
        llm_audit_id = None
        phase_str = "strategic" if is_strategic else "tactical"
        phase_model = config.llm.get_phase_config(phase_str).model
        model_kwargs = strategic_model_kwargs if is_strategic else tactical_model_kwargs
        if auditor:
            llm_audit_id = auditor.audit_llm_call(
                job_id=job_id,
                agent_type=config.agent_id,
                iteration=iteration,
                model=phase_model,
                input_message_count=len(prepared_messages),
                state_message_count=len(messages),
                metadata=state.get("metadata"),
                phase=phase_str,
                phase_number=phase_number,
            )

        # Retry loop for LLM call with exponential backoff
        attempt = 0

        # Total wall-clock timeout for LLM calls. httpx read timeout only fires
        # when no bytes arrive within the window, but vLLM sends HTTP headers
        # immediately and then blocks during inference — so the read timeout
        # never triggers. asyncio.wait_for enforces a hard cap on total time.
        import asyncio
        llm_timeout = phase_llm_config.timeout or 600.0

        while True:
            try:
                start_time = time.time()
                response = await asyncio.wait_for(
                    llm_with_tools.ainvoke(prepared_messages),
                    timeout=llm_timeout,
                )
                latency_ms = int((time.time() - start_time) * 1000)

                # Reset tool_use_failed streak on successful response
                _tool_use_failed_streak[0] = 0

                tool_calls_count = len(response.tool_calls) if hasattr(response, 'tool_calls') and response.tool_calls else 0
                content_str = response.content
                if isinstance(content_str, list):
                    content_str = " ".join(
                        b.get("text", "") if isinstance(b, dict) else str(b) for b in content_str
                    ).strip()
                content_len = len(content_str) if isinstance(content_str, str) else 0
                logger.info(f"[{job_id}] LLM response: {content_len} chars, {tool_calls_count} tool calls")

                # --- Response degeneration validation ---
                rv_config = config.limits.response_validation
                if rv_config.enabled and isinstance(content_str, str) and content_str:
                    tool_calls_list = response.tool_calls if hasattr(response, 'tool_calls') and response.tool_calls else None
                    validation = validate_response(
                        content_str,
                        tool_calls_list,
                        max_content_length=rv_config.max_content_length,
                        max_tag_repetitions=rv_config.max_tag_repetitions,
                        max_token_repetitions=rv_config.max_token_repetitions,
                        max_line_repetitions=rv_config.max_line_repetitions,
                    )

                    if validation.is_degenerate:
                        _degeneration_streak[0] += 1
                        streak = _degeneration_streak[0]
                        pattern_names = [p.name for p in validation.matched_patterns]
                        pattern_details = "; ".join(p.description for p in validation.matched_patterns)

                        if streak <= 3:
                            logger.warning(
                                f"[{job_id}] Response degeneration detected (streak {streak}/3): "
                                f"{pattern_details}"
                            )

                            ai_summary = AIMessage(content=(
                                "My previous response was degenerate — it contained repetitive or "
                                "malformed output that cannot be used. Detected patterns: "
                                f"{', '.join(pattern_names)}. I need to retry with a shorter, "
                                "more focused response."
                            ))
                            human_feedback = HumanMessage(content=(
                                "Your last response was detected as degenerate and has been discarded. "
                                f"Issues found: {pattern_details}\n\n"
                                "Please retry your action. Keep your response concise and focused. "
                                "If you were trying to call a tool, make the call with smaller arguments. "
                                "Do NOT repeat the same output."
                            ))

                            if auditor:
                                auditor.audit_step(
                                    job_id=job_id,
                                    agent_type=config.agent_id,
                                    step_type="warning",
                                    node_name="execute",
                                    iteration=iteration,
                                    data={
                                        "error": {
                                            "type": "response_degeneration",
                                            "message": pattern_details,
                                            "streak": streak,
                                            "patterns": pattern_names,
                                            "content_length": content_len,
                                            "content_preview": (validation.truncated_content or "")[:500],
                                        }
                                    },
                                    metadata=state.get("metadata"),
                                    phase=phase_str,
                                    phase_number=phase_number,
                                )

                            if context_was_compacted:
                                result_messages = remove_markers + messages + [ai_summary, human_feedback]
                            else:
                                result_messages = [ai_summary, human_feedback]

                            return {
                                "messages": result_messages,
                                "iteration": iteration + 1,
                                "error": None,
                            }

                        # Streak > 3: fall through to error
                        logger.error(
                            f"[{job_id}] Response degeneration streak exceeded (streak {streak}): "
                            f"{pattern_details}"
                        )
                        return {
                            "error": {
                                "message": f"Persistent response degeneration: {pattern_details}",
                                "type": "response_degeneration",
                                "recoverable": False,
                                "streak": streak,
                                "patterns": pattern_names,
                            },
                            "iteration": iteration + 1,
                        }

                    elif validation.has_warnings:
                        logger.warning(
                            f"[{job_id}] Response validation warnings: "
                            + "; ".join(p.description for p in validation.matched_patterns)
                        )
                        if auditor:
                            auditor.audit_step(
                                job_id=job_id,
                                agent_type=config.agent_id,
                                step_type="warning",
                                node_name="execute",
                                iteration=iteration,
                                data={
                                    "warning": {
                                        "type": "response_validation_warning",
                                        "patterns": [p.name for p in validation.matched_patterns],
                                        "details": [p.description for p in validation.matched_patterns],
                                    }
                                },
                                metadata=state.get("metadata"),
                                phase=phase_str,
                                phase_number=phase_number,
                            )
                    else:
                        # Clean response — reset degeneration streak
                        _degeneration_streak[0] = 0

                # Archive full LLM request/response to llm_requests collection
                request_id = None
                if auditor:
                    current_tool_schemas = strategic_tool_schemas if is_strategic else tactical_tool_schemas
                    request_id = auditor.archive(
                        job_id=job_id,
                        agent_type=config.agent_id,
                        messages=prepared_messages,
                        response=response,
                        model=phase_model,
                        latency_ms=latency_ms,
                        iteration=iteration,
                        metadata=state.get("metadata"),
                        phase=phase_str,
                        phase_number=phase_number,
                        tool_schemas=current_tool_schemas,
                        model_kwargs=model_kwargs,
                    )

                    # Build tool calls preview
                    tool_calls_preview = []
                    if hasattr(response, 'tool_calls') and response.tool_calls:
                        for tc in response.tool_calls:
                            tool_calls_preview.append({
                                "name": tc.get("name", "unknown"),
                                "call_id": tc.get("id", ""),
                            })

                    # Update the LLM audit document with response data
                    if llm_audit_id:
                        # Normalize content: Anthropic returns list of content blocks,
                        # OpenAI returns a plain string
                        content = response.content
                        if isinstance(content, list):
                            content = " ".join(
                                block.get("text", "") if isinstance(block, dict) else str(block)
                                for block in content
                            ).strip()
                        content_str = content if isinstance(content, str) else str(content or "")

                        auditor.update_llm_response(
                            audit_doc_id=llm_audit_id,
                            request_id=request_id,
                            response_preview=content_str[:500],
                            tool_calls=tool_calls_preview,
                            output_chars=len(content_str),
                            latency_ms=latency_ms,
                        )

                # Post-response todo reminder: if the model responded without tool calls
                # and there are pending todos, append a reminder to state so it persists
                # in conversation history. The model will see it on the next execute loop.
                injected_reminder = None
                if not (hasattr(response, 'tool_calls') and response.tool_calls):
                    remaining = todo_manager.list_pending()
                    if remaining:
                        first = remaining[0]
                        todo_lines = "\n".join(
                            f"  - {t.id}: {t.content[:80]}{'...' if len(t.content) > 80 else ''}"
                            for t in remaining
                        )
                        injected_reminder = HumanMessage(content=(
                            f'Action required: call `todo_complete(todo_id="{first.id}")` to mark your current task done.\n\n'
                            "If you already finished the work for this todo, that's perfectly fine — "
                            "just call `todo_complete` now to record it. You don't need to redo anything.\n\n"
                            f"Pending todos ({len(remaining)}):\n"
                            f"{todo_lines}\n\n"
                            "Do NOT respond with text. Your next action must be a tool call."
                        ))

                # Memory Light: extract + assemble memories via AuxiliaryLLM (async, non-blocking)
                new_turn_count = state.get("turn_count", 0) + 1
                extraction_triggered = False
                assembly_triggered = False
                recall_store_exec = tool_context.recall_store if tool_context else None
                if (recall_store_exec
                    and config.auxiliary.enabled
                    and config.auxiliary.tasks.get("extract_memories", None)
                    and config.auxiliary.tasks["extract_memories"].enabled):
                    from src.services.auxiliary import (
                        _should_extract_memories,
                        extract_and_store_memories,
                    )
                    last_observed = state.get("last_observed_turn", 0)
                    if _should_extract_memories(
                        new_turn_count,
                        config.memory.observer_interval,
                        last_observed,
                    ):
                        import asyncio
                        extraction_triggered = True
                        asyncio.create_task(
                            extract_and_store_memories(
                                auxiliary_llm=auxiliary_llm,
                                recall_store=recall_store_exec,
                                messages=messages,
                                memory_extraction_prompt=memory_extraction_prompt,
                                phase=phase_number,
                                source_turn_start=last_observed,
                                source_turn_end=new_turn_count,
                            )
                        )

                # Memory assembler: review conversation and adjust memory TTLs
                if (recall_store_exec
                    and config.auxiliary.enabled
                    and config.auxiliary.tasks.get("assemble_memories", None)
                    and config.auxiliary.tasks["assemble_memories"].enabled
                    and memory_assembler_prompt):
                    from src.services.auxiliary import (
                        _should_assemble_memories,
                        assemble_memories,
                    )
                    last_assembled = state.get("last_assembled_turn", 0)
                    if _should_assemble_memories(
                        new_turn_count,
                        config.memory.assembler_interval,
                        last_assembled,
                    ):
                        import asyncio
                        assembly_triggered = True
                        asyncio.create_task(
                            assemble_memories(
                                auxiliary_llm=auxiliary_llm,
                                recall_store=recall_store_exec,
                                messages=messages,
                                current_injection_text=_memory_block[0],
                                memory_assembler_prompt=memory_assembler_prompt,
                            )
                        )

                # Build result dict
                result_update = {
                    "iteration": iteration + 1,
                    "turn_count": new_turn_count,
                    "error": None,
                }
                if extraction_triggered:
                    result_update["last_observed_turn"] = new_turn_count
                if assembly_triggered:
                    result_update["last_assembled_turn"] = new_turn_count

                # Return compacted messages + response if compaction occurred,
                # otherwise just append the response (add_messages reducer handles this)
                if context_was_compacted:
                    # Include RemoveMessage markers so state reducer removes old messages
                    result_messages = remove_markers + messages + [response]
                    if injected_reminder:
                        result_messages.append(injected_reminder)
                    return {"messages": result_messages, **result_update}
                result_messages = [response]
                if injected_reminder:
                    result_messages.append(injected_reminder)
                return {"messages": result_messages, **result_update}

            except ContextOverflowError as e:
                # Layer 0 (HTTP layer) caught context overflow
                logger.warning(
                    f"[{job_id}] HTTP layer context overflow: "
                    f"{e.token_count:,} tokens exceeds limit of {e.limit:,}"
                )

                # Try emergency compaction once (on first attempt only)
                if attempt == 0:
                    logger.info(f"[{job_id}] Attempting emergency compaction after HTTP overflow")

                    # Force aggressive compaction
                    messages = await context_mgr.ensure_within_limits(
                        messages,
                        auxiliary_llm,
                        summarization_prompt,
                        max_summary_length=config.context_management.max_summary_length,
                        force=True,
                    )

                    # Separate RemoveMessage markers
                    emergency_remove_markers = [m for m in messages if isinstance(m, RemoveMessage)]
                    messages = [m for m in messages if not isinstance(m, RemoveMessage)]

                    # Rebuild prepared_messages with compacted history
                    system_msg = prepared_messages[0] if prepared_messages and isinstance(prepared_messages[0], SystemMessage) else None
                    prepared_messages = []
                    if system_msg:
                        prepared_messages.append(system_msg)

                    for msg in messages:
                        if isinstance(msg, SystemMessage):
                            if "[Summary of prior work]" in msg.content:
                                prepared_messages.append(msg)
                        else:
                            prepared_messages.append(msg)

                    # Merge remove markers
                    if emergency_remove_markers:
                        remove_markers = emergency_remove_markers + remove_markers
                        context_was_compacted = True

                    logger.info(
                        f"[{job_id}] Emergency compaction complete, "
                        f"retrying with {len(prepared_messages)} messages"
                    )
                    attempt += 1
                    continue

                # Compaction didn't help - this is unrecoverable
                logger.error(
                    f"[{job_id}] Context overflow persists after compaction: "
                    f"{e.token_count:,} tokens (limit: {e.limit:,})"
                )

                # Audit error
                if auditor:
                    auditor.audit_step(
                        job_id=job_id,
                        agent_type=config.agent_id,
                        step_type="error",
                        node_name="execute",
                        iteration=iteration,
                        data={
                            "error": {
                                "type": "context_overflow",
                                "message": str(e),
                                "token_count": e.token_count,
                                "limit": e.limit,
                                "recoverable": False,
                            }
                        },
                        metadata=state.get("metadata"),
                        phase=phase_str,
                        phase_number=phase_number,
                    )

                return {
                    "error": {
                        "message": str(e),
                        "type": "context_overflow",
                        "recoverable": False,
                        "token_count": e.token_count,
                        "limit": e.limit,
                    },
                    "iteration": iteration + 1,
                }

            except Exception as e:
                # Check for Groq tool_use_failed before standard retry logic
                failed_generation = _extract_tool_use_failed(e)
                if failed_generation is not None:
                    _tool_use_failed_streak[0] += 1
                    streak = _tool_use_failed_streak[0]

                    if streak <= 3:
                        logger.warning(
                            f"[{job_id}] Groq tool_use_failed (streak {streak}/3): "
                            f"output exceeded max completion tokens, giving model feedback"
                        )

                        # Build feedback for the model
                        feedback_text = _build_tool_use_failed_feedback(failed_generation)

                        # Create AIMessage (model's failed turn summary) + HumanMessage (guidance)
                        ai_summary = AIMessage(content=(
                            "My previous attempt to call a tool failed because the output "
                            "exceeded the maximum completion token limit. The tool call was "
                            "truncated and never executed. I need to retry with smaller chunks."
                        ))
                        human_feedback = HumanMessage(content=feedback_text)

                        # Audit as warning
                        if auditor:
                            preview = failed_generation[:500] if failed_generation else "(empty)"
                            auditor.audit_step(
                                job_id=job_id,
                                agent_type=config.agent_id,
                                step_type="warning",
                                node_name="execute",
                                iteration=iteration,
                                data={
                                    "error": {
                                        "type": "tool_use_failed",
                                        "message": "Groq output exceeded max completion tokens",
                                        "streak": streak,
                                        "failed_generation_preview": preview,
                                        "failed_generation_length": len(failed_generation),
                                    }
                                },
                                metadata=state.get("metadata"),
                                phase=phase_str,
                                phase_number=phase_number,
                            )

                        # Return feedback messages — graph continues normally
                        # Route: execute → check_todos (no tool_calls) → pending todos → execute
                        if context_was_compacted:
                            result_messages = remove_markers + messages + [ai_summary, human_feedback]
                        else:
                            result_messages = [ai_summary, human_feedback]

                        return {
                            "messages": result_messages,
                            "iteration": iteration + 1,
                            "error": None,
                        }

                    # Streak > 3: fall through to standard retry exhaustion
                    logger.error(
                        f"[{job_id}] Groq tool_use_failed streak exceeded (streak {streak}): "
                        f"model cannot produce output within token limits"
                    )

                retry_manager.record_failure("llm_invoke")

                if retry_manager.should_retry("llm_invoke", attempt):
                    delay = retry_manager.get_retry_delay(attempt)

                    # For rate limit errors, respect the retry-after header
                    rate_limit_delay = _extract_rate_limit_delay(e)
                    if rate_limit_delay is not None:
                        delay = max(delay, rate_limit_delay)
                        logger.warning(
                            f"[{job_id}] Rate limit hit (attempt {attempt + 1}/{retry_manager.max_retries}), "
                            f"waiting {delay:.0f}s before retry: {e}"
                        )
                    else:
                        logger.warning(
                            f"[{job_id}] LLM error (attempt {attempt + 1}/{retry_manager.max_retries}), "
                            f"retrying in {delay:.1f}s: {e}"
                        )

                    retry_manager.record_retry()
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue

                # Max retries exceeded
                logger.error(f"[{job_id}] LLM error after {attempt + 1} attempts: {e}")

                # Audit error
                if auditor:
                    auditor.audit_step(
                        job_id=job_id,
                        agent_type=config.agent_id,
                        step_type="error",
                        node_name="execute",
                        iteration=iteration,
                        data={
                            "error": {
                                "type": "llm_error",
                                "message": str(e)[:500],
                                "recoverable": True,
                                "attempts": attempt + 1,
                            }
                        },
                        metadata=state.get("metadata"),
                        phase=phase_str,
                        phase_number=phase_number,
                    )

                return {
                    "error": {
                        "message": str(e),
                        "type": "llm_error",
                        "recoverable": True,
                    },
                    "iteration": iteration + 1,
                }

    return execute


def create_check_todos_node(
    todo_manager: TodoManager,
    config: AgentConfig,
    tool_names: Optional[List[str]] = None,
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the check_todos node.

    This node checks if all todos are complete.
    """

    def check_todos(state: UniversalAgentState) -> Dict[str, Any]:
        """Check if all todos are complete."""
        job_id = state.get("job_id", "unknown")

        # Validate todos exist before checking completion
        todos = todo_manager.list_all()
        if not todos:
            # Check if we're in tactical phase with no todos - this is a stuck state
            # (can happen after resume if todo state wasn't persisted)
            is_strategic = state.get("is_strategic_phase", True)
            if not is_strategic:
                logger.warning(
                    f"[{job_id}] No todos in tactical phase - forcing phase complete to recover"
                )
                return {"phase_complete": True}

            # Strategic phase with no todos (likely after resume)
            # Reload appropriate predefined todos to recover
            phase_number = state.get("phase_number", 0)
            if phase_number == 0:
                # Initial strategic phase
                strategic_todos = get_initial_strategic_todos(config, tool_names=tool_names)
            else:
                # Transition strategic phase (between tactical phases)
                strategic_todos = get_transition_strategic_todos(config, tool_names=tool_names)

            if strategic_todos:
                todo_list = [todo.to_dict() for todo in strategic_todos]
                todo_manager.set_todos_from_list(todo_list)
                logger.warning(
                    f"[{job_id}] Reloaded {len(strategic_todos)} strategic todos after resume (phase {phase_number})"
                )
                return {"phase_complete": False}  # Continue with reloaded todos

            logger.warning(f"[{job_id}] No todos loaded and no predefined todos available")
            return {"phase_complete": False}

        # Check todos
        all_complete = todo_manager.all_complete()
        todo_manager.log_state()

        # Export TodoManager state for checkpointing
        todo_state = todo_manager.export_state()

        if all_complete:
            logger.info(f"[{job_id}] All todos complete")
            return {
                "phase_complete": True,
                "todos": todo_state["todos"],
                "staged_todos": todo_state["staged_todos"],
                "todo_next_id": todo_state["next_id"],
            }

        return {
            "phase_complete": False,
            "todos": todo_state["todos"],
            "staged_todos": todo_state["staged_todos"],
            "todo_next_id": todo_state["next_id"],
        }

    return check_todos


def create_archive_phase_node(
    todo_manager: TodoManager,
    plan_manager: PlanManager,
    config: AgentConfig,
    context_mgr: ContextManager,
    auxiliary_llm,
    summarization_prompt: str,
    snapshot_manager: Optional[PhaseSnapshotManager] = None,
    recall_store=None,
    tool_context: Optional[ToolContext] = None,
    workspace_manager: Optional[WorkspaceManager] = None,
    memory_extraction_prompt: str = "",
    curation_prompt: str = "",
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the archive_phase node.

    This node archives completed todos and marks phase complete.
    Also performs context compaction if configured.
    Creates phase snapshots for recovery if snapshot_manager is provided.
    Runs inline knowledge curation if knowledge base is available.
    """

    async def archive_phase(state: UniversalAgentState) -> Dict[str, Any]:
        """Archive todos and mark phase complete in plan."""
        job_id = state.get("job_id", "unknown")
        iteration = state.get("iteration", 0)
        phase_number = state.get("phase_number", 0)
        is_strategic = state.get("is_strategic_phase", True)
        messages = state.get("messages", [])

        current_phase = plan_manager.get_current_phase()
        logger.info(f"[{job_id}] Archiving phase: {current_phase}")

        import asyncio

        # Memory Light: extract memories at phase boundary via AuxiliaryLLM (async, non-blocking)
        if (recall_store
            and config.auxiliary.enabled
            and config.auxiliary.tasks.get("extract_memories", None)
            and config.auxiliary.tasks["extract_memories"].enabled):
            from src.services.auxiliary import extract_and_store_memories
            asyncio.create_task(
                extract_and_store_memories(
                    auxiliary_llm=auxiliary_llm,
                    recall_store=recall_store,
                    messages=messages,
                    memory_extraction_prompt=memory_extraction_prompt,
                    phase=phase_number,
                )
            )

        # Create phase snapshot BEFORE any modifications
        # This captures the clean state at end of phase for recovery
        if snapshot_manager:
            try:
                # Get todo stats for snapshot metadata
                todos = todo_manager.list_all()
                todos_completed = sum(1 for t in todos if t.status == TodoStatus.COMPLETED)
                todos_total = len(todos)

                snapshot_manager.create_snapshot(
                    phase_number=phase_number,
                    iteration=iteration,
                    message_count=len(messages),
                    is_strategic_phase=is_strategic,
                    todos_completed=todos_completed,
                    todos_total=todos_total,
                )
            except Exception as e:
                logger.warning(f"[{job_id}] Failed to create phase snapshot: {e}")

        # Collect completed todo summaries BEFORE archiving (archive clears them)
        phase_str = "strategic" if is_strategic else "tactical"
        curation_todo_summaries = []
        if (tool_context and tool_context.has_knowledge()
                and config.extra.get("curator", {}).get("enabled", False)):
            for todo in todo_manager.list_all():
                if todo.status == TodoStatus.COMPLETED and todo.notes:
                    curation_todo_summaries.append(
                        f"- {todo.content}: {'; '.join(todo.notes)}"
                    )

        # Archive todos
        archive_path = todo_manager.archive(current_phase or "phase")

        # Mark phase complete in plan
        if current_phase:
            plan_manager.mark_phase_complete(current_phase)

        # Audit phase completion
        auditor = get_archiver()
        if auditor:
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="phase_complete",
                node_name="archive_phase",
                iteration=iteration,
                data={
                    "phase": {
                        "completed": current_phase,
                        "archive_path": str(archive_path) if archive_path else None,
                    }
                },
                metadata=state.get("metadata"),
                phase=phase_str,
                phase_number=phase_number,
            )

        # Inline curation via AuxiliaryLLM (async, non-blocking)
        if (tool_context and tool_context.has_knowledge()
                and config.extra.get("curator", {}).get("enabled", False)
                and config.auxiliary.enabled):
            try:
                from src.services.auxiliary import curate_and_store_knowledge
                try:
                    ws_md = workspace_manager.read_file("workspace.md") if workspace_manager else ""
                except (FileNotFoundError, ValueError):
                    ws_md = ""
                try:
                    plan_md_content = workspace_manager.read_file("plan.md") if workspace_manager else ""
                except (FileNotFoundError, ValueError):
                    plan_md_content = ""
                phase_context_parts = [f"Phase {phase_number} ({phase_str}) archived."]
                if archive_path:
                    phase_context_parts.append(f"Archive: {archive_path}")
                phase_context_parts.extend(curation_todo_summaries)
                curation_phase_data = "\n".join(phase_context_parts)
                asyncio.create_task(curate_and_store_knowledge(
                    auxiliary_llm=auxiliary_llm,
                    tool_context=tool_context,
                    phase_data=curation_phase_data,
                    workspace_md=ws_md or "",
                    plan_md=plan_md_content or "",
                    curation_prompt=curation_prompt,
                ))
            except Exception as e:
                logger.warning(f"[{job_id}] Inline curation failed (non-fatal): {e}")

        message = AIMessage(
            content=f"Phase complete. Archived todos to {archive_path}. Moving to next phase."
        )

        # Context compaction at phase boundary using unified method
        messages = state.get("messages", [])
        compacted_messages = None

        if config.context_management.compact_on_archive:
            # Force summarization when transitioning from strategic to tactical
            # This gives tactical phases a "fresh conversation" with just the plan summary
            force_summarize = is_strategic  # True when completing strategic phase

            compacted_messages = await context_mgr.ensure_within_limits(
                messages,
                auxiliary_llm,
                summarization_prompt,
                max_summary_length=config.context_management.max_summary_length,
                force=force_summarize,
            )

            # Check for RemoveMessage markers to detect if compaction occurred
            # (RemoveMessage count makes len() unreliable)
            remove_markers = [m for m in compacted_messages if isinstance(m, RemoveMessage)]
            if remove_markers:
                # Compaction occurred - separate markers from actual messages
                actual_messages = [m for m in compacted_messages if not isinstance(m, RemoveMessage)]
                reason = "strategic→tactical transition" if force_summarize else "threshold exceeded"
                logger.info(
                    f"[{job_id}] Compacted context ({reason}): "
                    f"{len(messages)} -> {len(actual_messages)} messages "
                    f"(removing {len(remove_markers)} old messages)"
                )
                # Reassemble: RemoveMessage markers first, then actual messages
                compacted_messages = remove_markers + actual_messages
            else:
                compacted_messages = None  # No compaction occurred

        # Return with compacted messages if compaction occurred
        if compacted_messages is not None:
            logger.debug(
                f"[{job_id}] archive_phase returning {len(compacted_messages)} compacted messages "
                f"({len(remove_markers)} RemoveMessage + {len(actual_messages)} actual) + 1 new message"
            )
            return {
                "messages": compacted_messages + [message],
            }

        logger.debug(f"[{job_id}] archive_phase returning 1 new message (no compaction)")
        return {
            "messages": [message],
        }

    return archive_phase


async def _process_queued_replies(
    job_id: str,
    workspace: "WorkspaceManager",
    postgres_db,
) -> int:
    """Consume queued async replies from job context and write to workspace.

    Called at tactical→strategic phase boundaries so replies appear in
    ``git_diff`` during the upcoming strategic review.

    Returns:
        Number of replies processed.
    """
    from datetime import datetime, timezone as tz

    # Read queued_replies from job context
    row = await postgres_db.fetchrow(
        "SELECT context FROM jobs WHERE id = $1::uuid", job_id,
    )
    if not row:
        return 0

    ctx = row.get("context") or {}
    if isinstance(ctx, str):
        ctx = json.loads(ctx)

    queued = ctx.get("queued_replies")
    if not queued:
        return 0

    # NOTE: queued_replies are cleared from DB context by the orchestrator
    # when the agent reports completion with consumed_reply_threads=True.
    # We only write the replies to workspace files here.

    # Write each reply as a workspace file
    for reply in queued:
        thread_id = reply.get("thread_id", "unknown")
        message = reply.get("message", "")
        timestamp = reply.get("timestamp", datetime.now(tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

        msg_dir = f"messages/{thread_id}"
        try:
            existing = workspace.list_directory(msg_dir)
            seq = len(existing) + 1
        except Exception:
            seq = 1

        msg_content = (
            f"---\n"
            f"from: user\n"
            f"to: agent\n"
            f"date: {timestamp}\n"
            f"subject: (async reply)\n"
            f"thread: {thread_id}\n"
            f"sequence: {seq}\n"
            f"status: unread\n"
            f"---\n\n"
            f"{message}\n"
        )
        workspace.write_file(f"{msg_dir}/{seq:03d}_received.md", msg_content)

    if queued and workspace.git_manager and workspace.git_manager.is_active:
        workspace.git_manager.commit(
            f"Received {len(queued)} queued reply(ies)"
        )

    logger.info(f"[{job_id}] Processed {len(queued)} queued async replies at phase boundary")
    return len(queued)


def create_handle_transition_node(
    workspace: WorkspaceManager,
    todo_manager: TodoManager,
    config: AgentConfig,
    min_todos: int = 5,
    max_todos: int = 20,
    postgres_db: Optional[Any] = None,
    tool_names: Optional[List[str]] = None,
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the handle_transition node.

    This node handles phase transitions between strategic and tactical modes.
    It validates todos.yaml for strategic->tactical transitions and archives
    todos for tactical->strategic transitions.

    Args:
        workspace: WorkspaceManager for file access
        todo_manager: TodoManager for todo operations
        config: Agent configuration
        min_todos: Minimum todos for strategic->tactical transition
        max_todos: Maximum todos for strategic->tactical transition
    """

    async def handle_transition(state: UniversalAgentState) -> Dict[str, Any]:
        """Handle phase transition based on current mode."""
        job_id = state.get("job_id", "unknown")
        iteration = state.get("iteration", 0)
        is_strategic = state.get("is_strategic_phase", True)

        logger.info(
            f"[{job_id}] Handling phase transition from "
            f"{'strategic' if is_strategic else 'tactical'} phase"
        )

        # Process queued async replies at tactical→strategic boundaries.
        # Writes received message files to workspace so they appear in
        # git_diff during the upcoming strategic review phase.
        if not is_strategic and postgres_db:
            try:
                await _process_queued_replies(job_id, workspace, postgres_db)
            except Exception as e:
                logger.warning(f"[{job_id}] Failed to process queued replies: {e}")

        # Call transition handler
        result = handle_phase_transition(
            state=state,
            workspace=workspace,
            todo_manager=todo_manager,
            min_todos=min_todos,
            max_todos=max_todos,
            config=config,
            tool_names=tool_names,
        )

        # NOTE: Job status is NOT written to the DB here.
        # The orchestrator is the single authority for job status. freeze_data
        # is propagated through graph state → report_completion() → orchestrator,
        # which persists it and determines the final DB status.

        # Audit transition attempt
        phase_number = state.get("phase_number", 0)
        phase_str = "strategic" if is_strategic else "tactical"
        auditor = get_archiver()
        if auditor:
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="phase_transition",
                node_name="handle_transition",
                iteration=iteration,
                data={
                    "transition": {
                        "from_phase": "strategic" if is_strategic else "tactical",
                        "to_phase": "tactical" if is_strategic else "strategic",
                        "success": result.success,
                        "error": result.error_message,
                        "new_phase_number": result.state_updates.get("phase_number"),
                    }
                },
                metadata=state.get("metadata"),
                phase=phase_str,
                phase_number=phase_number,
            )

        if result.success:
            logger.info(
                f"[{job_id}] Phase transition successful: "
                f"phase_number={result.state_updates.get('phase_number')}"
            )
            # Update phase state on TodoManager for tool access
            new_is_strategic = result.state_updates.get("is_strategic_phase", is_strategic)
            todo_manager.is_strategic_phase = new_is_strategic
            new_phase_number = result.state_updates.get("phase_number", phase_number)
            todo_manager.phase_number = new_phase_number
        else:
            logger.warning(
                f"[{job_id}] Phase transition rejected: {result.error_message}"
            )

        updates = result.state_updates
        # Propagate freeze_data through graph state so it reaches
        # report_completion() → orchestrator for status determination.
        if result.freeze_data:
            updates["freeze_data"] = result.freeze_data
        return updates

    return handle_transition


# =============================================================================
# GOAL CHECK NODE
# =============================================================================


def create_check_goal_node(
    plan_manager: PlanManager,
    workspace: WorkspaceManager,
    config: AgentConfig,
    todo_manager: TodoManager,
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the check_goal node.

    This node checks if the overall goal is achieved.
    Supports:
    - Stop signal: should_stop=True in state (set by handle_transition on freeze/complete)
    - Legacy plan completion
    """

    def check_goal(state: UniversalAgentState) -> Dict[str, Any]:
        """Check if overall goal is achieved."""
        job_id = state.get("job_id", "unknown")
        iteration = state.get("iteration", 0)
        is_strategic = state.get("is_strategic_phase", True)
        phase_number = state.get("phase_number", 0)
        phase_str = "strategic" if is_strategic else "tactical"

        # Check for stop signal from handle_transition (set when job frozen or completed).
        # This replaces the old file-based detection (job_frozen.json / job_completion.json)
        # which caused stale signal leaks in project workspaces.
        if state.get("should_stop", False):
            goal_achieved = state.get("goal_achieved", False)
            decision = "goal_achieved" if goal_achieved else "frozen"
            reason = "job_completed" if goal_achieved else "job_frozen_for_review"

            logger.info(
                f"[{job_id}] Stop signal detected (goal_achieved={goal_achieved}) "
                f"- stopping gracefully"
            )

            # Audit stop state
            auditor = get_archiver()
            if auditor:
                auditor.audit_step(
                    job_id=job_id,
                    agent_type=config.agent_id,
                    step_type="check",
                    node_name="check_goal",
                    iteration=iteration,
                    data={
                        "check": {
                            "decision": decision,
                            "goal_achieved": goal_achieved,
                            "should_stop": True,
                            "reason": reason,
                        }
                    },
                    metadata=state.get("metadata"),
                    phase=phase_str,
                    phase_number=phase_number,
                )

            return {
                "goal_achieved": goal_achieved,
                "should_stop": True,
            }

        # Check if there are pending todos - if so, goal is NOT achieved
        # This check MUST come before plan completion check to prevent
        # early exit when todos have been staged but plan appears complete
        pending_todos = todo_manager.list_pending()
        if pending_todos:
            logger.info(f"[{job_id}] Goal not achieved - {len(pending_todos)} pending todos")
            return {"goal_achieved": False}

        # Legacy: Check if plan is complete
        is_complete = plan_manager.is_complete()

        if is_complete:
            logger.info(f"[{job_id}] Goal achieved - plan complete")

            # Audit goal achieved
            auditor = get_archiver()
            if auditor:
                auditor.audit_step(
                    job_id=job_id,
                    agent_type=config.agent_id,
                    step_type="check",
                    node_name="check_goal",
                    iteration=iteration,
                    data={
                        "check": {
                            "decision": "goal_achieved",
                            "goal_achieved": True,
                            "reason": "plan_complete",
                        }
                    },
                    metadata=state.get("metadata"),
                    phase=phase_str,
                    phase_number=phase_number,
                )

            return {
                "goal_achieved": True,
                "should_stop": True,
            }

        # Check if there's a next phase (legacy)
        next_phase = plan_manager.get_current_phase()
        if not next_phase:
            logger.info(f"[{job_id}] No more phases and no pending todos - goal achieved")

            # Audit goal achieved (no more phases)
            auditor = get_archiver()
            if auditor:
                auditor.audit_step(
                    job_id=job_id,
                    agent_type=config.agent_id,
                    step_type="check",
                    node_name="check_goal",
                    iteration=iteration,
                    data={
                        "check": {
                            "decision": "goal_achieved",
                            "goal_achieved": True,
                            "reason": "no_more_phases",
                        }
                    },
                    metadata=state.get("metadata"),
                    phase=phase_str,
                    phase_number=phase_number,
                )

            return {
                "goal_achieved": True,
                "should_stop": True,
            }

        logger.info(f"[{job_id}] Goal not achieved, next phase: {next_phase}")

        # Audit continue decision
        auditor = get_archiver()
        if auditor:
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="check",
                node_name="check_goal",
                iteration=iteration,
                data={
                    "check": {
                        "decision": "continue",
                        "goal_achieved": False,
                        "next_phase": next_phase,
                    }
                },
                metadata=state.get("metadata"),
                phase=phase_str,
                phase_number=phase_number,
            )

        return {
            "goal_achieved": False,
        }

    return check_goal


# =============================================================================
# ROUTING FUNCTIONS
# =============================================================================


def route_after_execute(state: UniversalAgentState) -> Literal["tools", "check_todos"]:
    """Route from execute based on tool calls."""
    messages = state.get("messages", [])

    if not messages:
        return "check_todos"

    last_message = messages[-1]

    if isinstance(last_message, AIMessage):
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tools"

    return "check_todos"


def route_after_check_todos(
    state: UniversalAgentState,
) -> Literal["execute", "archive_phase", "check_goal"]:
    """Route based on whether todos are complete or a freeze was requested."""
    # Mid-phase freeze (e.g., blocking send_message) — skip archive, go straight to exit
    if state.get("should_stop", False):
        return "check_goal"
    if state.get("phase_complete", False):
        return "archive_phase"
    return "execute"


def route_entry(state: UniversalAgentState) -> Literal["init_workspace", "restore_todo_state", "restore_from_feedback"]:
    """Route at entry based on initialization state.

    If resuming with feedback, route to feedback processing node.
    If already initialized (resume), restore todo state then go to execute.
    Otherwise, start initialization flow.
    """
    if state.get("resume_feedback"):
        return "restore_from_feedback"
    if state.get("initialized", False):
        return "restore_todo_state"
    return "init_workspace"


def create_restore_todo_state_node(
    todo_manager: TodoManager,
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create node that restores TodoManager from checkpointed state.

    This node is only executed on resume (when initialized=True).
    It restores the TodoManager's internal state from the checkpoint
    before continuing execution.

    Args:
        todo_manager: TodoManager instance to restore state into

    Returns:
        Node function that restores todo state
    """

    def restore_todo_state(state: UniversalAgentState) -> Dict[str, Any]:
        """Restore TodoManager from checkpointed state."""
        job_id = state.get("job_id", "unknown")

        todos = state.get("todos")
        staged_todos = state.get("staged_todos")
        todo_next_id = state.get("todo_next_id")

        # Check if we have todo state in the checkpoint
        if todos is not None or staged_todos is not None:
            todo_manager.restore_state({
                "todos": todos,
                "staged_todos": staged_todos,
                "next_id": todo_next_id,
            })
            logger.info(f"[{job_id}] Restored TodoManager from checkpoint state")
        else:
            # No todo state in checkpoint - this is expected for old checkpoints
            # The existing recovery logic in check_todos will handle this
            logger.warning(f"[{job_id}] No todo state in checkpoint, using legacy recovery")

        # Also restore phase state on TodoManager for tool access
        is_strategic = state.get("is_strategic_phase", True)
        todo_manager.is_strategic_phase = is_strategic
        todo_manager.phase_number = state.get("phase_number", 1)

        # Detect phase-boundary resume: agent had staged tactical todos ready
        # when it froze. Apply them and flip to tactical so execution continues
        # instead of re-entering strategic planning.
        if todo_manager.has_staged_todos() and not todo_manager.list_all():
            todo_manager.apply_staged_todos()
            todo_manager.is_strategic_phase = False
            logger.info(
                f"[{job_id}] Applied staged todos on resume "
                f"— continuing to tactical phase"
            )

            todo_state = todo_manager.export_state()
            return {
                "is_strategic_phase": False,
                "should_stop": False,
                "goal_achieved": False,
                "todos": todo_state["todos"],
                "staged_todos": todo_state["staged_todos"],
                "todo_next_id": todo_state["next_id"],
            }

        # Always clear stop flags on resume — the checkpoint may carry
        # should_stop=True from a previous freeze, which would cause
        # check_goal to immediately stop the graph.
        return {"should_stop": False, "goal_achieved": False}

    return restore_todo_state


def create_restore_from_feedback_node(
    workspace: WorkspaceManager,
    todo_manager: TodoManager,
    config: AgentConfig,
    context_mgr: ContextManager,
    auxiliary_llm,
    summarization_prompt: str,
    tool_names: Optional[List[str]] = None,
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create node that handles resume-from-feedback flow.

    This node is executed when a frozen job is resumed with --feedback.
    It:
    1. Force-compacts old conversation context
    2. Writes feedback.md to workspace for persistence
    3. Injects feedback as a HumanMessage
    4. Loads resume-specific strategic todos
    5. Clears is_final_phase / should_stop / goal_achieved flags

    Args:
        workspace: WorkspaceManager for file operations
        todo_manager: TodoManager instance to load resume todos into
        config: Agent configuration
        context_mgr: ContextManager for force compaction
        auxiliary_llm: AuxiliaryLLM instance for summarization
        summarization_prompt: Prompt template for summarization

    Returns:
        Async node function for the feedback resume flow
    """

    async def restore_from_feedback(state: UniversalAgentState) -> Dict[str, Any]:
        """Process feedback resume: compact context, inject feedback, load resume todos."""
        job_id = state.get("job_id", "unknown")
        feedback = state.get("resume_feedback", "")
        messages = state.get("messages", [])

        logger.info(f"[{job_id}] Restoring from feedback resume ({len(feedback)} chars)")

        # Step 1: Force-compact old conversation context
        # This gives the agent a "fresh start" with just a summary of prior work
        compacted_messages = await context_mgr.ensure_within_limits(
            messages,
            auxiliary_llm,
            summarization_prompt,
            max_summary_length=config.context_management.max_summary_length,
            force=True,
        )

        # Separate RemoveMessage markers from actual messages
        remove_markers = [m for m in compacted_messages if isinstance(m, RemoveMessage)]
        actual_messages = [m for m in compacted_messages if not isinstance(m, RemoveMessage)]

        if remove_markers:
            logger.info(
                f"[{job_id}] Compacted context for feedback resume: "
                f"{len(messages)} -> {len(actual_messages)} messages "
                f"(removing {len(remove_markers)} old messages)"
            )

        # Step 2: Write feedback.md to workspace for persistence across context compaction
        feedback_content = (
            f"# Human Feedback\n\n"
            f"Received when resuming frozen job.\n\n"
            f"## Feedback\n\n"
            f"{feedback}\n"
        )
        workspace.write_file("feedback.md", feedback_content)
        logger.info(f"[{job_id}] Wrote feedback.md to workspace")

        # Step 2b: If this is a reply to a blocking message, write received message file
        freeze_data = None
        try:
            frozen_content = workspace.read_file("output/job_frozen.json")
            if frozen_content:
                freeze_data = json.loads(frozen_content)
        except Exception:
            pass

        if freeze_data and freeze_data.get("freeze_type") == "blocking_message":
            thread_id = freeze_data.get("thread_id", "unknown")
            msg_dir = f"messages/{thread_id}"
            try:
                existing = workspace.list_directory(msg_dir)
                seq = len(existing) + 1
            except Exception:
                seq = 2  # First message was sent, reply is #2
            from datetime import datetime, timezone as tz
            msg_content = (
                f"---\n"
                f"from: user\n"
                f"to: agent\n"
                f"date: {datetime.now(tz.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
                f"subject: {freeze_data.get('subject', 'Reply')}\n"
                f"thread: {thread_id}\n"
                f"sequence: {seq}\n"
                f"status: unread\n"
                f"---\n\n"
                f"{feedback}\n"
            )
            workspace.write_file(f"{msg_dir}/{seq:03d}_received.md", msg_content)
            logger.info(f"[{job_id}] Wrote received message to {msg_dir}/{seq:03d}_received.md")

        # Step 3: Create HumanMessage with formatted feedback
        feedback_message = HumanMessage(
            content=(
                f"[FEEDBACK_RESUME] This job was previously frozen for human review. "
                f"The human operator has provided feedback and resumed the job.\n\n"
                f"## Human Feedback\n\n{feedback}\n\n"
                f"The feedback has been saved to feedback.md for reference. "
                f"Process the feedback using the strategic todos below, then create "
                f"corrective tactical todos to address each feedback item."
            )
        )

        # Step 4: Load resume-specific strategic todos
        resume_todos = get_resume_strategic_todos(config, tool_names=tool_names)
        todo_list = [todo.to_dict() for todo in resume_todos]
        todo_manager.set_todos_from_list(todo_list)
        todo_manager.is_strategic_phase = True
        todo_manager.phase_number = state.get("phase_number", 0)

        # Export todo state for checkpointing
        todo_state = todo_manager.export_state()

        logger.info(
            f"[{job_id}] Loaded {len(resume_todos)} resume strategic todos"
        )

        # Audit the feedback resume
        auditor = get_archiver()
        if auditor:
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="feedback_resume",
                node_name="restore_from_feedback",
                iteration=state.get("iteration", 0),
                data={
                    "feedback_length": len(feedback),
                    "resume_todos": len(resume_todos),
                    "messages_before": len(messages),
                    "messages_after": len(actual_messages),
                },
                metadata=state.get("metadata"),
                phase="strategic",
                phase_number=state.get("phase_number", 0),
            )

        # Step 5: Return state updates
        # Include remove markers + compacted messages + feedback message
        result_messages = remove_markers + actual_messages + [feedback_message]

        return {
            "messages": result_messages,
            "resume_feedback": None,  # Clear — consumed
            "is_final_phase": False,
            "should_stop": False,
            "goal_achieved": False,
            "is_strategic_phase": True,
            "phase_complete": False,
            "todos": todo_state["todos"],
            "staged_todos": todo_state["staged_todos"],
            "todo_next_id": todo_state["next_id"],
        }

    return restore_from_feedback


def create_route_after_transition(
    workspace: WorkspaceManager,
) -> Callable[[UniversalAgentState], Literal["execute", "check_goal"]]:
    """Create route_after_transition with workspace access for frozen job detection.

    Args:
        workspace: WorkspaceManager for checking job_frozen.json

    Returns:
        Routing function that checks for frozen jobs before routing
    """

    def route_after_transition(
        state: UniversalAgentState,
    ) -> Literal["execute", "check_goal"]:
        """Route after phase transition based on success/failure.

        IMPORTANT: If job is stopped (should_stop=True from handle_transition),
        always go to check_goal so the stop state can be detected and the graph ends.

        If transition was rejected (last message contains rejection marker),
        go back to execute so the agent can fix the issue. Otherwise,
        proceed to check_goal.
        """
        # Check if job should stop - must go to check_goal to detect and stop
        if state.get("should_stop", False):
            return "check_goal"

        # Check if transition was rejected (last message contains rejection marker)
        messages = state.get("messages", [])
        if messages:
            last_msg = messages[-1]
            content = getattr(last_msg, "content", "") or ""
            if "[TRANSITION_REJECTED]" in content:
                return "execute"

        # Default: transition succeeded, proceed to goal check
        return "check_goal"

    return route_after_transition


# =============================================================================
# AUDITED TOOL NODE
# =============================================================================


def create_audited_tool_node(
    tools: List[Any],
    config: AgentConfig,
    recall_store=None,
    tool_context: Optional[ToolContext] = None,
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create a tool node with audit logging, stuck detection, and tool masking.

    This wraps LangGraph's ToolNode to add:
    - MongoDB audit logging for tool calls and results
    - Fingerprint-based loop detection → tool masking
    - Progress-based stuck detection → diagnostic messages → freeze
    - Category failure tracking → category-wide masking
    - Hard budget caps per phase
    - Tool-not-found enrichment with actionable guidance

    See docs/features/stuck_agent_recovery.md for full design rationale.

    Args:
        tools: List of tool objects
        config: Agent configuration for agent_id
        recall_store: Optional RecallStore for memory storage
        tool_context: Optional ToolContext for draining queued memories

    Returns:
        A callable node function with audit logging
    """
    tool_node = ToolNode(tools, handle_tool_errors=True)

    # Loop detection state: track recent tool calls as (name, args_hash) tuples
    import hashlib
    from collections import deque
    _tool_call_history: deque = deque(maxlen=30)
    _LOOP_WARNING_THRESHOLD = 10  # warn after 10 identical calls in last 30

    # Progress-based stuck detection
    _calls_since_progress = [0]
    _reflection_injected = [False]
    _phase_tool_call_count = [0]
    _last_phase_number = [-1]

    # Loop warning state: signatures that have triggered warnings
    _warned_signatures: set = set()  # set of (name, args_hash) tuples
    _category_failures: dict = {}  # category -> set of failed tool names

    # Config-driven thresholds
    _PROGRESS_THRESHOLD = config.limits.progress_stall_threshold  # default 15
    _HARD_CAP = config.limits.max_tool_calls_per_phase  # default 100

    # Tools that indicate forward progress (reset stuck counter)
    PROGRESS_TOOLS = {
        "todo_complete", "write_file", "next_phase_todos",
        "job_complete", "mark_complete", "kb_write", "kb_update",
    }

    TOOL_NOT_FOUND_PATTERN = "is not a valid tool"

    def _get_tool_category(tool_name: str) -> Optional[str]:
        from .tools.registry import TOOL_REGISTRY
        return TOOL_REGISTRY.get(tool_name, {}).get("category")

    def _get_category_tool_names(category: str) -> List[str]:
        from .tools.registry import TOOL_REGISTRY
        return [name for name, meta in TOOL_REGISTRY.items()
                if meta.get("category") == category]

    # Build phase-allowed tool sets for defense-in-depth validation.
    # Primary enforcement is LLM schema binding; this catches hallucinated calls.
    from .tools.registry import filter_tools_by_phase as _filter_phase, TOOL_REGISTRY as _TOOL_REG
    _all_tool_names = [t.name for t in tools]
    _phase_allowed: Dict[str, set] = {
        "strategic": set(_filter_phase(_all_tool_names, "strategic")),
        "tactical": set(_filter_phase(_all_tool_names, "tactical")),
    }
    # Only gate tools that have phase metadata in the registry.
    # Unregistered tools (dynamic, test) have no phase restriction.
    _phase_gated_names = set(n for n in _all_tool_names if n in _TOOL_REG)

    async def audited_tools(state: UniversalAgentState) -> Dict[str, Any]:
        """Execute tools with audit logging and stuck detection."""
        job_id = state.get("job_id", "unknown")
        iteration = state.get("iteration", 0)
        messages = state.get("messages", [])
        is_strategic = state.get("is_strategic_phase", True)
        phase_number = state.get("phase_number", 0)
        phase_str = "strategic" if is_strategic else "tactical"

        # Extract tool calls from last message
        tool_calls_info = []
        if messages and isinstance(messages[-1], AIMessage):
            last_msg = messages[-1]
            if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                for tc in last_msg.tool_calls:
                    tool_calls_info.append({
                        "name": tc.get("name", "unknown"),
                        "call_id": tc.get("id", ""),
                        "args": tc.get("args", {}),
                    })

        # Defense-in-depth: reject tool calls not declared for the current phase.
        # LLM schema binding is the primary gate; this catches hallucinated calls.
        # ToolNode can't selectively skip calls, so if any call violates the
        # phase gate we reject the entire batch with explicit error messages.
        allowed = _phase_allowed.get(phase_str, set())
        phase_violations = [
            tc for tc in tool_calls_info
            if tc["name"] in _phase_gated_names and tc["name"] not in allowed
        ]
        if phase_violations:
            violated_names = [tc["name"] for tc in phase_violations]
            logger.warning(
                f"[{job_id}] Phase gate: {violated_names} not available "
                f"in {phase_str} phase — rejecting entire batch"
            )
            return {"messages": [
                ToolMessage(
                    content=(
                        f"Error: '{tc['name']}' is not available in the "
                        f"{phase_str} phase. Use tools appropriate for "
                        f"this phase."
                    ),
                    tool_call_id=tc["call_id"],
                    name=tc["name"],
                )
                for tc in tool_calls_info  # respond to ALL calls so LangGraph is happy
            ]}

        # Phase change: reset all detection state
        if phase_number != _last_phase_number[0]:
            _last_phase_number[0] = phase_number
            _tool_call_history.clear()
            _calls_since_progress[0] = 0
            _reflection_injected[0] = False
            _phase_tool_call_count[0] = 0
            _warned_signatures.clear()
            _category_failures.clear()

        # Hard cap check
        _phase_tool_call_count[0] += len(tool_calls_info)
        if _phase_tool_call_count[0] > _HARD_CAP:
            if phase_str == "strategic":
                # Strategic phases should be short — freeze if budget exceeded.
                logger.error(
                    f"[{job_id}] Hard cap in strategic phase: "
                    f"{_phase_tool_call_count[0]} calls "
                    f"(phase {phase_number})"
                )
                freeze_data = {
                    "freeze_type": "budget_exceeded",
                    "phase": phase_str,
                    "phase_number": phase_number,
                    "reason": f"Hard cap of {_HARD_CAP} tool calls exceeded in strategic phase",
                    "tool_calls_this_phase": _phase_tool_call_count[0],
                    "warned_signatures": len(_warned_signatures),
                }
                if tool_context and tool_context.workspace_manager:
                    try:
                        tool_context.workspace_manager.write_file(
                            "output/job_frozen.json",
                            json.dumps(freeze_data, indent=2, ensure_ascii=False),
                        )
                    except Exception as e:
                        logger.error(f"[{job_id}] Failed to write freeze file: {e}")
                freeze_msgs = [
                    ToolMessage(
                        content=(
                            f"PHASE FROZEN: {_phase_tool_call_count[0]} tool "
                            "calls in strategic phase without completing "
                            "review. Job paused for human review."
                        ),
                        tool_call_id=tc["call_id"],
                        name=tc["name"],
                    ) for tc in tool_calls_info
                ]
                return {"should_stop": True, "messages": freeze_msgs}
            else:
                # Tactical phase — don't freeze, rewind and let the agent retry.
                logger.warning(
                    f"[{job_id}] Hard cap in tactical phase: "
                    f"{_phase_tool_call_count[0]} calls "
                    f"(phase {phase_number}). Triggering rewind."
                )
                rewind_note = (
                    f"Budget of {_HARD_CAP} tool calls reached in this phase"
                )
                # Archive current todos and clear the list
                if tool_context and tool_context.todo_manager:
                    try:
                        tool_context.todo_manager.archive_with_failure_note(
                            rewind_note
                        )
                        if tool_context.todo_manager.has_staged_todos():
                            tool_context.todo_manager.clear_staged_todos()
                    except Exception as e:
                        logger.error(
                            f"[{job_id}] Failed to rewind todos: {e}"
                        )
                # Capture count before reset for the message
                calls_used = _phase_tool_call_count[0]
                # Reset counters so the next phase starts fresh
                _phase_tool_call_count[0] = 0
                _calls_since_progress[0] = 0
                _tool_call_history.clear()
                _warned_signatures.clear()
                _reflection_injected[0] = False

                rewind_msg = SystemMessage(content=(
                    f"PHASE BUDGET REACHED: "
                    f"{calls_used}/{_HARD_CAP} tool calls "
                    "consumed without completing this phase's objectives. "
                    "Current todos have been archived.\n\n"
                    "Before creating new todos:\n"
                    "1. Review what worked and what didn't in this phase\n"
                    "2. Update plan.md if the approach needs to change\n"
                    "3. Create smaller, more focused todos with "
                    "next_phase_todos()\n\n"
                    "Do not repeat the same sequence of actions."
                ))
                # Return ToolMessages (to satisfy pending tool calls) + the rewind message.
                # Don't execute the tools — skip straight to the rewind.
                tool_msgs = [
                    ToolMessage(
                        content=(
                            f"Skipped: phase budget of {_HARD_CAP} tool calls "
                            "reached. Todos archived — rethink your approach."
                        ),
                        tool_call_id=tc["call_id"],
                        name=tc["name"],
                    ) for tc in tool_calls_info
                ]
                return {"messages": tool_msgs + [rewind_msg]}

        # Fingerprint-based loop detection -> track signatures for warnings
        _loop_warned_call_ids: set = set()  # call_ids to warn about after execution
        for tc_info in tool_calls_info:
            args_str = json.dumps(tc_info["args"], sort_keys=True, default=str)
            args_hash = hashlib.md5(args_str.encode()).hexdigest()[:12]
            call_sig = (tc_info["name"], args_hash)
            _tool_call_history.append(call_sig)

            identical_count = sum(1 for c in _tool_call_history if c == call_sig)
            if identical_count >= _LOOP_WARNING_THRESHOLD:
                _loop_warned_call_ids.add(tc_info["call_id"])
                if call_sig not in _warned_signatures:
                    _warned_signatures.add(call_sig)
                    logger.warning(
                        f"[{job_id}] Loop detected: '{tc_info['name']}' called "
                        f"{identical_count} times with same args (hash {args_hash})"
                    )

        # Audit tool calls before execution (will be updated with results via update_tool_result)
        auditor = get_archiver()
        audit_ids: Dict[str, str] = {}  # call_id -> audit_doc_id
        if auditor:
            for tc_info in tool_calls_info:
                doc_id = auditor.audit_tool_call(
                    job_id=job_id,
                    agent_type=config.agent_id,
                    iteration=iteration,
                    tool_name=tc_info["name"],
                    call_id=tc_info["call_id"],
                    arguments=tc_info["args"],
                    metadata=state.get("metadata"),
                    phase=phase_str,
                    phase_number=phase_number,
                )
                if doc_id:
                    audit_ids[tc_info["call_id"]] = doc_id

        # Execute all tools (loop detection is advisory, never blocks execution)
        start_time = time.time()
        result = await tool_node.ainvoke(state)
        execution_time_ms = int((time.time() - start_time) * 1000)

        # Append loop warnings to tool results for flagged calls
        if _loop_warned_call_ids and "messages" in result:
            for msg in result["messages"]:
                if (isinstance(msg, ToolMessage)
                        and msg.tool_call_id in _loop_warned_call_ids):
                    msg.content = (
                        (msg.content or "") +
                        "\n\n[LOOP WARNING] You have called this tool with the "
                        "same arguments multiple times. You may be stuck in a "
                        "loop. Consider a different approach: try different "
                        "arguments, use a different tool, or mark the current "
                        "todo as blocked and move on."
                    )

        # Check for workspace unavailable errors (VM connection lost).
        # ToolNode catches all exceptions and turns them into error messages,
        # but WorkspaceUnavailableError should propagate for VM recovery.
        if "messages" in result:
            for msg in result["messages"]:
                if isinstance(msg, ToolMessage) and msg.content:
                    if "WorkspaceUnavailableError" in msg.content:
                        from .core.workspace_backend import WorkspaceUnavailableError
                        raise WorkspaceUnavailableError(
                            f"VM workspace connection lost during tool execution: "
                            f"{msg.content[:300]}"
                        )

        # Enrich tool-not-found errors with actionable guidance
        if "messages" in result:
            for msg in result["messages"]:
                if (isinstance(msg, ToolMessage)
                        and msg.content
                        and TOOL_NOT_FOUND_PATTERN in msg.content):
                    msg.content += (
                        "\n\nIf your current todo requires this tool, mark it "
                        "as blocked using todo_complete with a note explaining "
                        "which tool is needed, and proceed with the next todo."
                    )

        # Track category failures for logging
        if "messages" in result:
            for msg in result["messages"]:
                if isinstance(msg, ToolMessage) and _is_tool_error(msg.content or ""):
                    tool_name = None
                    for tc in tool_calls_info:
                        if tc["call_id"] == getattr(msg, "tool_call_id", ""):
                            tool_name = tc["name"]
                            break
                    if tool_name:
                        category = _get_tool_category(tool_name)
                        if category:
                            _category_failures.setdefault(category, set()).add(tool_name)
                            if len(_category_failures[category]) >= 3:
                                logger.warning(
                                    f"[{job_id}] Multiple failures in category "
                                    f"'{category}': {_category_failures[category]}"
                                )

        # Progress tracking
        progress_made = False
        executed_names = {tc["name"] for tc in tool_calls_info}
        if executed_names & PROGRESS_TOOLS:
            for msg in result.get("messages", []):
                if isinstance(msg, ToolMessage) and not _is_tool_error(msg.content or ""):
                    for tc in tool_calls_info:
                        if (tc["call_id"] == getattr(msg, "tool_call_id", "")
                                and tc["name"] in PROGRESS_TOOLS):
                            progress_made = True
                            break
                if progress_made:
                    break

        if progress_made:
            _calls_since_progress[0] = 0
            _reflection_injected[0] = False
        else:
            _calls_since_progress[0] += len(tool_calls_info)

        # todo_rewind is a deliberate re-plan: reset loop detection state
        if "todo_rewind" in {tc["name"] for tc in tool_calls_info}:
            for msg in result.get("messages", []):
                if (isinstance(msg, ToolMessage)
                        and not _is_tool_error(msg.content or "")
                        and any(tc["name"] == "todo_rewind"
                                and tc["call_id"] == msg.tool_call_id
                                for tc in tool_calls_info)):
                    _tool_call_history.clear()
                    _warned_signatures.clear()
                    _calls_since_progress[0] = 0
                    _reflection_injected[0] = False
                    logger.info(
                        f"[{job_id}] Loop detection reset after todo_rewind"
                    )
                    break

        # Progress nudge: periodic reminders to write findings down.
        # Never freezes the job — the hard cap (Layer 4) is the only stop.
        if _calls_since_progress[0] >= _PROGRESS_THRESHOLD:
            calls = _calls_since_progress[0]
            nudge_count = (calls - _PROGRESS_THRESHOLD) // _PROGRESS_THRESHOLD + 1
            # Inject a nudge every _PROGRESS_THRESHOLD calls without progress
            if calls == _PROGRESS_THRESHOLD or calls % _PROGRESS_THRESHOLD == 0:
                remaining = _HARD_CAP - _phase_tool_call_count[0]
                diagnostic = SystemMessage(content=(
                    f"OBSERVATION: {calls} tool calls since the last file "
                    f"write or todo completion. Phase budget: "
                    f"{remaining}/{_HARD_CAP} calls remaining.\n\n"
                    "If you have gathered useful information, write it to "
                    "a file now — findings not written to files are lost "
                    "during context compaction. If you still need specific "
                    "information, identify the gap and target it directly."
                ))
                result.setdefault("messages", []).append(diagnostic)
                logger.info(
                    f"[{job_id}] Progress nudge #{nudge_count}: "
                    f"{calls} calls without progress "
                    f"(phase {phase_number} {phase_str})"
                )

        # Update tool audit documents with results
        if auditor and "messages" in result:
            for msg in result["messages"]:
                if isinstance(msg, ToolMessage):
                    call_id = getattr(msg, "tool_call_id", "")
                    audit_doc_id = audit_ids.get(call_id)
                    if audit_doc_id:
                        content = msg.content if msg.content else ""
                        is_error = _is_tool_error(content)

                        auditor.update_tool_result(
                            audit_doc_id=audit_doc_id,
                            result=content,
                            success=not is_error,
                            latency_ms=execution_time_ms // max(len(tool_calls_info), 1),
                            error=content[:500] if is_error else None,
                        )

        # Memory Light: flush queued memories from sync tool functions
        if recall_store and tool_context:
            for mem in tool_context.drain_pending_memories():
                try:
                    await recall_store.store(**mem)
                except Exception as e:
                    logger.warning(f"[{job_id}] Failed to store queued memory: {e}")

        # Communication: check if a tool requested a job freeze (blocking send_message)
        if tool_context:
            freeze_req = tool_context.consume_freeze_request()
            if freeze_req:
                try:
                    ws = tool_context.workspace_manager
                    ws.write_file(
                        "output/job_frozen.json",
                        json.dumps(freeze_req, indent=2, ensure_ascii=False),
                    )
                    if ws.git_manager and ws.git_manager.is_active:
                        ws.git_manager.commit("Job frozen: waiting for reply")
                    result["should_stop"] = True
                    logger.info(
                        f"[{job_id}] Freeze requested by tool: "
                        f"{freeze_req.get('freeze_type')}"
                    )
                except Exception as e:
                    logger.error(f"[{job_id}] Failed to process freeze request: {e}")

        return result

    return audited_tools


# =============================================================================
# GRAPH BUILDER
# =============================================================================


def build_phase_alternation_graph(
    strategic_llm_with_tools: BaseChatModel,
    tactical_llm_with_tools: BaseChatModel,
    tools: List[Any],
    config: AgentConfig,
    workspace: WorkspaceManager,
    todo_manager: TodoManager,
    workspace_template: str = "",
    checkpointer: Optional[BaseCheckpointSaver] = None,
    auxiliary_llm=None,
    snapshot_manager: Optional[PhaseSnapshotManager] = None,
    tool_context: Optional[ToolContext] = None,
    postgres_db: Optional[Any] = None,
    # Backwards compatibility
    llm_with_tools: Optional[BaseChatModel] = None,
    summarization_llm: Optional[BaseChatModel] = None,
) -> CompiledStateGraph:
    """Build the phase alternation graph for the Universal Agent.

    Creates a single ReAct loop that alternates between strategic and tactical
    phases. The strategic agent uses tools to plan and create todos, while the
    tactical agent executes domain-specific work.

    Graph structure:
    - Initialization: init_workspace -> init_strategic_todos
    - ReAct loop: execute -> tools -> check_todos -> archive_phase -> handle_transition
    - Goal check: check_goal -> END or back to execute

    Args:
        strategic_llm_with_tools: LLM with tools for strategic (planning) phases
        tactical_llm_with_tools: LLM with tools for tactical (execution) phases
        tools: List of tool objects
        config: Agent configuration
        workspace: WorkspaceManager instance
        todo_manager: TodoManager instance (must be the same one used by tools)
        workspace_template: Template content for workspace.md
        checkpointer: Optional LangGraph checkpointer for state persistence.
            When provided, enables resume after crash using the same thread_id.
        auxiliary_llm: AuxiliaryLLM instance for summarization and support tasks.
        snapshot_manager: Optional PhaseSnapshotManager for creating phase snapshots.
            When provided, enables recovery to previous phases after corruption.
        tool_context: Optional ToolContext for inline curation and memory flush.
        summarization_llm: Deprecated - use auxiliary_llm instead.
        llm_with_tools: Deprecated - use strategic_llm_with_tools instead.

    Returns:
        Compiled StateGraph with checkpointing if checkpointer provided
    """
    # Backwards compatibility: if old param used, map to new params
    if llm_with_tools is not None and strategic_llm_with_tools is None:
        import warnings
        warnings.warn(
            "llm_with_tools is deprecated, use strategic_llm_with_tools and "
            "tactical_llm_with_tools instead",
            DeprecationWarning,
            stacklevel=2,
        )
        strategic_llm_with_tools = llm_with_tools
        tactical_llm_with_tools = llm_with_tools
    # Create managers (todo_manager is passed in to ensure it's the same instance used by tools)
    plan_manager = PlanManager(workspace)
    memory_manager = MemoryManager(workspace)

    # Create context manager for context window management
    context_config = ContextConfig(
        compaction_threshold_tokens=config.limits.context_threshold_tokens,
        summarization_threshold_tokens=config.limits.context_threshold_tokens,
        message_count_threshold=config.limits.message_count_threshold,
        message_count_min_tokens=config.limits.message_count_min_tokens,
        keep_recent_messages=config.context_management.keep_recent_messages,
        keep_recent_tool_results=config.context_management.keep_recent_tool_results,
        # Safety layer constants
        model_max_context_tokens=config.limits.model_max_context_tokens,
        summarization_safe_limit=config.limits.summarization_safe_limit,
        summarization_chunk_size=config.limits.summarization_chunk_size,
    )
    strategic_config = config.llm.get_phase_config("strategic")
    tactical_config = config.llm.get_phase_config("tactical")
    summarization_config = config.llm.get_phase_config("summarization")
    context_mgr = ContextManager(
        config=context_config,
        model=config.llm.model,
        strategic_model=strategic_config.model,
        tactical_model=tactical_config.model,
        summarization_timeout=summarization_config.timeout or config.llm.timeout or 600.0,
    )

    # Create retry manager for LLM call retries
    retry_manager = ToolRetryManager(max_retries=config.limits.tool_retry_count)

    # Load summarization prompt (use summarization model for matrix resolution)
    summarization_config = config.llm.get_phase_config("summarization")
    summarization_prompt = load_summarization_prompt(config, model=summarization_config.model)

    # Load auxiliary task prompts (use auxiliary model for matrix resolution)
    aux_model = config.auxiliary.model or summarization_config.model or config.llm.model
    memory_extraction_prompt = load_auxiliary_prompt(config, "memory_extraction", model=aux_model)
    memory_assembler_prompt = load_auxiliary_prompt(config, "memory_assembler", model=aux_model)
    curation_prompt = load_auxiliary_prompt(config, "curation", model=aux_model)

    # workspace_template is no longer used — workspace.md replaced by
    # project knowledge base + memory system. Parameter kept for backward compat.

    # Backwards compatibility: wrap a raw LLM in AuxiliaryLLM if needed
    if auxiliary_llm is None:
        from src.services.auxiliary import AuxiliaryLLM
        raw_llm = summarization_llm or strategic_llm_with_tools
        auxiliary_llm = AuxiliaryLLM(llm=raw_llm)

    # Extract RecallStore for memory injection and free sources
    recall_store = tool_context.recall_store if tool_context else None

    # Create graph
    workflow = StateGraph(UniversalAgentState)

    # Extract tool names for Jinja2 rendering of instruction templates
    _tool_names = [t.name for t in tools] if tools else None

    # Create nodes
    init_workspace = create_init_workspace_node(memory_manager, workspace_template, config)
    init_strategic_todos = create_init_strategic_todos_node(workspace, todo_manager, config, tool_names=_tool_names)
    restore_todo_state = create_restore_todo_state_node(todo_manager)
    restore_from_feedback = create_restore_from_feedback_node(
        workspace, todo_manager, config,
        context_mgr, auxiliary_llm, summarization_prompt,
        tool_names=_tool_names,
    )

    execute = create_execute_node(
        strategic_llm_with_tools=strategic_llm_with_tools,
        tactical_llm_with_tools=tactical_llm_with_tools,
        todo_manager=todo_manager,
        memory_manager=memory_manager,
        workspace_manager=workspace,
        config=config,
        context_mgr=context_mgr,
        retry_manager=retry_manager,
        auxiliary_llm=auxiliary_llm,
        summarization_prompt=summarization_prompt,
        memory_extraction_prompt=memory_extraction_prompt,
        memory_assembler_prompt=memory_assembler_prompt,
        tool_context=tool_context,
        tool_names=_tool_names,
    )
    check_todos = create_check_todos_node(todo_manager, config, tool_names=_tool_names)
    archive_phase = create_archive_phase_node(
        todo_manager, plan_manager, config,
        context_mgr, auxiliary_llm, summarization_prompt,
        snapshot_manager=snapshot_manager,
        recall_store=recall_store,
        tool_context=tool_context,
        workspace_manager=workspace,
        memory_extraction_prompt=memory_extraction_prompt,
        curation_prompt=curation_prompt,
    )

    handle_transition = create_handle_transition_node(
        workspace, todo_manager, config,
        min_todos=config.phase_settings.min_todos,
        max_todos=config.phase_settings.max_todos,
        postgres_db=postgres_db,
        tool_names=_tool_names,
    )

    check_goal = create_check_goal_node(plan_manager, workspace, config, todo_manager)
    tool_node = create_audited_tool_node(
        tools, config,
        recall_store=recall_store,
        tool_context=tool_context,
    )

    # Add nodes to graph
    workflow.add_node("init_workspace", init_workspace)
    workflow.add_node("init_strategic_todos", init_strategic_todos)
    workflow.add_node("restore_todo_state", restore_todo_state)
    workflow.add_node("restore_from_feedback", restore_from_feedback)
    workflow.add_node("execute", execute)
    workflow.add_node("tools", tool_node)
    workflow.add_node("check_todos", check_todos)
    workflow.add_node("archive_phase", archive_phase)
    workflow.add_node("handle_transition", handle_transition)
    workflow.add_node("check_goal", check_goal)

    logger.info("Building phase alternation graph")

    # Set conditional entry point
    workflow.set_conditional_entry_point(
        route_entry,
        {
            "init_workspace": "init_workspace",
            "restore_todo_state": "restore_todo_state",
            "restore_from_feedback": "restore_from_feedback",
        },
    )

    # Wire initialization: init_workspace -> init_strategic_todos -> execute
    workflow.add_edge("init_workspace", "init_strategic_todos")
    workflow.add_edge("init_strategic_todos", "execute")

    # Wire resume path: restore_todo_state -> execute
    workflow.add_edge("restore_todo_state", "execute")

    # Wire feedback resume path: restore_from_feedback -> execute
    workflow.add_edge("restore_from_feedback", "execute")

    # Wire ReAct loop
    workflow.add_conditional_edges(
        "execute",
        route_after_execute,
        {
            "tools": "tools",
            "check_todos": "check_todos",
        },
    )
    workflow.add_edge("tools", "check_todos")
    workflow.add_conditional_edges(
        "check_todos",
        route_after_check_todos,
        {
            "execute": "execute",
            "archive_phase": "archive_phase",
            "check_goal": "check_goal",
        },
    )

    # Wire phase transition
    # Create routing function with workspace access for frozen job detection
    route_after_transition_fn = create_route_after_transition(workspace)

    workflow.add_edge("archive_phase", "handle_transition")
    workflow.add_conditional_edges(
        "handle_transition",
        route_after_transition_fn,
        {
            "execute": "execute",  # Transition rejected, agent fixes issue
            "check_goal": "check_goal",  # Transition succeeded or job frozen
        },
    )

    # Wire goal check
    workflow.add_conditional_edges(
        "check_goal",
        lambda s: "end" if s.get("goal_achieved") or s.get("should_stop") else "execute",
        {
            "execute": "execute",
            "end": END,
        },
    )

    return workflow.compile(checkpointer=checkpointer)


# Backward compatibility alias
def build_nested_loop_graph(
    llm: BaseChatModel,
    llm_with_tools: BaseChatModel,
    tools: List[Any],
    config: AgentConfig,
    system_prompt_template: str,
    workspace: WorkspaceManager,
    todo_manager: TodoManager,
    workspace_template: str = "",
    checkpointer: Optional[BaseCheckpointSaver] = None,
    use_phase_alternation: bool = True,
) -> CompiledStateGraph:
    """Build the graph for the Universal Agent (deprecated).

    This function is deprecated. Use build_phase_alternation_graph() instead.
    The `llm`, `system_prompt_template`, and `use_phase_alternation` parameters
    are now ignored.

    Args:
        llm: Deprecated - ignored (was for planning/memory updates)
        llm_with_tools: LLM with tools bound for execution
        tools: List of tool objects
        config: Agent configuration
        system_prompt_template: Deprecated - ignored (phase prompts used instead)
        workspace: WorkspaceManager instance
        todo_manager: TodoManager instance (must be same one used by tools)
        workspace_template: Template content for workspace.md
        checkpointer: Optional LangGraph checkpointer
        use_phase_alternation: Deprecated - ignored (always True)

    Returns:
        Compiled StateGraph
    """
    import warnings
    warnings.warn(
        "build_nested_loop_graph is deprecated, use build_phase_alternation_graph instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return build_phase_alternation_graph(
        strategic_llm_with_tools=llm_with_tools,
        tactical_llm_with_tools=llm_with_tools,
        tools=tools,
        config=config,
        workspace=workspace,
        todo_manager=todo_manager,
        workspace_template=workspace_template,
        checkpointer=checkpointer,
        summarization_llm=llm,  # Use old llm param for summarization
    )


# =============================================================================
# STREAMING EXECUTION
# =============================================================================


async def run_graph_with_streaming(
    graph: StateGraph,
    graph_input: Optional[UniversalAgentState],
    config: Dict[str, Any],
):
    """Run the graph with streaming output.

    Yields state updates as the graph executes.

    Args:
        graph: Compiled graph
        graph_input: Initial state for new jobs, or None to resume from checkpoint
        config: LangGraph config (thread_id, recursion_limit, etc.)

    Yields:
        State updates from each node
    """
    async for state in graph.astream(graph_input, config=config, stream_mode="values"):
        yield state


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def get_managers_from_workspace(
    workspace: WorkspaceManager,
) -> tuple[TodoManager, PlanManager, MemoryManager]:
    """Create all managers from a workspace.

    Convenience function for tests and external use.

    Args:
        workspace: WorkspaceManager instance

    Returns:
        Tuple of (TodoManager, PlanManager, MemoryManager)
    """
    return (
        TodoManager(workspace),
        PlanManager(workspace),
        MemoryManager(workspace),
    )
