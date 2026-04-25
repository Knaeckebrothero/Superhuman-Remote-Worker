"""Lightweight smoke tests for the stage-3 Admin → Providers API surface.

Full TestClient integration would require standing up the orchestrator's
heavy module-level init (Keycloak, DBs, etc.). Instead, we:

1. Import main.py and check that every admin route is registered.
2. Validate the Pydantic bodies that gate the routes.
3. Check the whitelist constants stay consistent with the DB CHECK
   constraints (system_api_keys) and the helper kinds (builder/browser/
   citation).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Allow importing the orchestrator main module (matches test_models_api.py).
_ORCH = Path(__file__).parent.parent / "orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))

os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

from main import (  # noqa: E402
    VALID_DEFAULT_MODEL_KINDS,
    VALID_SYSTEM_API_KEY_PROVIDERS,
    AdminDefaultModelSet,
    app,
)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


ADMIN_ROUTES = {
    ("GET", "/api/admin/providers/keys"),
    ("PUT", "/api/admin/providers/keys/{provider}"),
    ("DELETE", "/api/admin/providers/keys/{provider}"),
    ("GET", "/api/admin/providers/endpoints"),
    ("POST", "/api/admin/providers/endpoints"),
    ("PATCH", "/api/admin/providers/endpoints/{endpoint_id}"),
    ("DELETE", "/api/admin/providers/endpoints/{endpoint_id}"),
    ("POST", "/api/admin/providers/endpoints/{endpoint_id}/models"),
    ("PATCH", "/api/admin/providers/endpoints/{endpoint_id}/models/{model_id:path}"),
    ("DELETE", "/api/admin/providers/endpoints/{endpoint_id}/models/{model_id:path}"),
    ("POST", "/api/admin/providers/endpoints/{endpoint_id}/test"),
    ("GET", "/api/admin/providers/defaults"),
    ("PUT", "/api/admin/providers/defaults/{kind}"),
}


def _registered_routes() -> set[tuple[str, str]]:
    """(method, path) tuples for every FastAPI route."""
    out: set[tuple[str, str]] = set()
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        for m in methods:
            out.add((m, path))
    return out


class TestAdminRoutesRegistered:
    def test_every_admin_route_is_wired(self):
        registered = _registered_routes()
        missing = [r for r in ADMIN_ROUTES if r not in registered]
        assert not missing, f"missing admin routes: {missing}"


# ---------------------------------------------------------------------------
# Whitelists
# ---------------------------------------------------------------------------


class TestProviderWhitelist:
    def test_system_api_key_providers_matches_db_check_constraint(self):
        # schema.sql: CHECK (provider IN (...))
        db_allowed = {
            "openai",
            "anthropic",
            "google",
            "groq",
            "openrouter",
            "tavily",
            "vision",
        }
        assert VALID_SYSTEM_API_KEY_PROVIDERS == db_allowed

    def test_codex_is_not_a_system_provider(self):
        """Codex auth is user-bound via the proxy — never a shared system key."""
        assert "codex" not in VALID_SYSTEM_API_KEY_PROVIDERS


class TestDefaultModelKinds:
    def test_covers_core_workload_kinds(self):
        # Chat-slot defaults (always present).
        assert {"builder", "browser", "citation"}.issubset(VALID_DEFAULT_MODEL_KINDS)
        # Non-chat slots added with the capability extension.
        assert {"embedding", "vision", "auxiliary"}.issubset(VALID_DEFAULT_MODEL_KINDS)

    def test_audio_capability_kinds_present(self):
        # Whisper + TTS join the cluster-wide-default set so admins can pin
        # an audio model without each user setting their own preference.
        # Dispatch injects WHISPER_*/TTS_* env vars for the agent at job-start.
        assert {"whisper", "tts"}.issubset(VALID_DEFAULT_MODEL_KINDS)


# ---------------------------------------------------------------------------
# Pydantic bodies
# ---------------------------------------------------------------------------


class TestAdminDefaultModelSet:
    def test_accepts_model_id(self):
        body = AdminDefaultModelSet(model="RedHatAI/gemma-4-31B-it-FP8-Dynamic")
        assert body.model == "RedHatAI/gemma-4-31B-it-FP8-Dynamic"

    def test_accepts_empty_string_for_clearing(self):
        body = AdminDefaultModelSet(model="")
        assert body.model == ""

    def test_model_field_is_required(self):
        with pytest.raises(Exception):
            AdminDefaultModelSet()  # type: ignore[call-arg]
