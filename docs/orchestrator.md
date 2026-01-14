# Orchestrator System

Documentation of the orchestrator system's current implementation status, architecture, and known gaps.

## Overview

The orchestrator coordinates the two-agent system (Creator + Validator) by managing job lifecycle, monitoring agent health, and generating reports. It runs on port 8000 and exposes a REST API.

```
┌─────────────────────────────────────────────────────────────────────┐
│                    ORCHESTRATOR (port 8000)                         │
│               Job Management, Monitoring, Reports                   │
└─────────────────────────┬───────────────────────────────────────────┘
                          │
       ┌──────────────────┴──────────────────┐
       │                                      │
       ▼                                      ▼
┌──────────────────┐                ┌──────────────────┐
│ CREATOR (8001)   │                │ VALIDATOR (8002) │
│ (Universal Agent)│                │ (Universal Agent)│
└──────────────────┘                └──────────────────┘
```

## Implemented Components

### Core Modules (`src/orchestrator/`)

| Module | Purpose |
|--------|---------|
| `job_manager.py` | Job lifecycle management (create, status, cancel, complete) |
| `monitor.py` | Job completion detection, stuck job detection, agent health checks |
| `reporter.py` | Job summaries, requirement statistics, compliance tracking |
| `app.py` | FastAPI REST API with all orchestrator endpoints |

### JobManager

