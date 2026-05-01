"""Tests for the global models API: env-var provider detection.

Post chunk 7 (models_yaml_removal), ``config/models.yaml`` is gone and
``/api/models`` reads exclusively from the DB-backed catalog. The legacy
YAML-shape tests + filtering tests that lived here were removed alongside
the file. The catalog endpoint itself is exercised by integration tests
under ``test_admin_models_api.py``.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "orchestrator"))

# main.py validates VECTOR_DB_URL at module level; set a dummy value so the
# import succeeds — tests here only exercise pure utility functions.
os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

from main import (  # noqa: E402
    _get_system_providers,
    _PROVIDER_ENV_KEYS,
)


# =============================================================================
# _get_system_providers
# =============================================================================


class TestGetSystemProviders:
    def test_no_env_vars(self):
        with patch.dict(os.environ, {}, clear=True):
            # Clear all provider env vars
            for env_vars in _PROVIDER_ENV_KEYS.values():
                for v in env_vars:
                    os.environ.pop(v, None)
            assert _get_system_providers() == set()

    def test_openai_key_set(self):
        env = {"OPENAI_API_KEY": "sk-test"}
        with patch.dict(os.environ, env, clear=False):
            providers = _get_system_providers()
            assert "openai" in providers

    def test_multiple_keys(self):
        env = {
            "OPENAI_API_KEY": "sk-test",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "GROQ_API_KEY": "gsk-test",
        }
        with patch.dict(os.environ, env, clear=False):
            providers = _get_system_providers()
            assert providers >= {"openai", "anthropic", "groq"}

    def test_empty_key_ignored(self):
        env = {"GOOGLE_API_KEY": ""}
        with patch.dict(os.environ, env, clear=False):
            providers = _get_system_providers()
            assert "google" not in providers
