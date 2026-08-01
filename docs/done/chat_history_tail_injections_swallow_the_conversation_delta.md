---
tags:
  - issue
  - bug
  - resolved
  - audit-store
  - cockpit
  - debugging
  - context-management
related:
  - "[[debug_audit_view_refactor]]"
  - "[[loop_optimization]]"
  - "[[audit_metadata_config_duplication_ooms_orchestrator]]"
  - "[[agent_tool_fixed_vocabularies_invisible_to_model]]"
---

# Tail-injected context swallows the conversation delta in `chat_history` (and re-stores itself every turn)

**Filed + fixed:** 2026-07-30, from a user report that the debug Chat History
panel renders the `<active_tasks>` todo block on every single turn.
**Status:** **FIXED — shipped on `develop` (`e4244dfe`, docs in `22c07e5f`,
follow-up `a5d93f71`) and confirmed on the live dev cluster 2026-07-30** (see
§4). Three layers changed: the archiver's delta extraction (write), a lean
listing + per-entry detail endpoint (read), and the cockpit chat panel (render).
**Components:** `src/core/archiver.py` · `orchestrator/database/audit_store.py`
· `orchestrator/main.py` · `orchestrator/services/formatters.py` ·
`cockpit/src/app/{core/services,core/models,views/chat-history}/**`.

---

## 1. Symptom

Two complaints, one root cause:

1. **Every turn renders the full todo list.** The Chat History panel shows a
   `👤 Human` bubble containing `<active_tasks>\nCurrent Tasks — Phase 1 …` on
   turn after turn, drowning the actual conversation.
2. **Tool results are never available.** Every tool card expands to
   *"Result pending or not available"*, and long messages end in `[truncated]`
   with no way to see the rest — the Request Viewer was the only escape.

Both are the visible surface of the same write-side defect. Symptom 2 is the
more serious one: the data isn't hidden, it was **never stored**.

## 2. Root cause

`LLMArchiver._archive_chat_entry` derives a turn's "new inputs" as *everything
after the last `AIMessage`* in the message list it is handed — and `graph.py`
hands it `prepared_messages`, i.e. the payload **including** the transient
tail-injection block.

Since [`loop_optimization`](../features/loop_optimization.md) F37 moved the
transient injections to the tail (prompt caches match a strict left-to-right
prefix, so a block that mutates every turn must sit *below* the stable
history), a typical payload ends:

```
AIMessage(real assistant turn, tool_calls=[call_abc])
ToolMessage(call_abc → real result)        ← the actual delta
AIMessage(kb_search, synthetic)            ← last_ai_idx lands HERE
ToolMessage(knowledge_inject_… → block)    ← recorded as inputs[0]
HumanMessage("<active_tasks>…")            ← recorded as inputs[1]
```

The scan therefore anchors on the **synthetic** `AIMessage` of an injection
pair, so the archived `inputs` are exactly *[injected knowledge, injected
todos]* — the real tool results are dropped, and the entire re-injected frame
is re-stored verbatim on every turn.

Two consequences follow mechanically:

- `getToolResult()` in the cockpit matched the next turn's inputs by
  `tool_call_id`; the only ids stored are `knowledge_inject_*`, so no real tool
  call could ever resolve → permanent "Result pending".
- `chat_history` becomes almost entirely duplicated context.

The bug is **intermittent by design**: it fires only when an injection that
carries a synthetic `AIMessage` (memory / knowledge / citation feedback /
instruction files) is active. With only the todos block injected there is no
synthetic `AIMessage`, the anchor stays on the real turn, and the delta
survives — which is why it went unnoticed.

### Measured on the dev cluster (`srw-auditdb`, 2026-07-29)

Job `4119f03c` (developer, 278 turns, knowledge injection on):

| metric | value |
|---|---|
| turns carrying the `<active_tasks>` block | **278 / 278** |
| turns with a synthetic injection input | 274 |
| …of those, turns whose **real delta was lost** | **274** |
| stored input bytes that are re-injected context | **3127 kB of 3152 kB (99.2 %)** |
| actual conversation content | ~25 kB |

