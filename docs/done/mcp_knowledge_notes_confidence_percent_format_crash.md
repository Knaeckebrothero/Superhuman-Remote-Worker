# MCP `list_knowledge_notes` crashes: `%` format applied to enum-string confidence


**Closed by the 2026-08-06 doc-truth sweep (batch #3):** Fixed `5f8b6047` — _fmt_confidence() routes all three format sites (file since relocated to src/shared/orch_surface/formatters.py); tests/test_formatters_confidence.py includes a direct regression of the crash string.

**Status**: Fixed + verified live 2026-07-11 on the main cluster. `_fmt_confidence()`
in `orchestrator/services/formatters.py` routes all three sites; covered by
`tests/test_formatters_confidence.py`. Filed 2026-07-09 during the Better Resavio
loop audit (project `68137e29`). Acceptance criterion #1 confirmed post-deploy:
`list_knowledge_notes` for `68137e29-6b1f-4f1b-a0c1-4e6dc2be3f9a` returns a clean
50-of-2053 listing (`confidence: high`/`medium` render as enum strings, no-confidence
notes omit the field), and `get_knowledge_note` renders `Confidence: medium` with
no `%`-format crash.

## Context

While auditing the Better Resavio loop over the orchestrator MCP server, every
`list_knowledge_notes` call against the project failed with:

```
Error calling tool 'list_knowledge_notes': Unknown format code '%' for object of type 'str'
```

The tool is unusable for effectively **every populated project KB** (see below —
the DB default makes the crashing branch the common case). Browsing a project's
notes currently requires falling back to `search_knowledge` + `get_knowledge_note`
one note at a time.

## Problem

`orchestrator/services/formatters.py` formats note confidence with a **float**
format spec:

```python
# format_knowledge_notes (serves MCP list_knowledge_notes), ~line 2037
if confidence is not None:
    header += f" (confidence: {confidence:.0%})"

# format_knowledge_note_detail (serves MCP get_knowledge_note), ~line 2073
if confidence is not None:
    lines.append(f"Confidence: {confidence:.0%}")
```

But the knowledge-notes store defines confidence as a **Postgres enum string**,
not a float:

```sql
-- orchestrator/database/vector_schema.sql:34,93 (and migrations/vector/0001_initial.sql)
CREATE TYPE confidence_level AS ENUM ('high', 'medium', 'low');
...
confidence confidence_level DEFAULT 'high',
```

`f"{'high':.0%}"` raises `ValueError: Unknown format code '%'`. Because the
column defaults to `'high'`, any list page containing at least one
non-null-confidence note — i.e. essentially any real project — kills the whole
listing. The MCP handler chain is
`orchestrator/mcp/server.py:1694` → `client.list_knowledge_notes`
(`orchestrator/mcp/client.py:1567`) → `fmt.format_knowledge_notes(data)`.

`format_knowledge_note_detail` (~line 2073) has the identical defect. In live
calls it happened to survive because the detail payload carried no
`confidence` field for the notes fetched (no `Confidence:` line was printed),
but the code path is latently the same crash.

Same-class defect nearby: the frozen-job formatter guards numerics but not
strings —

```python
# formatters.py ~line 956
f"Confidence: {conf:.0%}" if conf <= 1 else f"Confidence: {conf}"
```

`'high' <= 1` raises `TypeError`, so a string confidence in freeze data would
crash this path too (agents do emit both numeric and verbal confidences; the
iter-23 Better Resavio critic note alone contains `confidence: 0.82` and
`confidence: high` variants in its frontmatter).

## Proposal

Add one defensive helper in `formatters.py` and use it at all three sites:

```python
def _fmt_confidence(value: Any) -> str:
    """Render confidence that may be a 0-1 float, another number, or an enum string."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:.0%}" if 0 <= value <= 1 else str(value)
    return str(value)
```

- `format_knowledge_notes` → `header += f" (confidence: {_fmt_confidence(confidence)})"`
- `format_knowledge_note_detail` → same substitution
- frozen-job formatter (~line 956) → replace the `conf <= 1` ternary with the helper

Formatter-layer-only change; no API or schema change. The MCP result is
human/agent-readable text, so `confidence: high` rendering as-is is the desired
output, not a degradation.

## Acceptance

- `list_knowledge_notes` returns a note listing for the Better Resavio project
  (`68137e29`) on the main cluster instead of the `%`-format error.
- Unit tests in `tests/` cover `format_knowledge_notes`,
  `format_knowledge_note_detail`, and the frozen-job confidence line with each
  of: enum string (`'high'`), float (`0.82`), int/out-of-range numeric, `None`
  (line omitted), and none of them raise.
- `grep -n ':\.0%' orchestrator/services/formatters.py` shows no remaining
  unguarded `%`-format on a value that can be a string.

## Resolution

Implemented 2026-07-09 in `orchestrator/services/formatters.py`:

- Added `_fmt_confidence(value)` — bool → `str`; `0 <= number <= 1` → `{n:.0%}`;
  any other number → `str`; everything else (enum strings) → `str`. This is now
  the sole `:.0%` site in the file.
- `format_knowledge_notes` and `format_knowledge_note_detail` route confidence
  through it — fixes the live crash and the latent detail-formatter crash.
- `format_frozen_job` also routes through it. Note: that site was **already**
  guarded (`isinstance(conf, (int, float))` with a string fall-through), so it
  was not crashing in the current tree — the original TypeError characterization
  applied to a `conf <= 1`-only version. Folding it into the helper removes the
  duplicated logic and drops the `bool`-renders-as-`100%` edge case.
- `tests/test_formatters_confidence.py` covers all three formatters plus the
  helper across enum string / unit float / out-of-range numeric / bool / `None`.

## Notes

Low risk, single file + tests. Worth a quick sibling grep for other
`:.0%`/`:.1f` format specs fed by API dicts in `formatters.py` while in there —
the pattern (format spec assumes type the API doesn't guarantee) is likely not
unique to confidence. Found alongside (unrelated, separately tracked): stale
`offline` agent rows accumulating in `list_agents`.
