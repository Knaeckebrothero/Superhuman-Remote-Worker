---
tags:
  - feature
  - contacts
  - communication
  - cockpit
  - workspace
aliases:
  - contacts registry
  - address book
  - contacts page
related:
  - "[[whatsapp_messaging_channel]]"
  - "[[notify_user_tool]]"
  - "[[email_datasource]]"
  - "[[datasource_redesign]]"
  - "[[mcp_datasources]]"
  - "[[virtual_directories]]"
---

# Contacts Registry

> One address book for every person an agent may reach — email today, WhatsApp next — owned by a user, linked to projects, visible in Cockpit, and readable by agents as workspace files.

**Status:** Design approved 2026-07-26→28 (brainstorm; Cockpit layout picked via visual mockups), spec written 2026-07-29. §Agent surface amended 2026-07-30 to ride on [[virtual_directories]] instead of materialized files. Not yet implemented.
**Filed:** 2026-07-29

## Motivation

A registry of messageable third parties **already exists and already gates sends** — it just has no UI and can't hold anything but an email address:

| Piece | Where (cite symbols — `main.py` line numbers drift) |
|---|---|
| `external_contacts` table (`project_id NOT NULL`, `display_name`, `email`; `UNIQUE (project_id, email)`) | `orchestrator/database/migrations/app/0001_initial.sql:1221` |
| CRUD endpoints: `GET/POST /api/projects/{id}/contacts`, `DELETE /api/projects/{id}/contacts/{cid}` (all `require_project_member`; the delete never checks the contact belongs to *that* project — any project editor can delete any contact by id) | `orchestrator/main.py` — `add_external_contact`, `list_external_contacts`, `delete_external_contact` |
| DB methods incl. `resolve_external_contact(project_id, to)` | `orchestrator/database/postgres.py` — `add_external_contact` ff. |
| `send_message` resolution: project members → external contacts → 404 with `"Available: …"` | `orchestrator/main.py` — `send_agent_message` |
| Cockpit consumers | **none** — zero references in `cockpit/src` |

Meanwhile [[whatsapp_messaging_channel]] (approved, unimplemented) proposes a **second** registry (`messaging_contacts`, its migration 0063) because it didn't know this one existed. Two registries means two opt-in models and two places to add the same stakeholder. This feature replaces both with one normalized, cross-channel registry and builds the missing UI — it is the prerequisite slice the WhatsApp channel lands on top of.

Driving use case: stakeholder interviews — register a client's executives once, link them to the project, and let the agent message them (email now, WhatsApp once that channel ships) and know who is reachable where.

## Scope decisions (user, 2026-07-26→28)

- **Cross-channel address book**, not a WhatsApp-specific registry. One contact = one person with N channel addresses.
- **Not a connector type.** Connectors grant capability (credentials → tools); a contact grants nothing — it is an addressee that channels resolve against. The WhatsApp *channel* (credentials, adapter, webhook) is the thing that becomes a connector later; explicitly out of scope here.
- **Project-linked scoping**, mirroring `project_datasources`: an agent sees exactly the contacts linked to its project. The registry stays the control point — no per-message human approval, no raw addresses accepted from agents.
- **Own top-level Cockpit page** at `/contacts` (peer of `/datasources`), not a tab inside Connectors and not per-project-only UI.
- **Agents read contacts as a virtual `contacts/` directory** (`contacts/<slug>.md` served live through the file tools by [[virtual_directories]]; originally specced as materialized files, amended 2026-07-30). No new agent tools.

## Data model

Migration `0072_contacts_normalize.sql` (number = next free at implementation time; uncommitted branches may claim 0072 first):