Job `afd971bf` (scholar, 154 turns): 133 turns lost their delta. The
`chat_history_p2026_07` partition is 428 MB across 65,945 rows.

A direct read confirms the mechanism — iterations 12→23 store only
`knowledge_inject_d449e62d`, and the real `call_*` results reappear at
iteration 24 exactly when the knowledge block goes empty:

```
id    | iter | n_in | in_tool_ids                | resp_tc_ids
76624 |  12  |  2   | knowledge_inject_d449e62d  | call_141010331baf4d1dab96059f
…
76693 |  23  |  2   | knowledge_inject_2241a15b  | call_e2dfd38bf1334a98a341aa7c
76700 |  24  |  2   | call_e2dfd38bf1334a98a341aa7c | call_07c54c63…, call_97c2ae04…
```

## 3. Fix

### 3.1 Write — archive the delta, describe the frame (`src/core/archiver.py`)

`_archive_chat_entry` now partitions the payload with the existing
`is_workspace_injection_message()` predicate before scanning, so the anchor is
the last **real** `AIMessage` and real tool results are stored again.

The injections are not dropped — they are archived as compact
`type="context"` input entries carrying `kind`, an 8-hex content `hash`,
`chars`, `label` (instruction file path), and a 500-char `content_preview`.
Full `content` is written **only on the turn a block's hash changes**, tracked
per job in an in-memory `_context_hashes` map (bounded to 64 jobs; a process
restart just re-stores full content once). Change points therefore stay
reconstructable from `chat_history` alone, while steady-state turns cost a few
hundred bytes instead of several kB. Instruction blocks are keyed by label so
multiple files injected in one turn don't clobber each other.

Also widened: `response.tool_calls[].args` now stores arguments up to 4000
chars alongside the 200-char `args_preview`, so a `shell_execute` command is
readable in the panel without opening the request. It is omitted when the
preview already covers it — no duplicate copy for short args.

### 3.2 Read — lean listing + per-entry detail

Mirrors the split the audit stream already uses (`lean=` + `/audit/step/{id}`):

- `GET /api/jobs/{id}/chat?lean=true` → previews plus `truncated` / `chars`
  markers; full bodies, per-tool-call `args`, and `reasoning.content` stripped
  by the pure, non-mutating `_lean_chat_doc`.
- `GET /api/jobs/{id}/chat/entry/{entry_id}` → the complete row
  (`AuditStore.get_chat_entry`, job-scoped, int-parsed → 404-after-auth).
- The MCP `_format_chat_entry` collapses the frame to a single
  `[context]: knowledge, todos (re-injected each turn)` line, classifying both
  the new descriptors and legacy rows (by `<active_tasks>` prefix and
  `*_inject_` tool-call ids). MCP consumers stop paying for the duplication too.

### 3.3 Render — context strip + lazy hydration (cockpit)

- `ChatTraceService` pages with `lean=true` and gains `hydrateEntry(id)`, which
  fetches the full turn and swaps it into `rows` in place. Concurrent expands
  of one entry share a request; a hydration that loses to a job switch is
  discarded via the existing epoch token.
- The component splits each turn into *delta* (real human turns, real tool
  results) and *context frame*, rendering the latter as one muted collapsed
  strip — `⧉ Injected context  memory todos` — with a per-item change dot, size
  and hash on expand. Legacy rows are classified client-side, so the fix
  applies retroactively to every row already in the store.
- Tool results now resolve through a `tool_call_id → result` map built over the
  **whole loaded window** instead of peeking at `entries[idx+1]`; that survives
  empty-delta turns and page boundaries. Unresolved calls are labelled by
  `resolveToolResultState()` — see §3.4.
- Every message, reasoning block, tool result, args block and context item gets
  a `Show full (5.2 kB)` / `Collapse` control that hydrates on first expand.
- **Deleted:** the shell-state widget (~150 lines + styles + 5 i18n keys). It
  keyed on `ChatEntry.shell_state`, which neither the archiver nor
  `_chat_row_to_doc` has ever emitted — dead since the Postgres cutover.

### 3.4 Follow-up — "not loaded yet" is not "never recorded" (`a5d93f71`)

