"""The canonical, framework-independent SRW job-management surface."""

from .descriptors import (
    AUTH_CONTEXT_FAILURE_NOTICE,
    CallerCtx,
    JobDescriptor,
    JobToolResult,
    ToolImageAttachment,
    caller_default_names,
    get_descriptor,
    get_descriptors,
    make_bound_handler,
    registry_metadata,
)

# Importing operation modules is the single registration side effect.
from . import control as _control  # noqa: F401,E402
from . import inspection as _inspection  # noqa: F401,E402

JOB_DESCRIPTORS = get_descriptors()

__all__ = [
    "AUTH_CONTEXT_FAILURE_NOTICE",
    "CallerCtx",
    "JOB_DESCRIPTORS",
    "JobDescriptor",
    "JobToolResult",
    "ToolImageAttachment",
    "caller_default_names",
    "get_descriptor",
    "get_descriptors",
    "make_bound_handler",
    "registry_metadata",
]
