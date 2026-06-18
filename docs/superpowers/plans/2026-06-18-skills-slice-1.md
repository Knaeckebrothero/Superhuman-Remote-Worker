# Agent Skills — Slice 1 (Authoring Foundation) Implementation Plan

> **Status: ✅ Slice 1 COMPLETE & live-verified on k3d (2026-06-18).** All 12 tasks landed on `develop` (commits `b117fb3f`..`d138d234`). Backend 31 unit tests + Cockpit 4 vitest + `ng build` green; migration `0031` applies; `PostgresDB` CRUD round-trips byte-for-byte against real Postgres. End-to-end on the live k3d stack (`SKILLS_DB_ENABLED=true`, migration applied, deployed via Tilt): **14/14 checks** — create/get/update(version bump + file replace)/delete/duplicate, bundled `hello-skill` discoverable, **byte-comparable `import → export`**, and negative 422s (rename-via-frontmatter, missing SKILL.md). Verified at the API surface the Cockpit consumes (MCP-header auth); a browser click-through of `/skills` is the one remaining optional check.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the data model and authoring surface for Agent Skills — store, create, edit, duplicate, and import/export `SKILL.md` skills with a Cockpit editor — with **no agent runtime** (no menu injection, no `use_skill`, no workspace materialization; those are Slice 2).

**Architecture:** A skill is a *directory* (the open `SKILL.md` standard, agentskills.io). Storage mirrors experts (migration `0028`): bundled skills stay disk-canonical in `config/skills/`, while user/admin skills are rows in a new `skills` table plus a `skill_files(skill_id, path, content)` tree. The canonical artifact is the stored `SKILL.md` bytes; `name`/`description` are parsed from its frontmatter and denormalized onto the row for the catalog. Import/export is the **native zipped skill directory** (not a JSON envelope), so an exported skill drops straight into `.claude/skills` and a real Claude Code/Codex skill imports unchanged. Everything is gated by a new `SKILLS_DB_ENABLED` flag (dev-on/prod-off), exactly like `EXPERTS_DB_ENABLED`.

**Tech Stack:** Python 3.12 / FastAPI / asyncpg (orchestrator), PostgreSQL, Angular 21 standalone components + signals + Transloco (Cockpit), Helm.

**Design doc:** `docs/features/agent_skills.md` (Slice 1). **Mirrors:** `docs/superpowers/plans/2026-06-17-expert-crud-ui.md` (the experts feature this clones).

---

## Scope

**In scope (Slice 1):**
- `SKILLS_DB_ENABLED` flag + helm wiring.
- A pure `SKILL.md` format module: parse, validate paths, deterministic zip pack/unpack.
- Migration `0031_skills.sql`: `skills` + `skill_files` tables.
- Persistence methods on `PostgresDB`.
- HTTP CRUD: create, list, get, update, delete, duplicate, **zip** export, **zip** import, reload.
- Read-only MCP tools: `list_skills`, `get_skill`, `reload_skills`.
- Cockpit: `/skills`, `/skills/new`, `/skills/:id/edit` — list + multi-file editor.
- Save-time security: `hard_deny_scan` on parsed frontmatter + path-traversal validation.

**Explicitly out of scope (later slices):**
- Agent runtime — menu injection into `resolved_config`, `use_skill`, workspace materialization (**Slice 2**).
- `fence_persona` of description/body — that happens at *prompt assembly*, which Slice 1 doesn't touch (**Slice 2**).
- Expert↔skill bindings, migrating `todo_guide`/`research_guide` (**Slice 3**).
- Script execution behind the capability-grants `evaluate()` gate; binary (non-UTF-8) assets (**Slice 4**).
- Context-aware `semantic` auto-suggest (**later**).

## Design decisions (baked in — flag at review if you disagree)

1. **Migration `0031_skills.sql`** creates only `skills` + `skill_files`. **No `project_experts`-style junction and no `jobs.skill_id`** — a job loads many skills (binding is Slice 3), so skills are not 1:1 job-bound, and project skills come from the Gitea repo at runtime (Slice 2). This is deliberately leaner than experts' `0028`.
2. The `skills` row mirrors experts' *metadata* columns **minus `expert_type`, `config`, `prompts`** and adds nothing per-file. Files live in `skill_files(skill_id, path, content TEXT, PRIMARY KEY(skill_id, path))`, `ON DELETE CASCADE`. `content` is `TEXT` (UTF-8) — SKILL.md, markdown refs, and scripts are all text; binary assets are a Slice 4 concern.
3. **The stored `SKILL.md` bytes are canonical.** `name` + `description` are parsed from its frontmatter and denormalized to the row on every save. SRW presentation chrome (`display_name`, `icon`, `color`, `tags`) lives only on the row and is **never injected into `SKILL.md`** — so a vanilla Claude Code skill round-trips byte-for-byte through import→export.
4. **Import/export is the native zip** of the skill directory (`<name>/SKILL.md`, …), not a JSON bundle. Packing is deterministic (fixed timestamps, sorted entries) so import→export reproduces identical file contents.
5. **Security at save = `hard_deny_scan(frontmatter)` (422) + path-traversal validation.** No grants gate at save (that gates *script execution*, Slice 4) and no fencing (runtime, Slice 2).
6. **The list endpoint tags + concatenates** bundled + owned + global; it does **not** sort by precedence (downstream concern, Slice 2). `list_skills_visible` is `owner_id = $1 OR is_global` — no project join.
7. **`name` is immutable on edit** (mirrors experts). If an edited `SKILL.md`'s frontmatter `name` differs from the row's `name`, the update is rejected (422) — rename = create a new skill. Forking (duplicate/import) rewrites the copy's frontmatter `name` to match its new slug.
8. **Editor is a multi-file editor** (file list + per-file textarea, `SKILL.md` always present) — the faithful "skill = directory" authoring model. This is the one net-new UI shape vs. experts' single-form editor.
9. **Flag `SKILLS_DB_ENABLED` / helm `agent.skillsDbEnabled`**, default off (prod-safe), dev-on — mirrors `EXPERTS_DB_ENABLED` exactly.

## File structure

**Create:**
- `src/core/skill_format.py` — pure `SKILL.md` parse/validate/zip (net-new core, no DB/FastAPI).
- `orchestrator/database/migrations/app/0031_skills.sql` — `skills` + `skill_files`.
- `config/skills/hello-skill/SKILL.md` — one bundled example skill (proves the scanner + bundled path).
- `cockpit/src/app/views/skills/skills-page.component.ts` — route container.
- `cockpit/src/app/views/skills/skills-list.component.ts` — list/table + actions.
- `cockpit/src/app/views/skills/skill-editor.component.ts` — multi-file create/edit form.
- `cockpit/src/app/views/skills/skill-editor.util.ts` — pure helpers (slugify, files↔record, template).
- `tests/test_skill_format.py` — parser/zip unit tests.
- `tests/test_skill_crud.py` — model/validation unit tests.
- `cockpit/src/app/views/skills/skill-editor.util.spec.ts` — UI helper tests.

**Modify:**
- `orchestrator/database/postgres.py` — add a `# ── Skills ──` section (after experts, ~line 5398).
- `orchestrator/main.py` — models, flag helpers, scan, parse helper, CRUD endpoints.
- `orchestrator/mcp/server.py` — `list_skills` / `get_skill` / `reload_skills` tools.
- `orchestrator/mcp/client.py` — matching client methods.
- `cockpit/src/app/core/models/api.model.ts` — `Skill`, `SkillDetail`, request DTOs.
- `cockpit/src/app/core/services/api.service.ts` — write/read methods.
- `cockpit/src/app/app.routes.ts` — three routes + imports.
- `cockpit/src/app/shell/sidebar/sidebar.component.ts` — nav entry.
- `cockpit/src/assets/i18n/en.json` — `skills` block + `nav.skills`.
- `helm/values.yaml`, `helm/templates/configmap.yaml`, `helm/templates/orchestrator/deployment.yaml` — flag wiring.

---

## Task 1: `SKILL.md` format module (parse / validate / zip)

This is the net-new core. Pure functions, no DB, no FastAPI — fully unit-tested.

**Files:**
- Create: `src/core/skill_format.py`
- Test: `tests/test_skill_format.py`

- [x] **Step 1: Write the failing tests**

```python
# tests/test_skill_format.py
import pytest
from src.core.skill_format import (
    parse_skill_md,
    skill_identity,
    validate_skill_path,
    validate_skill_files,
    pack_skill_zip,
    unpack_skill_zip,
    set_skill_name,
    SkillFormatError,
)

SAMPLE = (
    "---\n"
    "name: pdf-filler\n"
    "description: Use when filling PDF forms from structured data.\n"
    "---\n"
    "\n"
    "# PDF Filler\n"
    "\n"
    "Run `scripts/fill.py`.\n"
)


def test_parse_splits_frontmatter_and_body():
    fm, body = parse_skill_md(SAMPLE)
    assert fm["name"] == "pdf-filler"
    assert fm["description"].startswith("Use when")
    assert body.startswith("\n# PDF Filler")


def test_parse_rejects_missing_frontmatter():
    with pytest.raises(SkillFormatError):
        parse_skill_md("# no frontmatter here\n")


def test_parse_rejects_non_mapping_frontmatter():
    with pytest.raises(SkillFormatError):
        parse_skill_md("---\n- just\n- a\n- list\n---\nbody\n")


def test_identity_extracts_name_and_description():
    fm, _ = parse_skill_md(SAMPLE)
    name, desc = skill_identity(fm)
    assert name == "pdf-filler"
    assert desc.startswith("Use when")


@pytest.mark.parametrize("bad", ["Bad Name", "1leading", "UPPER", "has space", ""])
def test_identity_rejects_bad_slug(bad):
    with pytest.raises(SkillFormatError):
        skill_identity({"name": bad, "description": "x"})


@pytest.mark.parametrize(
    "bad", ["/abs", "../escape", "a/../b", "a//b", "back\\slash", "", "trail/"]
)
def test_validate_path_rejects_unsafe(bad):
    with pytest.raises(SkillFormatError):
        validate_skill_path(bad)


def test_validate_path_accepts_nested():
    assert validate_skill_path("references/guide.md") == "references/guide.md"


def test_validate_files_requires_skill_md():
    with pytest.raises(SkillFormatError):
        validate_skill_files({"references/x.md": "y"})


def test_zip_round_trip_is_lossless():
    files = {"SKILL.md": SAMPLE, "references/g.md": "# Guide\n", "scripts/f.py": "print(1)\n"}
    data = pack_skill_zip("pdf-filler", files)
    assert unpack_skill_zip(data) == files


def test_zip_pack_is_deterministic():
    files = {"SKILL.md": SAMPLE, "references/g.md": "# Guide\n"}
    assert pack_skill_zip("pdf-filler", files) == pack_skill_zip("pdf-filler", files)


def test_unpack_strips_single_top_dir():
    files = {"SKILL.md": SAMPLE}
    data = pack_skill_zip("pdf-filler", files)  # writes 'pdf-filler/SKILL.md'
    assert unpack_skill_zip(data) == files  # top dir stripped


def test_set_skill_name_rewrites_frontmatter_only():
    out = set_skill_name(SAMPLE, "pdf-filler-copy")
    fm, _ = parse_skill_md(out)
    assert fm["name"] == "pdf-filler-copy"
    assert "# PDF Filler" in out  # body untouched
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_skill_format.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.core.skill_format'`

