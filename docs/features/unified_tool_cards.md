---
tags:
  - feature
  - sessions
  - cockpit
  - ux
  - debug
aliases:
  - tool card schema
  - unified tool cards
  - tool call rendering
related:
  - "[[session_turn_rendering]]"
  - "[[sessions]]"
  - "[[persistent_chat_ui_redesign]]"
---

# Unified Tool Cards

> Every agent tool call — in a live session or the debug audit trail — should render through **one** schema-driven card: tool name, input parameters, the result the model saw, and optional execution details. Expanding any card should look the same and tell you the same things, regardless of tool or data source.

**Status:** Slice 1 (schema + registry + `<app-tool-card>` + persistent-chat wiring) shipped + **live-verified on k3d 2026-06-17**. Slices 2–3 pending.
**Closes:** `session_turn_rendering.md` deferred decision #5 ("whether tool_result content gets its own collapse level inside the tool card or is always visible").

## Live verification (slice 1, 2026-06-17)

Drove a real session on k3d (gemma-4-moe-strix): `write_file` → `read_file` →
chained `run_command` (`ls -la && cat notes.md && echo …`). Confirmed live + after
a history reload: verb titles, basename/command hints, the full untruncated
command in the COMMAND block, the diff card for `write_file`, the code block (with
`markdown` language tag) for `read_file`, and the full terminal output for
`run_command`.