```sql
contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    notes TEXT,                          -- free text; becomes the agent-visible file body
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

contact_addresses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- denormalized from contacts
    channel TEXT NOT NULL CHECK (channel IN ('email', 'whatsapp')),
    address TEXT NOT NULL,               -- email address, or E.164 with leading '+'
    is_primary BOOLEAN NOT NULL DEFAULT false,
    opt_in_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (opt_in_status IN ('pending', 'opted_in', 'opted_out')),
    last_inbound_at TIMESTAMPTZ,         -- WhatsApp 24h-window state; NULL for email
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (owner_user_id, channel, address)
);
CREATE INDEX idx_contact_addresses_contact ON contact_addresses(contact_id);
CREATE UNIQUE INDEX uq_contact_primary_per_channel
    ON contact_addresses(contact_id, channel) WHERE is_primary;

project_contacts (                        -- mirrors project_datasources
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    added_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (project_id, contact_id)
);
CREATE INDEX idx_project_contacts_contact ON project_contacts(contact_id);
```

Semantics:

- **Uniqueness is per owner**, not global: two users may each register `anna@acme.de`. Inbound WhatsApp routing stays deterministic because a webhook is already scoped to the owner whose WABA credentials received it. `owner_user_id` is denormalized onto `contact_addresses` purely to make this constraint expressible; the application keeps it in sync with the parent contact (contacts never change owner in v1).
- **`is_primary` is per (contact, channel)**, enforced by the partial unique index. The first address added on a channel is auto-primary.
- **`opt_in_status` is channel-semantic.** WhatsApp addresses start `pending`; the first inbound message flips them to `opted_in` (Meta's self-serve opt-in, per the WhatsApp spec). Email addresses are created `opted_in` — no email opt-in requirement exists today, and anything else would silently regress current `send_message` behavior. Enforcement belongs to the sending channel, not the registry.
- **Editing an address value resets it**: `opt_in_status` back to the channel default, `last_inbound_at` to NULL. A consent state must not survive a change of the thing consented with.
- **Addresses are normalized on input**: emails lowercased; WhatsApp numbers stripped of spaces/dashes/parens and stored E.164 (`^\+[1-9]\d{6,14}$`). Inbound-webhook matching (Meta's `wa_id` has no `+`) normalizes before lookup.
- **A contact with zero addresses is legal** (a person you know but haven't collected details for). It renders as "no addresses" and can never be resolved as a recipient.
- **Visibility rule:** a user sees contacts they own **∪** contacts linked to any project they are a member of. Needed because an editor may link a colleague's contact into a shared project; co-members must see what their agents can reach.

### Migration & backfill (two-phase)

**Phase 1 — `0072`:** create the three tables; backfill; **leave `external_contacts` in place, untouched**. Code switches to the new tables in the same release (dev chart deploys migration + code together).

Backfill rules:
- Owner resolution per `external_contacts` row: `added_by` → else the project's `role='owner'` member (oldest membership if several) → else skip with a warning log (an owner-less row is unresolvable; `owner_user_id` is NOT NULL).
- Dedupe on `(resolved_owner, lower(email))`: one `contacts` row + one `'email'` address (`opted_in`, primary) + one `project_contacts` row per source project. On `display_name` conflict, the most recently created source row wins; log the losers.
- Idempotent: re-running produces no duplicates (match on the unique address key).

**Phase 2 — `0073` (a later release, after dev has run on the new path):** re-run the backfill sweep to catch any rows written to `external_contacts` in the deploy window, then drop the table. Prod is a manual `v0.0.X` cut with no rollback story for a bad one-shot backfill — hence two phases.

## API surface

There are zero UI consumers and `send_message` calls the DB layer directly, so this is a redesign, not a compat exercise. Only `tests/test_project_access.py` breaks, and it is updated alongside.

New top-level resource (all under the BFF-authed surface):

| Method | Path | Auth |
|---|---|---|
| `GET` | `/api/contacts` (`?project_id=`, `?channel=`, `?q=`) | authed; returns owned ∪ project-linked; `q` matches name or address (ILIKE); sorted `display_name`; no pagination in v1 (address books are small). Returns full objects — nested `addresses[]` and `projects[{id,name}]` — so the UI needs no second fetch |
| `POST` | `/api/contacts` `{display_name, notes?, addresses?[]}` | authed; caller becomes `owner_user_id` |
| `PATCH` | `/api/contacts/{id}` `{display_name?, notes?}` | owner only |
| `DELETE` | `/api/contacts/{id}` | owner only (cascade unlinks projects) |
| `POST` | `/api/contacts/{id}/addresses` `{channel, address, is_primary?}` | owner only |
| `PATCH` / `DELETE` | `/api/contacts/addresses/{address_id}` (`{address?, is_primary?}`; promoting to primary atomically demotes the channel's previous primary; changing `address` resets opt-in state) | owner only |
| `POST` / `DELETE` | `/api/contacts/{id}/projects/{project_id}` | `require_project_member(min_role="editor")` **and** contact visible to caller |

Kept and reshaped project-scoped endpoints:

- `GET /api/projects/{id}/contacts` — kept (project detail page wants it; gate already tested). Becomes a filtered view over the join.
- `POST /api/projects/{id}/contacts` — same URL and `min_role="editor"` gate; body grows to `{display_name, addresses?[]}`; semantics become **find-or-create-then-link**: match by any supplied address among caller-visible contacts, else by exact `display_name` among caller-visible contacts, else create owned by caller; then link (`added_by` = caller).
- `DELETE /api/projects/{id}/contacts/{cid}` — **retired.** Removing a contact *from a project* is the new unlink (`DELETE /api/contacts/{cid}/projects/{id}`); destroying the contact is owner-only `DELETE /api/contacts/{id}`. (The old endpoint conflated the two — and never verified the contact belonged to the project.)

**Ownership split:** project editors link/unlink contacts on their projects; only the contact's **owner** mutates or deletes the contact itself. Otherwise an editor on one project could rename a person out from under every other project using them.

### Resolver

`resolve_external_contact(project_id, to)` → **`resolve_contact(project_id, to, channel)`**:

1. Candidates = contacts linked to `project_id` where `lower(display_name) = lower(to)` or any address equals `to` (normalized).
2. Zero candidates → not-found (existing 404 text, now listing project-linked contact names).
3. More than one candidate by name → ambiguity error naming the candidates and their addresses ("two contacts named Anna — specify an address").
4. One candidate, no address on `channel` → the channel-specific error (see below).
5. Else return the primary address for `channel` (else newest on that channel).

`send_message` passes `channel='email'`; the future `message_contact` passes `'whatsapp'`. Rate limits, audit (`message_log`), and the messaging endpoints are otherwise untouched.

## Cockpit page

- Route `/contacts`, **lazy** `loadComponent` (the admin routes at `app.routes.ts:64+` are the pattern; the initial-bundle budget hard-fails at 2.75MB, so no new eager page).
- New directory `cockpit/src/app/views/contacts/` — deliberately *not* an extension of `datasource-list.component.ts` (3263 lines; the cautionary tale). Three components:
  - `contacts-page` — route shell, header, "+ New contact".
  - `contact-list` — rows + expansion.
  - `contact-form` — create/edit panel (house `showForm()`/`editingId()` pattern, one open at a time).
- Sidebar entry between Connectors and Experts. Transloco keys `contacts.*` in **both** `en.json` and `de-DE.json`.
- **Layout (decided via mockups): expandable rows, read-only expansion ("C1").**
  - Row: `display_name` + channel chips (email/whatsapp) + project count. Opt-in state is per **address**, not per contact, so it lives on the chip: a chip whose primary address is not `opted_in` carries the state (e.g. `whatsapp·pending`). The row still answers "who can actually be reached on WhatsApp?" at a glance.
  - Click row → expands in place, read-only: addresses with primary/opt-in/window state, project chips, `Edit`/`Delete`. Any number of rows may be open; nothing is ever dirty.
  - `Edit` → opens `contact-form` (addresses as key-value-style rows with add/remove, mirroring the env-var editor; project link chips with add/remove).
  - Delete → `AppConfirmNameDialogComponent`, dialog **names the projects that will lose the contact** (cascade).
- Filters: search box (`q`), project, channel — matching the approved mockup.
- vitest specs alongside (house: vitest is the reliable lane; `ng build` needs the monaco loader workaround).

```
Contacts                                    [+ New contact]
search…                          filter: project | channel
──────────────────────────────────────────────────────────
▾ Anna Weber        [email] [whatsapp·pending]      2 proj
    email     anna@acme.de · primary
    whatsapp  +49 170 … · primary · opt-in pending
    projects  (Acme Website) (Q3 Research)
    [Edit] [Delete]
▸ Markus In         [whatsapp]                      1 proj
▸ Priya Nair        [email]                         1 proj
```

## Agent surface — virtual `contacts/` directory, no new tools

*(Amended 2026-07-30: originally materialized files; now a virtual projection via [[virtual_directories]]. The rendered file format below is unchanged.)*

**DB is the source of truth; the agent sees a read-only virtual projection.** The [[virtual_directories]] ContactsProvider serves `contacts/<slug>.md` for every contact linked to the job's project — live through the file tools (orchestrator internal endpoint, ~60s TTL cache), plus a `README.md` index (display name + channel chips). Nothing is written to the workspace filesystem:

```markdown
---
name: anna-weber
display_name: Anna Weber
addresses:
  - {channel: email,    address: anna@acme.de, primary: true}
  - {channel: whatsapp, address: "+4917055…",  primary: true, opt_in: pending}
projects: [Acme Website, Q3 Research]
---

Head of Operations. Prefers short messages, CET. Owns the checkout flow —
ask her about cart abandonment before anyone else.
```

- Body = `notes` — the natural home for a per-stakeholder interview brief.
- **Raw addresses are included** (user decision 2026-07-28): useful to the agent, harmless to authorization — the orchestrator resolves recipients by name server-side and rejects raw addresses from agents. Snapshot-PII is handled by retention policy, not by crippling the file.
- Slugs: kebab-cased `display_name`, sanitized (existing `_safe_component` pattern), `-2` suffix on collision.
- Read path: existing `read_file` / `search_files` / `list_directory` (served by the overlay; invisible to the shell). Write path: none — mutations into `contacts/` are rejected by the overlay with a teaching error; the agent-edit-overwrite ambiguity of materialized files no longer exists.
- **Why files can't be the grant** (the registry stays server-side): a projection must not mint recipients (`datasource_redesign.md` records exactly this failure class with the advisory `read_only` flag); the orchestrator authorizes sends and resolves recipients server-side; `opt_in_status`/`last_inbound_at` are webhook-written runtime state. The virtual projection makes this structural — there is no writable surface at all.
- **Freshness:** linking a contact mid-session appears within the ~60s TTL (the materialization design's "invisible until next start" limitation is gone). Sends were always live (resolution is server-side).
- **Failure mode:** per [[virtual_directories]] — stale cache served on fetch failure; with no cache, reads return a "contacts temporarily unavailable" tool error; boot never blocks.
- Gated by `VIRTUAL_DIRS_ENABLED` (default `true`), shared with `tools/` — supersedes the previously planned `CONTACTS_MATERIALIZE_ENABLED`.
- ~~`contacts/` in `_LOOP_MAIN_GITIGNORE`~~ — no longer needed: no real files exist to leak onto a loop's `main` or into workspace snapshots. (The `skills/`-style PII-leak class is structurally closed.)
- No `list_contacts` tool, no context injection (revised during brainstorm): capability-surface cost rule — the virtual projection gives discovery for free (including its README index), and `send_message`'s `"Available: …"` 404 remains the fallback. If WhatsApp's `message_contact` later proves a listing tool necessary, it rides along then.

## Error handling

| Case | Behaviour |
|---|---|
| Unknown recipient | Existing 404 + `"Available: …"` listing project-linked contact names |
| Contact has no address on requested channel | Distinct error: *"Anna Weber has no email address (whatsapp only)"* — a generic not-found here would be actively misleading |
| Ambiguous name in project | Error naming candidates + addresses; caller retries with an address |
| Duplicate address on create | 409 from `UNIQUE (owner_user_id, channel, address)`; UI offers "link the existing contact to this project instead" |
| Delete linked contact | Cascade unlinks; confirm dialog names affected projects |
| Non-owner PATCH/DELETE | 403 |
| Non-member/viewer link attempt | Existing `require_project_member` 403 (editor floor) |
| Malformed address | Per-channel validation: existing email check; E.164 for whatsapp |
| Virtual-projection fetch failure | Stale cache served if warm; else "contacts temporarily unavailable" tool error; never blocks job start ([[virtual_directories]]) |

## Testing

- **Migration/backfill:** each `external_contacts` row → exactly one contact + one `opted_in` primary email address + one project link; dedupe across projects; owner fallback chain; idempotent re-run.
- **Cross-tenant:** two owners each holding `anna@acme.de` both succeed and never resolve into each other's contacts. (Exercises the per-owner uniqueness correction; the one test not to skip.)
- **Resolver:** name match, address match, wrong-channel miss (distinct error), unknown miss, ambiguous name, primary selection.
- **Access gates:** extend `tests/test_project_access.py` — member-only list, editor-only link/unlink, owner-only mutate, visibility union.
- **Virtual projection:** rendered files match the frontmatter format; slug collision determinism; README index; TTL/stale/error paths; mutation rejection — per [[virtual_directories]] §Testing (ContactsProvider bullets live there).
- **Cockpit (vitest):** rows render chips/opt-in/counts; expansion toggles read-only; single form instance; delete dialog names projects.
- CI (Py3.12) is the gate (local pytest is noisy on 3.14); live-verify on local k3d before dev deploy, per house practice.

## Out of scope (v1)

WhatsApp channel connector (credentials, adapter, webhook, `message_contact`) — this registry is its prerequisite · migrating the email-datasource recipient allowlist into contacts · `list_contacts` agent tool · per-contact grants · CSV/vCard import · orchestrator-MCP contact tools · contact avatars/dedup-merge UI · pagination.

## Companion change to the WhatsApp spec

[[whatsapp_messaging_channel]] §Schema is amended (same commit as this spec): drop `messaging_contacts` and migration 0063's contact table; key `messaging_conversations` / `messaging_messages` off `contact_addresses.id`; window state (`last_inbound_at`) lives on `contact_addresses`. Its `list_contacts` tool is superseded by the file projection.

## Decision log

- **2026-07-26:** Cross-channel address book; **not** a connector type (contacts constrain reach, connectors grant capability); project-linked scoping via a `project_contacts` join; own top-level `/contacts` page. (User.)
- **2026-07-26:** Normalize into `contacts` + `contact_addresses` + `project_contacts`, replacing `external_contacts`; two-phase migration (backfill now, drop later). (User.)
- **2026-07-27:** Layout C — expandable rows — then C1: expansion strictly read-only, editing via the house form panel, one at a time. (User, via visual mockups.)
- **2026-07-28:** Agents consume contacts as materialized `contacts/<slug>.md` workspace files (skills-projection pattern), DB remains truth; raw addresses included in frontmatter; no `list_contacts` tool. (User.)
- **2026-07-29:** Per-owner (not global) address uniqueness; channel-semantic opt-in defaults (email `opted_in`, whatsapp `pending`); opt-in resets on address edit; find-or-create-then-link semantics for the project-scoped POST. (Spec.)
- **2026-07-30:** Agent surface re-based from materialized files onto the [[virtual_directories]] ContactsProvider — live TTL reads replace boot-time seeding; gitignore/snapshot-PII/staleness/edit-overwrite concerns struck; `CONTACTS_MATERIALIZE_ENABLED` superseded by `VIRTUAL_DIRS_ENABLED`. File format and raw-address decision unchanged. (User.)