Full job lifecycle management:
- Create new jobs with document storage
- Retrieve job status with requirement statistics
- List jobs with filtering by status
- Cancel jobs safely (doesn't affect already integrated requirements)
- Update creator/validator status independently
- Mark jobs as completed/failed with error tracking
- Get pending jobs for agent processing
- Workspace cleanup functionality

### Monitor

Job and agent monitoring:
- Job completion detection with multiple criteria
- Stuck job detection (configurable threshold, default 60 minutes)
- Agent health checking via HTTP endpoints
- Job progress tracking with ETA calculations
- Detailed progress reporting with requirement breakdowns
- Failed/rejected requirement retrieval
- Reset mechanism for stuck requirements in 'validating' state
- Async callback support for `wait_for_completion()`

### Reporter

Reporting and statistics:
- Job summary generation with comprehensive statistics
- Requirement statistics (total, pending, integrated, rejected, failed)
- Compliance relevance tracking (GoBD, GDPR)
- Priority-based breakdown (high/medium/low)
- LLM usage statistics (requests, tokens - basic level)
- Citation summaries
- Text and JSON report generation

### FastAPI Application

REST API endpoints:
- `GET /health`, `GET /ready` - Health and readiness
- `POST /jobs` - Create job with optional document upload
- `GET /jobs` - List jobs with filtering
- `GET /jobs/{job_id}` - Job status
- `GET /jobs/{job_id}/progress` - Progress with ETA
- `GET /jobs/{job_id}/report` - Full report (text/JSON)
- `DELETE /jobs/{job_id}` - Cancel job
- `GET /agents/health` - Agent health monitoring
- `GET /jobs/stuck` - Stuck job detection
- `GET /metrics` - Prometheus-compatible metrics
- `GET /stats/daily` - Daily statistics

## Database Schema

### Jobs Table

```sql
CREATE TABLE jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prompt TEXT NOT NULL,
    document_path TEXT,
    document_content BYTEA,
    context JSONB DEFAULT '{}',
    status TEXT DEFAULT 'created',  -- created, processing, completed, failed, cancelled
    creator_status TEXT DEFAULT 'pending',
    validator_status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    error_message TEXT,
    error_details JSONB,
    total_tokens INTEGER DEFAULT 0,
    total_requests INTEGER DEFAULT 0
);
```

### Requirements Table

```sql
CREATE TABLE requirements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID REFERENCES jobs(id) ON DELETE CASCADE,
    requirement_text TEXT NOT NULL,
    requirement_name TEXT,
    requirement_type TEXT,
    source_document TEXT,
    source_location TEXT,
    is_gobd_relevant BOOLEAN DEFAULT FALSE,
    is_gdpr_relevant BOOLEAN DEFAULT FALSE,
    research_notes TEXT,
    citations JSONB DEFAULT '[]',
    mentioned_objects JSONB DEFAULT '[]',
    mentioned_messages JSONB DEFAULT '[]',
    confidence_score FLOAT,
    neo4j_id TEXT,  -- NULL until integrated into graph
    status TEXT DEFAULT 'pending',  -- pending, validating, integrated, rejected, failed
    rejection_reason TEXT,
    retry_count INTEGER DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Job Summary View

Aggregates requirement counts by status for efficient dashboard queries.

## CLI Scripts

### start_orchestrator.py

```bash
# Create job and wait for completion
python start_orchestrator.py --document-path ./data/doc.pdf --prompt "Extract GoBD requirements" --wait

# Create job without waiting
python start_orchestrator.py --prompt "Analyze compliance" --json
```

### job_status.py

```bash
python job_status.py --job-id <uuid>              # Brief status
python job_status.py --job-id <uuid> --report     # Full text report
python job_status.py --job-id <uuid> --json       # JSON output
python job_status.py --job-id <uuid> --progress   # Progress with ETA
python job_status.py --job-id <uuid> --rejected   # Rejected requirements
python job_status.py --job-id <uuid> --failed     # Failed requirements
```

### list_jobs.py

```bash
python list_jobs.py                    # All jobs
python list_jobs.py --status pending   # Filter by status
python list_jobs.py --stats            # Daily statistics
python list_jobs.py --limit 10         # Pagination
```

### cancel_job.py

```bash
python cancel_job.py --job-id <uuid>              # Cancel with confirmation
python cancel_job.py --job-id <uuid> --force      # Skip confirmation
python cancel_job.py --job-id <uuid> --cleanup    # Also cleanup workspace
```

## Configuration

Located in `src/config/llm_config.json`:

```json
{
  "orchestrator": {
    "job_timeout_hours": 168,
    "stuck_detection_minutes": 60,
    "max_requirement_retries": 5,
    "completion_check_interval_seconds": 30
  }
}
```

## Container Infrastructure

### Current State

The orchestrator has containerization support in place:

| Component | Location | Status |
|-----------|----------|--------|
| Dockerfile | `docker/Dockerfile.orchestrator` | Multi-stage build, health check, non-root user |
| docker-compose | `docker-compose.yml` | Service definition with networking |
| Environment | Via `DATABASE_URL`, `CREATOR_AGENT_URL`, etc. | Basic env var config |

### Dockerfile Features

```dockerfile
# Multi-stage build (builder + runtime)
# Python 3.11-slim base
# Non-root user (graphrag:graphrag)
# Health check: curl http://localhost:8000/health
# Exposed port: 8000
```

### docker-compose Service

```yaml
orchestrator:
  depends_on: [postgres]
  environment:
    DATABASE_URL: postgresql://...
    CREATOR_AGENT_URL: http://creator:8001
    VALIDATOR_AGENT_URL: http://validator:8002
  volumes:
    - workspace_data:/app/workspace
  ports:
    - "8000:8000"
```

## Production Readiness

### What's Missing

#### 1. Active Job Coordination

Currently agents poll PostgreSQL independently - the orchestrator is passive.

Needed:
- **Job dispatcher** that actively assigns jobs to available agents
- **Agent registration** so orchestrator knows what agents exist
- **Load balancing** if multiple Creator/Validator instances

#### 2. Authentication & Security

Missing from `app.py`:
- API key / JWT authentication middleware
- Role-based access (admin vs read-only)
- Rate limiting per client
- CORS configuration for web clients

#### 3. Resilience

| Feature | Current | Needed |
|---------|---------|--------|
| Job retry | Manual only | Auto-retry with backoff |
| Circuit breaker | None | For agent health checks |
| Graceful shutdown | Basic | Wait for in-flight requests |
| Connection pooling | Single conn | Pool with health checks |

#### 4. Database Lifecycle

- No automated migration runner at startup
- No schema version validation
- No seed data initialization option

#### 5. Observability

- Structured JSON logging (currently plain text)
- Request tracing with correlation IDs
- Detailed timing metrics per endpoint
- OpenTelemetry integration (optional)

#### 6. Configuration

- No startup validation of required env vars
- No config hot-reload capability
- No secrets management integration

### Implementation Roadmap

#### Phase 1 - Essential (Reliable Operation)

1. Add startup config validation
2. Add database migration runner
3. Add proper connection pooling
4. Add graceful shutdown handling

#### Phase 2 - Security (Safe Operation)

1. Add API key authentication
2. Add rate limiting
3. Configure CORS
4. Fix the SQL injection in reporter

#### Phase 3 - Resilience (Robust Operation)

1. Add job retry logic with exponential backoff
2. Add circuit breaker for agent calls
3. Add automatic stuck job recovery

#### Phase 4 - Observability (Debuggable Operation)

1. Structured logging with correlation IDs
2. Enhanced Prometheus metrics
3. Request/response timing

## Known Gaps

### High Priority

| Gap | Description |
|-----|-------------|
| **SQL Injection** | `reporter.py:458` uses string formatting instead of parameterized query for date interval |
| **No Auth** | REST API has no authentication or authorization |
| **No Tests** | No test coverage for orchestrator module |
| **No Recovery** | No job resumption mechanism; stuck jobs require manual intervention |
| **Auto-cleanup** | Stuck jobs detected but not automatically recovered |

### Medium Priority

| Gap | Description |
|-----|-------------|
| **LLM Stats Incomplete** | Only basic token tracking; no per-agent breakdown |
| **No Webhooks** | Clients must poll for job completion; no event notifications |
| **No Priority Queue** | FIFO processing only; no job prioritization |
| **No Job Dependencies** | Can't express that job B depends on job A |
| **Limited Logging** | No structured logging with correlation IDs |

### Low Priority

| Gap | Description |
|-----|-------------|
| **No Versioning** | Can't compare results across job versions |
| **No Deduplication** | Same document/prompt can be submitted multiple times |
| **Document Content** | `document_content` BYTEA field exists but is never populated |
| **Context Underutilized** | JSONB context stored but not queryable |
| **No Tracing** | No OpenTelemetry or distributed tracing |

## Potential Issues

1. **Polling Interval** - 1 hour stuck detection threshold may be too long for some use cases
2. **No Circuit Breaker** - Agent health checks don't implement backoff for repeated failures
3. **Status Races** - Multiple agents could update same job status without optimistic locking
4. **Report Performance** - Synchronous report generation could be slow for large jobs

## Recommended Improvements

### Immediate (Bug Fixes)

1. Fix SQL injection in `get_daily_statistics()`:
   ```python
   # Current (vulnerable)
   WHERE created_at > CURRENT_TIMESTAMP - INTERVAL '%s days'

   # Should be
   WHERE created_at > CURRENT_TIMESTAMP - INTERVAL '1 day' * $1
   ```

### Short Term

1. Add API authentication middleware
2. Implement job recovery/resumption
3. Add comprehensive test suite
4. Implement automatic stuck job cleanup

### Long Term

1. Webhook/event notification system
2. Job priority and scheduling
3. Detailed structured logging
4. OpenTelemetry integration
