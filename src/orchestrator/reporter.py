"""Reporter for the Graph-RAG Autonomous Agent System.

This module provides job result aggregation and reporting capabilities.
"""

import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field, asdict

from src.database.postgres_utils import (
    PostgresConnection,
    get_job,
    count_requirements_by_status,
)

logger = logging.getLogger(__name__)


@dataclass
class RequirementStatistics:
    """Statistics about requirements for a job."""
    total: int = 0
    pending: int = 0
    validating: int = 0
    integrated: int = 0
    rejected: int = 0
    failed: int = 0
    gobd_relevant: int = 0
    gdpr_relevant: int = 0
    high_priority: int = 0
    medium_priority: int = 0
    low_priority: int = 0


@dataclass
class LLMStatistics:
    """Statistics about LLM usage for a job."""
    total_requests: int = 0
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error_count: int = 0
    avg_duration_ms: float = 0.0
    by_agent: Dict[str, Dict[str, int]] = field(default_factory=dict)


@dataclass
class JobSummary:
    """Complete summary of a job."""
    job_id: uuid.UUID
    prompt: str
    status: str
    creator_status: str
    validator_status: str
    created_at: datetime
    completed_at: Optional[datetime]
    duration_seconds: float
    document_path: Optional[str]
    requirements: RequirementStatistics
    llm_usage: LLMStatistics
    error_message: Optional[str] = None
    integrated_rids: List[str] = field(default_factory=list)


