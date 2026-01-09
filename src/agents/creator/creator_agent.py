"""Creator Agent - Document Processing and Requirement Extraction.

This module implements the Creator Agent, a long-running autonomous agent that:
1. Processes documents (PDF, DOCX, TXT, HTML)
2. Extracts requirement candidates using LLM analysis
3. Enriches candidates with web/graph research
4. Writes citation-backed requirements to the PostgreSQL cache

The agent is designed for durable execution with automatic checkpointing
and context window management for extended operations.
"""

import os
import logging
import uuid
from typing import Dict, Any, List, TypedDict, Annotated, Optional
from datetime import datetime, timedelta

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langgraph.graph.message import add_messages
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from src.core.config import (
    load_config,
    load_prompt,
    get_project_root,
    get_creator_max_iterations,
    get_creator_recursion_limit,
)
from src.core.postgres_utils import (
    PostgresConnection,
    create_postgres_connection,
    create_requirement,
    get_job,
    update_job_status,
)
from src.agents.shared.context_manager import ContextManager, ContextConfig
from src.agents.shared.workspace_manager import WorkspaceManager
from src.agents.shared.todo_manager import TodoManager

logger = logging.getLogger(__name__)


# =============================================================================
# Agent State Definition
# =============================================================================

class CreatorAgentState(TypedDict):
    """State for the Creator Agent.

    The state tracks the agent's progress through document processing,
    candidate extraction, and requirement formulation phases.
    """
    # Core LangGraph state
    messages: Annotated[List[BaseMessage], add_messages]

    # Job context
    job_id: str
    job: Dict[str, Any]  # Full job record from database

    # Processing state
    current_phase: str  # preprocessing, identification, research, formulation, output
    document_path: str
    document_metadata: Dict[str, Any]
    document_chunks: List[Dict[str, Any]]

    # Candidate tracking
    candidates: List[Dict[str, Any]]
    current_candidate_index: int
    processed_candidates: int

    # Output tracking
    requirements_created: List[str]  # IDs of created requirements

    # Control flow
    iteration: int
    max_iterations: int
    error: Optional[Dict[str, Any]]
    should_stop: bool


# =============================================================================
# Creator Agent
# =============================================================================

