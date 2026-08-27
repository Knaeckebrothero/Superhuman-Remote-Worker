#!/usr/bin/env python3
"""Compatibility wrapper for the in-image repository reconciliation CLI."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_ORCHESTRATOR = _ROOT / "orchestrator"
for path in (_ROOT, _ORCHESTRATOR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from operator_cli.managed_repository_reconciliation import (  # noqa: E402
    _safe_inventory_counts,
    main,
)

__all__ = ["_safe_inventory_counts", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
