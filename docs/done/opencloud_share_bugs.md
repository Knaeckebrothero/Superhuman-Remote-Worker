# OpenCloud session-folder share bugs — fixed 2026-04-23

**Status:** Fixed in commit `25283b3` (silently bundled into a larger Keycloak
commit — not mentioned in the commit message, hence the postmortem here so
the fix is discoverable).

**Verified fixed on 2026-05-01** by re-reading
`orchestrator/services/cloud/opencloud.py` against the original symptoms.

## Symptoms

`_setup_main_cloud` (orchestrator `main.py:9466-9471`) called
`share_session_folder` for every new persistent session. The call failed,
the warning-log-and-continue branch swallowed the error, and the user got
an unshared folder — clicking the cloud button in the cockpit thread
returned 404 because the user had no read ACL on the agent-home Space's
session subfolder.

The failure mode was silent: orchestrator log line ≈ "share failed,
continuing" with no escalation, and the cockpit had no signal that the
folder existed but was inaccessible.

## Root causes

### Bug 1 — wrong role weight (was at `opencloud.py:61-63`, used at `:721-724`)

```python
_SPACE_EDITOR_ROLE_WEIGHT = 90  # "Can edit" — Space-applicable
_SPACE_EDITOR_ROLE_NAME = "Can edit"
```

OpenCloud's role catalog has multiple "Can edit" entries with different
weights, each applicable to a different resource type:

- weight=10 / 60 — **share-weighted**, target
  `/drives/{id}/items/{item}/invite` (a specific subfolder within a drive)
- weight=40 / 90 / 120 — **Space-weighted**, target
  `/drives/{id}/root/invite` (a whole drive)

Session folders are subfolders inside the agent-home Space, so they need
the share-weighted "Can edit" (weight=60). Using weight=90 against
`/items/{item}/invite` returns `400 "role not applicable to this
resource"`.

### Bug 2 — `_resolve_item_id` used a LibreGraph path that doesn't resolve subfolders in project Spaces (was at `opencloud.py:1166-1186`)

```python
f"/graph/v1.0/drives/{safe_drive}/root:/{safe_path}"
```

Returned 404 for `sessions/b3f9eb0b` even when the folder existed
(confirmed via WebDAV PROPFIND against the same drive+path). The native
LibreGraph `root:/{path}` form on current OpenCloud
(`docker.io/opencloudeu/opencloud:latest` as of 2026-04-23) does not
resolve subfolders inside project-style Spaces — only Space roots.

## Fix

Both fixes shipped in commit `25283b3` (2026-04-23):

1. **Disambiguated role constants** at `opencloud.py:73-78`:
   ```python
   _SPACE_EDITOR_ROLE_WEIGHT  = 90  # whole-Space invite
   _SPACE_VIEWER_ROLE_WEIGHT  = 40  # whole-Space invite
   _FOLDER_EDITOR_ROLE_WEIGHT = 60  # subfolder invite
   ```
   The 55-72 comment block documents the weight↔endpoint mapping so the
   next reader doesn't repeat the mistake. `share_session_folder`
   (`:741-759`) now uses `_FOLDER_EDITOR_ROLE_WEIGHT`; whole-Space
   invites (project Spaces) keep using `_SPACE_EDITOR_ROLE_WEIGHT`.

2. **Rewrote `_resolve_item_id`** at `opencloud.py:1201-1259` to do
   WebDAV PROPFIND on `/dav/spaces/{drive}/{path}` (Depth: 0), extract
   `oc:fileid` from the response with a regex, and return the compound
   `{drive_id}!{item_uuid}` id that the
   `/graph/v1beta1/drives/{id}/items/{item}/invite` endpoint expects.

## Loose ends not covered by the fix

These were noted during the original investigation but deliberately left
out of scope; they belong in their own issue if/when they bite:

- `ensure_user` 400-collision handling: when an admin creates a user in
  Keycloak before OpenCloud has provisioned them, the
  identities.issuerAssignedId field on the OpenCloud user can be missing.
- `_find_user_by_sub` is broken in practice because OpenCloud's `$search`
  doesn't index `identities.issuerAssignedId`. The runtime works because
  `resolve_user_identity` falls back to email-based lookup, but the
  sub-based path silently returns nothing. Fixable either by switching
  the query to `$filter` (if supported) or by deleting the sub-based
  branch and committing to email as the join key.

## Related

- `docs/done/session_folder_placement.md` — the layout question that
  this investigation surfaced (project-bound vs cross-project session
  folders). Independent of these bugs but discovered together.