**Integration bug found + fixed during the test:** persistent-chat has two
imperative DOM post-processors — `collapseCodeBlocks()` and `addCopyButtons()` —
that target **every** `.message-body pre` taller than 200 px (meant for markdown
code blocks in the agent's prose). They were double-wrapping the card's
`.tc__result` / `.tc__code` pres in a `code-collapse` "Click to expand" disclosure
and adding a duplicate copy button. Fixed by excluding tool-card internals from
both selectors (`:not(.tc__result):not(.tc__code)`). Lesson for slice 2/3: any new
structured `<pre>` inside `.message-body` must opt out of these post-processors.

## Motivation

There are two completely separate tool-card renderers today, fed by two data sources, with overlapping-but-divergent per-tool logic:

| | Persistent chat | Debug agent-activity |
|---|---|---|
| Source | WS stream / `thread_messages` → `ToolCallEvent` | MongoDB audit → `AuditToolInfo` |
| Result render | raw `<pre>` nested in `<details>` (box-in-a-box) | `result_preview` in a styled block |
| Per-tool knowledge | `TOOL_LABELS`, `toolIcon`, `formatToolArgs`, `toolLabelContext`, `fileEditView` | `toolCategories`, `toolCategoryColors`, `getStepBadge` |

Neither shares anything, so every per-tool decision is duplicated and drifts. Concrete UX failures in the chat card:

1. **Read-file result is a `<pre>` floating inside the card** — a box inside a box, visually noisy.
2. **Chained shell commands (`a && b && c`) are truncated in the summary title and exist nowhere in full** — the only copy of the command is the ellipsized header.
3. **No uniform notion of "parameters" vs "result" vs "details"** — so traceability of what the agent actually did is poor.

## End-state

One normalized view-model (`ToolCardView`) that both data sources map into, rendered by **one** presentational component (`<app-tool-card>`), driven by a **per-tool descriptor registry** that is the single source of truth for title/icon/params/result-kind.

```
ToolCallEvent ─┐                              ┌─ persistent-chat (turn bubble)
               ├─ adapter → ToolCardView ─ <app-tool-card> ─┤
AuditToolInfo ─┘     ▲                        └─ debug agent-activity (audit list)
                     │
              TOOL_DESCRIPTORS registry
```

### The schema (`core/models/tool-card.model.ts`)

```ts
type ToolCardStatus = 'pending' | 'running' | 'ok' | 'error' | 'denied';
type ToolResultKind = 'text' | 'code' | 'terminal' | 'diff' | 'json' | 'markdown' | 'none';
type ParamKind = 'code' | 'path' | 'text' | 'json';

interface ToolCardView {
  tool: string;                 // raw id, e.g. 'run_command' — shown small/mono in the body
  title: string;                // verb, e.g. 'Execute command', 'Read file'
  icon: string;                 // material symbol
  subtitle?: string;            // short, single-line, ellipsized collapsed-row hint
  status: ToolCardStatus;
  params: ToolParam[];          // inputs, full (never truncated for display)
  result?: ToolResult;          // the content the model saw
  details: ToolDetail[];        // duration / exit code / size — omitted when empty
  error?: string;               // explicit error message (audit) or errored output
}

interface ToolParam  { label: string; value: string; kind: ParamKind; }
interface ToolDetail { label: string; value: string; tone?: 'default' | 'ok' | 'error' | 'warn'; }
interface ToolResult {
  kind: ToolResultKind;
  content?: string;             // text/code/terminal/json/markdown
  language?: string;            // code highlight hint, derived from file extension
  diffLines?: DiffLine[];       // kind==='diff'
  diffMode?: 'replace' | 'append' | 'prepend' | 'write';
  truncatedLines?: number;      // lines dropped by the render cap
  bytesTotal?: number;          // set when `content` is a preview (debug side)
}
```

### Normalization seam

Both adapters first produce a minimal `NormalizedToolCall`, then a single pure `buildToolCardView()` does all descriptor-driven work. Source-specific code is ~30 lines per side; all tool semantics live once.

```ts
interface NormalizedToolCall {
  tool: string;
  args: Record<string, unknown>;
  status: ToolCardStatus;       // already mapped (completed→ok, success:false→error, …)
  result?: string | null;       // content the model saw (may be a preview)
  resultBytesTotal?: number;
  error?: string | null;
  durationMs?: number;
  exitCode?: number;
}
```

### The descriptor registry (`core/tools/tool-descriptors.ts`)

Replaces all five scattered maps. A generic fallback covers unknown tools (prettified name, all args as params, result as text), so a brand-new tool still renders sanely with zero registry work.

```ts
interface ToolDescriptor {
  title: string;                                       // i18n key resolved with this literal as fallback
  icon: string;
  category?: ToolCategory;
  params: Array<{ key: string | string[]; label: string; kind: ParamKind }>;
  result?: { kind: ToolResultKind; languageFrom?: 'path' };
  subtitle?: (args: Record<string, unknown>) => string;
}
```

Seed examples:

| Tool | title | params | result kind | subtitle |
|---|---|---|---|---|
| `read_file` | Read file | Path | `code` (lang from ext) | basename |
| `run_command` / `shell_execute` | Execute command | Command (full, wrapping) | `terminal` (+ exit/duration details) | first ~48 chars |
| `edit_file` / `write_file` | Edit / Write file | Path | `diff` (reuse `lineDiff`) | basename |
| `search_files` / `web_search` | Search … | Query / Pattern | `text` | the query |

### The component (`ui/tool-card/`)

Standalone, `OnPush`, `input<ToolCardView>()`. Owns its own `<details>`. Fixed body order: **raw tool name → Parameters → Result → Details → Error**. Behaviors:

- **Collapsed header**: `icon · title(verb) · subtitle(hint, ellipsized) · status pill`. Decision #1 resolved → keep a short hint so lists stay scannable; the *full* value always lives in the body.
- **Result block**: a single labelled section, full card width, monospace, with a copy button and a line cap (~200 lines) + "show N more". `kind: 'diff'` renders add/del lines (ported from the current `fileEditView`); `terminal` gets a dark tint; `code` shows a language label. **No nested `<pre>` floating inside the card** — the result *is* the body. Fixes failure #1.
- **Dynamic size** (added 2026-06-17): a collapsed card is a **content-width pill** (`.tc { width: fit-content; max-width: 100% }`) with a tight header row (4px vertical padding) — so the status sits right after the hint instead of being banished to the far right, and short cards don't stretch into full-width bars (a long-command hint still ellipsizes at `max-width: 100%`). On expand (`.tc[open] { width: 100% }`) the card goes **full width** and the result/diff blocks have **no `max-height`**, growing to full content height (the chat scrolls, not a nested 420px box). The ~200-line "show more" cap is the only bound on huge output. Net: short pills in a list, full-size only when opened.
- **Parameters**: each param a labelled block; `code`/`path` wrap and are never truncated. The shell command lives here in full. Fixes failure #2.
- **Details**: `duration`, `exit code` (tone error when non-zero), `preview · N KB total`. Omitted entirely when empty — trivial reads show just Path + content. Addresses #3.
- **Auto-open** on `error` / `denied` (ports current behavior); `defaultOpen` input for callers that want all-open (debug).

## Sequencing

1. **Slice 1 (this change)** — schema + registry + chat adapter + `<app-tool-card>` + wire into `persistent-chat.component.ts` (replace the `#toolDetails` card body; keep the run-fold/grouping around it). Ship + verify on k3d.
2. **Slice 2 (follow-up)** — audit adapter (`toolCardViewFromAudit`) is written and unit-tested in slice 1 to prove the schema is source-agnostic; slice 2 points `agent-activity.component.ts`'s tool step at `<app-tool-card>` and deletes its bespoke tool rendering.
3. **Slice 3 (polish)** — Prism syntax highlighting for `code` results (deps already present: `prismjs`); `chat-history.component.ts` as a third consumer; delete the now-dead SCSS/helpers in persistent-chat.

## Acceptance (slice 1)

- A `read_file` card expands to Path + the file content as a clean full-width code block — no box-in-box.
- A `run_command "a && b && c"` card shows verb "Execute command" + a short hint collapsed; expanded shows the **full** command and the **full** output, plus duration/exit code.
- An `edit_file` card still shows the old→new diff.
- An unknown/new tool renders via the fallback (name + args + text result) with no registry entry.
- Status, denied badge, and auto-open-on-error behavior match today.
- `ruff`-equivalent (`vitest` + `tsc`) green; pure registry/adapter logic unit-tested.

## Open decisions

- **#1 collapsed hint vs pure verb** → resolved: verb + short hint (full value in body). One-line flip if we change our mind.
- **Result line cap** → 200 lines + "show more"; copy always copies full. Revisit empirically.
- **Highlighting** → deferred to slice 3 to keep slice 1 small; language is captured now so the block is forward-compatible.
