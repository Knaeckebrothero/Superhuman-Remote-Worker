# config_override credential leak — at-rest plaintext + unredacted API responses

**Status:** ✅ Implemented, unit-tested, and **E2E-verified on k3d** (2026-06-14).
Merged to `develop`; pending dev/prod CI-CD rollout.

## Summary

Resolved plaintext LLM / embedding / auxiliary API keys leaked two ways:

1. **At rest.** Thread creation decrypts keys from the source tables and injects
   them into `config_override`, then persisted the *enriched* copy into
   `threads.metadata.config_override` (JSONB) **in plaintext** (also on model
   hot-swap via `merge_thread_config_override`). Confirmed by direct DB read on
   session `a6436fb8`: a shared homelab embedding/aux key (`sk-mo-…`,
   `ai.h4ll.app`) and the LLM key were stored in cleartext.
2. **Over the API.** User-token GET endpoints returned that `config_override`
   unredacted (`GET /api/jobs[/{id}]`, `GET /api/projects/{id}/jobs`,
   `GET /api/persistent/threads[/{id}]`), so a session owner — including a
   non-admin — received **shared infrastructure credentials** in plaintext (and
   the cockpit pulled them into the browser).

This is the "encryption at rest bypassed at the response boundary" pattern that
`docs/multi_tenancy.md:301` already flagged for datasources; it was never
applied to `config_override`. The AES-GCM encryption-at-rest scheme
(`orchestrator/security/crypto.py`) protects the *source* key tables; the
*derived* copy in `config_override` escaped it.

**Worker jobs were already correct** — `jobs.config_override` is bare at rest and
re-injected in-flight at dispatch *and resume* (`_inject_dispatch_credentials`).
The leak was threads-only. The fix makes threads behave like jobs.

## Fix

Decision: **strip + re-inject** (never store secrets), not encrypt-in-place —
strongest at-rest posture and one mental model shared with jobs.

**Fix 1 — API redaction (defense in depth at the boundary).**
- New `redact_config_override(co)` in `orchestrator/security/access.py`:
  recursive, non-mutating, key-name based (strips `api_key`, `*_API_KEY`,
  `password`, `secret`, `token`, `private_key`, `rclone_spec`; preserves
  `model`/`provider`/`base_url`/`*_MODEL`/`*_BASE_URL`/`*_PROVIDER`/etc.).
- Applied at the 5 user-facing GET endpoints via `_redact_job_config_override` /
  `_redact_thread_metadata` (type-preserving: re-serialize so response shape is
  unchanged). `require_internal` agent endpoints are intentionally **not**
  redacted (agent trust boundary).

**Fix 2 — strip at rest, re-inject in-flight.**
- Extracted `_inject_thread_dispatch_credentials(config_override, *, user_id,
  project_id, user_settings)` — the persistent-session sibling of
  `_inject_dispatch_credentials`. Idempotent + re-injection-safe (drops None
  transport sentinels, then setdefault-injects), so it repopulates a *stripped*
  copy.
- **Create** (`create_thread`) and **hot-swap** (`agent_update_thread_config`)
  persist `redact_config_override(...)` while the live attach / return keep the
  enriched in-memory dict.
- **Resume** re-injection at two sites: the agent-only workspace endpoint
  `agent_get_thread_workspace` (the agent's real key source on resume, via the
  `persistent_app.py` attach fallback) and the `resume_thread` idle-pool attach
  (needed because datasource sessions make the attach payload truthy and
  suppress the agent fallback).
- **Backfill** `PostgresDB.backfill_strip_thread_config_secrets()` strips legacy
  plaintext from existing rows (idempotent), wired into the FastAPI lifespan next
  to `backfill_encrypt_datasource_credentials` (not `init.py` — not reliably
  invoked at deploy time).

## Files

- `orchestrator/security/access.py` — `redact_config_override`.
- `orchestrator/main.py` — import; 5 GET endpoints; `_inject_thread_dispatch_credentials`;
  create + hot-swap persist (stripped); workspace-endpoint + resume re-inject;
  lifespan backfill wiring.
- `orchestrator/database/postgres.py` — `backfill_strip_thread_config_secrets`.
- `tests/test_config_override_redaction.py`, `tests/test_thread_config_persistence.py`.

## Key implementation notes / gotchas

- `threads` has **no** `config_override` column — it lives only inside `metadata`
  (asyncpg returns it as a JSON string). At resume `thread.get("config_override")`
  is `None`; the agent recovers config from the workspace endpoint. That endpoint
  is therefore the load-bearing re-injection site.
- The hot-swap sets explicit `provider/base_url/api_key = None` sentinels so the
  *live* agent's deep-merge clears the previous model's transport. The injector
  drops None-valued transport keys before re-injecting so those sentinels don't
  block `setdefault` on resume.
- The embedding block's original `"EMBEDDING_MODEL" not in env_keys_block` guard
  would skip re-injection (EMBEDDING_MODEL survives stripping); the extracted
  helper calls the setdefault-based env-key injector unconditionally instead.

## Deferred / out of scope

- Log redaction of `sk-…` patterns in the log-stream path (`docs/cloud_workspace.md:163`).
- The agent pod still receives the plaintext key (it must, to call the LLM
  directly). Truly hiding keys from the session owner would require proxying LLM
  traffic through the orchestrator — not justified for this deployment.
- Approach B (encrypt-in-place) considered and rejected.

## Verification

- Unit: `pytest tests/test_config_override_redaction.py tests/test_thread_config_persistence.py`
  (+ `test_datasource_access.py`, `test_dispatch_phase_credentials.py` regression). All green.
- E2E on **k3d-srw** (2026-06-14; vector DB `srw-pgvector` left untouched — no restart):
  - Lifespan backfill stripped all 20 existing threads → **0** plaintext keys at rest
    (`model`/`base_url`/`provider`/`*_MODEL` preserved).
  - Internal workspace endpoint **re-injected** `llm.api_key` (len 46), `auxiliary.api_key`,
    and `EMBEDDING_API_KEY` on a stripped thread.
  - **Resume keystone:** a resumed (stripped-at-rest) session applied the re-injected key
    (`sk-mo-…`, `base_url=ai.h4ll.app`), made a successful `POST /v1/embeddings → 200 OK`,
    and returned a live chat reply — **no 401** anywhere in the agent logs.
  - **Create:** a freshly-created session persisted stripped (no `api_key`/`*_API_KEY` at rest).
  - **GET redaction:** user-token `GET /api/persistent/threads[/{id}]` returned `config_override`
    with no secrets, while the internal endpoint returned them — the intended split.
  - One unrelated cockpit↔agent WebSocket glitch (known k3d service-worker issue) cleared on reload.
