"""Fail-closed rollout settings for infrastructure metering."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})
_STABLE_CLUSTER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _flag(source: Mapping[str, str], name: str, *, default: bool = False) -> bool:
    raw = source.get(name, "true" if default else "false").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


@dataclass(frozen=True)
class InfrastructureMeteringSettings:
    """Independent dark-launch gates; every gate defaults to off.

    ``publication_enabled`` is intentionally the strictest gate. Merely
    enabling v2 reads or shadow collection can never cause ledger writes.
    Runtime schema/source capability checks provide a second, independent
    publication fence once Slice 1 adds the publisher.
    """

    v2_reads_enabled: bool = False
    collector_enabled: bool = False
    shadow_enabled: bool = False
    publication_enabled: bool = False
    stable_cluster_id: str = ""

    @classmethod
    def from_env(
        cls, source: Mapping[str, str] | None = None
    ) -> "InfrastructureMeteringSettings":
        env = os.environ if source is None else source
        settings = cls(
            v2_reads_enabled=_flag(env, "INFRASTRUCTURE_METERING_V2_READS_ENABLED"),
            collector_enabled=_flag(env, "INFRASTRUCTURE_METERING_COLLECTOR_ENABLED"),
            shadow_enabled=_flag(env, "INFRASTRUCTURE_METERING_SHADOW_ENABLED"),
            publication_enabled=_flag(
                env, "INFRASTRUCTURE_METERING_PUBLICATION_ENABLED"
            ),
            stable_cluster_id=env.get(
                "INFRASTRUCTURE_METERING_STABLE_CLUSTER_ID", ""
            ).strip(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.stable_cluster_id and not _STABLE_CLUSTER_ID.fullmatch(
            self.stable_cluster_id
        ):
            raise ValueError(
                "infrastructure metering stable cluster id must be 1-128 "
                "characters using letters, digits, '.', '_', ':', or '-'"
            )
        if self.shadow_enabled and not self.collector_enabled:
            raise ValueError(
                "infrastructure metering shadow mode requires its collector"
            )
        if self.publication_enabled:
            missing: list[str] = []
            if not self.collector_enabled:
                missing.append("collector")
            if not self.shadow_enabled:
                missing.append("shadow")
            if not self.stable_cluster_id:
                missing.append("stable cluster id")
            if missing:
                raise ValueError(
                    "infrastructure metering publication requires " + ", ".join(missing)
                )