class CreatorAgent:
    """LangGraph-based Creator Agent for requirement extraction.

    The Creator Agent is responsible for the first half of the two-agent
    requirement processing pipeline. It:

    1. Polls PostgreSQL for new jobs
    2. Processes attached documents
    3. Extracts requirement candidates using LLM analysis
    4. Enriches candidates with web search and graph context
    5. Formulates citation-backed requirements
    6. Writes requirements to the shared cache

    The agent uses durable execution with PostgreSQL checkpointing,
    allowing it to resume from failures and handle multi-day operations.

    Example:
        ```python
        agent = CreatorAgent()
        await agent.start()

        # Agent runs continuously, polling for jobs
        # To process a specific job:
        result = await agent.process_job(job_id)
        ```
    """

    def __init__(
        self,
        postgres_conn: Optional[PostgresConnection] = None,
        config: Optional[Dict[str, Any]] = None
    ):
        """Initialize the Creator Agent.

        Args:
            postgres_conn: PostgreSQL connection for job queue and cache.
                          If not provided, creates from DATABASE_URL.
            config: Configuration overrides. Defaults to llm_config.json.
        """
        # Load configuration
        full_config = load_config("llm_config.json")
        self.config = {**full_config.get("creator_agent", {}), **(config or {})}

        # Database connection
        self.postgres_conn = postgres_conn

        # LLM setup
        llm_kwargs = {
            "model": self.config.get("model", "gpt-oss-120b"),
            "temperature": self.config.get("temperature", 0.0),
            "api_key": os.getenv("OPENAI_API_KEY"),
        }
        base_url = os.getenv("LLM_BASE_URL")
        if base_url:
            llm_kwargs["base_url"] = base_url

        self.llm = ChatOpenAI(**llm_kwargs)

        # Context management
        context_config = full_config.get("context_management", {})
        self.context_manager = ContextManager(
            config=ContextConfig(
                compaction_threshold_tokens=context_config.get("compaction_threshold_tokens", 100_000),
                summarization_trigger_tokens=context_config.get("summarization_trigger_tokens", 128_000),
                keep_raw_turns=context_config.get("keep_raw_turns", 3),
                max_output_tokens=context_config.get("max_output_tokens", 80_000),
            )
        )

        # Initialize tools (imported separately)
        from src.agents.creator.tools import CreatorAgentTools
        self.tools_provider = CreatorAgentTools(
            postgres_conn=self.postgres_conn,
            config=self.config
        )
        self.tools = self.tools_provider.get_tools()

        # Bind tools to LLM
        self.llm_with_tools = self.llm.bind_tools(self.tools)

        # Build the graph
        self.graph = self._build_graph()

        # Checkpointer (set on first use)
        self._checkpointer = None

        # Runtime state
        self.current_job_id: Optional[str] = None
        self.workspace: Optional[WorkspaceManager] = None
        self.todo_manager: Optional[TodoManager] = None

        logger.info("Creator Agent initialized")

    async def _get_checkpointer(self) -> AsyncPostgresSaver:
        """Get or create PostgreSQL checkpointer."""
        if self._checkpointer is None:
            connection_string = os.getenv("DATABASE_URL")
            self._checkpointer = AsyncPostgresSaver.from_conn_string(connection_string)
            await self._checkpointer.setup()
        return self._checkpointer

    async def _ensure_connection(self) -> None:
        """Ensure PostgreSQL connection is established."""
        if self.postgres_conn is None:
            self.postgres_conn = create_postgres_connection()
        if not self.postgres_conn.is_connected:
            await self.postgres_conn.connect()

    def _get_system_prompt(self, phase: str) -> str:
        """Get the system prompt for the current phase.

        Args:
            phase: Current processing phase

        Returns:
            System prompt string
        """
        # Try to load phase-specific prompt
        try:
            prompt = load_prompt(f"creator_{phase}.txt")
            return prompt
        except FileNotFoundError:
            pass

        # Fall back to generic creator prompt
        return self._get_default_system_prompt(phase)

    def _get_default_system_prompt(self, phase: str) -> str:
        """Generate default system prompt for a phase."""
        reasoning_level = self.config.get("reasoning_level", "high")

        base_prompt = f"""You are the Creator Agent, part of a two-agent system for requirement traceability.

Your role is to analyze documents and extract well-formed, citation-backed requirements.

## Current Phase: {phase.upper()}

## Available Tools
You have tools for:
- Document extraction and chunking
- Web search (Tavily) for research
- Graph querying for similar requirements
- Citation creation
- Requirement cache writing

## Domain Context
You are working with a car rental business system, with focus on:
- GoBD compliance (German accounting/retention requirements)
- GDPR compliance (data protection)
- Business process traceability

## GoBD Indicators
Watch for requirements related to:
- Aufbewahrung (retention), Nachvollziehbarkeit (traceability)
- Unveränderbarkeit (immutability), Revisionssicherheit (audit-proof)
- Protokollierung (logging), Archivierung (archiving)

## Reasoning Level: {reasoning_level}
{"Think through each step carefully and explain your reasoning." if reasoning_level == "high" else "Be concise but thorough."}

## Important Guidelines
1. Always cite your sources - every claim needs a citation
2. When uncertain, use research tools to gather more context
3. Check for duplicate/similar requirements in the graph before creating new ones
4. Focus on actionable, testable requirements
5. Preserve traceability to source documents
"""
        return base_prompt

    # =========================================================================
    # Graph Nodes
    # =========================================================================

    async def _initialize_node(self, state: CreatorAgentState) -> CreatorAgentState:
        """Initialize processing for a job.

        Sets up workspace, loads job context, and prepares for document processing.
        """
        job_id = state["job_id"]
        logger.info(f"Initializing Creator Agent for job {job_id}")

        # Load job details
        await self._ensure_connection()
        job = await get_job(self.postgres_conn, uuid.UUID(job_id))

        if not job:
            state["error"] = {"message": f"Job {job_id} not found", "recoverable": False}
            state["should_stop"] = True
            return state

        state["job"] = dict(job)
        state["document_path"] = job.get("document_path", "")
        state["current_phase"] = "preprocessing"
        state["candidates"] = []
        state["current_candidate_index"] = 0
        state["processed_candidates"] = 0
        state["requirements_created"] = []
        state["iteration"] = 0
        state["should_stop"] = False

        # Initialize workspace
        self.workspace = WorkspaceManager(job_id=job_id)

        # Initialize todo manager
        self.todo_manager = TodoManager(self.workspace)

        # Update job status
        await update_job_status(
            self.postgres_conn,
            uuid.UUID(job_id),
            creator_status="processing"
        )

        # Set up initial message
        system_prompt = self._get_system_prompt("preprocessing")
        user_msg = f"""Process the following job:

Job ID: {job_id}
Prompt: {job.get('prompt', 'Extract requirements from document')}
Document: {state['document_path'] or 'No document attached'}
Context: {job.get('context', {})}

Begin by:
1. If a document is provided, extract and chunk it
2. Identify requirement candidates
3. Research each candidate for context
4. Formulate final requirements with citations
5. Write requirements to the cache

Start processing now."""

        state["messages"] = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_msg),
        ]

        return state

    async def _process_node(self, state: CreatorAgentState) -> CreatorAgentState:
        """Main processing node - orchestrates LLM-driven workflow."""
        state["iteration"] = state.get("iteration", 0) + 1

        # Check iteration limit
        max_iterations = state.get("max_iterations", self.config.get("max_iterations_per_candidate", 50))
        if state["iteration"] > max_iterations:
            logger.warning(f"Max iterations ({max_iterations}) reached for job {state['job_id']}")
            state["should_stop"] = True
            return state

        # Apply context management
        hook = self.context_manager.create_pre_model_hook()
        processed = hook(state)
        messages_to_send = processed.get("llm_input_messages", state["messages"])

        # Handle consecutive AIMessages - add continuation prompt if needed
        # Some LLM APIs reject requests with 2+ assistant messages at the end
        if messages_to_send:
            last_msg = messages_to_send[-1]
            if isinstance(last_msg, AIMessage) and not (hasattr(last_msg, "tool_calls") and last_msg.tool_calls):
                continuation = HumanMessage(content="Continue with the task. Use the available tools to make progress, or indicate if the task is complete.")
                messages_to_send = list(messages_to_send) + [continuation]
                state["messages"].append(continuation)
                logger.debug(f"Added continuation prompt after AIMessage without tool calls")

        # Get LLM response
        try:
            response = await self.llm_with_tools.ainvoke(messages_to_send)
            state["messages"].append(response)
        except Exception as e:
            logger.error(f"LLM error in job {state['job_id']}: {e}")
            state["error"] = {"message": str(e), "recoverable": True}
            # Don't stop - allow retry

        return state

    async def _tool_node(self, state: CreatorAgentState) -> CreatorAgentState:
        """Execute tool calls from LLM response."""
        messages = state["messages"]
        last_message = messages[-1] if messages else None

        if not last_message or not hasattr(last_message, "tool_calls") or not last_message.tool_calls:
            return state

        # Execute tools using ToolNode
        tool_node = ToolNode(self.tools)
        tool_result = await tool_node.ainvoke(state)

        # Update messages with tool results
        if "messages" in tool_result:
            state["messages"] = tool_result["messages"]

        return state

    async def _check_completion_node(self, state: CreatorAgentState) -> CreatorAgentState:
        """Check if processing is complete and prepare for output."""
        messages = state["messages"]
        last_message = messages[-1] if messages else None

        # Check if LLM has signaled completion
        if last_message and isinstance(last_message, AIMessage):
            content = last_message.content if isinstance(last_message.content, str) else str(last_message.content)

            # Look for completion signals in response
            completion_indicators = [
                "all requirements have been extracted",
                "processing complete",
                "no more candidates",
                "finished processing",
            ]

            if any(ind.lower() in content.lower() for ind in completion_indicators):
                state["current_phase"] = "output"
                state["should_stop"] = True

        # Check if we've processed all candidates
        if state.get("candidates"):
            if state["current_candidate_index"] >= len(state["candidates"]):
                state["current_phase"] = "output"
                state["should_stop"] = True

        return state

    async def _finalize_node(self, state: CreatorAgentState) -> CreatorAgentState:
        """Finalize job processing and update status."""
        job_id = state["job_id"]
        logger.info(f"Finalizing Creator Agent for job {job_id}")

        # Update job status
        await self._ensure_connection()

        if state.get("error") and not state["error"].get("recoverable"):
            await update_job_status(
                self.postgres_conn,
                uuid.UUID(job_id),
                creator_status="failed",
                error_message=state["error"].get("message")
            )
        else:
            await update_job_status(
                self.postgres_conn,
                uuid.UUID(job_id),
                creator_status="completed"
            )

        # Log summary
        requirements_created = state.get("requirements_created", [])
        logger.info(
            f"Job {job_id} complete: {len(requirements_created)} requirements created, "
            f"{state.get('iteration', 0)} iterations"
        )

        return state

    def _should_continue(self, state: CreatorAgentState) -> str:
        """Determine the next node to execute.

        Returns:
            Node name: "tools", "check", "finalize", or "continue"
        """
        # Check for errors that should stop processing
        if state.get("error") and not state["error"].get("recoverable"):
            return "finalize"

        # Check explicit stop flag
        if state.get("should_stop"):
            return "finalize"

        # Check for tool calls in last message
        messages = state.get("messages", [])
        last_message = messages[-1] if messages else None

        if last_message and hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tools"

        # Continue processing
        return "check"

    def _after_tools(self, state: CreatorAgentState) -> str:
        """Determine next step after tool execution.

        Returns:
            Node name: "process" to continue, "finalize" to end
        """
        if state.get("should_stop"):
            return "finalize"
        return "process"

    def _after_check(self, state: CreatorAgentState) -> str:
        """Determine next step after completion check.

        Returns:
            Node name: "process" to continue, "finalize" to end
        """
        if state.get("should_stop"):
            return "finalize"
        return "process"

    # =========================================================================
    # Graph Construction
    # =========================================================================

    def _build_graph(self) -> StateGraph:
        """Build the LangGraph state machine.

        Returns:
            Compiled StateGraph
        """
        workflow = StateGraph(CreatorAgentState)

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
                "check": "check",
                "finalize": "finalize",
            },
        )

        workflow.add_conditional_edges(
            "tools",
            self._after_tools,
            {
                "process": "process",
                "finalize": "finalize",
            },
        )

        workflow.add_conditional_edges(
            "check",
            self._after_check,
            {
                "process": "process",
                "finalize": "finalize",
            },
        )

        workflow.add_edge("finalize", END)

        return workflow.compile()

    # =========================================================================
    # Public Methods
    # =========================================================================

    async def process_job(
        self,
        job_id: str,
        max_iterations: Optional[int] = None
    ) -> Dict[str, Any]:
        """Process a single job.

        Args:
            job_id: UUID of the job to process
            max_iterations: Override max iterations limit

        Returns:
            Final state dictionary with processing results
        """
        self.current_job_id = job_id

        initial_state = CreatorAgentState(
            messages=[],
            job_id=job_id,
            job={},
            current_phase="initialization",
            document_path="",
            document_metadata={},
            document_chunks=[],
            candidates=[],
            current_candidate_index=0,
            processed_candidates=0,
            requirements_created=[],
            iteration=0,
            max_iterations=max_iterations or get_creator_max_iterations(),
            error=None,
            should_stop=False,
        )

        # Get checkpointer for durable execution
        checkpointer = await self._get_checkpointer()

        # Create thread config for this job
        thread_config = {
            "configurable": {
                "thread_id": f"creator_{job_id}",
            },
            "recursion_limit": get_creator_recursion_limit(),
        }

        # Run the graph with checkpointing
        final_state = await self.graph.ainvoke(
            initial_state,
            config=thread_config,
        )

        self.current_job_id = None
        return final_state

    async def process_job_stream(
        self,
        job_id: str,
        max_iterations: Optional[int] = None
    ):
        """Process a job and yield state updates.

        Args:
            job_id: UUID of the job to process
            max_iterations: Override max iterations limit

        Yields:
            State dictionaries as processing progresses
        """
        self.current_job_id = job_id

        initial_state = CreatorAgentState(
            messages=[],
            job_id=job_id,
            job={},
            current_phase="initialization",
            document_path="",
            document_metadata={},
            document_chunks=[],
            candidates=[],
            current_candidate_index=0,
            processed_candidates=0,
            requirements_created=[],
            iteration=0,
            max_iterations=max_iterations or get_creator_max_iterations(),
            error=None,
            should_stop=False,
        )

        thread_config = {
            "configurable": {
                "thread_id": f"creator_{job_id}",
            },
            "recursion_limit": get_creator_recursion_limit(),
        }

        async for state in self.graph.astream(initial_state, config=thread_config):
            yield state

        self.current_job_id = None

    async def start(
        self,
        polling_interval: Optional[int] = None,
        one_shot: bool = False
    ) -> None:
        """Start the agent's polling loop.

        Args:
            polling_interval: Seconds between job queue checks
            one_shot: If True, process one job and exit
        """
        interval = polling_interval or self.config.get("polling_interval_seconds", 30)
        logger.info(f"Creator Agent starting (polling interval: {interval}s)")

        await self._ensure_connection()

        while True:
            try:
                # Check for pending jobs
                job = await self._poll_for_job()

                if job:
                    job_id = str(job["id"])
                    logger.info(f"Processing job {job_id}")

                    try:
                        await self.process_job(job_id)
                    except Exception as e:
                        logger.error(f"Error processing job {job_id}: {e}")
                        await update_job_status(
                            self.postgres_conn,
                            uuid.UUID(job_id),
                            creator_status="failed",
                            error_message=str(e)
                        )

                    if one_shot:
                        break

                else:
                    if one_shot:
                        logger.info("No pending jobs found")
                        break

                    # Wait before next poll
                    import asyncio
                    await asyncio.sleep(interval)

            except Exception as e:
                logger.error(f"Polling loop error: {e}")
                import asyncio
                await asyncio.sleep(interval)

    async def _poll_for_job(self) -> Optional[Dict[str, Any]]:
        """Poll for the next pending job.

        Returns:
            Job dictionary or None if no jobs available
        """
        await self._ensure_connection()

        # Get next pending job (not yet processed by creator)
        row = await self.postgres_conn.fetchrow(
            """
            SELECT * FROM jobs
            WHERE status = 'pending' AND creator_status = 'pending'
            ORDER BY created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        )

        if row:
            return dict(row)
        return None

    async def shutdown(self) -> None:
        """Gracefully shutdown the agent."""
        logger.info("Creator Agent shutting down")

        if self.postgres_conn:
            await self.postgres_conn.disconnect()

        if self._checkpointer:
            # Checkpointer cleanup if needed
            pass


# =============================================================================
# Factory Function
# =============================================================================

def create_creator_agent(
    postgres_conn: Optional[PostgresConnection] = None,
    config: Optional[Dict[str, Any]] = None
) -> CreatorAgent:
    """Create a Creator Agent instance.

    Args:
        postgres_conn: Optional PostgreSQL connection
        config: Optional configuration overrides

    Returns:
        Configured CreatorAgent instance
    """
    return CreatorAgent(postgres_conn=postgres_conn, config=config)
