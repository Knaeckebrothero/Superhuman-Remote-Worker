"""Universal Agent State.

Defines the minimal state structure for the workspace-centric agent architecture.
This state is simpler than the phase-based states used by Creator/Validator agents
because the agent manages its workflow through workspace files rather than state fields.
"""

from typing import Any, Dict, List, Optional, Annotated

from typing_extensions import TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class UniversalAgentState(TypedDict):
    """Minimal state for workspace-centric autonomous agent.

    Unlike phase-based agents, the Universal Agent:
    - Uses workspace files for strategic planning (plans/main_plan.md)
    - Uses TodoManager for tactical execution (in-memory, archived to workspace)
    - Tracks minimal state - just what's needed for graph execution

    The agent discovers what to do by reading instructions.md and maintains
    progress through workspace files and todos, not through state fields.

    Attributes:
        messages: Conversation history with automatic deduplication
        job_id: Unique identifier for the current job
        workspace_path: Path to the job's workspace directory
        iteration: Current LLM iteration count (for limits)
        error: Error information if something went wrong
        should_stop: Flag to signal workflow termination
        metadata: Optional job-specific metadata (document path, etc.)
        context_stats: Statistics from context management operations
        tool_retry_state: State for tool retry tracking
    """

    # Core LangGraph state - messages are automatically merged/deduped
    messages: Annotated[List[BaseMessage], add_messages]

    # Job identification
    job_id: str

    # Workspace path (relative to workspace base)
    workspace_path: str

    # Execution control
    iteration: int
    error: Optional[Dict[str, Any]]
    should_stop: bool
    consecutive_llm_errors: int

    # Job metadata (flexible, agent-type specific)
    # For Creator: document_path, prompt, etc.
    # For Validator: requirement_id, requirement_data, etc.
    metadata: Dict[str, Any]

    # Context management state (Phase 6)
    # Tracks token counts, compaction operations, summaries generated
    context_stats: Optional[Dict[str, Any]]

    # Tool retry state (Phase 6)
    # Tracks failed tool calls and retry attempts
    tool_retry_state: Optional[Dict[str, Any]]


def create_initial_state(
    job_id: str,
    workspace_path: str,
    metadata: Optional[Dict[str, Any]] = None
) -> UniversalAgentState:
    """Create an initial state for a new job.

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
        messages=[],
        job_id=job_id,
        workspace_path=workspace_path,
        iteration=0,
        error=None,
        should_stop=False,
        consecutive_llm_errors=0,
        metadata=metadata or {},
        context_stats=None,
        tool_retry_state=None,
    )
