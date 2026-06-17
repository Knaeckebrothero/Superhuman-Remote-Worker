# Orchestrator-Resolved Config — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or
> subagent-driven-development) to implement this task-by-task. Steps use checkbox
> (`- [ ]`) syntax. **One session, one branch (`develop`)** — parallel sessions on
> this feature clobbered each other once already.

**Goal:** Move all agent config resolution into the orchestrator. The orchestrator
emits one fully-resolved, frozen config blob; the agent hydrates it and stops
reading `experts`/`config_overrides` from the DB.

**Architecture:** One shared `resolve_config()` in the orchestrator composes the
existing `src/core/loader` steps (`load_and_merge_config` → inject DB layers →
`_apply_settings_matrix` → `serialize_resolved_config`). Jobs resolve at dispatch
(frozen into `jobs.resolved_config`); sessions resolve on delivery (cold:
`get_thread_workspace`; warm: `/session/attach`). The agent branches: blob present
→ `load_agent_config_from_dict`; absent → `from_config(config_name)` (today's path
= the migration fallback). Gated by `EXPERTS_DB_ENABLED`.

**Tech Stack:** Python (orchestrator FastAPI + agent), `src/core/loader`, asyncpg,
Angular cockpit, k3d/tilt for verification.

**Spec (contract of record):** `docs/superpowers/specs/2026-06-17-orchestrator-resolved-config-design.md` — supersedes `global_expert_management.md` Decision 6.

**Open items resolved (from the spec):**
1. Session blob storage → **none**; resolve-on-delivery (jobs keep `jobs.resolved_config`).
2. Resolver home → `orchestrator/services/config_resolver.py`.
3. Compose loader steps (inject DB layers mid-pipeline), not `load_agent_config` whole.
4. Flag → reuse `EXPERTS_DB_ENABLED`; blob-presence is the agent-side switch.
5. Blob channel → new `resolved_config` field on JobStartRequest, `get_thread_workspace` return, and the `/session/attach` payload.

---

## File structure

**Create:**
- `orchestrator/services/config_resolver.py` — the shared resolver.
- `tests/test_config_resolver.py` — resolver unit tests.
- `tests/test_resolved_config_hydrate.py` — agent hydrate/fallback tests.

**Modify (orchestrator):** `orchestrator/main.py` (dispatch, `get_thread_workspace`,
`_send_session_attach`, restore `list_experts` DB-merge + `_load_expert_detail` +
`_is_experts_db_enabled` + `_is_uuid`, create_thread/create_job expert_id), `orchestrator/database/postgres.py` (already has the methods — no change expected).

**Modify (agent):** `src/agent.py` (`from_resolved` classmethod), `src/api/app.py`,
`src/api/dual_app.py`, `src/api/persistent_app.py` (hydrate branch at the 3 entry
points + `_attach_session`), `src/api/models.py` (`JobStartRequest.resolved_config`).

**Modify (cockpit):** `views/create/job-create.component.ts`,
`views/session-create/session-create.component.ts`, `core/models/api.model.ts`.

**Modify (docs):** `docs/features/global_expert_management.md` (supersede Decision 6).

---

## Phase 1 — The shared resolver

### Task 1: `resolve_config()` skeleton + base resolution

**Files:** Create `orchestrator/services/config_resolver.py`; Test `tests/test_config_resolver.py`

- [ ] **Step 1: Write the failing test** — base config resolves to a serialized blob.

```python
# tests/test_config_resolver.py
import pytest
from orchestrator.services.config_resolver import resolve_config

@pytest.mark.asyncio
async def test_base_only_resolves_to_blob(monkeypatch):
    # No expert, no overrides → just the bundled base, serialized.
    blob = await resolve_config(
        base_config_name="persistent_defaults",
        expert_row=None, project_overrides=None, db_overrides=None,
        user_settings=None, request_override=None, expert_type="session",
    )
    assert "agent" in blob and "prompts" in blob and "instructions" in blob
    assert blob["agent"]["agent_id"]            # base loaded
    assert "api_key" not in blob["agent"].get("llm", {})  # nothing injected yet
```

