# Project Backlog / Idea Pipeline — Implementation Plan (sequential v1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the project loop's fictional "open backlog" — re-derived by similarity search on every job — with a real, ordered, queryable ticket pool made of OKF notes, injected verbatim into every loop kickoff.

**Architecture:** A ticket is an OKF note with a new `note_type` (`feature`/`issue`/`idea`) and a non-binding `priority` rank. The **pool** is an indexed SQL listing over `knowledge_index`; **in progress** is the loop's existing `campaign` object (whose `initiative_note_id` already points at a note), so the campaign engine is unchanged. The orchestrator fetches and renders the pool at spawn time and passes it into `build_loop_kickoff` as a pre-rendered string, keeping that function pure. Closing a campaign mirrors the ticket's status to **both** the markdown file in the jobs repo and the index row.

**Tech Stack:** Python 3.12 (FastAPI orchestrator), asyncpg/PostgreSQL + pgvector, pytest + AsyncMock, Angular 21/vitest (cockpit), SQL migrations under `orchestrator/database/migrations/vector/`.

**Spec:** `docs/superpowers/specs/2026-07-26-project-backlog-pipeline-design.md`

## Global Constraints

- Work directly on branch `develop`. No feature branches. **NEVER `git push`** — the user pushes explicitly.
- Every commit message ends with the trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- **The orchestrator cannot import from `src/`.** `orchestrator/` runs in an image without the agent's dependencies; importing `src.services.*` raises at runtime. Vocabularies needed on both sides are **duplicated with a comment naming the canonical copy** — this is the established pattern (`orchestrator/services/kb_reindex.py:66-68` duplicates the note types; the KB TTL SQL is inlined in `main.py` with the comment *"run inline — the orchestrator can't import src/"*).
- **Four note-type vocabulary sites must stay in sync.** `src/services/knowledge_graph.py:46` (`NOTE_TYPES`, canonical) · `src/tools/knowledge/knowledge_tools.py:60` (`NoteTypeValue`) · `orchestrator/services/kb_reindex.py:68` (`VALID_NOTE_TYPES`) · the `valid_note_type` CHECK in `orchestrator/database/vector_schema.sql` **and** the new migration. `tests/test_tool_vocabularies.py:87` enforces the first two.
- **Priority is never binding.** No code path may refuse, reorder, or gate work based on priority. It orders a displayed list; nothing else. Ranks: `0=high, 1=normal, 2=low`, default `1`.
- **A ticket in progress keeps `status='active'`.** "In progress" is derived from `campaign.initiative_note_id`; it is never written to the note.
- **The engine never reads a note to decide what to do next.** Selection reads the pool (a query); execution reads the campaign (a row). The note's `status` is a mirror only.
- Budget stays **job-counted**. The cycles rename (unified-engine Phase 2) is cut; do not rename `max_iterations`/`remaining_iterations`.
- Cockpit: `TestBed.createComponent` does not work in this repo — use the bare `Injector.create` + `runInInjectionContext` pattern. Any key added to `en.json` **must** be mirrored in `de-DE.json` or the `i18n:check` gate fails.

## File Structure

| File | Responsibility |
| --- | --- |
| `orchestrator/database/migrations/vector/0013_kb_backlog_ticket_types.sql` | **Create.** Ticket types + `priority` column + partial pool index. |
| `orchestrator/database/vector_schema.sql` | Fresh-install schema kept in step with the migration. |
| `src/services/knowledge_graph.py` | Canonical vocabularies: `NOTE_TYPES`, new `PRIORITY_RANKS`/`PRIORITY_WORDS`. |
| `src/tools/knowledge/knowledge_tools.py` | Agent tool surface: literals, `priority` on `kb_write`/`kb_update`, rendering in `_render_note_md` and `kb_list`. |
| `src/services/knowledge_store.py` | Persistence: `KnowledgeRecord.priority`, `upsert_note` column. |
| `orchestrator/services/kb_reindex.py` | File→index ingest: mirrored vocabulary + frontmatter `priority` parse. |
| `orchestrator/services/project_backlog.py` | **Create.** Pool query (`fetch_backlog`), pure renderer (`render_backlog_block`), ticket close (`close_backlog_ticket`). |
| `orchestrator/services/project_loops.py` | Kickoff assembly: accept the pre-rendered block, delete the fictional line, role wording. |
| `orchestrator/main.py` | Fetch the pool in `_spawn_loop_job`; mirror ticket status on disposition. |
| `orchestrator/routers/project_loops.py` | `GET /api/projects/{id}/backlog` for the cockpit. |
| `cockpit/src/app/views/project-detail/project-backlog.component.ts` | **Create.** Backlog panel. |
| `tests/test_project_backlog.py` | **Create.** Pool query, renderer, close, mirror. |

---

### Task 1: Ticket types and the priority column

**Files:**
- Create: `orchestrator/database/migrations/vector/0013_kb_backlog_ticket_types.sql`
- Modify: `orchestrator/database/vector_schema.sql:491-507` (both CHECK spots), the `CREATE TABLE` body, and the index block at `:509-512`
- Modify: `src/services/knowledge_graph.py:46-61`
- Modify: `src/services/knowledge_store.py:55-70` (`KB_TTL_BY_NOTE_TYPE`)
- Modify: `src/tools/knowledge/knowledge_tools.py:60-73`
- Modify: `orchestrator/services/kb_reindex.py:68-79`
- Test: `tests/test_tool_vocabularies.py` (existing test must still pass)

**Interfaces:**
- Produces: note types `feature`, `issue`, `idea`; `knowledge_index.priority SMALLINT NOT NULL DEFAULT 1`; `PRIORITY_RANKS = {"high":0,"normal":1,"low":2}`, `PRIORITY_WORDS = {0:"high",1:"normal",2:"low"}`, `DEFAULT_PRIORITY_RANK = 1` in `src/services/knowledge_graph.py`; `PriorityValue` literal in `knowledge_tools.py`.

- [ ] **Step 1: Write the failing vocabulary test**

Add to `tests/test_tool_vocabularies.py`, inside the same class as `test_kb_literals_match_knowledge_graph_vocabularies`:

```python
    def test_backlog_ticket_types_are_in_every_vocabulary(self):
        """A ticket type missing from any one site silently degrades: the tool
        rejects it, or kb_reindex rewrites it to 'learning' on the next pass."""
        from orchestrator.services.kb_reindex import VALID_NOTE_TYPES
        from src.services.knowledge_graph import NOTE_TYPES
        from src.tools.knowledge.knowledge_tools import NoteTypeValue

        for ticket_type in ("feature", "issue", "idea"):
            assert ticket_type in NOTE_TYPES
            assert ticket_type in set(get_args(NoteTypeValue))
            assert ticket_type in VALID_NOTE_TYPES

    def test_priority_literal_matches_priority_ranks(self):
        from src.services.knowledge_graph import PRIORITY_RANKS
        from src.tools.knowledge.knowledge_tools import PriorityValue

        assert set(get_args(PriorityValue)) == set(PRIORITY_RANKS)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_tool_vocabularies.py -q -k "backlog_ticket_types or priority_literal"`
Expected: FAIL — `ImportError: cannot import name 'PRIORITY_RANKS'` / assertion that `"feature"` is not in `NOTE_TYPES`.

- [ ] **Step 3: Extend the canonical vocabulary**

