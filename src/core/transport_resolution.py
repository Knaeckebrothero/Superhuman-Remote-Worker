"""Transport-resolvability policy (shared, dependency-free).

Mirrors the raise conditions in ``src/core/loader.py``'s ``create_llm``
factories so the orchestrator dispatch pre-flight and the agent loader agree on
what counts as a *usable* transport — without the orchestrator importing the
full loader (which pulls ``aiosqlite``, absent in the orchestrator image). Pure
stdlib only.

The point: a session/role that can never start (a chat model whose provider
factory will raise for a missing key, or an embedding endpoint the memory
reranker rides but can't reach) should be rejected BEFORE a pod is spawned,
with an actionable reason — not crash the agent at startup and hang the UI.
See knowledge-base/knowledge/issues/openrouter_auxiliary_crashes_session_via_memory_reranker.md.

Per-provider base_url / api_key behaviour, from the loader factories:

- ``openrouter`` / ``mistral`` / ``anthropic`` / ``google`` / ``groq``: supply a
  built-in default base_url, so the endpoint is never the problem — but they
  RAISE when no api_key resolves (``config.api_key or os.getenv(<PROVIDER>_API_KEY)``).
- ``openai`` (and the unset default): ``base_url = config.base_url`` (None →
  api.openai.com); the key falls back to a ``"not-needed"`` sentinel and never
  raises. So a keyless/self-hosted openai-shaped endpoint is valid (no key check)
  and only a *missing* base_url on an endpoint-backed model is wrong — but that
  isn't distinguishable from a genuine OpenAI model at this layer, so it is not
  flagged here (it 401s at runtime, it does not crash startup).
- ``codex``: default base_url (CODEX proxy), keyless. Never flagged.
"""

from typing import Mapping, Optional

# Providers whose create_llm factory RAISES when no api_key resolves.
KEY_REQUIRED_PROVIDERS = frozenset(
    {"openrouter", "mistral", "anthropic", "google", "groq"}
)

# The env var each provider's factory falls back to (config.api_key or getenv).
PROVIDER_ENV_KEY = {
    "openrouter": "OPENROUTER_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def llm_role_violation(
    role: str, section: Mapping, *, env: Optional[Mapping] = None
) -> Optional[str]:
    """Reason string if an LLM role's resolved transport is unusable (would raise
    in ``create_llm``), else ``None``.

    ``section`` is the credential-injected config-override dict for the role
    (e.g. ``blob['agent']['llm']`` / ``blob['agent']['auxiliary']``). ``env``
    should combine the delivery blob's ``env_keys`` with the process
    environment, since the orchestrator injects some provider keys into
    ``env_keys`` (e.g. ``OPENROUTER_API_KEY``) rather than onto the section —
    which is exactly why an OpenRouter chat model builds while only the reranker
    crashed in the original incident.
    """
    if env is None:
        import os

        env = os.environ
    if not isinstance(section, Mapping):
        return None
    provider = (section.get("provider") or "openai").lower()
    model = section.get("model") or "?"
    api_key = section.get("api_key")
    if provider in KEY_REQUIRED_PROVIDERS:
        env_key = PROVIDER_ENV_KEY.get(provider, "")
        if not api_key and not (env_key and env.get(env_key)):
            return (
                f"{role} model '{model}' ({provider}) has no usable api_key — "
                f"none injected and {env_key} is unset; the agent's create_llm "
                f"will raise at startup"
            )
    return None


def embedding_role_violation(
    env_keys: Optional[Mapping],
) -> Optional[str]:
    """Reason string if the *configured* embedding transport is unusable, else
    ``None``.

    The memory reranker rides the embedding endpoint (``EMBEDDING_BASE_URL``), so
    an unresolvable embedding transport now means the reranker can't build either
    — the exact class of the original crash. Only ``env_keys`` is consulted (not
    the process env): a session that carries no ``EMBEDDING_MODEL`` override
    relies on the agent pod's cluster-default embedding env, which the
    orchestrator can't see and must not second-guess (no false rejects). We flag
    only the unambiguous case: the delivery blob resolved an embedding *model*
    but no *endpoint* for it (a decrypt-miss or a missing endpoint base_url).
    """
    if not env_keys:
        return None
    model = env_keys.get("EMBEDDING_MODEL")
    if not model:
        return None  # not overridden → cluster default, don't second-guess
    provider = (env_keys.get("EMBEDDING_PROVIDER") or "local").lower()
    base_url = env_keys.get("EMBEDDING_BASE_URL")
    if provider in ("local", "openai") and not base_url:
        return (
            f"embedding model '{model}' ({provider}) resolved but no "
            f"EMBEDDING_BASE_URL — memory, KB, and the reranker cannot reach the "
            f"embedding endpoint"
        )
    return None