- [x] **Step 3: Write the module**

```python
# src/core/skill_format.py
"""SKILL.md open-standard (agentskills.io) parsing + packaging for SRW skills.

A skill is a directory: a required SKILL.md (YAML frontmatter + markdown body)
plus optional reference/script/asset files. The SKILL.md is the canonical
artifact — we store its bytes verbatim and only PARSE it to denormalize
name/description onto the skills row. This module is pure (no DB, no FastAPI):
parse, validate paths, and pack/unpack the native zip used for import/export.

Design: docs/features/agent_skills.md (Slice 1).
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

import yaml

SKILL_MD = "SKILL.md"
# Skill name slug — same shape as expert names (see ExpertCreate in main.py).
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
# Leading '---' line, YAML, closing '---' line, then the body.
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
# Fixed timestamp so import->export is byte-reproducible (zip stores mtime).
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


class SkillFormatError(ValueError):
    """Raised when a skill bundle or SKILL.md is malformed (maps to HTTP 422)."""


def parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    """Split a SKILL.md into (frontmatter dict, body str)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise SkillFormatError("SKILL.md must start with a '---' YAML frontmatter block")
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise SkillFormatError(f"SKILL.md frontmatter is not valid YAML: {e}") from e
    if not isinstance(fm, dict):
        raise SkillFormatError("SKILL.md frontmatter must be a mapping")
    return fm, m.group(2)


def skill_identity(frontmatter: dict[str, Any]) -> tuple[str, str]:
    """Extract and validate (name, description) from frontmatter."""
    name = str(frontmatter.get("name", "")).strip()
    if not _NAME_RE.match(name):
        raise SkillFormatError(
            f"SKILL.md 'name' must match ^[a-z][a-z0-9_-]*$ (got {name!r})"
        )
    description = str(frontmatter.get("description", "") or "").strip()
    return name, description


def validate_skill_path(path: str) -> str:
    """Return a safe relative path, or raise. Rejects abs/traversal/empty/backslash."""
    if not path or path.strip() != path or path.endswith("/"):
        raise SkillFormatError(f"illegal file path: {path!r}")
    if path.startswith("/") or "\\" in path or "\x00" in path:
        raise SkillFormatError(f"illegal file path: {path!r}")
    if any(seg in ("", ".", "..") for seg in path.split("/")):
        raise SkillFormatError(f"illegal file path: {path!r}")
    return path


def validate_skill_files(files: dict[str, str]) -> None:
    """Require a root SKILL.md and safe paths everywhere."""
    if SKILL_MD not in files:
        raise SkillFormatError("a skill must contain a SKILL.md at its root")
    for path in files:
        validate_skill_path(path)


def pack_skill_zip(name: str, files: dict[str, str]) -> bytes:
    """Pack files into a deterministic zip under a top-level <name>/ dir."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(files):
            info = zipfile.ZipInfo(f"{name}/{path}", date_time=_ZIP_EPOCH)
            info.external_attr = 0o644 << 16
            zf.writestr(info, files[path])
    return buf.getvalue()


def unpack_skill_zip(data: bytes) -> dict[str, str]:
    """Unpack a skill zip into a path->content map rooted at the skill dir."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise SkillFormatError(f"not a valid zip archive: {e}") from e
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names:
        raise SkillFormatError("zip archive is empty")
    tops = {n.split("/", 1)[0] for n in names}
    strip = len(tops) == 1 and all("/" in n for n in names)
    prefix = f"{next(iter(tops))}/" if strip else ""
    files: dict[str, str] = {}
    for n in names:
        rel = n[len(prefix):] if prefix and n.startswith(prefix) else n
        validate_skill_path(rel)
        try:
            files[rel] = zf.read(n).decode("utf-8")
        except UnicodeDecodeError as e:
            raise SkillFormatError(f"{rel}: only UTF-8 text files are supported") from e
    validate_skill_files(files)
    return files


def set_skill_name(text: str, new_name: str) -> str:
    """Rewrite frontmatter 'name:' to new_name, preserving the body. For forks."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise SkillFormatError("SKILL.md must start with a '---' YAML frontmatter block")
    block, body = m.group(1), m.group(2)
    new_block, n = re.subn(r"(?m)^name:.*$", f"name: {new_name}", block, count=1)
    if n == 0:
        new_block = f"name: {new_name}\n{block}"
    return f"---\n{new_block}\n---\n{body}"
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_skill_format.py -v`
Expected: PASS (all cases)

- [x] **Step 5: Commit**

```bash
git add src/core/skill_format.py tests/test_skill_format.py
git commit -m "feat(skills): SKILL.md format module — parse, validate, zip round-trip"
```

---

## Task 2: Migration `0031_skills.sql`

Mirrors `0028_experts.sql` conventions (header block, `BEGIN` + `SET LOCAL` timeouts, `gen_random_uuid()` PK, `(name, owner_id)` unique index, `owner_id` CASCADE). No `jobs` FK, no junction.

**Files:**
- Create: `orchestrator/database/migrations/app/0031_skills.sql`

- [x] **Step 1: Write the migration**

```sql
-- migration:     0031_skills.sql
-- description:   DB-backed user/admin Agent Skills (Slice 1, authoring foundation).
--                Mirrors experts (0028): bundled SKILL.md skills in config/skills/
--                stay disk-canonical; this table holds user/admin rows. A skill is
--                a directory, so skill_files holds the file tree (SKILL.md + refs).
--                The SKILL.md is canonical; name/description are denormalized here
--                for the catalog/menu. No project junction and no jobs.skill_id yet
--                (project skills come from the Gitea repo; expert<->skill bindings
--                are a later slice). Deleting a row cascades its files away.
--                Design: docs/features/agent_skills.md (Slice 1).
-- depends-on:    0001_initial.sql
-- expected:      < 1s on dev DB. Two new empty tables, no table rewrite.
-- locks:         AccessExclusiveLock on the two new tables only.
-- transactional: yes

BEGIN;
SET LOCAL lock_timeout                        = '2s';
SET LOCAL statement_timeout                   = '15min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

CREATE TABLE IF NOT EXISTS skills (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         VARCHAR(100) NOT NULL,        -- slug ^[a-z][a-z0-9_-]*$, from SKILL.md
    display_name VARCHAR(200) NOT NULL,
    description  TEXT,
    icon         VARCHAR(100) NOT NULL DEFAULT 'extension',
    color        VARCHAR(7)   NOT NULL DEFAULT '#6B7280',
    tags         TEXT[]       NOT NULL DEFAULT '{}',
    owner_id     UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    is_global    BOOLEAN      NOT NULL DEFAULT FALSE,
    version      INTEGER      NOT NULL DEFAULT 1,
    updated_by   UUID         REFERENCES users(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_skills_name_owner ON skills (name, owner_id);
CREATE INDEX IF NOT EXISTS idx_skills_owner ON skills (owner_id);

CREATE TABLE IF NOT EXISTS skill_files (
    skill_id  UUID NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    path      TEXT NOT NULL,            -- relative, e.g. 'SKILL.md', 'references/x.md'
    content   TEXT NOT NULL,            -- UTF-8; binary assets deferred (Slice 4)
    PRIMARY KEY (skill_id, path)
);

COMMENT ON TABLE skills IS
    'DB-backed user/admin Agent Skills (overlay over bundled config/skills/). '
    'name/description denormalized from the canonical SKILL.md in skill_files. '
    'Design: docs/features/agent_skills.md.';

COMMIT;
```

- [x] **Step 2: Apply the migration locally and verify the tables exist**

Run (against the local k3d / dev Postgres — adjust connection to your dev DB):
```bash
# from inside the orchestrator pod or with DATABASE_URL pointing at dev:
python -m orchestrator.database.migrate   # or the project's standard migrate entrypoint
psql "$DATABASE_URL" -c "\d skills" -c "\d skill_files"
```
Expected: both tables print with the columns above; `uq_skills_name_owner` listed on `skills`.

> Cross-check the migrate entrypoint against how `0030_capability_grants.sql` is applied in this repo (same runner). If unsure, grep `orchestrator/database/` for the migration runner and `docs/db_migration.md`.

- [x] **Step 3: Commit**

```bash
git add orchestrator/database/migrations/app/0031_skills.sql
git commit -m "feat(skills): migration 0031 — skills + skill_files tables"
```

---

## Task 3: Persistence methods on `PostgresDB`

Mirror the expert methods at `orchestrator/database/postgres.py:5248-5398`. `tags` is `TEXT[]` (bind a list directly, no `json.dumps`). `create_skill`/`update_skill` touch two tables, so use an explicit transaction (`async with self.acquire()` + `conn.transaction()`), unlike the single-statement expert methods.

