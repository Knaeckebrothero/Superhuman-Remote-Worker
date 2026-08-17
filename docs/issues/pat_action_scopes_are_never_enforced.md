# PAT action scopes are validated, stored, and never enforced

**Status:** Open — found 2026-08-17 while auditing why an MCP token in
`.mcp.json` could drive the full orchestrator REST API. **Latent, not live:**
zero PATs exist on dev (`SELECT count(*) FROM auth_tokens WHERE kind='api'` → 0),
so nobody currently holds a credential they wrongly believe is constrained. The
defect arms itself the moment the first scoped PAT is issued.

## Symptom

`POST /api/api-keys` accepts an action-scope list, validates it against a
closed vocabulary, rejects unknown values, and persists it. Auth then loads
those scopes onto the caller. **No route ever reads them.** A PAT minted as
read-only `jobs:read` carries its owner's full write and admin reach, and the
`admin` scope is likewise just an unchecked string in a list.

The advertised vocabulary (`orchestrator/main.py:9582`):

```python
VALID_PAT_SCOPES = {
    "jobs:read", "jobs:write",
    "chat:read", "chat:write",
    "knowledge:read", "knowledge:write",
    "admin",
}
```

## Evidence

The scope value survives the whole pipeline except the last step:

| stage | site | behaviour |
|---|---|---|
| validated | `main.py:48705` | `bad = requested - VALID_PAT_SCOPES` → 400 on unknown |
| persisted | `main.py:48731` | `scopes=sorted(requested)` |
| loaded (PAT) | `security/auth.py:489` | `user["scopes"] = list(row.get("scopes") or [])` |
| loaded (legacy MCP) | `security/auth.py:522` | single `'user'`/`'all'`/`'project:<uuid>'` string |
| loaded (MCP header path) | `security/auth.py:643` | from `X-MCP-Scope` |
| **enforced** | — | **nowhere** |

`security/access.py:1165` is where a reader would expect the check, and it
explicitly defers:

```python
# Legacy MCP rows carry exactly one scope string; PAT rows have a list
# of action scopes (not the legacy 'user'/'all'/'project:<uuid>' shape).
# Treat anything that doesn't look like a legacy MCP scope as a no-op
# here — PAT scopes are checked by the action-scope decorator, not by
# row-level visibility.
```

**That decorator does not exist.** `grep -rn "requires_scope\|require_scopes\|@scope"
orchestrator/ --include=*.py` returns 0 hits. The `_action_scope` matches in
`services/lifecycle/vm_manager.py` are unrelated VM-lifecycle context managers.

Every actual scope consumer handles only the legacy `project:<uuid>` shape and
returns "no restriction" for everything else:

- `access.py:239 _scope_project_id` — returns `None` for PAT auth and for
  `user`/`all`.
- `access.py:1144 apply_mcp_scope` — returns `("", {})` for `''`, `all`,
  `user`, and for PAT-shaped lists.
- `main.py:29258 _require_scoped_datasource_mutation` — calls
  `mcp_scope_project_id(user)` and returns an empty set unless the token is
  project-scoped.

## What is *not* wrong

Two things that look adjacent and are working as designed — recorded so a
future reader doesn't "fix" them:

1. **An MCP token authenticating the REST API directly is intended.**
   `_resolve_legacy_mcp_token` (`auth.py:494`) documents itself as serving
   "callers that hit the orchestrator API *directly* with an MCP token in the
   Authorization header — the consolidated auth surface advertised in the
   design doc." The MCP server is a client, not an authorization boundary; the
   MCP tool schema is a UX affordance. Enforcing authorization at the tool-schema
   layer would be client-side security and a worse design.
2. **`project:<uuid>` scoping is genuinely enforced and fails closed.** A
   malformed UUID yields an all-zero sentinel (`access.py:259`) and an
   unsatisfiable `project_id = NULL` WHERE fragment, so a bad token gets the
   empty set rather than everything.

## Secondary findings from the same audit

- **`all` and `user` are behaviourally identical.** Both return an empty
  restriction from `mcp_scope_restriction`; `all` is documented as
  "admin-equivalent *for this token's user*", and admin bypass is gated on the
  realm role, not the scope. A UI showing "scope: user" therefore implies a
  constraint that does not exist. Either implement the distinction or collapse
  the vocabulary to `all` + `project:<uuid>`.
- **Non-expiring, unrevoked, dormant tokens.** On dev, 4 of 6 `kind='mcp'` rows
  have `expires_at IS NULL`, none are revoked, and at least two are dormant
  (`last_used_at` 2026-07-17 and 2026-08-04). For an admin owner these are
  standing admin-capable credentials. Practically this is a larger exposure
  than the scope gap, and it is fixable today by revoking the dormant rows.

## Why it matters now rather than later

The scope system is exactly the kind of control that gets *assumed* by whatever
is built on top of it. Two things on the roadmap consume that assumption:
`docs/multi_tenancy.md` (Tier 0) and the public FSL release, after
which "hand someone a scoped token" becomes a normal operation rather than a
theoretical one. A vocabulary that validates and stores but never checks is
worse than no vocabulary, because it produces confident, wrong mental models —
and the 400 on an unknown scope actively reinforces the belief that the system
is checking.

## Fix sketch

Small and mechanical; the plumbing already exists.

1. Add a `require_scopes(*needed)` FastAPI dependency in
   `orchestrator/security/auth.py` that:
   - no-ops when `user["auth_method"]` is cookie/OIDC (session callers are
     governed by role + visibility, not token scopes);
   - no-ops for legacy MCP scope strings (`''`/`user`/`all`/`project:<uuid>`),
     which keep their current row-level semantics;
   - otherwise requires `needed ⊆ set(user["scopes"])`, treating `admin` as a
     superset, and raises 403 `"Insufficient token scope"` on failure.
2. Apply it to the routes matching the advertised vocabulary — job read/write,
   chat read/write, knowledge read/write — and gate admin-only routes on
   `admin`.
3. **Fail closed on the default**: a PAT presenting an empty scope list should
   be rejected rather than treated as unrestricted, otherwise the bug survives
   as its own default.
4. Delete the stale "checked by the action-scope decorator" comment at
   `access.py:1171` — or make it true.
5. Tests: one PAT per scope proving the allowed route passes and a
   neighbouring route 403s; one proving cookie/OIDC callers are unaffected;
   one proving an empty-scope PAT is refused.

Related: `docs/features/auth_bff_and_api_tokens.md` §3 (PAT design),
`docs/issues/gitea_admin_credential_in_every_agent_workspace.md` (the other
standing-credential issue).
