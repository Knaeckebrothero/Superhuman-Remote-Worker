"""
Universal Agent - Nested Loop Graph for Universal Agent.

Implements the nested loop graph architecture with:
- Initialization flow (runs once)
- Outer loop (strategic planning at phase transitions)
- Inner loop (tactical execution with ReAct)
- Goal check for termination

Graph Structure:
```
╔═══════════════════════════════════════════════════════════════════════════╗
║                         INITIALIZATION (runs once)                        ║
║                                                                           ║
║   init_workspace → read_instructions → create_plan → init_todos           ║
║                                                                           ║
╠═══════════════════════════════════════════════════════════════════════════╣
║                         OUTER LOOP (Strategic)                            ║
║                                                                           ║
║   ┌─────────────────────────────────────────────────────────────────┐     ║
║   │                      PLAN PHASE                                 │     ║
║   │   read_plan → update_memory → create_todos                      │     ║
║   └─────────────────────────────────────────────────────────────────┘     ║
║                                    ↓                                      ║
║   ┌─────────────────────────────────────────────────────────────────┐     ║
║   │                     EXECUTE PHASE (inner loop)                  │     ║
║   │                                                                 │     ║
║   │         ┌────────────────────────────────────────┐              │     ║
║   │         ↓                                        │              │     ║
║   │      execute ─→ check_todos ─→ todos done? ──no──┘              │     ║
║   │      (ReAct)           │                                        │     ║
║   │                       yes                                       │     ║
║   │                        ↓                                        │     ║
║   │                  archive_phase                                  │     ║
║   └─────────────────────────────────────────────────────────────────┘     ║
║                                    ↓                                      ║
║                               check_goal                                  ║
║                              ↓          ↓                                 ║
║                             no         yes                                ║
║                              ↓          ↓                                 ║
║                       back to PLAN     END                                ║
║                                                                           ║
╚═══════════════════════════════════════════════════════════════════════════╝
```
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
from langgraph.prebuilt import ToolNode

from .core.state import UniversalAgentState
from .core.loader import (
    AgentConfig,
    load_planning_prompt,
    load_todo_extraction_prompt,
    load_memory_update_prompt,
    load_summarization_prompt,
    render_system_prompt,
)
from .core.workspace import WorkspaceManager
from .core.archiver import get_archiver
from .core.context import ContextManager, ContextConfig, find_safe_slice_start
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
        **File: `main_plan.md`**
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


def create_read_instructions_node(
    workspace: WorkspaceManager,
    config: AgentConfig,
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the read_instructions node.

    This node reads instructions.md and adds it to the conversation.
    """

    def read_instructions(state: UniversalAgentState) -> Dict[str, Any]:
        """Read instructions.md into context."""
        job_id = state.get("job_id", "unknown")
        logger.info(f"[{job_id}] Reading instructions")

        try:
            instructions = workspace.read_file("instructions.md")
        except FileNotFoundError:
            instructions = "No instructions.md found. Please create a plan based on job metadata."
            logger.warning(f"[{job_id}] instructions.md not found")

        # Audit instructions read
        auditor = get_archiver()
        if auditor:
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="initialize",
                node_name="read_instructions",
                iteration=0,
                data={"instructions": {"length": len(instructions)}},
                metadata=state.get("metadata"),
            )

        # Add instructions as a human message
        message = HumanMessage(
            content=f"## Instructions\n\n{instructions}\n\nPlease create a plan to accomplish this task."
        )

        return {
            "messages": [message],
        }

    return read_instructions


