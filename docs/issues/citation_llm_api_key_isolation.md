# Citation LLM auth: isolate from `OPENAI_API_KEY`

## Problem

The orchestrator now dispatches `CITATION_LLM_MODEL`, `CITATION_LLM_URL`, and
`CITATION_LLM_API_KEY` per-job (resolved from
`system_settings.default_citation_model` via Admin → Providers, see
`orchestrator/main.py:831` dispatch loop). The first two are honored by the
upstream `citation_engine` package, but `CITATION_LLM_API_KEY` is **not** —
the package falls back to `OPENAI_API_KEY` for any non-Groq provider.

Source (`citation_engine/engine.py:165-179`):

```python
self.llm_url = os.getenv("CITATION_LLM_URL")
self.llm_model = os.getenv("CITATION_LLM_MODEL", "gpt-4o-mini")
self.llm_provider = _resolve_llm_provider(self.llm_model, self.llm_url)

if self.llm_provider == "groq":
    self.llm_api_key = os.getenv("GROQ_API_KEY", "")
    ...
else:
    self.llm_api_key = os.getenv("OPENAI_API_KEY", "")
```

When `CITATION_LLM_URL` is set, `_resolve_llm_provider` returns `"custom"` —
which falls into the `else` branch and reads `OPENAI_API_KEY`. So a
self-hosted citation endpoint with its own credentials cannot authenticate
unless that same key happens to live in `OPENAI_API_KEY` for the job (which
the orchestrator already injects for the chat LLM, possibly with a different
endpoint's key).

This means: **the citation endpoint and the chat endpoint must currently
accept the same API key**. For deployments where citation runs on a
self-hosted vLLM with `not-needed` and chat runs on OpenAI with a real key,
this happens to work. For mixed-provider setups (citation on a different
managed provider than chat) it 401s.

## Why it didn't break the migration

The orchestrator-side wiring is correct and forward-compatible: it injects
`CITATION_LLM_API_KEY` already, anticipating a fix in the engine. The
migration also documents the limitation in `.env.example`,
`src/utils/citation_utils.py`, and the dispatch loop comment.

## Proposed fix

Patch the upstream `citation_engine` package (currently installed editable
from `/home/ghost/Repositories/Uni-Projekt-Graph-RAG/citation_tool`,
distributed via `git+https://github.com/Knaeckebrothero/CitationEngine.git`)
to read a dedicated key first, fall back to the shared one — mirroring the
existing pattern in `src/services/vision_helper.py:86`:

```python
# In citation_engine/engine.py
if self.llm_provider == "groq":
    self.llm_api_key = (
        os.getenv("CITATION_LLM_API_KEY")
        or os.getenv("GROQ_API_KEY", "")
    )
    ...
else:
    self.llm_api_key = (
        os.getenv("CITATION_LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY", "")
    )
```

Also update the package's docstring/README and add a test that pins the
precedence (dedicated key wins over `OPENAI_API_KEY`).

## Steps

1. Patch the package upstream
   (`/home/ghost/Repositories/Uni-Projekt-Graph-RAG/citation_tool`).
2. Cut a release / push to `main` on
   `github.com/Knaeckebrothero/CitationEngine`.
3. Bump the pinned commit in `requirements.txt:78`
   (`citation-engine[full] @ git+...`) so the deployed agent images get the
   fix.
4. Drop the "informational only" caveat from the citation block in
   `.env.example`, `.env`, and the docstring in
   `src/utils/citation_utils.py`.
5. Optional: stop the dispatch-loop alias hack in `orchestrator/main.py`
   (the `if _kind == "citation" and "CITATION_LLM_BASE_URL" in env_keys_block`
   block) once the package also reads `CITATION_LLM_BASE_URL` natively as
   an alias for `CITATION_LLM_URL` — or leave it, it's cheap.

## Out of scope

- Per-user citation model pinning. `UserSettingsUpdate` in
  `orchestrator/main.py:2598` does not yet expose `default_citation_model`;
  only the system-level pin works today. Worth adding alongside the other
  per-user model preferences if/when there's a user case for it.
- Wiring citation injection into the persistent-thread create paths
  (`agent_create_thread` ~line 9120, thread create ~line 9650). They
  currently only inject embedding, since persistent agents are
  chat-focused. Trivial to add if persistent agents ever need citation.