- [ ] **Step 2: Run it, expect ImportError/fail.** `pytest tests/test_config_resolver.py -x`

- [ ] **Step 3: Implement base resolution** (compose loader steps; injection layers are stubs for now).

```python
# orchestrator/services/config_resolver.py
"""Single source of agent config resolution (supersedes agent-side Decision 6).

Composes src/core/loader steps so the orchestrator produces the full, frozen
config blob the agent hydrates. Reused by job dispatch AND session attach —
identical resolution; only timing/delivery differ.
"""
from __future__ import annotations

from typing import Any, Optional

from src.core.loader import (
    _apply_settings_matrix,
    deep_merge,
    load_agent_config_from_dict,
    load_and_merge_config,
    resolve_config_path,
    serialize_resolved_config,
)


async def resolve_config(
    *,
    base_config_name: str,
    expert_row: Optional[dict] = None,
    project_overrides: Optional[dict] = None,
    db_overrides: Optional[dict] = None,
    user_settings: Optional[dict] = None,
    request_override: Optional[dict] = None,
    expert_type: str = "session",
) -> dict:
    """Resolve the full agent config to a serialize_resolved_config-shaped blob.

    Layer order (global_expert_management.md:246-260):
      bundled base -> expert fragment -> project_experts.config_override
      -> DB config_overrides (0022) -> user persistent_agent settings
      -> request config_override (most-specific wins).
    Credentials are injected by the caller AFTER this returns (Task 6) so the
    persisted copy can be the pre-injection blob.
    """
    base_path, deployment_dir = resolve_config_path(base_config_name)
    data = load_and_merge_config(base_path)  # bundled base + $extends

    # (expert + override layers injected in Task 2/3 — stubs here)
    for layer in (project_overrides, db_overrides, user_settings, request_override):
        if layer:
            data = deep_merge(data, layer)

    _apply_settings_matrix(data, _explicit_llm_keys(data, request_override), deployment_dir)
    config = load_agent_config_from_dict(data, deployment_dir=deployment_dir)
    return serialize_resolved_config(config, model=config.llm.model)


def _explicit_llm_keys(data: dict, request_override: Optional[dict]) -> set:
    """LLM keys set explicitly (so the settings-matrix won't overwrite them)."""
    keys = set((data.get("llm") or {}).keys())
    if request_override and request_override.get("llm"):
        keys |= set(request_override["llm"].keys())
    return keys
```

- [ ] **Step 4: Run test, expect PASS.** `pytest tests/test_config_resolver.py -x`
- [ ] **Step 5: Commit.** `git add … && git commit -m "feat(resolver): orchestrator-side base config resolution"`

### Task 2: Expert fragment layer (the base layer, fenced persona)

**Files:** `orchestrator/services/config_resolver.py`; `tests/test_config_resolver.py`

- [ ] **Step 1: Failing test** — an expert row layers onto the type base and sets the fence marker.

```python
@pytest.mark.asyncio
async def test_expert_fragment_is_base_layer_and_fenced():
    expert_row = {
        "expert_type": "session", "name": "sess-helper",
        "config": {"llm": {"model": "gemma-4-moe"}},
        "prompts": {"persona": "SENTINEL-PERSONA", "instructions": "do X"},
    }
    blob = await resolve_config(
        base_config_name="persistent_defaults", expert_row=expert_row,
        request_override={"llm": {"model": "user-pick"}},  # user wins over expert
        expert_type="session",
    )
    assert blob["agent"]["llm"]["model"] == "user-pick"          # request > expert
    assert blob["agent"]["extra"]["_persona_source"] == "db"      # fenced
    assert blob["prompts"]["persona"] == "SENTINEL-PERSONA"       # persona delivered
```