def create_create_plan_node(
    llm: BaseChatModel,
    plan_manager: PlanManager,
    config: AgentConfig,
    planning_prompt: str = "",
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the create_plan node.

    This node uses the LLM to create main_plan.md from instructions.

    Args:
        llm: LLM instance for plan generation
        plan_manager: Plan manager for reading/writing plans
        config: Agent configuration
        planning_prompt: Prompt template for plan creation (loaded from file)
    """

    def create_plan(state: UniversalAgentState) -> Dict[str, Any]:
        """LLM creates main_plan.md from instructions."""
        job_id = state.get("job_id", "unknown")
        logger.info(f"[{job_id}] Creating plan")

        # Check if plan already exists (resuming job)
        if plan_manager.exists():
            logger.info(f"[{job_id}] Plan already exists, skipping creation")
            plan_content = plan_manager.read()
            # Add message indicating plan exists
            message = AIMessage(
                content=f"I found an existing plan. Let me review it:\n\n{plan_content[:1000]}..."
            )
            return {"messages": [message]}

        messages = state.get("messages", [])

        # Format planning prompt with reasoning level
        oss_reasoning_level = config.llm.reasoning_level or "high"
        prompt_text = planning_prompt.format(oss_reasoning_level=oss_reasoning_level)

        # Call LLM to create plan
        plan_messages = messages + [HumanMessage(content=prompt_text)]

        # Audit LLM call
        auditor = get_archiver()
        if auditor:
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="llm_call",
                node_name="create_plan",
                iteration=0,
                data={
                    "llm": {
                        "model": config.llm.model,
                        "input_message_count": len(plan_messages),
                    }
                },
                metadata=state.get("metadata"),
            )

        start_time = time.time()
        response = llm.invoke(plan_messages)
        latency_ms = int((time.time() - start_time) * 1000)

        # Archive full LLM request/response
        if auditor:
            auditor.archive(
                job_id=job_id,
                agent_type=config.agent_id,
                messages=plan_messages,
                response=response,
                model=config.llm.model,
                latency_ms=latency_ms,
                iteration=0,
                metadata=state.get("metadata"),
            )

            # Audit LLM response
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="llm_response",
                node_name="create_plan",
                iteration=0,
                data={
                    "llm": {
                        "model": config.llm.model,
                        "response_content_preview": response.content[:500] if response.content else "",
                        "metrics": {
                            "output_chars": len(response.content) if response.content else 0,
                        }
                    }
                },
                latency_ms=latency_ms,
                metadata=state.get("metadata"),
            )

        # Extract plan content from response, cleaning up any markdown wrappers
        plan_content = _extract_markdown_content(response.content)

        # Write plan to workspace
        plan_manager.write(plan_content)
        logger.info(f"[{job_id}] Created main_plan.md ({len(plan_content)} chars)")

        return {
            "messages": [response],
        }

    return create_plan


def create_init_todos_node(
    llm: BaseChatModel,
    plan_manager: PlanManager,
    todo_manager: TodoManager,
    config: AgentConfig,
    todo_extraction_prompt: str = "",
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the init_todos node.

    This node extracts todos for the first phase from the plan.

    Args:
        llm: LLM instance for todo extraction
        plan_manager: Plan manager for reading plan
        todo_manager: Todo manager for writing todos
        config: Agent configuration
        todo_extraction_prompt: Prompt template with {current_phase} and {plan_content} vars
    """

    def init_todos(state: UniversalAgentState) -> Dict[str, Any]:
        """Extract todos for first phase from plan."""
        job_id = state.get("job_id", "unknown")
        logger.info(f"[{job_id}] Initializing todos from plan")

        plan_content = plan_manager.read()
        current_phase = plan_manager.get_current_phase()

        if not current_phase:
            current_phase = "Phase 1"

        # Format with reasoning level
        oss_reasoning_level = config.llm.reasoning_level or "high"
        extraction_prompt = todo_extraction_prompt.format(
            current_phase=current_phase,
            plan_content=plan_content,
            oss_reasoning_level=oss_reasoning_level,
        )

        messages = state.get("messages", [])
        todo_messages = messages + [HumanMessage(content=extraction_prompt)]

        # Audit LLM call
        auditor = get_archiver()
        if auditor:
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="llm_call",
                node_name="init_todos",
                iteration=0,
                data={
                    "llm": {
                        "model": config.llm.model,
                        "input_message_count": len(todo_messages),
                    },
                    "phase": {"current": current_phase},
                },
                metadata=state.get("metadata"),
            )

        start_time = time.time()
        response = llm.invoke(todo_messages)
        latency_ms = int((time.time() - start_time) * 1000)

        # Archive full LLM request/response
        if auditor:
            auditor.archive(
                job_id=job_id,
                agent_type=config.agent_id,
                messages=todo_messages,
                response=response,
                model=config.llm.model,
                latency_ms=latency_ms,
                iteration=0,
                metadata=state.get("metadata"),
            )

            # Audit LLM response
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="llm_response",
                node_name="init_todos",
                iteration=0,
                data={
                    "llm": {
                        "model": config.llm.model,
                        "response_content_preview": response.content[:500] if response.content else "",
                        "metrics": {
                            "output_chars": len(response.content) if response.content else 0,
                        }
                    }
                },
                latency_ms=latency_ms,
                metadata=state.get("metadata"),
            )

        # Parse todos from response
        import json
        import re

        try:
            # Extract JSON from response
            json_match = re.search(r'\[.*\]', response.content, re.DOTALL)
            if json_match:
                todos_data = json.loads(json_match.group())
            else:
                # Fallback: create a generic first todo
                todos_data = [{"content": f"Complete {current_phase}", "priority": "high"}]
        except json.JSONDecodeError:
            todos_data = [{"content": f"Complete {current_phase}", "priority": "high"}]
            logger.warning(f"[{job_id}] Failed to parse todos, using fallback")

        # Add todos to manager
        todo_manager.clear()
        for todo_data in todos_data:
            todo_manager.add(
                content=todo_data.get("content", "Unknown task"),
                priority=todo_data.get("priority", "medium"),
            )

        logger.info(f"[{job_id}] Created {len(todos_data)} todos for {current_phase}")

        # Create confirmation message
        todo_list = todo_manager.format_for_display()
        message = AIMessage(
            content=f"I've created todos for {current_phase}:\n\n{todo_list}\n\nLet me start working on these."
        )

        return {
            "messages": [message],
            "initialized": True,
            "phase_complete": False,
            "goal_achieved": False,
        }

    return init_todos


