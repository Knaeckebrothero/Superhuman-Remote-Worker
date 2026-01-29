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

import logging
import time
from typing import Any, Callable, Dict, List, Literal, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
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
    get_phase_system_prompt,
)
from .core.workspace import WorkspaceManager
from .core.archiver import get_archiver
from .core.context import ContextManager, ContextConfig, ToolRetryManager, find_safe_slice_start, sanitize_message_history
from .core.phase_snapshot import PhaseSnapshotManager
from .core.phase import (
    handle_phase_transition,
    TransitionResult,
    get_initial_strategic_todos,
    get_transition_strategic_todos,
)
from .managers import TodoManager, TodoStatus, PlanManager, MemoryManager

logger = logging.getLogger(__name__)


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


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

    This node initializes workspace.md from template if it doesn't exist.
    """

    def init_workspace(state: UniversalAgentState) -> Dict[str, Any]:
        """Initialize workspace.md from template."""
        job_id = state.get("job_id", "unknown")
        logger.info(f"[{job_id}] Initializing workspace")

        workspace_created = not memory_manager.exists()
        if workspace_created:
            memory_manager.write(workspace_template)
            logger.info(f"[{job_id}] Created workspace.md from template")
        else:
            logger.debug(f"[{job_id}] workspace.md already exists")

        # Read workspace into state for system prompt injection
        workspace_memory = memory_manager.read()

        # Audit workspace initialization
        auditor = get_archiver()
        if auditor:
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="initialize",
                node_name="init_workspace",
                iteration=0,
                data={"workspace": {"created": workspace_created}},
                metadata=state.get("metadata"),
                phase="strategic",
                phase_number=0,
            )

        return {
            "workspace_memory": workspace_memory,
        }

    return init_workspace


def create_init_strategic_todos_node(
    workspace: WorkspaceManager,
    todo_manager: TodoManager,
    config: AgentConfig,
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

        # Read instructions for context
        try:
            instructions = workspace.read_file("instructions.md")
        except FileNotFoundError:
            instructions = "No instructions.md found. Please create a plan based on job metadata."
            logger.warning(f"[{job_id}] instructions.md not found")

        # Load predefined strategic todos from config template
        strategic_todos = get_initial_strategic_todos(config)
        todo_list = [todo.to_dict() for todo in strategic_todos]
        todo_manager.set_todos_from_list(todo_list)

        # Initialize phase state on TodoManager for tool access
        todo_manager.is_strategic_phase = True

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
                    "instructions_length": len(instructions),
                },
                metadata=state.get("metadata"),
                phase="strategic",
                phase_number=0,
            )

        # Add instructions as initial context for the strategic agent
        message = HumanMessage(
            content=f"## Task Instructions\n\n{instructions}\n\n"
            "You are starting in strategic mode. Work through the predefined todos "
            "to understand the task, create a plan, and prepare todos for execution."
        )

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
    llm_with_tools: BaseChatModel,
    todo_manager: TodoManager,
    memory_manager: MemoryManager,
    workspace_manager: WorkspaceManager,
    config: AgentConfig,
    context_mgr: ContextManager,
    retry_manager: ToolRetryManager,
    summarization_llm: BaseChatModel,
    summarization_prompt: str,
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the execute node.

    This is the main ReAct execution node that processes todos.

    Args:
        llm_with_tools: LLM with tools bound for execution
        todo_manager: TodoManager for task tracking
        memory_manager: MemoryManager for workspace.md access
        workspace_manager: WorkspaceManager for file operations
        config: Agent configuration
        context_mgr: ContextManager for context window management
        retry_manager: ToolRetryManager for LLM call retry logic
        summarization_llm: LLM for context summarization
        summarization_prompt: Prompt template for summarization
    """

    async def execute(state: UniversalAgentState) -> Dict[str, Any]:
        """Execute current todo using ReAct pattern."""
        job_id = state.get("job_id", "unknown")
        iteration = state.get("iteration", 0)
        messages = state.get("messages", [])

        logger.debug(f"[{job_id}] Execute iteration {iteration}")

        # Debug: log message types in state
        msg_types = {}
        for msg in messages:
            msg_type = type(msg).__name__
            msg_types[msg_type] = msg_types.get(msg_type, 0) + 1
        logger.debug(f"[{job_id}] State messages: {len(messages)} total, types: {msg_types}")

        # Build messages for LLM
        prepared_messages = []

        # Get current dynamic content for system prompt
        todos_content = todo_manager.format_for_display()
        workspace_content = ""
        if workspace_manager.exists("workspace.md"):
            workspace_content = workspace_manager.read_file("workspace.md")

        # Get phase-aware system prompt
        is_strategic = state.get("is_strategic_phase", True)
        phase_number = state.get("phase_number", 0)
        full_system = get_phase_system_prompt(
            config=config,
            is_strategic=is_strategic,
            phase_number=phase_number,
            todos_content=todos_content,
            workspace_content=workspace_content,
        )
        logger.debug(
            f"[{job_id}] Using {'strategic' if is_strategic else 'tactical'} "
            f"prompt for phase {phase_number}"
        )
        prepared_messages.append(SystemMessage(content=full_system))

        # Ensure context is within limits before LLM call
        original_message_count = len(messages)
        oss_reasoning_level = config.context_management.reasoning_level or config.llm.reasoning_level or "high"
        messages = await context_mgr.ensure_within_limits(
            messages,
            summarization_llm,
            summarization_prompt,
            oss_reasoning_level=oss_reasoning_level,
            max_summary_length=config.context_management.max_summary_length,
        )
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

        # Always clear old tool results, keep last 10
        messages = context_mgr.clear_old_tool_results(messages)

        # Sanitize message history to remove orphaned ToolMessages
        # (can occur from improper context compaction or checkpoint corruption)
        messages = sanitize_message_history(messages)

        # Add full conversation history (excluding system messages, but keeping summaries)
        # We exclude regular SystemMessages because we regenerate the system prompt fresh,
        # but summary SystemMessages (from context compaction) should be included
        for msg in messages:
            if isinstance(msg, SystemMessage):
                # Include summary SystemMessages, exclude others
                if "[Summary of prior work]" in msg.content:
                    prepared_messages.append(msg)
            else:
                prepared_messages.append(msg)

        # Handle consecutive AI messages — inject dynamic todo reminder
        if prepared_messages and isinstance(prepared_messages[-1], AIMessage):
            if not getattr(prepared_messages[-1], 'tool_calls', None):
                remaining = todo_manager.list_pending()
                if remaining:
                    todo_lines = "\n".join(
                        f"  - {t.id}: {t.content}"
                        for t in remaining
                    )
                    reminder = (
                        f"You have {len(remaining)} incomplete todo(s):\n"
                        f"{todo_lines}\n\n"
                        "Continue working on these items. "
                        "Use `todo_complete(todo_id=\"<id>\")` to mark a todo as done, "
                        "or call `todo_complete()` with no arguments to complete the next pending item. "
                        "Use `todo_list()` to view the full list with completed and remaining todos."
                    )
                else:
                    reminder = "All todos are complete. Continue with the current task."
                prepared_messages.append(HumanMessage(content=reminder))

        # LAYER 1 SAFETY CHECK: Ensure we don't exceed model context limit
        # This catches bad configs and edge cases that slip through normal compaction
        total_tokens = context_mgr.get_token_count(prepared_messages)
        model_max = config.limits.model_max_context_tokens

        if total_tokens > model_max:
            logger.warning(
                f"[{job_id}] Pre-request safety triggered: {total_tokens} tokens exceeds "
                f"{model_max} limit. Forcing summarization."
            )

            # Force summarization on conversation messages (not system prompt)
            messages = await context_mgr.ensure_within_limits(
                messages,
                summarization_llm,
                summarization_prompt,
                oss_reasoning_level=oss_reasoning_level,
                max_summary_length=config.context_management.max_summary_length,
                force=True,
            )

            # Separate RemoveMessage markers from actual messages
            safety_remove_markers = [m for m in messages if isinstance(m, RemoveMessage)]
            messages = [m for m in messages if not isinstance(m, RemoveMessage)]

            # Rebuild prepared_messages with compacted history
            # Keep system prompt, replace conversation history
            system_msg = prepared_messages[0] if prepared_messages else None
            prepared_messages = []
            if system_msg and isinstance(system_msg, SystemMessage):
                prepared_messages.append(system_msg)

            # Add compacted conversation (including summary SystemMessages)
            for msg in messages:
                if isinstance(msg, SystemMessage):
                    if "[Summary of prior work]" in msg.content:
                        prepared_messages.append(msg)
                else:
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
        if auditor:
            llm_audit_id = auditor.audit_llm_call(
                job_id=job_id,
                agent_type=config.agent_id,
                iteration=iteration,
                model=config.llm.model,
                input_message_count=len(prepared_messages),
                state_message_count=len(messages),
                metadata=state.get("metadata"),
                phase=phase_str,
                phase_number=phase_number,
            )

        # Retry loop for LLM call with exponential backoff
        attempt = 0
        last_error = None

        while True:
            try:
                start_time = time.time()
                response = llm_with_tools.invoke(prepared_messages)
                latency_ms = int((time.time() - start_time) * 1000)

                tool_calls_count = len(response.tool_calls) if hasattr(response, 'tool_calls') and response.tool_calls else 0
                logger.info(f"[{job_id}] LLM response: {len(response.content)} chars, {tool_calls_count} tool calls")

                # Archive full LLM request/response to llm_requests collection
                request_id = None
                if auditor:
                    request_id = auditor.archive(
                        job_id=job_id,
                        agent_type=config.agent_id,
                        messages=prepared_messages,
                        response=response,
                        model=config.llm.model,
                        latency_ms=latency_ms,
                        iteration=iteration,
                        metadata=state.get("metadata"),
                        phase=phase_str,
                        phase_number=phase_number,
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
                        auditor.update_llm_response(
                            audit_doc_id=llm_audit_id,
                            request_id=request_id,
                            response_preview=response.content[:500] if response.content else "",
                            tool_calls=tool_calls_preview,
                            output_chars=len(response.content) if response.content else 0,
                            latency_ms=latency_ms,
                        )

                # Return compacted messages + response if compaction occurred,
                # otherwise just append the response (add_messages reducer handles this)
                if context_was_compacted:
                    # Include RemoveMessage markers so state reducer removes old messages
                    return {
                        "messages": remove_markers + messages + [response],
                        "iteration": iteration + 1,
                        "error": None,
                    }
                return {
                    "messages": [response],
                    "iteration": iteration + 1,
                    "error": None,
                }

            except Exception as e:
                last_error = e
                retry_manager.record_failure("llm_invoke")

                if retry_manager.should_retry("llm_invoke", attempt):
                    delay = retry_manager.get_retry_delay(attempt)
                    logger.warning(
                        f"[{job_id}] LLM error (attempt {attempt + 1}/{retry_manager.max_retries}), "
                        f"retrying in {delay:.1f}s: {e}"
                    )
                    retry_manager.record_retry()
                    time.sleep(delay)
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
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the check_todos node.

    This node checks if all todos are complete.
    """

    def check_todos(state: UniversalAgentState) -> Dict[str, Any]:
        """Check if all todos are complete."""
        job_id = state.get("job_id", "unknown")
        iteration = state.get("iteration", 0)

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
                strategic_todos = get_initial_strategic_todos(config)
            else:
                # Transition strategic phase (between tactical phases)
                strategic_todos = get_transition_strategic_todos(config)

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

        if all_complete:
            logger.info(f"[{job_id}] All todos complete")
            return {"phase_complete": True}

        return {"phase_complete": False}

    return check_todos


def create_archive_phase_node(
    todo_manager: TodoManager,
    plan_manager: PlanManager,
    config: AgentConfig,
    context_mgr: ContextManager,
    llm: BaseChatModel,
    summarization_prompt: str,
    snapshot_manager: Optional[PhaseSnapshotManager] = None,
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the archive_phase node.

    This node archives completed todos and marks phase complete.
    Also performs context compaction if configured.
    Creates phase snapshots for recovery if snapshot_manager is provided.
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

        # Archive todos
        archive_path = todo_manager.archive(current_phase or "phase")

        # Mark phase complete in plan
        if current_phase:
            plan_manager.mark_phase_complete(current_phase)

        # Audit phase completion
        phase_str = "strategic" if is_strategic else "tactical"
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

        message = AIMessage(
            content=f"Phase complete. Archived todos to {archive_path}. Moving to next phase."
        )

        # Context compaction at phase boundary using unified method
        messages = state.get("messages", [])
        compacted_messages = None

        if config.context_management.compact_on_archive:
            oss_reasoning_level = config.context_management.reasoning_level or config.llm.reasoning_level or "high"

            # Force summarization when transitioning from strategic to tactical
            # This gives tactical phases a "fresh conversation" with just the plan summary
            force_summarize = is_strategic  # True when completing strategic phase

            compacted_messages = await context_mgr.ensure_within_limits(
                messages,
                llm,
                summarization_prompt,
                oss_reasoning_level=oss_reasoning_level,
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


def create_handle_transition_node(
    workspace: WorkspaceManager,
    todo_manager: TodoManager,
    config: AgentConfig,
    min_todos: int = 5,
    max_todos: int = 20,
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

    def handle_transition(state: UniversalAgentState) -> Dict[str, Any]:
        """Handle phase transition based on current mode."""
        job_id = state.get("job_id", "unknown")
        iteration = state.get("iteration", 0)
        is_strategic = state.get("is_strategic_phase", True)

        logger.info(
            f"[{job_id}] Handling phase transition from "
            f"{'strategic' if is_strategic else 'tactical'} phase"
        )

        # Call transition handler
        result = handle_phase_transition(
            state=state,
            workspace=workspace,
            todo_manager=todo_manager,
            min_todos=min_todos,
            max_todos=max_todos,
            config=config,
        )

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
        else:
            logger.warning(
                f"[{job_id}] Phase transition rejected: {result.error_message}"
            )

        return result.state_updates

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
    - Frozen state: job_frozen.json exists -> stop for human review (goal_achieved=False, should_stop=True)
    - Completed state: job_completion.json exists -> truly done (goal_achieved=True, should_stop=True)
    - Legacy plan completion
    """

    def check_goal(state: UniversalAgentState) -> Dict[str, Any]:
        """Check if overall goal is achieved."""
        job_id = state.get("job_id", "unknown")
        iteration = state.get("iteration", 0)
        is_strategic = state.get("is_strategic_phase", True)
        phase_number = state.get("phase_number", 0)
        phase_str = "strategic" if is_strategic else "tactical"

        # Check for frozen state (job awaiting human review)
        # job_complete now writes job_frozen.json instead of job_completion.json
        job_frozen_path = workspace.get_path("output/job_frozen.json")
        if job_frozen_path.exists():
            logger.info(f"[{job_id}] Job frozen for human review - stopping gracefully")

            # Audit frozen state
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
                            "decision": "frozen",
                            "goal_achieved": False,
                            "should_stop": True,
                            "reason": "job_frozen_for_review",
                        }
                    },
                    metadata=state.get("metadata"),
                    phase=phase_str,
                    phase_number=phase_number,
                )

            return {
                "goal_achieved": False,  # Not truly achieved yet
                "should_stop": True,     # But stop the loop for human review
            }

        # Check if job was approved (job_completion.json created by --approve command)
        # This file is only created when a human approves a frozen job
        job_completion_path = workspace.get_path("output/job_completion.json")
        if job_completion_path.exists():
            logger.info(f"[{job_id}] Goal achieved - job approved by human")

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
                            "reason": "job_approved_by_human",
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


