"""Monitor for the Graph-RAG Autonomous Agent System.

This module provides monitoring capabilities including completion detection,
stuck state detection, and agent health checking.
"""

import uuid
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Callable, Awaitable
from dataclasses import dataclass
from enum import Enum

from src.core.postgres_utils import (
    PostgresConnection,
    get_job,
    count_requirements_by_status,
)
from src.core.config import load_config

logger = logging.getLogger(__name__)


class JobCompletionStatus(Enum):
    """Job completion status enum."""
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    STUCK = "stuck"
    FAILED = "failed"


@dataclass
class HealthStatus:
    """Agent health status."""
    agent: str
    healthy: bool
    last_activity: Optional[datetime]
    current_job_id: Optional[uuid.UUID]
    message: str


@dataclass
class StuckJobInfo:
    """Information about a stuck job."""
    job_id: uuid.UUID
    stuck_component: str  # 'creator', 'validator', 'both'
    stuck_since: datetime
    last_activity: datetime
    pending_requirements: int
    reason: str


class Monitor:
    """Monitors job progress, completion, and agent health.

    Provides:
    - Completion detection for jobs
    - Stuck state detection with configurable thresholds
    - Agent health checking via HTTP endpoints
    - Progress tracking and statistics

    Example:
        ```python
        monitor = Monitor(postgres_conn)

        # Check if a job is complete
        status = await monitor.check_job_completion(job_id)

        # Detect stuck jobs
        stuck_jobs = await monitor.detect_stuck_jobs()

        # Check agent health
        health = await monitor.check_agent_health("creator", "http://localhost:8001")
        ```
    """

    def __init__(
        self,
        conn: PostgresConnection,
        config: Optional[Dict[str, Any]] = None
    ):
        """Initialize the Monitor.

        Args:
            conn: PostgreSQL connection instance
            config: Optional configuration override. If not provided,
                    loads from llm_config.json orchestrator section
        """
        self.conn = conn

        # Load configuration
        if config is None:
            full_config = load_config("llm_config.json")
            config = full_config.get("orchestrator", {})

        self.job_timeout_hours = config.get("job_timeout_hours", 168)  # 7 days default
        self.stuck_detection_minutes = config.get("stuck_detection_minutes", 60)
        self.max_requirement_retries = config.get("max_requirement_retries", 5)
        self.completion_check_interval = config.get("completion_check_interval_seconds", 30)

        logger.info(
            f"Monitor initialized: timeout={self.job_timeout_hours}h, "
            f"stuck_threshold={self.stuck_detection_minutes}min"
        )

    async def check_job_completion(self, job_id: uuid.UUID) -> JobCompletionStatus:
        """Check if a job is complete.

        A job is complete when:
        1. Creator has finished (creator_status == 'completed')
        2. All requirements in cache have been processed (not pending/validating)

        Args:
            job_id: Job UUID

        Returns:
            JobCompletionStatus indicating current state
        """
        job = await get_job(self.conn, job_id)
        if not job:
            logger.warning(f"Job {job_id} not found")
            return JobCompletionStatus.FAILED

        # Check for explicit failure or cancellation
        if job['status'] in ('failed', 'cancelled'):
            return JobCompletionStatus.FAILED

        # Check for explicit completion
        if job['status'] == 'completed':
            return JobCompletionStatus.COMPLETED

        # Check timeout
        created_at = job['created_at']
        if datetime.utcnow() - created_at.replace(tzinfo=None) > timedelta(hours=self.job_timeout_hours):
            logger.warning(f"Job {job_id} exceeded timeout of {self.job_timeout_hours} hours")
            return JobCompletionStatus.STUCK

        # Creator must complete first
        if job['creator_status'] != 'completed':
            return JobCompletionStatus.IN_PROGRESS

        # Count pending/validating requirements
        req_counts = await count_requirements_by_status(self.conn, job_id)
        pending_count = req_counts.get('pending', 0) + req_counts.get('validating', 0)

        if pending_count > 0:
            return JobCompletionStatus.IN_PROGRESS

        # All requirements processed
        return JobCompletionStatus.COMPLETED

    async def is_job_complete(self, job_id: uuid.UUID) -> bool:
        """Simple boolean check for job completion.

        Args:
            job_id: Job UUID

        Returns:
            True if job is completed
        """
        status = await self.check_job_completion(job_id)
        return status == JobCompletionStatus.COMPLETED

    async def detect_stuck_jobs(self) -> List[StuckJobInfo]:
        """Detect jobs that appear to be stuck.

        A job is considered stuck if:
        - No progress in the last N minutes (configurable)
        - Has pending requirements but no recent validation activity
        - Creator is processing but no recent requirement creation

        Returns:
            List of StuckJobInfo for stuck jobs
        """
        stuck_threshold = datetime.utcnow() - timedelta(minutes=self.stuck_detection_minutes)

        # Find processing jobs with stale activity
        rows = await self.conn.fetch(
            """
            SELECT j.*, js.*
            FROM jobs j
            JOIN job_summary js ON j.id = js.id
            WHERE j.status = 'processing'
            AND j.updated_at < $1
            """,
            stuck_threshold
        )

        stuck_jobs = []
        for row in rows:
            job_data = dict(row)
            job_id = job_data['id']

            # Determine which component is stuck
            stuck_component = self._determine_stuck_component(job_data)
            if stuck_component:
                reason = self._get_stuck_reason(job_data, stuck_component)
                stuck_jobs.append(StuckJobInfo(
                    job_id=job_id,
                    stuck_component=stuck_component,
                    stuck_since=stuck_threshold,
                    last_activity=job_data['updated_at'],
                    pending_requirements=job_data.get('pending_requirements', 0),
                    reason=reason
                ))

        if stuck_jobs:
            logger.warning(f"Detected {len(stuck_jobs)} stuck jobs")

        return stuck_jobs

    def _determine_stuck_component(self, job_data: Dict[str, Any]) -> Optional[str]:
        """Determine which component is stuck.

        Args:
            job_data: Job data dictionary

        Returns:
            'creator', 'validator', 'both', or None
        """
        creator_stuck = (
            job_data['creator_status'] == 'processing' and
            job_data.get('pending_requirements', 0) == 0 and
            job_data.get('integrated_requirements', 0) == 0
        )

        validator_stuck = (
            job_data['creator_status'] == 'completed' and
            job_data['validator_status'] == 'processing' and
            job_data.get('pending_requirements', 0) > 0
        )

        if creator_stuck and validator_stuck:
            return 'both'
        elif creator_stuck:
            return 'creator'
        elif validator_stuck:
            return 'validator'
        return None

    def _get_stuck_reason(self, job_data: Dict[str, Any], component: str) -> str:
        """Get human-readable reason for stuck state.

        Args:
            job_data: Job data dictionary
            component: Stuck component name

        Returns:
            Reason string
        """
        if component == 'creator':
            return "Creator agent not producing requirements"
        elif component == 'validator':
            pending = job_data.get('pending_requirements', 0)
            return f"Validator not processing {pending} pending requirements"
        else:
            return "Both agents appear stuck"

    async def check_agent_health(
        self,
        agent: str,
        endpoint: str,
        timeout: float = 5.0
    ) -> HealthStatus:
        """Check health of an agent via HTTP endpoint.

        Args:
            agent: Agent name ('creator' or 'validator')
            endpoint: HTTP endpoint URL (e.g., http://localhost:8001/health)
            timeout: Request timeout in seconds

        Returns:
            HealthStatus with health information
        """
        try:
            import httpx
        except ImportError:
            return HealthStatus(
                agent=agent,
                healthy=False,
                last_activity=None,
                current_job_id=None,
                message="httpx not installed"
            )

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(endpoint, timeout=timeout)

                if response.status_code == 200:
                    data = response.json()
                    return HealthStatus(
                        agent=agent,
                        healthy=True,
                        last_activity=datetime.fromisoformat(data.get('last_activity', ''))
                            if data.get('last_activity') else None,
                        current_job_id=uuid.UUID(data['current_job_id'])
                            if data.get('current_job_id') else None,
                        message=data.get('status', 'healthy')
                    )
                else:
                    return HealthStatus(
                        agent=agent,
                        healthy=False,
                        last_activity=None,
                        current_job_id=None,
                        message=f"HTTP {response.status_code}"
                    )

        except httpx.TimeoutException:
            return HealthStatus(
                agent=agent,
                healthy=False,
                last_activity=None,
                current_job_id=None,
                message="Connection timeout"
            )
        except httpx.ConnectError:
            return HealthStatus(
                agent=agent,
                healthy=False,
                last_activity=None,
                current_job_id=None,
                message="Connection refused"
            )
        except Exception as e:
            return HealthStatus(
                agent=agent,
                healthy=False,
                last_activity=None,
                current_job_id=None,
                message=str(e)
            )

    async def get_job_progress(self, job_id: uuid.UUID) -> Dict[str, Any]:
        """Get detailed progress information for a job.

        Args:
            job_id: Job UUID

        Returns:
            Dictionary with progress details
        """
        job = await get_job(self.conn, job_id)
        if not job:
            return {"error": "Job not found"}

        req_counts = await count_requirements_by_status(self.conn, job_id)
        total = sum(req_counts.values())
        processed = req_counts.get('integrated', 0) + req_counts.get('rejected', 0)

        # Calculate estimated time remaining
        elapsed = datetime.utcnow() - job['created_at'].replace(tzinfo=None)
        if processed > 0:
            avg_time_per_req = elapsed.total_seconds() / processed
            remaining = (total - processed) * avg_time_per_req
            eta_seconds = remaining
        else:
            eta_seconds = None

        return {
            "job_id": str(job_id),
            "status": job['status'],
            "creator_status": job['creator_status'],
            "validator_status": job['validator_status'],
            "requirements": {
                "total": total,
                "pending": req_counts.get('pending', 0),
                "validating": req_counts.get('validating', 0),
                "integrated": req_counts.get('integrated', 0),
                "rejected": req_counts.get('rejected', 0),
                "failed": req_counts.get('failed', 0),
            },
            "progress_percent": round(processed / total * 100, 1) if total > 0 else 0,
            "elapsed_seconds": elapsed.total_seconds(),
            "eta_seconds": eta_seconds,
            "created_at": job['created_at'].isoformat(),
        }

    async def wait_for_completion(
        self,
        job_id: uuid.UUID,
        callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
        max_wait_seconds: Optional[int] = None
    ) -> JobCompletionStatus:
        """Wait for a job to complete with optional progress callback.

        Args:
            job_id: Job UUID
            callback: Async callback called with progress on each check
            max_wait_seconds: Maximum time to wait (None for unlimited)

        Returns:
            Final JobCompletionStatus
        """
        start_time = datetime.utcnow()

        while True:
            status = await self.check_job_completion(job_id)

            if status in (JobCompletionStatus.COMPLETED, JobCompletionStatus.FAILED):
                return status

            if callback:
                progress = await self.get_job_progress(job_id)
                await callback(progress)

            if max_wait_seconds:
                elapsed = (datetime.utcnow() - start_time).total_seconds()
                if elapsed >= max_wait_seconds:
                    logger.warning(f"Wait timeout for job {job_id}")
                    return JobCompletionStatus.STUCK

            await asyncio.sleep(self.completion_check_interval)

    async def get_failed_requirements(
        self,
        job_id: uuid.UUID,
        include_retryable: bool = True
    ) -> List[Dict[str, Any]]:
        """Get failed requirements for a job.

        Args:
            job_id: Job UUID
            include_retryable: Include requirements that can be retried

        Returns:
            List of failed requirement dictionaries
        """
        if include_retryable:
            rows = await self.conn.fetch(
                """
                SELECT * FROM requirement_cache
                WHERE job_id = $1 AND status = 'failed'
                ORDER BY created_at
                """,
                job_id
            )
        else:
            rows = await self.conn.fetch(
                """
                SELECT * FROM requirement_cache
                WHERE job_id = $1 AND status = 'failed'
                AND retry_count >= $2
                ORDER BY created_at
                """,
                job_id, self.max_requirement_retries
            )

        return [dict(row) for row in rows]

    async def reset_stuck_requirements(
        self,
        job_id: uuid.UUID,
        older_than_minutes: int = 30
    ) -> int:
        """Reset requirements stuck in 'validating' state.

        Args:
            job_id: Job UUID
            older_than_minutes: Only reset requirements validating longer than this

        Returns:
            Number of requirements reset
        """
        threshold = datetime.utcnow() - timedelta(minutes=older_than_minutes)

        result = await self.conn.execute(
            """
            UPDATE requirement_cache
            SET status = 'pending', retry_count = retry_count + 1
            WHERE job_id = $1
            AND status = 'validating'
            AND updated_at < $2
            """,
            job_id, threshold
        )

        # Parse the number from "UPDATE N"
        count = int(result.split()[-1]) if result else 0
        if count > 0:
            logger.info(f"Reset {count} stuck requirements for job {job_id}")

        return count


def create_monitor(
    conn: PostgresConnection,
    config: Optional[Dict[str, Any]] = None
) -> Monitor:
    """Factory function to create a Monitor instance.

    Args:
        conn: PostgreSQL connection instance
        config: Optional configuration override

    Returns:
        Monitor instance
    """
    return Monitor(conn, config)
