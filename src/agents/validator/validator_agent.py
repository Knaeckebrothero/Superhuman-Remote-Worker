"""Validator Agent for requirement validation and graph integration.

This module implements the Validator Agent, which:
- Polls the requirement_cache for pending requirements
- Validates requirements for relevance and duplicates
- Analyzes fulfillment against existing graph entities
- Integrates validated requirements into Neo4j
- Creates fulfillment relationships (FULFILLED_BY_*, NOT_FULFILLED_BY_*)

The agent uses LangGraph with durable execution (PostgresSaver) for crash recovery.
"""

import os
import logging
import uuid
import asyncio
from typing import Dict, Any, List, TypedDict, Annotated, Optional
from datetime import datetime

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langgraph.graph.message import add_messages

# PostgresSaver for durable execution
try:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    POSTGRES_SAVER_AVAILABLE = True
except ImportError:
    POSTGRES_SAVER_AVAILABLE = False
    AsyncPostgresSaver = None

from src.core.neo4j_utils import Neo4jConnection
from src.core.config import load_config
from src.agents.shared.context_manager import ContextManager, ContextConfig
from src.agents.shared.workspace import Workspace

logger = logging.getLogger(__name__)


# =============================================================================
# Agent State Definition
# =============================================================================


class ValidatorAgentState(TypedDict):
    """State for the Validator Agent."""

    # Core state
    messages: Annotated[List[BaseMessage], add_messages]
    job_id: str
    requirement_id: str
    requirement: Dict[str, Any]

    # Processing state
    current_phase: str  # understanding, relevance, fulfillment, planning, integration, documentation
    related_objects: List[Dict[str, Any]]
    related_messages: List[Dict[str, Any]]
    fulfillment_analysis: Dict[str, Any]
    planned_operations: List[Dict[str, Any]]

    # Output
    validation_result: Dict[str, Any]
    graph_changes: List[Dict[str, Any]]

    # Control
    iteration: int
    max_iterations: int
    error: Optional[Dict[str, Any]]
    should_stop: bool


# =============================================================================
# Validator Agent Class
# =============================================================================


