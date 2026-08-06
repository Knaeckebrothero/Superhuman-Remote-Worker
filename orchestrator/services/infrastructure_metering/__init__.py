"""Infrastructure allocation metering foundations.

Slice 0 provides typed reads, pricing/configuration contracts, and capability
checks. The gated Slice 1 foundation adds authenticated inventory collection
and shadow interval reconciliation. Strict frozen-plan publication mechanics
exist as dark code, but no runtime loop starts them and the independent gate
defaults off until legacy repair and the durable cutover are implemented.
"""

from .capabilities import MeteringSchemaCapabilities, probe_schema_capabilities
from .config import InfrastructureMeteringSettings
from .materializer import InfrastructureUsageMaterializer
from .queries import UsageV2QueryService, UsageVisibility
from .rollup import (
    BootstrapStatus,
    TypedUsageDailyRollup,
    typed_usage_rollup_loop,
)
from .types import UsageSummaryV2

__all__ = [
    "InfrastructureMeteringSettings",
    "InfrastructureUsageMaterializer",
    "MeteringSchemaCapabilities",
    "BootstrapStatus",
    "TypedUsageDailyRollup",
    "UsageSummaryV2",
    "UsageV2QueryService",
    "UsageVisibility",
    "probe_schema_capabilities",
    "typed_usage_rollup_loop",
]
