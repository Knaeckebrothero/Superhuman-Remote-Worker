"""Boot-time dependency checks for completion status reordering."""

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
async def test_reorder_without_completion_commands_fails_before_db_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", False)
    monkeypatch.setattr(main, "COMPLETION_STATUS_REORDER_ENABLED", True)
    connect = AsyncMock()

    with patch.object(main.postgres_db, "connect", connect):
        with patch("main.sys.exit", side_effect=SystemExit) as exit_mock:
            with pytest.raises(SystemExit):
                async with main.lifespan(MagicMock()):
                    pass

    exit_mock.assert_called_once_with(1)
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_reorder_with_completion_commands_reaches_db_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(main, "COMPLETION_STATUS_REORDER_ENABLED", True)

    with patch.object(
        main.postgres_db,
        "connect",
        AsyncMock(side_effect=RuntimeError("stop after dependency check")),
    ) as connect:
        with pytest.raises(RuntimeError, match="stop after dependency check"):
            async with main.lifespan(MagicMock()):
                pass

    connect.assert_awaited_once()