def route_after_check_todos(state: UniversalAgentState) -> Literal["execute", "archive_phase"]:
    """Route based on whether todos are complete."""
    if state.get("phase_complete", False):
        return "archive_phase"
    return "execute"


def route_entry(state: UniversalAgentState) -> Literal["init_workspace", "execute"]:
    """Route at entry based on initialization state.

    If already initialized (resume), go directly to execute.
    Otherwise, start initialization flow.
    """
    if state.get("initialized", False):
        return "execute"
    return "init_workspace"


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

        IMPORTANT: If job is frozen (job_complete was called), always go to
        check_goal so the frozen state can be detected and the graph can stop.

        If transition was rejected (last message contains rejection marker),
        go back to execute so the agent can fix the issue. Otherwise,
        proceed to check_goal.
        """
        # Check if job is frozen - must go to check_goal to detect and stop
        job_frozen_path = workspace.get_path("output/job_frozen.json")
        if job_frozen_path.exists():
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
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create a tool node with audit logging.

    This wraps LangGraph's ToolNode to add MongoDB audit logging for
    tool calls and results.

    Args:
        tools: List of tool objects
        config: Agent configuration for agent_id

    Returns:
        A callable node function with audit logging
    """
    tool_node = ToolNode(tools)

    async def audited_tools(state: UniversalAgentState) -> Dict[str, Any]:
        """Execute tools with audit logging."""
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

        # Execute tools with timing (use ainvoke for async tool support)
        start_time = time.time()
        result = await tool_node.ainvoke(state)
        execution_time_ms = int((time.time() - start_time) * 1000)

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

        return result

    return audited_tools


