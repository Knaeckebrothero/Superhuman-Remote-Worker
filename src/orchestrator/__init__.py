"""Orchestrator package for job management and agent coordination.

This package provides:
- JobManager: Job lifecycle management (creation, status, cancellation)
- Monitor: Completion detection, stuck state detection, agent health
- Reporter: Job summary generation, statistics, citations

Example:
    ```python
    from src.database.postgres_utils import create_postgres_connection
    from src.orchestrator import (
        JobManager, create_job_manager,
        Monitor, create_monitor,
        Reporter, create_reporter
    )

    # Initialize
    conn = create_postgres_connection()
    await conn.connect()

    job_manager = create_job_manager(conn)
    monitor = create_monitor(conn)
    reporter = create_reporter(conn)

    # Create a job
    job_id = await job_manager.create_new_job(
        prompt="Analyze GDPR requirements",
        document_path="./data/gdpr.pdf"
    )

    # Wait for completion
    status = await monitor.wait_for_completion(job_id)

    # Generate report
    report = await reporter.generate_text_report(job_id)
    ```
"""

from src.orchestrator.job_manager import (
    JobManager,
    create_job_manager,
)
from src.orchestrator.monitor import (
    Monitor,
    JobCompletionStatus,
    HealthStatus,
    StuckJobInfo,
    create_monitor,
)
from src.orchestrator.reporter import (
    Reporter,
    JobSummary,
    RequirementStatistics,
    LLMStatistics,
    create_reporter,
)

__all__ = [
    # Job Management
    "JobManager",
    "create_job_manager",
    # Monitoring
    "Monitor",
    "JobCompletionStatus",
    "HealthStatus",
    "StuckJobInfo",
    "create_monitor",
    # Reporting
    "Reporter",
    "JobSummary",
    "RequirementStatistics",
    "LLMStatistics",
    "create_reporter",
]