class Reporter:
    """Generates reports and summaries for jobs.

    Provides:
    - Job summary generation with statistics
    - Requirement statistics by type, priority, relevance
    - LLM usage statistics
    - Citation summaries
    - Export to various formats

    Example:
        ```python
        reporter = Reporter(postgres_conn)

        # Get job summary
        summary = await reporter.get_job_summary(job_id)

        # Get requirement statistics
        stats = await reporter.get_requirement_statistics(job_id)

        # Generate text report
        report = await reporter.generate_text_report(job_id)
        ```
    """

    def __init__(self, conn: PostgresConnection):
        """Initialize the Reporter.

        Args:
            conn: PostgreSQL connection instance
        """
        self.conn = conn
        logger.info("Reporter initialized")

    async def get_job_summary(self, job_id: uuid.UUID) -> Optional[JobSummary]:
        """Get a complete summary of a job.

        Args:
            job_id: Job UUID

        Returns:
            JobSummary dataclass or None if job not found
        """
        job = await get_job(self.conn, job_id)
        if not job:
            return None

        # Get requirement statistics
        req_stats = await self.get_requirement_statistics(job_id)

        # Get LLM statistics
        llm_stats = await self.get_llm_statistics(job_id)

        # Get integrated requirement IDs
        integrated_rids = await self._get_integrated_rids(job_id)

        # Calculate duration
        if job['completed_at']:
            duration = (job['completed_at'] - job['created_at']).total_seconds()
        else:
            duration = (datetime.utcnow() - job['created_at'].replace(tzinfo=None)).total_seconds()

        return JobSummary(
            job_id=job_id,
            prompt=job['prompt'],
            status=job['status'],
            creator_status=job['creator_status'],
            validator_status=job['validator_status'],
            created_at=job['created_at'],
            completed_at=job['completed_at'],
            duration_seconds=duration,
            document_path=job.get('document_path'),
            requirements=req_stats,
            llm_usage=llm_stats,
            error_message=job.get('error_message'),
            integrated_rids=integrated_rids
        )

    async def get_requirement_statistics(self, job_id: uuid.UUID) -> RequirementStatistics:
        """Get detailed requirement statistics for a job.

        Args:
            job_id: Job UUID

        Returns:
            RequirementStatistics dataclass
        """
        # Get status counts
        status_counts = await count_requirements_by_status(self.conn, job_id)

        # Get additional breakdowns
        row = await self.conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE gobd_relevant = true) as gobd_relevant,
                COUNT(*) FILTER (WHERE gdpr_relevant = true) as gdpr_relevant,
                COUNT(*) FILTER (WHERE priority = 'high') as high_priority,
                COUNT(*) FILTER (WHERE priority = 'medium') as medium_priority,
                COUNT(*) FILTER (WHERE priority = 'low') as low_priority
            FROM requirements
            WHERE job_id = $1
            """,
            job_id
        )

        additional = dict(row) if row else {}

        return RequirementStatistics(
            total=sum(status_counts.values()),
            pending=status_counts.get('pending', 0),
            validating=status_counts.get('validating', 0),
            integrated=status_counts.get('integrated', 0),
            rejected=status_counts.get('rejected', 0),
            failed=status_counts.get('failed', 0),
            gobd_relevant=additional.get('gobd_relevant', 0),
            gdpr_relevant=additional.get('gdpr_relevant', 0),
            high_priority=additional.get('high_priority', 0),
            medium_priority=additional.get('medium_priority', 0),
            low_priority=additional.get('low_priority', 0)
        )

    async def get_llm_statistics(self, job_id: uuid.UUID) -> LLMStatistics:
        """Get LLM usage statistics for a job.

        Note: LLM statistics are now stored in MongoDB via llm_archiver.py.
        This method returns data from the jobs table resource tracking fields.

        Args:
            job_id: Job UUID

        Returns:
            LLMStatistics dataclass
        """
        # Get basic stats from jobs table
        row = await self.conn.fetchrow(
            """
            SELECT total_tokens_used, total_requests
            FROM jobs
            WHERE id = $1
            """,
            job_id
        )

        if row:
            return LLMStatistics(
                total_requests=row['total_requests'] or 0,
                total_tokens=row['total_tokens_used'] or 0,
                prompt_tokens=0,  # Not tracked in jobs table
                completion_tokens=0,  # Not tracked in jobs table
                error_count=0,  # Not tracked in jobs table
                avg_duration_ms=0.0,  # Not tracked in jobs table
                by_agent={}  # Detailed breakdown available in MongoDB
            )

        return LLMStatistics()

    async def get_citation_summary(self, job_id: uuid.UUID) -> Dict[str, Any]:
        """Get citation summary for a job.

        Args:
            job_id: Job UUID

        Returns:
            Dictionary with citation statistics
        """
        # Get citation counts from requirements
        rows = await self.conn.fetch(
            """
            SELECT citations FROM requirements
            WHERE job_id = $1 AND citations IS NOT NULL
            """,
            job_id
        )

        all_citations = []
        for row in rows:
            if row['citations']:
                import json
                if isinstance(row['citations'], str):
                    citations = json.loads(row['citations'])
                else:
                    citations = row['citations']
                all_citations.extend(citations)

        unique_citations = list(set(all_citations))

        return {
            "total_citation_references": len(all_citations),
            "unique_citations": len(unique_citations),
            "citation_ids": unique_citations[:100]  # Limit for display
        }

    async def _get_integrated_rids(self, job_id: uuid.UUID) -> List[str]:
        """Get list of integrated requirement IDs.

        Args:
            job_id: Job UUID

        Returns:
            List of graph_node_id values
        """
        rows = await self.conn.fetch(
            """
            SELECT graph_node_id FROM requirements
            WHERE job_id = $1 AND status = 'integrated' AND graph_node_id IS NOT NULL
            ORDER BY validated_at
            """,
            job_id
        )
        return [row['graph_node_id'] for row in rows]

    async def generate_text_report(self, job_id: uuid.UUID) -> str:
        """Generate a human-readable text report for a job.

        Args:
            job_id: Job UUID

        Returns:
            Formatted text report
        """
        summary = await self.get_job_summary(job_id)
        if not summary:
            return f"Job {job_id} not found"

        # Format duration
        duration = timedelta(seconds=int(summary.duration_seconds))

        report = f"""
================================================================================
                        JOB REPORT: {summary.job_id}
================================================================================

OVERVIEW
--------
Prompt:           {summary.prompt[:80]}{'...' if len(summary.prompt) > 80 else ''}
Status:           {summary.status.upper()}
Creator Status:   {summary.creator_status}
Validator Status: {summary.validator_status}
Created:          {summary.created_at.strftime('%Y-%m-%d %H:%M:%S')}
Completed:        {summary.completed_at.strftime('%Y-%m-%d %H:%M:%S') if summary.completed_at else 'In Progress'}
Duration:         {duration}

REQUIREMENTS
------------
Total Extracted:  {summary.requirements.total}
  - Integrated:   {summary.requirements.integrated}
  - Rejected:     {summary.requirements.rejected}
  - Pending:      {summary.requirements.pending}
  - Failed:       {summary.requirements.failed}

