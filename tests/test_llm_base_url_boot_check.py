"""Tests for the LLM_BASE_URL boot check (chunk 6 of models_yaml_removal).

The orchestrator hard-fails (sys.exit(1)) when ``LLM_BASE_URL`` is set
because the env-var-driven routing for self-hosted "Local" group models
was removed; leaving it set with no consumer is exactly the "active
misconfiguration that won't self-heal" path that produced the 401-against-
api.openai.com bug captured in docs/llm_routing_issues.md.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ORCH = Path(__file__).parent.parent / "orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))

os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

import main  # noqa: E402


@pytest.mark.asyncio
async def test_lifespan_exits_when_llm_base_url_set(caplog):
    """When LLM_BASE_URL is set at boot, the lifespan handler logs an
    ERROR and calls sys.exit(1) before any DB connection happens."""
    with patch.dict(os.environ, {"LLM_BASE_URL": "http://stale-vllm:8080/v1"}):
        with patch("main.sys.exit", side_effect=SystemExit) as mock_exit:
            with patch.object(main.postgres_db, "connect", AsyncMock()):
                with pytest.raises(SystemExit):
                    async with main.lifespan(MagicMock()):
                        pass
    mock_exit.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_lifespan_proceeds_when_llm_base_url_unset():
    """Without LLM_BASE_URL, the boot check is a no-op and lifespan
    proceeds to its DB-connect step."""
    env = {k: v for k, v in os.environ.items() if k != "LLM_BASE_URL"}
    with patch.dict(os.environ, env, clear=True):
        with patch("main.sys.exit") as mock_exit:
            # Stub everything past the LLM_BASE_URL check so we can verify
            # exit was not called without actually starting the orchestrator.
            with patch.object(
                main.postgres_db,
                "connect",
                AsyncMock(side_effect=RuntimeError("stop here")),
            ):
                with pytest.raises(RuntimeError, match="stop here"):
                    async with main.lifespan(MagicMock()):
                        pass
    mock_exit.assert_not_called()