# =============================================================================
# PLAN PHASE NODES (Outer Loop)
# =============================================================================


def create_read_plan_node(
    plan_manager: PlanManager,
    memory_manager: MemoryManager,
    config: AgentConfig,
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the read_plan node.

    This node reads main_plan.md at the start of each phase.
    """

    def read_plan(state: UniversalAgentState) -> Dict[str, Any]:
        """Read plan and memory at phase transition."""
        job_id = state.get("job_id", "unknown")
        iteration = state.get("iteration", 0)
        logger.info(f"[{job_id}] Reading plan for new phase")

        plan_content = plan_manager.read()
        current_phase = plan_manager.get_current_phase()

        # Also refresh workspace memory
        workspace_memory = memory_manager.read()

        # Audit phase transition
        auditor = get_archiver()
        if auditor:
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="phase_transition",
                node_name="read_plan",
                iteration=iteration,
                data={"phase": {"current": current_phase}},
                metadata=state.get("metadata"),
            )

        # Add phase context message
        message = HumanMessage(
            content=f"## Phase Transition\n\nCurrent phase: {current_phase or 'Unknown'}\n\nReview the plan and prepare todos for this phase."
        )

        return {
            "messages": [message],
            "workspace_memory": workspace_memory,
            "phase_complete": False,  # Reset for new phase
        }

    return read_plan


def create_update_memory_node(
    llm: BaseChatModel,
    memory_manager: MemoryManager,
    config: AgentConfig,
    memory_update_prompt: str = "",
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the update_memory node.

    This node has the LLM update workspace.md with learnings.

    Args:
        llm: LLM instance for memory updates
        memory_manager: Memory manager for reading/writing workspace.md
        config: Agent configuration
        memory_update_prompt: Prompt template with {current_memory} var
    """

    def update_memory(state: UniversalAgentState) -> Dict[str, Any]:
        """LLM updates workspace.md with learnings from last phase."""
        job_id = state.get("job_id", "unknown")
        iteration = state.get("iteration", 0)
        logger.info(f"[{job_id}] Updating workspace memory")

        current_memory = memory_manager.read()
        messages = state.get("messages", [])

        # Format with reasoning level
        oss_reasoning_level = config.llm.reasoning_level or "high"
        update_prompt = memory_update_prompt.format(
            current_memory=current_memory,
            oss_reasoning_level=oss_reasoning_level,
        )

        # Slice messages safely to avoid orphaning ToolMessages
        target_start = max(0, len(messages) - 10)
        safe_start = find_safe_slice_start(messages, target_start)
        memory_messages = messages[safe_start:] + [HumanMessage(content=update_prompt)]

        # Audit LLM call
        auditor = get_archiver()
        if auditor:
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="llm_call",
                node_name="update_memory",
                iteration=iteration,
                data={
                    "llm": {
                        "model": config.llm.model,
                        "input_message_count": len(memory_messages),
                    }
                },
                metadata=state.get("metadata"),
            )

        start_time = time.time()
        response = llm.invoke(memory_messages)
        latency_ms = int((time.time() - start_time) * 1000)

        # Archive full LLM request/response
        if auditor:
            auditor.archive(
                job_id=job_id,
                agent_type=config.agent_id,
                messages=memory_messages,
                response=response,
                model=config.llm.model,
                latency_ms=latency_ms,
                iteration=iteration,
                metadata=state.get("metadata"),
            )

            # Audit LLM response
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="llm_response",
                node_name="update_memory",
                iteration=iteration,
                data={
                    "llm": {
                        "model": config.llm.model,
                        "response_content_preview": response.content[:500] if response.content else "",
                        "metrics": {
                            "output_chars": len(response.content) if response.content else 0,
                        }
                    }
                },
                latency_ms=latency_ms,
                metadata=state.get("metadata"),
            )

        # Update memory file
        new_memory = response.content
        memory_manager.write(new_memory)
        logger.info(f"[{job_id}] Updated workspace.md")

        return {
            "workspace_memory": new_memory,
        }

    return update_memory


