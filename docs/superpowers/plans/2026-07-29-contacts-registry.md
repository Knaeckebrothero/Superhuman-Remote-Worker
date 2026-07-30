# Contacts Registry Implementation Plan

> **EXECUTED 2026-07-30 — this plan is a historical record, not a to-do list.** Status below. Do not re-run tasks 1–8.

## Execution status

Run via subagent-driven development (fresh implementer per task, task-scoped review after each, whole-branch review at the end). All work is on `develop`, **unpushed**.

| Task | State | Commits |
|---|---|---|
| 1 · Migration + backfill | ✅ Shipped as **`0076_contacts_normalize.sql`** — the plan said 0072, it landed as 0075, then had to move to 0076 when a concurrent session's `0075_project_loop_officer_scheduling.sql` landed and was already applied on dev. `discover()` in `migrate.py` raises on duplicate prefixes, so a duplicate takes the whole runner down at boot — **always re-check the free number immediately before pushing, not just before writing** | `a5d9605c`, renumbered in `6b6cff77` |
| 2 · DB layer | ✅ Shipped (1 fix round: primary-promotion race → clean duplicate result) | `0154ff34`, `8560093c` |
| 3 · Contacts router | ✅ Shipped (1 fix round: 409 rollback, test gaps, tuple-extraction lock) | `bd8adcc4`, `62750597` |
| 4 · Send rewire + retirement | ✅ Shipped (1 fix round: find-or-create 409 rollback) | `60c7d177`, `4bdb8ed7` |
| 5 · `contact_files` | ✅ Shipped, **dormant** — retained as the [[virtual_directories]] ContactsProvider's renderer; no caller today | `17a23116` |
| 6 · Materialization wiring | ⛔ **REVERTED** — superseded by the virtual-dirs ContactsProvider (user decision, same day). Shipped then reverted; `test_contacts_materialization.py` deleted | `7d419866`, `1978a431`, reverted by `91c9e4dd` |
| 7 · Cockpit types + service | ✅ Shipped | `a8ae44b9` |
| 8 · Cockpit `/contacts` page | ✅ Shipped (1 fix round: **critical** — form re-seeds via `linkedSignal`; a stale form could previously write contact A's data onto contact B and delete B's addresses) | `cf91f79b`, `dfb3ac5e` |
| 9 · Live k3d gate | ❌ **NOT RUN** — cluster API unreachable from host; also needs a full tilt rebuild. Scope now smaller (no materialization to verify) | — |

**Whole-branch review** (no Critical): 5 Important defects fixed in one wave — `add_contact_address` committed a primary demotion it should have rolled back; duplicate-address PATCH returned 200 with a null body; blank `display_name` was writable; non-owner Edit/Delete buttons wedged the confirm dialog permanently on 403; saved-address channel edits were silently dropped. Plus chip i18n and dialog labels. → `4ccab18c`

**Deviations from this plan's text, all reviewed:** `require_project_member` returns `(user, project)` (plan assumed a bare user) → index-`[0]` extraction; the legacy delete endpoint was `/api/projects/{id}/contacts/{cid}` and never verified project membership → retired and split into unlink (editor) vs destroy (owner); `PostgresDB`, not `PostgresDatabase`; Cockpit used the house `ApiService.getProjects()` instead of the plan's inline HTTP fallback, and the confirm dialog needed an `[open]` binding the plan omitted entirely.

**Known gap for future work:** this repo's vitest harness cannot drive signal `input()` values (no ngtsc transform in `vitest.config.ts`, no `test` architect target), so the Task-8 form regression is proven by a three-layer proxy rather than end-to-end. `@angular/build`'s unit-test runner is installed but unwired — worth its own task.

**Owed:** the live gate (task 9), the phase-2 `external_contacts` drop migration (guard its sweep — see caution below), and the ContactsProvider agent surface.

> **Phase-2 caution:** naively re-running task 1's backfill body in the drop migration would overwrite `display_name`s users have since edited in Cockpit and resurrect contacts whose backfilled address they deleted. `updated_at > created_at` is *not* a usable "was edited" discriminator, because task 1 backdates `created_at` while leaving `updated_at = NOW()`.

---

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cross-channel contact registry (email + whatsapp) replacing `external_contacts`: normalized schema + backfill, owner/editor-split API, agent discovery via materialized `contacts/<slug>.md` workspace files (⛔ superseded during execution — see task 6 above), and a `/contacts` Cockpit page.

**Architecture:** Postgres migration adds `contacts` / `contact_addresses` / `project_contacts` (legacy table stays until a later drop migration). A new FastAPI router owns the CRUD + link endpoints; `send_agent_message` switches to a channel-aware `resolve_contact`. The orchestrator gathers project contacts into the resolved-config blob exactly like skills; agent + session runtimes write the files. Cockpit gets a lazy-loaded page (expandable read-only rows + one-at-a-time edit form).

**Tech Stack:** asyncpg + raw SQL (house style), FastAPI routers with late-resolved `postgres_db`, Angular standalone components + signals + Transloco, vitest.

**Spec:** `docs/features/contacts_registry.md` — the authority for behavior. Read it before starting.

## Global Constraints

- Work on `develop`. Commit after each task. **NEVER push** — pushing is the user's call (a push also triggers ruff auto-rewrite of SHAs in CI).
- End every commit message with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Migration number: plan assumes `0072`/`0073`. Before Task 1, run `ls orchestrator/database/migrations/app/ | tail -3` — if 0072 is taken, renumber (filename only; content unchanged).
- `orchestrator/main.py` is huge and actively edited by other sessions — locate code by **symbol** (grep the function name), never by line number. Same for `cockpit/src/app/core/models/api.model.ts` (currently dirty in the tree): append, don't reflow.
- Python: run `ruff check --fix <changed files> && ruff format <changed files>` before each commit. Local pytest is noisy on Py3.14; run only the targeted test files — CI (Py3.12) is the gate.
- Cockpit: `npx vitest run <spec>` for tests. Do not add eager routes (initial-bundle budget hard-fails at 2.75MB). Keep component styles tiny (36kB warn per component). i18n keys go in **both** `en.json` and `de-DE.json`.
- Channel opt-in defaults (spec): `email → 'opted_in'`, `whatsapp → 'pending'`. Changing an address value resets opt-in to the channel default and `last_inbound_at` to NULL.
- Address normalization (spec): emails lowercased; whatsapp stripped of `[\s\-().]` and matched against `^\+[1-9]\d{6,14}$`.
- asyncpg gotcha (house): JSON/JSONB and `json_agg` columns come back as **strings** — `json.loads` them in the DB method, never return raw.

---

### Task 1: Migration 0072 — tables + backfill

**Files:**
- Create: `orchestrator/database/migrations/app/0072_contacts_normalize.sql`

**Interfaces:**
- Produces: tables `contacts`, `contact_addresses`, `project_contacts` exactly as below — every later task depends on these names/columns. `external_contacts` remains untouched.

- [ ] **Step 1: Write the migration**

```sql
-- migration:     0072_contacts_normalize.sql
-- description:   Cross-channel contacts registry, Phase 1 of
--                docs/features/contacts_registry.md. Creates the normalized
--                contacts / contact_addresses / project_contacts tables and
--                backfills them from external_contacts. The legacy table is
--                deliberately left in place and untouched; a later migration
--                (0073, next release) re-runs the idempotent backfill sweep
--                and drops it. Uniqueness is per OWNER, not global: two users
--                may each register anna@acme.de. Idempotency anchor is the
--                (owner_user_id, channel, address) key — contacts itself has
--                no natural unique key by design (duplicate names are legal).

CREATE TABLE IF NOT EXISTS contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_contacts_owner ON contacts(owner_user_id);

CREATE TABLE IF NOT EXISTS contact_addresses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK (channel IN ('email', 'whatsapp')),
    address TEXT NOT NULL,
    is_primary BOOLEAN NOT NULL DEFAULT false,
    opt_in_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (opt_in_status IN ('pending', 'opted_in', 'opted_out')),
    last_inbound_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (owner_user_id, channel, address)
);
CREATE INDEX IF NOT EXISTS idx_contact_addresses_contact
    ON contact_addresses(contact_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_contact_primary_per_channel
    ON contact_addresses(contact_id, channel) WHERE is_primary;

CREATE TABLE IF NOT EXISTS project_contacts (
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    added_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (project_id, contact_id)
);
CREATE INDEX IF NOT EXISTS idx_project_contacts_contact
    ON project_contacts(contact_id);

-- Backfill. Row-by-row plpgsql (not set-based): the join-back from inserted
-- contacts to source rows is ambiguous when two same-named contacts have
-- different emails, and we want RAISE WARNING for unresolvable-owner skips.
-- Iterating created_at ASC + overwriting display_name each hit implements
-- "most recently created source row wins" for name conflicts.
DO $$
DECLARE
    r RECORD;
    v_owner UUID;
    v_contact UUID;
BEGIN
    FOR r IN SELECT * FROM external_contacts ORDER BY created_at ASC LOOP
        SELECT COALESCE(
            r.added_by,
            (SELECT pm.user_id FROM project_members pm
              WHERE pm.project_id = r.project_id AND pm.role = 'owner'
              ORDER BY pm.added_at ASC LIMIT 1)
        ) INTO v_owner;
        IF v_owner IS NULL THEN
            RAISE WARNING 'contacts backfill: skipping external_contact % (no resolvable owner)', r.id;
            CONTINUE;
        END IF;
        SELECT ca.contact_id INTO v_contact FROM contact_addresses ca
         WHERE ca.owner_user_id = v_owner
           AND ca.channel = 'email'
           AND ca.address = LOWER(r.email);
        IF v_contact IS NULL THEN
            INSERT INTO contacts (owner_user_id, display_name, created_at)
                 VALUES (v_owner, r.display_name, r.created_at)
              RETURNING id INTO v_contact;
            INSERT INTO contact_addresses
                   (contact_id, owner_user_id, channel, address, is_primary, opt_in_status)
            VALUES (v_contact, v_owner, 'email', LOWER(r.email), true, 'opted_in');
        ELSE
            UPDATE contacts SET display_name = r.display_name, updated_at = NOW()
             WHERE id = v_contact;
        END IF;
        INSERT INTO project_contacts (project_id, contact_id, added_by, created_at)
             VALUES (r.project_id, v_contact, r.added_by, r.created_at)
        ON CONFLICT DO NOTHING;
    END LOOP;
END $$;
```

- [ ] **Step 2: Stand up a disposable Postgres and run the full chain**

```bash
podman run -d --rm --name contacts-pg -e POSTGRES_USER=srw -e POSTGRES_PASSWORD=t \
  -e POSTGRES_DB=srw -p 5433:5432 docker.io/library/postgres:16
sleep 4
python orchestrator/database/migrate.py \
  --database-url postgresql://srw:t@localhost:5433/srw \
  --dir orchestrator/database/migrations/app
```

Expected: all migrations up to 0072 apply, exit 0. (0072 backfills zero rows — table is empty; this proves the DDL.)

- [ ] **Step 3: Seed legacy fixtures covering every backfill rule, then re-apply 0072 twice**

