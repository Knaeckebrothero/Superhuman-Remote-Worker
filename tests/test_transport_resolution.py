"""Transport-resolvability policy tests.

Guards knowledge-base/knowledge/issues/openrouter_auxiliary_crashes_session_via_memory_reranker.md:
a session/role that can never start must be caught before a pod spawns.
"""

from src.core.transport_resolution import (
    embedding_role_violation,
    llm_role_violation,
)


class TestLLMRoleViolation:
    def test_openrouter_with_key_in_env_ok(self):
        # The real incident: openrouter aux, key delivered via env_keys (not the
        # section). Must NOT be flagged — the chat model builds fine.
        section = {"model": "openrouter/minimax/minimax-m3", "provider": "openrouter"}
        env = {"OPENROUTER_API_KEY": "sk-or-xxx"}
        assert llm_role_violation("auxiliary", section, env=env) is None

    def test_openrouter_with_key_on_section_ok(self):
        section = {
            "model": "openrouter/x",
            "provider": "openrouter",
            "api_key": "sk-or-xxx",
        }
        assert llm_role_violation("llm", section, env={}) is None

    def test_openrouter_no_key_anywhere_flagged(self):
        section = {"model": "openrouter/minimax/minimax-m3", "provider": "openrouter"}
        reason = llm_role_violation("auxiliary", section, env={})
        assert reason is not None
        assert "openrouter" in reason
        assert "OPENROUTER_API_KEY" in reason
        assert "auxiliary" in reason

    def test_mistral_no_key_flagged(self):
        section = {"model": "mistral-large", "provider": "mistral"}
        reason = llm_role_violation("llm", section, env={})
        assert reason and "MISTRAL_API_KEY" in reason

    def test_openai_keyless_endpoint_not_flagged(self):
        # openai provider never raises on a missing key (falls back to the
        # "not-needed" sentinel) — a keyless self-hosted endpoint is valid.
        section = {
            "model": "gemma",
            "provider": "openai",
            "base_url": "https://ai.h4ll.app/v1",
        }
        assert llm_role_violation("llm", section, env={}) is None

    def test_codex_not_flagged(self):
        section = {"model": "gpt-5.5", "provider": "codex"}
        assert llm_role_violation("llm", section, env={}) is None

    def test_provider_defaults_to_openai(self):
        # No provider → treated as openai → never flagged for a missing key.
        section = {"model": "gpt-4o"}
        assert llm_role_violation("llm", section, env={}) is None

    def test_non_dict_section_is_safe(self):
        assert llm_role_violation("llm", None, env={}) is None  # type: ignore[arg-type]


class TestEmbeddingRoleViolation:
    def test_local_model_with_base_url_ok(self):
        env_keys = {
            "EMBEDDING_MODEL": "qwen3-embedding-8b",
            "EMBEDDING_PROVIDER": "local",
            "EMBEDDING_BASE_URL": "https://ai.h4ll.app/v1",
        }
        assert embedding_role_violation(env_keys) is None

    def test_local_model_no_base_url_flagged(self):
        env_keys = {
            "EMBEDDING_MODEL": "qwen3-embedding-8b",
            "EMBEDDING_PROVIDER": "local",
        }
        reason = embedding_role_violation(env_keys)
        assert reason is not None
        assert "EMBEDDING_BASE_URL" in reason
        assert "reranker" in reason

    def test_openai_provider_no_base_url_flagged(self):
        env_keys = {"EMBEDDING_MODEL": "text-embed", "EMBEDDING_PROVIDER": "openai"}
        assert embedding_role_violation(env_keys) is not None

    def test_no_embedding_model_not_flagged(self):
        # No override → cluster-default embedding on the pod; don't second-guess.
        assert embedding_role_violation({"EMBEDDING_BASE_URL": "x"}) is None
        assert embedding_role_violation({}) is None
        assert embedding_role_violation(None) is None

    def test_hosted_provider_without_base_url_not_flagged(self):
        # A hosted embedding provider (not local/openai-shaped) supplies its own
        # endpoint — a missing EMBEDDING_BASE_URL is not a violation.
        env_keys = {"EMBEDDING_MODEL": "voyage-3", "EMBEDDING_PROVIDER": "voyage"}
        assert embedding_role_violation(env_keys) is None
