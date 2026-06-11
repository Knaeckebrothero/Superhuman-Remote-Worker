"""MemoryManager seam — single entry point for agent memory.

Design: docs/features/agent_memory_overhaul.md. Phase-1 kernel: data
vocabulary (types), plugin protocols, registry, and the binder
(MemoryManager.from_config). Current behaviour transplants into
registered plugins in the follow-up slices; until the cutover flag
(memory.manager.enabled) flips, nothing in production constructs this.
"""

from .manager import MemoryManager
from .registry import (
    MEMORY_PLUGIN_REGISTRY,
    MemoryPluginSpec,
    UnknownMemoryPluginError,
    available_memory_plugins,
    register_memory_plugin,
)
from .types import (
    CAPTURE_KINDS,
    AssembleRequest,
    AssembleStats,
    BucketRef,
    Candidate,
    CaptureEvent,
    InjectionBlock,
    MemoryPayload,
    MemoryRuntime,
    Query,
    Scored,
    TaskFrame,
)

# Importing the plugins package registers the built-in plugins (same
# import-time registration as src/tools/registry.py). Empty until the
# Phase-1 transplant slices land.
from . import plugins  # noqa: F401  (import for side effect)

__all__ = [
    "MemoryManager",
    "MEMORY_PLUGIN_REGISTRY",
    "MemoryPluginSpec",
    "UnknownMemoryPluginError",
    "available_memory_plugins",
    "register_memory_plugin",
    "CAPTURE_KINDS",
    "AssembleRequest",
    "AssembleStats",
    "BucketRef",
    "Candidate",
    "CaptureEvent",
    "InjectionBlock",
    "MemoryPayload",
    "MemoryRuntime",
    "Query",
    "Scored",
    "TaskFrame",
]