```bash
podman exec -i contacts-pg psql -U srw -d srw <<'SQL'
INSERT INTO users (id, email) VALUES
  ('00000000-0000-0000-0000-00000000000a', 'owner-a@test'),
  ('00000000-0000-0000-0000-00000000000b', 'owner-b@test');
INSERT INTO projects (id, name) VALUES
  ('00000000-0000-0000-0000-0000000000f1', 'P1'),
  ('00000000-0000-0000-0000-0000000000f2', 'P2'),
  ('00000000-0000-0000-0000-0000000000f3', 'P3-orphan');
INSERT INTO project_members (project_id, user_id, role) VALUES
  ('00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-00000000000a', 'owner'),
  ('00000000-0000-0000-0000-0000000000f2', '00000000-0000-0000-0000-00000000000a', 'owner');
-- dup email across two projects (dedupe → 1 contact, 2 links); newest name wins
INSERT INTO external_contacts (project_id, display_name, email, added_by, created_at) VALUES
  ('00000000-0000-0000-0000-0000000000f1', 'Anna Old', 'ANNA@acme.de',
   '00000000-0000-0000-0000-00000000000a', NOW() - interval '2 days'),
  ('00000000-0000-0000-0000-0000000000f2', 'Anna Weber', 'anna@acme.de',
   '00000000-0000-0000-0000-00000000000a', NOW() - interval '1 day'),
-- added_by NULL → falls back to project owner member
  ('00000000-0000-0000-0000-0000000000f1', 'Markus', 'markus@acme.de', NULL, NOW()),
-- cross-tenant: owner-b holds the same email as owner-a
  ('00000000-0000-0000-0000-0000000000f1', 'Anna B', 'anna@acme.de',
   '00000000-0000-0000-0000-00000000000b', NOW()),
-- orphan: added_by NULL and project has no owner member → skipped with WARNING
  ('00000000-0000-0000-0000-0000000000f3', 'Ghost', 'ghost@acme.de', NULL, NOW());
SQL
# NOTE: if 0001's projects/users tables demand more NOT NULL columns, extend the
# INSERTs minimally (check \d users / \d projects) — fixture intent is above.
podman exec -i contacts-pg psql -U srw -d srw \
  < orchestrator/database/migrations/app/0072_contacts_normalize.sql
podman exec -i contacts-pg psql -U srw -d srw \
  < orchestrator/database/migrations/app/0072_contacts_normalize.sql
```

- [ ] **Step 4: Assert the backfill invariants**

```bash
podman exec -i contacts-pg psql -U srw -d srw <<'SQL'
SELECT COUNT(*) AS contacts FROM contacts;                       -- expect 3 (Anna/a, Markus/a, Anna B/b)
SELECT COUNT(*) AS addrs FROM contact_addresses;                 -- expect 3, all lowercased, opted_in, primary
SELECT COUNT(*) AS links FROM project_contacts;                  -- expect 4 (Anna×2, Markus×1, AnnaB×1)
SELECT display_name FROM contacts
 WHERE owner_user_id='00000000-0000-0000-0000-00000000000a'
   AND id IN (SELECT contact_id FROM contact_addresses WHERE address='anna@acme.de'
              AND owner_user_id='00000000-0000-0000-0000-00000000000a');  -- expect 'Anna Weber' (newest wins)
SELECT COUNT(*) FROM contacts WHERE display_name='Ghost';        -- expect 0 (skipped)
SQL
```

Expected: exactly the annotated counts — the double apply in Step 3 proves idempotency (same counts, no duplicates).

- [ ] **Step 5: Tear down and commit**

