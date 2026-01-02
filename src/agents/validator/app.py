"""FastAPI application for the Validator Agent.

This module provides HTTP endpoints for health checking, status monitoring,
and graceful shutdown of the Validator Agent container.

Endpoints:
- GET /health - Basic health check
- GET /ready - Readiness probe (checks database connections)
- GET /status - Detailed agent status
- POST /shutdown - Request graceful shutdown
"""

import os
import asyncio
import logging
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.core.postgres_utils import create_postgres_connection, PostgresConnection
from src.core.neo4j_utils import create_neo4j_connection, Neo4jConnection
from src.agents.validator.validator_agent import ValidatorAgent, create_validator_agent

logger = logging.getLogger(__name__)

# Global state
_agent: Optional[ValidatorAgent] = None
_postgres_conn: Optional[PostgresConnection] = None
_neo4j_conn: Optional[Neo4jConnection] = None
_start_time: datetime = datetime.utcnow()
_shutdown_requested: bool = False
_agent_task: Optional[asyncio.Task] = None

# Metrics
_requirements_validated: int = 0
_requirements_integrated: int = 0
_requirements_rejected: int = 0
_last_activity: Optional[datetime] = None


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    agent: str = "validator"
    uptime_seconds: float


class ReadyResponse(BaseModel):
    """Readiness check response."""
    ready: bool
    database: str
    neo4j: str
    message: Optional[str] = None


class StatusResponse(BaseModel):
    """Detailed status response."""
    agent: str = "validator"
    state: str
    current_job_id: Optional[str]
    current_requirement_id: Optional[str]
    last_activity: Optional[str]
    shutdown_requested: bool
    metrics: dict