**Files:**
- Modify: `orchestrator/database/postgres.py` (add after `delete_expert`, ~line 5398)
- Test: `tests/test_skill_crud.py` (DB methods are exercised live in Task 12; the unit test here covers the pure HTTP layer in Task 5)

- [x] **Step 1: Add the Skills persistence section**

```python
    # ── Skills (Agent Skills, Slice 1: authoring foundation) ──────────────
    async def create_skill(
        self,
        *,
        name: str,
        display_name: str,
        owner_id: str,
        files: Dict[str, str],
        description: str | None = None,
        icon: str = "extension",
        color: str = "#6B7280",
        tags: List[str] | None = None,
        is_global: bool = False,
    ) -> Dict[str, Any]:
        """Insert an owned skill + its files atomically. (name, owner_id) unique."""
        async with self.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO skills
                        (name, display_name, description, icon, color, tags,
                         owner_id, is_global)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    RETURNING *
                    """,
                    name,
                    display_name,
                    description,
                    icon,
                    color,
                    tags or [],
                    UUID(str(owner_id)),
                    is_global,
                )
                await conn.executemany(
                    "INSERT INTO skill_files (skill_id, path, content) "
                    "VALUES ($1, $2, $3)",
                    [(row["id"], p, c) for p, c in sorted(files.items())],
                )
        return dict(row)

    async def get_skill_by_id(self, skill_id: str) -> Dict[str, Any] | None:
        row = await self.fetchrow(
            "SELECT * FROM skills WHERE id = $1", UUID(str(skill_id))
        )
        return dict(row) if row else None

    async def get_skill_files(self, skill_id: str) -> Dict[str, str]:
        rows = await self.fetch(
            "SELECT path, content FROM skill_files WHERE skill_id = $1 ORDER BY path",
            UUID(str(skill_id)),
        )
        return {r["path"]: r["content"] for r in rows}

    async def list_skills_visible(self, *, user_id: str) -> List[Dict[str, Any]]:
        """Owned + global skills visible to the caller. No project junction in
        Slice 1 (project skills come from the Gitea repo at runtime — Slice 2)."""
        rows = await self.fetch(
            """
            SELECT * FROM skills
            WHERE owner_id = $1 OR is_global = TRUE
            ORDER BY created_at DESC
            """,
            UUID(str(user_id)),
        )
        return [dict(r) for r in rows]

    async def update_skill(
        self,
        skill_id: str,
        *,
        updated_by: str,
        files: Dict[str, str] | None = None,
        **fields: Any,
    ) -> Dict[str, Any] | None:
        """Patch mutable metadata (NOT name — immutable) + optionally replace the
        file set, bumping version. Column names come from a fixed allow-list."""
        allowed = {"display_name", "description", "icon", "color", "tags", "is_global"}
        async with self.acquire() as conn:
            async with conn.transaction():
                sets, vals = [], []
                for k, v in fields.items():
                    if k not in allowed:
                        continue
                    vals.append(v)
                    sets.append(f"{k} = ${len(vals)}")
                set_sql = (", ".join(sets) + ", ") if sets else ""
                vals.append(UUID(str(updated_by)))
                vals.append(UUID(str(skill_id)))
                row = await conn.fetchrow(
                    f"""
                    UPDATE skills
                    SET {set_sql}version = version + 1,
                        updated_by = ${len(vals) - 1}, updated_at = NOW()
                    WHERE id = ${len(vals)}
                    RETURNING *
                    """,
                    *vals,
                )
                if row and files is not None:
                    await conn.execute(
                        "DELETE FROM skill_files WHERE skill_id = $1",
                        UUID(str(skill_id)),
                    )
                    await conn.executemany(
                        "INSERT INTO skill_files (skill_id, path, content) "
                        "VALUES ($1, $2, $3)",
                        [(row["id"], p, c) for p, c in sorted(files.items())],
                    )
        return dict(row) if row else None

    async def delete_skill(self, skill_id: str) -> bool:
        result = await self.execute(
            "DELETE FROM skills WHERE id = $1", UUID(str(skill_id))
        )
        return result == "DELETE 1"
```

> No `skill_delete_blockers` in Slice 1: nothing references a skill yet (bindings are Slice 3). `skill_files` rows are removed by the `ON DELETE CASCADE` FK.

- [x] **Step 2: Sanity-check syntax**

Run: `python -c "import orchestrator.database.postgres"`
Expected: no import error (no test harness needed; live CRUD is verified in Task 12).

- [x] **Step 3: Commit**

```bash
git add orchestrator/database/postgres.py
git commit -m "feat(skills): PostgresDB CRUD for skills + skill_files"
```

---

## Task 4: Orchestrator — models, flag, scan, parse helper

Mirror `ExpertInfo`/`ExpertCreate`/`ExpertUpdate` (`main.py:16108-16488`), `_is_experts_db_enabled`/`_require_experts_db` (`main.py:944-951, 16491-16494`), `_scan_experts` (`main.py:16135-16184`), and `_validate_expert_fragment` (`main.py:16497-16507`).

**Files:**
- Modify: `orchestrator/main.py`
- Test: `tests/test_skill_crud.py`

- [x] **Step 1: Write the failing tests**

```python
# tests/test_skill_crud.py
import pytest
from fastapi import HTTPException

from orchestrator.main import (
    SkillCreate,
    SkillUpdate,
    _validate_skill_frontmatter,
    _parse_skill_bundle,
)

GOOD = (
    "---\n"
    "name: my-helper\n"
    "description: Use when X.\n"
    "---\n"
    "# My Helper\n"
)


def test_skill_create_minimal_ok():
    s = SkillCreate(files={"SKILL.md": GOOD})
    assert s.icon == "extension" and s.color == "#6B7280" and s.tags == []


def test_skill_create_rejects_bad_color():
    with pytest.raises(Exception):
        SkillCreate(files={"SKILL.md": GOOD}, color="red")


def test_skill_update_excludes_name():
    assert "name" not in SkillUpdate.model_fields


def test_parse_bundle_returns_name_and_description():
    name, desc, files = _parse_skill_bundle({"SKILL.md": GOOD})
    assert name == "my-helper"
    assert desc == "Use when X."
    assert "SKILL.md" in files


def test_parse_bundle_rejects_missing_skill_md():
    with pytest.raises(HTTPException) as ei:
        _parse_skill_bundle({"references/x.md": "y"})
    assert ei.value.status_code == 422


def test_parse_bundle_rejects_path_traversal():
    with pytest.raises(HTTPException) as ei:
        _parse_skill_bundle({"SKILL.md": GOOD, "../evil": "x"})
    assert ei.value.status_code == 422


def test_validate_frontmatter_blocks_credentials():
    with pytest.raises(HTTPException) as ei:
        _validate_skill_frontmatter({"connections": {"token": "secret"}})
    assert ei.value.status_code == 422


def test_validate_frontmatter_allows_clean():
    _validate_skill_frontmatter({"name": "x", "description": "y"})
```

- [x] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_skill_crud.py -v`
Expected: FAIL with `ImportError: cannot import name 'SkillCreate'`

- [x] **Step 3: Add models near the experts models (`main.py`, after `ExpertUpdate` ~16488)**

```python
class SkillInfo(BaseModel):
    """Skill catalog metadata for discovery (the L1 'menu' entry)."""

    id: str
    name: str
    display_name: str
    description: str
    icon: str = "extension"
    color: str = "#6B7280"
    tags: list[str] = []


class SkillCreate(BaseModel):
    """Create a DB-backed skill from its file tree (must include SKILL.md).

    name + description are parsed from SKILL.md frontmatter, not sent separately."""

    files: dict[str, str]
    display_name: str | None = Field(None, max_length=200)
    icon: str = "extension"
    color: str = Field("#6B7280", pattern=r"^#[0-9A-Fa-f]{6}$")
    tags: list[str] = []


class SkillUpdate(BaseModel):
    """Patch a DB skill; name is immutable (derived from SKILL.md) so it is absent."""

    files: dict[str, str] | None = None
    display_name: str | None = Field(None, min_length=1, max_length=200)
    icon: str | None = None
    color: str | None = Field(None, pattern=r"^#[0-9A-Fa-f]{6}$")
    tags: list[str] | None = None
    is_global: bool | None = None
```

- [x] **Step 4: Add the flag helper near `_is_experts_db_enabled` (`main.py` ~951)**

```python
def _is_skills_db_enabled() -> bool:
    """True when DB-backed skills are on (env). Dev on / prod off (helm
    ``skillsDbEnabled``). Mirrors ``EXPERTS_DB_ENABLED``."""
    return os.getenv("SKILLS_DB_ENABLED", "").lower().strip() in ("true", "1", "yes")
```

- [x] **Step 5: Add the scan, parse, and validation helpers near the experts helpers (`main.py`, after `_validate_expert_fragment` ~16507)**

```python
# Cache bundled skills at startup (mirrors _experts_cache).
_skills_cache: list[SkillInfo] | None = None


def _require_skills_db() -> None:
    """The DB-skills feature is fully behind SKILLS_DB_ENABLED."""
    if not _is_skills_db_enabled():
        raise HTTPException(status_code=404, detail="DB-backed skills are not enabled")


def _validate_skill_frontmatter(frontmatter: dict[str, Any]) -> None:
    """Reject credential sections in SKILL.md frontmatter (reuses expert deny-scan)."""
    from src.core.expert_resolution import hard_deny_scan

    offending = hard_deny_scan(frontmatter)
    if offending:
        raise HTTPException(
            status_code=422,
            detail="SKILL.md frontmatter may not set credential sections: "
            + ", ".join(sorted(offending)),
        )


def _parse_skill_bundle(files: dict[str, str]) -> tuple[str, str, dict[str, str]]:
    """Validate paths, parse SKILL.md, deny-scan. Returns (name, description, files)."""
    from src.core.skill_format import (
        SkillFormatError,
        parse_skill_md,
        skill_identity,
        validate_skill_files,
    )

    try:
        validate_skill_files(files)
        fm, _body = parse_skill_md(files["SKILL.md"])
        name, description = skill_identity(fm)
    except SkillFormatError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    _validate_skill_frontmatter(fm)
    return name, description, files


