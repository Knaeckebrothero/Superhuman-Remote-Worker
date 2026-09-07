"""Paper-provider health snapshots and worker-image readiness probe.

This module intentionally does not participate in Kubernetes liveness.  Paper
providers are optional and the Scholar has a web fallback, so a transient
external outage must not make an otherwise useful worker unready.  Operators
and deployment acceptance can run the module inside the actual agent image:

    python -m agent.tools.research.utils.provider_health

The output contains dependency/provider states and credential *presence* only;
it never serializes credential values or provider response bodies.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
from functools import lru_cache
from typing import Any

from agent.tools.research.utils.semantic_scholar_client import (
    get_semantic_scholar_health,
    probe_semantic_scholar,
)


@lru_cache(maxsize=1)
def get_arxiv_health() -> dict[str, Any]:
    """Check the installed arxiv.py API contract without network traffic."""

    try:
        import arxiv

        version = importlib.metadata.version("arxiv")
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        return {
            "state": "unavailable",
            "version": None,
            "message": f"arxiv dependency is unavailable: {type(exc).__name__}",
        }

    if not callable(getattr(arxiv.Client, "results", None)):
        return {
            "state": "incompatible",
            "version": version,
            "message": "Installed arxiv.Client does not expose results(search).",
        }

    return {
        "state": "ready",
        "version": version,
        "message": "Installed arxiv.Client exposes results(search).",
    }


def get_paper_provider_health() -> dict[str, dict[str, Any]]:
    """Return cached/local checks only; never performs external I/O."""

    return {
        "arxiv": get_arxiv_health(),
        "semantic_scholar": get_semantic_scholar_health(),
    }


async def probe_paper_providers(*, timeout: float = 15) -> dict[str, Any]:
    """Run the real Semantic Scholar handshake plus the local arXiv check."""

    semantic = await probe_semantic_scholar(timeout=timeout)
    providers = {
        "arxiv": get_arxiv_health(),
        "semantic_scholar": semantic,
    }
    return {
        "ready": all(item.get("state") == "ready" for item in providers.values()),
        "providers": providers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a secret-free paper-provider worker readiness probe."
    )
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()
    result = asyncio.run(probe_paper_providers(timeout=args.timeout))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
