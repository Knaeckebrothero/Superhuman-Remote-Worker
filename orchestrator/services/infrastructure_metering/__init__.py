"""Infrastructure allocation metering foundations.

Slice 0 provides typed reads, pricing/configuration contracts, and capability
checks. Slice 1 adds authenticated Kubernetes inventory, shadow reconciliation,
an explicit fleet-admin cutover, strict publication, coverage-aware sealing,
and source-aware reads. Every mutating/runtime path remains independently gated
and schema-probed; the defaults perform no collection, cutover, or publication.
"""

from .capabilities import MeteringSchemaCapabilities, probe_schema_capabilities
from .config import InfrastructureMeteringSettings
from .coverage import (
    CoverageGapWaiverResult,
    CoverageGapWaiverService,
)
from .cutover import (
    CutoverResumeResult,
    CutoverStatus,
    InfrastructureWorkspaceCutover,
    LegacyWorkspaceCutoverLedger,
)
from .cutover_ledger import LegacyWorkspaceUsageLedgerAdapter
from .materializer import InfrastructureUsageMaterializer
from .queries import UsageV2QueryService, UsageVisibility
from .read_model import SourceAwareUsageReadModel
from .sealer import InfrastructureUsageDaySealer
from .runtime import (
    InfrastructureMeteringRuntime,
    infrastructure_metering_runtime_loop,
)
from .rollup import (
    BootstrapStatus,
    TypedUsageDailyRollup,
    typed_usage_rollup_loop,
)
from .types import UsageSummaryV2

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
