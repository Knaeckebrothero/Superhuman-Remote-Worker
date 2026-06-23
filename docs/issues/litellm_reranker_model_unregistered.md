# Memory reranker dead on LiteLLM clusters — `qwen3-reranker-8b` not registered

**Status:** Open · root cause confirmed · non-fatal (memory degrades gracefully)
**Found:** 2026-06-23, investigating session `6810288e` on the main cluster
**Component:** LiteLLM gateway model registry · memory recall reranker
(`src/services/memory/plugins/reranker.py`) · scoped-key minting
**Related:** [[session_empty_response_gpt5_codex_stop]] (surfaced in the same investigation)

## Symptom

On every persistent-session turn that runs memory recall, the agent logs:

```
POST http://srw-litellm:4000/v1/rerank "HTTP/1.1 403 Forbidden"
Memory scorer 'reranker' failed (contained): HTTPStatusError: 403 Forbidden
```

LiteLLM-side:

```
key not allowed to access model. This key can only access models=
['gemma-4-31b','gemma-4-moe','gpt-5.3-codex-spark','gpt-5.4-mini','gpt-5.5',
 'kokoro','qwen3-embedding-8b','whisper-large-v3']. Tried to access qwen3-reranker-8b
```

It is **non-fatal**: `MemoryManager` contains the failure and recall falls back
to dense + sparse fusion **without** reranking. But (a) it degrades recall
quality (Phase-3 reranker stage is inert) and (b) it fires an error every turn.

## Root cause

The reranker "rides the auxiliary endpoint" by design
(`config/defaults.yaml:253`, `config/persistent_defaults.yaml:175`:
`model: qwen3-reranker-8b`; `reranker.py` posts to `{aux_base_url}/rerank`). On
this cluster the auxiliary base_url is the LiteLLM gateway
(`http://srw-litellm:4000/v1`), and **`qwen3-reranker-8b` is not registered in
LiteLLM**.

Confirmed against `srw-litellmdb` (`LiteLLM_ProxyModelTable`): the registry
holds exactly the 8 models in the error's allow-list — the reranker is absent.
The scoped key's grant = all-registered-models, so the "403 key not allowed" is
just how LiteLLM reports an unregistered model, **not** a key-scoping mistake.
(Embeddings work because the session's `EMBEDDING_BASE_URL` points **direct** to
`https://ai.h4ll.app/v1`, bypassing LiteLLM — the reranker does not.)

## Fix options

1. **Register `qwen3-reranker-8b` in LiteLLM** (Admin → Models) pointing at the
   backend that serves it (same upstream as `gemma-4-moe` / `qwen3-embedding-8b`,
   i.e. `ai.h4ll.app`). Scoped keys then pick it up automatically. Verify the
   gateway exposes a Cohere-shaped `POST /rerank` for it.
2. **Point the reranker direct at the rerank backend** (like embeddings) via a
   `reranker.base_url` override, bypassing LiteLLM — quickest unblock, but loses
   gateway measurement/rate-limiting for rerank calls.
3. **Suppress the per-turn error** when the reranker model is unavailable
   (probe once, disable for the session) — cosmetic; do alongside 1 or 2.

Prefer option 1 (keeps the gateway as the single measurement plane, consistent
with the usage-monitoring design).
