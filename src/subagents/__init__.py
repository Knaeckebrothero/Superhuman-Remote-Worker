"""Built-in subagents (U3): a child session driven inside its parent's process.

Package layout:

- ``host``     — ``ParentHost`` (what a child needs from its parent, B.13),
                 ``WorkerHost`` (the worker job's implementation)
- ``ledger``   — ``SubagentLedger`` (durable child state; the null / recording impls)
- ``persistence`` — ``DbSubagentLedger`` (a ``threads`` row of kind='subagent'
                 + its ``thread_messages`` transcript, WP3)
- ``budgets``  — ``ChildBudgets`` + the staleness watcher (B.4)
- ``child``    — build the child from a resolved roster entry (B.2, B.8, B.9)
- ``driver``   — ``SubagentDriver`` on ``run_persistent_loop`` (B.3)
- ``runtime``  — ``SubagentRuntime`` per parent: roster, handles, semaphore,
                 batch sharing, idempotent re-execution (B.3, B.6)
- ``envelope`` — the return contract (B.5)
- ``fork``     — ``fork=true`` history seeding (B.7)

Import rule: nothing in this package imports ``src.tools.registry`` at module
level (registry → delegation → subagents → persistent_graph would cycle);
the ``delegate_agent`` tool factory and ``agent.py`` import this package
lazily.
"""

from .budgets import ChildBudgets, StalenessWatcher
from .child import (
    ChildBuild,
    SharedWriterGuard,
    SpawnRefused,
    build_child,
    build_child_config,
    select_child_tool_names,
)
from .driver import STOP, SubagentDriver, SubagentResult
from .envelope import build_envelope, neutralise_control_markers, return_budget
from .fork import seed_fork_history
from .host import ContextProbe, ParentHost, SimpleParentHost, WorkerHost
from .ledger import (
    SUBAGENT_STATUSES,
    NullLedger,
    RecordingLedger,
    SubagentLedger,
    is_terminal_status,
)
from .persistence import DbSubagentLedger
from .runtime import (
    BACKGROUND_UNAVAILABLE,
    SubagentCall,
    SubagentRecord,
    SubagentRuntime,
)

__all__ = [
    "BACKGROUND_UNAVAILABLE",
    "STOP",
    "SUBAGENT_STATUSES",
    "ChildBudgets",
    "ChildBuild",
    "ContextProbe",
    "DbSubagentLedger",
    "NullLedger",
    "ParentHost",
    "RecordingLedger",
    "SharedWriterGuard",
    "SimpleParentHost",
    "SpawnRefused",
    "StalenessWatcher",
    "SubagentCall",
    "SubagentDriver",
    "SubagentLedger",
    "SubagentRecord",
    "SubagentResult",
    "SubagentRuntime",
    "WorkerHost",
    "build_child",
    "build_child_config",
    "build_envelope",
    "is_terminal_status",
    "neutralise_control_markers",
    "return_budget",
    "seed_fork_history",
    "select_child_tool_names",
]
