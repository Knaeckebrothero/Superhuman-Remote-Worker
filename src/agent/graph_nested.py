"""Nested Loop Graph for Universal Agent.

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
║   init_workspace → read_instructions → create_plan → init_todos          ║
║                                                                           ║
╠═══════════════════════════════════════════════════════════════════════════╣
║                         OUTER LOOP (Strategic)                            ║
║                                                                           ║
║   ┌─────────────────────────────────────────────────────────────────┐    ║
║   │                      PLAN PHASE                                  │    ║
║   │   read_plan → update_memory → create_todos                       │    ║
║   └─────────────────────────────────────────────────────────────────┘    ║
║                                    ↓                                      ║
║   ┌─────────────────────────────────────────────────────────────────┐    ║
║   │                     EXECUTE PHASE (inner loop)                   │    ║
║   │                                                                  │    ║
║   │         ┌────────────────────────────────────────┐              │    ║
║   │         ↓                                        │              │    ║
║   │      execute ──→ check_todos ──→ todos done? ──no──┘            │    ║
║   │      (ReAct)           │                                        │    ║
║   │                       yes                                       │    ║
║   │                        ↓                                        │    ║
║   │                  archive_phase                                  │    ║
║   └─────────────────────────────────────────────────────────────────┘    ║
║                                    ↓                                      ║
║                            check_goal                                     ║
║                             ↓          ↓                                  ║
║                            no         yes                                 ║
║                             ↓          ↓                                  ║
║                    back to PLAN       END                                ║
║                                                                           ║
╚═══════════════════════════════════════════════════════════════════════════╝
```
"""

import logging
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
from .core.loader import AgentConfig
from .core.workspace import WorkspaceManager
from .managers import TodoManager, PlanManager, MemoryManager

logger = logging.getLogger(__name__)


# =============================================================================
# INITIALIZATION NODES
# =============================================================================


