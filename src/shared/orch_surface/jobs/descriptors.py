"""Framework-independent descriptors for the shared job-management surface."""

from __future__ import annotations

from dataclasses import dataclass
import functools
import inspect
from typing import Any, Awaitable, Callable, Literal

from ..client import AsyncCockpitClient

JobGroup = Literal["job_control", "job_inspection"]
JobPlane = Literal["control", "observability", "object"]
CallerKind = Literal["mcp", "session", "officer"]
GrantKind = Literal["explicit"]
JobHandler = Callable[..., Awaitable[str]]


@dataclass(frozen=True, slots=True)
class CallerCtx:
    """Trusted caller identity and lineage hidden from public tool schemas.

    ``project_ids`` contains bindings established by the session/token adapter;
    it is never populated from model arguments.  Only an exactly-one binding
    becomes an ``X-MCP-Scope`` header.  ``lineage_project_id`` is kept
    separately because an existing multi-project session can still have a
    primary project used by the job-create funnel without claiming that its
    reads are single-project scoped.
    """

    kind: CallerKind
    user_id: str | None = None
    project_ids: tuple[str, ...] = ()
    lineage_project_id: str | None = None
    thread_id: str | None = None
    parent_job_id: str | None = None
    explicit_scope: str | None = None
    resolve_job_id_prefixes: bool = False

    @property
    def project_scope(self) -> str | None:
        projects = tuple(dict.fromkeys(p for p in self.project_ids if p))
        if len(projects) == 1:
            return f"project:{projects[0]}"
        return None

    @property
    def scope_header(self) -> str | None:
        return self.project_scope or self.explicit_scope


@dataclass(frozen=True, slots=True)
class JobDescriptor:
    """One public job operation and the policy metadata shared by adapters."""

    name: str
    handler: JobHandler
    group: JobGroup
    plane: JobPlane
    caller_defaults: frozenset[CallerKind]
    phases: tuple[str, ...]
    grant: GrantKind | None
    gate: str | None
    short_description: str
    description: str
    public_signature: inspect.Signature

    def registry_metadata(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "module": "orchestrator.jobs",
            "function": self.name,
            "description": self.description,
            "short_description": self.short_description,
            "category": self.group,
            "group": self.group,
            "plane": self.plane,
            "caller_defaults": sorted(self.caller_defaults),
            "phases": list(self.phases),
        }
        if self.grant:
            metadata["grant"] = self.grant
        if self.gate:
            metadata["gate"] = self.gate
        return metadata


_DESCRIPTORS: dict[str, JobDescriptor] = {}


def descriptor(
    *,
    group: JobGroup,
    plane: JobPlane,
    caller_defaults: frozenset[CallerKind],
    grant: GrantKind | None = None,
    gate: str | None = None,
    phases: tuple[str, ...] = ("strategic", "tactical"),
) -> Callable[[JobHandler], JobHandler]:
    """Declare one job operation from its signature, docstring, and metadata."""

    def decorate(handler: JobHandler) -> JobHandler:
        name = handler.__name__
        if name in _DESCRIPTORS:
            raise RuntimeError(f"Duplicate job descriptor: {name}")
        signature = inspect.signature(handler)
        parameters = list(signature.parameters.values())
        if [parameter.name for parameter in parameters[:2]] != ["client", "caller"]:
            raise TypeError(
                f"{name} must begin with (client, caller); got "
                f"{[parameter.name for parameter in parameters[:2]]}"
            )
        description = inspect.getdoc(handler) or ""
        if not description:
            raise TypeError(f"{name} must have a public docstring")
        short_description = description.splitlines()[0].strip()
        if grant and not gate:
            raise TypeError(f"{name}: grant metadata requires a gate explanation")
        public_signature = signature.replace(parameters=parameters[2:])
        _DESCRIPTORS[name] = JobDescriptor(
            name=name,
            handler=handler,
            group=group,
            plane=plane,
            caller_defaults=caller_defaults,
            phases=phases,
            grant=grant,
            gate=gate,
            short_description=short_description,
            description=description,
            public_signature=public_signature,
        )
        return handler

    return decorate


def get_descriptors() -> tuple[JobDescriptor, ...]:
    """Return the deterministic canonical inventory."""
    return tuple(_DESCRIPTORS[name] for name in sorted(_DESCRIPTORS))


def get_descriptor(name: str) -> JobDescriptor:
    return _DESCRIPTORS[name]


def registry_metadata() -> dict[str, dict[str, Any]]:
    """Return agent-registry metadata derived only from descriptors."""
    return {item.name: item.registry_metadata() for item in get_descriptors()}


def caller_default_names(caller: CallerKind, group: JobGroup) -> frozenset[str]:
    """Return the current effective default subset for a caller and group."""
    return frozenset(
        item.name
        for item in get_descriptors()
        if item.group == group and caller in item.caller_defaults
    )


def make_bound_handler(
    item: JobDescriptor,
    *,
    client_provider: Callable[[], AsyncCockpitClient],
    caller_provider: Callable[[], CallerCtx],
) -> JobHandler:
    """Bind hidden dependencies while retaining the reviewed public signature."""

    @functools.wraps(item.handler)
    async def invoke(*args: Any, **kwargs: Any) -> str:
        client = client_provider()
        caller = caller_provider()
        with client.invocation_scope(
            user_id=caller.user_id,
            scope=caller.scope_header,
        ):
            return await item.handler(client, caller, *args, **kwargs)

    invoke.__name__ = item.name
    invoke.__qualname__ = item.name
    invoke.__signature__ = item.public_signature  # type: ignore[attr-defined]
    return invoke


__all__ = [
    "CallerCtx",
    "CallerKind",
    "JobDescriptor",
    "JobGroup",
    "JobPlane",
    "caller_default_names",
    "descriptor",
    "get_descriptor",
    "get_descriptors",
    "make_bound_handler",
    "registry_metadata",
]