class ShutdownResponse(BaseModel):
    """Shutdown response."""
    status: str
    message: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler - startup and shutdown."""
    global _agent, _postgres_conn, _neo4j_conn, _agent_task, _start_time

    logger.info("Validator Agent application starting...")
    _start_time = datetime.utcnow()

    # Initialize PostgreSQL connection
    _postgres_conn = create_postgres_connection()
    await _postgres_conn.connect()
    logger.info("PostgreSQL connection established")

    # Initialize Neo4j connection
    _neo4j_conn = create_neo4j_connection()
    _neo4j_conn.connect()
    logger.info("Neo4j connection established")

    # Create agent
    _agent = create_validator_agent(
        neo4j_connection=_neo4j_conn,
        postgres_connection_string=os.getenv("DATABASE_URL"),
    )
    logger.info("Validator Agent initialized")

    # Start agent polling in background
    _agent_task = asyncio.create_task(_run_agent_loop())
    logger.info("Validator Agent polling loop started")

    yield

    # Shutdown
    logger.info("Validator Agent application shutting down...")

    global _shutdown_requested
    _shutdown_requested = True

    # Request agent shutdown
    if _agent:
        _agent.request_shutdown()

    # Cancel agent task
    if _agent_task and not _agent_task.done():
        _agent_task.cancel()
        try:
            await _agent_task
        except asyncio.CancelledError:
            pass

    # Close database connections
    if _neo4j_conn:
        _neo4j_conn.disconnect()

    if _postgres_conn:
        await _postgres_conn.disconnect()

    logger.info("Validator Agent application stopped")


async def _run_agent_loop():
    """Run the agent polling loop."""
    global _requirements_validated, _requirements_integrated, _requirements_rejected, _last_activity

    if not _agent:
        return

    try:
        # Use the agent's built-in polling loop
        # We wrap it to track our own metrics
        from src.core.postgres_utils import get_pending_requirement, update_requirement_status
        import uuid

        while not _shutdown_requested:
            try:
                # Poll for pending requirement
                requirement = await get_pending_requirement(_postgres_conn)

                if requirement:
                    _last_activity = datetime.utcnow()
                    req_id = str(requirement["id"])
                    job_id = str(requirement["job_id"])

                    logger.info(f"Validating requirement {req_id}")

                    result = await _agent.validate_requirement(
                        requirement=requirement,
                        job_id=job_id,
                        requirement_id=req_id,
                    )

                    _requirements_validated += 1
                    _last_activity = datetime.utcnow()

                    # Track outcomes
                    if result.get("graph_changes"):
                        _requirements_integrated += 1
                        await update_requirement_status(
                            _postgres_conn,
                            uuid.UUID(req_id),
                            status="integrated",
                            validation_result=result,
                        )
                    else:
                        _requirements_rejected += 1
                        await update_requirement_status(
                            _postgres_conn,
                            uuid.UUID(req_id),
                            status="rejected",
                            validation_result=result,
                            rejection_reason=result.get("rejection_reason", "Validation rejected"),
                        )

                else:
                    # No pending requirements, wait before polling again
                    await asyncio.sleep(_agent.polling_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in agent loop: {e}")
                await asyncio.sleep(10)

    except Exception as e:
        logger.error(f"Agent loop fatal error: {e}")


# Create FastAPI app
app = FastAPI(
    title="Validator Agent",
    description="Graph-RAG Validator Agent for requirement validation and graph integration",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
async def health():
    """Basic health check endpoint.

    Returns 200 if the application is running.
    Used by container orchestrators for liveness probes.
    """
    uptime = (datetime.utcnow() - _start_time).total_seconds()
    return HealthResponse(
        status="healthy",
        uptime_seconds=uptime,
    )


@app.get("/ready", response_model=ReadyResponse)
async def ready():
    """Readiness check endpoint.

    Returns 200 if the agent is ready to process requirements.
    Checks both PostgreSQL and Neo4j connectivity.
    """
    if _shutdown_requested:
        return ReadyResponse(
            ready=False,
            database="disconnected",
            neo4j="disconnected",
            message="Shutdown in progress",
        )

    db_ok = False
    neo4j_ok = False
    messages = []

    # Check PostgreSQL connection
    try:
        if _postgres_conn and _postgres_conn.is_connected:
            await _postgres_conn.fetchrow("SELECT 1")
            db_ok = True
        else:
            messages.append("PostgreSQL not connected")
    except Exception as e:
        messages.append(f"PostgreSQL error: {e}")

    # Check Neo4j connection
    try:
        if _neo4j_conn and _neo4j_conn.driver:
            with _neo4j_conn.driver.session() as session:
                session.run("RETURN 1").consume()
            neo4j_ok = True
        else:
            messages.append("Neo4j not connected")
    except Exception as e:
        messages.append(f"Neo4j error: {e}")

    return ReadyResponse(
        ready=db_ok and neo4j_ok,
        database="connected" if db_ok else "error",
        neo4j="connected" if neo4j_ok else "error",
        message="; ".join(messages) if messages else None,
    )


@app.get("/status", response_model=StatusResponse)
async def status():
    """Detailed agent status endpoint.

    Returns current agent state, metrics, and configuration.
    """
    uptime = (datetime.utcnow() - _start_time).total_seconds()
    uptime_hours = uptime / 3600

    # Determine state
    if _shutdown_requested:
        state = "shutting_down"
    elif _agent and _agent._current_requirement_id:
        state = "validating"
    else:
        state = "idle"

    return StatusResponse(
        state=state,
        current_job_id=_agent._current_job_id if _agent else None,
        current_requirement_id=_agent._current_requirement_id if _agent else None,
        last_activity=_last_activity.isoformat() if _last_activity else None,
        shutdown_requested=_shutdown_requested,
        metrics={
            "requirements_validated": _requirements_validated,
            "requirements_integrated": _requirements_integrated,
            "requirements_rejected": _requirements_rejected,
            "uptime_hours": round(uptime_hours, 2),
        },
    )


@app.post("/shutdown", response_model=ShutdownResponse)
async def shutdown():
    """Request graceful shutdown of the agent.

    The agent will complete any current validation before shutting down.
    """
    global _shutdown_requested

    if _shutdown_requested:
        return ShutdownResponse(
            status="already_requested",
            message="Shutdown already in progress",
        )

    _shutdown_requested = True
    if _agent:
        _agent.request_shutdown()

    logger.info("Shutdown requested via API")

    return ShutdownResponse(
        status="shutdown_initiated",
        message="Agent will shutdown after completing current validation",
    )


@app.get("/metrics")
async def metrics():
    """Prometheus-compatible metrics endpoint."""
    uptime = (datetime.utcnow() - _start_time).total_seconds()

    lines = [
        "# HELP validator_agent_uptime_seconds Agent uptime in seconds",
        "# TYPE validator_agent_uptime_seconds gauge",
        f"validator_agent_uptime_seconds {uptime:.2f}",
        "",
        "# HELP validator_agent_requirements_validated_total Total requirements validated",
        "# TYPE validator_agent_requirements_validated_total counter",
        f"validator_agent_requirements_validated_total {_requirements_validated}",
        "",
        "# HELP validator_agent_requirements_integrated_total Total requirements integrated",
        "# TYPE validator_agent_requirements_integrated_total counter",
        f"validator_agent_requirements_integrated_total {_requirements_integrated}",
        "",
        "# HELP validator_agent_requirements_rejected_total Total requirements rejected",
        "# TYPE validator_agent_requirements_rejected_total counter",
        f"validator_agent_requirements_rejected_total {_requirements_rejected}",
        "",
        "# HELP validator_agent_ready Agent readiness status",
        "# TYPE validator_agent_ready gauge",
        f"validator_agent_ready {1 if not _shutdown_requested else 0}",
    ]

    return "\n".join(lines)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("VALIDATOR_PORT", "8002"))
    uvicorn.run(
        "src.agents.validator.app:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
