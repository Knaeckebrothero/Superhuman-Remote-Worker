from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from orchestrator.operator_cli import canvas_viewer_database_attestation as cli


class _Database:
    def __init__(self) -> None:
        self.connection = object()
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.mark.asyncio
async def test_execute_uses_runtime_contract_and_closes_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = _Database()
    attest = AsyncMock()

    result = await cli.execute(db_factory=lambda: database, attest=attest)

    assert result == 0
    database.connect.assert_awaited_once_with()
    attest.assert_awaited_once_with(database.connection)
    database.disconnect.assert_awaited_once_with()
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "contains_database_coordinates_or_credentials": False,
        "event": "canvas_viewer_database_attestation",
        "status": "passed",
    }


@pytest.mark.asyncio
async def test_execute_closes_database_after_failed_attestation() -> None:
    database = _Database()
    attest = AsyncMock(side_effect=RuntimeError("postgresql://user:secret@host/db"))

    with pytest.raises(RuntimeError, match="secret"):
        await cli.execute(db_factory=lambda: database, attest=attest)

    database.disconnect.assert_awaited_once_with()


def test_main_redacts_unhandled_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail() -> int:
        raise RuntimeError("postgresql://user:secret@host/db")

    monkeypatch.setattr(cli, "execute", fail)

    assert cli.main([]) == 1
    output = capsys.readouterr().out
    assert "secret" not in output
    assert json.loads(output)["status"] == "failed"
