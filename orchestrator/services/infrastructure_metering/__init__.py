"""Infrastructure allocation metering foundations.

Slice 0 provides typed reads, pricing/configuration contracts, and capability
checks. The gated Slice 1 foundation adds authenticated inventory collection
and shadow interval reconciliation. Ledger publication remains unavailable
until its later rollout gate is implemented and explicitly enabled.
"""

from .capabilities import MeteringSchemaCapabilities, probe_schema_capabilities
from .config import InfrastructureMeteringSettings
from .queries import UsageV2QueryService, UsageVisibility
from .rollup import (
    BootstrapStatus,
    TypedUsageDailyRollup,
    typed_usage_rollup_loop,
)
from .types import UsageSummaryV2

__all__ = [
    "InfrastructureMeteringSettings",
    "MeteringSchemaCapabilities",
    "BootstrapStatus",
    "TypedUsageDailyRollup",
    "UsageSummaryV2",
    "UsageV2QueryService",
    "UsageVisibility",
    "probe_schema_capabilities",
    "typed_usage_rollup_loop",
]
