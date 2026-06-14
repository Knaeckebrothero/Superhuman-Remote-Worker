"""Unit tests for ``security.access.redact_config_override``.

Pure function, no DB. Mirrors ``TestRedactDatasource`` in
``tests/test_datasource_access.py``. Guards the credential-leak fix: secrets
must be stripped from ``config_override`` at the API boundary and before
persistence, while every non-secret field is preserved verbatim.
"""

import copy

from security import access


def _full_config_override() -> dict:
    """A config_override carrying every secret-bearing path we inject."""
    return {
        "llm": {
            "model": "gemini-3.5-flash",
            "provider": "google",
            "base_url": "https://ai.h4ll.app/v1",
            "temperature": 0.0,
            "api_key": "sk-llm-SECRET",
            "strategic": {"model": "opus", "api_key": "sk-strategic-SECRET"},
            "tactical": {"model": "sonnet", "api_key": "sk-tactical-SECRET"},
            "summarization": {"model": "haiku", "api_key": "sk-sum-SECRET"},
        },
        "auxiliary": {
            "model": "gemma-4-moe",
            "provider": "openai",
            "base_url": "https://ai.h4ll.app/v1",
            "api_key": "sk-aux-SECRET",
        },
        "env_keys": {
            "EMBEDDING_MODEL": "qwen3-embedding-8b",
            "EMBEDDING_BASE_URL": "https://ai.h4ll.app/v1",
            "EMBEDDING_PROVIDER": "openai",
            "EMBEDDING_API_KEY": "sk-emb-SECRET",
            "OPENROUTER_API_KEY": "sk-or-SECRET",
            "VISION_API_KEY": "sk-vis-SECRET",
            "CITATION_LLM_API_KEY": "sk-cit-SECRET",
            "CITATION_LLM_MODEL": "gpt-4o",
        },
        "workspace": {
            "backend": "virtual",
            "mounts": [
                {"name": "home", "prefix": "u/", "rclone_spec": "s3:KEY:SECRET@bucket"},
            ],
        },
        "interactive": {"permission_mode": "autonomous"},
    }


class TestRedactConfigOverride:
    def test_strips_top_level_and_phase_llm_keys(self):
        out = access.redact_config_override(_full_config_override())
        assert "api_key" not in out["llm"]
        assert "api_key" not in out["llm"]["strategic"]
        assert "api_key" not in out["llm"]["tactical"]
        assert "api_key" not in out["llm"]["summarization"]
        assert "api_key" not in out["auxiliary"]

    def test_strips_env_key_api_keys_only(self):
        out = access.redact_config_override(_full_config_override())
        env = out["env_keys"]
        # All *_API_KEY removed
        assert "EMBEDDING_API_KEY" not in env
        assert "OPENROUTER_API_KEY" not in env
        assert "VISION_API_KEY" not in env
        assert "CITATION_LLM_API_KEY" not in env
        # Non-secret env knobs preserved
        assert env["EMBEDDING_MODEL"] == "qwen3-embedding-8b"
        assert env["EMBEDDING_BASE_URL"] == "https://ai.h4ll.app/v1"
        assert env["EMBEDDING_PROVIDER"] == "openai"
        assert env["CITATION_LLM_MODEL"] == "gpt-4o"

    def test_strips_rclone_spec_in_list(self):
        out = access.redact_config_override(_full_config_override())
        mount = out["workspace"]["mounts"][0]
        assert "rclone_spec" not in mount
        # Sibling, non-secret fields preserved
        assert mount["name"] == "home"
        assert mount["prefix"] == "u/"

    def test_preserves_non_secret_fields(self):
        out = access.redact_config_override(_full_config_override())
        assert out["llm"]["model"] == "gemini-3.5-flash"
        assert out["llm"]["provider"] == "google"
        assert out["llm"]["base_url"] == "https://ai.h4ll.app/v1"
        assert out["llm"]["temperature"] == 0.0
        assert out["llm"]["strategic"]["model"] == "opus"
        assert out["auxiliary"]["model"] == "gemma-4-moe"
        assert out["workspace"]["backend"] == "virtual"
        assert out["interactive"]["permission_mode"] == "autonomous"

    def test_no_secret_string_survives_anywhere(self):
        import json

        out = access.redact_config_override(_full_config_override())
        assert "SECRET" not in json.dumps(out)

    def test_case_insensitive(self):
        co = {"llm": {"API_KEY": "x", "Api_Key": "y"}, "Password": "z", "TOKEN": "t"}
        out = access.redact_config_override(co)
        assert out["llm"] == {}
        assert "Password" not in out
        assert "TOKEN" not in out

    def test_does_not_mutate_input(self):
        co = _full_config_override()
        before = copy.deepcopy(co)
        access.redact_config_override(co)
        assert co == before

    def test_idempotent(self):
        co = _full_config_override()
        once = access.redact_config_override(co)
        twice = access.redact_config_override(once)
        assert once == twice

    def test_handles_non_dict_inputs(self):
        assert access.redact_config_override(None) is None
        assert access.redact_config_override({}) == {}
        assert access.redact_config_override("scalar") == "scalar"
        assert access.redact_config_override([{"api_key": "x", "model": "m"}]) == [
            {"model": "m"}
        ]