class ValidatorAgent:
    """LangGraph-based agent for requirement validation and graph integration.

    The Validator Agent processes requirements from the PostgreSQL cache,
    validates them against the Neo4j knowledge graph, and integrates valid
    requirements as nodes with appropriate relationships.

    Phases:
    1. Understanding - Parse requirement, identify entities
    2. Relevance - Check domain relevance, find related graph entities
    3. Fulfillment - Analyze which entities fulfill the requirement
    4. Planning - Plan graph operations (nodes, relationships)
    5. Integration - Execute graph mutations with transaction safety
    6. Documentation - Update cache status, create citations

    Example:
        ```python
        agent = ValidatorAgent(neo4j_conn, postgres_conn)
        await agent.start()  # Starts polling loop
        ```
    """

    def __init__(
        self,
        neo4j_connection: Neo4jConnection,
        postgres_connection_string: Optional[str] = None,
        llm_model: Optional[str] = None,
        temperature: float = 0.0,
        reasoning_level: str = "medium",
        duplicate_threshold: float = 0.95,
        auto_integrate: bool = False,
        require_citations: bool = True,
        polling_interval: int = 10,
    ):
        """Initialize the Validator Agent.

        Args:
            neo4j_connection: Active Neo4j connection
            postgres_connection_string: PostgreSQL connection string (optional, uses DATABASE_URL)
            llm_model: LLM model name (optional, uses config)
            temperature: LLM temperature (0.0-1.0)
            reasoning_level: Reasoning depth (low, medium, high)
            duplicate_threshold: Similarity threshold for duplicate detection (0.0-1.0)
            auto_integrate: Auto-integrate high-confidence requirements without review
            require_citations: Require citations for all validation decisions
            polling_interval: Seconds between cache polls
        """
        self.neo4j = neo4j_connection
        self.postgres_connection_string = postgres_connection_string or os.getenv("DATABASE_URL")

        # Load configuration
        config = load_config("llm_config.json")
        agent_config = config.get("agent", {})
        validator_config = config.get("validator_agent", {})

        # LLM settings
        self.llm_model = llm_model or agent_config.get("model", "gpt-4o")
        self.temperature = temperature
        self.reasoning_level = reasoning_level
        self.max_iterations = agent_config.get("max_iterations", 100)

        # Validator settings
        self.duplicate_threshold = duplicate_threshold or validator_config.get("duplicate_threshold", 0.95)
        self.auto_integrate = auto_integrate or validator_config.get("auto_integrate", False)
        self.require_citations = require_citations if require_citations is not None else validator_config.get("require_citations", True)
        self.polling_interval = polling_interval or validator_config.get("polling_interval_seconds", 10)

        # Initialize LLM
        llm_kwargs = {
            "model": self.llm_model,
            "temperature": self.temperature,
            "api_key": os.getenv("OPENAI_API_KEY"),
        }
        base_url = os.getenv("LLM_BASE_URL")
        if base_url:
            llm_kwargs["base_url"] = base_url

        self.llm = ChatOpenAI(**llm_kwargs)

        # Initialize tools (imported here to avoid circular imports)
        from src.agents.validator.tools import ValidatorAgentTools
        self.tools_provider = ValidatorAgentTools(
            neo4j_connection=neo4j_connection,
            duplicate_threshold=self.duplicate_threshold,
        )
        self.tools = self.tools_provider.get_tools()

        # Bind tools to LLM
        self.llm_with_tools = self.llm.bind_tools(self.tools)

        # Context management
        context_config = config.get("context_management", {})
        self.context_manager = ContextManager(
            config=ContextConfig(
                compaction_threshold_tokens=context_config.get("compaction_threshold_tokens", 100_000),
                summarization_trigger_tokens=context_config.get("summarization_trigger_tokens", 128_000),
                keep_raw_turns=context_config.get("keep_raw_turns", 3),
                max_output_tokens=context_config.get("max_output_tokens", 80_000),
            )
        )

        # Build the graph
        self.graph = self._build_graph()

        # Workspace for working data
        self.workspace: Optional[Workspace] = None

        # State tracking
        self._shutdown_requested = False
        self._current_job_id: Optional[str] = None
        self._current_requirement_id: Optional[str] = None

        logger.info(f"ValidatorAgent initialized with model {self.llm_model}")

    def _get_reasoning_directive(self) -> str:
        """Build reasoning directive based on configured level."""
        level = self.reasoning_level if self.reasoning_level in ("low", "medium", "high") else "medium"
        return f"Reasoning: {level}"

    def _load_phase_prompt(self, phase: str) -> str:
        """Load phase-specific prompt from file."""
        prompt_path = f"config/prompts/validator_{phase}.txt"
        try:
            with open(prompt_path, "r") as f:
                return f.read()
        except FileNotFoundError:
            logger.warning(f"Prompt file not found: {prompt_path}")
            return ""

    # =========================================================================
    # LangGraph Nodes
    # =========================================================================

    def _initialize_node(self, state: ValidatorAgentState) -> ValidatorAgentState:
        """Initialize validation for a requirement."""
        requirement = state.get("requirement", {})
        reasoning = self._get_reasoning_directive()

        # Load understanding phase prompt
        phase_prompt = self._load_phase_prompt("understanding")
        if not phase_prompt:
            phase_prompt = """You are validating a requirement candidate for integration into a Neo4j knowledge graph.

Your task is to:
1. Understand the requirement's intent and scope
2. Identify mentioned business objects and messages
3. Assess GoBD/GDPR relevance
4. Prepare for graph exploration

Analyze the requirement and identify key entities to search for in the graph."""

        system_message = f"{reasoning}\n\n{phase_prompt}"

        user_message = f"""Validate this requirement candidate:

**Requirement Text:** {requirement.get('text', '')}
**Name:** {requirement.get('name', 'Unnamed')}
**Type:** {requirement.get('type', 'unknown')}
**Priority:** {requirement.get('priority', 'medium')}
**GoBD Relevant:** {requirement.get('gobd_relevant', False)}
**GDPR Relevant:** {requirement.get('gdpr_relevant', False)}
**Mentioned Objects:** {requirement.get('mentioned_objects', [])}
**Mentioned Messages:** {requirement.get('mentioned_messages', [])}
**Confidence:** {requirement.get('confidence', 0.0)}

Begin by understanding the requirement and identifying entities to search for."""

        state["messages"] = [
            SystemMessage(content=system_message),
            HumanMessage(content=user_message),
        ]
        state["current_phase"] = "understanding"
        state["iteration"] = 0
        state["related_objects"] = []
        state["related_messages"] = []
        state["fulfillment_analysis"] = {}
        state["planned_operations"] = []
        state["graph_changes"] = []
        state["validation_result"] = {}
        state["error"] = None
        state["should_stop"] = False

        return state

    def _process_node(self, state: ValidatorAgentState) -> ValidatorAgentState:
        """Main processing node - LLM decides actions."""
        # Apply context compaction if needed
        messages = state.get("messages", [])
        messages = self.context_manager.maybe_compact(messages)
        state["messages"] = messages

        # Add phase context
        phase = state.get("current_phase", "understanding")
        iteration = state.get("iteration", 0)
        max_iter = state.get("max_iterations", self.max_iterations)

        # Load phase-specific prompt
        phase_prompt = self._load_phase_prompt(phase)
        if phase_prompt:
            context_msg = f"Phase: {phase.upper()}\nIteration: {iteration}/{max_iter}\n\n{phase_prompt}"
        else:
            context_msg = f"Phase: {phase.upper()}\nIteration: {iteration}/{max_iter}\n\nContinue validation."

        state["messages"].append(HumanMessage(content=context_msg))

        # Get LLM response
        response = self.llm_with_tools.invoke(state["messages"])
        state["messages"].append(response)

        return state

    def _tool_node(self, state: ValidatorAgentState) -> ValidatorAgentState:
        """Execute tool calls from LLM."""
        tool_node = ToolNode(self.tools)
        return tool_node.invoke(state)

    def _check_completion_node(self, state: ValidatorAgentState) -> ValidatorAgentState:
        """Check if validation is complete and update phase."""
        state["iteration"] = state.get("iteration", 0) + 1

        messages = state.get("messages", [])
        if not messages:
            return state

        last_message = messages[-1]
        current_phase = state.get("current_phase", "understanding")

        # Check for tool calls - if present, continue processing
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return state

        # Check for completion signals in message content
        content = getattr(last_message, "content", "") or ""
        content_lower = content.lower()

        # Phase transition logic
        phase_transitions = {
            "understanding": "relevance",
            "relevance": "fulfillment",
            "fulfillment": "planning",
            "planning": "integration",
            "integration": "documentation",
            "documentation": None,  # End
        }

        # Check for phase completion signals
        completion_phrases = [
            "moving to",
            "proceeding to",
            "phase complete",
            "ready for",
            "validation complete",
            "integration complete",
            "requirement integrated",
            "requirement rejected",
        ]

        should_advance = any(phrase in content_lower for phrase in completion_phrases)

        if should_advance:
            next_phase = phase_transitions.get(current_phase)
            if next_phase:
                state["current_phase"] = next_phase
                logger.info(f"Advancing to phase: {next_phase}")
            else:
                state["should_stop"] = True
                logger.info("Validation workflow complete")

        # Check iteration limit
        if state.get("iteration", 0) >= state.get("max_iterations", self.max_iterations):
            state["should_stop"] = True
            state["error"] = {
                "type": "max_iterations_exceeded",
                "message": f"Exceeded maximum iterations ({state.get('max_iterations')})",
            }
            logger.warning("Max iterations exceeded")

        return state

    def _finalize_node(self, state: ValidatorAgentState) -> ValidatorAgentState:
        """Finalize validation and prepare results."""
        # Build validation result
        result = {
            "requirement_id": state.get("requirement_id"),
            "job_id": state.get("job_id"),
            "status": "completed" if not state.get("error") else "failed",
            "phases_completed": state.get("current_phase"),
            "iterations": state.get("iteration", 0),
            "related_objects": state.get("related_objects", []),
            "related_messages": state.get("related_messages", []),
            "fulfillment_analysis": state.get("fulfillment_analysis", {}),
            "graph_changes": state.get("graph_changes", []),
            "error": state.get("error"),
            "completed_at": datetime.utcnow().isoformat(),
        }

        state["validation_result"] = result
        logger.info(f"Validation finalized: {result['status']}")

        return state

    # =========================================================================
    # Graph Routing
    # =========================================================================

    def _should_continue(self, state: ValidatorAgentState) -> str:
        """Determine next step in the workflow."""
        # Check for errors
        if state.get("error"):
            return "finalize"

        # Check for explicit stop
        if state.get("should_stop"):
            return "finalize"

        messages = state.get("messages", [])
        if not messages:
            return "process"

        last_message = messages[-1]

        # Check for tool calls
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tools"

        # Check iteration limit
        if state.get("iteration", 0) >= state.get("max_iterations", self.max_iterations):
            return "finalize"

        # Continue processing
        return "process"

    # =========================================================================
    # Graph Construction
    # =========================================================================

    def _build_graph(self) -> StateGraph:
        """Build the LangGraph state machine."""
        workflow = StateGraph(ValidatorAgentState)

        # Add nodes
        workflow.add_node("initialize", self._initialize_node)
        workflow.add_node("process", self._process_node)
        workflow.add_node("tools", self._tool_node)
        workflow.add_node("check", self._check_completion_node)
        workflow.add_node("finalize", self._finalize_node)

        # Set entry point
        workflow.set_entry_point("initialize")

        # Add edges
        workflow.add_edge("initialize", "process")
        workflow.add_conditional_edges(
            "process",
            self._should_continue,
            {
                "tools": "tools",
                "process": "process",
                "finalize": "finalize",
            }
        )
        workflow.add_edge("tools", "check")
        workflow.add_conditional_edges(
            "check",
            self._should_continue,
            {
                "tools": "tools",
                "process": "process",
                "finalize": "finalize",
            }
        )
        workflow.add_edge("finalize", END)

        return workflow.compile()

    # =========================================================================
    # Public Methods
    # =========================================================================

    async def validate_requirement(
        self,
        requirement: Dict[str, Any],
        job_id: str,
        requirement_id: str,
    ) -> Dict[str, Any]:
        """Validate a single requirement.

        Args:
            requirement: Requirement data from cache
            job_id: Job UUID
            requirement_id: Requirement UUID

        Returns:
            Validation result dictionary
        """
        logger.info(f"Validating requirement {requirement_id} from job {job_id}")

        # Initialize state
        initial_state = ValidatorAgentState(
            messages=[],
            job_id=job_id,
            requirement_id=requirement_id,
            requirement=requirement,
            current_phase="understanding",
            related_objects=[],
            related_messages=[],
            fulfillment_analysis={},
            planned_operations=[],
            validation_result={},
            graph_changes=[],
            iteration=0,
            max_iterations=self.max_iterations,
            error=None,
            should_stop=False,
        )

        # Run the workflow
        final_state = self.graph.invoke(
            initial_state,
            config={"recursion_limit": 100}
        )

        return final_state.get("validation_result", {})

    async def validate_requirement_stream(
        self,
        requirement: Dict[str, Any],
        job_id: str,
        requirement_id: str,
    ):
        """Validate a requirement and yield state updates.

        Args:
            requirement: Requirement data
            job_id: Job UUID
            requirement_id: Requirement UUID

        Yields:
            State updates during validation
        """
        initial_state = ValidatorAgentState(
            messages=[],
            job_id=job_id,
            requirement_id=requirement_id,
            requirement=requirement,
            current_phase="understanding",
            related_objects=[],
            related_messages=[],
            fulfillment_analysis={},
            planned_operations=[],
            validation_result={},
            graph_changes=[],
            iteration=0,
            max_iterations=self.max_iterations,
            error=None,
            should_stop=False,
        )

        return self.graph.stream(
            initial_state,
            config={"recursion_limit": 100}
        )

    async def start_polling(self) -> None:
        """Start the requirement polling loop.

        Continuously polls the requirement_cache for pending requirements
        and validates them one at a time.
        """
        from src.core.postgres_utils import (
            create_postgres_connection,
            get_pending_requirement,
            update_requirement_status,
        )

        logger.info("Starting Validator Agent polling loop")

        pg_conn = create_postgres_connection(self.postgres_connection_string)
        await pg_conn.connect()

        try:
            while not self._shutdown_requested:
                try:
                    # Poll for pending requirement
                    requirement = await get_pending_requirement(pg_conn)

                    if requirement:
                        self._current_requirement_id = str(requirement["id"])
                        self._current_job_id = str(requirement["job_id"])

                        logger.info(f"Processing requirement {self._current_requirement_id}")

                        # Validate the requirement
                        result = await self.validate_requirement(
                            requirement=requirement,
                            job_id=self._current_job_id,
                            requirement_id=self._current_requirement_id,
                        )

                        # Update cache status based on result
                        if result.get("status") == "completed":
                            # Check if requirement was integrated or rejected
                            graph_changes = result.get("graph_changes", [])
                            if graph_changes:
                                # Get the created node ID
                                node_id = None
                                for change in graph_changes:
                                    if change.get("type") == "create_node":
                                        node_id = change.get("node_id")
                                        break

                                await update_requirement_status(
                                    pg_conn,
                                    uuid.UUID(self._current_requirement_id),
                                    status="integrated",
                                    validation_result=result,
                                    graph_node_id=node_id,
                                )
                            else:
                                # Rejected
                                await update_requirement_status(
                                    pg_conn,
                                    uuid.UUID(self._current_requirement_id),
                                    status="rejected",
                                    validation_result=result,
                                    rejection_reason=result.get("rejection_reason", "Validation rejected"),
                                )
                        else:
                            # Failed
                            await update_requirement_status(
                                pg_conn,
                                uuid.UUID(self._current_requirement_id),
                                status="failed",
                                error=str(result.get("error", "Unknown error")),
                            )

                        self._current_requirement_id = None
                        self._current_job_id = None
                    else:
                        # No pending requirements, wait before polling again
                        await asyncio.sleep(self.polling_interval)

                except Exception as e:
                    logger.error(f"Error in polling loop: {e}")
                    if self._current_requirement_id:
                        await update_requirement_status(
                            pg_conn,
                            uuid.UUID(self._current_requirement_id),
                            status="failed",
                            error=str(e),
                        )
                        self._current_requirement_id = None
                        self._current_job_id = None

                    # Wait before retrying
                    await asyncio.sleep(self.polling_interval)

        finally:
            await pg_conn.disconnect()
            logger.info("Validator Agent polling loop stopped")

    def request_shutdown(self) -> None:
        """Request graceful shutdown of the polling loop."""
        logger.info("Shutdown requested")
        self._shutdown_requested = True

    @property
    def current_status(self) -> Dict[str, Any]:
        """Get current agent status."""
        return {
            "agent": "validator",
            "model": self.llm_model,
            "current_job_id": self._current_job_id,
            "current_requirement_id": self._current_requirement_id,
            "shutdown_requested": self._shutdown_requested,
            "settings": {
                "duplicate_threshold": self.duplicate_threshold,
                "auto_integrate": self.auto_integrate,
                "require_citations": self.require_citations,
                "polling_interval": self.polling_interval,
            }
        }


# =============================================================================
# Factory Function
# =============================================================================


def create_validator_agent(
    neo4j_connection: Neo4jConnection,
    postgres_connection_string: Optional[str] = None,
) -> ValidatorAgent:
    """Create a Validator Agent with configuration from config file.

    Args:
        neo4j_connection: Active Neo4j connection
        postgres_connection_string: PostgreSQL connection string (optional)

    Returns:
        Configured ValidatorAgent instance
    """
    config = load_config("llm_config.json")
    agent_config = config.get("agent", {})
    validator_config = config.get("validator_agent", {})

    return ValidatorAgent(
        neo4j_connection=neo4j_connection,
        postgres_connection_string=postgres_connection_string,
        llm_model=agent_config.get("model"),
        temperature=agent_config.get("temperature", 0.0),
        reasoning_level=agent_config.get("reasoning_level", "medium"),
        duplicate_threshold=validator_config.get("duplicate_threshold", 0.95),
        auto_integrate=validator_config.get("auto_integrate", False),
        require_citations=validator_config.get("require_citations", True),
        polling_interval=validator_config.get("polling_interval_seconds", 10),
    )
