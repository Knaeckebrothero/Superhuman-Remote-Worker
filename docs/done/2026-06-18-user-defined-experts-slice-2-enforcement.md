# User-Defined Experts — Slice 2 (Capability Grants: Enforcement + Admin API) Implementation Plan — v2

> ## ✅ STATUS: COMPLETE — executed inline on `develop` (uncommitted) + live-verified on k3d-srw, 2026-06-18
>
> All 15 tasks executed. **29 new tests** (21 pure PDP + 8 contract) + 268 area-regression green;
> ruff clean; `orchestrator.main` imports and all routes register. **Live-verified on k3d-srw:**
> migration `0030` + grandfather backfill; save-time 422 (multi-key); dispatch PEP reject with
> per-key resolution (granted `shell_tools` passes, ungranted `delegation` blocks, job → `failed`);
> admin grants CRUD + append-only audit (`set`/`revoke`/`set`); `/api/users/me/capabilities` (admin
> null + non-admin resolved); `delete_user` → grant-row cleanup. The per-task `- [ ]` checkboxes below
> are left **unchecked as the historical plan of record** — this banner is the as-built record.
>
> **Deviations from this plan, as executed (all intentional):**
> 1. **Wired `scan_fragment_text`** (raw-body duplicate/non-ASCII/credential-key scan) into all 3 save
>    endpoints via a combined `_enforce_expert_save` helper — Task 5 built it "to be wired in Task 8"
>    but Task 8's literal steps omitted the wiring.
> 2. **Added `_validate_grant_value`** on the admin set-grant endpoint — rejects a value whose type
>    mismatches the catalog (e.g. an out-of-range enum) so it can't crash `meet()` at dispatch.
> 3. **Fixed a bug in Task 11's pseudo-code:** `(config_name).replace("default","defaults")` corrupts
>    `"defaults"`→`"defaultss"`; used the dispatch path's `if name == "default"` guard instead.
> 4. **`resume_job` endpoint PEP placed before its broad `try`** so a 403 isn't swallowed; a resolve
>    *infra* error proceeds (the dispatch-time check already passed) rather than 500-ing every resume.
> 5. **Session attach fails CLOSED on a resolve _error_** (experts enabled): refuses rather than
>    delivering the unvetted `config_override`. Behavior change; the experts-*disabled* path keeps the
>    legacy fallback.
> 6. **Runtime import-path bug caught live + fixed:** the three lazy `from orchestrator.services.grants_service import …`
>    were wrong in the pod (its flattened layout uses sibling `from services…`); the test `sys.path`
>    (both roots on it) masked this. Fixed to `from services.grants_service import …`. **Lesson:** for
>    runtime imports in `orchestrator/`, use sibling `from services…`/`from security…`, never
>    `from orchestrator.…`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Commits are the user's responsibility.** This user handles all commits/pushes ("orchestration"). Every "Checkpoint" means *stop, confirm tests green, leave changes staged for the user* — do NOT run `git commit`/`git push`.
>
> **v2 (2026-06-18):** revised after a 5-agent adversarial review (codebase, DB, security, web best-practice, docs). Posture set to **deny-by-default least-privilege via grandfathering** (see Context). All review findings folded in — changelog at the end.

**Goal:** Generalize the single `can_use_vm` boolean into a scoped capability-grants system that is **deny-by-default** for security-relevant capabilities — enforced at expert save-time (422), at dispatch (job + session + resume, fail-closed), through one shared decision function — while grandfathering existing approved users so nothing breaks on upgrade.

**Architecture:** A code-side **catalog** + a `capability_grants` table (user→project→global→default, restrict-only) feed one pure **PDP** `evaluate(merged_config, grants)`. PEPs call it: save-time on the raw expert fragment; dispatch-time on the orchestrator-resolved **merged** config (job dispatch, session attach, **and job resume**). The base config ships shell/delegation enabled, so the migration **grandfathers** existing approved users with those grants; new principals are deny-by-default and an admin provisions them (no base re-architecture, no capability injection — `reject`, never silently strip, per spec decision 9). Admin grants CRUD + an append-only audit + `/api/users/me/capabilities` round it out. Cockpit grants panel + editor control-greying are a deferred fast-follow.

**Tech Stack:** Python 3.12 (CI gate), FastAPI, asyncpg/PostgreSQL 15+, pytest. Pure security logic in `src/core/` (no framework imports) for hermetic unit testing; async DB glue in `orchestrator/`.

---

## Context & resolved design questions

