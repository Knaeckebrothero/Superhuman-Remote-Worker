"""FastAPI application for the Validator Agent.

This module provides HTTP endpoints for health checking, status monitoring,
validation submission, and graceful shutdown of the Validator Agent container.

Endpoints:
- GET /health - Basic health check
- GET /ready - Readiness probe (checks database connections)
- GET /status - Detailed agent status
- POST /shutdown - Request graceful shutdown
- POST /validate - Submit a requirement for validation
- GET /validate/{request_id} - Get validation status/result
- GET /requirements/pending - List pending requirements
- GET /requirements/{id} - Get requirement details
"""

import os
import asyncio
import logging
import uuid as uuid_module
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel

from src.core.postgres_utils import (
    create_postgres_connection,
    PostgresConnection,
    get_pending_requirement,
    update_requirement_status,
    create_requirement,
)
from src.core.neo4j_utils import create_neo4j_connection, Neo4jConnection
from src.agents.validator.validator_agent import ValidatorAgent, create_validator_agent
from src.agents.validator.models import (
    ValidateRequest,
    ValidateSubmitResponse,
    ValidateStatusResponse,
    GraphChange,
    PendingRequirementResponse,
    PendingRequirementsListResponse,
    RequirementDetailResponse,
)

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

# Manual validation tracking - tracks validations submitted via API
_manual_validations: Dict[str, Dict[str, Any]] = {}  # request_id -> {status, phase, result, ...}


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
                            uuid_module.UUID(req_id),
                            status="integrated",
                            validation_result=result,
                        )
                    else:
                        _requirements_rejected += 1
                        await update_requirement_status(
                            _postgres_conn,
                            uuid_module.UUID(req_id),
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


# =============================================================================
# Validation Submission and Status Endpoints
# =============================================================================


async def _process_manual_validation(
    request_id: str,
    requirement: Dict[str, Any],
    job_id: str,
    requirement_id: str,
    max_iterations: int,
):
    """Background task to process a manually submitted validation."""
    global _requirements_validated, _requirements_integrated, _requirements_rejected, _last_activity

    try:
        _manual_validations[request_id]["status"] = "processing"
        _manual_validations[request_id]["started_at"] = datetime.utcnow()
        _last_activity = datetime.utcnow()

        if _agent:
            result = await _agent.validate_requirement(
                requirement=requirement,
                job_id=job_id,
                requirement_id=requirement_id,
            )

            _requirements_validated += 1
            _last_activity = datetime.utcnow()

            # Determine outcome
            if result.get("graph_changes"):
                _requirements_integrated += 1
                _manual_validations[request_id]["status"] = "completed"
                await update_requirement_status(
                    _postgres_conn,
                    uuid_module.UUID(requirement_id),
                    status="integrated",
                    validation_result=result,
                )
            else:
                _requirements_rejected += 1
                _manual_validations[request_id]["status"] = "rejected"
                await update_requirement_status(
                    _postgres_conn,
                    uuid_module.UUID(requirement_id),
                    status="rejected",
                    validation_result=result,
                    rejection_reason=result.get("rejection_reason", "Validation rejected"),
                )

            _manual_validations[request_id]["completed_at"] = datetime.utcnow()
            _manual_validations[request_id]["result"] = result

            logger.info(f"Manual validation {request_id} completed with status {_manual_validations[request_id]['status']}")
        else:
            _manual_validations[request_id]["status"] = "failed"
            _manual_validations[request_id]["error"] = {"message": "Agent not initialized"}

    except Exception as e:
        logger.error(f"Error processing manual validation {request_id}: {e}")
        _manual_validations[request_id]["status"] = "failed"
        _manual_validations[request_id]["error"] = {"message": str(e)}
        _manual_validations[request_id]["completed_at"] = datetime.utcnow()


@app.post("/validate", response_model=ValidateSubmitResponse)
async def submit_validation(request: ValidateRequest, background_tasks: BackgroundTasks):
    """Submit a requirement for validation.

    Can either validate an existing requirement by ID, or create and validate
    an ad-hoc requirement from the provided data.
    """
    if not _postgres_conn or not _postgres_conn.is_connected:
        raise HTTPException(status_code=503, detail="Database not connected")

    if _shutdown_requested:
        raise HTTPException(status_code=503, detail="Agent is shutting down")

    try:
        requirement_id = None
        requirement_data = None
        job_id = None

        if request.requirement_id:
            # Validate existing requirement from cache
            try:
                req_uuid = uuid_module.UUID(request.requirement_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid requirement ID format")

            # Fetch requirement from database
            row = await _postgres_conn.fetchrow(
                """
                SELECT * FROM requirement_cache WHERE id = $1
                """,
                req_uuid,
            )
            if not row:
                raise HTTPException(status_code=404, detail="Requirement not found")

            requirement_id = request.requirement_id
            job_id = str(row["job_id"])
            requirement_data = dict(row)

        elif request.text:
            # Create ad-hoc requirement for validation
            # First, we need a job - create a dummy job for manual testing
            dummy_job_id = await _postgres_conn.fetchval(
                """
                INSERT INTO jobs (prompt, status, creator_status, validator_status)
                VALUES ('Manual validation test', 'processing', 'completed', 'processing')
                RETURNING id
                """
            )
            job_id = str(dummy_job_id)

            # Create the requirement
            req_uuid = await create_requirement(
                conn=_postgres_conn,
                job_id=dummy_job_id,
                text=request.text,
                name=request.name,
                req_type=request.type,
                priority=request.priority,
                gobd_relevant=request.gobd_relevant,
                gdpr_relevant=request.gdpr_relevant,
                mentioned_objects=request.mentioned_objects,
                mentioned_messages=request.mentioned_messages,
                confidence=0.9,  # High confidence for manual entries
            )
            requirement_id = str(req_uuid)

            # Fetch the created requirement
            row = await _postgres_conn.fetchrow(
                "SELECT * FROM requirement_cache WHERE id = $1", req_uuid
            )
            requirement_data = dict(row)

        else:
            raise HTTPException(
                status_code=400,
                detail="Must provide either requirement_id or text",
            )

        # Generate request ID for tracking
        request_id = str(uuid_module.uuid4())

        # Track the manual validation
        _manual_validations[request_id] = {
            "requirement_id": requirement_id,
            "job_id": job_id,
            "status": "submitted",
            "phase": None,
            "iteration": 0,
            "max_iterations": request.max_iterations,
            "started_at": None,
            "completed_at": None,
            "error": None,
            "result": None,
        }

        # Start validation in background
        background_tasks.add_task(
            _process_manual_validation,
            request_id,
            requirement_data,
            job_id,
            requirement_id,
            request.max_iterations,
        )

        logger.info(f"Manual validation {request_id} submitted for requirement {requirement_id}")

        return ValidateSubmitResponse(
            request_id=request_id,
            requirement_id=requirement_id,
            status="submitted",
            message="Validation submitted. Poll GET /validate/{request_id} for status.",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error submitting validation: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/validate/{request_id}", response_model=ValidateStatusResponse)
async def get_validation_status(request_id: str):
    """Get the current status of a validation request."""
    if request_id not in _manual_validations:
        raise HTTPException(status_code=404, detail="Validation request not found")

    info = _manual_validations[request_id]
    result = info.get("result", {}) or {}

    # Convert graph changes to response model
    graph_changes = []
    for change in result.get("graph_changes", []):
        graph_changes.append(
            GraphChange(
                operation=change.get("operation", "unknown"),
                node_type=change.get("node_type"),
                node_id=change.get("node_id"),
                relationship_type=change.get("relationship_type"),
                source_id=change.get("source_id"),
                target_id=change.get("target_id"),
                properties=change.get("properties"),
            )
        )

    # Calculate progress based on phases
    phases = ["understanding", "relevance", "fulfillment", "planning", "integration", "documentation"]
    phases_completed = result.get("phases_completed", [])
    if isinstance(phases_completed, str):
        # Convert single phase string to list
        phase_idx = phases.index(phases_completed) if phases_completed in phases else -1
        phases_completed = phases[: phase_idx + 1] if phase_idx >= 0 else []
    progress = (len(phases_completed) / len(phases)) * 100 if phases else 0

    return ValidateStatusResponse(
        request_id=request_id,
        requirement_id=info.get("requirement_id"),
        status=info.get("status", "unknown"),
        current_phase=info.get("phase") or result.get("current_phase"),
        phases_completed=phases_completed,
        iteration=info.get("iteration", 0),
        max_iterations=info.get("max_iterations", 100),
        progress_percent=progress,
        related_objects=result.get("related_objects", []),
        related_messages=result.get("related_messages", []),
        fulfillment_analysis=result.get("fulfillment_analysis"),
        graph_changes=graph_changes,
        graph_node_id=result.get("graph_node_id"),
        rejection_reason=result.get("rejection_reason"),
        started_at=info.get("started_at"),
        completed_at=info.get("completed_at"),
        error=info.get("error"),
    )


@app.get("/requirements/pending", response_model=PendingRequirementsListResponse)
async def list_pending_requirements(limit: int = 50):
    """List pending requirements from the cache."""
    if not _postgres_conn or not _postgres_conn.is_connected:
        raise HTTPException(status_code=503, detail="Database not connected")

    rows = await _postgres_conn.fetch(
        """
        SELECT
            id, job_id, name, text, type, priority, confidence,
            gobd_relevant, gdpr_relevant, created_at, status
        FROM requirement_cache
        WHERE status = 'pending'
        ORDER BY created_at ASC
        LIMIT $1
        """,
        limit,
    )

    # Get total count
    total = await _postgres_conn.fetchval(
        "SELECT COUNT(*) FROM requirement_cache WHERE status = 'pending'"
    )

    requirements = [
        PendingRequirementResponse(
            id=str(row["id"]),
            job_id=str(row["job_id"]),
            name=row.get("name"),
            text=row["text"][:500] + "..." if len(row["text"]) > 500 else row["text"],
            type=row.get("type"),
            priority=row.get("priority"),
            confidence=row.get("confidence", 0.0),
            gobd_relevant=row.get("gobd_relevant", False),
            gdpr_relevant=row.get("gdpr_relevant", False),
            created_at=row.get("created_at"),
            status=row.get("status", "pending"),
        )
        for row in rows
    ]

    return PendingRequirementsListResponse(
        total=total,
        requirements=requirements,
    )


@app.get("/requirements/{requirement_id}", response_model=RequirementDetailResponse)
async def get_requirement_detail(requirement_id: str):
    """Get full details of a requirement."""
    if not _postgres_conn or not _postgres_conn.is_connected:
        raise HTTPException(status_code=503, detail="Database not connected")

    try:
        req_uuid = uuid_module.UUID(requirement_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid requirement ID format")

    row = await _postgres_conn.fetchrow(
        "SELECT * FROM requirement_cache WHERE id = $1", req_uuid
    )

    if not row:
        raise HTTPException(status_code=404, detail="Requirement not found")

    return RequirementDetailResponse(
        id=str(row["id"]),
        job_id=str(row["job_id"]),
        candidate_id=row.get("candidate_id"),
        name=row.get("name"),
        text=row["text"],
        type=row.get("type"),
        priority=row.get("priority"),
        source_document=row.get("source_document"),
        source_location=row.get("source_location"),
        gobd_relevant=row.get("gobd_relevant", False),
        gdpr_relevant=row.get("gdpr_relevant", False),
        citations=row.get("citations") or [],
        mentioned_objects=row.get("mentioned_objects") or [],
        mentioned_messages=row.get("mentioned_messages") or [],
        reasoning=row.get("reasoning"),
        research_notes=row.get("research_notes"),
        confidence=row.get("confidence", 0.0),
        status=row.get("status", "pending"),
        validation_result=row.get("validation_result"),
        graph_node_id=row.get("graph_node_id"),
        rejection_reason=row.get("rejection_reason"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        validated_at=row.get("validated_at"),
        tags=row.get("tags") or [],
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("VALIDATOR_PORT", "8002"))
    uvicorn.run(
        "src.agents.validator.app:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