def _skill_row_to_meta(row: dict[str, Any]) -> dict[str, Any]:
    """Project a skills row into the catalog metadata shape."""
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "display_name": row["display_name"],
        "description": row.get("description") or "",
        "icon": row["icon"],
        "color": row["color"],
        "tags": row.get("tags") or [],
        "version": row.get("version"),
        "owner_id": str(row["owner_id"]) if row.get("owner_id") else None,
    }


def _scan_skills() -> list[SkillInfo]:
    """Scan config/skills/<name>/SKILL.md for bundled skills."""
    from src.core.skill_format import SkillFormatError, parse_skill_md, skill_identity

    skills_dir = _get_config_dir() / "skills"
    skills: list[SkillInfo] = []
    if not skills_dir.is_dir():
        return skills
    for entry in sorted(skills_dir.iterdir()):
        skill_md = entry / "SKILL.md"
        if not entry.is_dir() or not skill_md.exists():
            continue
        try:
            fm, _ = parse_skill_md(skill_md.read_text(encoding="utf-8"))
            name, description = skill_identity(fm)
            skills.append(
                SkillInfo(
                    id=entry.name,
                    name=name,
                    display_name=fm.get("display_name", name.replace("-", " ").title()),
                    description=description,
                    icon=fm.get("icon", "extension"),
                    color=fm.get("color", "#6B7280"),
                    tags=fm.get("tags", []),
                )
            )
        except (SkillFormatError, OSError, ValueError) as e:
            logger.warning(f"Failed to parse bundled skill {skill_md}: {e}")
    return skills


