"""Exact pinned-session binding and its non-secret readiness proof."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import UUID


PINNED_SESSION_READY_IDENTITY_CONTRACT = 1
_DOMAIN = b"srw:pinned-session-ready:v1\0"


def _canonical_uuid(value: Any) -> str:
    return str(UUID(str(value)))


def _required_text(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\0" in value
    ):
        raise ValueError(f"{field} must be non-empty and NUL-free")
    return value


@dataclass(frozen=True, slots=True)
class PinnedSessionBinding:
    """One immutable reciprocal DB/routing snapshot for a pinned runtime."""

    thread_id: str
    runtime_generation: str
    agent_id: str
    runtime_attach_token: str = field(repr=False)
    agent_hostname: str
    pod_uid: str
    pod_ip: str
    pod_port: int
    agent_status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "thread_id", _canonical_uuid(self.thread_id))
        object.__setattr__(
            self,
            "runtime_generation",
            _canonical_uuid(self.runtime_generation),
        )
        object.__setattr__(self, "agent_id", _canonical_uuid(self.agent_id))
        object.__setattr__(
            self,
            "runtime_attach_token",
            _canonical_uuid(self.runtime_attach_token),
        )
        for attribute in ("agent_hostname", "pod_uid", "pod_ip", "agent_status"):
            object.__setattr__(
                self,
                attribute,
                _required_text(getattr(self, attribute), field=attribute),
            )
        if isinstance(self.pod_port, bool) or not isinstance(self.pod_port, int):
            raise ValueError("pod_port must be an integer")
        if self.pod_port < 1 or self.pod_port > 65_535:
            raise ValueError("pod_port must be between 1 and 65535")

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> PinnedSessionBinding:
        """Parse the aliases returned by the single joined DB query."""

        port = row.get("pod_port")
        return cls(
            thread_id=row.get("thread_id"),
            runtime_generation=row.get("runtime_generation"),
            agent_id=row.get("agent_id"),
            runtime_attach_token=row.get("runtime_attach_token"),
            agent_hostname=row.get("agent_hostname"),
            pod_uid=row.get("pod_uid"),
            pod_ip=row.get("pod_ip"),
            pod_port=8001 if port is None else port,
            agent_status=row.get("agent_status"),
        )

    @property
    def session_identity_fingerprint(self) -> str:
        fingerprint = pinned_session_ready_identity_fingerprint(
            thread_id=self.thread_id,
            runtime_generation=self.runtime_generation,
            agent_id=self.agent_id,
            runtime_attach_token=self.runtime_attach_token,
            pod_uid=self.pod_uid,
        )
        if fingerprint is None:  # pragma: no cover - __post_init__ proves it
            raise AssertionError("canonical binding failed to produce a fingerprint")
        return fingerprint

    @property
    def target_key(self) -> tuple[str, str, str, str, str, str, str, int]:
        """All immutable DB/routing coordinates compared after every await."""

        return (
            self.thread_id,
            self.runtime_generation,
            self.agent_id,
            self.runtime_attach_token,
            self.agent_hostname,
            self.pod_uid,
            self.pod_ip,
            self.pod_port,
        )


def pinned_session_ready_identity_fingerprint(
    *,
    thread_id: Any,
    runtime_generation: Any,
    agent_id: Any,
    runtime_attach_token: Any,
    pod_uid: Any,
) -> str | None:
    """Hash the complete local binding without disclosing its attach token."""

    try:
        identity = (
            str(UUID(str(thread_id))),
            str(UUID(str(runtime_generation))),
            str(UUID(str(agent_id))),
            str(UUID(str(runtime_attach_token))),
        )
    except (TypeError, ValueError, AttributeError):
        return None
    if (
        not isinstance(pod_uid, str)
        or not pod_uid
        or pod_uid != pod_uid.strip()
        or "\0" in pod_uid
    ):
        return None
    material = _DOMAIN + "\0".join((*identity, pod_uid)).encode("utf-8")
    return f"sha256:{hashlib.sha256(material).hexdigest()}"