The first cut labelled every unresolved tool call *"result arrives in a later
turn"* whenever `hasMore()` was true. On a partially loaded job that is a lie
for all but one row: a result lives in the **next** turn's inputs, so only the
**last loaded** turn can still be waiting on data. Every earlier unresolved call
was never recorded — which is exactly the state the write-side bug left behind,
so the panel was reassuring the reader that lost results were still coming.

The pure exported `resolveToolResultState(entryId, lastLoadedEntryId, hasMore)`
now returns `unloaded` only for `entryId === lastLoadedEntryId && hasMore`, and
`missing` otherwise (including an empty window, which would otherwise render as
perpetually loading). `unloaded` shows a spinner plus a **Load it** action
calling `loadMore()`; `missing` says *"No result recorded for this call"*.

Deliberately **not** auto-fetching the next page when the last row is expanded:
that cascades one page fetch per page and re-creates the eager download this
whole refactor removed. i18n: `resultPending` removed; `resultNotRecorded` /
`resultInLaterTurn` / `resultLoadNext` / `resultLoading` added in en + de-DE
parity.

## 4. Verification

- **Unit.** 6 new Python tests (`tests/test_archiver_pg.py` delta-survives-
  injections / content-only-on-change / tool-call args; `tests/test_chat_lean.py`
  lean projection + non-mutation + formatter collapse for new *and* legacy
  rows) and 11 new vitest cases (service paging/hydration/dedupe/epoch-loss;
  component split + classification + result index). Full suites: **2236 pytest
  passed**, **1471 vitest passed**, ruff + `ng build` clean.
  (`tests/test_database_phase1.py::test_connect_disconnect` fails identically
  on a clean tree — it wants a live local Postgres.)
- **k3d API.** `pageSize=2` → lean 5087 B vs full 18042 B; detail endpoint
  200 / 404 on a bad id / 404 on a valid id from another job; lean rows carry
  `truncated:true`+`chars`, the detail row restores every body.
- **k3d browser (Playwright).** On tail-era job `3adc5d1b`: 100/100 turns render
  a collapsed context strip, **zero** raw `<active_tasks>` bubbles (was 100),
  strips expand to `memory 740 B` / `todos 5.2 kB`. Hydration expanded a human
  turn from the 500-char preview to 5623 chars. On a pre-tail-era job, 93/100
  tool calls show real results with working expand controls. No console errors
  from the panel.
- **Live write path confirmed on dev (2026-07-30).** Deploy carrying the fix is
  `d7027501` (`sha-576a15f`). Job `b5134690` (09:00Z, 168 turns) archives real
  tool results per turn (`[tool]: Written: plan.md`, …) alongside the `context`
  descriptors. Job `e239ef27` (06:20Z, 69 turns) — same day, older agent image —
  has **zero** tool inputs across all 69 turns: the pre-rollout write path, lost
  for good. Injection kind does *not* explain the split; memory and knowledge
  injections share the synthetic `AIMessage`+`ToolMessage` shape
  (`src/core/memory_injection.py:22-52`, `knowledge_injection.py:266-296`) and
  poisoned the old delta scan equally. The variable is the agent image.
- The `a5d93f71` follow-up (§3.4) adds 5 vitest cases (**1476 green**) and is
  **not** browser-driven: staging both states live needs a >100-turn job for
  `unloaded` and a pre-fix job for `missing`, and the k3d orchestrator was
  churning at the time. Logic is pure and unit-covered; the render path is the
  same one already browser-verified above.

## 5. Residual

- **Old rows keep their loss.** Tool results dropped by the write path are
  unrecoverable from `chat_history`; they remain in `llm_requests` (the full
  payload is archived there), reachable via the turn's request-id link. The UI
  classification makes legacy rows readable, not complete.
- **No backfill.** Existing rows keep the duplicated frame. This is storage
  weight only (~99 % of chat input bytes on injection-heavy jobs); a
  `chat_history` prune/rewrite is a cleanup, not a fix, and would destroy the
  only record of what was injected when.
- `_context_hashes` is per-process, so a worker restart mid-job re-stores each
  block's full content once. Deliberate: the alternative is a read-back per
  turn.
