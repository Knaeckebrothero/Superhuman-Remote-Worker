#!/usr/bin/env python3
"""Compatibility wrapper for the in-image repository reconciliation CLI."""

from __future__ import annotations


from orchestrator.operator_cli.managed_repository_reconciliation import (
    _safe_inventory_counts,
    main,
)

__all__ = ["_safe_inventory_counts", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
