"""Infrastructure allocation metering foundations.

Slice 0 deliberately contains only typed read/configuration contracts and
schema capability checks. Collectors and ledger publication remain disabled
until their later rollout gates are implemented and explicitly enabled.
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
