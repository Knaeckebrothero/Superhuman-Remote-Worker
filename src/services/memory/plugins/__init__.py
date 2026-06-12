"""Built-in memory plugins.

Modules in this package call ``register_memory_plugin`` at import time;
importing the package (done by src/services/memory/__init__.py) populates
MEMORY_PLUGIN_REGISTRY — the same import-time registration pattern as
src/tools/registry.py.

Phase-1 transplant slices land the legacy current-behaviour plugins here
(two-tier recall retriever, KB hybrid-search retriever, the extraction/
curation writers); Phase 3+ adds the measured improvements (reranker,
gate, bounded core). See docs/features/agent_memory_overhaul.md §5.
"""

from . import bounded  # noqa: F401  (import-time registration)
from . import legacy  # noqa: F401  (import-time registration)
from . import legacy_writers  # noqa: F401  (import-time registration)
from . import reranker  # noqa: F401  (import-time registration)