- [ ] **Step 2: Run, expect fail.**
- [ ] **Step 3: Implement** — reuse `build_expert_config`, inject as the layer *below* the overrides, set the fence marker.

```python
# in resolve_config(), replace the base block:
    base_path, deployment_dir = resolve_config_path(base_config_name)
    data = load_and_merge_config(base_path)

    prompts_override = {}
    if expert_row is not None:
        from src.core.expert_resolution import build_expert_config
        data, prompts_override = build_expert_config(data, expert_row)  # fragment onto base
        data.setdefault("extra", {})["_persona_source"] = "db"  # decision 7: fence user persona

    for layer in (project_overrides, db_overrides, user_settings, request_override):
        if layer:
            data = deep_merge(data, layer)

    _apply_settings_matrix(data, _explicit_llm_keys(data, request_override), deployment_dir)
    config = load_agent_config_from_dict(data, deployment_dir=deployment_dir)
    # carry the DB persona/instructions into the resolved prompts (fenced at render)
    if prompts_override:
        rp = config.extra.setdefault("_resolved_prompts", {})
        for k in ("persona", "instructions"):
            if prompts_override.get(k):
                rp[k] = prompts_override[k]
    return serialize_resolved_config(config, model=config.llm.model)
```

- [ ] **Step 4: Run, expect PASS.** Also add a test: bundled expert (no row, persona from file) does NOT get `_persona_source=db`.
- [ ] **Step 5: Commit.** `feat(resolver): expert fragment as fenced base layer`

### Task 3: Credential injection + stripping contract

**Files:** `orchestrator/services/config_resolver.py`; `tests/test_config_resolver.py`

- [ ] **Step 1: Failing test** — `resolve_config(..., inject_credentials=cb)` puts creds in the blob; `strip_secrets(blob)` removes them.

```python
@pytest.mark.asyncio
async def test_credentials_injected_then_strippable():
    async def fake_inject(co, **kw):  # mirrors _inject_thread_dispatch_credentials
        co.setdefault("llm", {})["api_key"] = "sk-secret"; return co
    blob = await resolve_config(base_config_name="persistent_defaults",
                                inject_credentials=fake_inject, expert_type="session")
    assert blob["agent"]["llm"]["api_key"] == "sk-secret"
    from orchestrator.services.config_resolver import strip_secrets
    persisted = strip_secrets(blob)
    assert "api_key" not in persisted["agent"]["llm"]
```