```bash
podman stop contacts-pg
git add orchestrator/database/migrations/app/0072_contacts_normalize.sql
git commit -m "$(cat <<'EOF'
feat(db): contacts registry schema + external_contacts backfill (0072)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: DB layer — contacts methods in postgres.py

**Files:**
- Modify: `orchestrator/database/postgres.py` (add a new `# Contacts registry` section next to the existing external-contacts methods — locate with `grep -n "async def add_external_contact" orchestrator/database/postgres.py`)
- Test: `tests/test_contacts_db.py` (integration, env-gated; mirrors Task 1's podman harness)

**Interfaces:**
- Consumes: Task 1 tables.
- Produces (exact signatures — Tasks 3/4/6 call these):
  - `list_contacts_for_user(user_id: str, project_id: str | None = None, channel: str | None = None, q: str | None = None) -> List[Dict]` — owned ∪ project-linked; nested `addresses` + `projects`
  - `get_contact(contact_id: str) -> Dict | None` — nested, includes `owner_user_id`
  - `create_contact(owner_user_id: str, display_name: str, notes: str | None = None) -> Dict`
  - `update_contact(contact_id: str, display_name: str | None = None, notes: str | None = None) -> Dict | None`
  - `delete_contact(contact_id: str) -> bool`
  - `add_contact_address(contact_id: str, owner_user_id: str, channel: str, address: str, is_primary: bool = False) -> Dict | None` — **None on duplicate** (unique key); first address on a channel is auto-primary
  - `update_contact_address(address_id: str, address: str | None = None, is_primary: bool | None = None) -> Dict | None` — address change resets opt-in; promotion demotes old primary atomically
  - `delete_contact_address(address_id: str) -> bool`
  - `get_contact_address(address_id: str) -> Dict | None` — includes `owner_user_id` (router ownership gate)
  - `link_contact_to_project(project_id: str, contact_id: str, added_by: str | None) -> bool`
  - `unlink_contact_from_project(project_id: str, contact_id: str) -> bool`
  - `get_project_contacts(project_id: str) -> List[Dict]` — nested addresses (replaces `get_external_contacts` for the project view)
  - `user_can_see_contact(user_id: str, contact_id: str) -> bool`
  - `resolve_contact(project_id: str, to: str, channel: str) -> Dict` — `{"status": "ok"|"not_found"|"ambiguous"|"no_channel_address", ...}`
  - Module constant `CONTACT_OPT_IN_DEFAULT = {"email": "opted_in", "whatsapp": "pending"}`

- [ ] **Step 1: Write the integration test (env-gated so CI without a DB skips it)**

```python
"""Contacts DB layer — integration tests against a disposable Postgres.

Run:  podman run -d --rm --name contacts-pg -e POSTGRES_USER=srw \
        -e POSTGRES_PASSWORD=t -e POSTGRES_DB=srw -p 5433:5432 \
        docker.io/library/postgres:16
      python orchestrator/database/migrate.py \
        --database-url postgresql://srw:t@localhost:5433/srw \
        --dir orchestrator/database/migrations/app
      CONTACTS_TEST_DSN=postgresql://srw:t@localhost:5433/srw \
        python -m pytest tests/test_contacts_db.py -v
"""
import os
import uuid

import pytest

DSN = os.getenv("CONTACTS_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="CONTACTS_TEST_DSN not set")


@pytest.fixture
async def db():
    # Instantiate the house DB class against DSN. Find the exact constructor:
    #   grep -n "^class \|def __init__\|async def connect" orchestrator/database/postgres.py | head
    # and mirror how orchestrator/database/migrate.py builds its pool if the
    # class wants a pool. Import style: tests run with orchestrator/ on
    # sys.path (see how tests/test_project_access.py imports `main`), so:
    from database.postgres import PostgresDatabase  # adjust class name per grep
    d = PostgresDatabase(DSN)
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
async def seeded(db):
    """Two users, one shared project; returns ids."""
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    p = str(uuid.uuid4())
    async with db.acquire() as conn:
        await conn.execute("INSERT INTO users (id, email) VALUES ($1, $2), ($3, $4)",
                           a, f"{a}@t", b, f"{b}@t")
        await conn.execute("INSERT INTO projects (id, name) VALUES ($1, 'T')", p)
        await conn.execute(
            "INSERT INTO project_members (project_id, user_id, role) VALUES ($1, $2, 'owner'), ($1, $3, 'editor')",
            p, a, b)
    return {"a": a, "b": b, "p": p}


async def test_cross_tenant_same_address(db, seeded):
    """THE test not to skip (spec): two owners each hold anna@acme.de."""
    ca = await db.create_contact(seeded["a"], "Anna A")
    cb = await db.create_contact(seeded["b"], "Anna B")
    assert await db.add_contact_address(ca["id"], seeded["a"], "email", "anna@acme.de")
    assert await db.add_contact_address(cb["id"], seeded["b"], "email", "anna@acme.de")
    # same owner + same address → duplicate → None
    assert await db.add_contact_address(ca["id"], seeded["a"], "email", "anna@acme.de") is None


async def test_resolver_statuses(db, seeded):
    c = await db.create_contact(seeded["a"], "Priya Nair")
    await db.add_contact_address(c["id"], seeded["a"], "email", "priya@x.de")
    await db.link_contact_to_project(seeded["p"], c["id"], seeded["a"])
    ok = await db.resolve_contact(seeded["p"], "Priya Nair", "email")
    assert ok["status"] == "ok" and ok["address"] == "priya@x.de"
    by_addr = await db.resolve_contact(seeded["p"], "PRIYA@x.de", "email")
    assert by_addr["status"] == "ok"
    assert (await db.resolve_contact(seeded["p"], "Priya Nair", "whatsapp"))["status"] == "no_channel_address"
    assert (await db.resolve_contact(seeded["p"], "Nobody", "email"))["status"] == "not_found"
    c2 = await db.create_contact(seeded["b"], "Priya Nair")
    await db.add_contact_address(c2["id"], seeded["b"], "email", "priya2@x.de")
    await db.link_contact_to_project(seeded["p"], c2["id"], seeded["b"])
    amb = await db.resolve_contact(seeded["p"], "Priya Nair", "email")
    assert amb["status"] == "ambiguous" and len(amb["candidates"]) == 2


async def test_visibility_union_and_primary(db, seeded):
    c = await db.create_contact(seeded["a"], "Tom")
    await db.add_contact_address(c["id"], seeded["a"], "whatsapp", "+4917011111")
    first = (await db.get_contact(c["id"]))["addresses"][0]
    assert first["is_primary"] is True and first["opt_in_status"] == "pending"
    # b can't see it until linked into the shared project
    assert await db.user_can_see_contact(seeded["b"], c["id"]) is False
    await db.link_contact_to_project(seeded["p"], c["id"], seeded["a"])
    assert await db.user_can_see_contact(seeded["b"], c["id"]) is True
    assert any(x["id"] == c["id"] for x in await db.list_contacts_for_user(seeded["b"]))
    # promotion demotes; address edit resets opt-in
    second = await db.add_contact_address(c["id"], seeded["a"], "whatsapp", "+4917022222")
    await db.update_contact_address(second["id"], is_primary=True)
    rows = (await db.get_contact(c["id"]))["addresses"]
    assert [r["is_primary"] for r in sorted(rows, key=lambda r: r["address"])] == [False, True]
    async with db.acquire() as conn:  # simulate a prior opt-in
        await conn.execute("UPDATE contact_addresses SET opt_in_status='opted_in', last_inbound_at=NOW() WHERE id=$1", second["id"])
    edited = await db.update_contact_address(second["id"], address="+4917033333")
    assert edited["opt_in_status"] == "pending" and edited["last_inbound_at"] is None
```

- [ ] **Step 2: Run to verify it fails**

Run (with harness up per the docstring): `CONTACTS_TEST_DSN=postgresql://srw:t@localhost:5433/srw python -m pytest tests/test_contacts_db.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'create_contact'` (fixture adapted first per its comment).

- [ ] **Step 3: Implement the methods**

Add to `postgres.py` (match surrounding docstring/`acquire()` style). Core implementations — the rest are one-statement CRUD in the same shape:

```python
CONTACT_OPT_IN_DEFAULT = {"email": "opted_in", "whatsapp": "pending"}

_CONTACT_SELECT = """
    SELECT c.id, c.owner_user_id, c.display_name, c.notes, c.created_at, c.updated_at,
           COALESCE((SELECT json_agg(json_build_object(
               'id', ca.id, 'channel', ca.channel, 'address', ca.address,
               'is_primary', ca.is_primary, 'opt_in_status', ca.opt_in_status,
               'last_inbound_at', ca.last_inbound_at, 'created_at', ca.created_at)
               ORDER BY ca.created_at)
             FROM contact_addresses ca WHERE ca.contact_id = c.id), '[]') AS addresses,
           COALESCE((SELECT json_agg(json_build_object('id', p.id, 'name', p.name))
             FROM project_contacts pc JOIN projects p ON p.id = pc.project_id
            WHERE pc.contact_id = c.id), '[]') AS projects
    FROM contacts c
"""

def _contact_row(row) -> Dict[str, Any]:
    d = dict(row)
    # asyncpg returns json_agg as a string — decode (house gotcha).
    d["addresses"] = json.loads(d["addresses"]) if isinstance(d["addresses"], str) else d["addresses"]
    d["projects"] = json.loads(d["projects"]) if isinstance(d["projects"], str) else d["projects"]
    return d
```

```python
async def list_contacts_for_user(self, user_id, project_id=None, channel=None, q=None):
    conds = ["""(c.owner_user_id = $1 OR EXISTS (
        SELECT 1 FROM project_contacts pc
        JOIN project_members pm ON pm.project_id = pc.project_id AND pm.user_id = $1
        WHERE pc.contact_id = c.id))"""]
    args: list = [user_id]
    if project_id:
        args.append(project_id)
        conds.append(f"EXISTS (SELECT 1 FROM project_contacts pc2 WHERE pc2.contact_id = c.id AND pc2.project_id = ${len(args)})")
    if channel:
        args.append(channel)
        conds.append(f"EXISTS (SELECT 1 FROM contact_addresses ca2 WHERE ca2.contact_id = c.id AND ca2.channel = ${len(args)})")
    if q:
        args.append(f"%{q}%")
        conds.append(f"(c.display_name ILIKE ${len(args)} OR EXISTS (SELECT 1 FROM contact_addresses ca3 WHERE ca3.contact_id = c.id AND ca3.address ILIKE ${len(args)}))")
    sql = _CONTACT_SELECT + " WHERE " + " AND ".join(conds) + " ORDER BY c.display_name ASC"
    async with self.acquire() as conn:
        return [_contact_row(r) for r in await conn.fetch(sql, *args)]
```

```python
async def add_contact_address(self, contact_id, owner_user_id, channel, address, is_primary=False):
    async with self.acquire() as conn:
        async with conn.transaction():
            has_primary = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM contact_addresses WHERE contact_id=$1 AND channel=$2 AND is_primary)",
                contact_id, channel)
            make_primary = is_primary or not has_primary
            if is_primary and has_primary:
                await conn.execute(
                    "UPDATE contact_addresses SET is_primary=false WHERE contact_id=$1 AND channel=$2 AND is_primary",
                    contact_id, channel)
            row = await conn.fetchrow(
                """INSERT INTO contact_addresses
                       (contact_id, owner_user_id, channel, address, is_primary, opt_in_status)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   ON CONFLICT (owner_user_id, channel, address) DO NOTHING
                   RETURNING *""",
                contact_id, owner_user_id, channel, address, make_primary,
                CONTACT_OPT_IN_DEFAULT.get(channel, "pending"))
            return dict(row) if row else None
```

```python
async def update_contact_address(self, address_id, address=None, is_primary=None):
    async with self.acquire() as conn:
        async with conn.transaction():
            cur = await conn.fetchrow("SELECT * FROM contact_addresses WHERE id=$1", address_id)
            if cur is None:
                return None
            if is_primary is True and not cur["is_primary"]:
                await conn.execute(
                    "UPDATE contact_addresses SET is_primary=false WHERE contact_id=$1 AND channel=$2 AND is_primary",
                    cur["contact_id"], cur["channel"])
            sets, args = [], []
            if address is not None and address != cur["address"]:
                args.append(address); sets.append(f"address=${len(args)}")
                args.append(CONTACT_OPT_IN_DEFAULT.get(cur["channel"], "pending"))
                sets.append(f"opt_in_status=${len(args)}")
                sets.append("last_inbound_at=NULL")
            if is_primary is not None:
                args.append(is_primary); sets.append(f"is_primary=${len(args)}")
            if not sets:
                return dict(cur)
            args.append(address_id)
            row = await conn.fetchrow(
                f"UPDATE contact_addresses SET {', '.join(sets)} WHERE id=${len(args)} RETURNING *", *args)
            return dict(row) if row else None
```

```python
async def resolve_contact(self, project_id, to, channel):
    """Channel-aware recipient resolution among project-linked contacts.

    Statuses: ok {contact_id, display_name, address, channel} ·
    not_found {} · ambiguous {candidates: [{display_name, addresses}]} ·
    no_channel_address {display_name, channels}.
    """
    async with self.acquire() as conn:
        rows = await conn.fetch(
            """SELECT c.id, c.display_name, ca.id AS addr_id, ca.channel,
                      ca.address, ca.is_primary, ca.created_at
                 FROM contacts c
                 JOIN project_contacts pc ON pc.contact_id = c.id AND pc.project_id = $1
                 LEFT JOIN contact_addresses ca ON ca.contact_id = c.id
                WHERE LOWER(c.display_name) = LOWER($2)
                   OR c.id IN (SELECT contact_id FROM contact_addresses
                                WHERE LOWER(address) = LOWER($2))""",
            project_id, to)
    if not rows:
        return {"status": "not_found"}
    by_contact: dict = {}
    for r in rows:
        by_contact.setdefault(r["id"], {"display_name": r["display_name"], "addrs": []})
        if r["addr_id"]:
            by_contact[r["id"]]["addrs"].append(dict(r))
    if len(by_contact) > 1:
        return {"status": "ambiguous", "candidates": [
            {"display_name": v["display_name"],
             "addresses": [a["address"] for a in v["addrs"]]}
            for v in by_contact.values()]}
    cid, entry = next(iter(by_contact.items()))
    on_channel = [a for a in entry["addrs"] if a["channel"] == channel]
    if not on_channel:
        return {"status": "no_channel_address", "display_name": entry["display_name"],
                "channels": sorted({a["channel"] for a in entry["addrs"]})}
    on_channel.sort(key=lambda a: (not a["is_primary"], a["created_at"] and -a["created_at"].timestamp()))
    best = on_channel[0]
    return {"status": "ok", "contact_id": str(cid), "display_name": entry["display_name"],
            "address": best["address"], "channel": channel}
```

Remaining one-liners (implement in the obvious house shape): `get_contact` (`_CONTACT_SELECT + " WHERE c.id=$1"`), `create_contact` (INSERT RETURNING → `get_contact`; store `NULLIF($3, '')` for notes), `update_contact` (`SET display_name = COALESCE($2, display_name), notes = CASE WHEN $3 IS NULL THEN notes ELSE NULLIF($3, '') END, updated_at = NOW()` — so `None` = unchanged, `""` = clear, text = set; the Cockpit form sends `""` to clear), `delete_contact` (DELETE, rowcount bool), `delete_contact_address`, `get_contact_address` (SELECT *), `link_contact_to_project` (INSERT ON CONFLICT DO NOTHING, bool), `unlink_contact_from_project` (DELETE, bool), `get_project_contacts` (`_CONTACT_SELECT` + `JOIN project_contacts pc ON pc.contact_id=c.id AND pc.project_id=$1 ORDER BY c.display_name`), `user_can_see_contact` (EXISTS of the visibility union).

- [ ] **Step 4: Run tests to verify they pass**

Run: `CONTACTS_TEST_DSN=postgresql://srw:t@localhost:5433/srw python -m pytest tests/test_contacts_db.py -v`
Expected: 3 passed (skips cleanly when DSN unset: `python -m pytest tests/test_contacts_db.py -v` → skipped).

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix orchestrator/database/postgres.py tests/test_contacts_db.py && \
ruff format orchestrator/database/postgres.py tests/test_contacts_db.py
git add orchestrator/database/postgres.py tests/test_contacts_db.py
git commit -m "$(cat <<'EOF'
feat(db): contacts registry DB layer (CRUD, linking, channel-aware resolver)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Contacts router — new API surface

**Files:**
- Create: `orchestrator/routers/contacts.py`
- Modify: `orchestrator/main.py` (imports + two `include_router` lines — find the block with `grep -n "include_router" orchestrator/main.py | head -3`)
- Test: `tests/test_contacts_api.py`

**Interfaces:**
- Consumes: every Task 2 method, by exact name.
- Produces: endpoints per the spec table. `router` (prefix `/api/contacts`) and `project_router` (prefix `/api/projects`). Task 4 moves the legacy project endpoints onto `project_router`; Task 7's service calls these URLs.

- [ ] **Step 1: Write the failing tests**

Model on `tests/test_project_access.py` (read its `_patch_caller_and_db` helper first — same idiom, but patch `routers.contacts` targets):

```python
"""Contacts API — gates + behavior, mocked DB (house style)."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from routers import contacts as contacts_router

USER_A = {"id": "aaaaaaaa-0000-0000-0000-000000000000", "is_admin": False}
USER_B = {"id": "bbbbbbbb-0000-0000-0000-000000000000", "is_admin": False}


def _req():
    return MagicMock()


@pytest.fixture
def db(monkeypatch):
    d = MagicMock()
    for name in ("list_contacts_for_user", "get_contact", "create_contact",
                 "update_contact", "delete_contact", "add_contact_address",
                 "update_contact_address", "delete_contact_address",
                 "get_contact_address", "link_contact_to_project",
                 "unlink_contact_from_project", "get_project_contacts",
                 "user_can_see_contact", "resolve_contact"):
        setattr(d, name, AsyncMock())
    monkeypatch.setattr(contacts_router, "_get_db", lambda: d)
    return d


@pytest.fixture
def as_user_a(monkeypatch):
    monkeypatch.setattr(contacts_router, "require_approved_user",
                        AsyncMock(return_value=USER_A))


async def test_list_scopes_to_caller(db, as_user_a):
    db.list_contacts_for_user.return_value = []
    out = await contacts_router.list_contacts(_req(), project_id=None, channel=None, q=None)
    db.list_contacts_for_user.assert_awaited_once_with(
        USER_A["id"], project_id=None, channel=None, q=None)
    assert out == {"contacts": []}


async def test_patch_contact_owner_only(db, as_user_a):
    db.get_contact.return_value = {"id": "c1", "owner_user_id": USER_B["id"]}
    with pytest.raises(HTTPException) as e:
        await contacts_router.patch_contact(_req(), "c1",
            contacts_router.ContactPatch(display_name="X"))
    assert e.value.status_code == 403
    db.update_contact.assert_not_awaited()


async def test_add_address_validates_and_409s(db, as_user_a):
    db.get_contact.return_value = {"id": "c1", "owner_user_id": USER_A["id"]}
    with pytest.raises(HTTPException) as e:
        await contacts_router.add_address(_req(), "c1",
            contacts_router.ContactAddressIn(channel="whatsapp", address="not-a-number"))
    assert e.value.status_code == 400
    db.add_contact_address.return_value = None  # duplicate
    with pytest.raises(HTTPException) as e:
        await contacts_router.add_address(_req(), "c1",
            contacts_router.ContactAddressIn(channel="whatsapp", address="+49 170-555 (0)1"))
    assert e.value.status_code == 409
    # normalization stripped [\s\-().] before hitting the DB
    assert db.add_contact_address.await_args.args[3] == "+4917055501" or \
           db.add_contact_address.await_args.kwargs.get("address") == "+4917055501"


async def test_link_requires_editor_and_visibility(db, as_user_a, monkeypatch):
    gate = AsyncMock()
    monkeypatch.setattr(contacts_router, "require_project_member", gate)
    db.user_can_see_contact.return_value = False
    with pytest.raises(HTTPException) as e:
        await contacts_router.link_contact_to_project(_req(), "c1", "p1")
    assert e.value.status_code == 404
    gate.assert_awaited()  # editor gate ran before visibility


async def test_delete_contact_owner_only(db, as_user_a):
    db.get_contact.return_value = {"id": "c1", "owner_user_id": USER_A["id"],
                                   "projects": [{"id": "p1", "name": "P"}]}
    db.delete_contact.return_value = True
    out = await contacts_router.delete_contact(_req(), "c1")
    assert out == {"status": "deleted"}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_contacts_api.py -v`
Expected: FAIL — `ModuleNotFoundError: routers.contacts` (or import errors). If `routers` isn't importable bare, copy the import style the existing router tests use (`grep -rn "from routers" tests/ | head -3`).

- [ ] **Step 3: Implement the router**

`orchestrator/routers/contacts.py` — late-import `postgres_db` (idiom: `orchestrator/routers/sessions.py` `_get_db`); auth imports copied from how `main.py` imports them (`grep -n "require_project_member\|require_approved_user" orchestrator/main.py | head -4`):

```python
"""/api/contacts — cross-channel contact registry (docs/features/contacts_registry.md).

Owner mutates; project editors link/unlink; members read. Channel addresses
are normalized here (emails lowercased, whatsapp to E.164) so the DB only
ever sees canonical values. project_router carries the two kept
/api/projects/{id}/contacts endpoints (find-or-create-then-link semantics).
"""
from __future__ import annotations

import re
from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from security.auth import require_approved_user
from security.access import require_project_member  # adjust to main.py's import if it differs

router = APIRouter(prefix="/api/contacts", tags=["Contacts"])
project_router = APIRouter(prefix="/api/projects", tags=["Contacts"])

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def _get_db() -> Any:
    import main
    return main.postgres_db


def _normalize_address(channel: str, address: str) -> str:
    """Canonical form or 400. Spec: emails lowercased; whatsapp E.164."""
    address = address.strip()
    if channel == "email":
        if "@" not in address or "." not in address.split("@")[-1]:
            raise HTTPException(status_code=400, detail="Invalid email format")
        return address.lower()
    if channel == "whatsapp":
        cleaned = re.sub(r"[\s\-().]", "", address)
        if not _E164.match(cleaned):
            raise HTTPException(status_code=400,
                detail="Invalid WhatsApp number — expected E.164 like +4917012345678")
        return cleaned
    raise HTTPException(status_code=400, detail=f"Unknown channel '{channel}'")


class ContactAddressIn(BaseModel):
    channel: str
    address: str
    is_primary: bool = False


class ContactCreate(BaseModel):
    display_name: str
    notes: Optional[str] = None
    addresses: List[ContactAddressIn] = []


class ContactPatch(BaseModel):
    display_name: Optional[str] = None
    notes: Optional[str] = None


class AddressPatch(BaseModel):
    address: Optional[str] = None
    is_primary: Optional[bool] = None


async def _owned_contact(request: Request, contact_id: str) -> dict:
    db = _get_db()
    user = await require_approved_user(request, db)
    contact = await db.get_contact(contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    if str(contact["owner_user_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="Only the contact owner can modify it")
    return contact


@router.get("")
async def list_contacts(request: Request, project_id: Optional[str] = None,
                        channel: Optional[str] = None, q: Optional[str] = None) -> dict:
    db = _get_db()
    user = await require_approved_user(request, db)
    rows = await db.list_contacts_for_user(user["id"], project_id=project_id,
                                           channel=channel, q=q)
    return {"contacts": rows}


@router.post("")
async def create_contact(request: Request, body: ContactCreate) -> dict:
    db = _get_db()
    user = await require_approved_user(request, db)
    if not body.display_name.strip():
        raise HTTPException(status_code=400, detail="display_name is required")
    normalized = [(a.channel, _normalize_address(a.channel, a.address), a.is_primary)
                  for a in body.addresses]
    contact = await db.create_contact(user["id"], body.display_name.strip(), body.notes)
    for channel_, addr, prim in normalized:
        added = await db.add_contact_address(contact["id"], user["id"], channel_, addr, prim)
        if added is None:
            raise HTTPException(status_code=409, detail=(
                f"Address {addr} already belongs to one of your contacts — "
                "link that contact to the project instead"))
    return {"contact": await db.get_contact(contact["id"])}


@router.patch("/{contact_id}")
async def patch_contact(request: Request, contact_id: str, body: ContactPatch) -> dict:
    await _owned_contact(request, contact_id)
    updated = await _get_db().update_contact(contact_id, body.display_name, body.notes)
    return {"contact": updated}


@router.delete("/{contact_id}")
async def delete_contact(request: Request, contact_id: str) -> dict:
    await _owned_contact(request, contact_id)
    await _get_db().delete_contact(contact_id)
    return {"status": "deleted"}


@router.post("/{contact_id}/addresses")
async def add_address(request: Request, contact_id: str, body: ContactAddressIn) -> dict:
    contact = await _owned_contact(request, contact_id)
    addr = _normalize_address(body.channel, body.address)
    added = await _get_db().add_contact_address(
        contact["id"], contact["owner_user_id"], body.channel, addr, body.is_primary)
    if added is None:
        raise HTTPException(status_code=409, detail=(
            f"Address {addr} already belongs to one of your contacts — "
            "link that contact to the project instead"))
    return {"address": added}


@router.patch("/addresses/{address_id}")
async def patch_address(request: Request, address_id: str, body: AddressPatch) -> dict:
    db = _get_db()
    user = await require_approved_user(request, db)
    existing = await db.get_contact_address(address_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Address not found")
    if str(existing["owner_user_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="Only the contact owner can modify it")
    new_addr = None
    if body.address is not None:
        new_addr = _normalize_address(existing["channel"], body.address)
    updated = await db.update_contact_address(address_id, new_addr, body.is_primary)
    return {"address": updated}


@router.delete("/addresses/{address_id}")
async def delete_address(request: Request, address_id: str) -> dict:
    db = _get_db()
    user = await require_approved_user(request, db)
    existing = await db.get_contact_address(address_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Address not found")
    if str(existing["owner_user_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="Only the contact owner can modify it")
    await db.delete_contact_address(address_id)
    return {"status": "deleted"}


@router.post("/{contact_id}/projects/{project_id}")
async def link_contact_to_project(request: Request, contact_id: str, project_id: str) -> dict:
    db = _get_db()
    user = await require_project_member(request, db, project_id, min_role="editor")
    if not await db.user_can_see_contact(user["id"], contact_id):
        raise HTTPException(status_code=404, detail="Contact not found")
    await db.link_contact_to_project(project_id, contact_id, user["id"])
    return {"status": "linked"}


@router.delete("/{contact_id}/projects/{project_id}")
async def unlink_contact_from_project(request: Request, contact_id: str, project_id: str) -> dict:
    db = _get_db()
    await require_project_member(request, db, project_id, min_role="editor")
    removed = await db.unlink_contact_from_project(project_id, contact_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Contact not linked to this project")
    return {"status": "unlinked"}
```

(`require_project_member` returns the caller's user dict in house usage — verify with one existing call site and adjust `user["id"]` extraction if it returns something else.)

Register in `main.py` next to the existing `include_router` block:

```python
from routers.contacts import project_router as contacts_project_router
from routers.contacts import router as contacts_router
app.include_router(contacts_router)
app.include_router(contacts_project_router)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_contacts_api.py -v`
Expected: 5 passed.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix orchestrator/routers/contacts.py orchestrator/main.py tests/test_contacts_api.py && \
ruff format orchestrator/routers/contacts.py orchestrator/main.py tests/test_contacts_api.py
git add orchestrator/routers/contacts.py orchestrator/main.py tests/test_contacts_api.py
git commit -m "$(cat <<'EOF'
feat(api): contacts registry router (owner CRUD, project link/unlink)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Rewire send_agent_message; retire the legacy endpoints

**Files:**
- Modify: `orchestrator/main.py` — `send_agent_message` (find: `grep -n "Fallback: try external contacts" orchestrator/main.py`); delete `add_external_contact`, `list_external_contacts`, `delete_external_contact` endpoint functions + `ExternalContactCreate` model
- Modify: `orchestrator/routers/contacts.py` — add the two kept `/api/projects/{id}/contacts` endpoints to `project_router`
- Modify: `orchestrator/database/postgres.py` — delete `add_external_contact`, `get_external_contacts`, `delete_external_contact`, `resolve_external_contact` methods (table stays; only code retires)
- Test: `tests/test_contacts_api.py` (extend), `tests/test_project_access.py` (update)

**Interfaces:**
- Consumes: `resolve_contact` / `get_project_contacts` / `link_contact_to_project` / `create_contact` / `add_contact_address` / `list_contacts_for_user` (Task 2); `project_router` (Task 3).
- Produces: `GET /api/projects/{id}/contacts` (member) and `POST /api/projects/{id}/contacts` (editor, find-or-create-then-link) — Task 7 consumes the GET.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_contacts_api.py`:

```python
async def test_project_get_contacts_gated(db, monkeypatch):
    gate = AsyncMock(return_value=USER_A)
    monkeypatch.setattr(contacts_router, "require_project_member", gate)
    db.get_project_contacts.return_value = []
    out = await contacts_router.get_project_contacts(_req(), "p1")
    gate.assert_awaited()
    assert out == {"contacts": []}


async def test_project_post_find_or_create_then_link(db, monkeypatch):
    monkeypatch.setattr(contacts_router, "require_project_member",
                        AsyncMock(return_value=USER_A))
    # no address/name match among visible → create then link
    db.list_contacts_for_user.return_value = []
    db.create_contact.return_value = {"id": "c-new"}
    db.get_contact.return_value = {"id": "c-new", "owner_user_id": USER_A["id"]}
    db.add_contact_address.return_value = {"id": "a1"}
    out = await contacts_router.add_project_contact(_req(), "p1",
        contacts_router.ContactCreate(display_name="Anna",
            addresses=[contacts_router.ContactAddressIn(channel="email", address="Anna@X.de")]))
    db.link_contact_to_project.assert_awaited_once_with("p1", "c-new", USER_A["id"])
    assert out["contact"]["id"] == "c-new"
    # address match among visible → link existing, no create
    db.reset_mock()
    db.list_contacts_for_user.return_value = [
        {"id": "c-exist", "display_name": "Anna",
         "addresses": [{"channel": "email", "address": "anna@x.de"}]}]
    db.get_contact.return_value = {"id": "c-exist", "owner_user_id": USER_B["id"]}
    await contacts_router.add_project_contact(_req(), "p1",
        contacts_router.ContactCreate(display_name="ignored",
            addresses=[contacts_router.ContactAddressIn(channel="email", address="ANNA@x.de")]))
    db.create_contact.assert_not_awaited()
    db.link_contact_to_project.assert_awaited_once_with("p1", "c-exist", USER_A["id"])
```

In `tests/test_project_access.py`, update the two legacy-contact gate tests: the DELETE `/api/projects/{id}/contacts/{cid}` test is removed with the endpoint; POST/GET gate tests re-point at `routers.contacts` (`from routers.contacts import get_project_contacts, add_project_contact` and patch `routers.contacts.require_project_member` / `routers.contacts._get_db` instead of `main.*`). Also update the module docstring listing.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_contacts_api.py tests/test_project_access.py -v`
Expected: new tests FAIL (`get_project_contacts` not on module); legacy tests fail once endpoints move — fix them in the same pass.

- [ ] **Step 3: Implement**

Add to `orchestrator/routers/contacts.py`:

```python
@project_router.get("/{project_id}/contacts")
async def get_project_contacts(request: Request, project_id: str) -> dict:
    db = _get_db()
    await require_project_member(request, db, project_id)
    return {"contacts": await db.get_project_contacts(project_id)}


@project_router.post("/{project_id}/contacts")
async def add_project_contact(request: Request, project_id: str, body: ContactCreate) -> dict:
    """Find-or-create-then-link (spec): match by supplied address among
    caller-visible contacts, else by exact display_name, else create owned
    by caller; then link."""
    db = _get_db()
    user = await require_project_member(request, db, project_id, min_role="editor")
    normalized = [(a.channel, _normalize_address(a.channel, a.address), a.is_primary)
                  for a in body.addresses]
    visible = await db.list_contacts_for_user(user["id"])
    match = None
    for c in visible:
        for ch, addr, _ in normalized:
            if any(a["channel"] == ch and a["address"] == addr for a in c["addresses"]):
                match = c
                break
        if match:
            break
    if match is None:
        wanted = body.display_name.strip().lower()
        named = [c for c in visible if c["display_name"].strip().lower() == wanted]
        match = named[0] if named else None
    if match is None:
        created = await db.create_contact(user["id"], body.display_name.strip(), body.notes)
        for ch, addr, prim in normalized:
            if await db.add_contact_address(created["id"], user["id"], ch, addr, prim) is None:
                raise HTTPException(status_code=409, detail=(
                    f"Address {addr} already belongs to one of your contacts — "
                    "link that contact to the project instead"))
        match = {"id": created["id"]}
    await db.link_contact_to_project(project_id, match["id"], user["id"])
    return {"contact": await db.get_contact(match["id"])}
```

In `send_agent_message` (main.py), replace the whole `# Fallback: try external contacts` block (from `ext_contact = await postgres_db.resolve_external_contact(` through the `raise HTTPException` closing the not-found branch) with:

```python
                # Fallback: contacts registry (channel-aware; email path here)
                resolved = await postgres_db.resolve_contact(
                    project_id, request.to, "email"
                )
                if resolved["status"] == "ok":
                    recipient_email = resolved["address"]
                    recipient_name = resolved["display_name"]
                    # Contacts don't have a user_id — keep job owner's
                elif resolved["status"] == "no_channel_address":
                    raise HTTPException(status_code=404, detail=(
                        f"{resolved['display_name']} has no email address "
                        f"({', '.join(resolved['channels']) or 'no addresses'} only)."))
                elif resolved["status"] == "ambiguous":
                    cands = "; ".join(
                        f"{c['display_name']} <{', '.join(c['addresses'])}>"
                        for c in resolved["candidates"])
                    raise HTTPException(status_code=404, detail=(
                        f"Recipient '{request.to}' is ambiguous — specify an address. "
                        f"Candidates: {cands}"))
                else:
                    available = ", ".join(m.get("display_name", "?") for m in members)
                    contact_rows = await postgres_db.get_project_contacts(project_id)
                    if contact_rows:
                        names = ", ".join(c.get("display_name", "?") for c in contact_rows)
                        available += f" | Contacts: {names}"
                    raise HTTPException(status_code=404, detail=(
                        f"Recipient '{request.to}' not found among project members "
                        f"or contacts. Available: {available}"))
```

Then delete the three legacy endpoint functions + `ExternalContactCreate` from main.py and the four legacy DB methods from postgres.py. Confirm nothing else references them: `grep -rn "external_contact\|ExternalContactCreate" orchestrator/ src/ tests/ --include=*.py` → only migrations + `tests/test_contacts_db.py` docstring may remain.

- [ ] **Step 4: Run tests to verify green**

Run: `python -m pytest tests/test_contacts_api.py tests/test_project_access.py -v`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix orchestrator/main.py orchestrator/routers/contacts.py orchestrator/database/postgres.py tests/test_contacts_api.py tests/test_project_access.py && \
ruff format orchestrator/main.py orchestrator/routers/contacts.py orchestrator/database/postgres.py tests/test_contacts_api.py tests/test_project_access.py
git add -u orchestrator tests
git commit -m "$(cat <<'EOF'
feat(api): channel-aware recipient resolution; retire external_contacts code

send_agent_message now resolves via resolve_contact(project_id, to,
'email') with distinct no-channel-address and ambiguous errors. Legacy
/api/projects contacts endpoints move to the contacts router with
find-or-create-then-link semantics; external_contacts DB methods retire
(table drops in 0073, next release).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: contact_files — render + slug (pure functions)

**Files:**
- Create: `src/core/contact_files.py`
- Test: `tests/test_contact_files.py`

**Interfaces:**
- Consumes: nothing (stdlib only — orchestrator imports this module; keep it dependency-free per the orchestrator-image-deps gotcha).
- Produces: `contact_slug(display_name: str, taken: set[str]) -> str`; `render_contact_md(contact: dict) -> str`; `contacts_to_workspace_files(contacts: list[dict]) -> dict[str, str]` (keys `contacts/<slug>.md`). Task 6 calls `contacts_to_workspace_files` with Task 2's nested contact dicts.

- [ ] **Step 1: Write the failing tests**

```python
"""contacts/<slug>.md rendering — pure functions, no I/O."""
from src.core.contact_files import contact_slug, contacts_to_workspace_files, render_contact_md


def _anna():
    return {
        "display_name": "Anna Weber",
        "notes": "Head of Operations. Prefers short messages, CET.",
        "addresses": [
            {"channel": "email", "address": "anna@acme.de", "is_primary": True,
             "opt_in_status": "opted_in"},
            {"channel": "whatsapp", "address": "+4917055501", "is_primary": True,
             "opt_in_status": "pending"},
        ],
        "projects": [{"id": "p1", "name": "Acme Website"}],
    }


def test_slug_kebab_sanitize_collide():
    taken: set[str] = set()
    assert contact_slug("Anna Weber", taken) == "anna-weber"
    taken.add("anna-weber")
    assert contact_slug("Anna Weber", taken) == "anna-weber-2"
    assert contact_slug("Ümläut / Sla$h!", set()) == "umlaut-sla-h"
    assert contact_slug("", set()) == "contact"


def test_render_frontmatter_and_body():
    md = render_contact_md({**_anna(), "_slug": "anna-weber"})
    assert md.startswith("---\n")
    assert 'name: "anna-weber"' in md
    assert 'display_name: "Anna Weber"' in md
    assert '"channel": "whatsapp"' in md and '"opt_in": "pending"' in md
    # email is opted_in → no opt_in noise on that line
    assert md.count('"opt_in"') == 1
    assert md.rstrip().endswith("Prefers short messages, CET.")


def test_files_dict_paths_and_collisions():
    files = contacts_to_workspace_files([_anna(), {**_anna(), "notes": None}])
    assert set(files) == {"contacts/anna-weber.md", "contacts/anna-weber-2.md"}
    assert files["contacts/anna-weber-2.md"].rstrip().endswith("---")  # empty body ok
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_contact_files.py -v`
Expected: FAIL — `ModuleNotFoundError: src.core.contact_files`.

- [ ] **Step 3: Implement**

```python
"""Render project contacts as workspace files (docs/features/contacts_registry.md).

DB is the source of truth; these files are a read-only projection written at
job/session start (same lever as skills/). Frontmatter scalars are emitted via
json.dumps — JSON is valid YAML flow syntax, so this needs no yaml dependency
(this module is imported by the orchestrator image; keep it stdlib-only).
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Dict, List

CONTACTS_DIR = "contacts"


def contact_slug(display_name: str, taken: set) -> str:
    base = unicodedata.normalize("NFKD", display_name or "")
    base = base.encode("ascii", "ignore").decode("ascii").lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-") or "contact"
    slug, n = base, 1
    while slug in taken:
        n += 1
        slug = f"{base}-{n}"
    return slug


def render_contact_md(contact: Dict[str, Any]) -> str:
    lines = ["---", f"name: {json.dumps(contact['_slug'])}",
             f"display_name: {json.dumps(contact.get('display_name', ''))}"]
    addresses = contact.get("addresses") or []
    if addresses:
        lines.append("addresses:")
        for a in addresses:
            entry = {"channel": a.get("channel"), "address": a.get("address")}
            if a.get("is_primary"):
                entry["primary"] = True
            if a.get("opt_in_status") and a["opt_in_status"] != "opted_in":
                entry["opt_in"] = a["opt_in_status"]
            lines.append(f"  - {json.dumps(entry)}")
    projects = [p.get("name", "?") for p in (contact.get("projects") or [])]
    if projects:
        lines.append(f"projects: {json.dumps(projects)}")
    lines.append("---")
    body = (contact.get("notes") or "").strip()
    return "\n".join(lines) + ("\n\n" + body + "\n" if body else "\n")


def contacts_to_workspace_files(contacts: List[Dict[str, Any]]) -> Dict[str, str]:
    taken: set = set()
    out: Dict[str, str] = {}
    for c in contacts:
        slug = contact_slug(c.get("display_name", ""), taken)
        taken.add(slug)
        out[f"{CONTACTS_DIR}/{slug}.md"] = render_contact_md({**c, "_slug": slug})
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_contact_files.py -v`
Expected: 3 passed.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix src/core/contact_files.py tests/test_contact_files.py && \
ruff format src/core/contact_files.py tests/test_contact_files.py
git add src/core/contact_files.py tests/test_contact_files.py
git commit -m "$(cat <<'EOF'
feat(core): render contacts as workspace markdown files

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Materialization wiring — orchestrator gather → blob → both runtimes + gitignore floor

**Files:**
- Modify: `orchestrator/main.py` — add `_gather_project_contacts`, call it at both `_gather_in_scope_skills` call sites (`grep -n "_gather_in_scope_skills(" orchestrator/main.py`)
- Modify: `orchestrator/services/config_resolver.py` — `contacts` param + `blob["contacts"]` (beside the `skills` handling)
- Modify: `src/core/loader.py` — `_resolved_contacts` beside `_resolved_skills` (find: `grep -n '_resolved_skills..* = resolved' src/core/loader.py`)
- Modify: `src/agent.py` — extend `_deploy_instruction_files` (after the skills loop)
- Modify: `src/api/persistent_session.py` — extend `_deploy_catalog_skill_files` (after the skills loop; contacts **overwrite**, unlike add-only skills)
- Modify: `orchestrator/services/job_provisioning.py` — `"contacts/",` line in `_LOOP_MAIN_GITIGNORE`
- Test: `tests/test_contacts_materialization.py`

**Interfaces:**
- Consumes: `contacts_to_workspace_files` (Task 5), `get_project_contacts` (Task 2).
- Produces: blob key `"contacts"` = `{"files": {"contacts/<slug>.md": str}}`; agent-side `config.extra["_resolved_contacts"]`. Env gate `CONTACTS_MATERIALIZE_ENABLED` (default on).

- [ ] **Step 1: Write the failing tests**

```python
"""Contacts materialization — gather gate + gitignore floor + write loop."""
import os
from unittest.mock import AsyncMock, patch

import pytest


async def test_gather_returns_files_dict():
    import main
    db = AsyncMock()
    db.get_project_contacts.return_value = [
        {"display_name": "Anna Weber", "notes": "CET.",
         "addresses": [{"channel": "email", "address": "anna@x.de",
                        "is_primary": True, "opt_in_status": "opted_in"}],
         "projects": [{"id": "p1", "name": "P"}]}]
    with patch.object(main, "postgres_db", db):
        out = await main._gather_project_contacts("u1", ["p1"])
    assert list(out["files"]) == ["contacts/anna-weber.md"]


async def test_gather_gates():
    import main
    with patch.dict(os.environ, {"CONTACTS_MATERIALIZE_ENABLED": "false"}):
        assert await main._gather_project_contacts("u1", ["p1"]) == {}
    assert await main._gather_project_contacts(None, ["p1"]) == {}
    assert await main._gather_project_contacts("u1", []) == {}


def test_loop_main_gitignore_floors_contacts():
    from services.job_provisioning import _LOOP_MAIN_GITIGNORE
    assert "contacts/" in _LOOP_MAIN_GITIGNORE.splitlines()


def test_resolver_blob_carries_contacts():
    from services.config_resolver import resolve_config
    import inspect
    assert "contacts" in inspect.signature(resolve_config).parameters
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_contacts_materialization.py -v`
Expected: FAIL — `_gather_project_contacts` missing, gitignore line missing, signature missing.

- [ ] **Step 3: Implement**

`main.py`, directly below `_gather_in_scope_skills`:

```python
def _is_contacts_materialize_enabled() -> bool:
    return os.getenv("CONTACTS_MATERIALIZE_ENABLED", "true").strip().lower() not in (
        "false", "0", "no", "off")


async def _gather_project_contacts(
    user_id: str | None, project_ids: list[str] | None = None
) -> dict[str, Any]:
    """Resolved-blob contacts payload: contacts/<slug>.md files for every
    contact linked to the job's project(s). Read-only projection — DB stays
    truth; sends authorize server-side regardless. {} when gated off.
    Failures degrade discovery, never job start."""
    from src.core.contact_files import contacts_to_workspace_files

    if not _is_contacts_materialize_enabled() or not user_id or not project_ids:
        return {}
    try:
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for pid in project_ids:
            for c in await postgres_db.get_project_contacts(str(pid)):
                if str(c["id"]) not in seen:
                    seen.add(str(c["id"]))
                    rows.append(c)
        if not rows:
            return {}
        return {"files": contacts_to_workspace_files(rows)}
    except Exception:
        logger.exception("contacts materialization gather failed (non-fatal)")
        return {}
```

At **each** of the two `_skills_payload = await _gather_in_scope_skills(...)` sites, add the sibling call `_contacts_payload = await _gather_project_contacts(<same user_id>, <same project_ids>)` and pass `contacts=_contacts_payload` wherever that site passes `skills=_skills_payload` into `resolve_config`.

`config_resolver.py` — mirror the `skills` lines exactly:

```python
    contacts: Optional[dict] = None,   # ← parameter, beside skills
```
```python
    if contacts:
        blob["contacts"] = contacts
```

`src/core/loader.py`, beside the `_resolved_skills` assignment:

```python
    config.extra["_resolved_contacts"] = resolved.get("contacts") or {}
```

`src/agent.py`, end of `_deploy_instruction_files` (after the skills loop, same write idiom):

```python
        # Contacts projection (docs/features/contacts_registry.md): read-only
        # discovery files; DB is truth, sends authorize server-side. Same
        # write path as skills; regenerated (overwritten) every deploy.
        contacts_files = self.config.extra.get("_resolved_contacts", {}).get("files", {})
        for ws_path, content in contacts_files.items():
            parent_dir = str(Path(ws_path).parent)
            if parent_dir and parent_dir != ".":
                self._workspace_manager.backend.mkdir(parent_dir)
            self._workspace_manager.write_file(ws_path, content)
            logger.debug(f"Deployed contact file to workspace: {ws_path}")
```

`src/api/persistent_session.py`, end of `_deploy_catalog_skill_files`: same loop but via that method's `self.workspace_manager` — **no** `exists()` skip (contacts overwrite; skills' add-only semantics don't apply).

`job_provisioning.py`: add `"contacts/",` on the line after `"skills/",` in `_LOOP_MAIN_GITIGNORE`, and extend that constant's comment with: `contacts/ is the materialized contact projection — names, emails, phone numbers; leaking it onto a loop's main is the skills/ leak with PII.`

- [ ] **Step 4: Run tests to verify green**

Run: `python -m pytest tests/test_contacts_materialization.py tests/test_contact_files.py -v`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
ruff check --fix orchestrator/main.py orchestrator/services/config_resolver.py src/core/loader.py src/agent.py src/api/persistent_session.py orchestrator/services/job_provisioning.py tests/test_contacts_materialization.py && \
ruff format orchestrator/main.py orchestrator/services/config_resolver.py src/core/loader.py src/agent.py src/api/persistent_session.py orchestrator/services/job_provisioning.py tests/test_contacts_materialization.py
git add -u orchestrator src && git add tests/test_contacts_materialization.py
git commit -m "$(cat <<'EOF'
feat(agent): materialize project contacts into the workspace

Orchestrator gathers contacts/<slug>.md files into the resolved-config
blob (skills lever); worker + session runtimes write them; contacts/
joins the loop-main gitignore floor (PII must not reach project repos).
Gated by CONTACTS_MATERIALIZE_ENABLED (default on); failures are
non-fatal.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Cockpit — types + ContactsService

**Files:**
- Modify: `cockpit/src/app/core/models/api.model.ts` (**append only** — file is dirty from another session)
- Create: `cockpit/src/app/core/services/contacts.service.ts`
- Test: `cockpit/src/app/core/services/contacts.service.spec.ts`

**Interfaces:**
- Consumes: Task 3/4 endpoints.
- Produces (Task 8 imports these): `ContactAddress`, `Contact`, `ContactProjectRef` interfaces; `ContactsService` with `list(filters?) / create(body) / update(id, patch) / remove(id) / addAddress(contactId, body) / patchAddress(addressId, patch) / removeAddress(addressId) / link(contactId, projectId) / unlink(contactId, projectId)`.

- [ ] **Step 1: Write the failing spec**

```typescript
import {TestBed} from '@angular/core/testing';
import {provideHttpClient} from '@angular/common/http';
import {HttpTestingController, provideHttpClientTesting} from '@angular/common/http/testing';
import {describe, expect, it, beforeEach, afterEach} from 'vitest';

import {ContactsService} from './contacts.service';
import {environment} from '../environment';

describe('ContactsService', () => {
  let service: ContactsService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(ContactsService);
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => http.verify());

  it('lists with filters', () => {
    service.list({q: 'anna', channel: 'whatsapp'}).subscribe();
    const req = http.expectOne(
      r => r.url === `${environment.apiUrl}/contacts`
        && r.params.get('q') === 'anna' && r.params.get('channel') === 'whatsapp');
    expect(req.request.method).toBe('GET');
    req.flush({contacts: []});
  });

  it('links a contact to a project', () => {
    service.link('c1', 'p1').subscribe();
    const req = http.expectOne(`${environment.apiUrl}/contacts/c1/projects/p1`);
    expect(req.request.method).toBe('POST');
    req.flush({status: 'linked'});
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd cockpit && npx vitest run src/app/core/services/contacts.service.spec.ts`
Expected: FAIL — cannot resolve `./contacts.service`.

- [ ] **Step 3: Implement**

Append to `api.model.ts`:

```typescript
// --- Contacts registry (docs/features/contacts_registry.md) ---
export type ContactChannel = 'email' | 'whatsapp';
export type ContactOptIn = 'pending' | 'opted_in' | 'opted_out';

export interface ContactAddress {
  id: string;
  channel: ContactChannel;
  address: string;
  is_primary: boolean;
  opt_in_status: ContactOptIn;
  last_inbound_at: string | null;
  created_at: string;
}

export interface ContactProjectRef {
  id: string;
  name: string;
}

export interface Contact {
  id: string;
  owner_user_id: string;
  display_name: string;
  notes: string | null;
  addresses: ContactAddress[];
  projects: ContactProjectRef[];
  created_at: string;
  updated_at: string;
}
```

`contacts.service.ts` (ApiKeysService idiom: `inject(HttpClient)` + `environment.apiUrl`):

```typescript
import {Injectable, inject} from '@angular/core';
import {HttpClient, HttpParams} from '@angular/common/http';
import {Observable, map} from 'rxjs';

import {Contact, ContactAddress} from '../models/api.model';
import {environment} from '../environment';

export interface ContactAddressIn {
  channel: string;
  address: string;
  is_primary?: boolean;
}

export interface ContactCreateBody {
  display_name: string;
  notes?: string | null;
  addresses?: ContactAddressIn[];
}

@Injectable({providedIn: 'root'})
export class ContactsService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = `${environment.apiUrl}/contacts`;

  list(filters: {project_id?: string; channel?: string; q?: string} = {}): Observable<Contact[]> {
    let params = new HttpParams();
    for (const [k, v] of Object.entries(filters)) {
      if (v) params = params.set(k, v);
    }
    return this.http
      .get<{contacts: Contact[]}>(this.baseUrl, {params})
      .pipe(map(r => r.contacts));
  }

  create(body: ContactCreateBody): Observable<Contact> {
    return this.http.post<{contact: Contact}>(this.baseUrl, body).pipe(map(r => r.contact));
  }

  update(id: string, patch: {display_name?: string; notes?: string | null}): Observable<Contact> {
    return this.http.patch<{contact: Contact}>(`${this.baseUrl}/${id}`, patch).pipe(map(r => r.contact));
  }

  remove(id: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl}/${id}`);
  }

  addAddress(contactId: string, body: ContactAddressIn): Observable<ContactAddress> {
    return this.http
      .post<{address: ContactAddress}>(`${this.baseUrl}/${contactId}/addresses`, body)
      .pipe(map(r => r.address));
  }

  patchAddress(addressId: string, patch: {address?: string; is_primary?: boolean}): Observable<ContactAddress> {
    return this.http
      .patch<{address: ContactAddress}>(`${this.baseUrl}/addresses/${addressId}`, patch)
      .pipe(map(r => r.address));
  }

  removeAddress(addressId: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl}/addresses/${addressId}`);
  }

  link(contactId: string, projectId: string): Observable<void> {
    return this.http.post<void>(`${this.baseUrl}/${contactId}/projects/${projectId}`, {});
  }

  unlink(contactId: string, projectId: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl}/${contactId}/projects/${projectId}`);
  }
}
```

(If `environment.apiUrl` already ends with `/api`, the list URL is `/api/contacts` — verify against how ApiKeysService builds `/api/api-keys` and match it.)

- [ ] **Step 4: Run to verify green**

Run: `cd cockpit && npx vitest run src/app/core/services/contacts.service.spec.ts`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add cockpit/src/app/core/models/api.model.ts cockpit/src/app/core/services/contacts.service.ts cockpit/src/app/core/services/contacts.service.spec.ts
git commit -m "$(cat <<'EOF'
feat(cockpit): contact types + ContactsService

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Cockpit — /contacts page (C1 layout), route, sidebar, i18n

**Files:**
- Create: `cockpit/src/app/views/contacts/contacts-page.component.ts`
- Create: `cockpit/src/app/views/contacts/contact-list.component.ts`
- Create: `cockpit/src/app/views/contacts/contact-form.component.ts`
- Modify: `cockpit/src/app/app.routes.ts` (lazy route after `datasources`)
- Modify: `cockpit/src/app/shell/sidebar/sidebar.component.ts` (nav entry after the datasources anchor)
- Modify: `cockpit/src/assets/i18n/en.json`, `cockpit/src/assets/i18n/de-DE.json`
- Test: `cockpit/src/app/views/contacts/contact-list.component.spec.ts`

**Interfaces:**
- Consumes: `ContactsService`, `Contact`/`ContactAddress` types (Task 7); `AppConfirmNameDialogComponent` (`title`/`message`/`requiredName`/`confirmLabel` inputs); existing projects listing (locate: `grep -rn "api/projects" cockpit/src/app/core/services/*.ts | head -3` — use that service's list method; fallback: `http.get<{projects:{id,name}[]}>(`${environment.apiUrl}/projects`)`).
- Produces: user-facing page; nothing downstream.

- [ ] **Step 1: Write the failing list spec**

Model the harness on `datasource-list.component.spec.ts` (signals + `runInInjectionContext`; mock services with `of()`):

```typescript
import {Injector, runInInjectionContext} from '@angular/core';
import {of} from 'rxjs';
import {describe, expect, it, vi} from 'vitest';

import {Contact} from '../../core/models/api.model';
import {ContactListComponent} from './contact-list.component';

function anna(overrides: Partial<Contact> = {}): Contact {
  return {
    id: 'c1', owner_user_id: 'u1', display_name: 'Anna Weber', notes: 'CET.',
    addresses: [
      {id: 'a1', channel: 'email', address: 'anna@acme.de', is_primary: true,
       opt_in_status: 'opted_in', last_inbound_at: null, created_at: ''},
      {id: 'a2', channel: 'whatsapp', address: '+4917055501', is_primary: true,
       opt_in_status: 'pending', last_inbound_at: null, created_at: ''},
    ],
    projects: [{id: 'p1', name: 'Acme Website'}, {id: 'p2', name: 'Q3'}],
    created_at: '', updated_at: '', ...overrides,
  };
}

describe('ContactListComponent', () => {
  function make(): ContactListComponent {
    const injector = Injector.create({providers: []});
    return runInInjectionContext(injector, () => new ContactListComponent());
  }

  it('annotates a chip when its primary address is not opted in', () => {
    const c = make();
    expect(c.chipLabel(anna(), 'whatsapp')).toBe('whatsapp·pending');
    expect(c.chipLabel(anna(), 'email')).toBe('email');
  });

  it('expansion is per-row and read-only state only', () => {
    const c = make();
    expect(c.isExpanded('c1')).toBe(false);
    c.toggle('c1');
    c.toggle('c2');
    expect(c.isExpanded('c1')).toBe(true);
    expect(c.isExpanded('c2')).toBe(true);  // several rows open at once (C1)
    c.toggle('c1');
    expect(c.isExpanded('c1')).toBe(false);
  });

  it('channelsOf lists channels present on the contact', () => {
    const c = make();
    expect(c.channelsOf(anna())).toEqual(['email', 'whatsapp']);
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd cockpit && npx vitest run src/app/views/contacts/contact-list.component.spec.ts`
Expected: FAIL — cannot resolve `./contact-list.component`.

- [ ] **Step 3: Implement the three components**

`contact-list.component.ts` — presentational; inputs/outputs only, expansion state local:

```typescript
import {ChangeDetectionStrategy, Component, input, output, signal} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';

import {Contact, ContactChannel} from '../../core/models/api.model';

@Component({
  selector: 'app-contact-list',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TranslocoPipe],
  template: `
    @for (contact of contacts(); track contact.id) {
      <div class="contact-row" (click)="toggle(contact.id)">
        <span class="caret">{{ isExpanded(contact.id) ? '▾' : '▸' }}</span>
        <span class="name">{{ contact.display_name }}</span>
        @for (ch of channelsOf(contact); track ch) {
          <span class="chip" [class.pending]="chipLabel(contact, ch) !== ch">
            {{ chipLabel(contact, ch) }}
          </span>
        }
        <span class="proj-count">{{ contact.projects.length }} {{ 'contacts.projectsShort' | transloco }}</span>
      </div>
      @if (isExpanded(contact.id)) {
        <div class="contact-detail">
          @for (a of contact.addresses; track a.id) {
            <div class="addr">
              <span class="chip">{{ a.channel }}</span>
              <span>{{ a.address }}</span>
              @if (a.is_primary) { <span class="tag">{{ 'contacts.primary' | transloco }}</span> }
              @if (a.opt_in_status !== 'opted_in') {
                <span class="tag pending">{{ 'contacts.optIn.' + a.opt_in_status | transloco }}</span>
              }
            </div>
          }
          @if (!contact.addresses.length) {
            <div class="addr muted">{{ 'contacts.noAddresses' | transloco }}</div>
          }
          <div class="projects">
            @for (p of contact.projects; track p.id) { <span class="chip">{{ p.name }}</span> }
          </div>
          @if (contact.notes) { <p class="notes">{{ contact.notes }}</p> }
          <div class="actions">
            <button (click)="edit.emit(contact); $event.stopPropagation()">{{ 'common.edit' | transloco }}</button>
            <button (click)="remove.emit(contact); $event.stopPropagation()">{{ 'common.delete' | transloco }}</button>
          </div>
        </div>
      }
    }
    @if (!contacts().length) { <p class="muted">{{ 'contacts.empty' | transloco }}</p> }
  `,
  styles: [`
    .contact-row { display: flex; align-items: center; gap: .5rem; padding: .5rem .75rem;
      border-bottom: 1px solid var(--border-color, rgba(128,128,128,.25)); cursor: pointer; }
    .name { flex: 1; font-weight: 600; }
    .chip { border: 1px solid var(--border-color, rgba(128,128,128,.4)); border-radius: 999px;
      padding: 0 .5rem; font-size: .75rem; }
    .chip.pending, .tag.pending { border-color: var(--warning-color, #e6963c); }
    .contact-detail { padding: .5rem .75rem .75rem 2rem;
      border-bottom: 1px solid var(--border-color, rgba(128,128,128,.25)); }
    .addr { display: flex; gap: .5rem; align-items: center; margin: .25rem 0; }
    .muted { opacity: .6; }
    .actions { display: flex; gap: .5rem; margin-top: .5rem; }
  `],
})
export class ContactListComponent {
  readonly contacts = input<Contact[]>([]);
  readonly edit = output<Contact>();
  readonly remove = output<Contact>();

  private readonly expanded = signal<Set<string>>(new Set());

  isExpanded(id: string): boolean {
    return this.expanded().has(id);
  }

  toggle(id: string): void {
    const next = new Set(this.expanded());
    next.has(id) ? next.delete(id) : next.add(id);
    this.expanded.set(next);
  }

  channelsOf(contact: Contact): string[] {
    return [...new Set(contact.addresses.map(a => a.channel))].sort();
  }

  /** Chip carries opt-in state when the channel's primary isn't opted_in (spec). */
  chipLabel(contact: Contact, channel: ContactChannel | string): string {
    const primary = contact.addresses.find(a => a.channel === channel && a.is_primary)
      ?? contact.addresses.find(a => a.channel === channel);
    if (!primary || primary.opt_in_status === 'opted_in') return channel;
    return `${channel}·${primary.opt_in_status === 'pending' ? 'pending' : 'opted out'}`;
  }
}
```

`contact-form.component.ts` — dumb buffered editor; page orchestrates API diffs:

```typescript
import {ChangeDetectionStrategy, Component, computed, input, output, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {TranslocoPipe} from '@jsverse/transloco';

import {Contact, ContactProjectRef} from '../../core/models/api.model';

export interface ContactFormResult {
  display_name: string;
  /** Trimmed; "" means "clear notes" (backend maps "" → NULL). */
  notes: string;
  addresses: {id?: string; channel: string; address: string; is_primary: boolean}[];
  projectIds: string[];
}

@Component({
  selector: 'app-contact-form',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, TranslocoPipe],
  template: `
    <div class="form-panel">
      <h3>{{ (contact() ? 'contacts.form.editTitle' : 'contacts.form.newTitle') | transloco }}</h3>
      <input [(ngModel)]="name" [placeholder]="'contacts.form.name' | transloco" />
      <textarea [(ngModel)]="notes" rows="3"
        [placeholder]="'contacts.form.notes' | transloco"></textarea>
      <div class="label">{{ 'contacts.form.addresses' | transloco }}</div>
      @for (row of rows(); track $index; let i = $index) {
        <div class="addr-row">
          <select [ngModel]="row.channel" (ngModelChange)="patchRow(i, {channel: $event})">
            <option value="email">email</option>
            <option value="whatsapp">whatsapp</option>
          </select>
          <input [ngModel]="row.address" (ngModelChange)="patchRow(i, {address: $event})"
            [placeholder]="row.channel === 'whatsapp' ? '+4917012345678' : 'anna@acme.de'" />
          <label><input type="checkbox" [ngModel]="row.is_primary"
            (ngModelChange)="patchRow(i, {is_primary: $event})" />{{ 'contacts.primary' | transloco }}</label>
          <button (click)="dropRow(i)">✕</button>
        </div>
      }
      <button (click)="addRow()">{{ 'contacts.form.addAddress' | transloco }}</button>
      <div class="label">{{ 'contacts.form.projects' | transloco }}</div>
      @for (p of projects(); track p.id) {
        <label class="proj">
          <input type="checkbox" [ngModel]="selectedProjects().has(p.id)"
            (ngModelChange)="toggleProject(p.id)" /> {{ p.name }}
        </label>
      }
      <div class="actions">
        <button (click)="cancelled.emit()">{{ 'common.cancel' | transloco }}</button>
        <button [disabled]="!valid()" (click)="submit()">{{ 'common.save' | transloco }}</button>
      </div>
    </div>
  `,
  styles: [`
    .form-panel { border: 1px solid var(--border-color, rgba(128,128,128,.4));
      border-radius: 8px; padding: 1rem; margin: .75rem 0; display: flex;
      flex-direction: column; gap: .5rem; }
    .addr-row { display: flex; gap: .5rem; align-items: center; }
    .label { font-size: .75rem; text-transform: uppercase; opacity: .7; margin-top: .5rem; }
    .actions { display: flex; justify-content: flex-end; gap: .5rem; }
  `],
})
export class ContactFormComponent {
  readonly contact = input<Contact | null>(null);
  readonly projects = input<ContactProjectRef[]>([]);
  readonly saved = output<ContactFormResult>();
  readonly cancelled = output<void>();

  name = '';
  notes = '';
  readonly rows = signal<{id?: string; channel: string; address: string; is_primary: boolean}[]>([]);
  readonly selectedProjects = signal<Set<string>>(new Set());
  readonly valid = computed(() =>
    this.name.trim().length > 0 || (this.contact()?.display_name ?? '').length > 0);

  ngOnInit(): void {
    const c = this.contact();
    if (c) {
      this.name = c.display_name;
      this.notes = c.notes ?? '';
      this.rows.set(c.addresses.map(a => ({
        id: a.id, channel: a.channel, address: a.address, is_primary: a.is_primary})));
      this.selectedProjects.set(new Set(c.projects.map(p => p.id)));
    }
  }

  addRow(): void {
    this.rows.update(r => [...r, {channel: 'email', address: '', is_primary: false}]);
  }
  dropRow(i: number): void {
    this.rows.update(r => r.filter((_, idx) => idx !== i));
  }
  patchRow(i: number, patch: Partial<{channel: string; address: string; is_primary: boolean}>): void {
    this.rows.update(r => r.map((row, idx) => (idx === i ? {...row, ...patch} : row)));
  }
  toggleProject(id: string): void {
    const next = new Set(this.selectedProjects());
    next.has(id) ? next.delete(id) : next.add(id);
    this.selectedProjects.set(next);
  }
  submit(): void {
    this.saved.emit({
      display_name: this.name.trim(),
      notes: this.notes.trim(),
      addresses: this.rows().filter(r => r.address.trim()),
      projectIds: [...this.selectedProjects()],
    });
  }
}
```

`contacts-page.component.ts` — shell; loads data, one form at a time (`showForm`/`editing`), diffs on save, delete dialog naming projects:

```typescript
import {ChangeDetectionStrategy, Component, OnInit, inject, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {forkJoin, of, switchMap} from 'rxjs';

import {Contact, ContactProjectRef} from '../../core/models/api.model';
import {ContactsService} from '../../core/services/contacts.service';
import {AppConfirmNameDialogComponent} from '../../ui/confirm-name-dialog';
import {ContactFormComponent, ContactFormResult} from './contact-form.component';
import {ContactListComponent} from './contact-list.component';

@Component({
  selector: 'app-contacts-page',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, TranslocoPipe, ContactListComponent, ContactFormComponent,
            AppConfirmNameDialogComponent],
  template: `
    <div class="page-header">
      <h2>{{ 'contacts.title' | transloco }}</h2>
      <button [disabled]="showForm()" (click)="openNew()">{{ 'contacts.new' | transloco }}</button>
    </div>
    <div class="filters">
      <input [ngModel]="q()" (ngModelChange)="q.set($event); reload()"
        [placeholder]="'contacts.search' | transloco" />
      <select [ngModel]="channel()" (ngModelChange)="channel.set($event); reload()">
        <option value="">{{ 'contacts.filter.allChannels' | transloco }}</option>
        <option value="email">email</option>
        <option value="whatsapp">whatsapp</option>
      </select>
      <select [ngModel]="projectId()" (ngModelChange)="projectId.set($event); reload()">
        <option value="">{{ 'contacts.filter.allProjects' | transloco }}</option>
        @for (p of projects(); track p.id) { <option [value]="p.id">{{ p.name }}</option> }
      </select>
    </div>
    @if (showForm()) {
      <app-contact-form [contact]="editing()" [projects]="projects()"
        (saved)="save($event)" (cancelled)="closeForm()" />
    }
    <app-contact-list [contacts]="contacts()" (edit)="openEdit($event)" (remove)="askDelete($event)" />
    @if (deleting(); as target) {
      <app-confirm-name-dialog
        [title]="'contacts.delete.title' | transloco"
        [message]="deleteMessage(target)"
        [requiredName]="target.display_name"
        [confirmLabel]="'common.delete' | transloco"
        (confirmed)="doDelete(target)"
        (dismissed)="deleting.set(null)" />
    }
  `,
  styles: [`
    .page-header { display: flex; justify-content: space-between; align-items: center; }
    .filters { display: flex; gap: .5rem; margin: .5rem 0 1rem; }
  `],
})
export class ContactsPageComponent implements OnInit {
  private readonly api = inject(ContactsService);
  private readonly http = inject(HttpClient);   // loadProjects fallback only
  private readonly transloco = inject(TranslocoService);

  readonly contacts = signal<Contact[]>([]);
  readonly projects = signal<ContactProjectRef[]>([]);
  readonly showForm = signal(false);
  readonly editing = signal<Contact | null>(null);
  readonly deleting = signal<Contact | null>(null);
  readonly q = signal('');
  readonly channel = signal('');
  readonly projectId = signal('');

  ngOnInit(): void {
    this.reload();
    this.loadProjects();
  }

  reload(): void {
    this.api.list({q: this.q() || undefined, channel: this.channel() || undefined,
                   project_id: this.projectId() || undefined})
      .subscribe(rows => this.contacts.set(rows));
  }

  private loadProjects(): void {
    // Prefer the house projects service if one exists
    // (grep -rn "api/projects" cockpit/src/app/core/services/*.ts | head -3).
    // Self-contained fallback (add `inject(HttpClient)` + `environment` imports):
    this.http.get<{projects: ContactProjectRef[]}>(`${environment.apiUrl}/projects`)
      .subscribe({next: r => this.projects.set(r.projects ?? []),
                  error: () => this.projects.set([])});
  }

  openNew(): void { this.editing.set(null); this.showForm.set(true); }
  openEdit(c: Contact): void { this.editing.set(c); this.showForm.set(true); }
  closeForm(): void { this.showForm.set(false); this.editing.set(null); }

  deleteMessage(c: Contact): string {
    const names = c.projects.map(p => p.name).join(', ');
    return this.transloco.translate('contacts.delete.message', {projects: names || '—'});
  }

  askDelete(c: Contact): void { this.deleting.set(c); }

  doDelete(c: Contact): void {
    this.api.remove(c.id).subscribe(() => { this.deleting.set(null); this.reload(); });
  }

  save(result: ContactFormResult): void {
    const existing = this.editing();
    const base$ = existing
      ? this.api.update(existing.id, {display_name: result.display_name, notes: result.notes})
      : this.api.create({display_name: result.display_name, notes: result.notes,
                         addresses: result.addresses});
    base$.pipe(switchMap(contact => {
      const ops = [];
      if (existing) {
        const beforeAddrs = existing.addresses;
        for (const a of beforeAddrs) {
          if (!result.addresses.some(r => r.id === a.id)) ops.push(this.api.removeAddress(a.id));
        }
        for (const r of result.addresses) {
          if (!r.id) { ops.push(this.api.addAddress(contact.id, r)); continue; }
          const before = beforeAddrs.find(a => a.id === r.id);
          if (before && (before.address !== r.address || before.is_primary !== r.is_primary)) {
            ops.push(this.api.patchAddress(r.id, {address: r.address, is_primary: r.is_primary}));
          }
        }
      }
      const beforeProjects = new Set((existing?.projects ?? []).map(p => p.id));
      for (const pid of result.projectIds) {
        if (!beforeProjects.has(pid)) ops.push(this.api.link(contact.id, pid));
      }
      for (const pid of beforeProjects) {
        if (!result.projectIds.includes(pid)) ops.push(this.api.unlink(contact.id, pid));
      }
      return ops.length ? forkJoin(ops) : of(null);
    })).subscribe({next: () => { this.closeForm(); this.reload(); },
                   error: () => this.reload()});
  }
}
```

(In `loadProjects`, prefer the real house projects service found by the grep — the bracket-access fallback works but is a last resort; replace it if a service exists.)

- [ ] **Step 4: Route + sidebar + i18n**

`app.routes.ts`, after the `datasources` line:

```typescript
  {
    path: 'contacts',
    loadComponent: () =>
      import('./views/contacts/contacts-page.component').then(m => m.ContactsPageComponent),
    canActivate: [authGuard],
  },
```

`sidebar.component.ts`, after the `/datasources` anchor (copy its exact classes):

```html
          <a
            class="nav-link"
            routerLink="/contacts"
            routerLinkActive="active"
          >
            <app-icon size="md" class="nav-icon">group</app-icon>
            {{ 'nav.contacts' | transloco }}
          </a>
```

`en.json` — add `"contacts": "Contacts"` inside the existing `nav` object, and a top-level block:

```json
"contacts": {
  "title": "Contacts",
  "new": "New contact",
  "search": "Search contacts…",
  "empty": "No contacts yet. Contacts are people your agents may reach — add one and link it to a project.",
  "projectsShort": "proj",
  "primary": "primary",
  "noAddresses": "No addresses",
  "optIn": {"pending": "opt-in pending", "opted_out": "opted out"},
  "filter": {"allChannels": "All channels", "allProjects": "All projects"},
  "form": {"newTitle": "New contact", "editTitle": "Edit contact", "name": "Display name",
           "notes": "Notes (visible to agents — briefings, preferences, context)",
           "addresses": "Addresses", "addAddress": "+ Add address", "projects": "Projects"},
  "delete": {"title": "Delete contact",
             "message": "This removes the contact from these projects: {{projects}}. Agents will no longer be able to message them."}
}
```

`de-DE.json` — same keys:

```json
"contacts": {
  "title": "Kontakte",
  "new": "Neuer Kontakt",
  "search": "Kontakte durchsuchen…",
  "empty": "Noch keine Kontakte. Kontakte sind Personen, die deine Agenten erreichen dürfen — lege einen an und verknüpfe ihn mit einem Projekt.",
  "projectsShort": "Proj.",
  "primary": "primär",
  "noAddresses": "Keine Adressen",
  "optIn": {"pending": "Opt-in ausstehend", "opted_out": "abgemeldet"},
  "filter": {"allChannels": "Alle Kanäle", "allProjects": "Alle Projekte"},
  "form": {"newTitle": "Neuer Kontakt", "editTitle": "Kontakt bearbeiten", "name": "Anzeigename",
           "notes": "Notizen (für Agenten sichtbar — Briefings, Präferenzen, Kontext)",
           "addresses": "Adressen", "addAddress": "+ Adresse hinzufügen", "projects": "Projekte"},
  "delete": {"title": "Kontakt löschen",
             "message": "Der Kontakt wird aus diesen Projekten entfernt: {{projects}}. Agenten können diese Person danach nicht mehr erreichen."}
}
```

Also add `nav.contacts` = `"Kontakte"` in de-DE's `nav` block. If `common.edit` / `common.delete` / `common.cancel` / `common.save` don't exist in the `common` block, add them in both languages (check first: `python3 -c "import json; print(json.load(open('cockpit/src/assets/i18n/en.json'))['common'])"`).

- [ ] **Step 5: Run tests + build check**

```bash
cd cockpit && npx vitest run src/app/views/contacts/ src/app/core/services/contacts.service.spec.ts
npm i --no-save @monaco-editor/loader && npx ng build   # budget check (house workaround)
```

Expected: specs pass; build completes with the initial bundle under budget (contacts is lazy — it must not move the initial number materially).

- [ ] **Step 6: Commit**

```bash
git add cockpit/src/app/views/contacts cockpit/src/app/app.routes.ts \
  cockpit/src/app/shell/sidebar/sidebar.component.ts cockpit/src/assets/i18n/en.json \
  cockpit/src/assets/i18n/de-DE.json
git commit -m "$(cat <<'EOF'
feat(cockpit): /contacts page — expandable rows, one-at-a-time edit form

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Live gate on local k3d + spec status flip

**Files:**
- Modify: `docs/features/contacts_registry.md` (Status line only, after the gate passes)

**Interfaces:**
- Consumes: everything. This is the house live-verify step before any dev deploy.

- [ ] **Step 1: Deploy the branch to the local stack**

```bash
kubectl config use-context k3d-srw   # local stack (tilt registry :5005); neo4j stays OFF
tilt up   # from repo root; wait for orchestrator + agent images to converge
```

Migration 0072 applies via the orchestrator's boot migration path — confirm:
`kubectl logs deploy/orchestrator | grep 0072` → `applied 0072_contacts_normalize.sql`.

- [ ] **Step 2: Seed a contact and link it (in-pod psql — BFF-authed endpoints need a browser session; the UI check comes later)**

```bash
ORCH_POD=$(kubectl get pod -l app=orchestrator -o name | head -1)
kubectl exec -i "$ORCH_POD" -- python - <<'PY'
import asyncio, os, asyncpg
async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    user = await conn.fetchrow("SELECT id FROM users ORDER BY created_at LIMIT 1")
    proj = await conn.fetchrow("SELECT id FROM projects ORDER BY created_at LIMIT 1")
    c = await conn.fetchrow(
        "INSERT INTO contacts (owner_user_id, display_name, notes) VALUES ($1, 'Live Gate Anna', 'Gate note.') RETURNING id",
        user["id"])
    await conn.execute(
        "INSERT INTO contact_addresses (contact_id, owner_user_id, channel, address, is_primary, opt_in_status) VALUES ($1,$2,'email','gate-anna@example.org',true,'opted_in')",
        c["id"], user["id"])
    await conn.execute(
        "INSERT INTO project_contacts (project_id, contact_id, added_by) VALUES ($1,$2,$3)",
        proj["id"], c["id"], user["id"])
    print("seeded", c["id"], "project", proj["id"])
asyncio.run(main())
PY
```

(If `DATABASE_URL` isn't in the pod env, build the DSN from the `POSTGRES_*` vars the same way `migrate.py` does.)

- [ ] **Step 3: Run a job in that project; verify the projection + resolution**

Create a job in the seeded project (Cockpit UI, or the house internal-API path with `X-Internal-Key: dev_mcp_internal_key` and `user_id` in the body). Prompt: `Read the file contacts/live-gate-anna.md and send a message to "Live Gate Anna" with subject "gate" saying hi.`

Verify, in order:
1. Workspace file exists: job log shows the `read_file` returning the frontmatter with `display_name: "Live Gate Anna"` and the `Gate note.` body.
2. `send_message` resolves: job log shows the send succeeding to `gate-anna@example.org` (or failing only at SMTP — resolution happened if the recipient was accepted).
3. Negative: a second job messaging `"Nobody Realname"` gets the 404 listing `Live Gate Anna` under `Contacts:`.

- [ ] **Step 4: Verify the Cockpit page end-to-end**

In the local Cockpit: `/contacts` shows `Live Gate Anna` with an `email` chip and `1 proj`; expansion shows the address + project chip + notes; Edit → add a whatsapp address `+4917012345678` → row chip shows `whatsapp·pending`; Delete dialog names the project; sidebar entry present in both languages (switch language in settings).

- [ ] **Step 5: Loop floor spot-check**

Create a loop/project job in the seeded project; after its first cycle, confirm the loop repo's `main` has **no** `contacts/` directory (the floor `.gitignore` line from Task 6) — `git ls-tree` via the Gitea UI or clone.

- [ ] **Step 6: Flip the spec status and commit**

In `docs/features/contacts_registry.md`, change the Status line to:

```markdown
**Status:** Implemented on develop (live-gated on local k3d) — YYYY-MM-DD. Phase-2 drop migration (0073) still owed next release.
```

```bash
git add docs/features/contacts_registry.md
git commit -m "$(cat <<'EOF'
docs(features): contacts registry — implemented, live-gated on k3d

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

## Self-review notes (already applied)

- Spec coverage: schema/backfill → T1; DB semantics incl. per-owner uniqueness, primary demotion, opt-in reset → T2; API table incl. find-or-create-then-link and retirements → T3+T4; resolver statuses + send_message errors → T2+T4; materialization + gitignore floor + env gate → T5+T6; Cockpit page/route/sidebar/i18n/C1 → T7+T8; live k3d gate → T9. Phase-2 migration 0073 is deliberately **not** a task (spec: later release).
- Known bounded lookups (constructor name in T2 fixture, `require_project_member` import path in T3, projects service in T8): each has a grep command + a working fallback inline.
