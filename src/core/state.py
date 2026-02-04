"""Universal Agent State.

Defines the state structure for the nested loop graph architecture.
The state supports:
- Initialization flow (runs once at job start)
- Outer loop (strategic planning at phase transitions)
- Inner loop (tactical execution with todos)

File-based memory (workspace.md, plan.md) provides persistence
across context compaction, while state fields control loop flow.
"""

from typing import Any, Dict, List, Optional, Annotated

from typing_extensions import TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class UniversalAgentState(TypedDict):
    """State for nested loop graph architecture.

    The Universal Agent uses a nested loop structure:
    1. Initialization: Set up workspace, read instructions, create plan
    2. Outer loop: Read plan, update memory, create todos for phase
    3. Inner loop: Execute todos until phase complete
    4. Goal check: Continue outer loop or end

    File-based context:
    - workspace.md: Long-term memory, always in system prompt
    - plan.md: Strategic direction, read at phase transitions
    - archive/: Completed todos by phase

    Attributes:
        messages: Conversation history with automatic deduplication
        job_id: Unique identifier for the current job
        workspace_path: Path to the job's workspace directory

        # Loop control (nested loop architecture)
        initialized: Whether initialization has completed
        phase_complete: Inner loop exit condition (all todos done)
        goal_achieved: Outer loop exit condition (plan complete)
        iteration: Current iteration count (for tracking/logging)

        # Phase alternation (strategic/tactical mode)
        is_strategic_phase: True = strategic mode (planning), False = tactical mode (execution)
        phase_number: Increments at each phase transition (for tracking/logging)
        is_final_phase: True when job_complete was called, job completes when todos done

        # File-based context
        workspace_memory: Contents of workspace.md for system prompt

        # Execution control
        error: Error information if something went wrong
        should_stop: Flag to signal workflow termination
        consecutive_llm_errors: Count of consecutive LLM failures

        # Job metadata
        metadata: Job-specific data (document_path, prompt, etc.)

        # Context management
        context_stats: Token counts and compaction statistics
        tool_retry_state: Failed tool call tracking

        # Legacy (for backwards compatibility)
        phase_transition: Old phase transition state
    """

    # Core LangGraph state - messages are automatically merged/deduped
    messages: Annotated[List[BaseMessage], add_messages]

    # Job identification
    job_id: str
    workspace_path: str

    # Loop control (nested loop architecture)
    initialized: bool                    # Has initialization completed
    phase_complete: bool                 # Inner loop exit: all todos done
    goal_achieved: bool                  # Outer loop exit: plan complete
    iteration: int                       # Current iteration count

    # Phase alternation (strategic/tactical mode)
    is_strategic_phase: bool             # True = strategic mode, False = tactical mode
    phase_number: int                    # Increments at each phase transition
    is_final_phase: bool                 # True when job_complete called, awaiting todo completion

    # File-based context (read from workspace into state)
    workspace_memory: str                # Contents of workspace.md

    # Execution control
    error: Optional[Dict[str, Any]]
    should_stop: bool
    consecutive_llm_errors: int

    # Job metadata (flexible, agent-type specific)
    # For Creator: document_path, prompt, etc.
    # For Validator: requirement_id, requirement_data, etc.
    metadata: Dict[str, Any]

    # Context management state
    # Tracks token counts, compaction operations, summaries generated
    context_stats: Optional[Dict[str, Any]]

    # Tool retry state
    # Tracks failed tool calls and retry attempts
    tool_retry_state: Optional[Dict[str, Any]]

    # Phase transition state (legacy, for backwards compatibility)
    # Set when a phase transition is triggered by todo_complete or todo_rewind
    # Contains: transition_type, trigger_summarization, metadata
    phase_transition: Optional[Dict[str, Any]]

    # Todo persistence (for checkpoint/resume)
    # These fields sync TodoManager state to LangGraph checkpoints
    todos: Optional[List[Dict[str, Any]]]
    staged_todos: Optional[List[Dict[str, Any]]]
    todo_next_id: Optional[int]


def create_initial_state(
    job_id: str,
    workspace_path: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> UniversalAgentState:
    """Create an initial state for a new job.

    The state is initialized for the nested loop graph:
    - initialized=False: Triggers initialization flow
    - phase_complete=False: Inner loop will run
    - goal_achieved=False: Outer loop will continue
    - workspace_memory="": Will be populated from workspace.md

    Args:
        job_id: Unique job identifier
        workspace_path: Path to job workspace
        metadata: Optional job-specific data

    Returns:
        Initial UniversalAgentState ready for graph invocation

    Example:
        ```python
        state = create_initial_state(
            job_id="abc123",
            workspace_path="job_abc123",
            metadata={"document_path": "/data/doc.pdf"}
        )
        result = await graph.ainvoke(state)
        ```
    """
    return UniversalAgentState(
        # Core
        messages=[],
        job_id=job_id,
        workspace_path=workspace_path,

        # Loop control
        initialized=False,
        phase_complete=False,
        goal_achieved=False,
        iteration=0,

        # Phase alternation (start in strategic mode)
        is_strategic_phase=True,
        phase_number=0,
        is_final_phase=False,

        # File-based context
        workspace_memory="",

        # Execution control
        error=None,
        should_stop=False,
        consecutive_llm_errors=0,

        # Metadata
        metadata=metadata or {},

        # Context management
        context_stats=None,
        tool_retry_state=None,

        # Legacy
        phase_transition=None,

        # Todo persistence
        todos=None,
        staged_todos=None,
        todo_next_id=1,
    )