- [ ] **Step 2-4:** Add an `inject_credentials` callback param (applied to the merged `data` before `load_agent_config_from_dict`) and a `strip_secrets(blob)` helper (mirrors `redact_config_override`'s key set: `llm.api_key`, `env_keys` secrets, `connections`, `workspace.remote`). Run tests green.
- [ ] **Step 5: Commit.** `feat(resolver): credential injection + strip-for-persist`

---

## Phase 2 — Agent hydration + fallback

### Task 4: `UniversalAgent.from_resolved()` + hydrate helper

**Files:** `src/agent.py`; Test `tests/test_resolved_config_hydrate.py`

- [ ] **Step 1: Failing test** — hydrate a blob round-trips through the agent.

```python
# tests/test_resolved_config_hydrate.py
from src.agent import UniversalAgent
from src.core.loader import serialize_resolved_config, load_agent_config, resolve_config_path

def test_from_resolved_round_trips():
    p, d = resolve_config_path("persistent_defaults")
    blob = serialize_resolved_config(load_agent_config(p, d), model="m")
    agent = UniversalAgent.from_resolved(blob)
    assert agent.config.agent_id == blob["agent"]["agent_id"]
    # prompts from the blob land in _resolved_prompts
    assert "_resolved_prompts" in agent.config.extra
```

- [ ] **Step 2: Run, fail.**
- [ ] **Step 3: Implement** `from_resolved` (mirrors `from_config` at `src/agent.py:216`, but hydrates from the dict + seeds prompts):

```python
@classmethod
def from_resolved(cls, blob: dict, postgres_conn: Optional[Any] = None) -> "UniversalAgent":
    """Hydrate from an orchestrator-resolved blob (serialize_resolved_config shape).
    No file/DB resolution — the blob is already fully merged + frozen."""
    from .core.loader import load_agent_config_from_dict
    config = load_agent_config_from_dict(blob["agent"], deployment_dir=None)
    rp = config.extra.setdefault("_resolved_prompts", {})
    for k, v in (blob.get("prompts") or {}).items():
        if v:
            rp.setdefault(k, v)
    for k, v in (blob.get("instructions") or {}).items():
        if v:
            rp.setdefault(f"instructions::{k}", v)
    return cls(config, postgres_conn)
```

- [ ] **Step 4: Run, PASS.**
- [ ] **Step 5: Commit.** `feat(agent): from_resolved() hydration constructor`

### Task 5: Hydrate branch at the 3 entrypoints (with fallback)

**Files:** `src/api/models.py`, `src/api/app.py`, `src/api/dual_app.py`, `src/api/persistent_app.py`

- [ ] **Step 1:** Add the field. `src/api/models.py` `JobStartRequest`:
```python
    resolved_config: Optional[Dict[str, Any]] = Field(
        default=None, description="Orchestrator-resolved config blob (preferred over config_name)")
```

- [ ] **Step 2:** Worker (`src/api/app.py` lifespan ~83-104) and dual (`dual_app.py` ~192-209) — add the branch BEFORE `from_config`:
```python
    _resolved = _get_boot_resolved_config()  # env AGENT_RESOLVED_CONFIG (JSON) or None
    if _resolved:
        _agent = UniversalAgent.from_resolved(_resolved)
    else:
        _agent = UniversalAgent.from_config(config_path)   # fallback (today's path)
    await _agent.initialize()
```
(For jobs the per-job blob arrives in `JobStartRequest.resolved_config` — see Task 7 — and is applied in `process_job`; the lifespan boot blob covers the pool/dedicated boot.)

- [ ] **Step 3:** Session (`persistent_app.py` lifespan ~770) — same branch for the boot config; `_attach_session` gets the per-thread blob (Task 8).

- [ ] **Step 4:** Test (`tests/test_resolved_config_hydrate.py`): with `AGENT_RESOLVED_CONFIG` set, the entrypoint uses `from_resolved`; unset → `from_config`. (Test the selector helper `_get_boot_resolved_config` + the branch in isolation.)
- [ ] **Step 5: Commit.** `feat(agent): prefer resolved_config blob at boot, fall back to config_name`

---

## Phase 3 — Wire job dispatch

### Task 6: Orchestrator resolves + freezes at dispatch

**Files:** `orchestrator/main.py` (~1436 dispatch); Test: extend resolver tests with a dispatch-path integration shim.

- [ ] **Step 1:** At dispatch (`main.py:1436-1452`), before building `JobStartRequest`, call the resolver and freeze:
```python
    resolved = await resolve_config(
        base_config_name=job.get("config_name", "defaults"),
        expert_row=(await postgres_db.get_expert_by_id(job["expert_id"])
                    if job.get("expert_id") and _is_experts_db_enabled() else None),
        request_override=config_override,
        user_settings=...,            # existing source
        inject_credentials=lambda co, **k: _inject_job_dispatch_credentials(co, ...),
        expert_type="worker",
    )
    await postgres_db.store_resolved_config(_uuid.UUID(job_id), strip_secrets(resolved))
    job_start = JobStartRequest(..., resolved_config=resolved, config_name=job.get("config_name","default"), ...)
```
- [ ] **Step 2:** Agent side — `app.py`/`dual_app.py` `_process_orchestrator_job`: when `request.resolved_config` present, hydrate it for the job (replace the agent-side resolve + the `store_resolved_config` freeze at `src/api/agent.py:1102` — orchestrator now owns the freeze). Keep `config_override` merge only for the no-blob fallback path.
- [ ] **Step 3:** Test: a job with `expert_id` set produces a `JobStartRequest.resolved_config` whose `agent.llm.model` reflects the expert, and `jobs.resolved_config` is the *stripped* blob.
- [ ] **Step 4:** k3d: dispatch a worker job with `expert_id` → agent boots from the blob, no DB read. Verify `list_llm_requests`/logs show the expert's model + fenced persona.
- [ ] **Step 5: Commit.** `feat(dispatch): orchestrator-resolved config for jobs`

---

## Phase 4 — Wire session attach (cold + warm)

### Task 7: Cold/dedicated attach — `get_thread_workspace` returns the blob

**Files:** `orchestrator/main.py` (~11909), `src/api/persistent_app.py` (`_attach_session` ~1028, merge ~1183)

- [ ] **Step 1:** `agent_get_thread_workspace` — replace the `config_override`-only return with a resolved blob: resolve from `thread.config_name` + `metadata.expert_id` + `metadata.config_override` + user settings, inject creds, return under a new `resolved_config` key (keep `config_override` for fallback during migration).
- [ ] **Step 2:** `_attach_session` — when `resolved_config` arrives (from `_poll_workspace_ready`/`get_thread_workspace`), hydrate it (`_agent` swaps to `from_resolved`, rebuild LLM/tools via the existing `_handle_config_update` machinery) instead of the `_load_expert_config(config_name)` + `deep_merge(config_override)` path (`persistent_app.py:1183-1226`). Keep that path as the fallback when no blob.
- [ ] **Step 3:** Test: cold attach with a thread carrying `metadata.expert_id` hydrates the expert (model + fenced persona) and does NOT call `_load_expert_config`.
- [ ] **Step 4:** k3d: open a fresh dedicated session with an expert → agent boots `persistent_defaults`, hydrates the blob at attach, persona fenced. (This is the path the 3-min-stall investigation exercised.)
- [ ] **Step 5: Commit.** `feat(session): orchestrator-resolved config on cold attach`

### Task 8: Warm-pool attach — blob in the `/session/attach` payload

**Files:** `orchestrator/main.py` (`_send_session_attach` ~1763), `src/api/persistent_app.py` (`/session/attach` ~1793, `_attach_session`)

- [ ] **Step 1:** `_send_session_attach` — resolve the thread's config and add `resolved_config` to the POST payload (alongside the existing `config_override`/`config_name`).
- [ ] **Step 2:** `/session/attach` route — pass `request.get("resolved_config")` into `_attach_session` (which already handles it from Task 7).
- [ ] **Step 3:** Test: the warm-pool payload carries `resolved_config`; the route forwards it.
- [ ] **Step 4:** k3d: attach a session to an idle **pool** agent with an expert → expert applies (this is the exact gap that caused the original crash/stall).
- [ ] **Step 5: Commit.** `feat(session): orchestrator-resolved config on warm-pool attach`

---

## Phase 5 — Restore expert visibility + cockpit `expert_id`

### Task 9: Restore the `list_experts` DB-merge + detail + flag helpers

**Files:** `orchestrator/main.py` (~15682)

- [ ] **Step 1:** Restore (from the recovered code in the spec/agent-map) `_is_experts_db_enabled`, `_is_uuid`, the DB-aware `list_experts` (bundled + `list_experts_visible`, with `source` tags + `type` filter), and the DB/uuid branch of `_load_expert_detail`. (Read-only surface only — NO create/update/delete; CRUD is the deferred fast-follow.)
- [ ] **Step 2:** Test (CI/manual): `GET /api/experts` with `EXPERTS_DB_ENABLED=true` returns the 3 DB rows tagged `source:user` alongside bundled.
- [ ] **Step 3:** k3d: the cockpit session/job picker shows the DB experts again.
- [ ] **Step 4: Commit.** `feat(experts): restore DB-merge in list_experts (read surface)`

### Task 10: Cockpit sends `expert_id`; propagate it

**Files:** `cockpit/.../job-create.component.ts:1356`, `cockpit/.../session-create.component.ts:~400`, `cockpit/.../api.model.ts:1025`, `orchestrator/main.py` (create_job + create_thread)

- [ ] **Step 1:** `api.model.ts` — add `expert_id?: string` to `JobCreateRequest`; session body already untyped.
- [ ] **Step 2:** job-create: replace `request.config_name = expert.id` with `request.expert_id = expert.id` (leave `config_name` to default). session-create: send `expert_id: expert?.id` and keep `config_name` as the base (`persistent_defaults`), not the expert id.
- [ ] **Step 3:** Orchestrator `create_job`/`create_thread`: persist `expert_id` (jobs column / `metadata.expert_id`) from the request. (The `config_name` UUID guards from `abe3a90b` stay as belt-and-suspenders.)
- [ ] **Step 4:** k3d: pick an expert in a NEW session/job → thread/job row carries `expert_id`, `config_name` is the base, dispatch/attach resolves it. End-to-end with no `config_name=<uuid>`.
- [ ] **Step 5: Commit.** `feat(cockpit): send expert_id (not config_name) for expert selection`

---

## Phase 6 — Flag, vault, verify, finish

### Task 11: Flag default + vault supersession

**Files:** `helm/values.yaml` (already `expertsDbEnabled: "false"`), `docs/features/global_expert_management.md`

- [ ] **Step 1:** Confirm `EXPERTS_DB_ENABLED` gates resolution (orchestrator: resolve only when on; agent: blob-presence drives hydrate). Dev on / prod off.
- [ ] **Step 2:** Edit `global_expert_management.md`: mark **Decision 6 superseded** by orchestrator-resolution; link this plan + the spec.
- [ ] **Step 3: Commit.** `docs(experts): supersede Decision 6 (orchestrator-resolved config)`

### Task 12: End-to-end verification on k3d

- [ ] **Step 1:** `EXPERTS_DB_ENABLED=true`. Session with expert (warm pool) → boots base, hydrates blob, fenced persona in the rendered prompt (grep the `llm_request` for the sentinel), no 3-min stall.
- [ ] **Step 2:** Worker job with expert → `jobs.resolved_config` is the stripped blob; agent ran the expert's model/persona.
- [ ] **Step 3:** Flag **off** regression → bundled experts unaffected; sessions/jobs boot via `from_config` fallback (no blob).
- [ ] **Step 4:** Note the now-dead agent-side resolver paths (`_load_expert_config` for experts; the agent-side freeze) for a deletion follow-up — do NOT delete in this plan (keeps the fallback intact).

---

## Risks / notes
- **Fallback is load-bearing during migration** — never remove the `from_config` branch until both paths are verified on dev; the blob's absence must always be safe.
- **Settings-matrix fidelity:** resolve_config must apply `_apply_settings_matrix` exactly as `load_agent_config` does (Task 1) — the earlier config_override shortcut's bug was skipping full resolution. The round-trip test (Task 4) guards this.
- **Credentials:** the *delivered* blob has secrets; the *persisted* copy (`jobs.resolved_config`, any thread store) must be `strip_secrets`'d (Task 3). Mirror `redact_config_override`'s key set exactly.
- **Live config changes (future)** ride the same `resolve_config` + the existing `_handle_config_update` rebuild — keep hydrate re-runnable at a turn boundary (don't special-case boot-only).
- **Decision-9 enforcement seam:** the post-merge point inside `resolve_config` (after all layers, before `serialize`) is the single place dispatch-time capability enforcement belongs. v1 leaves it a **pass-through** (no `capability_grants` yet) — but resolution and the future check are co-located by construction, which is half the reason for this whole change.
