"""Universal Agent Package.

A configurable, workspace-centric autonomous agent that can be deployed
as Creator, Validator, or any future agent type by changing its
configuration file.

Key Components:
- UniversalAgent: Main agent class with config-driven behavior
- UniversalAgentState: Minimal state for workspace-centric architecture
- AgentConfig: Configuration dataclass loaded from JSON
- FastAPI app: HTTP interface for job management

Example Usage:
    ```python
    from src.agents.universal import UniversalAgent

    # Create agent from config
    agent = UniversalAgent.from_config("creator")
    await agent.initialize()

    # Process a job
    result = await agent.process_job(
        job_id="abc123",
        metadata={"document_path": "/data/doc.pdf"}
    )

    # Or start polling loop
    await agent.start_polling()

    # Cleanup
    await agent.shutdown()
    ```

Running as FastAPI Service:
    ```bash
    # Set config via environment
    AGENT_CONFIG=creator uvicorn src.agents.universal.app:app --port 8001

    # Or pass config path directly
    python run_universal_agent.py --config creator --port 8001
    ```
"""

from .state import UniversalAgentState, create_initial_state
from .loader import (
    AgentConfig,
    LLMConfig,
    WorkspaceConfig,
    ToolsConfig,
    TodoConfig,
    ConnectionsConfig,
    PollingConfig,
    LimitsConfig,
    ContextManagementConfig,
    load_agent_config,
    create_llm,
    load_system_prompt,
    load_instructions,
    load_summarization_prompt,
    resolve_config_path,
    get_all_tool_names,
)
from .graph import build_agent_graph, run_graph_with_streaming, run_graph_with_summarization
from .context import (
    ContextConfig,
    ContextManager,
    ContextManagementState,
    ToolRetryManager,
    count_tokens_tiktoken,
    count_tokens_approximate,
    get_token_counter,
    write_error_to_workspace,
)
from .agent import UniversalAgent
from .models import (
    JobStatus,
    HealthStatus,
    JobSubmitRequest,
    JobSubmitResponse,
    JobStatusResponse,
    HealthResponse,
    ReadyResponse,
    AgentStatusResponse,
    ErrorResponse,
)
from .app import create_app, app

__all__ = [
    # State
    "UniversalAgentState",
    "create_initial_state",
    # Configuration
    "AgentConfig",
    "LLMConfig",
    "WorkspaceConfig",
    "ToolsConfig",
    "TodoConfig",
    "ConnectionsConfig",
    "PollingConfig",
    "LimitsConfig",
    "ContextManagementConfig",
    "load_agent_config",
    "create_llm",
    "load_system_prompt",
    "load_instructions",
    "load_summarization_prompt",
    "resolve_config_path",
    "get_all_tool_names",
    # Graph
    "build_agent_graph",
    "run_graph_with_streaming",
    "run_graph_with_summarization",
    # Context Management (Phase 6)
    "ContextConfig",
    "ContextManager",
    "ContextManagementState",
    "ToolRetryManager",
    "count_tokens_tiktoken",
    "count_tokens_approximate",
    "get_token_counter",
    "write_error_to_workspace",
    # Agent
    "UniversalAgent",
    # API Models
    "JobStatus",
    "HealthStatus",
    "JobSubmitRequest",
    "JobSubmitResponse",
    "JobStatusResponse",
    "HealthResponse",
    "ReadyResponse",
    "AgentStatusResponse",
    "ErrorResponse",
    # FastAPI
    "create_app",
    "app",
]