In `src/services/knowledge_graph.py`, replace the `NOTE_TYPES` block and add the priority maps directly beneath `NOTE_STATUSES`:

```python
# Valid note types and statuses
NOTE_TYPES = frozenset(
    {
        "goal",
        "plan",
        "decision",
        "learning",
        "code",
        "source",
        "question",
        "state",
        "retrospective",
        "datasource",
        # Backlog ticket types (docs/superpowers/specs/
        # 2026-07-26-project-backlog-pipeline-design.md). These carry no TTL —
        # KB_TTL_BY_NOTE_TYPE's default of None already covers them, so a
        # backlog entry never expires on a clock.
        "feature",
        "issue",
        "idea",
    }
)

NOTE_STATUSES = frozenset({"active", "resolved", "superseded", "archived"})

# Backlog priority. A LABEL, never a contract: it orders the list agents are
# shown and nothing in the engine may gate, refuse, or reorder work on it.
# Stored as a rank so ordering is an index scan rather than a CASE.
PRIORITY_RANKS: dict[str, int] = {"high": 0, "normal": 1, "low": 2}
PRIORITY_WORDS: dict[int, str] = {0: "high", 1: "normal", 2: "low"}
DEFAULT_PRIORITY_RANK = 1
```

- [ ] **Step 4: Extend the tool literals**

In `src/tools/knowledge/knowledge_tools.py`, add the three types to `NoteTypeValue` and add `PriorityValue` after `NoteConfidenceValue`:

```python
NoteTypeValue = Literal[
    "goal",
    "plan",
    "decision",
    "learning",
    "code",
    "source",
    "question",
    "state",
    "retrospective",
    "datasource",
    "feature",
    "issue",
    "idea",
]
NoteStatusValue = Literal["active", "resolved", "superseded", "archived"]
NoteConfidenceValue = Literal["high", "medium", "low"]
PriorityValue = Literal["high", "normal", "low"]
```

- [ ] **Step 5: Mirror the vocabulary on the orchestrator side**

In `orchestrator/services/kb_reindex.py`, extend `VALID_NOTE_TYPES` (the orchestrator cannot import `src/`, so this copy is deliberate):

```python
# CHECK-constraint vocabularies from vector/0001 + vector/0013 — frontmatter is
# human-editable, so unknown values map to safe defaults instead of failing the
# row INSERT. Canonical copy: src/services/knowledge_graph.py NOTE_TYPES (not
# importable here — the orchestrator image has no agent deps).
VALID_NOTE_TYPES = {
    "goal",
    "plan",
    "decision",
    "learning",
    "code",
    "source",
    "question",
    "state",
    "retrospective",
    "datasource",
    "feature",
    "issue",
    "idea",
}
```

- [ ] **Step 6: Make the no-TTL decision explicit**

`KB_TTL_DEFAULT = None` already means unknown types never expire, so this changes no behavior — it records the intent where the next reader of the TTL map will look, instead of leaving them to infer it. In `src/services/knowledge_store.py`, add to `KB_TTL_BY_NOTE_TYPE` after the `"source": None,` entry:

```python
    # Backlog tickets — durable by design. A ticket is work that still needs
    # doing; expiring it on a clock would delete the queue.
    "feature": None,
    "issue": None,
    "idea": None,
```

- [ ] **Step 7: Write the migration**

Create `orchestrator/database/migrations/vector/0013_kb_backlog_ticket_types.sql`:

```sql
-- migration:     0013_kb_backlog_ticket_types.sql
-- description:   Backlog / idea-pipeline support on knowledge_index: the
--                feature/issue/idea ticket types and a non-binding priority
--                rank, so the project loop can read an ordered work pool
--                instead of re-deriving a fictional backlog by similarity
--                search. Priority is a LABEL — nothing gates on it.
-- depends-on:    0012_kb_watermark_progress.sql
-- transactional: YES.

ALTER TABLE knowledge_index DROP CONSTRAINT IF EXISTS valid_note_type;
ALTER TABLE knowledge_index ADD CONSTRAINT valid_note_type CHECK (note_type IN (
    'goal', 'plan', 'decision', 'learning', 'code',
    'source', 'question', 'state', 'retrospective', 'datasource',
    'feature', 'issue', 'idea'
));

-- 0 = high, 1 = normal, 2 = low. Default normal, so every pre-existing note
-- and every note written by a client that does not know about priority sorts
-- in the middle. No backfill needed beyond this default.
ALTER TABLE knowledge_index
    ADD COLUMN IF NOT EXISTS priority SMALLINT NOT NULL DEFAULT 1;

ALTER TABLE knowledge_index
    ADD CONSTRAINT knowledge_index_priority_valid
    CHECK (priority BETWEEN 0 AND 2)
    NOT VALID;
ALTER TABLE knowledge_index
    VALIDATE CONSTRAINT knowledge_index_priority_valid;

-- The pool query: open tickets for one project, priority then age.
CREATE INDEX IF NOT EXISTS idx_knowledge_backlog
    ON knowledge_index (project_id, priority, created_at)
    WHERE status = 'active' AND note_type IN ('feature', 'issue', 'idea');

COMMENT ON COLUMN knowledge_index.priority IS
    'Backlog rank: 0=high, 1=normal, 2=low. A display label only — no code '
    'path may gate or reorder work on it.';
```

- [ ] **Step 8: Keep the fresh-install schema in step**

In `orchestrator/database/vector_schema.sql`: add `priority SMALLINT NOT NULL DEFAULT 1,` to the `CREATE TABLE knowledge_index` body immediately after the `status VARCHAR(50) DEFAULT 'active',` line; add the three ticket types to **both** the inline `valid_note_type` CHECK (`:491`) and the `DO $$` re-add block (`:501`); and append the pool index next to the existing indexes at `:509-512`:

```sql
CREATE INDEX IF NOT EXISTS idx_knowledge_backlog
    ON knowledge_index (project_id, priority, created_at)
    WHERE status = 'active' AND note_type IN ('feature', 'issue', 'idea');
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `python -m pytest tests/test_tool_vocabularies.py tests/test_kb_convergence.py -q`
Expected: PASS (all, including the pre-existing `test_kb_literals_match_knowledge_graph_vocabularies`).

- [ ] **Step 10: Commit**

```bash
git add orchestrator/database/migrations/vector/0013_kb_backlog_ticket_types.sql \
        orchestrator/database/vector_schema.sql \
        src/services/knowledge_graph.py \
        src/services/knowledge_store.py \
        src/tools/knowledge/knowledge_tools.py \
        orchestrator/services/kb_reindex.py \
        tests/test_tool_vocabularies.py
git commit -m "feat(kb): backlog ticket types and priority rank

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Persist and round-trip `priority`

