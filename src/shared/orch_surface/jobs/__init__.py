"""The canonical, framework-independent SRW job-management surface."""

from .descriptors import (
    CallerCtx,
    JobDescriptor,
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
    "CallerCtx",
    "JOB_DESCRIPTORS",
    "JobDescriptor",
    "caller_default_names",
    "get_descriptor",
    "get_descriptors",
    "make_bound_handler",
    "registry_metadata",
]
