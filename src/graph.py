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
from .core.context import ContextManager, ContextConfig, ToolRetryManager, find_safe_slice_start
from .core.phase import (
    handle_phase_transition,
    TransitionResult,
    get_initial_strategic_todos,
)
from .managers import TodoManager, PlanManager, MemoryManager

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
    """

    def execute(state: UniversalAgentState) -> Dict[str, Any]:
        """Execute current todo using ReAct pattern."""
        job_id = state.get("job_id", "unknown")
        iteration = state.get("iteration", 0)
        messages = state.get("messages", [])

        logger.debug(f"[{job_id}] Execute iteration {iteration}")

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

        # Always clear old tool results, keep last 10
        messages = context_mgr.clear_old_tool_results(messages)

        # Add full conversation history (excluding system messages)
        for msg in messages:
            if not isinstance(msg, SystemMessage):
                prepared_messages.append(msg)

        # Handle consecutive AI messages
        if prepared_messages and isinstance(prepared_messages[-1], AIMessage):
            if not getattr(prepared_messages[-1], 'tool_calls', None):
                prepared_messages.append(
                    HumanMessage(content="Continue with the current task.")
                )

        # Audit LLM call
        auditor = get_archiver()
        if auditor:
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="llm_call",
                node_name="execute",
                iteration=iteration,
                data={
                    "llm": {
                        "model": config.llm.model,
                        "input_message_count": len(prepared_messages),
                    },
                    "state": {
                        "message_count": len(messages),
                    }
                },
                metadata=state.get("metadata"),
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

                # Archive full LLM request/response
                if auditor:
                    auditor.archive(
                        job_id=job_id,
                        agent_type=config.agent_id,
                        messages=prepared_messages,
                        response=response,
                        model=config.llm.model,
                        latency_ms=latency_ms,
                        iteration=iteration,
                        metadata=state.get("metadata"),
                    )

                    # Build tool calls preview
                    tool_calls_preview = []
                    if hasattr(response, 'tool_calls') and response.tool_calls:
                        for tc in response.tool_calls:
                            tool_calls_preview.append({
                                "name": tc.get("name", "unknown"),
                                "call_id": tc.get("id", ""),
                            })

                    # Audit LLM response
                    auditor.audit_step(
                        job_id=job_id,
                        agent_type=config.agent_id,
                        step_type="llm_response",
                        node_name="execute",
                        iteration=iteration,
                        data={
                            "llm": {
                                "model": config.llm.model,
                                "response_content_preview": response.content[:500] if response.content else "",
                                "tool_calls": tool_calls_preview,
                                "metrics": {
                                    "output_chars": len(response.content) if response.content else 0,
                                    "tool_call_count": tool_calls_count,
                                }
                            }
                        },
                        latency_ms=latency_ms,
                        metadata=state.get("metadata"),
                    )

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
        max_iterations = state.get("max_iterations", 500)

        # Check iteration limit
        if iteration >= max_iterations:
            logger.warning(f"[{job_id}] Max iterations reached")

            # Audit iteration limit error
            auditor = get_archiver()
            if auditor:
                auditor.audit_step(
                    job_id=job_id,
                    agent_type=config.agent_id,
                    step_type="error",
                    node_name="check_todos",
                    iteration=iteration,
                    data={
                        "error": {
                            "type": "iteration_limit",
                            "message": f"Max iterations ({max_iterations}) reached",
                            "recoverable": False,
                        }
                    },
                    metadata=state.get("metadata"),
                )

            return {
                "should_stop": True,
                "error": {
                    "message": f"Max iterations ({max_iterations}) reached",
                    "type": "iteration_limit",
                },
            }

        # Validate todos exist before checking completion
        todos = todo_manager.list_all()
        if not todos:
            logger.warning(f"[{job_id}] No todos loaded - phase cannot be complete")
            return {"phase_complete": False}

        # Check todos
        all_complete = todo_manager.all_complete()
        todo_manager.log_state()

        # Audit check decision
        auditor = get_archiver()
        if auditor:
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="check",
                node_name="check_todos",
                iteration=iteration,
                data={
                    "check": {
                        "decision": "phase_complete" if all_complete else "continue",
                        "todos_complete": all_complete,
                    }
                },
                metadata=state.get("metadata"),
            )

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
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the archive_phase node.

    This node archives completed todos and marks phase complete.
    Also performs context compaction if configured.
    """

    def archive_phase(state: UniversalAgentState) -> Dict[str, Any]:
        """Archive todos and mark phase complete in plan."""
        job_id = state.get("job_id", "unknown")
        iteration = state.get("iteration", 0)

        current_phase = plan_manager.get_current_phase()
        logger.info(f"[{job_id}] Archiving phase: {current_phase}")

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
            )

        message = AIMessage(
            content=f"Phase complete. Archived todos to {archive_path}. Moving to next phase."
        )

        # Context compaction at phase boundary
        messages = state.get("messages", [])
        compacted_messages = None

        if config.context_management.compact_on_archive:
            if context_mgr.should_summarize(messages):
                import asyncio
                oss_reasoning_level = config.llm.reasoning_level or "high"

                # Run async summarization
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                compacted_messages = loop.run_until_complete(
                    context_mgr.summarize_and_compact(
                        messages,
                        llm,
                        summarization_prompt,
                        oss_reasoning_level,
                    )
                )
                logger.info(
                    f"[{job_id}] Compacted context: {len(messages)} -> {len(compacted_messages)} messages"
                )

        # Return with compacted messages if compaction occurred
        if compacted_messages is not None:
            return {
                "messages": compacted_messages + [message],
            }

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
            )

        if result.success:
            logger.info(
                f"[{job_id}] Phase transition successful: "
                f"phase_number={result.state_updates.get('phase_number')}"
            )
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
    Supports both legacy plan completion and phase alternation job_complete.
    """

    def check_goal(state: UniversalAgentState) -> Dict[str, Any]:
        """Check if overall goal is achieved."""
        job_id = state.get("job_id", "unknown")
        iteration = state.get("iteration", 0)

        # Phase alternation: check if job_complete was called
        # (writes output/job_completion.json)
        job_completion_path = workspace.get_path("output/job_completion.json")
        if job_completion_path.exists():
            logger.info(f"[{job_id}] Goal achieved - job_complete called")

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
                            "reason": "job_complete_called",
                        }
                    },
                    metadata=state.get("metadata"),
                )

            return {
                "goal_achieved": True,
                "should_stop": True,
            }

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
                )

            return {
                "goal_achieved": True,
                "should_stop": True,
            }

        # Check if there are pending todos - if so, goal is NOT achieved
        pending_todos = todo_manager.list_pending()
        if pending_todos:
            logger.info(f"[{job_id}] Goal not achieved - {len(pending_todos)} pending todos")
            return {"goal_achieved": False}

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


def route_after_transition(
    state: UniversalAgentState,
) -> Literal["execute", "check_goal"]:
    """Route after phase transition based on success/failure.

    If transition was rejected (messages contain error), go back to execute
    so the agent can fix the issue. If transition succeeded (messages cleared),
    proceed to check_goal.
    """
    messages = state.get("messages", [])

    # If messages is empty, transition succeeded and cleared history
    if not messages:
        return "check_goal"

    # If messages exist, check if it's a rejection error
    if messages:
        last_msg = messages[-1]
        content = getattr(last_msg, "content", "") or ""
        if "[TRANSITION_REJECTED]" in content:
            return "execute"

    # Default: proceed to goal check
    return "check_goal"


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

        # Audit tool calls before execution
        auditor = get_archiver()
        if auditor:
            for tc_info in tool_calls_info:
                auditor.audit_tool_call(
                    job_id=job_id,
                    agent_type=config.agent_id,
                    iteration=iteration,
                    tool_name=tc_info["name"],
                    call_id=tc_info["call_id"],
                    arguments=tc_info["args"],
                    metadata=state.get("metadata"),
                )

        # Execute tools with timing (use ainvoke for async tool support)
        start_time = time.time()
        result = await tool_node.ainvoke(state)
        execution_time_ms = int((time.time() - start_time) * 1000)

        # Audit tool results
        if auditor and "messages" in result:
            call_id_to_info = {tc["call_id"]: tc for tc in tool_calls_info}
            for msg in result["messages"]:
                if isinstance(msg, ToolMessage):
                    call_id = getattr(msg, "tool_call_id", "")
                    tc_info = call_id_to_info.get(call_id, {})
                    content = msg.content if msg.content else ""
                    is_error = _is_tool_error(content)

                    auditor.audit_tool_result(
                        job_id=job_id,
                        agent_type=config.agent_id,
                        iteration=iteration,
                        tool_name=tc_info.get("name", getattr(msg, "name", "unknown")),
                        call_id=call_id,
                        result=content,
                        success=not is_error,
                        latency_ms=execution_time_ms // max(len(tool_calls_info), 1),
                        error=content[:500] if is_error else None,
                        metadata=state.get("metadata"),
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
        keep_recent_messages=10,
        keep_recent_tool_results=10,
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
    )
    check_todos = create_check_todos_node(todo_manager, config)
    archive_phase = create_archive_phase_node(
        todo_manager, plan_manager, config,
        context_mgr, llm_for_summarization, summarization_prompt
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
    workflow.add_edge("archive_phase", "handle_transition")
    workflow.add_conditional_edges(
        "handle_transition",
        route_after_transition,
        {
            "execute": "execute",  # Transition rejected, agent fixes issue
            "check_goal": "check_goal",  # Transition succeeded
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
    initial_state: UniversalAgentState,
    config: Dict[str, Any],
):
    """Run the graph with streaming output.

    Yields state updates as the graph executes.

    Args:
        graph: Compiled graph
        initial_state: Initial state
        config: LangGraph config (thread_id, recursion_limit, etc.)

    Yields:
        State updates from each node
    """
    async for state in graph.astream(initial_state, config=config):
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
