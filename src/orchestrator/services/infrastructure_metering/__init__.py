"""Infrastructure allocation metering foundations.

Slice 0 provides typed reads, pricing/configuration contracts, and capability
checks. Slice 1 adds authenticated Kubernetes inventory, shadow reconciliation,
an explicit fleet-admin cutover, strict publication, coverage-aware sealing,
and source-aware reads. Every mutating/runtime path remains independently gated
and schema-probed; the defaults perform no collection, cutover, or publication.
"""

from orchestrator.services.infrastructure_metering.capabilities import (
    MeteringSchemaCapabilities,
    probe_schema_capabilities,
)
from orchestrator.services.infrastructure_metering.config import (
    InfrastructureMeteringSettings,
)
from orchestrator.services.infrastructure_metering.coverage import (
    CoverageGapWaiverResult,
    CoverageGapWaiverService,
)
from orchestrator.services.infrastructure_metering.cutover import (
    CutoverResumeResult,
    CutoverStatus,
    InfrastructureWorkspaceCutover,
    LegacyWorkspaceCutoverLedger,
)
from orchestrator.services.infrastructure_metering.cutover_ledger import (
    LegacyWorkspaceUsageLedgerAdapter,
)
from orchestrator.services.infrastructure_metering.materializer import (
    InfrastructureUsageMaterializer,
)
from orchestrator.services.infrastructure_metering.queries import (
    UsageV2QueryService,
    UsageVisibility,
)
from orchestrator.services.infrastructure_metering.read_model import (
    SourceAwareUsageReadModel,
)
from orchestrator.services.infrastructure_metering.sealer import (
    InfrastructureUsageDaySealer,
)
from orchestrator.services.infrastructure_metering.runtime import (
    InfrastructureMeteringRuntime,
    infrastructure_metering_runtime_loop,
)
from orchestrator.services.infrastructure_metering.rollup import (
    BootstrapStatus,
    TypedUsageDailyRollup,
    typed_usage_rollup_loop,
)
from orchestrator.services.infrastructure_metering.types import UsageSummaryV2

__all__ = [
    "InfrastructureMeteringSettings",
    "InfrastructureMeteringRuntime",
    "InfrastructureUsageMaterializer",
    "InfrastructureUsageDaySealer",
    "InfrastructureWorkspaceCutover",
    "LegacyWorkspaceCutoverLedger",
    "LegacyWorkspaceUsageLedgerAdapter",
    "CoverageGapWaiverResult",
    "CoverageGapWaiverService",
    "MeteringSchemaCapabilities",
    "SourceAwareUsageReadModel",
    "BootstrapStatus",
    "TypedUsageDailyRollup",
    "CutoverResumeResult",
    "CutoverStatus",
    "UsageSummaryV2",
    "UsageV2QueryService",
    "UsageVisibility",
    "probe_schema_capabilities",
    "infrastructure_metering_runtime_loop",
    "typed_usage_rollup_loop",
]