By Priority:
  - High:         {summary.requirements.high_priority}
  - Medium:       {summary.requirements.medium_priority}
  - Low:          {summary.requirements.low_priority}

Compliance Relevance:
  - GoBD:         {summary.requirements.gobd_relevant}
  - GDPR:         {summary.requirements.gdpr_relevant}

LLM USAGE
---------
Total Requests:   {summary.llm_usage.total_requests}
Total Tokens:     {summary.llm_usage.total_tokens:,}
  - Prompt:       {summary.llm_usage.prompt_tokens:,}
  - Completion:   {summary.llm_usage.completion_tokens:,}
Avg Duration:     {summary.llm_usage.avg_duration_ms:.0f}ms
Errors:           {summary.llm_usage.error_count}

By Agent:"""

        for agent, stats in summary.llm_usage.by_agent.items():
            report += f"\n  - {agent}: {stats['requests']} requests, {stats['tokens']:,} tokens"

        if summary.integrated_rids:
            report += f"""

INTEGRATED REQUIREMENTS
-----------------------
{', '.join(summary.integrated_rids[:20])}{'...' if len(summary.integrated_rids) > 20 else ''}
"""

        if summary.error_message:
            report += f"""

ERROR
-----
{summary.error_message}
"""

        report += "\n================================================================================\n"

        return report

    async def generate_json_report(self, job_id: uuid.UUID) -> Dict[str, Any]:
        """Generate a JSON-serializable report for a job.

        Args:
            job_id: Job UUID

        Returns:
            Dictionary suitable for JSON serialization
        """
        summary = await self.get_job_summary(job_id)
        if not summary:
            return {"error": f"Job {job_id} not found"}

        citation_summary = await self.get_citation_summary(job_id)

        return {
            "job_id": str(summary.job_id),
            "prompt": summary.prompt,
            "status": summary.status,
            "creator_status": summary.creator_status,
            "validator_status": summary.validator_status,
            "created_at": summary.created_at.isoformat(),
            "completed_at": summary.completed_at.isoformat() if summary.completed_at else None,
            "duration_seconds": summary.duration_seconds,
            "document_path": summary.document_path,
            "requirements": asdict(summary.requirements),
            "llm_usage": asdict(summary.llm_usage),
            "citations": citation_summary,
            "error_message": summary.error_message,
            "integrated_rids": summary.integrated_rids
        }

    async def get_rejected_requirements(
        self,
        job_id: uuid.UUID
    ) -> List[Dict[str, Any]]:
        """Get details about rejected requirements.

        Args:
            job_id: Job UUID

        Returns:
            List of rejected requirement details
        """
        rows = await self.conn.fetch(
            """
            SELECT id, name, text, rejection_reason, confidence
            FROM requirements
            WHERE job_id = $1 AND status = 'rejected'
            ORDER BY validated_at
            """,
            job_id
        )
        return [dict(row) for row in rows]

    async def get_failed_requirements(
        self,
        job_id: uuid.UUID
    ) -> List[Dict[str, Any]]:
        """Get details about failed requirements.

        Args:
            job_id: Job UUID

        Returns:
            List of failed requirement details with error info
        """
        rows = await self.conn.fetch(
            """
            SELECT id, name, text, last_error, retry_count
            FROM requirements
            WHERE job_id = $1 AND status = 'failed'
            ORDER BY created_at
            """,
            job_id
        )
        return [dict(row) for row in rows]

    async def get_daily_statistics(
        self,
        days: int = 7
    ) -> List[Dict[str, Any]]:
        """Get daily job statistics.

        Args:
            days: Number of days to include

        Returns:
            List of daily statistics
        """
        rows = await self.conn.fetch(
            """
            SELECT
                DATE(created_at) as date,
                COUNT(*) as jobs_created,
                COUNT(*) FILTER (WHERE status = 'completed') as jobs_completed,
                COUNT(*) FILTER (WHERE status = 'failed') as jobs_failed
            FROM jobs
            WHERE created_at > CURRENT_TIMESTAMP - INTERVAL '%s days'
            GROUP BY DATE(created_at)
            ORDER BY date DESC
            """ % days
        )
        return [dict(row) for row in rows]


def create_reporter(conn: PostgresConnection) -> Reporter:
    """Factory function to create a Reporter instance.

    Args:
        conn: PostgreSQL connection instance

    Returns:
        Reporter instance
    """
    return Reporter(conn)