# =============================================================================
# GRAPH BUILDER
# =============================================================================


def build_phase_alternation_graph(
    llm_with_tools: BaseChatModel,
    tools: List[Any],
    config: AgentConfig,
    workspace: WorkspaceManager,
    todo_manager: TodoManager,
    workspace_template: str = "",
    checkpointer: Optional[BaseCheckpointSaver] = None,
    summarization_llm: Optional[BaseChatModel] = None,
    snapshot_manager: Optional[PhaseSnapshotManager] = None,
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
        llm_with_tools: LLM with tools bound for execution
        tools: List of tool objects
        config: Agent configuration
        workspace: WorkspaceManager instance
        todo_manager: TodoManager instance (must be the same one used by tools)
        workspace_template: Template content for workspace.md
        checkpointer: Optional LangGraph checkpointer for state persistence.
            When provided, enables resume after crash using the same thread_id.
        summarization_llm: Optional LLM for context summarization at phase boundaries.
            If not provided, uses llm_with_tools for summarization.
        snapshot_manager: Optional PhaseSnapshotManager for creating phase snapshots.
            When provided, enables recovery to previous phases after corruption.

    Returns:
        Compiled StateGraph with checkpointing if checkpointer provided
    """
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
    context_mgr = ContextManager(config=context_config, model=config.llm.model)

    # Create retry manager for LLM call retries
    retry_manager = ToolRetryManager(max_retries=config.limits.tool_retry_count)

    # Load summarization prompt
    summarization_prompt = load_summarization_prompt(config)

    if not workspace_template:
        raise ValueError("workspace_template is required")

    # Use provided summarization LLM or fall back to main LLM
    llm_for_summarization = summarization_llm or llm_with_tools

    # Create graph
    workflow = StateGraph(UniversalAgentState)

    # Create nodes
    init_workspace = create_init_workspace_node(memory_manager, workspace_template, config)
    init_strategic_todos = create_init_strategic_todos_node(workspace, todo_manager, config)

    execute = create_execute_node(
        llm_with_tools, todo_manager, memory_manager, workspace,
        config, context_mgr, retry_manager,
        summarization_llm=llm_for_summarization,
        summarization_prompt=summarization_prompt,
    )
    check_todos = create_check_todos_node(todo_manager, config)
    archive_phase = create_archive_phase_node(
        todo_manager, plan_manager, config,
        context_mgr, llm_for_summarization, summarization_prompt,
        snapshot_manager=snapshot_manager,
    )

    handle_transition = create_handle_transition_node(
        workspace, todo_manager, config,
        min_todos=config.phase_settings.min_todos,
        max_todos=config.phase_settings.max_todos,
    )

    check_goal = create_check_goal_node(plan_manager, workspace, config, todo_manager)
    tool_node = create_audited_tool_node(tools, config)

    # Add nodes to graph
    workflow.add_node("init_workspace", init_workspace)
    workflow.add_node("init_strategic_todos", init_strategic_todos)
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
            "execute": "execute",
        },
    )

    # Wire initialization: init_workspace -> init_strategic_todos -> execute
    workflow.add_edge("init_workspace", "init_strategic_todos")
    workflow.add_edge("init_strategic_todos", "execute")

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
        llm_with_tools=llm_with_tools,
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
    async for state in graph.astream(graph_input, config=config):
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