- **Spec:** `docs/done/global_expert_management.md` — decisions 8, 9, 19, 21–23; catalog at lines **274–282**; enforcement flow **341–360**; `capability_grants` DDL sketch **238–268**.
- **Posture — deny-by-default via grandfathering (the central v2 change).** The dispatch PEP checks the **full merged config** (`config_resolver.py` `data` after `_apply_settings_matrix`). The operator base (`config/defaults.yaml`) ships `tools.shell` + `tools.delegation` + `delegation.enabled: true` for **every** worker job; the session base (`persistent_defaults.yaml`) ships `tools.shell` (delegation empty). With deny-default grants, an un-grandfathered non-admin would have **every** job rejected — a self-DoS. **Fix:** migration `0030` grandfathers all existing approved users with `shell_tools` + `delegation` grants (the always-on base capabilities). New principals start deny-by-default; an admin grants them. This honors spec decision 9 ("reject, no silent stripping") and decision 19's "no-op on upgrade" (existing users unaffected) without trimming the base or injecting capabilities.
- **Verified base values (so we grandfather only what's needed):** `defaults.yaml` `autonomy: review` (= ceiling default → no autonomy self-DoS); `sql`/`mongodb`/`graph: []` (empty → datasource not triggered); `persistent_defaults.yaml` `permission_mode: supervised` (lowest → no permission self-DoS). So **only `shell_tools` + `delegation` need grandfathering**; autonomy/permission/model/datasource escalations are opt-in and correctly require explicit grants (not grandfathered — that is the point of deny-by-default).
- **Migration number:** `0029` is taken by `0029_add_mistral_provider.sql`; this slice uses **`0030`** and corrects every stale `0029_capability_grants` reference (Task 14).
- **Single dispatch checkpoint (verified):** in `_resolve_session_config` (`orchestrator/main.py:1013-1025`) the thread's stored `config_override` — into which `create_thread` bakes `users.settings.persistent_agent` — is passed as `request_override` to `resolve_config`, so the persistent_agent "hole" lands inside the single merged fragment. **But resume bypasses it** (Task 11) and must be closed separately.
- **The merged config:** inside `resolve_config` (`orchestrator/services/config_resolver.py`), the local `data` dict after `_apply_settings_matrix` (line 114) is the full merged config in **fragment shape** (`tools`, `llm`, `autonomy`, `workspace`, `delegation`, `interactive`, …) — the same shape as an expert's `body.config` at save-time. One PDP serves both PEPs. Exposed via a non-breaking `capture` out-param.
- **Fail-closed hazard:** `_dispatch_job_to_agent` (`main.py:1548-1590`), `_send_session_attach` (`main.py:1964-1980`), and the resume paths wrap resolution in `try/except` that falls back to the **unchecked `config_override`** on error. The dispatch PEP raises a dedicated `GrantDenied` that these blocks must NOT swallow.
- **Documented v1 stances** (from the security review, accepted not fixed):
  - **Principal = job owner** (`job["user_id"]`). For most jobs owner == runner. Delegation children inherit the parent owner's grants (spec already defers transitive delegation-chain checks). Admin-bypass reads the runner's DB `is_admin` (un-shadowed by "view-as"); save-time correctly uses the request principal (shadowed). Acceptable for v1; documented in Task 10.
  - **`browser` stays allow-by-default** (base ships `browser_direct`; spec defers browser to a later deny-review). Flagged for the user — flip to deny + grandfather if strict web-gating is wanted.
  - **`permission_mode` ceiling default = `supervised`** (deny-by-default; opt-in `auto_accept`/`autonomous` sessions require a grant).
  - **Fail-mode is per-capability** (OPA guidance): a grants-*read* failure falls back to the legacy column for `vm_workspace` (Task 13) and **fails closed** for the other security keys; a *resolution* failure fails closed (refuse dispatch).
- **Scope:** ENFORCEMENT + admin API only. **Deferred to the fast-follow:** the Cockpit Admin→Users grants panel and `/api/users/me/capabilities`-driven editor greying (the endpoint ships here so the UI can consume it later).

## File Structure

**New files:**
- `src/core/capability_grants.py` — pure catalog + PDP: `CATALOG`, `meet()`, `resolve_grants()`, `evaluate()`, `_truthy()`, `_fragment_models()`. No DB/framework imports.
- `orchestrator/services/grants_service.py` — async glue: `resolve_grants_for(postgres_db, user_id, project_ids)`.
- `orchestrator/database/migrations/app/0030_capability_grants.sql` — tables + `can_use_vm` migrate-in + **grandfather backfill**.
- `tests/test_capability_grants.py` — pure PDP + resolution + adversarial matrix.
- `tests/test_capability_grants_api.py` — helper/contract tests.

**Modified files:**
- `orchestrator/database/postgres.py` — grants store + audit + `user_can_use_vm()` dual-read; `delete_grants_for_scope` wired into `delete_user` + `delete_project`.
- `orchestrator/services/config_resolver.py` — `capture` out-param on `resolve_config`.
- `src/core/expert_resolution.py` — canonicalize-before-scan + **reject non-ASCII keys**.
- `orchestrator/main.py` — `GrantDenied`; save-time PEP; dispatch PEPs (job + session + resume, fail-closed); `user_experts` kill-switch; `can_use_vm` swap; `/api/admin/grants` CRUD + audit; `/api/users/me/capabilities`.
- `docs/done/global_expert_management.md`, `docs/db_migration.md`, `docs/superpowers/plans/2026-06-15-user-defined-experts-slice-1.md` — 0029→0030.

## Test commands
- Pure unit (authoritative for this slice): `python -m pytest tests/test_capability_grants.py -v`
- Contract: `python -m pytest tests/test_capability_grants_api.py -v`
- CI (Py3.12) is the gate; local (Py3.14, missing optional deps) may be noisy — expected, see `tests/test_expert_crud.py:5-7`.

---

### Task 1: Migration `0030_capability_grants` (tables + migrate-in + grandfather)

**Files:** Create `orchestrator/database/migrations/app/0030_capability_grants.sql`.
(No `schema.sql` edit — it is a **frozen, not-applied** reference snapshot per `docs/db_migration.md:23`; `experts` (0028) was never mirrored there either. Migrations are the only applied artifact.)

- [ ] **Step 1: Write the migration**, mirroring `0028_experts.sql` header/idempotency style:

```sql
-- migration:     0030_capability_grants.sql
-- description:   Capability grants (User-Defined Experts, Slice 2). Scoped twin of
--                config_overrides; generalizes users.can_use_vm into deny-by-default
--                per-principal entitlements (user>project>global>default, restrict-only)
--                gating which tools/models/autonomy a config may use. Append-only audit
--                (decision 23). Grandfathers existing approved users for the base-shipped
--                always-on capabilities (shell, delegation) so deny-by-default is a no-op
--                on upgrade (decision 19). 0029 was claimed by add_mistral_provider.
--                Design: docs/done/global_expert_management.md (Slice 2).
-- depends-on:    0001_initial.sql
-- expected:      < 1s. Two empty tables + INSERT..SELECT backfills over (small) users.
-- locks:         AccessExclusiveLock on the new tables only.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

CREATE TABLE IF NOT EXISTS capability_grants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_kind  TEXT NOT NULL CHECK (scope_kind IN ('user', 'project', 'global')),
    scope_id    UUID,                          -- NULL for global; no FK (polymorphic)
    key         TEXT NOT NULL,
    value_json  JSONB NOT NULL,
    granted_by  UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- One grant per (scope, key). NULLS NOT DISTINCT (PG15+, fleet is PG15/16) so the
-- single global row per key collides correctly despite scope_id being NULL.
CREATE UNIQUE INDEX IF NOT EXISTS uq_grants_scope_key
    ON capability_grants (scope_kind, scope_id, key) NULLS NOT DISTINCT;
CREATE INDEX IF NOT EXISTS idx_grants_scope ON capability_grants (scope_kind, scope_id);

CREATE TABLE IF NOT EXISTS capability_grant_audit (
    id          BIGSERIAL PRIMARY KEY,
    actor       UUID,
    scope_kind  TEXT NOT NULL,
    scope_id    UUID,
    key         TEXT NOT NULL,
    old_value   JSONB,
    new_value   JSONB,
    action      TEXT NOT NULL CHECK (action IN ('set', 'update', 'revoke')),
    reason      TEXT,
    at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_grant_audit_scope ON capability_grant_audit (scope_kind, scope_id);

-- Migrate the existing one-off VM grant (decision 8).
INSERT INTO capability_grants (scope_kind, scope_id, key, value_json, granted_by)
SELECT 'user', id, 'vm_workspace', 'true'::jsonb, NULL
FROM users WHERE can_use_vm = TRUE
ON CONFLICT (scope_kind, scope_id, key) DO NOTHING;

-- GRANDFATHER: the operator base ships shell + delegation enabled for every job, so
-- without this every existing non-admin user's jobs would be rejected under deny-by-
-- default. Grant both to all currently-approved users; NEW users stay deny-by-default.
INSERT INTO capability_grants (scope_kind, scope_id, key, value_json, granted_by)
SELECT 'user', id, 'shell_tools', 'true'::jsonb, NULL
FROM users WHERE is_approved = TRUE
ON CONFLICT (scope_kind, scope_id, key) DO NOTHING;
INSERT INTO capability_grants (scope_kind, scope_id, key, value_json, granted_by)
SELECT 'user', id, 'delegation', 'true'::jsonb, NULL
FROM users WHERE is_approved = TRUE
ON CONFLICT (scope_kind, scope_id, key) DO NOTHING;

COMMENT ON TABLE capability_grants IS
  'Scoped capability entitlements (Slice 2). user>project>global>default, restrict-only, deny-by-default. Deleting a user/project must delete its grant rows in app code — no cascade fires.';

COMMIT;
```

- [ ] **Step 2: Lint the SQL parses.** Run: `python -c "import pathlib; s=pathlib.Path('orchestrator/database/migrations/app/0030_capability_grants.sql').read_text(); assert s.count('BEGIN;')==1 and s.count('COMMIT;')==1 and 'NULLS NOT DISTINCT' in s and s.count('is_approved = TRUE')==2; print('ok')"`  Expected: `ok`
- [ ] **Step 3: Checkpoint.** (Live apply + grandfather verification in Task 15.)

---

### Task 2: Grant catalog + `meet` (pure)

**Files:** Create `src/core/capability_grants.py`; test `tests/test_capability_grants.py`.

- [ ] **Step 1: Write failing tests:**

```python
from src.core.capability_grants import CATALOG, meet


def test_catalog_keys_and_defaults():
    assert set(CATALOG) == {
        "vm_workspace", "shell_tools", "delegation", "datasource_tools",
        "browser", "model_selection", "autonomy_ceiling", "permission_mode",
    }
    assert all(spec["restrict_only"] for spec in CATALOG.values())
    assert CATALOG["vm_workspace"]["default"] is False
    assert CATALOG["shell_tools"]["default"] is False        # deny-by-default
    assert CATALOG["delegation"]["default"] is False
    assert CATALOG["browser"]["default"] is True             # spec-deferred allow
    assert CATALOG["datasource_tools"]["default"] is True
    assert CATALOG["model_selection"]["default"] is None
    assert CATALOG["autonomy_ceiling"]["default"] == "review"
    assert CATALOG["permission_mode"]["default"] == "supervised"


def test_meet_bool_enum_list():
    assert meet(CATALOG["vm_workspace"], True, False) is False
    assert meet(CATALOG["autonomy_ceiling"], "full", "review") == "review"
    assert meet(CATALOG["permission_mode"], "autonomous", "supervised") == "supervised"
    assert meet(CATALOG["model_selection"], None, ["a", "b"]) == ["a", "b"]
    assert meet(CATALOG["model_selection"], ["a", "b"], ["b", "c"]) == ["b"]
```

- [ ] **Step 2: Run to verify failure.** Run: `python -m pytest tests/test_capability_grants.py -v`  Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement:**

```python
# src/core/capability_grants.py
"""Pure capability-grants logic: catalog, resolution, policy decision. No DB/
framework imports — hermetically unit-testable (the security boundary). Async DB
glue is in orchestrator/services/grants_service.py.

Spec: docs/done/global_expert_management.md (decisions 8, 9, 19, 21-23).
Restrict-only (decision 22): a more-specific scope may only narrow an inherited
value. Deny-by-default for security keys; existing users grandfathered by the
0030 migration backfill (shell_tools, delegation)."""
from __future__ import annotations

from typing import Any

_AUTONOMY_ORDER = ["dependent", "guided", "partial", "review", "full"]
_PERMISSION_ORDER = ["supervised", "auto_accept", "autonomous"]

CATALOG: dict[str, dict[str, Any]] = {
    "vm_workspace":     {"type": "bool", "default": False, "restrict_only": True},
    "shell_tools":      {"type": "bool", "default": False, "restrict_only": True},
    "delegation":       {"type": "bool", "default": False, "restrict_only": True},
    "datasource_tools": {"type": "bool", "default": True,  "restrict_only": True},
    "browser":          {"type": "bool", "default": True,  "restrict_only": True},
    "model_selection":  {"type": "list", "default": None,  "restrict_only": True},
    "autonomy_ceiling": {"type": "enum", "default": "review",
                         "restrict_only": True, "order": _AUTONOMY_ORDER},
    "permission_mode":  {"type": "enum", "default": "supervised",
                         "restrict_only": True, "order": _PERMISSION_ORDER},
}


def meet(spec: dict, a: Any, b: Any) -> Any:
    """Greatest-lower-bound (the restrict-only combinator). bool->AND,
    enum->more restrictive by catalog order, list->intersection (None = ⊤)."""
    t = spec["type"]
    if t == "bool":
        return bool(a) and bool(b)
    if t == "enum":
        order = spec["order"]
        return a if order.index(a) <= order.index(b) else b
    if t == "list":
        if a is None:
            return b
        if b is None:
            return a
        bset = set(b)
        return [x for x in a if x in bset]
    raise ValueError(f"unknown catalog type {t!r}")
```

- [ ] **Step 4: Run to verify pass.** Run: `python -m pytest tests/test_capability_grants.py -v`  Expected: PASS.
- [ ] **Step 5: Checkpoint.**

---

### Task 3: Grant resolution `resolve_grants` (pure)

**Files:** Modify `src/core/capability_grants.py`; test same file.

- [ ] **Step 1: Write failing tests** (escalation-safety is mandatory):

```python
from src.core.capability_grants import resolve_grants


def _rows(**kv):
    return [{"key": k, "value_json": v} for k, v in kv.items()]


def test_lone_user_grant_widens_past_default():
    g = resolve_grants(user_rows=_rows(shell_tools=True), project_rows=[], global_rows=[])
    assert g["shell_tools"] is True
    assert g["vm_workspace"] is False and g["model_selection"] is None
    assert g["autonomy_ceiling"] == "review" and g["permission_mode"] == "supervised"


def test_project_cap_clamps_user_grant():
    g = resolve_grants(user_rows=_rows(shell_tools=True),
                       project_rows=_rows(shell_tools=False), global_rows=[])
    assert g["shell_tools"] is False


def test_autonomy_and_permission_clamped_by_global():
    g = resolve_grants(user_rows=_rows(autonomy_ceiling="full", permission_mode="autonomous"),
                       project_rows=[],
                       global_rows=_rows(autonomy_ceiling="review", permission_mode="auto_accept"))
    assert g["autonomy_ceiling"] == "review" and g["permission_mode"] == "auto_accept"


def test_model_selection_intersects():
    g = resolve_grants(user_rows=_rows(model_selection=["a", "b"]),
                       project_rows=_rows(model_selection=["b", "c"]), global_rows=[])
    assert g["model_selection"] == ["b"]
```

- [ ] **Step 2: Run to verify failure.** Run: `python -m pytest tests/test_capability_grants.py -k "resolve or clamp or widen or intersect" -v`  Expected: FAIL.

- [ ] **Step 3: Implement:**

```python
def _scope_value(rows: list[dict], key: str, spec: dict) -> Any:
    """The value one scope asserts for key, meeting duplicates (multi-project ->
    most restrictive). None => scope does not set the key."""
    vals = [r["value_json"] for r in rows if r.get("key") == key]
    if not vals:
        return None
    acc = vals[0]
    for v in vals[1:]:
        acc = meet(spec, acc, v)
    return acc


def resolve_grants(*, user_rows: list[dict], project_rows: list[dict],
                   global_rows: list[dict]) -> dict[str, Any]:
    """Resolve every catalog key for one principal. granted = most-specific scope
    that sets it (user>project>global) else catalog default; restrict-only keys
    are clamped to the meet of every scope that set the key (decision 22 — a child
    can never widen past a parent cap)."""
    out: dict[str, Any] = {}
    for key, spec in CATALOG.items():
        u = _scope_value(user_rows, key, spec)
        p = _scope_value(project_rows, key, spec)
        gl = _scope_value(global_rows, key, spec)
        set_pairs = [(s, v) for s, v in ((2, u), (1, p), (0, gl)) if v is not None]
        if not set_pairs:
            out[key] = spec["default"]
            continue
        granted = max(set_pairs, key=lambda t: t[0])[1]
        if spec["restrict_only"]:
            eff = granted
            for _s, v in set_pairs:
                eff = meet(spec, eff, v)
            out[key] = eff
        else:
            out[key] = granted
    return out
```

- [ ] **Step 4: Run to verify pass.** Expected: PASS.
- [ ] **Step 5: Checkpoint.**

---

### Task 4: The PDP `evaluate()` (pure) — incl. `permission_mode` + delegation fix

**Files:** Modify `src/core/capability_grants.py`; test same file.

- [ ] **Step 1: Write failing tests:**

```python
from src.core.capability_grants import evaluate, CATALOG
DEFAULTS = {k: v["default"] for k, v in CATALOG.items()}


def test_allows_within_grants():
    assert evaluate({"tools": {"shell": ["ls"]}, "autonomy": "review"},
                    {**DEFAULTS, "shell_tools": True}) == []


def test_flags_ungranted_shell_and_vm_and_autonomy():
    v = evaluate({"workspace": {"backend": "vm"}, "tools": {"shell": ["ls"]}, "autonomy": "full"},
                 DEFAULTS)
    j = " ".join(v)
    assert "shell_tools" in j and "vm_workspace" in j and "autonomy_ceiling" in j


def test_delegation_reads_enabled_not_dict_presence():
    # A disabled delegation settings-dict must NOT trip the gate.
    assert evaluate({"delegation": {"enabled": False, "max_depth": 3}}, DEFAULTS) == []
    assert evaluate({"delegation": {"enabled": True}}, DEFAULTS)  # flagged (deny default)


def test_session_permission_mode_gated():
    # sessions use interactive.permission_mode, NOT autonomy.
    v = evaluate({"interactive": {"permission_mode": "autonomous"}}, DEFAULTS)
    assert len(v) == 1 and "permission_mode" in v[0]
    assert evaluate({"interactive": {"permission_mode": "supervised"}}, DEFAULTS) == []


def test_model_not_in_selection():
    v = evaluate({"llm": {"strategic": {"model": "x"}, "tactical": {"model": "y"}}},
                 {**DEFAULTS, "model_selection": ["y"]})
    assert len(v) == 1 and "x" in v[0]


def test_admin_short_circuits_and_empty_is_clean():
    assert evaluate({"tools": {"shell": ["ls"]}}, DEFAULTS, is_admin=True) == []
    assert evaluate({}, DEFAULTS) == []
```

- [ ] **Step 2: Run to verify failure.** Expected: FAIL (`ImportError: evaluate`).

- [ ] **Step 3: Implement:**

```python
def _truthy(x: Any) -> bool:
    return x not in (None, False, 0, "", [], {})


def _fragment_models(fragment: dict) -> list[str]:
    llm = fragment.get("llm") or {}
    out = []
    for v in (llm.get("model"), (llm.get("strategic") or {}).get("model"),
              (llm.get("tactical") or {}).get("model")):
        if isinstance(v, str) and v:
            out.append(v)
    return out


def _enum_exceeds(value: Any, ceiling: str, order: list[str]) -> bool:
    return isinstance(value, str) and value in order and order.index(value) > order.index(ceiling)


def evaluate(fragment: dict, grants: dict, *, is_admin: bool = False) -> list[str]:
    """Violation messages for a config vs a resolved grant set ([] = allowed). The
    single PDP: fed the raw expert fragment at save-time and the full merged config
    at dispatch. Admins short-circuit. Absent gated keys never violate."""
    if is_admin:
        return []
    v: list[str] = []
    tools = fragment.get("tools") or {}
    ws = fragment.get("workspace") or {}
    deleg = fragment.get("delegation") or {}
    inter = fragment.get("interactive") or {}

    if not grants.get("vm_workspace", False) and ws.get("backend") == "vm":
        v.append("vm_workspace: workspace.backend='vm' requires the vm_workspace grant")
    if not grants.get("shell_tools", False) and _truthy(tools.get("shell")):
        v.append("shell_tools: tools.shell requires the shell_tools grant")
    # delegation gates on the .enabled flag (a settings dict) OR a non-empty tool list,
    # NOT mere presence of the settings dict.
    if not grants.get("delegation", False) and (
        deleg.get("enabled") is True or _truthy(tools.get("delegation"))
    ):
        v.append("delegation: delegation requires the delegation grant")
    if not grants.get("datasource_tools", True) and any(
        _truthy(tools.get(k)) for k in ("sql", "mongodb", "graph")
    ):
        v.append("datasource_tools: datasource tools are not permitted")
    if not grants.get("browser", True) and _truthy(tools.get("browser_direct")):
        v.append("browser: tools.browser_direct is not permitted")

    allowed = grants.get("model_selection")  # None = all
    if allowed is not None:
        for m in _fragment_models(fragment):
            if m not in allowed:
                v.append(f"model_selection: model '{m}' is not in the permitted set")

    if _enum_exceeds(fragment.get("autonomy"), grants.get("autonomy_ceiling", "review"),
                     _AUTONOMY_ORDER):
        v.append(f"autonomy_ceiling: autonomy '{fragment.get('autonomy')}' exceeds the ceiling")
    if _enum_exceeds(inter.get("permission_mode"), grants.get("permission_mode", "supervised"),
                     _PERMISSION_ORDER):
        v.append(f"permission_mode: '{inter.get('permission_mode')}' exceeds the ceiling")
    return v
```

- [ ] **Step 4: Run to verify pass.** Expected: PASS.
- [ ] **Step 5: Checkpoint.**

---

### Task 5: Canonicalize-before-scan + reject non-ASCII keys

**Files:** Modify `src/core/expert_resolution.py`; test `tests/test_capability_grants.py`.

Closes the duplicate-key / unicode-alias bypass family. **Reject non-ASCII keys outright** (NFKC alone is insufficient — confusables are already NFC; cf. the OpenAI Codex unicode-bypass CVE).

- [ ] **Step 1: Write failing tests:**

```python
from src.core.expert_resolution import scan_fragment_text


def test_rejects_duplicate_keys():
    assert scan_fragment_text('{"llm": {"api_key": null, "api_key": "x"}}')


def test_rejects_non_ascii_key():
    # fullwidth 'api_key' — reject the non-ASCII key outright, don't try to normalize.
    assert scan_fragment_text('{"llm": {"ａｐｉ＿ｋｅｙ": "x"}}')


def test_allows_clean_fragment_text():
    assert scan_fragment_text('{"llm": {"model": "gemma-4-moe"}, "tools": {"shell": []}}') == []
```

- [ ] **Step 2: Run to verify failure.** Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement** (build on the existing `hard_deny_scan`/`canonical_key`):

```python
import re
_ASCII_KEY = re.compile(r"^[\x20-\x7E]*$")  # printable ASCII only


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict:
    """json object_pairs_hook: reject duplicate keys (after canonicalization, RFC
    7493) AND any non-ASCII key (reject, don't normalize — confusables defense)."""
    seen: set[str] = set()
    out: dict = {}
    for k, val in pairs:
        if not _ASCII_KEY.match(str(k)):
            raise ValueError(f"non-ASCII key not allowed: {k!r}")
        ck = canonical_key(str(k))
        if ck in seen:
            raise ValueError(f"duplicate key after canonicalization: {k}")
        seen.add(ck)
        out[k] = val
    return out


def scan_fragment_text(text: str) -> list[str]:
    """Parse raw fragment TEXT rejecting duplicate/non-ASCII keys, then hard-deny-
    scan. Returns offending paths ([] = clean); a parse rejection is a synthetic
    offence so the caller refuses the fragment."""
    try:
        parsed = json.loads(text, object_pairs_hook=_strict_object_pairs)
    except ValueError as e:
        return [f"<malformed>: {e}"]
    return hard_deny_scan(parsed)
```

- [ ] **Step 4: Run to verify pass.** Expected: PASS.
- [ ] **Step 5: Checkpoint.** (Wiring into the save endpoints is Task 8.)

---

### Task 6: Grants store + audit + dual-read + scope cleanup (asyncpg)

**Files:** Modify `orchestrator/database/postgres.py` (methods near the expert methods ~5400); wire `delete_grants_for_scope` into `delete_user` (6767-6787) + `delete_project` (7108-7128).

**CRITICAL — JSONB returns STRINGS here.** This codebase registers no global JSONB codec, so asyncpg returns `value_json` as a JSON **string**; every read must `json.loads`. Skipping this makes `bool("false") == True` → silent privilege escalation. (Confirmed: `get_system_setting` does this at postgres.py:8637-8641.)

- [ ] **Step 1: Add the store methods** (mirror `create_expert` 5248, `upsert_system_setting` 8644):

```python
    # --- Capability grants (Slice 2) ---

    async def list_grants_for_scopes(
        self, *, user_id: str | None, project_ids: list[str]
    ) -> dict[str, list[dict]]:
        """{'user': [...], 'project': [...], 'global': [...]} of {key, value_json}.
        JSONB is returned by asyncpg as a STRING here — deserialize on read."""
        rows = await self.fetch(
            """
            SELECT scope_kind, key, value_json FROM capability_grants
            WHERE (scope_kind = 'global')
               OR (scope_kind = 'user'    AND scope_id = $1)
               OR (scope_kind = 'project' AND scope_id = ANY($2::uuid[]))
            """,
            UUID(user_id) if user_id else None,
            [UUID(p) for p in project_ids],
        )
        out: dict[str, list[dict]] = {"user": [], "project": [], "global": []}
        for r in rows:
            raw = r["value_json"]
            val = json.loads(raw) if isinstance(raw, str) else raw
            out[r["scope_kind"]].append({"key": r["key"], "value_json": val})
        return out

    async def list_grants(self, *, scope_kind: str, scope_id: str | None) -> list[dict]:
        rows = await self.fetch(
            "SELECT key, value_json, granted_by, updated_at FROM capability_grants "
            "WHERE scope_kind = $1 AND scope_id IS NOT DISTINCT FROM $2 ORDER BY key",
            scope_kind, UUID(scope_id) if scope_id else None,
        )
        result = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("value_json"), str):
                d["value_json"] = json.loads(d["value_json"])
            result.append(d)
        return result

    async def set_grant(self, *, scope_kind: str, scope_id: str | None, key: str,
                        value_json: Any, actor: str | None, reason: str | None = None) -> dict:
        """Upsert one grant + audit row, one transaction. prev value_json is a JSON
        string from asyncpg — pass straight to the $::jsonb cast (don't re-dumps)."""
        async with self.acquire() as conn:
            async with conn.transaction():
                prev = await conn.fetchrow(
                    "SELECT value_json FROM capability_grants WHERE scope_kind=$1 "
                    "AND scope_id IS NOT DISTINCT FROM $2 AND key=$3",
                    scope_kind, UUID(scope_id) if scope_id else None, key,
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO capability_grants (scope_kind, scope_id, key, value_json, granted_by)
                    VALUES ($1, $2, $3, $4::jsonb, $5)
                    ON CONFLICT (scope_kind, scope_id, key) DO UPDATE
                        SET value_json = EXCLUDED.value_json, granted_by = EXCLUDED.granted_by,
                            updated_at = NOW()
                    RETURNING *
                    """,
                    scope_kind, UUID(scope_id) if scope_id else None, key,
                    json.dumps(value_json), UUID(actor) if actor else None,
                )
                await conn.execute(
                    "INSERT INTO capability_grant_audit "
                    "(actor, scope_kind, scope_id, key, old_value, new_value, action, reason) "
                    "VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8)",
                    UUID(actor) if actor else None, scope_kind,
                    UUID(scope_id) if scope_id else None, key,
                    prev["value_json"] if prev else None,    # already a JSON string
                    json.dumps(value_json), "update" if prev else "set", reason,
                )
                d = dict(row)
                if isinstance(d.get("value_json"), str):
                    d["value_json"] = json.loads(d["value_json"])
                return d

    async def delete_grant(self, *, scope_kind: str, scope_id: str | None, key: str,
                           actor: str | None, reason: str | None = None) -> bool:
        async with self.acquire() as conn:
            async with conn.transaction():
                prev = await conn.fetchrow(
                    "DELETE FROM capability_grants WHERE scope_kind=$1 "
                    "AND scope_id IS NOT DISTINCT FROM $2 AND key=$3 RETURNING value_json",
                    scope_kind, UUID(scope_id) if scope_id else None, key,
                )
                if prev is None:
                    return False
                await conn.execute(
                    "INSERT INTO capability_grant_audit "
                    "(actor, scope_kind, scope_id, key, old_value, new_value, action, reason) "
                    "VALUES ($1,$2,$3,$4,$5::jsonb,NULL,'revoke',$6)",
                    UUID(actor) if actor else None, scope_kind,
                    UUID(scope_id) if scope_id else None, key, prev["value_json"], reason,
                )
                return True

    async def delete_grants_for_scope(self, conn, *, scope_kind: str, scope_id: str) -> int:
        """Hard-delete a removed user/project's grant rows (no FK cascade fires —
        decision 23). Takes an existing connection so it runs in the caller's
        delete transaction (atomic with the principal removal)."""
        result = await conn.execute(
            "DELETE FROM capability_grants WHERE scope_kind=$1 AND scope_id=$2",
            scope_kind, UUID(scope_id),
        )
        return int(result.split()[-1]) if result else 0
```

- [ ] **Step 2: Wire scope cleanup into BOTH hard-delete paths** (both exist; do it inside the same transaction/connection as the principal DELETE). In `delete_user` (postgres.py:6767-6787), before/with the `DELETE FROM users`, call `await self.delete_grants_for_scope(conn, scope_kind="user", scope_id=user_id)`. In `delete_project` (7108-7128), `await self.delete_grants_for_scope(conn, scope_kind="project", scope_id=project_id)`. (Adapt to each method's existing `acquire()`/conn handling.)

- [ ] **Step 3: Checkpoint.** (Live in Task 15.)

---

### Task 7: `grants_service` async resolver

**Files:** Create `orchestrator/services/grants_service.py`.

- [ ] **Step 1: Implement:**

```python
# orchestrator/services/grants_service.py
"""Async resolution of a principal's effective capability grants. Pure logic in
src/core/capability_grants.py; this is the DB glue (mirrors config_resolver's
pure-core / async split)."""
from __future__ import annotations
from typing import Any
from src.core.capability_grants import resolve_grants


async def resolve_grants_for(postgres_db, *, user_id: str | None,
                             project_ids: list[str]) -> dict[str, Any]:
    scoped = await postgres_db.list_grants_for_scopes(
        user_id=user_id, project_ids=project_ids or [])
    return resolve_grants(user_rows=scoped["user"], project_rows=scoped["project"],
                          global_rows=scoped["global"])
```

- [ ] **Step 2: Checkpoint.**

---

### Task 8: Save-time PEP at the 3 expert endpoints

**Files:** Modify `orchestrator/main.py` — `_enforce_save_grants` near `_validate_expert_fragment` (~16216); call it in `create_expert` (after validate ~16322), `update_expert` (~16364), `import_expert` (~16443). Test `tests/test_capability_grants_api.py`.

**Signature fix:** `user_visible_project_ids(user: dict, db)` returns `set[UUID] | "all"` (NOT `(id_str)`). Handle the `"all"` sentinel.

- [ ] **Step 1: Write failing test:**

```python
def test_violations_detail_lists_keys():
    from orchestrator.main import _grant_violations_detail
    assert "shell_tools" in _grant_violations_detail(
        ["shell_tools: tools.shell requires the shell_tools grant"])
```

- [ ] **Step 2: Run to verify failure.** Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement:**

```python
def _grant_violations_detail(violations: list[str]) -> str:
    return "config exceeds your capability grants: " + "; ".join(violations)


async def _grant_project_ids(user: dict) -> list[str]:
    """Project scope ids for grant resolution. user_visible_project_ids returns
    'all' for admins (who bypass anyway) — treat as no project constraint."""
    vis = await user_visible_project_ids(user, postgres_db)  # security/access.py
    return [] if vis == "all" else [str(p) for p in vis]


async def _enforce_save_grants(config: dict[str, Any], *, user: dict[str, Any]) -> None:
    """Save-time PEP (decision 9): the author's grants must cover the raw fragment.
    422 naming offending keys. Admins bypass."""
    if user.get("is_admin"):
        return
    from src.core.capability_grants import evaluate
    from orchestrator.services.grants_service import resolve_grants_for
    grants = await resolve_grants_for(
        postgres_db, user_id=str(user["id"]), project_ids=await _grant_project_ids(user))
    violations = evaluate(config, grants)
    if violations:
        raise HTTPException(status_code=422, detail=_grant_violations_detail(violations))
```

In each endpoint, after the existing `_validate_expert_fragment(body.config)`:
```python
        await _enforce_save_grants(body.config, user=user)
```

- [ ] **Step 4: Run to verify pass.** Expected: PASS.
- [ ] **Step 5: Checkpoint.** (422-for-ungranted-author verified live in Task 15.)

---

### Task 9: `resolve_config` `capture` out-param

**Files:** Modify `orchestrator/services/config_resolver.py`; test `tests/test_capability_grants.py`.

- [ ] **Step 1: Write a failing test:**

```python
def test_resolve_config_capture_exposes_merged_fragment():
    from orchestrator.services.config_resolver import resolve_config
    cap: dict = {}
    resolve_config(base_config_name="defaults", capture=cap, expert_type="worker")
    assert "merged_fragment" in cap and isinstance(cap["merged_fragment"], dict)
    assert "tools" in cap["merged_fragment"]  # base tools present (the deny-by-default subject)
```

- [ ] **Step 2: Run to verify failure.** Expected: FAIL (`capture` unexpected kwarg).

- [ ] **Step 3: Implement** — add `capture: Optional[dict] = None` to the signature, and after `_apply_settings_matrix(data, explicit_llm_keys, deployment_dir)` (line 114):

```python
    if capture is not None:
        # Full merged config in fragment shape — the policy view for the dispatch
        # PEP (single PDP). The base's shell/delegation are present here; deny-by-
        # default is reconciled by grandfathering existing users (migration 0030).
        capture["merged_fragment"] = copy.deepcopy(data)
```

Default `None` keeps every caller + the keystone fidelity test byte-identical.

- [ ] **Step 4: Run to verify pass.** Expected: PASS.
- [ ] **Step 5: Checkpoint.**

---

### Task 10: Dispatch PEP — jobs (fail-closed)

**Files:** Modify `orchestrator/main.py` — `GrantDenied` + `_enforce_dispatch_grants` near `_check_vm_permission` (~2327); wire into the `_dispatch_job_to_agent` resolve block (1548-1590).

- [ ] **Step 1: Add the exception + helper:**

```python
class GrantDenied(Exception):
    """A merged config exceeds the runner's grants (dispatch PEP). Must NOT be
    swallowed by a resolve fallback (fail closed)."""
    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("; ".join(violations))


async def _enforce_dispatch_grants(merged: dict, *, runner_user_id: str | None,
                                   project_ids: list[str]) -> None:
    """Authoritative dispatch PEP (decision 9): the merged config must fit the
    RUNNER's grants. Raises GrantDenied on violation. Admin runner bypasses.
    NOTE (v1 stance): runner = job owner (job['user_id']); for delegation children
    inheriting a privileged owner this bypasses (spec defers transitive checks)."""
    user = await postgres_db.get_user(runner_user_id) if runner_user_id else None
    if user and user.get("is_admin"):
        return
    from src.core.capability_grants import evaluate
    from orchestrator.services.grants_service import resolve_grants_for
    grants = await resolve_grants_for(postgres_db, user_id=runner_user_id, project_ids=project_ids)
    violations = evaluate(merged, grants)
    if violations:
        raise GrantDenied(violations)
```

- [ ] **Step 2: Wire into `_dispatch_job_to_agent`** — capture the fragment, run the PEP, catch `GrantDenied` BEFORE the generic fallback (so it is never downgraded to the unchecked `config_override`):

```python
                _cap: dict = {}
                _resolved = resolve_config(
                    base_config_name=_base_name, base_defaults=_base_defaults,
                    expert_row=expert_row, request_override=config_override,
                    expert_type="worker", capture=_cap,
                )
                if await _user_experts_enabled():     # Task 11 kill-switch
                    await _enforce_dispatch_grants(
                        _cap["merged_fragment"],
                        runner_user_id=str(job["user_id"]) if job.get("user_id") else None,
                        project_ids=[str(job["project_id"])] if job.get("project_id") else [],
                    )
                resolved_config = await inject_blob_credentials(
                    _resolved, lambda co: _inject_dispatch_credentials(job, co))
                await postgres_db.store_resolved_config(
                    job_id, redact_config_override(resolved_config))
                # ... existing logger.info ...
            except GrantDenied as gd:
                logger.warning("Dispatch denied for job %s: %s", job_id, gd)
                await postgres_db.update_job_status(
                    job_id, status="failed", error_message=_grant_violations_detail(gd.violations))
                return False
            except Exception:
                logger.exception("Dispatch: resolve_config failed for job %s; fallback ...", job_id)
                resolved_config = None
```

- [ ] **Step 3: Verify parse.** Run: `python -c "import ast,pathlib; ast.parse(pathlib.Path('orchestrator/main.py').read_text()); print('parse ok')"`  Expected: `parse ok`
- [ ] **Step 4: Checkpoint.**

---

### Task 11: Job resume PEP (close the frozen-blob bypass) + `user_experts` kill-switch

**Files:** Modify `orchestrator/main.py` — resume paths `_resume_job_on_agent` (~1665) + `resume_job` endpoint (~6866); kill-switch endpoints near the `vm_workspaces` pair (~19171).

**Resume bypass (B3):** resume re-delivers the stored `config_override` / frozen `resolved_config` and never re-checks grants — a revoked grant would still resume. Re-run the PEP on resume.

- [ ] **Step 1: Add the kill-switch gate + endpoints** (mirror `vm_workspaces`, `_require_admin` at 18520):

```python
async def _user_experts_enabled() -> bool:
    """Runtime kill-switch (decision 8). Absent row = enabled (fail-open for fresh
    installs). When disabled, DB-expert creation + grant enforcement are off."""
    try:
        row = await postgres_db.get_system_setting("user_experts")
    except Exception:
        logger.exception("user_experts read failed; fail-open"); return True
    value = (row or {}).get("value") or {}
    return not (isinstance(value, dict) and value.get("enabled") is False)


@app.get("/api/admin/system-settings/user_experts")
async def get_user_experts_settings(request: Request) -> dict[str, Any]:
    await _require_admin(request)
    row = await postgres_db.get_system_setting("user_experts")
    value = (row or {}).get("value") or {}
    return {"enabled": not (isinstance(value, dict) and value.get("enabled") is False),
            "updated_by": (row or {}).get("updated_by")}


@app.put("/api/admin/system-settings/user_experts")
async def put_user_experts_settings(body: dict[str, Any], request: Request) -> dict[str, Any]:
    admin = await _require_admin(request)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="`enabled` must be a boolean")
    await postgres_db.upsert_system_setting("user_experts", {"enabled": enabled},
        updated_by=admin.get("email") or str(admin.get("id", "")))
    return {"enabled": enabled}
```

- [ ] **Step 2: Gate save-time** — in the 3 expert endpoints, before `_enforce_save_grants`, add: `if not await _user_experts_enabled(): raise HTTPException(403, detail="User-defined experts are disabled by the administrator")`.

- [ ] **Step 3: Enforce on resume.** In `_resume_job_on_agent` (~1665) and the `resume_job` endpoint (~6866), before POSTing `/job/resume`, re-resolve + check (the resume already has `job` + its `config_override`):

```python
        if await _user_experts_enabled():
            try:
                _cap: dict = {}
                resolve_config(
                    base_config_name=(job.get("config_name") or "defaults").replace("default", "defaults"),
                    base_defaults=await _resolve_default_models(job.get("user_id")),
                    expert_row=(await postgres_db.get_expert_by_id(str(job["expert_id"]))
                                if job.get("expert_id") else None),
                    request_override=job.get("config_override"),
                    expert_type="worker", capture=_cap)
                await _enforce_dispatch_grants(
                    _cap["merged_fragment"],
                    runner_user_id=str(job["user_id"]) if job.get("user_id") else None,
                    project_ids=[str(job["project_id"])] if job.get("project_id") else [])
            except GrantDenied as gd:
                logger.warning("Resume denied for job %s: %s", job.get("id"), gd)
                await postgres_db.update_job_status(str(job["id"]), status="failed",
                    error_message=_grant_violations_detail(gd.violations))
                return False   # (resume_job endpoint: raise HTTPException(403, _grant_violations_detail(gd.violations)))
```

- [ ] **Step 4: Verify parse.** Run: `python -c "import ast,pathlib; ast.parse(pathlib.Path('orchestrator/main.py').read_text()); print('parse ok')"`  Expected: `parse ok`
- [ ] **Step 5: Checkpoint.**

---

### Task 12: Dispatch PEP — sessions (fail-closed, warm + cold; incl. permission_mode)

**Files:** Modify `orchestrator/main.py` — `_resolve_session_config` (982-1037), `_send_session_attach` (1964-1980), `agent_get_thread_workspace` (~12188).

The session merged fragment carries `interactive.permission_mode` (Task 4 gates it) and any `persistent_agent` keys. Same `_enforce_dispatch_grants` helper.

- [ ] **Step 1: Add `capture` + status + the PEP to `_resolve_session_config`** (raise `GrantDenied` so it escapes the broad `except Exception`):

```python
async def _resolve_session_config(thread, metadata, *, config_override=None, status=None):
    if not _is_experts_db_enabled() or not await _user_experts_enabled():
        if status is not None: status["state"] = "disabled"
        return None
    try:
        # ... existing user_id/project_id/expert_row/base/request_override setup ...
        _cap: dict = {}
        resolved = resolve_config(base_config_name=base, base_defaults=base_defaults,
            expert_row=expert_row, request_override=request_override,
            expert_type="session", capture=_cap)
        await _enforce_dispatch_grants(_cap["merged_fragment"], runner_user_id=user_id,
            project_ids=[project_id] if project_id else [])
        delivered = await inject_blob_credentials(resolved,
            lambda co: _inject_thread_dispatch_credentials(co, user_id=user_id, project_id=project_id))
        if status is not None: status["state"] = "ok"
        return delivered
    except GrantDenied:
        if status is not None: status["state"] = "denied"
        raise                       # escape the generic except — never fall back
    except Exception:
        logger.exception("Session resolve failed for thread %s; ...", thread.get("id"))
        if status is not None: status["state"] = "error"
        return None
```

- [ ] **Step 2: Fail-closed in `_send_session_attach`** (warm, 1964-1980): wrap the `_resolve_session_config` call with a `status` dict; `except GrantDenied: return False`; and after, `if status.get("state") == "error": return False` (refuse rather than deliver the unvetted `config_override`).

- [ ] **Step 3: Same in the cold path `agent_get_thread_workspace`** (~12188): pass a `status` dict; `except GrantDenied` / `status=='error'` → `raise HTTPException(403, detail="capability grants deny this session config")`.

- [ ] **Step 4: Verify parse + checkpoint.** Run: `python -c "import ast,pathlib; ast.parse(pathlib.Path('orchestrator/main.py').read_text()); print('parse ok')"`

---

### Task 13: `can_use_vm` → grants dual-read

**Files:** `orchestrator/database/postgres.py` (`user_can_use_vm`), `orchestrator/main.py` (`_check_vm_permission` 2356-2362).

- [ ] **Step 1: Add the dual-read helper:**

```python
    async def user_can_use_vm(self, user: dict) -> bool:
        """Effective vm_workspace grant; fall back to the legacy can_use_vm column
        during rollout / on grant-read failure (per-capability fail-mode)."""
        try:
            scoped = await self.list_grants_for_scopes(user_id=str(user["id"]), project_ids=[])
            from src.core.capability_grants import resolve_grants
            g = resolve_grants(user_rows=scoped["user"], project_rows=scoped["project"],
                               global_rows=scoped["global"])
            if any(r["key"] == "vm_workspace"
                   for r in scoped["user"] + scoped["project"] + scoped["global"]):
                return bool(g["vm_workspace"])
        except Exception:
            logger.exception("vm grant read failed; fall back to can_use_vm column")
        return bool(user.get("can_use_vm"))
```

- [ ] **Step 2: Switch `_check_vm_permission`** per-user line (2358) to `if not user or not await postgres_db.user_can_use_vm(user):`. Kill-switch + admin-bypass above unchanged. The 9 SELECT sites that hydrate `user["can_use_vm"]` stay (they feed the fallback).
- [ ] **Step 3: Checkpoint.**

---

### Task 14: Admin grants API + `/api/users/me/capabilities` + doc renumber

**Files:** `orchestrator/main.py`; `docs/done/global_expert_management.md`; `docs/db_migration.md`; `docs/superpowers/plans/2026-06-15-user-defined-experts-slice-1.md`.

- [ ] **Step 1: Add endpoints** (model on `admin_patch_user`/`_require_admin`):

```python
class GrantSet(BaseModel):
    value_json: Any
    reason: str | None = None


@app.get("/api/admin/grants")
async def list_grants_endpoint(request: Request, scope_kind: str, scope_id: str | None = None) -> dict:
    await _require_admin(request)
    if scope_kind not in ("user", "project", "global"):
        raise HTTPException(status_code=400, detail="bad scope_kind")
    from src.core.capability_grants import CATALOG
    return {"grants": await postgres_db.list_grants(scope_kind=scope_kind, scope_id=scope_id),
            "catalog": CATALOG}


@app.put("/api/admin/grants/{scope_kind}/{scope_id}/{key}")
async def set_grant_endpoint(scope_kind: str, scope_id: str, key: str, body: GrantSet, request: Request) -> dict:
    admin = await _require_admin(request)
    from src.core.capability_grants import CATALOG
    if key not in CATALOG or scope_kind not in ("user", "project", "global"):
        raise HTTPException(status_code=400, detail="unknown key or scope_kind")
    return {"grant": await postgres_db.set_grant(
        scope_kind=scope_kind, scope_id=(None if scope_kind == "global" else scope_id),
        key=key, value_json=body.value_json, actor=str(admin["id"]), reason=body.reason)}


@app.delete("/api/admin/grants/{scope_kind}/{scope_id}/{key}")
async def delete_grant_endpoint(scope_kind: str, scope_id: str, key: str, request: Request) -> dict:
    admin = await _require_admin(request)
    return {"deleted": await postgres_db.delete_grant(
        scope_kind=scope_kind, scope_id=(None if scope_kind == "global" else scope_id),
        key=key, actor=str(admin["id"]))}


@app.get("/api/users/me/capabilities")
async def my_capabilities(request: Request) -> dict:
    user = await require_approved_user(request, postgres_db)
    from src.core.capability_grants import CATALOG
    if user.get("is_admin"):
        return {"is_admin": True, "grants": None, "catalog": CATALOG}
    from orchestrator.services.grants_service import resolve_grants_for
    grants = await resolve_grants_for(postgres_db, user_id=str(user["id"]),
                                      project_ids=await _grant_project_ids(user))
    return {"is_admin": False, "grants": grants, "catalog": CATALOG}
```

- [ ] **Step 2: Renumber 0029→0030 EVERYWHERE.** Edit: `docs/db_migration.md:365` (note that 0029 went to mistral; grants = 0030); `docs/done/global_expert_management.md` (schema header ~238, Slice-1 reserve ~489, References bullet ~620); **and `docs/superpowers/plans/2026-06-15-user-defined-experts-slice-1.md`** (the `0029_capability_grants.sql` reserved-slot lines ~79/84/91/96/146). Add a one-line note in the spec's enforcement section that the resolved-config refactor routes `persistent_agent` through the single merged fragment.
- [ ] **Step 3: Verify the renumber is complete.** Run: `grep -rn "0029_capability_grants" docs/`  Expected: no matches.
- [ ] **Step 4: Checkpoint.**

---

### Task 15: Adversarial unit matrix + live k3d verification

**Files:** `tests/test_capability_grants.py`; live cluster.

- [ ] **Step 1: Adversarial unit cases:**

```python
def test_user_grant_cannot_exceed_project_ceiling():
    from src.core.capability_grants import resolve_grants
    g = resolve_grants(user_rows=[{"key": "autonomy_ceiling", "value_json": "full"}],
                       project_rows=[{"key": "autonomy_ceiling", "value_json": "guided"}],
                       global_rows=[])
    assert g["autonomy_ceiling"] == "guided"


def test_null_deletion_of_guardrail_caught_in_merged():
    from src.core.capability_grants import evaluate, CATALOG
    d = {k: v["default"] for k, v in CATALOG.items()}
    assert evaluate({"autonomy": "full"}, d)  # ceiling review -> flagged


def test_cross_layer_credential_assembly_denied():
    from src.core.expert_resolution import hard_deny_scan
    assert hard_deny_scan({"llm": {"model": "x", "api_key": "leaked"}})
```

- [ ] **Step 2: Run the full pure suite.** Run: `python -m pytest tests/test_capability_grants.py -v`  Expected: PASS.

- [ ] **Step 3: Live k3d integration** (asyncpg/auth surface; MCP-header user-auth trick: `X-Internal-Key: dev_mcp_internal_key` + `X-MCP-User-Id: <uuid>` to `localhost:8085`). Verify:
  - **Migration + grandfather:** `0030` applied; the admin + every approved user has `shell_tools`+`delegation`+`vm_workspace` grant rows; an **existing user's ordinary job still dispatches** (grandfather works — no self-DoS).
  - **New user deny-by-default:** create a fresh approved user with no grants → an ordinary worker job **fails** at dispatch naming `shell_tools`/`delegation`; grant them → it dispatches.
  - **Save-time 422:** non-admin without browser grant... (skip — browser allow); instead a non-admin without `vm_workspace` saving `config={"workspace":{"backend":"vm"}}` → 422 naming `vm_workspace`.
  - **Dispatch reject after revoke + resume:** grant `shell_tools`, run a job, pause it, revoke → **resume fails** with the message (Task 11).
  - **persistent_agent path:** a no-grant user PATCHing `persistent_agent.permission_mode=autonomous` → session attach rejected (`permission_mode` ceiling).
  - **Automation fire:** an automation owned by a no-shell user whose config uses shell → the auto-created job fails at dispatch (covered transitively via `_dispatch_job_to_agent`).
  - **Scholar/critic subjob:** a no-delegation user's job that spawns a critic → the subjob (inherits owner) fails (documents the deny-by-default consequence).
  - **VM parity + kill-switch + audit:** migrated `vm_workspace` gates VM like the old column; admin bypasses; `user_experts` off → creation 403; every set/update/revoke wrote a `capability_grant_audit` row.

- [ ] **Step 4: Final checkpoint.** Report results; leave staged for the user.

---

## Self-Review

**Spec coverage:** decision 8 (catalog/table/migrate-in/kill-switch) → T1,2,6,11; decision 9 (save 422 + dispatch/resume reject on merged stack incl. persistent_agent, no silent strip) → T8,10,11,12; **decision 19 (no-op on upgrade)** → T1 grandfather backfill; decision 21 (single PDP/two PEPs) → one `evaluate()` (T4) at T8/10/11/12; decision 22 (restrict-only escalation-safe) → T3; decision 23 (append-only audit + scope cleanup) → T6. Adversarial acceptance (dup-key/non-ASCII/cross-layer/null-deletion/escalation) → T5,15. Renumber → T1,14.

**Review findings folded in (changelog):** B1 self-DoS → deny-by-default via **grandfather** (T1) not base-trim; B2 session autonomy → **`permission_mode`** key (T2/4); B3 resume bypass → **T11**; JSONB-as-string → `json.loads` on read (T6); `user_visible_project_ids(user, db)`/`"all"` → T8/14; `delete_grants` both paths in-txn → T6; `delegation.enabled` not dict-presence → T4; schema.sql mirror **dropped** (frozen/not-applied) → T1; renumber completeness (Slice-1 plan + refs) → T14; reject non-ASCII keys → T5; per-capability fail-mode → T13/Context; principal owner-vs-runner + view-as → documented in T10; automation/subjob coverage → T15.

**Type consistency:** `evaluate(fragment, grants, *, is_admin=False) -> list[str]`, `resolve_grants(*, user_rows, project_rows, global_rows) -> dict`, `_enforce_dispatch_grants(merged, *, runner_user_id, project_ids)`, `_enforce_save_grants(config, *, user)`, `_grant_project_ids(user) -> list[str]`, `GrantDenied.violations: list[str]`, grant rows `{key, value_json}` (deserialized) — consistent across T3–T15.

**Open / flagged for the user:** `browser` kept allow-by-default (flip to deny+grandfather if strict web-gating wanted); `permission_mode` default `supervised`; runner=owner admin-bypass + delegation-child inheritance accepted for v1 (spec defers transitive delegation checks); future: runner∩author intersection, per-request capability tokens, mid-run re-check on revoke (bounded-TOCTOU accepted).