def create_init_workspace_node(
    memory_manager: MemoryManager,
    workspace_template: str,
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the init_workspace node.

    This node initializes workspace.md from template if it doesn't exist.
    """

    def init_workspace(state: UniversalAgentState) -> Dict[str, Any]:
        """Initialize workspace.md from template."""
        job_id = state.get("job_id", "unknown")
        logger.info(f"[{job_id}] Initializing workspace")

        if not memory_manager.exists():
            memory_manager.write(workspace_template)
            logger.info(f"[{job_id}] Created workspace.md from template")
        else:
            logger.debug(f"[{job_id}] workspace.md already exists")

        # Read workspace into state for system prompt injection
        workspace_memory = memory_manager.read()

        return {
            "workspace_memory": workspace_memory,
        }

    return init_workspace


def create_read_instructions_node(
    workspace: WorkspaceManager,
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
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the create_plan node.

    This node uses the LLM to create main_plan.md from instructions.
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

        # Create planning prompt
        planning_prompt = """Based on the instructions, create a structured plan in markdown format.

The plan should have:
1. A clear goal statement
2. Numbered phases (## Phase 1, ## Phase 2, etc.)
3. Each phase should have specific, actionable steps
4. Mark initial status as "pending" for each phase

Format example:
```markdown
# Plan: [Goal Summary]

## Goal
[Clear statement of what needs to be accomplished]

## Phase 1: [Phase Name]
Status: pending

Steps:
- [ ] Step 1
- [ ] Step 2

## Phase 2: [Phase Name]
Status: pending

Steps:
- [ ] Step 1
- [ ] Step 2
```

Create the plan now. Be specific and actionable."""

        # Call LLM to create plan
        plan_messages = messages + [HumanMessage(content=planning_prompt)]
        response = llm.invoke(plan_messages)

        # Extract plan content from response
        plan_content = response.content

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
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the init_todos node.

    This node extracts todos for the first phase from the plan.
    """

    def init_todos(state: UniversalAgentState) -> Dict[str, Any]:
        """Extract todos for first phase from plan."""
        job_id = state.get("job_id", "unknown")
        logger.info(f"[{job_id}] Initializing todos from plan")

        plan_content = plan_manager.read()
        current_phase = plan_manager.get_current_phase()

        if not current_phase:
            current_phase = "Phase 1"

        # Create todo extraction prompt
        extraction_prompt = f"""Based on this plan, extract the specific todos for {current_phase}.

Plan:
{plan_content}

List the todos as a JSON array with this format:
[
  {{"content": "Todo description", "priority": "high|medium|low"}},
  ...
]

Only include todos for {current_phase}. Be specific and actionable.
Return ONLY the JSON array, no other text."""

        messages = state.get("messages", [])
        todo_messages = messages + [HumanMessage(content=extraction_prompt)]

        response = llm.invoke(todo_messages)

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
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the read_plan node.

    This node reads main_plan.md at the start of each phase.
    """

    def read_plan(state: UniversalAgentState) -> Dict[str, Any]:
        """Read plan and memory at phase transition."""
        job_id = state.get("job_id", "unknown")
        logger.info(f"[{job_id}] Reading plan for new phase")

        plan_content = plan_manager.read()
        current_phase = plan_manager.get_current_phase()

        # Also refresh workspace memory
        workspace_memory = memory_manager.read()

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
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the update_memory node.

    This node has the LLM update workspace.md with learnings.
    """

    def update_memory(state: UniversalAgentState) -> Dict[str, Any]:
        """LLM updates workspace.md with learnings from last phase."""
        job_id = state.get("job_id", "unknown")
        logger.info(f"[{job_id}] Updating workspace memory")

        current_memory = memory_manager.read()
        messages = state.get("messages", [])

        # Create memory update prompt
        update_prompt = f"""Review the recent work and update the workspace memory.

Current workspace.md:
{current_memory}

Update the following sections as needed:
- Current State: Update phase and status
- Accomplishments: Add any completed work
- Key Decisions: Record important decisions made
- Notes: Add relevant observations

Return the complete updated workspace.md content.
Keep the same markdown structure. Be concise."""

        memory_messages = messages[-10:] + [HumanMessage(content=update_prompt)]
        response = llm.invoke(memory_messages)

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
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the create_todos node.

    This node extracts todos for the current phase.
    """

    def create_todos(state: UniversalAgentState) -> Dict[str, Any]:
        """Extract todos for current phase from plan."""
        job_id = state.get("job_id", "unknown")
        logger.info(f"[{job_id}] Creating todos for current phase")

        plan_content = plan_manager.read()
        current_phase = plan_manager.get_current_phase()

        if not current_phase:
            logger.info(f"[{job_id}] No more phases found, goal achieved")
            return {
                "goal_achieved": True,
            }

        # Create todo extraction prompt
        extraction_prompt = f"""Extract specific todos for {current_phase} from this plan:

{plan_content}

Return a JSON array:
[{{"content": "Task description", "priority": "high|medium|low"}}]

Only include todos for {current_phase}. Return ONLY the JSON array."""

        messages = state.get("messages", [])
        response = llm.invoke(messages[-5:] + [HumanMessage(content=extraction_prompt)])

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
    system_prompt: str,
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

        # System prompt with workspace memory
        workspace_memory = state.get("workspace_memory", "")
        full_system = f"{system_prompt}\n\n## Workspace Context\n\n{workspace_memory}"
        prepared_messages.append(SystemMessage(content=full_system))

        # Add todo context
        todo_display = todo_manager.format_for_display()
        prepared_messages.append(SystemMessage(content=f"\n{todo_display}\n"))

        # Add conversation history (keep recent)
        for msg in messages[-20:]:
            if not isinstance(msg, SystemMessage):
                prepared_messages.append(msg)

        # Handle consecutive AI messages
        if prepared_messages and isinstance(prepared_messages[-1], AIMessage):
            if not getattr(prepared_messages[-1], 'tool_calls', None):
                prepared_messages.append(
                    HumanMessage(content="Continue with the current task.")
                )

        try:
            response = llm_with_tools.invoke(prepared_messages)

            tool_calls = len(response.tool_calls) if hasattr(response, 'tool_calls') and response.tool_calls else 0
            logger.info(f"[{job_id}] LLM response: {len(response.content)} chars, {tool_calls} tool calls")

            return {
                "messages": [response],
                "iteration": iteration + 1,
                "error": None,
            }

        except Exception as e:
            logger.error(f"[{job_id}] LLM error: {e}")
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

        if all_complete:
            logger.info(f"[{job_id}] All todos complete")
            return {"phase_complete": True}

        return {"phase_complete": False}

    return check_todos


def create_archive_phase_node(
    todo_manager: TodoManager,
    plan_manager: PlanManager,
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the archive_phase node.

    This node archives completed todos and marks phase complete.
    """

    def archive_phase(state: UniversalAgentState) -> Dict[str, Any]:
        """Archive todos and mark phase complete in plan."""
        job_id = state.get("job_id", "unknown")

        current_phase = plan_manager.get_current_phase()
        logger.info(f"[{job_id}] Archiving phase: {current_phase}")

        # Archive todos
        archive_path = todo_manager.archive(current_phase or "phase")

        # Mark phase complete in plan
        if current_phase:
            plan_manager.mark_phase_complete(current_phase)

        message = AIMessage(
            content=f"Phase complete. Archived todos to {archive_path}. Moving to next phase."
        )

        return {
            "messages": [message],
        }

    return archive_phase


# =============================================================================
# GOAL CHECK NODE
# =============================================================================


def create_check_goal_node(
    plan_manager: PlanManager,
) -> Callable[[UniversalAgentState], Dict[str, Any]]:
    """Create the check_goal node.

    This node checks if the overall goal is achieved.
    """

    def check_goal(state: UniversalAgentState) -> Dict[str, Any]:
        """Check if overall goal is achieved."""
        job_id = state.get("job_id", "unknown")

        # Check if plan is complete
        is_complete = plan_manager.is_complete()

        if is_complete:
            logger.info(f"[{job_id}] Goal achieved - plan complete")
            return {
                "goal_achieved": True,
                "should_stop": True,
            }

        # Check if there's a next phase
        next_phase = plan_manager.get_current_phase()
        if not next_phase:
            logger.info(f"[{job_id}] No more phases - goal achieved")
            return {
                "goal_achieved": True,
                "should_stop": True,
            }

        logger.info(f"[{job_id}] Goal not achieved, next phase: {next_phase}")
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
# GRAPH BUILDER
# =============================================================================


def build_nested_loop_graph(
    llm: BaseChatModel,
    llm_with_tools: BaseChatModel,
    tools: List[Any],
    config: AgentConfig,
    system_prompt: str,
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
        system_prompt: Base system prompt
        workspace: WorkspaceManager instance
        workspace_template: Template content for workspace.md

    Returns:
        Compiled StateGraph
    """
    # Create managers
    todo_manager = TodoManager(workspace)
    plan_manager = PlanManager(workspace)
    memory_manager = MemoryManager(workspace)

    # Default workspace template
    if not workspace_template:
        workspace_template = """# Workspace Memory

This file is your persistent memory across context compaction.
Update it with important information as you work.

## Current State

Phase: 1
Status: Starting

## Accomplishments

(None yet)

## Key Decisions

(None yet)

## Notes

(Working notes)
"""

    # Create graph
    workflow = StateGraph(UniversalAgentState)

    # Create nodes
    # Initialization
    init_workspace = create_init_workspace_node(memory_manager, workspace_template)
    read_instructions = create_read_instructions_node(workspace)
    create_plan = create_create_plan_node(llm, plan_manager)
    init_todos = create_init_todos_node(llm, plan_manager, todo_manager)

    # Plan phase
    read_plan = create_read_plan_node(plan_manager, memory_manager)
    update_memory = create_update_memory_node(llm, memory_manager)
    create_todos = create_create_todos_node(llm, plan_manager, todo_manager)

    # Execute phase
    execute = create_execute_node(llm_with_tools, todo_manager, memory_manager, system_prompt)
    check_todos = create_check_todos_node(todo_manager)
    archive_phase = create_archive_phase_node(todo_manager, plan_manager)

    # Goal check
    check_goal = create_check_goal_node(plan_manager)

    # Tools node
    tool_node = ToolNode(tools)

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