def create_create_todos_node(
    llm: BaseChatModel,
    plan_manager: PlanManager,
    todo_manager: TodoManager,
    config: AgentConfig,
    todo_extraction_prompt: str = "",
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the create_todos node.

    This node extracts todos for the current phase.

    Args:
        llm: LLM instance for todo extraction
        plan_manager: Plan manager for reading plan
        todo_manager: Todo manager for writing todos
        config: Agent configuration
        todo_extraction_prompt: Prompt template with {current_phase} and {plan_content} vars
    """

    def create_todos(state: UniversalAgentState) -> Dict[str, Any]:
        """Extract todos for current phase from plan."""
        job_id = state.get("job_id", "unknown")
        iteration = state.get("iteration", 0)
        logger.info(f"[{job_id}] Creating todos for current phase")

        plan_content = plan_manager.read()
        current_phase = plan_manager.get_current_phase()

        if not current_phase:
            logger.info(f"[{job_id}] No more phases found, goal achieved")
            return {
                "goal_achieved": True,
            }

        # Format with reasoning level
        oss_reasoning_level = config.llm.reasoning_level or "high"
        extraction_prompt = todo_extraction_prompt.format(
            current_phase=current_phase,
            plan_content=plan_content,
            oss_reasoning_level=oss_reasoning_level,
        )

        messages = state.get("messages", [])
        # Slice messages safely to avoid orphaning ToolMessages
        target_start = max(0, len(messages) - 5)
        safe_start = find_safe_slice_start(messages, target_start)
        todo_messages = messages[safe_start:] + [HumanMessage(content=extraction_prompt)]

        # Audit LLM call
        auditor = get_archiver()
        if auditor:
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="llm_call",
                node_name="create_todos",
                iteration=iteration,
                data={
                    "llm": {
                        "model": config.llm.model,
                        "input_message_count": len(todo_messages),
                    },
                    "phase": {"current": current_phase},
                },
                metadata=state.get("metadata"),
            )

        start_time = time.time()
        response = llm.invoke(todo_messages)
        latency_ms = int((time.time() - start_time) * 1000)

        # Archive full LLM request/response
        if auditor:
            auditor.archive(
                job_id=job_id,
                agent_type=config.agent_id,
                messages=todo_messages,
                response=response,
                model=config.llm.model,
                latency_ms=latency_ms,
                iteration=iteration,
                metadata=state.get("metadata"),
            )

            # Audit LLM response
            auditor.audit_step(
                job_id=job_id,
                agent_type=config.agent_id,
                step_type="llm_response",
                node_name="create_todos",
                iteration=iteration,
                data={
                    "llm": {
                        "model": config.llm.model,
                        "response_content_preview": response.content[:500] if response.content else "",
                        "metrics": {
                            "output_chars": len(response.content) if response.content else 0,
                        }
                    }
                },
                latency_ms=latency_ms,
                metadata=state.get("metadata"),
            )

        # Parse todos
        import json
        import re

        try:
            json_match = re.search(r'\[.*\]', response.content, re.DOTALL)
            if json_match:
                todos_data = json.loads(json_match.group())
            else:
                todos_data = [{"content": f"Complete {current_phase}", "priority": "high"}]
        except json.JSONDecodeError:
            todos_data = [{"content": f"Complete {current_phase}", "priority": "high"}]

        # Add todos
        todo_manager.clear()
        for todo_data in todos_data:
            todo_manager.add(
                content=todo_data.get("content", "Unknown task"),
                priority=todo_data.get("priority", "medium"),
            )

        logger.info(f"[{job_id}] Created {len(todos_data)} todos for {current_phase}")

        todo_list = todo_manager.format_for_display()
        message = AIMessage(
            content=f"Todos for {current_phase}:\n\n{todo_list}"
        )

        return {
            "messages": [message],
        }

    return create_todos


# =============================================================================
# EXECUTE PHASE NODES (Inner Loop)
# =============================================================================


def create_execute_node(
    llm_with_tools: BaseChatModel,
    todo_manager: TodoManager,
    memory_manager: MemoryManager,
    workspace_manager: WorkspaceManager,
    system_prompt_template: str,
    config: AgentConfig,
    context_mgr: ContextManager,
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the execute node.

    This is the main ReAct execution node that processes todos.
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

        # Render system prompt with current todos and workspace
        full_system = render_system_prompt(
            template=system_prompt_template,
            config=config,
            todos_content=todos_content,
            workspace_content=workspace_content,
        )
        prepared_messages.append(SystemMessage(content=full_system))

        # Always clear old tool results, keep last 10
        messages = context_mgr.clear_old_tool_results(messages)

        # Add conversation history (keep recent), ensuring we don't orphan ToolMessages
        # Find a safe slice boundary that doesn't split AIMessage/ToolMessage pairs
        target_start = max(0, len(messages) - 20)
        safe_start = find_safe_slice_start(messages, target_start)
        for msg in messages[safe_start:]:
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
            logger.error(f"[{job_id}] LLM error: {e}")

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


# =============================================================================
# GOAL CHECK NODE
# =============================================================================


def create_check_goal_node(
    plan_manager: PlanManager,
    config: AgentConfig,
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the check_goal node.

    This node checks if the overall goal is achieved.
    """

    def check_goal(state: UniversalAgentState) -> Dict[str, Any]:
        """Check if overall goal is achieved."""
        job_id = state.get("job_id", "unknown")
        iteration = state.get("iteration", 0)

        # Check if plan is complete
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

        # Check if there's a next phase
        next_phase = plan_manager.get_current_phase()
        if not next_phase:
            logger.info(f"[{job_id}] No more phases - goal achieved")

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


def route_after_init(state: UniversalAgentState) -> Literal["read_plan", "execute"]:
    """Route after initialization based on whether we have todos."""
    # After init, go directly to execute (todos already created)
    return "execute"


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


def route_after_check_goal(state: UniversalAgentState) -> Literal["read_plan", "end"]:
    """Route based on whether goal is achieved."""
    if state.get("goal_achieved", False) or state.get("should_stop", False):
        return "end"
    return "read_plan"


def route_entry(state: UniversalAgentState) -> Literal["init_workspace", "read_plan"]:
    """Route at entry based on initialization state."""
    if state.get("initialized", False):
        return "read_plan"
    return "init_workspace"


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

    def audited_tools(state: UniversalAgentState) -> Dict[str, Any]:
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

        # Execute tools with timing
        start_time = time.time()
        result = tool_node.invoke(state)
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


def build_nested_loop_graph(
    llm: BaseChatModel,
    llm_with_tools: BaseChatModel,
    tools: List[Any],
    config: AgentConfig,
    system_prompt_template: str,
    workspace: WorkspaceManager,
    workspace_template: str = "",
) -> StateGraph:
    """Build the nested loop graph for the Universal Agent.

    Creates the full nested loop architecture with:
    - Initialization flow (first run only)
    - Outer loop (plan phase transitions)
    - Inner loop (ReAct execution)
    - Goal check

    Args:
        llm: LLM for planning/memory updates (no tools)
        llm_with_tools: LLM with tools bound for execution
        tools: List of tool objects
        config: Agent configuration
        system_prompt_template: Raw system prompt template with placeholders
        workspace: WorkspaceManager instance
        workspace_template: Template content for workspace.md

    Returns:
        Compiled StateGraph
    """
    # Create managers
    todo_manager = TodoManager(workspace)
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

    # Load prompts from files
    planning_prompt = load_planning_prompt()
    todo_extraction_prompt = load_todo_extraction_prompt()
    memory_update_prompt = load_memory_update_prompt()
    summarization_prompt = load_summarization_prompt(config)

    if not workspace_template:
        raise ValueError("workspace_template is required")

    # Create graph
    workflow = StateGraph(UniversalAgentState)

    # Create nodes
    # Initialization
    init_workspace = create_init_workspace_node(memory_manager, workspace_template, config)
    read_instructions = create_read_instructions_node(workspace, config)
    create_plan = create_create_plan_node(llm, plan_manager, config, planning_prompt)
    init_todos = create_init_todos_node(llm, plan_manager, todo_manager, config, todo_extraction_prompt)

    # Plan phase
    read_plan = create_read_plan_node(plan_manager, memory_manager, config)
    update_memory = create_update_memory_node(llm, memory_manager, config, memory_update_prompt)
    create_todos = create_create_todos_node(llm, plan_manager, todo_manager, config, todo_extraction_prompt)

    # Execute phase
    execute = create_execute_node(
        llm_with_tools, todo_manager, memory_manager, workspace,
        system_prompt_template, config, context_mgr
    )
    check_todos = create_check_todos_node(todo_manager, config)
    archive_phase = create_archive_phase_node(
        todo_manager, plan_manager, config,
        context_mgr, llm, summarization_prompt
    )

    # Goal check
    check_goal = create_check_goal_node(plan_manager, config)

    # Tools node (with audit logging)
    tool_node = create_audited_tool_node(tools, config)

    # Add nodes to graph
    workflow.add_node("init_workspace", init_workspace)
    workflow.add_node("read_instructions", read_instructions)
    workflow.add_node("create_plan", create_plan)
    workflow.add_node("init_todos", init_todos)

    workflow.add_node("read_plan", read_plan)
    workflow.add_node("update_memory", update_memory)
    workflow.add_node("create_todos", create_todos)

    workflow.add_node("execute", execute)
    workflow.add_node("tools", tool_node)
    workflow.add_node("check_todos", check_todos)
    workflow.add_node("archive_phase", archive_phase)

    workflow.add_node("check_goal", check_goal)

    # Set conditional entry point
    workflow.set_conditional_entry_point(
        route_entry,
        {
            "init_workspace": "init_workspace",
            "read_plan": "read_plan",
        },
    )

    # Wire initialization sequence
    workflow.add_edge("init_workspace", "read_instructions")
    workflow.add_edge("read_instructions", "create_plan")
    workflow.add_edge("create_plan", "init_todos")
    workflow.add_edge("init_todos", "execute")  # Go directly to execute after init

    # Wire plan phase (outer loop entry)
    workflow.add_edge("read_plan", "update_memory")
    workflow.add_edge("update_memory", "create_todos")
    workflow.add_conditional_edges(
        "create_todos",
        lambda s: "check_goal" if s.get("goal_achieved") else "execute",
        {
            "execute": "execute",
            "check_goal": "check_goal",
        },
    )

    # Wire execute phase (inner loop)
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

    # Wire phase completion to goal check
    workflow.add_edge("archive_phase", "check_goal")

    # Wire goal check (outer loop exit or continue)
    workflow.add_conditional_edges(
        "check_goal",
        route_after_check_goal,
        {
            "read_plan": "read_plan",
            "end": END,
        },
    )

    return workflow.compile()


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
