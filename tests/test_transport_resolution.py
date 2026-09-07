"""Transport-resolvability policy tests.

Guards knowledge-base/knowledge/issues/openrouter_auxiliary_crashes_session_via_memory_reranker.md:
a session/role that can never start must be caught before a pod spawns.
"""

import pytest

from shared.runtime.core.transport_resolution import (
    CitationTransportError,
    embedding_role_violation,
    is_openai_default_endpoint,
    llm_role_violation,
    resolve_citation_transport,
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


class TestCitationTransport:
    """Citation-client credential isolation
    (knowledge-base/knowledge/issues/citation_llm_api_key_isolation.md).

    ``OPENAI_API_KEY`` belongs to the chat model: it may be sent to
    api.openai.com and nowhere else. A dedicated ``CITATION_LLM_API_KEY`` is
    the citation client's key whenever it is set.
    """

    CUSTOM = "http://cite.internal:8080/v1"

    def test_dedicated_key_wins_over_openai_key(self):
        t = resolve_citation_transport(
            {
                "CITATION_LLM_URL": self.CUSTOM,
                "CITATION_LLM_API_KEY": "sk-cit",
                "OPENAI_API_KEY": "sk-openai",
            }
        )
        assert t.api_key == "sk-cit"
        assert t.base_url == self.CUSTOM
        assert t.key_source == "CITATION_LLM_API_KEY"

    def test_dedicated_key_wins_even_for_openai_endpoint(self):
        t = resolve_citation_transport(
            {"CITATION_LLM_API_KEY": "sk-cit", "OPENAI_API_KEY": "sk-openai"}
        )
        assert t.api_key == "sk-cit"
        assert t.base_url is None

    def test_base_url_alias_beats_env_name(self):
        # The orchestrator dispatches CITATION_LLM_BASE_URL and aliases it to
        # CITATION_LLM_URL; the dispatched name wins if both are present.
        t = resolve_citation_transport(
            {
                "CITATION_LLM_BASE_URL": "http://dispatched:1/v1",
                "CITATION_LLM_URL": "http://stale:2/v1",
                "CITATION_LLM_API_KEY": "sk-cit",
            }
        )
        assert t.base_url == "http://dispatched:1/v1"

    def test_custom_url_without_dedicated_key_never_leaks_openai_key(self):
        with pytest.raises(CitationTransportError) as exc:
            resolve_citation_transport(
                {"CITATION_LLM_URL": self.CUSTOM, "OPENAI_API_KEY": "sk-openai"}
            )
        msg = str(exc.value)
        assert "CITATION_LLM_API_KEY" in msg
        assert self.CUSTOM in msg
        assert "sk-openai" not in msg  # never echo the secret

    def test_custom_url_with_empty_dedicated_key_is_unset(self):
        # `CITATION_LLM_API_KEY=` in a .env file must not fall through to the
        # chat key either.
        with pytest.raises(CitationTransportError):
            resolve_citation_transport(
                {
                    "CITATION_LLM_URL": self.CUSTOM,
                    "CITATION_LLM_API_KEY": "",
                    "OPENAI_API_KEY": "sk-openai",
                }
            )

    def test_custom_url_without_any_key_is_still_a_config_error(self):
        # Fail closed: validity must not depend on whether OPENAI_API_KEY
        # happens to be present in the process.
        with pytest.raises(CitationTransportError) as exc:
            resolve_citation_transport({"CITATION_LLM_BASE_URL": self.CUSTOM})
        assert "CITATION_LLM_BASE_URL" in str(exc.value)

    def test_no_citation_vars_keeps_openai_default(self):
        t = resolve_citation_transport({"OPENAI_API_KEY": "sk-openai"})
        assert t.base_url is None
        assert t.api_key == "sk-openai"
        assert t.key_source == "OPENAI_API_KEY"

    def test_no_citation_vars_and_no_openai_key(self):
        # None → the openai factory's "not-needed" sentinel, as before.
        t = resolve_citation_transport({})
        assert t.base_url is None
        assert t.api_key is None
        assert t.key_source == "none"

    @pytest.mark.parametrize(
        "url",
        [
            "https://api.openai.com/v1",
            "https://api.openai.com/v1/",
            "https://API.OpenAI.com/v1",
            "api.openai.com/v1",
        ],
    )
    def test_openai_default_endpoint_may_use_openai_key(self, url):
        t = resolve_citation_transport(
            {"CITATION_LLM_URL": url, "OPENAI_API_KEY": "sk-openai"}
        )
        assert t.api_key == "sk-openai"
        assert t.base_url == url

    @pytest.mark.parametrize(
        "url",
        [
            "http://cite.internal:8080/v1",
            "https://openrouter.ai/api/v1",
            "https://api.openai.com.evil.example/v1",
            "https://evil.example/api.openai.com/v1",
            "http://[not-a-url",
        ],
    )
    def test_non_openai_hosts_are_custom(self, url):
        assert not is_openai_default_endpoint(url)

    def test_reads_process_env_by_default(self, monkeypatch):
        for var in (
            "CITATION_LLM_BASE_URL",
            "CITATION_LLM_URL",
            "CITATION_LLM_API_KEY",
            "OPENAI_API_KEY",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("CITATION_LLM_URL", self.CUSTOM)
        monkeypatch.setenv("CITATION_LLM_API_KEY", "sk-cit")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        t = resolve_citation_transport()
        assert (t.base_url, t.api_key) == (self.CUSTOM, "sk-cit")