**Files:**
- Modify: `src/services/knowledge_store.py` (the `KnowledgeRecord` dataclass at `:76`, `from_row` at `:95`, `upsert_note`'s INSERT at `:358`)
- Modify: `src/tools/knowledge/knowledge_tools.py:388` (`_render_note_md`)
- Modify: `orchestrator/services/kb_reindex.py` (frontmatter parse, ~`:167-196`)
- Test: `tests/test_project_backlog.py` (create)

**Interfaces:**
- Consumes: `PRIORITY_RANKS` / `PRIORITY_WORDS` / `DEFAULT_PRIORITY_RANK` (Task 1).
- Produces: `KnowledgeRecord.priority: int`; `upsert_note(..., priority: int = 1)`; `_render_note_md` emits a `priority:` frontmatter line; `kb_reindex._parse_note` returns `"priority": int`.

- [ ] **Step 1: Write the failing round-trip test**

Create `tests/test_project_backlog.py`:

```python
"""Project backlog / idea pipeline — spec:
docs/superpowers/specs/2026-07-26-project-backlog-pipeline-design.md
"""

import pytest


class TestPriorityRoundTrip:
    def test_render_emits_priority_word(self):
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md(
            {"id": "feature-x", "type": "feature", "content": "body", "priority": 0}
        )
        assert "priority: high" in md

    def test_render_omits_priority_when_absent(self):
        """Existing notes must not gain noise in their frontmatter."""
        from src.tools.knowledge.knowledge_tools import _render_note_md

        md = _render_note_md({"id": "d-1", "type": "decision", "content": "body"})
        assert "priority:" not in md

    def test_reindex_parses_priority_word_to_rank(self):
        from orchestrator.services.kb_reindex import _parse_note

        parsed = _parse_note(
            "feature-x.md",
            "---\nid: feature-x\ntype: feature\npriority: high\n---\n# T\nbody\n",
        )
        assert parsed["priority"] == 0

    def test_reindex_defaults_unknown_priority_to_normal(self):
        """Frontmatter is human-editable; a typo must not fail the row."""
        from orchestrator.services.kb_reindex import _parse_note

        parsed = _parse_note(
            "feature-x.md",
            "---\nid: feature-x\ntype: feature\npriority: URGENT!!\n---\n# T\nb\n",
        )
        assert parsed["priority"] == 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_project_backlog.py -q`
Expected: FAIL — `"priority: high" not in md`, and `KeyError: 'priority'` from `_parse_note`.

Note: if `_parse_note` is private-named differently in the file, use the actual name — check `grep -n "def _parse_note" orchestrator/services/kb_reindex.py` and adjust the import in the test to match.

- [ ] **Step 3: Render priority in the note frontmatter**

In `src/tools/knowledge/knowledge_tools.py`, inside `_render_note_md`, immediately **before** the `fm.append(f"status: ...")` line, add:

```python
    # Backlog rank as a human-facing word. Omitted when absent so non-ticket
    # notes keep their existing frontmatter byte-for-byte.
    if note.get("priority") is not None:
        from src.services.knowledge_graph import PRIORITY_WORDS

        raw_priority = note["priority"]
        word = (
            PRIORITY_WORDS.get(int(raw_priority))
            if isinstance(raw_priority, int)
            else str(raw_priority).strip().lower()
        )
        if word in ("high", "normal", "low"):
            fm.append(f"priority: {word}")
```

Also extend the docstring's "Expected keys" list to mention `priority`.

- [ ] **Step 4: Parse priority on the file→index path**

In `orchestrator/services/kb_reindex.py`, add the rank map beside the existing mirrored vocabularies:

```python
# Canonical copy: src/services/knowledge_graph.py PRIORITY_RANKS (not importable
# here — the orchestrator image has no agent deps).
PRIORITY_RANKS = {"high": 0, "normal": 1, "low": 2}
_DEFAULT_PRIORITY_RANK = 1
```

Then, in the frontmatter parser next to the `status` fallback, add:

```python
    priority = PRIORITY_RANKS.get(
        str(fm.get("priority", "")).strip().lower(), _DEFAULT_PRIORITY_RANK
    )
```

and add `"priority": priority,` to the returned dict.

- [ ] **Step 5: Persist it**

In `src/services/knowledge_store.py`:

1. Add to the `KnowledgeRecord` dataclass, after `status: str = "active"`:

```python
    priority: int = 1
```

2. Add to `from_row`, after the `status=` line:

```python
            priority=row.get("priority", 1),
```

3. In `upsert_note`, add a `priority: int = 1` keyword parameter, add `priority` to the INSERT column list and `$19` to the `VALUES` tuple (renumbering nothing else — it goes last, after `remaining_cycles`'s `$18`), add `priority = EXCLUDED.priority,` to the `ON CONFLICT DO UPDATE SET` list, and pass `priority` as the final bound argument.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_project_backlog.py tests/test_kb_convergence.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/services/knowledge_store.py src/tools/knowledge/knowledge_tools.py \
        orchestrator/services/kb_reindex.py tests/test_project_backlog.py
git commit -m "feat(kb): persist and round-trip note priority

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: `priority` on the agent tool surface

**Files:**
- Modify: `src/tools/knowledge/knowledge_tools.py` (`kb_write` at `:895`, `kb_update` at `:1116`, `kb_list` at `:1259`)
- Test: `tests/test_project_backlog.py`

**Interfaces:**
- Consumes: `PriorityValue` (Task 1); `_render_note_md` priority support (Task 2).
- Produces: `kb_write(..., priority: PriorityValue = "normal")`, `kb_update(..., priority: Optional[PriorityValue] = None)`; `kb_list` output lines carry `[priority]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_project_backlog.py`:

```python
class TestKbToolPriority:
    def test_kb_write_accepts_priority(self):
        """Agents must be able to file a ticket at a priority in one call."""
        import inspect

        from src.tools.knowledge.knowledge_tools import create_knowledge_tools

        tools = {t.name: t for t in create_knowledge_tools(_kb_context())}
        params = inspect.signature(tools["kb_write"].func).parameters
        assert "priority" in params

    def test_kb_update_accepts_priority(self):
        import inspect

        from src.tools.knowledge.knowledge_tools import create_knowledge_tools

        tools = {t.name: t for t in create_knowledge_tools(_kb_context())}
        params = inspect.signature(tools["kb_update"].func).parameters
        assert "priority" in params
```

Add this helper near the top of the file:

```python
def _kb_context():
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.job_id = "aaaaaaaa-0000-0000-0000-000000000001"
    return ctx
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_project_backlog.py -q -k KbToolPriority`
Expected: FAIL — `assert 'priority' in params`.

If `create_knowledge_tools` needs a differently-shaped context, mirror whatever `tests/` already does for these tools (`grep -rn "create_knowledge_tools" tests/ | head -3`) and adjust `_kb_context`.

- [ ] **Step 3: Add the parameter to `kb_write`**

Add `priority: PriorityValue = "normal",` to the signature after `confidence`, document it in the Args block as:

```
            priority: Backlog rank for feature/issue/idea tickets: "high",
                "normal" (default) or "low". A LABEL only — it orders the
                backlog list the loop is shown and nothing refuses or
                reprioritizes work because of it.
```

and include `"priority": PRIORITY_RANKS[priority]` in the note dict handed to the dual-write, importing `PRIORITY_RANKS` from `src.services.knowledge_graph` at the top of the module.

- [ ] **Step 4: Add the parameter to `kb_update`**

Add `priority: Optional[PriorityValue] = None,` to the signature, document it as *"Change the backlog rank; omit to leave it unchanged."*, and when it is not `None` include `PRIORITY_RANKS[priority]` in the update payload.

- [ ] **Step 5: Show priority in `kb_list` output**

In `kb_list`'s result rendering, prefix each ticket-type line with its priority word so an agent listing the tail sees the same shape as the injected block:

```python
        prefix = (
            f"[{PRIORITY_WORDS.get(row.get('priority', 1), 'normal')}] "
            if row.get("note_type") in ("feature", "issue", "idea")
            else ""
        )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_project_backlog.py tests/test_tool_vocabularies.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/tools/knowledge/knowledge_tools.py tests/test_project_backlog.py
git commit -m "feat(kb): priority on kb_write/kb_update, shown in kb_list

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: The pool query and its renderer

**Files:**
- Create: `orchestrator/services/project_backlog.py`
- Test: `tests/test_project_backlog.py`

**Interfaces:**
- Produces:
  - `BACKLOG_NOTE_TYPES: tuple[str, ...] = ("feature", "issue", "idea")`
  - `async fetch_backlog(vector_db, project_id: str, *, exclude_note_id: str | None = None, limit: int = 20) -> tuple[list[dict], dict[int, int]]` — returns `(rows, counts_by_rank)`; each row has `note_id`, `note_type`, `title`, `priority`.
  - `render_backlog_block(rows: list[dict], counts: dict[int, int], *, in_progress: dict | None = None, limit: int = 20) -> str` — pure.

- [ ] **Step 1: Write the failing renderer tests**

Append to `tests/test_project_backlog.py`:

```python
def _row(note_id, note_type="feature", title="T", priority=1):
    return {
        "note_id": note_id,
        "note_type": note_type,
        "title": title,
        "priority": priority,
    }


class TestRenderBacklogBlock:
    def test_counts_line_comes_first_and_breaks_down_by_priority(self):
        """The cap hides the tail; the counts line is what tells the reader a
        tail exists at all. It must lead the block."""
        from orchestrator.services.project_backlog import render_backlog_block

        rows = [_row("a", priority=0), _row("b", priority=1)]
        counts = {0: 12, 1: 15, 2: 7}
        block = render_backlog_block(rows, counts, limit=20)

        first = block.splitlines()[0]
        assert first == (
            "PROJECT BACKLOG — 34 open: 12 high, 15 normal, 7 low (showing top 20)"
        )

    def test_in_progress_line_precedes_the_list(self):
        from orchestrator.services.project_backlog import render_backlog_block

        block = render_backlog_block(
            [_row("a")],
            {1: 1},
            in_progress={"note_id": "issue-deploy", "title": "Deploy docs",
                         "priority": 0, "note_type": "issue"},
        )
        lines = block.splitlines()
        assert lines[1].startswith("IN PROGRESS: [high] issue-deploy — Deploy docs")
        assert lines[2].lstrip().startswith("[normal]")

    def test_no_in_progress_renders_none(self):
        from orchestrator.services.project_backlog import render_backlog_block

        block = render_backlog_block([_row("a")], {1: 1})
        assert "IN PROGRESS: (none)" in block

    def test_remainder_is_reported_when_capped(self):
        from orchestrator.services.project_backlog import render_backlog_block

        rows = [_row(f"n{i}") for i in range(3)]
        block = render_backlog_block(rows, {1: 10}, limit=3)
        assert "… 7 more" in block

    def test_empty_pool_tells_the_agent_to_file_tickets(self):
        """An empty pool must not render an empty void — the loop's own agents
        are the ones expected to fill it."""
        from orchestrator.services.project_backlog import render_backlog_block

        block = render_backlog_block([], {})
        assert "0 open" in block
        assert "kb_write" in block
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_project_backlog.py -q -k RenderBacklogBlock`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.services.project_backlog'`.

- [ ] **Step 3: Write the module**

Create `orchestrator/services/project_backlog.py`:

```python
"""Project backlog — the loop's real work pool.

Before this existed, every loop kickoff told the agent to "check the KB for …
the current open backlog" and there was no backlog: each agent re-derived one
by similarity search, every job. This module makes the pool a deterministic,
indexed listing that the orchestrator hands over verbatim.

Two buckets (docs/superpowers/specs/2026-07-26-project-backlog-pipeline-design.md):

* **pool** — notes of type feature/issue/idea with ``status='active'``,
  ordered by priority then age.
* **in progress** — the loop's existing ``campaign``, whose
  ``initiative_note_id`` is the ticket. A ticket being worked on keeps
  ``status='active'``; "in progress" is derived, never written to the note.
  The pool query therefore EXCLUDES the campaign's initiative.

Priority is a LABEL. Nothing here (or anywhere) may gate, refuse or reorder
work because of it — it only sorts what the agent is shown.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Canonical copy: src/services/knowledge_graph.py (not importable here — the
# orchestrator image has no agent deps; see kb_reindex.py for the same pattern).
PRIORITY_WORDS: dict[int, str] = {0: "high", 1: "normal", 2: "low"}
DEFAULT_PRIORITY_RANK = 1

BACKLOG_NOTE_TYPES: tuple[str, ...] = ("feature", "issue", "idea")

# How many tickets ride in a kickoff. The counts line (see render_backlog_block)
# is what keeps this cap from hiding the tail.
BACKLOG_INJECTION_LIMIT = 20


async def fetch_backlog(
    vector_db: Any,
    project_id: str,
    *,
    exclude_note_id: str | None = None,
    limit: int = BACKLOG_INJECTION_LIMIT,
) -> tuple[list[dict[str, Any]], dict[int, int]]:
    """Return ``(rows, counts_by_rank)`` for a project's open ticket pool.

    ``exclude_note_id`` drops the in-progress campaign's initiative so the
    overseer is never offered something already underway. Both queries hit
    ``idx_knowledge_backlog``.
    """
    async with vector_db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT note_id, note_type, title, priority
              FROM knowledge_index
             WHERE project_id = $1::uuid
               AND status = 'active'
               AND note_type = ANY($2::text[])
               AND ($3::text IS NULL OR note_id <> $3)
             ORDER BY priority ASC, created_at ASC
             LIMIT $4
            """,
            project_id,
            list(BACKLOG_NOTE_TYPES),
            exclude_note_id,
            limit,
        )
        count_rows = await conn.fetch(
            """
            SELECT priority, COUNT(*) AS n
              FROM knowledge_index
             WHERE project_id = $1::uuid
               AND status = 'active'
               AND note_type = ANY($2::text[])
               AND ($3::text IS NULL OR note_id <> $3)
             GROUP BY priority
            """,
            project_id,
            list(BACKLOG_NOTE_TYPES),
            exclude_note_id,
        )
    return (
        [dict(r) for r in rows],
        {int(r["priority"]): int(r["n"]) for r in count_rows},
    )


def _priority_word(rank: Any) -> str:
    try:
        return PRIORITY_WORDS.get(int(rank), "normal")
    except (TypeError, ValueError):
        return "normal"


def render_backlog_block(
    rows: list[dict[str, Any]],
    counts: dict[int, int],
    *,
    in_progress: dict[str, Any] | None = None,
    limit: int = BACKLOG_INJECTION_LIMIT,
) -> str:
    """Render the kickoff's backlog block. Pure — no I/O, no DB.

    Shape (order is load-bearing: the per-priority totals lead, because with a
    hard cap a large pool otherwise hides its own tail and the idea bucket
    silently becomes write-only)::

        PROJECT BACKLOG — 34 open: 12 high, 15 normal, 7 low (showing top 20)
        IN PROGRESS: [high] issue-deploy-docs — Deployment docs missing
          [high]   feature  feature-rag-boundary — Permission-aware RAG boundary
          … 18 more
    """
    total = sum(counts.values())
    breakdown = ", ".join(
        f"{counts[rank]} {word}"
        for rank, word in sorted(PRIORITY_WORDS.items())
        if counts.get(rank)
    )
    header = f"PROJECT BACKLOG — {total} open"
    if breakdown:
        header += f": {breakdown}"
    if total > len(rows):
        header += f" (showing top {limit})"

    lines = [header]

    if in_progress:
        lines.append(
            f"IN PROGRESS: [{_priority_word(in_progress.get('priority'))}] "
            f"{in_progress.get('note_id')} — {in_progress.get('title') or ''}".rstrip()
            .rstrip("—")
            .rstrip()
        )
    else:
        lines.append("IN PROGRESS: (none)")

    if not rows:
        lines.append(
            "  (the pool is empty — file feature/issue/idea notes with kb_write "
            "so the next iterations have a real queue to work from)"
        )
        return "\n".join(lines)

    for row in rows:
        word = _priority_word(row.get("priority"))
        lines.append(
            f"  [{word}]".ljust(12)
            + f"{row.get('note_type', ''):<9}"
            + f"{row.get('note_id')} — {row.get('title') or ''}".rstrip()
        )

    remainder = total - len(rows)
    if remainder > 0:
        lines.append(f"  … {remainder} more (use kb_list to see them)")

    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_project_backlog.py -q`
Expected: PASS. If the `[high]` column padding trips `test_in_progress_line_precedes_the_list`, adjust the `ljust`/`:<9` widths — the assertions check prefixes, not exact spacing.

- [ ] **Step 5: Add the fetch test**

Append:

```python
class TestFetchBacklog:
    @pytest.mark.asyncio
    async def test_excludes_the_in_progress_initiative(self):
        """The overseer must never be offered a ticket already underway."""
        from unittest.mock import AsyncMock, MagicMock

        from orchestrator.services.project_backlog import fetch_backlog

        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[])

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        vector_db = MagicMock()
        vector_db.acquire = MagicMock(return_value=_CM())

        await fetch_backlog(vector_db, "p-1", exclude_note_id="issue-x")

        first_sql = conn.fetch.await_args_list[0].args[0]
        assert "note_id <> $3" in first_sql
        assert conn.fetch.await_args_list[0].args[3] == "issue-x"
```

- [ ] **Step 6: Run it and commit**

Run: `python -m pytest tests/test_project_backlog.py -q` → PASS

```bash
git add orchestrator/services/project_backlog.py tests/test_project_backlog.py
git commit -m "feat(loop): backlog pool query and kickoff renderer

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Inject the backlog into every kickoff

**Files:**
- Modify: `orchestrator/services/project_loops.py` (`build_loop_kickoff` at `:657`, the parts list at `:698-716`, `create_loop_job` at `:737`, the kickoff call at `:842`)
- Modify: `orchestrator/main.py` (`_spawn_loop_job` at `:12544`)
- Test: `tests/test_project_backlog.py`, `tests/test_project_loops.py`

**Interfaces:**
- Consumes: `fetch_backlog`, `render_backlog_block`, `BACKLOG_INJECTION_LIMIT` (Task 4).
- Produces: `build_loop_kickoff(loop, *, role, iteration, extra_context=None, backlog_block: str | None = None)`; `create_loop_job(..., backlog_block: str | None = None)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_project_backlog.py`:

```python
class TestKickoffInjection:
    def _loop(self):
        return {
            "id": "105a6f98-134c-4077-b7e1-6d08916650d7",
            "status": "running",
            "scheduling": "standard",
            "role_sequence": ["scholar", "critic", "developer"],
            "seq_index": 0,
            "goal": "Build a thing",
            "max_iterations": 30,
            "remaining_iterations": 20,
            "campaign": None,
            "campaign_history": [],
        }

    def test_block_is_injected_verbatim(self):
        from orchestrator.services.project_loops import build_loop_kickoff

        kickoff = build_loop_kickoff(
            self._loop(),
            role="critic",
            iteration=3,
            backlog_block="PROJECT BACKLOG — 2 open: 2 high\nIN PROGRESS: (none)",
        )
        assert "PROJECT BACKLOG — 2 open: 2 high" in kickoff

    def test_the_fictional_backlog_instruction_is_gone(self):
        """The old kickoff told the agent to go find a backlog that did not
        exist. Injection replaces the search; the instruction must not survive
        or agents will keep similarity-hunting for it."""
        from orchestrator.services.project_loops import build_loop_kickoff

        kickoff = build_loop_kickoff(
            self._loop(), role="critic", iteration=3, backlog_block="X"
        )
        assert "the current open backlog" not in kickoff

    def test_no_block_still_builds_a_kickoff(self):
        from orchestrator.services.project_loops import build_loop_kickoff

        kickoff = build_loop_kickoff(self._loop(), role="scholar", iteration=1)
        assert "PROJECT GOAL" in kickoff
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_project_backlog.py -q -k KickoffInjection`
Expected: FAIL — `TypeError: build_loop_kickoff() got an unexpected keyword argument 'backlog_block'`.

- [ ] **Step 3: Accept and place the block**

In `orchestrator/services/project_loops.py`, add the parameter to `build_loop_kickoff`:

```python
def build_loop_kickoff(
    loop: dict[str, Any],
    *,
    role: str,
    iteration: int,
    extra_context: dict[str, Any] | None = None,
    backlog_block: str | None = None,
) -> str:
```

Document it in the docstring:

```
    ``backlog_block`` is the pre-rendered work pool (see
    ``services/project_backlog.render_backlog_block``). It is injected
    VERBATIM: this function stays pure and does no I/O, so the caller fetches
    it. Passing None (start-up paths, tests) simply omits the section.
```

Then, in the `parts` list, insert the block immediately after the `LOOP STATUS` entry and rewrite the "BEFORE you act" entry:

```python
        f"LOOP STATUS: iteration {iteration}. {budget_line} Do NOT try to finish "
        "the whole goal in one job — make ONE solid, verifiable increment and "
        "hand off through the KB.",
    ]

    # The work pool, handed over rather than searched for. Placed before the
    # role block so selection duty reads it in order.
    if backlog_block:
        parts.append(backlog_block)

    parts += [
        "BEFORE you act: restate the goal in one line, then check the KB for "
        "(a) what's already done and (b) what's been TRIED AND REJECTED (do "
        "not re-propose it). The open backlog is listed above — it is given to "
        "you, do not go searching for it.",
        f"YOUR ROLE THIS ITERATION — {role.upper()}:\n{role_block}",
        "WHEN DONE: write to the KB what you did, what you learned, and what the "
        "next agent should do. If you closed or abandoned an approach, record it "
        "as tried/rejected so nobody repeats it. File any new work you spotted "
        "but did not do as a `feature`, `issue` or `idea` note (kb_write) — that "
        "is the project backlog, and it is the only place future iterations will "
        "look for it.",
    ]
```

(Delete the original "BEFORE you act", "YOUR ROLE THIS ITERATION" and "WHEN DONE" entries from the first list literal — they move into this second block.)

- [ ] **Step 4: Thread it through `create_loop_job`**

Add `backlog_block: str | None = None,` to `create_loop_job`'s keyword parameters and pass it on at `:842`:

```python
    kickoff = build_loop_kickoff(
        loop,
        role=role,
        iteration=iteration,
        extra_context=extra_context,
        backlog_block=backlog_block,
    )
```

- [ ] **Step 5: Fetch the pool at the single spawn funnel**

In `orchestrator/main.py`'s `_spawn_loop_job`, immediately before the `create_loop_job(...)` call, add:

```python
    # Hand the work pool over rather than making the agent hunt for it. Every
    # loop spawn funnels through here. Non-fatal: a KB outage costs the block,
    # not the job.
    backlog_block: str | None = None
    project_id = loop.get("project_id")
    if project_id and vector_db is not None:
        from services.project_backlog import fetch_backlog, render_backlog_block

        campaign = loop.get("campaign") or {}
        in_progress_id = campaign.get("initiative_note_id")
        try:
            rows, counts = await fetch_backlog(
                vector_db, str(project_id), exclude_note_id=in_progress_id
            )
            in_progress = None
            if in_progress_id:
                in_progress = {
                    "note_id": in_progress_id,
                    "title": campaign.get("title") or "",
                    "priority": 1,
                    "note_type": "feature",
                }
            backlog_block = render_backlog_block(
                rows, counts, in_progress=in_progress
            )
        except Exception:
            logger.warning(
                "loop %s: backlog fetch failed — spawning without the block",
                str(loop.get("id"))[:8],
                exc_info=True,
            )
```

and pass `backlog_block=backlog_block` into `create_loop_job`.

- [ ] **Step 6: Point the two funnel-facing roles at the pool**

The generic instruction in Step 3 covers every role. These two additions name the specific duty the spec assigns them (§7). In `orchestrator/services/project_loops.py`'s `_ROLE_BLOCKS`, append one sentence to the existing `scholar` block:

```
 Default to foraging widely rather than waiting to be told what to look at — file what you find as `idea` notes; that is how the backlog grows.
```

and one to the existing `product-qa` block:

```
 File every defect you confirm as an `issue` note (kb_write) — an issue that exists only in your report is invisible to the next iteration.
```

Do not restructure either block; append to the prose that is already there.

- [ ] **Step 7: Run the tests**

Run: `python -m pytest tests/test_project_backlog.py tests/test_project_loops.py tests/test_loop_campaign_scheduling.py -q`
Expected: PASS. Any existing assertion that matched the old "BEFORE you act" wording must be updated to the new sentence — that is a legitimate update, not a workaround.

- [ ] **Step 8: Commit**

```bash
git add orchestrator/services/project_loops.py orchestrator/main.py \
        tests/test_project_backlog.py tests/test_project_loops.py
git commit -m "feat(loop): inject the real backlog into every kickoff

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Close the ticket when the campaign is disposed

**Files:**
- Modify: `orchestrator/services/project_backlog.py`
- Modify: `orchestrator/main.py` (`_advance_planner_campaign`, the `if campaign and disposition:` block)
- Test: `tests/test_project_backlog.py`

**Interfaces:**
- Consumes: `BACKLOG_NOTE_TYPES` (Task 4); `GiteaClient.get_file_content` / `create_or_update_file` (`orchestrator/services/gitea.py:690` / `:461`); module-level `gitea_client` (`main.py:338`).
- Produces: `async close_backlog_ticket(vector_db, gitea, project_id: str, note_id: str, new_status: str) -> bool`.

**Why both writes:** `kb_reindex` re-ingests `knowledge/*.md` from the jobs repo and overwrites the index row's `status` from frontmatter. An index-only close would be silently reverted on the next reindex and the ticket would reappear in the pool. The file is the durable write; the index write makes the pool correct immediately instead of at reindex latency.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_project_backlog.py`:

```python
class TestCloseBacklogTicket:
    @pytest.mark.asyncio
    async def test_writes_file_and_index(self):
        """Index-only would be reverted by the next kb_reindex pass, which
        re-ingests status from the markdown frontmatter."""
        from unittest.mock import AsyncMock, MagicMock

        from orchestrator.services.project_backlog import close_backlog_ticket

        conn = MagicMock()
        conn.execute = AsyncMock(return_value="UPDATE 1")

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        vector_db = MagicMock()
        vector_db.acquire = MagicMock(return_value=_CM())

        gitea = MagicMock()
        gitea.get_file_content = AsyncMock(
            return_value="---\nid: feature-x\ntype: feature\nstatus: active\n---\n# T\n"
        )
        gitea.create_or_update_file = AsyncMock(return_value=True)

        ok = await close_backlog_ticket(
            vector_db, gitea, "68137e29-6b1f-4f1b-a0c1-4e6dc2be3f9a",
            "feature-x", "resolved",
        )

        assert ok is True
        written = gitea.create_or_update_file.await_args.args[2]
        assert "status: resolved" in written
        assert "status: active" not in written
        conn.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_gitea_failure_is_not_fatal(self):
        """A disposition must never fail because a mirror write failed."""
        from unittest.mock import AsyncMock, MagicMock

        from orchestrator.services.project_backlog import close_backlog_ticket

        conn = MagicMock()
        conn.execute = AsyncMock(return_value="UPDATE 1")

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        vector_db = MagicMock()
        vector_db.acquire = MagicMock(return_value=_CM())

        gitea = MagicMock()
        gitea.get_file_content = AsyncMock(side_effect=RuntimeError("gitea down"))

        ok = await close_backlog_ticket(
            vector_db, gitea, "68137e29-6b1f-4f1b-a0c1-4e6dc2be3f9a",
            "feature-x", "resolved",
        )
        assert ok is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_project_backlog.py -q -k CloseBacklogTicket`
Expected: FAIL — `ImportError: cannot import name 'close_backlog_ticket'`.

- [ ] **Step 3: Implement the close**

Append to `orchestrator/services/project_backlog.py`:

```python
import re

_STATUS_LINE = re.compile(r"^status:.*$", re.MULTILINE)


def _rewrite_status(markdown: str, new_status: str) -> str:
    """Replace the frontmatter ``status:`` line, or insert one if absent.

    Deliberately a line rewrite rather than a YAML round-trip: the note is a
    human-editable document and reserializing it would reformat everything the
    author wrote.
    """
    if not markdown.startswith("---"):
        return markdown
    head, sep, tail = markdown[3:].partition("\n---")
    if not sep:
        return markdown
    if _STATUS_LINE.search(head):
        head = _STATUS_LINE.sub(f"status: {new_status}", head, count=1)
    else:
        head = head.rstrip("\n") + f"\nstatus: {new_status}\n"
    return "---" + head + sep + tail


async def close_backlog_ticket(
    vector_db: Any,
    gitea: Any,
    project_id: str,
    note_id: str,
    new_status: str,
) -> bool:
    """Mirror a ticket's closed status to the note file AND the index row.

    The database (campaign + campaign_history) stays authoritative for what the
    loop did; this only keeps the pool and the human-readable note in step.
    Best-effort by contract: a disposition must never fail because a mirror
    write failed. Returns True only when the durable (file) write succeeded.
    """
    repo_name = f"project-{str(project_id)[:8]}-jobs"
    file_path = f"knowledge/{note_id}.md"
    file_written = False
    try:
        current = await gitea.get_file_content(repo_name, file_path)
        if current:
            updated = _rewrite_status(current, new_status)
            file_written = bool(
                await gitea.create_or_update_file(
                    repo_name,
                    file_path,
                    updated,
                    f"backlog: {note_id} → {new_status}",
                )
            )
        else:
            logger.info(
                "backlog: note file %s not found in %s — index-only close",
                file_path,
                repo_name,
            )
    except Exception:
        logger.warning(
            "backlog: file mirror failed for %s (%s) — the next kb_reindex will "
            "restore the pool entry and the overseer will see it again",
            note_id,
            new_status,
            exc_info=True,
        )

    try:
        async with vector_db.acquire() as conn:
            await conn.execute(
                """
                UPDATE knowledge_index
                   SET status = $3, modified_at = NOW()
                 WHERE project_id = $1::uuid
                   AND note_id = $2
                   AND note_type = ANY($4::text[])
                """,
                project_id,
                note_id,
                new_status,
                list(BACKLOG_NOTE_TYPES),
            )
    except Exception:
        logger.warning(
            "backlog: index mirror failed for %s (%s)", note_id, new_status,
            exc_info=True,
        )

    return file_written
```

- [ ] **Step 4: Call it on disposition**

In `orchestrator/main.py`, inside `_advance_planner_campaign`'s `if campaign and disposition:` block, after the `_notify_loop_event(...)` await, add:

```python
        # Mirror the verdict onto the ticket. ship → resolved, kill → archived;
        # extend leaves it active because the continuing campaign still owns it.
        ticket_status = {"ship": "resolved", "kill": "archived"}.get(outcome)
        ticket_id = campaign.get("initiative_note_id")
        if ticket_status and ticket_id and vector_db is not None:
            from services.project_backlog import close_backlog_ticket

            await close_backlog_ticket(
                vector_db,
                gitea_client,
                str(loop.get("project_id")),
                str(ticket_id),
                ticket_status,
            )
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_project_backlog.py tests/test_loop_campaign_scheduling.py -q`
Expected: PASS — including the existing disposition tests, which must be unaffected (they patch `main.postgres_db` and leave `vector_db` as `None`, so the new branch no-ops).

- [ ] **Step 6: Commit**

```bash
git add orchestrator/services/project_backlog.py orchestrator/main.py \
        tests/test_project_backlog.py
git commit -m "feat(loop): close the backlog ticket when its campaign is disposed

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Backlog API for the cockpit

**Files:**
- Modify: `orchestrator/routers/project_loops.py`
- Test: `tests/test_project_backlog.py`

**Interfaces:**
- Consumes: `fetch_backlog` (Task 4).
- Produces: `GET /api/projects/{project_id}/backlog` → `{"total": int, "counts": {"high": n, "normal": n, "low": n}, "in_progress": {...} | None, "items": [{"note_id","note_type","title","priority"}]}` (priority as the **word**, so the client never learns the rank encoding).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_project_backlog.py`:

```python
class TestBacklogEndpoint:
    @pytest.mark.asyncio
    async def test_returns_words_not_ranks(self):
        """The wire format must not leak the 0/1/2 encoding — the cockpit
        should never have to know it."""
        from unittest.mock import AsyncMock, patch

        from orchestrator.routers.project_loops import get_project_backlog

        rows = [{"note_id": "f-1", "note_type": "feature", "title": "T",
                 "priority": 0}]
        with patch(
            "routers.project_loops.fetch_backlog",
            AsyncMock(return_value=(rows, {0: 1})),
        ):
            out = await get_project_backlog(
                "68137e29-6b1f-4f1b-a0c1-4e6dc2be3f9a", user_id=None
            )

        assert out["items"][0]["priority"] == "high"
        assert out["counts"]["high"] == 1
        assert out["total"] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_project_backlog.py -q -k BacklogEndpoint`
Expected: FAIL — `ImportError: cannot import name 'get_project_backlog'`.

Note: match the router's existing dependency-injection and auth conventions — read the neighbouring endpoints in `orchestrator/routers/project_loops.py` first and mirror how they resolve the project, the DB handles and the caller. Adjust the test's call signature to whatever those conventions require; the assertions are what matter.

- [ ] **Step 3: Implement the endpoint**

Add to `orchestrator/routers/project_loops.py`, following the file's existing endpoint conventions for auth and DB access:

```python
@router.get("/{project_id}/backlog")
async def get_project_backlog(project_id: str, user_id: str | None = None) -> dict:
    """The project's ticket pool — what the loop's overseer is shown.

    Priority is returned as a word; the 0/1/2 rank is a storage detail.
    """
    from services.project_backlog import PRIORITY_WORDS, fetch_backlog

    loop = await postgres_db.get_active_project_loop(project_id)
    campaign = (loop or {}).get("campaign") or {}
    in_progress_id = campaign.get("initiative_note_id")

    rows, counts = await fetch_backlog(
        vector_db, project_id, exclude_note_id=in_progress_id, limit=200
    )
    return {
        "total": sum(counts.values()),
        "counts": {
            word: counts.get(rank, 0) for rank, word in sorted(PRIORITY_WORDS.items())
        },
        "in_progress": (
            {"note_id": in_progress_id, "title": campaign.get("title") or ""}
            if in_progress_id
            else None
        ),
        "items": [
            {
                "note_id": r["note_id"],
                "note_type": r["note_type"],
                "title": r["title"],
                "priority": PRIORITY_WORDS.get(int(r["priority"]), "normal"),
            }
            for r in rows
        ],
    }
```

Use whatever the file's other endpoints use to reach the loop row and the DB handles (`get_active_project_loop` may be named differently — check with `grep -n "async def " orchestrator/routers/project_loops.py`).

- [ ] **Step 4: Run the tests and lint**

Run: `python -m pytest tests/test_project_backlog.py -q && ruff check orchestrator/ src/ tests/`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/routers/project_loops.py tests/test_project_backlog.py
git commit -m "feat(api): GET /api/projects/{id}/backlog

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Cockpit backlog panel

**Files:**
- Create: `cockpit/src/app/views/project-detail/project-backlog.component.ts`
- Create: `cockpit/src/app/views/project-detail/project-backlog.component.spec.ts`
- Modify: `cockpit/src/app/core/models/api.model.ts`
- Modify: `cockpit/src/app/views/project-detail/project-detail.component.ts`
- Modify: `cockpit/src/assets/i18n/en.json`, `cockpit/src/assets/i18n/de-DE.json`

**Interfaces:**
- Consumes: `GET /api/projects/{id}/backlog` (Task 7).
- Produces: `BacklogItem` / `ProjectBacklog` interfaces in `api.model.ts`; `<app-project-backlog [projectId]="…">`.

- [ ] **Step 1: Write the failing spec**

Create `cockpit/src/app/views/project-detail/project-backlog.component.spec.ts`:

```typescript
import { describe, expect, it } from 'vitest';
import { groupByPriority } from './project-backlog.component';

describe('groupByPriority', () => {
  it('orders high, normal, low regardless of input order', () => {
    const grouped = groupByPriority([
      { note_id: 'c', note_type: 'idea', title: 'C', priority: 'low' },
      { note_id: 'a', note_type: 'feature', title: 'A', priority: 'high' },
      { note_id: 'b', note_type: 'issue', title: 'B', priority: 'normal' },
    ]);
    expect(grouped.map((g) => g.priority)).toEqual(['high', 'normal', 'low']);
    expect(grouped[0].items[0].note_id).toBe('a');
  });

  it('omits empty priority groups', () => {
    const grouped = groupByPriority([
      { note_id: 'a', note_type: 'feature', title: 'A', priority: 'high' },
    ]);
    expect(grouped).toHaveLength(1);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd cockpit && npx vitest run src/app/views/project-detail/project-backlog.component.spec.ts`
Expected: FAIL — cannot resolve `./project-backlog.component`.

- [ ] **Step 3: Add the model types**

In `cockpit/src/app/core/models/api.model.ts`:

```typescript
export type BacklogPriority = 'high' | 'normal' | 'low';

export interface BacklogItem {
  note_id: string;
  note_type: 'feature' | 'issue' | 'idea';
  title: string;
  priority: BacklogPriority;
}

export interface ProjectBacklog {
  total: number;
  counts: Record<BacklogPriority, number>;
  in_progress: { note_id: string; title: string } | null;
  items: BacklogItem[];
}
```

- [ ] **Step 4: Write the component**

Create `cockpit/src/app/views/project-detail/project-backlog.component.ts`. Note the conventions this follows: `standalone: true`; inline `styles` (keeps the file out of the stylelint glob and clear of the 36 kB per-component budget); the `transloco` pipe on every user-visible string; and the decision logic as an **exported pure function** so the spec can test it without `TestBed`, which cannot render components in this repo.

```typescript
import { CommonModule } from '@angular/common';
import { Component, computed, inject, input, resource } from '@angular/core';
import { TranslocoModule } from '@jsverse/transloco';

import { ApiService } from '../../core/services/api.service';
import type {
  BacklogItem,
  BacklogPriority,
  ProjectBacklog,
} from '../../core/models/api.model';

/** Group tickets into fixed high → normal → low order, dropping empty groups. */
export function groupByPriority(
  items: BacklogItem[],
): { priority: BacklogPriority; items: BacklogItem[] }[] {
  const order: BacklogPriority[] = ['high', 'normal', 'low'];
  return order
    .map((priority) => ({
      priority,
      items: items.filter((i) => i.priority === priority),
    }))
    .filter((g) => g.items.length > 0);
}

@Component({
  selector: 'app-project-backlog',
  standalone: true,
  imports: [CommonModule, TranslocoModule],
  template: `
    <section class="backlog">
      <header>
        <h3>{{ 'projectBacklog.title' | transloco }}</h3>
        <span class="total">{{ backlog.value()?.total ?? 0 }}</span>
      </header>

      @if (backlog.value()?.in_progress; as wip) {
        <p class="wip">
          <span class="chip">{{ 'projectBacklog.inProgress' | transloco }}</span>
          {{ wip.title || wip.note_id }}
        </p>
      }

      @if (groups().length === 0) {
        <p class="empty">{{ 'projectBacklog.empty' | transloco }}</p>
      } @else {
        @for (group of groups(); track group.priority) {
          <h4>{{ 'projectBacklog.priority.' + group.priority | transloco }}</h4>
          <ul>
            @for (item of group.items; track item.note_id) {
              <li>
                <span class="type">
                  {{ 'projectBacklog.type.' + item.note_type | transloco }}
                </span>
                {{ item.title || item.note_id }}
              </li>
            }
          </ul>
        }
      }
    </section>
  `,
  styles: [
    `
      .backlog { display: flex; flex-direction: column; gap: 0.5rem; }
      header { display: flex; align-items: center; justify-content: space-between; }
      .wip .chip,
      .type {
        font-size: 0.75rem;
        padding: 0.1rem 0.4rem;
        border-radius: 0.25rem;
        background: var(--surface-2);
      }
      ul { margin: 0; padding-left: 1rem; }
      .empty { opacity: 0.7; }
    `,
  ],
})
export class ProjectBacklogComponent {
  private readonly api = inject(ApiService);

  readonly projectId = input.required<string>();

  readonly backlog = resource<ProjectBacklog, string>({
    params: () => this.projectId(),
    loader: ({ params }) =>
      this.api.get<ProjectBacklog>(`/api/projects/${params}/backlog`),
  });

  readonly groups = computed(() => groupByPriority(this.backlog.value()?.items ?? []));
}
```

Match `ApiService`'s actual method name and return type (Promise vs Observable) — check a neighbouring component with `grep -n "inject(ApiService)" -A 6 cockpit/src/app/views/project-detail/project-loop.component.ts` and mirror it. If the codebase uses signals + explicit loading rather than `resource()`, follow that instead; the exported `groupByPriority` and the template structure are what this step fixes.

- [ ] **Step 5: Add i18n keys to BOTH locales**

Add to `cockpit/src/assets/i18n/en.json` and mirror key-for-key in `de-DE.json` (the parity gate fails otherwise):

```
projectBacklog.title            "Backlog"
projectBacklog.inProgress       "In progress"
projectBacklog.empty            "No open tickets yet — the loop's scholars will file ideas here."
projectBacklog.priority.high    "High"
projectBacklog.priority.normal  "Normal"
projectBacklog.priority.low     "Low"
projectBacklog.type.feature     "Feature"
projectBacklog.type.issue       "Issue"
projectBacklog.type.idea        "Idea"
```

- [ ] **Step 6: Mount it on the project page**

Import `ProjectBacklogComponent` in `project-detail.component.ts` and render `<app-project-backlog [projectId]="projectId()" />` next to the loop panel.

- [ ] **Step 7: Run the cockpit gates**

```bash
cd cockpit
npx vitest run src/app/views/project-detail/
npm run i18n:check
npm install --no-save @monaco-editor/loader && npx ng build
```
Expected: specs pass, i18n parity clean, build succeeds (the build is the real template type-checker).

- [ ] **Step 8: Commit**

```bash
git add cockpit/src/app/views/project-detail/project-backlog.component.ts \
        cockpit/src/app/views/project-detail/project-backlog.component.spec.ts \
        cockpit/src/app/views/project-detail/project-detail.component.ts \
        cockpit/src/app/core/models/api.model.ts \
        cockpit/src/assets/i18n/en.json cockpit/src/assets/i18n/de-DE.json
git commit -m "feat(cockpit): project backlog panel

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Full gate

Run before declaring the plan complete:

```bash
python -m pytest tests/test_project_backlog.py tests/test_project_loops.py \
  tests/test_project_loop_sweeper.py tests/test_loop_campaign_scheduling.py \
  tests/test_loop_merge.py tests/test_critic_loop.py \
  tests/test_loop_unified_advance.py tests/test_tool_vocabularies.py \
  tests/test_kb_convergence.py -q
ruff check orchestrator/ src/ tests/
cd cockpit && npx vitest run && npm run i18n:check
```

## Deploy notes

- **Migration before code.** `vector/0013` must be applied before the new orchestrator image starts; a pod that writes `note_type='feature'` against the old CHECK gets a constraint violation on every `kb_write`.
- **Old images tolerate the new column** (`priority` has a DEFAULT and no old code names it), so the rolling window is safe in that direction.
- **First run has an empty pool.** That is the designed empty-state: the block tells agents to file tickets, and the pool fills from the next scholar onward. Do not pre-seed it by hand — watching it fill is the validation.
- **Live check:** add one `feature` ticket at `high` from the cockpit panel, wait for the next checkpoint critic, and confirm (a) the kickoff in `task_brief.md` contains the `PROJECT BACKLOG` block with the counts line first, (b) the critic's campaign names that `note_id` as its initiative, (c) on `ship`, both `knowledge/<id>.md` in the jobs repo and the `knowledge_index` row read `resolved`.

## Out of scope (spec §"Out of scope for v1")

Resource pool and concurrent workers; multiple loops per project; the dispatcher scheduler mode; binding priority; subloops; ticket dependencies or epics; Obsidian/cloud sync; automatic dedup.