def _bundled_skill_bundle(skill_name: str) -> dict[str, Any] | None:
    """Read a bundled skill's full directory into a metadata + files dict."""
    from src.core.skill_format import parse_skill_md, skill_identity, validate_skill_path

    skill_dir = _get_config_dir() / "skills" / skill_name
    skill_md = skill_dir / "SKILL.md"
    if not skill_dir.is_dir() or not skill_md.exists():
        return None
    files: dict[str, str] = {}
    for fp in sorted(skill_dir.rglob("*")):
        if not fp.is_file():
            continue
        rel = str(fp.relative_to(skill_dir))
        try:
            validate_skill_path(rel)
            files[rel] = fp.read_text(encoding="utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
    fm, _ = parse_skill_md(files["SKILL.md"])
    name, description = skill_identity(fm)
    return {
        "id": skill_name,
        "name": name,
        "display_name": fm.get("display_name", name.replace("-", " ").title()),
        "description": description,
        "icon": fm.get("icon", "extension"),
        "color": fm.get("color", "#6B7280"),
        "tags": fm.get("tags", []),
        "files": files,
    }


async def _create_forked_skill(
    src: dict[str, Any], owner_id: str, suffix: str = "copy"
) -> dict[str, Any]:
    """Create an owned skill from a source dict, suffixing the slug on collision
    and rewriting the copy's SKILL.md 'name' to match (mirrors _create_forked_expert)."""
    from src.core.skill_format import set_skill_name

    base_name = src["name"]
    for attempt in range(6):
        name = (f"{base_name}-{suffix}" if attempt == 0 else f"{base_name}-{suffix}-{attempt}")[:100]
        files = dict(src["files"])
        files["SKILL.md"] = set_skill_name(src["files"]["SKILL.md"], name)
        try:
            return await postgres_db.create_skill(
                name=name,
                display_name=f"{src['display_name']} ({suffix})"[:200],
                description=src.get("description"),
                icon=src.get("icon", "extension"),
                color=src.get("color", "#6B7280"),
                tags=src.get("tags") or [],
                owner_id=owner_id,
                files=files,
            )
        except HTTPException:
            raise
        except Exception as e:
            if "uq_skills_name_owner" in str(e):
                continue
            raise
    raise HTTPException(status_code=409, detail="No free name for the copy")
```

- [x] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_skill_crud.py -v`
Expected: PASS

- [x] **Step 7: Commit**

```bash
git add orchestrator/main.py tests/test_skill_crud.py
git commit -m "feat(skills): orchestrator models, flag, bundled scan, parse+deny helpers"
```

---

## Task 5: Orchestrator — CRUD endpoints (create/list/get/update/delete/reload)

Mirror the experts handlers at `main.py:16187-16679`. Auth via `require_approved_user(request, postgres_db)`; bundled vs DB distinguished by `_looks_like_uuid` (`main.py:1063-1070`); admin reload via `_require_admin`.

**Files:**
- Modify: `orchestrator/main.py` (place near the experts endpoints, ~16760)

- [x] **Step 1: Add the create, list, reload, get endpoints**

```python
@app.post("/api/skills")
async def create_skill(request: Request, body: SkillCreate) -> dict[str, Any]:
    """Create an owned DB skill from its file tree (Slice 1: deny-scan validated)."""
    _require_skills_db()
    user = await require_approved_user(request, postgres_db)
    name, description, files = _parse_skill_bundle(body.files)
    try:
        return await postgres_db.create_skill(
            name=name,
            display_name=body.display_name or name,
            description=description,
            icon=body.icon,
            color=body.color,
            tags=body.tags,
            owner_id=str(user["id"]),
            files=files,
        )
    except HTTPException:
        raise
    except Exception as e:
        if "uq_skills_name_owner" in str(e):
            raise HTTPException(
                status_code=409, detail=f"You already have a skill named '{name}'"
            ) from e
        raise


@app.get("/api/skills")
async def list_skills(request: Request) -> list[dict[str, Any]]:
    """List skills: bundled (disk) + DB rows visible to the caller (owned + global),
    each tagged with ``source``. Read-only; tags-and-concatenates (precedence is a
    Slice-2 resolver concern)."""
    user = await require_approved_user(request, postgres_db)
    global _skills_cache
    if _skills_cache is None:
        _skills_cache = _scan_skills()
    result = [{**s.model_dump(), "source": "bundled"} for s in _skills_cache]
    if _is_skills_db_enabled():
        rows = await postgres_db.list_skills_visible(user_id=str(user["id"]))
        result += [
            {
                **_skill_row_to_meta(r),
                "source": "global" if r["is_global"] else "user",
            }
            for r in rows
        ]
    return result


@app.post("/api/skills/reload")
async def reload_skills(request: Request) -> dict[str, Any]:
    """Force reload of bundled skill cache. **Admin only**."""
    await _require_admin(request)
    global _skills_cache
    _skills_cache = _scan_skills()
    return {"status": "reloaded", "count": len(_skills_cache)}


@app.get("/api/skills/{skill_id}")
async def get_skill(request: Request, skill_id: str) -> dict[str, Any]:
    """Full skill detail (metadata + file tree). DB skill by UUID, else bundled."""
    await require_approved_user(request, postgres_db)
    if _is_skills_db_enabled() and _looks_like_uuid(skill_id):
        row = await postgres_db.get_skill_by_id(skill_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Skill not found: {skill_id}")
        files = await postgres_db.get_skill_files(skill_id)
        return {
            **_skill_row_to_meta(row),
            "source": "global" if row["is_global"] else "user",
            "files": files,
        }
    bundle = _bundled_skill_bundle(skill_id)
    if not bundle:
        raise HTTPException(status_code=404, detail=f"Skill not found: {skill_id}")
    return {**bundle, "source": "bundled"}
```

- [x] **Step 2: Add the update and delete endpoints**

```python
@app.put("/api/skills/{skill_id}")
async def update_skill(
    request: Request, skill_id: str, body: SkillUpdate
) -> dict[str, Any]:
    """Update an owned DB skill (owner or admin). Bundled skills are read-only.
    ``name`` is immutable — an edited SKILL.md whose frontmatter name differs is
    rejected (rename = create a new skill)."""
    _require_skills_db()
    user = await require_approved_user(request, postgres_db)
    if not _looks_like_uuid(skill_id):
        raise HTTPException(status_code=403, detail="Bundled skills are read-only")
    existing = await postgres_db.get_skill_by_id(skill_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Skill not found")
    if str(existing["owner_id"]) != str(user["id"]) and not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Only the owner may edit this skill")
    fields = body.model_dump(exclude_unset=True, exclude={"files"})
    files = body.files
    if files is not None:
        name, description, files = _parse_skill_bundle(files)
        if name != existing["name"]:
            raise HTTPException(
                status_code=422,
                detail=f"SKILL.md name '{name}' must match the skill's name "
                f"'{existing['name']}'; create a new skill to rename",
            )
        fields["description"] = description
    return await postgres_db.update_skill(
        skill_id, updated_by=str(user["id"]), files=files, **fields
    )


@app.delete("/api/skills/{skill_id}")
async def delete_skill(request: Request, skill_id: str) -> dict[str, Any]:
    """Delete an owned DB skill (owner or admin). Files cascade away."""
    _require_skills_db()
    user = await require_approved_user(request, postgres_db)
    if not _looks_like_uuid(skill_id):
        raise HTTPException(status_code=403, detail="Bundled skills cannot be deleted")
    existing = await postgres_db.get_skill_by_id(skill_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Skill not found")
    if str(existing["owner_id"]) != str(user["id"]) and not user.get("is_admin"):
        raise HTTPException(
            status_code=403, detail="Only the owner may delete this skill"
        )
    await postgres_db.delete_skill(skill_id)
    return {"deleted": True}
```

- [x] **Step 3: Smoke-check the app imports**

Run: `python -c "import orchestrator.main"`
Expected: no import error (route registration succeeds).

- [x] **Step 4: Commit**

```bash
git add orchestrator/main.py
git commit -m "feat(skills): CRUD endpoints (create/list/get/update/delete/reload)"
```

---

## Task 6: Orchestrator — duplicate + zip export + zip import

Export/import diverge from experts: **native zip**, not JSON. Export returns a `Response` with `application/zip`; import accepts a multipart `UploadFile`.

**Files:**
- Modify: `orchestrator/main.py` (after the CRUD endpoints from Task 5)

- [x] **Step 1: Ensure the FastAPI imports are present**

Confirm `File`, `UploadFile`, and `Response` are imported at the top of `main.py`. If any are missing, add to the existing `from fastapi import ...` line.

Run: `python -c "from fastapi import File, UploadFile, Response; print('ok')"`
Expected: `ok`

- [x] **Step 2: Add duplicate, export, import endpoints**

```python
@app.post("/api/skills/{skill_id}/duplicate")
async def duplicate_skill(request: Request, skill_id: str) -> dict[str, Any]:
    """Fork any visible skill (bundled or DB) into an owned copy."""
    _require_skills_db()
    user = await require_approved_user(request, postgres_db)
    if _looks_like_uuid(skill_id):
        row = await postgres_db.get_skill_by_id(skill_id)
        if not row:
            raise HTTPException(status_code=404, detail="Skill not found")
        src = {**_skill_row_to_meta(row), "files": await postgres_db.get_skill_files(skill_id)}
    else:
        src = _bundled_skill_bundle(skill_id)
        if not src:
            raise HTTPException(status_code=404, detail="Skill not found")
    return await _create_forked_skill(src, str(user["id"]))


@app.get("/api/skills/{skill_id}/export")
async def export_skill(request: Request, skill_id: str) -> Response:
    """Serialize a skill to a native zipped directory (drops into .claude/skills)."""
    from src.core.skill_format import pack_skill_zip

    _require_skills_db()
    await require_approved_user(request, postgres_db)
    if _looks_like_uuid(skill_id):
        row = await postgres_db.get_skill_by_id(skill_id)
        if not row:
            raise HTTPException(status_code=404, detail="Skill not found")
        name, files = row["name"], await postgres_db.get_skill_files(skill_id)
    else:
        bundle = _bundled_skill_bundle(skill_id)
        if not bundle:
            raise HTTPException(status_code=404, detail="Skill not found")
        name, files = bundle["name"], bundle["files"]
    return Response(
        content=pack_skill_zip(name, files),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}.zip"'},
    )


@app.post("/api/skills/import")
async def import_skill(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    """Create an owned skill from an uploaded skill zip (fork-on-name-collision)."""
    from src.core.skill_format import SkillFormatError, unpack_skill_zip

    _require_skills_db()
    user = await require_approved_user(request, postgres_db)
    try:
        files = unpack_skill_zip(await file.read())
    except SkillFormatError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    name, description, files = _parse_skill_bundle(files)
    src = {
        "name": name,
        "display_name": name.replace("-", " ").title(),
        "description": description,
        "files": files,
    }
    return await _create_forked_skill(src, str(user["id"]), suffix="import")
```

> Reusing `_create_forked_skill` for import keeps the collision/`set_skill_name` logic in one place; the imported copy's slug becomes `<name>-import` only if `<name>` is already taken by this owner.

- [x] **Step 3: Smoke-check imports**

Run: `python -c "import orchestrator.main"`
Expected: no error.

- [x] **Step 4: Commit**

```bash
git add orchestrator/main.py
git commit -m "feat(skills): duplicate + native-zip export/import endpoints"
```

---

## Task 7: Bundled example skill + MCP read tools

A bundled skill proves the scanner/bundled path end-to-end; the MCP tools mirror `list_experts`/`get_expert`/`reload_experts` (`server.py:961,976,1400`; `client.py:1096,1103,1143`).

**Files:**
- Create: `config/skills/hello-skill/SKILL.md`
- Modify: `orchestrator/mcp/server.py`, `orchestrator/mcp/client.py`

- [x] **Step 1: Create the bundled example skill**

```markdown
---
name: hello-skill
description: Use when the user explicitly asks to test the skills system end-to-end.
---

# Hello Skill

A minimal bundled skill that proves discovery and the bundled directory path.

## Steps
1. Confirm you can see this skill in the catalog.
2. Report its name and description back to the user.
```

- [x] **Step 2: Add client methods (`orchestrator/mcp/client.py`, after `reload_experts` ~1143)**

```python
    @_create_retry_decorator()
    async def list_skills(self) -> list[dict[str, Any]]:
        """List available skills (bundled + DB)."""
        resp = await self._client.get("/api/skills")
        resp.raise_for_status()
        return resp.json()

    @_create_retry_decorator()
    async def get_skill(self, skill_id: str) -> dict[str, Any]:
        """Get full skill detail (metadata + files)."""
        resp = await self._client.get(f"/api/skills/{skill_id}")
        resp.raise_for_status()
        return resp.json()

    async def reload_skills(self) -> dict[str, Any]:
        """Force reload of bundled skills from disk."""
        resp = await self._client.post("/api/skills/reload")
        resp.raise_for_status()
        return resp.json()
```

- [x] **Step 3: Add server tools (`orchestrator/mcp/server.py`, near `reload_experts` ~1400)**

```python
@mcp.tool
async def list_skills() -> str:
    """List available agent skills (the catalog the agent selects from).

    Returns:
        Skills with id, name, description, and tags.
    """
    client = _get_client()
    try:
        skills = await client.list_skills()
        return fmt.format_skills(skills)
    except Exception as e:
        return fmt.format_monitoring_error("list skills", e)


@mcp.tool
async def get_skill(skill_id: str) -> str:
    """Get full detail for a skill including its SKILL.md body and file list.

    Args:
        skill_id: Skill id (bundled name or DB UUID).

    Returns:
        The skill's metadata, body, and bundled file paths.
    """
    client = _get_client()
    try:
        data = await client.get_skill(skill_id)
        return fmt.format_skill_detail(skill_id, data)
    except Exception as e:
        return fmt.format_monitoring_error(f"get skill '{skill_id}'", e)


@mcp.tool
async def reload_skills() -> str:
    """Force reload of bundled skills from disk.

    Returns:
        Reload confirmation with skill count.
    """
    client = _get_client()
    result = await client.reload_skills()
    return f"Skills reloaded ({result.get('count', 0)} bundled skills loaded)."
```

- [x] **Step 4: Add the `fmt` formatters used above**

Find the module the experts formatters live in (`fmt.format_experts` / `fmt.format_expert_detail` — grep `def format_experts` under `orchestrator/mcp/`). Add sibling formatters mirroring them:

```python
def format_skills(skills: list[dict]) -> str:
    if not skills:
        return "No skills found."
    lines = ["# Skills", ""]
    for s in skills:
        src = s.get("source", "")
        lines.append(f"- **{s.get('name')}** ({src}) — {s.get('description', '')}")
    return "\n".join(lines)


def format_skill_detail(skill_id: str, data: dict) -> str:
    files = data.get("files", {})
    body = files.get("SKILL.md", "")
    paths = ", ".join(sorted(p for p in files if p != "SKILL.md")) or "(none)"
    return (
        f"# Skill: {data.get('name', skill_id)}\n\n"
        f"{data.get('description', '')}\n\n"
        f"**Bundled files:** {paths}\n\n"
        f"---\n{body}"
    )
```

- [x] **Step 5: Smoke-check imports**

Run: `python -c "import orchestrator.mcp.server, orchestrator.mcp.client"`
Expected: no error.

- [x] **Step 6: Commit**

```bash
git add config/skills/hello-skill/SKILL.md orchestrator/mcp/server.py orchestrator/mcp/client.py
git commit -m "feat(skills): bundled example skill + read-only MCP tools"
```

---

## Task 8: Cockpit — DTOs + ApiService methods

Mirror the experts DTOs (`api.model.ts`) and service methods (`api.service.ts:473-530`). Export returns a **Blob**; import posts **FormData**.

**Files:**
- Modify: `cockpit/src/app/core/models/api.model.ts`
- Modify: `cockpit/src/app/core/services/api.service.ts`

- [x] **Step 1: Add the DTOs (`api.model.ts`, near the `Expert` interfaces)**

```typescript
export interface Skill {
  id: string;
  name: string;
  display_name: string;
  description: string;
  icon: string;
  color: string;
  tags: string[];
  source?: string; // 'bundled' | 'user' | 'global'
}

export interface SkillDetail extends Skill {
  files: Record<string, string>;
  version?: number;
  owner_id?: string;
}

export interface SkillCreateRequest {
  files: Record<string, string>;
  display_name?: string | null;
  icon?: string;
  color?: string;
  tags?: string[];
}

export interface SkillUpdateRequest {
  files?: Record<string, string>;
  display_name?: string;
  icon?: string;
  color?: string;
  tags?: string[];
  is_global?: boolean;
}
```

- [x] **Step 2: Add the service methods (`api.service.ts`, after the experts methods ~530)**

```typescript
  /** List skills (bundled + DB-backed). Fails gracefully to []. */
  getSkills(): Observable<Skill[]> {
    return this.http.get<Skill[]>(`${this.baseUrl}/skills`).pipe(catchError(() => of([])));
  }

  /** Full skill detail incl. the file tree. */
  getSkillDetail(id: string): Observable<SkillDetail | null> {
    return this.http
      .get<SkillDetail>(`${this.baseUrl}/skills/${id}`)
      .pipe(catchError(() => of(null)));
  }

  /** Create a DB skill. Errors propagate (409 name collision / 422 malformed). */
  createSkill(body: SkillCreateRequest): Observable<SkillDetail> {
    return this.http.post<SkillDetail>(`${this.baseUrl}/skills`, body);
  }

  /** Update an owned DB skill. Bumps version. */
  updateSkill(id: string, body: SkillUpdateRequest): Observable<SkillDetail> {
    return this.http.put<SkillDetail>(`${this.baseUrl}/skills/${id}`, body);
  }

  /** Delete an owned DB skill. */
  deleteSkill(id: string): Observable<{deleted: boolean}> {
    return this.http.delete<{deleted: boolean}>(`${this.baseUrl}/skills/${id}`);
  }

  /** Fork any visible skill into an owned copy. */
  duplicateSkill(id: string): Observable<SkillDetail> {
    return this.http.post<SkillDetail>(`${this.baseUrl}/skills/${id}/duplicate`, {});
  }

  /** Download a skill as a native zip (drops into .claude/skills). */
  exportSkill(id: string): Observable<Blob> {
    return this.http.get(`${this.baseUrl}/skills/${id}/export`, {responseType: 'blob'});
  }

  /** Import a skill from an uploaded zip (fork-on-collision). */
  importSkill(file: File): Observable<SkillDetail> {
    const fd = new FormData();
    fd.append('file', file);
    return this.http.post<SkillDetail>(`${this.baseUrl}/skills/import`, fd);
  }
```

Ensure `Skill`, `SkillDetail`, `SkillCreateRequest`, `SkillUpdateRequest` are added to the existing api.model import in `api.service.ts`.

- [x] **Step 3: Build-check the types**

Run: `cd cockpit && npx tsc --noEmit -p tsconfig.json`
Expected: no new type errors from these files.

- [x] **Step 4: Commit**

```bash
git add cockpit/src/app/core/models/api.model.ts cockpit/src/app/core/services/api.service.ts
git commit -m "feat(skills): cockpit DTOs + ApiService methods"
```

---

## Task 9: Cockpit — editor utilities (pure, TDD)

The editor's pure helpers (slugify, files-array ↔ record, starter template) are unit-tested before the components.

**Files:**
- Create: `cockpit/src/app/views/skills/skill-editor.util.ts`
- Test: `cockpit/src/app/views/skills/skill-editor.util.spec.ts`

- [x] **Step 1: Write the failing tests**

```typescript
// cockpit/src/app/views/skills/skill-editor.util.spec.ts
import {filesToRecord, recordToFiles, hasSkillMd, NEW_SKILL_TEMPLATE} from './skill-editor.util';

describe('skill-editor.util', () => {
  it('round-trips files array <-> record', () => {
    const arr = [
      {path: 'SKILL.md', content: 'a'},
      {path: 'references/x.md', content: 'b'},
    ];
    expect(recordToFiles(filesToRecord(arr))).toEqual(arr);
  });

  it('hasSkillMd detects the canonical file', () => {
    expect(hasSkillMd([{path: 'SKILL.md', content: 'x'}])).toBe(true);
    expect(hasSkillMd([{path: 'references/x.md', content: 'x'}])).toBe(false);
  });

  it('the new-skill template is a valid SKILL.md skeleton', () => {
    expect(NEW_SKILL_TEMPLATE).toContain('---');
    expect(NEW_SKILL_TEMPLATE).toContain('name:');
    expect(NEW_SKILL_TEMPLATE).toContain('description:');
  });
});
```

- [x] **Step 2: Run to verify failure**

Run: `cd cockpit && npx vitest run src/app/views/skills/skill-editor.util.spec.ts`
Expected: FAIL (module not found).

- [x] **Step 3: Write the util**

```typescript
// cockpit/src/app/views/skills/skill-editor.util.ts
export interface SkillFile {
  path: string;
  content: string;
}

export const NEW_SKILL_TEMPLATE = `---
name: my-skill
description: Use when ... (third person; state when to use it).
---

# My Skill

## Overview
What this is, in one or two sentences.

## Steps
1. ...
`;

/** Array form (editor state) -> record form (API payload), sorted by path. */
export function filesToRecord(files: SkillFile[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const f of files) out[f.path] = f.content;
  return out;
}

/** Record form -> array form, sorted by path (SKILL.md first for display). */
export function recordToFiles(rec: Record<string, string>): SkillFile[] {
  return Object.keys(rec)
    .sort((a, b) => (a === 'SKILL.md' ? -1 : b === 'SKILL.md' ? 1 : a.localeCompare(b)))
    .map((path) => ({path, content: rec[path]}));
}

export function hasSkillMd(files: SkillFile[]): boolean {
  return files.some((f) => f.path === 'SKILL.md');
}
```

> `recordToFiles` sorts SKILL.md first for display; the round-trip test compares against an array that is already in that order. If you add files out of order in the editor, persist via `filesToRecord` (order-independent).

- [x] **Step 4: Run to verify pass**

Run: `cd cockpit && npx vitest run src/app/views/skills/skill-editor.util.spec.ts`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add cockpit/src/app/views/skills/skill-editor.util.ts cockpit/src/app/views/skills/skill-editor.util.spec.ts
git commit -m "feat(skills): cockpit editor utilities (files<->record, template)"
```

---

## Task 10: Cockpit — page, list, and multi-file editor components

Mirror the experts components' patterns (standalone, signals, Transloco, the list's action/import/export/delete wiring) but with the multi-file editor body. Components reuse the existing shared `app-*` UI primitives (button, input, textarea, icon, dialog, badge) — confirm their import paths against `experts-list.component.ts` / `expert-editor.component.ts`.

**Files:**
- Create: `cockpit/src/app/views/skills/skills-page.component.ts`
- Create: `cockpit/src/app/views/skills/skills-list.component.ts`
- Create: `cockpit/src/app/views/skills/skill-editor.component.ts`
- Modify: `cockpit/src/app/app.routes.ts`
- Modify: `cockpit/src/app/shell/sidebar/sidebar.component.ts`
- Modify: `cockpit/src/assets/i18n/en.json`

- [x] **Step 1: Add the page container**

```typescript
// cockpit/src/app/views/skills/skills-page.component.ts
import {Component} from '@angular/core';
import {SkillsListComponent} from './skills-list.component';

@Component({
  selector: 'app-skills-page',
  standalone: true,
  imports: [SkillsListComponent],
  template: `<app-skills-list />`,
})
export class SkillsPageComponent {}
```

- [x] **Step 2: Add the list component**

```typescript
// cockpit/src/app/views/skills/skills-list.component.ts
import {Component, OnInit, inject, signal} from '@angular/core';
import {Router} from '@angular/router';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {ApiService} from '../../core/services/api.service';
import {Skill} from '../../core/models/api.model';
// Import the same shared UI primitives experts-list uses (verify paths):
import {AppButtonComponent} from '../../shared/ui/button/button.component';
import {AppIconButtonComponent} from '../../shared/ui/icon-button/icon-button.component';
import {AppBadgeComponent} from '../../shared/ui/badge/badge.component';
import {AppIconComponent} from '../../shared/ui/icon/icon.component';
import {AppSpinnerComponent} from '../../shared/ui/spinner/spinner.component';
import {AppDialogComponent} from '../../shared/ui/dialog/dialog.component';

@Component({
  selector: 'app-skills-list',
  standalone: true,
  imports: [
    TranslocoPipe,
    AppButtonComponent,
    AppIconButtonComponent,
    AppBadgeComponent,
    AppIconComponent,
    AppSpinnerComponent,
    AppDialogComponent,
  ],
  template: `
    <div class="header">
      <h1>{{ 'skills.title' | transloco }}</h1>
      <div class="actions">
        <label class="import-label">
          <input type="file" accept=".zip" hidden (change)="onImport($event)" />
          <app-button>{{ 'skills.import' | transloco }}</app-button>
        </label>
        <app-button (click)="newSkill()">{{ 'skills.new' | transloco }}</app-button>
      </div>
    </div>
    @if (loading()) {
      <app-spinner />
    } @else if (!rows().length) {
      <p class="empty">{{ 'skills.empty' | transloco }}</p>
    } @else {
      <table>
        <thead>
          <tr>
            <th>{{ 'skills.colName' | transloco }}</th>
            <th>{{ 'skills.colSource' | transloco }}</th>
            <th>{{ 'skills.colActions' | transloco }}</th>
          </tr>
        </thead>
        <tbody>
          @for (s of rows(); track s.id) {
            <tr>
              <td>
                <app-icon>{{ s.icon }}</app-icon> {{ s.display_name }}
                <small>{{ s.description }}</small>
              </td>
              <td><app-badge>{{ s.source }}</app-badge></td>
              <td>
                <app-icon-button icon="content_copy" (click)="duplicate(s)" />
                <app-icon-button icon="download" (click)="exportSkill(s)" />
                @if (s.source !== 'bundled') {
                  <app-icon-button icon="edit" (click)="edit(s)" />
                  <app-icon-button icon="delete" (click)="askDelete(s)" />
                }
              </td>
            </tr>
          }
        </tbody>
      </table>
    }
    @if (successMessage()) { <p class="ok">{{ successMessage() }}</p> }
    @if (errorMessage()) { <p class="err">{{ errorMessage() }}</p> }
    <app-dialog [open]="confirmOpen()" (closed)="confirmOpen.set(false)">
      <p>{{ 'skills.confirmDeleteBody' | transloco }} "{{ pendingDelete()?.display_name }}"?</p>
      <app-button (click)="confirmDelete()">{{ 'skills.delete' | transloco }}</app-button>
      <app-button (click)="confirmOpen.set(false)">{{ 'skills.cancel' | transloco }}</app-button>
    </app-dialog>
  `,
})
export class SkillsListComponent implements OnInit {
  private api = inject(ApiService);
  private router = inject(Router);
  private transloco = inject(TranslocoService);

  rows = signal<Skill[]>([]);
  loading = signal(true);
  successMessage = signal('');
  errorMessage = signal('');
  confirmOpen = signal(false);
  pendingDelete = signal<Skill | null>(null);

  ngOnInit(): void {
    this.refresh();
  }

  refresh(): void {
    this.loading.set(true);
    this.api.getSkills().subscribe((rows) => {
      this.rows.set(rows);
      this.loading.set(false);
    });
  }

  newSkill(): void {
    this.router.navigate(['/skills/new']);
  }

  edit(s: Skill): void {
    this.router.navigate(['/skills', s.id, 'edit']);
  }

  duplicate(s: Skill): void {
    this.api.duplicateSkill(s.id).subscribe({
      next: () => {
        this.successMessage.set(this.transloco.translate('skills.duplicated'));
        this.refresh();
      },
      error: () => this.errorMessage.set(this.transloco.translate('skills.saveFailed')),
    });
  }

  exportSkill(s: Skill): void {
    this.api.exportSkill(s.id).subscribe((blob) => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${s.name}.zip`;
      a.click();
      URL.revokeObjectURL(url);
    });
  }

  onImport(ev: Event): void {
    const input = ev.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    this.api.importSkill(file).subscribe({
      next: () => {
        this.successMessage.set(this.transloco.translate('skills.imported'));
        this.refresh();
      },
      error: (err) =>
        this.errorMessage.set(
          (err?.error?.detail as string) ?? this.transloco.translate('skills.invalidZip'),
        ),
    });
    input.value = '';
  }

  askDelete(s: Skill): void {
    this.pendingDelete.set(s);
    this.confirmOpen.set(true);
  }

  confirmDelete(): void {
    const s = this.pendingDelete();
    if (!s) return;
    this.api.deleteSkill(s.id).subscribe({
      next: () => {
        this.confirmOpen.set(false);
        this.successMessage.set(this.transloco.translate('skills.deleted'));
        this.refresh();
      },
      error: () => this.confirmOpen.set(false),
    });
  }
}
```

- [x] **Step 3: Add the multi-file editor component**

```typescript
// cockpit/src/app/views/skills/skill-editor.component.ts
import {Component, OnInit, computed, inject, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {ActivatedRoute, Router} from '@angular/router';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {ApiService} from '../../core/services/api.service';
import {SkillCreateRequest, SkillUpdateRequest} from '../../core/models/api.model';
import {AppButtonComponent} from '../../shared/ui/button/button.component';
import {AppInputComponent} from '../../shared/ui/input/input.component';
import {AppTextareaComponent} from '../../shared/ui/textarea/textarea.component';
import {AppIconButtonComponent} from '../../shared/ui/icon-button/icon-button.component';
import {
  NEW_SKILL_TEMPLATE,
  SkillFile,
  filesToRecord,
  hasSkillMd,
  recordToFiles,
} from './skill-editor.util';

@Component({
  selector: 'app-skill-editor',
  standalone: true,
  imports: [
    FormsModule,
    TranslocoPipe,
    AppButtonComponent,
    AppInputComponent,
    AppTextareaComponent,
    AppIconButtonComponent,
  ],
  template: `
    <h1>{{ (isEdit() ? 'skills.editTitle' : 'skills.newTitle') | transloco }}</h1>

    <app-input [(ngModel)]="form.display_name" [label]="'skills.displayName' | transloco" />
    <app-input [(ngModel)]="form.icon" [label]="'skills.icon' | transloco" />
    <app-input [(ngModel)]="form.color" [label]="'skills.color' | transloco" />
    <app-input [(ngModel)]="form.tags" [label]="'skills.tags' | transloco" />

    <div class="files">
      <div class="file-tabs">
        @for (f of files(); track f.path; let i = $index) {
          <button
            class="file-tab"
            [class.active]="i === selected()"
            (click)="selected.set(i)"
          >
            {{ f.path }}
            @if (f.path !== 'SKILL.md') {
              <app-icon-button icon="close" (click)="removeFile(i, $event)" />
            }
          </button>
        }
        <app-button (click)="addFile()">+ {{ 'skills.addFile' | transloco }}</app-button>
      </div>
      <app-textarea
        [ngModel]="currentContent()"
        (ngModelChange)="setContent($event)"
        rows="24"
        [monospace]="true"
      />
    </div>

    @if (errorMessage()) { <p class="err">{{ errorMessage() }}</p> }
    <div class="actions">
      <app-button [disabled]="saving()" (click)="save()">{{ 'skills.save' | transloco }}</app-button>
      <app-button (click)="cancel()">{{ 'skills.cancel' | transloco }}</app-button>
    </div>
  `,
})
export class SkillEditorComponent implements OnInit {
  private api = inject(ApiService);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private transloco = inject(TranslocoService);

  editingId = signal<string | null>(null);
  saving = signal(false);
  errorMessage = signal('');
  files = signal<SkillFile[]>([{path: 'SKILL.md', content: NEW_SKILL_TEMPLATE}]);
  selected = signal(0);

  form = {display_name: '', icon: 'extension', color: '#6B7280', tags: ''};

  isEdit = computed(() => this.editingId() !== null);
  currentContent = computed(() => this.files()[this.selected()]?.content ?? '');

  ngOnInit(): void {
    const id = this.route.snapshot.paramMap.get('id');
    if (!id) return;
    this.editingId.set(id);
    this.api.getSkillDetail(id).subscribe((d) => {
      if (!d) return;
      this.form.display_name = d.display_name ?? '';
      this.form.icon = d.icon ?? 'extension';
      this.form.color = d.color ?? '#6B7280';
      this.form.tags = (d.tags ?? []).join(', ');
      this.files.set(recordToFiles(d.files ?? {}));
      this.selected.set(0);
    });
  }

  setContent(v: string): void {
    this.files.update((fs) => fs.map((f, i) => (i === this.selected() ? {...f, content: v} : f)));
  }

  addFile(): void {
    const path = prompt(this.transloco.translate('skills.newFilePath'), 'references/guide.md');
    if (!path || this.files().some((f) => f.path === path)) return;
    this.files.update((fs) => [...fs, {path, content: ''}]);
    this.selected.set(this.files().length - 1);
  }

  removeFile(i: number, ev: Event): void {
    ev.stopPropagation();
    this.files.update((fs) => fs.filter((_, idx) => idx !== i));
    this.selected.set(0);
  }

  save(): void {
    this.errorMessage.set('');
    if (!hasSkillMd(this.files())) {
      this.errorMessage.set(this.transloco.translate('skills.needSkillMd'));
      return;
    }
    const tags = this.form.tags.split(',').map((t) => t.trim()).filter(Boolean);
    const files = filesToRecord(this.files());
    this.saving.set(true);
    const id = this.editingId();
    const obs = id
      ? this.api.updateSkill(id, {
          files,
          display_name: this.form.display_name,
          icon: this.form.icon,
          color: this.form.color,
          tags,
        } as SkillUpdateRequest)
      : this.api.createSkill({
          files,
          display_name: this.form.display_name || null,
          icon: this.form.icon,
          color: this.form.color,
          tags,
        } as SkillCreateRequest);
    obs.subscribe({
      next: () => this.router.navigate(['/skills']),
      error: (err) => {
        this.saving.set(false);
        const d = (err as {error?: {detail?: unknown}})?.error?.detail;
        this.errorMessage.set(
          typeof d === 'string' ? d : this.transloco.translate('skills.saveFailed'),
        );
      },
    });
  }

  cancel(): void {
    this.router.navigate(['/skills']);
  }
}
```

> If `app-textarea` has no `monospace` input, drop it or add a `class` binding — match the actual `AppTextareaComponent` API used in `expert-editor.component.ts`. `prompt()` for the new-file path is a pragmatic Slice-1 affordance; a proper inline field is a polish item.

- [x] **Step 4: Register routes (`app.routes.ts`)**

```typescript
import {SkillsPageComponent} from './views/skills/skills-page.component';
import {SkillEditorComponent} from './views/skills/skill-editor.component';

// in the routes array, next to the experts routes:
{ path: 'skills', component: SkillsPageComponent, canActivate: [authGuard] },
{ path: 'skills/new', component: SkillEditorComponent, canActivate: [authGuard] },
{ path: 'skills/:id/edit', component: SkillEditorComponent, canActivate: [authGuard] },
```

- [x] **Step 5: Add the nav entry (`sidebar.component.ts`, next to the experts link ~86-93)**

```html
<a class="nav-link" routerLink="/skills" routerLinkActive="active">
  <app-icon size="md" class="nav-icon">extension</app-icon>
  {{ 'nav.skills' | transloco }}
</a>
```

- [x] **Step 6: Add i18n strings (`en.json`)**

```json
"skills": {
  "title": "Skills",
  "new": "New Skill",
  "newTitle": "New Skill",
  "editTitle": "Edit Skill",
  "import": "Import",
  "empty": "No skills yet. Create one or import a Claude Code skill.",
  "colName": "Name",
  "colSource": "Source",
  "colActions": "Actions",
  "displayName": "Display name",
  "icon": "Icon",
  "color": "Color",
  "tags": "Tags (comma-separated)",
  "addFile": "Add file",
  "newFilePath": "New file path (e.g. references/guide.md)",
  "needSkillMd": "A skill must contain a SKILL.md file",
  "edit": "Edit",
  "duplicate": "Duplicate",
  "export": "Export",
  "delete": "Delete",
  "cancel": "Cancel",
  "save": "Save",
  "confirmDeleteBody": "This permanently deletes",
  "deleted": "Skill deleted",
  "duplicated": "Skill duplicated",
  "imported": "Skill imported",
  "saveFailed": "Save failed",
  "invalidZip": "Invalid skill archive"
}
```

Also add `"skills": "Skills"` under the existing `nav` block.

- [x] **Step 7: Build-check Cockpit**

Run: `cd cockpit && npm install --no-save @monaco-editor/loader && npx ng build`
Expected: build succeeds. (Per repo notes, `ng build` needs the monaco loader installed first; vitest is reliable for unit tests.)

- [x] **Step 8: Commit**

```bash
git add cockpit/src/app/views/skills/ cockpit/src/app/app.routes.ts cockpit/src/app/shell/sidebar/sidebar.component.ts cockpit/src/assets/i18n/en.json
git commit -m "feat(skills): cockpit skills page, list, and multi-file editor"
```

---

## Task 11: Helm — `SKILLS_DB_ENABLED` flag wiring

Mirror `EXPERTS_DB_ENABLED` exactly (`values.yaml:136`, `configmap.yaml:57`, `orchestrator/deployment.yaml:108-112`).

**Files:**
- Modify: `helm/values.yaml`
- Modify: `helm/templates/configmap.yaml`
- Modify: `helm/templates/orchestrator/deployment.yaml`

- [x] **Step 1: Add the helm value (`values.yaml`, next to `expertsDbEnabled` ~136)**

```yaml
  # DB-backed Agent Skills (Slice 1). Prod-safe default off; the dev values
  # file turns it on. Mirrors expertsDbEnabled.
  skillsDbEnabled: "false"
```

- [x] **Step 2: Map it in the ConfigMap (`configmap.yaml`, next to line 57)**

```yaml
  SKILLS_DB_ENABLED: {{ .Values.agent.skillsDbEnabled | default "false" | quote }}
```

- [x] **Step 3: Reference it in the orchestrator Deployment env (`orchestrator/deployment.yaml`, next to lines 108-112)**

```yaml
            - name: SKILLS_DB_ENABLED
              valueFrom:
                configMapKeyRef:
                  name: {{ include "srw.configMapName" . }}
                  key: SKILLS_DB_ENABLED
```

- [x] **Step 4: Lint the chart**

Run: `helm lint helm/ && helm template helm/ | grep -A4 SKILLS_DB_ENABLED`
Expected: lint passes; the env block + configmap key render.

> Also set `skillsDbEnabled: "true"` in the in-repo dev values that turn `expertsDbEnabled` on: `deployment/values-local.yaml` (Tilt/local k3d) and `deployment/values-experimental.yaml` (develop→Fleet→dev cluster). **DONE** — both committed; `helm template` confirms `SKILLS_DB_ENABLED: "true"` renders in the configmap + orchestrator env.

- [x] **Step 5: Commit**

```bash
git add helm/values.yaml helm/templates/configmap.yaml helm/templates/orchestrator/deployment.yaml
git commit -m "feat(skills): wire SKILLS_DB_ENABLED through helm (dev-on/prod-off)"
```

---

## Task 12: Full test sweep + live verification on k3d

**Files:** none (verification only)

- [x] **Step 1: Backend tests + lint**

Run:
```bash
python -m pytest tests/test_skill_format.py tests/test_skill_crud.py -v
ruff check src/core/skill_format.py orchestrator/main.py orchestrator/database/postgres.py orchestrator/mcp/
```
Expected: all tests pass; ruff clean (the push workflow runs ruff and will rewrite SHAs otherwise).

- [x] **Step 2: Cockpit tests + build**

Run:
```bash
cd cockpit && npx vitest run src/app/views/skills/ && npx ng build
```
Expected: skills unit tests pass; build succeeds.

- [x] **Step 3: Deploy to local k3d with the flag on**

Bring up / sync the local tilt stack with `SKILLS_DB_ENABLED=true` for the orchestrator, and apply migration `0031`. Confirm:
```bash
# the bundled skill is discoverable even before any DB row:
curl -s -H "X-Internal-Key: dev_mcp_internal_key" http://<orch>/api/skills | jq '.[].name'
```
Expected: includes `hello-skill` (source `bundled`).

> Per repo notes, the orchestrator MCP points at the remote cluster; to drive *local* k3d code use `X-Internal-Key: dev_mcp_internal_key` + in-pod `python3 urllib`, and log in via Cockpit once to seed the first user. A fresh cluster also needs an LLM provider for the readiness gate.

- [x] **Step 4: End-to-end authoring round-trip (the DoD)**

In Cockpit (logged in, flag on):
1. **Create from scratch** — `/skills/new`, fill display name, edit the SKILL.md body, add a `references/guide.md` file, Save → appears in the list as `user`.
2. **Edit** — open it, change the body, Save → reload shows the change; `version` incremented.
3. **Import a real Claude Code skill** — download a skill from `github.com/anthropics/skills` (e.g. a prompt-only one), zip its directory, Import → it appears.
4. **Byte-comparable export** — Export the just-imported skill, unzip, and diff each file against the original:
   ```bash
   diff -r <original-skill-dir> <unzipped-export>/<skill-name>
   ```
   Expected: **no differences** (lossless storage proven).
5. **Duplicate** — duplicate the imported skill → a `-copy` appears; open it and confirm its SKILL.md frontmatter `name` was rewritten to the copy's slug.
6. **Delete** — delete a user skill → gone from the list; confirm `skill_files` rows are gone:
   ```bash
   psql "$DATABASE_URL" -c "SELECT count(*) FROM skill_files WHERE skill_id = '<deleted-id>';"
   ```
   Expected: `0`.

- [x] **Step 5: Negative checks**

- Importing a zip with no `SKILL.md` → 422 with a clear message.
- Importing a zip with a `../escape` path → 422.
- Editing a skill and changing the frontmatter `name` → 422 ("must match the skill's name").
- A SKILL.md frontmatter with a `connections:` block → 422 (deny-scan).

- [x] **Step 6: Final commit (if any verification fixes were needed)**

```bash
git add -A
git commit -m "fix(skills): address Slice-1 live-verification findings"
```

---

## Self-review notes (author)

**Spec coverage** — every Slice-1 line in `agent_skills.md` maps to a task:
- `SKILLS_DB_ENABLED` + helm → Task 4 (flag), Task 11 (helm).
- bundled `config/skills/` + `skills` row + `skill_files` → Task 2 (schema), Task 3 (persistence), Task 7 (bundled example).
- `SKILL.md` parser/serializer → Task 1.
- CRUD + duplicate + native-zip import/export + edit → Tasks 5, 6.
- Cockpit editor cloned from experts → Tasks 8, 9, 10.
- `hard_deny_scan` → Task 4 (`_validate_skill_frontmatter`); **path validation** added (net-new for the file tree).
- DoD (byte-comparable round-trip; create/edit from scratch) → Task 12 steps 4.
- Project Gitea `skills/` scan is listed in the doc's storage section but is a **runtime/resolution** concern (no authoring surface) → deferred to **Slice 2** with the rest of the runtime. Flagged for review.

**Deviations from the design doc (flag at review):**
1. **`fence_persona` is NOT in Slice 1.** Fencing happens at prompt assembly (runtime); Slice 1 has no runtime. The doc's Slice-1 line says "fence_persona (at rest)" — corrected here to Slice 2. Save-time security in Slice 1 is `hard_deny_scan` + path validation.
2. **No `project_skills` junction, no `jobs.skill_id`** in migration `0031` — leaner than experts' `0028`; project scope and expert↔skill binding are later slices.
3. **`skill_files.content` is `TEXT` (UTF-8 only)** — binary assets deferred to Slice 4; the round-trip DoD uses a text/script-bearing skill (scripts are text).

**Type consistency** — `name`/`description` flow: parsed by `skill_identity` (Task 1) → `_parse_skill_bundle` (Task 4) → `create_skill`/`update_skill` (Task 3); the row's denormalized `description` is refreshed on every file-bearing save. `files` is `dict[str,str]` end-to-end (API ↔ DB ↔ `skill_format`), and `Record<string,string>` ↔ `SkillFile[]` on the Cockpit side (Task 9 helpers). `_create_forked_skill` (Task 4) is the single fork path used by both duplicate and import (Task 6).

**Placeholder scan** — every code step carries real, adapted code. UI primitive import paths and the `app-textarea` API are the only "verify against the experts component" notes (Task 10), because those are existing shared components whose exact paths must match the repo.

---

## As-built notes (post-implementation, 2026-06-18)

All 12 tasks executed inline on `develop` (commits `b117fb3f`..`d138d234`), plus two follow-ups. Every step above is checked. Where the build diverged from the written plan:

**Backend refinements**
- **`_create_forked_skill` gained `prefer_original`** (Task 4/6). Import tries the *source* name first and stores the `SKILL.md` **verbatim** (no frontmatter rewrite) so a clean `import → export` is byte-comparable; only on a name collision does it suffix + rewrite. Duplicate still always suffixes `-copy`. The written plan's simpler "always suffix" helper would have failed the byte-comparable DoD.
- `_parse_skill_bundle` enforces **path-traversal validation** in addition to `hard_deny_scan` (net-new vs experts, since skills carry a file tree).

**Cockpit as-built (corrections to the plan's draft component code, Task 10)** — verified against the live experts components + `ng build`:
- Shared UI primitives import from **`../../ui/<name>`** barrels (not `../../shared/ui/<name>/<name>.component`).
- Form controls use **`[value]` + `(valueChange)`**, *not* `[(ngModel)]` → **no `FormsModule`** import needed.
- Buttons: `variant="primary|secondary|danger"` + `(clicked)`. Icon-buttons: `size`/`variant`/`[ariaLabel]`/`[tooltip]` + `(clicked)` with a child `<app-icon>`. Dialog: `[open]`/`[title]`/`(closed)` + `appDialogActions`. Badge: `[tone]`. Page wrapper uses `SidebarToggleComponent`.
- Editor is a multi-file tab list + per-file `<app-textarea>`; add-file via `prompt()` (Slice-1 pragmatic affordance).

**Dev enablement (Task 11 follow-up)** — the flag is set **in-repo** (not external): `deployment/values-local.yaml` (Tilt/local k3d) and `deployment/values-experimental.yaml` (develop→Fleet→dev cluster), mirroring `expertsDbEnabled`. Chart default stays OFF (prod-safe).

**Bundle-budget follow-up** — `cockpit/angular.json` budgets right-sized: initial warn `1.5MB→2.25MB` / error `2.25MB→2.75MB`, component-style warn `32kB→36kB` / error `40kB→48kB`. The 2.07 MB raw initial bundle is **~420 kB gzipped transfer** (healthy); the stale 1.5 MB warning was pure noise and the 2.25 MB error sat ~180 kB above current (fragile). Route-level lazy loading remains the deeper raw-size lever if ever needed.

**Live verification (Task 12, §3–5) — DONE on k3d.** Tilt rebuilt + deployed the committed code; orchestrator listens on **:8085**; auth via the **MCP-header trick** (`X-Internal-Key: dev_mcp_internal_key` + `X-MCP-User-Id: <approved user>`). An in-pod urllib script ran **14/14 checks**: bundled `hello-skill` discoverable; create/get/update (version bump + file replacement)/delete; **byte-comparable `import → export`** and export-byte-comparable; duplicate→`-copy`; name parsed from frontmatter; negatives 422 (rename-via-frontmatter, missing `SKILL.md`). `skills`/`skill_files` tables confirmed present (migration `0031` applied on orchestrator start). The e2e script was a throwaway (`/tmp`, not committed); test rows cleaned up. Only a browser click-through of `/skills` remains optional.

**Deferred to later slices (unchanged):** persona fencing of menu/body (Slice 2 runtime), project-Gitea `skills/` scan + `use_skill` + workspace materialization (Slice 2), expert↔skill bindings + `todo_guide`/`research_guide` migration (Slice 3), script execution behind the grants `evaluate()` gate + binary assets (Slice 4).
