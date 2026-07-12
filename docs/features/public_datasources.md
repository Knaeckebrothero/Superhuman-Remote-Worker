# Public Datasources: grant-gated org-wide publishing

Status: **IMPLEMENTED on develop 2026-07-12 — pending dev-deploy verification**
Date: 2026-07-11
Scope: orchestrator (grant catalog, datasource create/update API, one migration),
Cockpit (datasource modal, badges, confirmation dialogs). Builds on
`datasource_redesign.md` (CLI-for-read-write / credentials-are-the-boundary) and
coheres with `okf_knowledge_base.md` §org-vault (an org wiki is a `kb` datasource
with `is_global: true`).

## Problem

The intended model is: datasources are **private by default**, org-wide ("public",
`is_global=true`) only deliberately. Reality today:

1. **The write path is ungated.** `POST /api/datasources` passes
   `body.is_global` straight through (`create_datasource` →
   `postgres.create_datasource`). Any approved user can publish an org-wide
   datasource via raw API. The Cockpit form simply doesn't render a toggle —
   UI omission is the only "enforcement".
2. **There is no legitimate path to publish.** The OKF knowledge-base design
   explicitly relies on `is_global: true` for org-wide vaults ("no new sharing
   machinery"), and the intended org workflow is a curator maintaining shared
   datasources (e.g. the org wiki). Today that curator would have to be an
   admin using raw API calls.
3. **Public datasources hand out the publisher's credentials.** When another
   user attaches a public datasource, their agent runs with the publisher's
   stored credentials (redaction hides them from the API, not from the agent).
   Publishing therefore deserves deliberate friction and a clear read-only
   default, not a silent checkbox.

## Design

### 1. Capability grant

One new entry in the grant catalog (`src/core/capability_grants.py::CATALOG`):

```python
"public_datasources": {"type": "bool", "default": False, "restrict_only": True},
```

- **Deny-by-default**; admins short-circuit (same pattern as `vm_workspace`).
- The `/admin/grants` page is catalog-driven — the key appears there
  automatically (per-user / per-project / global scope), **zero admin-UI work**.
- New helper `user_can_publish_datasource(user)` in `postgres.py`, mirroring
  `user_can_use_vm`: admin → `True`; else resolve grants via
  `list_grants_for_scopes` + `resolve_grants` and return the `public_datasources`
  value.
- **No grandfathering.** No legitimate user-created `is_global` rows exist
  (system-seeded defaults are `created_by=NULL` and unaffected). New key defaults
  to deny for everyone; the admin grants it to the curator role/user.

### 2. Data model

One new column (migration `0056_datasources_read_only.sql`):

```sql
ALTER TABLE datasources ADD COLUMN IF NOT EXISTS read_only BOOLEAN;
```

- `NULL` = not applicable (private and job-scoped datasources — behavior
  unchanged).
- Publishing requires it set; the server defaults it to `TRUE` when absent.
- **First-class column, not a `config` key**: `datasources.config` (0054) is
  documented as *type-specific* configuration; `read_only` is cross-type,
  drives list badges/filtering, and per the asyncpg JSONB-as-string gotcha a
  boolean column is the cheaper thing to read everywhere.

**The flag is declarative, not enforced** — deliberately, per
`datasource_redesign.md`: credentials are the security boundary ("use a
read-only deploy token" for repos, a restricted DB account for databases).
The flag drives UI badges, confirmation tiers, and a note in the agent-facing
datasource index. It does **not** change tool selection or credential
injection. (Rejected alternative: routing declared-RO managed connectors into
the existing `project_read_only` tool fork. Decided against for v1 — one
mechanism, one story: credentials enforce, the flag declares.)

**Exception — `kb` datasources are read-only by architecture.** Per
`okf_knowledge_base.md` (org-vault write policy, resolved 2026-07-11), every
external KB datasource is read-only in v1 and its credentials never reach
agents (orchestrator-side indexing; agents get query tools only). For
`type="kb"`, the server forces `read_only=TRUE` on publish and rejects
`read_only=false`; the UI renders the access choice as fixed "Read-only".
This is the primary intended use case (org wiki), and it is the one place
read-only is genuinely enforced — for free.

### 3. API enforcement

- `POST /api/datasources` with `is_global=true` → **403** unless
  `user_can_publish_datasource(user)`. Detail string:
  `"Publishing public datasources requires the 'public_datasources' capability"`.
- `DatasourceUpdate` gains two optional fields: `is_global: bool | None`,
  `read_only: bool | None`.
  - `is_global false→true` (publish): requires the grant.
  - `is_global true→false` (unpublish): **creator/admin only, no grant needed**
    — revoking a user's grant must not trap their datasource in public state.
  - `read_only` edits: allowed for whoever can edit the datasource (existing
    creator/admin gate); `type="kb"` rejects `read_only=false` (400).
  - Server keeps the invariant: `is_global=true` ⇒ `read_only IS NOT NULL`
    (defaults to `TRUE` on publish if unset).
- MCP `create_datasource` tool: unchanged — it exposes no `is_global` param,
  so there is no MCP publish path (fine for v1).
- Redaction (`redact_datasource`) untouched — credentials stay hidden on all
  read paths regardless of scope.

### 4. Cockpit UI

**Modal (create + edit).** A "Visibility" section, rendered only when the
resolved capabilities (existing `capabilities.service` /
`GET /api/users/me/capabilities`, catalog-driven) include
`public_datasources=true`:

- Toggle: *"Public — visible to all users"*.
- When public: access radio **Read-only (default)** / Read-write, with a
  per-type credential hint (fallback generic): *"Read-only is enforced by your
  credentials — use a read-only deploy token / a restricted database account."*
  For `type="kb"`: the radio is locked to Read-only with hint *"Knowledge-base
  datasources are always read-only; credentials never reach agents."*
- Users without the capability see no Visibility section at all (their
  create/edit flow is unchanged; server still enforces).

**Confirmation tiers** (on Create/Update submit when the action applies):

| Action | Friction |
|---|---|
| Publish read-only | Standard confirm dialog (`AppDialogComponent`): "This datasource becomes visible to all users. Their agents will use its stored credentials." |
| Publish read-write, or flip a public datasource RO→RW | **Type-the-name dialog**: same warning plus write-access callout; an input must match the datasource name exactly before the confirm button enables. |
| Unpublish, or RW→RO | No dialog (reduces exposure). |

The type-the-name dialog is a small new reusable component
(`app-confirm-name-dialog`, built on `AppDialogComponent`) — nothing like it
exists in Cockpit yet. Deliberately reusable so datasource deletion (currently
confirm-less) can adopt it later; that adoption is out of scope here.

**Badges.**

- Scope badge for `is_global=true` datasources: display string changes
  "Global" → **"Public"** (i18n value edit only; keys stay `scopeGlobal`).
  Scope logic itself shipped earlier (`scopeLabelKey`/`scopeTone`).
- Public + read-write additionally shows a warning-tone **"RW"** chip in the
  datasource list and in the job/session picker (`datasources-group`
  component), so attachers see what they're touching. Public + read-only shows
  a subtle "RO" chip in the picker.

i18n: en + de-DE, `i18n:check` parity clean.

### 5. Testing

Backend (pytest):
- `POST` with `is_global=true` → 403 without grant; 201 with grant; 201 for
  admin without explicit grant.
- Update publish/unpublish matrix: publish needs grant; unpublish works for
  creator without grant; non-creator non-admin denied by existing gate.
- `read_only` defaulting on publish; `kb` + `read_only=false` → 400.
- Eligible-picker unchanged: published datasource appears for other users,
  private one does not (regression pin).

Frontend (vitest):
- Visibility section hidden without capability, shown with it.
- Confirmation tier selection (RO-publish → confirm; RW-publish / RO→RW flip →
  name dialog; unpublish → none).
- Name-dialog enable logic (exact match, case-sensitive).
- `kb` type locks the radio to read-only.

### 6. Error handling

- 403 grant denial and 400 kb-write-flag map to toasts via the existing error
  interceptor; detail strings above are user-readable.
- Capabilities fetch failure: `capabilities.service` fails open for UI
  greying elsewhere, but this section **fails closed** (hidden) — the server
  is the real gate either way.

## Out of scope (v1)

- Any read-only *enforcement* beyond the `kb` architecture (declaration only).
- Datasource-delete confirmation (component is reusable for it later).
- Per-attachment read-only overrides for public datasources
  (`project_datasources.read_only` continues to govern managed-connector mode
  for project links, unchanged).
- Credential write-probes (ro_probe-style verification).
- MCP/API publish path for agents.
- Transfer-of-ownership for public datasources.

## Coordination note

The OKF KB work in flight (uncommitted on develop as of 2026-07-11) touches the
same files this feature will edit (`datasource-list.component.ts`,
`orchestrator/main.py`, `api.model.ts`, i18n). Implementation should start
after that lands, and the `kb` publish flow should be smoke-tested against the
then-current `kb` type behavior.
