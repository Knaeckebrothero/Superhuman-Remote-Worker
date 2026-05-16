"""Regression test for `_inject_dispatch_credentials` phase-override handling.

Covers the 2026-05-12 bug where a job's `config_override` of the shape
`{"llm": {"tactical": {"model": "X"}, "strategic": {"model": "X"}}}`
walked through dispatch with no `base_url`/`api_key` injection on the
phase blocks. The agent's LLM factory then fell back to the parent's
`base_url` and returned `404 Model 'X' not found` in an infinite retry
loop.

See docs/issues/orchestrator_phase_override_credentials_not_injected.md
for the incident write-up.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# Same path-setup pattern as test_create_builder_llm.py.
_ORCH = Path(__file__).parent.parent / "orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))

os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

import main  # noqa: E402
from src.core.model_registry import ModelMeta  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _job(*, job_id: str = "00000000-0000-0000-0000-000000000001") -> dict:
    """Minimal job dict for the dispatch path."""
    return {
        "id": job_id,
        "user_id": "00000000-0000-0000-0000-0000000000aa",
        "project_id": "00000000-0000-0000-0000-0000000000bb",
    }


CODEX_ENDPOINT_ID = "11111111-1111-1111-1111-111111111111"
CODEX_BASE_URL = "http://srw-codex-proxy:8317/v1"
CODEX_API_KEY = "sk-codex-test"


@pytest.fixture
def patched_main(monkeypatch):
    """Patch the DB + registry collaborators of `_inject_dispatch_credentials`
    so the test exercises only the injection branching.

    `_resolve_model` is wired to a side_effect that returns a real
    `ModelMeta` for `gpt-5.3-codex-spark` (origin=custom, endpoint pointing
    at the codex proxy) and `None` for everything else — mirroring the
    failing job's registry state.
    """

    async def fake_resolve(model_id, user_id=None):
        if model_id == "gpt-5.3-codex-spark":
            return ModelMeta(
                model_id="gpt-5.3-codex-spark",
                provider="openai",
                family="codex",
                display_name="GPT-5.3 Codex Spark",
                origin="custom",
                endpoint_id=CODEX_ENDPOINT_ID,
                api_key_ref="openai",
            )
        return None

    monkeypatch.setattr(
        main, "_resolve_model", AsyncMock(side_effect=fake_resolve), raising=True
    )

    async def fake_get_endpoint(endpoint_id):
        if endpoint_id == CODEX_ENDPOINT_ID:
            return {
                "id": CODEX_ENDPOINT_ID,
                "label": "codex-proxy",
                "base_url": CODEX_BASE_URL,
                "api_key": CODEX_API_KEY,
            }
        return None

    monkeypatch.setattr(
        main.postgres_db,
        "get_user_llm_endpoint",
        AsyncMock(side_effect=fake_get_endpoint),
    )
    monkeypatch.setattr(
        main.postgres_db,
        "resolve_api_keys_for_job",
        AsyncMock(return_value={}),
    )
    # No user-default fallback — the incident scenario is purely
    # job-override-driven.
    monkeypatch.setattr(
        main.postgres_db,
        "get_user_settings",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        main.postgres_db,
        "resolve_default_for_capability",
        AsyncMock(return_value=None),
    )


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------


class TestPhaseOverrideCredentialInjection:
    @pytest.mark.asyncio
    async def test_tactical_and_strategic_overrides_get_endpoint_injected(
        self, patched_main
    ):
        """The exact failing config_override from the 2026-05-12 incident."""
        override = {
            "llm": {
                "tactical": {"model": "gpt-5.3-codex-spark"},
                "strategic": {"model": "gpt-5.3-codex-spark"},
            }
        }

        result = await main._inject_dispatch_credentials(_job(), override)

        assert result["llm"]["tactical"]["base_url"] == CODEX_BASE_URL
        assert result["llm"]["tactical"]["api_key"] == CODEX_API_KEY
        assert result["llm"]["strategic"]["base_url"] == CODEX_BASE_URL
        assert result["llm"]["strategic"]["api_key"] == CODEX_API_KEY

    @pytest.mark.asyncio
    async def test_auxiliary_override_also_injected(self, patched_main):
        """Same hole existed for top-level `auxiliary` overrides."""
        override = {"auxiliary": {"model": "gpt-5.3-codex-spark"}}

        result = await main._inject_dispatch_credentials(_job(), override)

        assert result["auxiliary"]["base_url"] == CODEX_BASE_URL
        assert result["auxiliary"]["api_key"] == CODEX_API_KEY

    @pytest.mark.asyncio
    async def test_existing_base_url_in_phase_block_is_not_overwritten(
        self, patched_main
    ):
        """Caller-supplied base_url wins; injection is additive-only."""
        override = {
            "llm": {
                "tactical": {
                    "model": "gpt-5.3-codex-spark",
                    "base_url": "https://caller-pinned.example/v1",
                }
            }
        }

        result = await main._inject_dispatch_credentials(_job(), override)

        assert (
            result["llm"]["tactical"]["base_url"] == "https://caller-pinned.example/v1"
        )
        # The guard skips injection entirely when base_url is already set,
        # so api_key from the endpoint is NOT injected either — matching
        # the pre-existing _inject_model_credentials early-return contract.
        assert "api_key" not in result["llm"]["tactical"]

    @pytest.mark.asyncio
    async def test_unknown_phase_model_logs_warning_no_crash(
        self, patched_main, caplog
    ):
        """A phase pin for a model the registry doesn't know must not crash —
        it should log a warning and leave the section unmodified for the
        downstream agent to surface a clear error."""
        override = {"llm": {"tactical": {"model": "does-not-exist"}}}

        with caplog.at_level("WARNING", logger=main.logger.name):
            result = await main._inject_dispatch_credentials(_job(), override)

        assert result["llm"]["tactical"]["model"] == "does-not-exist"
        assert "base_url" not in result["llm"]["tactical"]
        assert any(
            "does-not-exist" in rec.message and "tactical" in rec.message
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_empty_override_is_a_noop(self, patched_main):
        """No phase sections → no injection, no crashes."""
        result = await main._inject_dispatch_credentials(_job(), {})

        assert "tactical" not in result.get("llm", {})
        assert "strategic" not in result.get("llm", {})
        assert "auxiliary" not in result
