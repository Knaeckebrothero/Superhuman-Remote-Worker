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
  - "[[session_wake_on_job_completion]]"
---

# Unified Tool Cards

> Every agent tool call — in a live session or the debug audit trail — should render through **one** schema-driven card: tool name, input parameters, the result the model saw, and optional execution details. Expanding any card should look the same and tell you the same things, regardless of tool or data source.

**Status:** Slice 1 (schema + registry + `<app-tool-card>` + persistent-chat wiring) shipped + **live-verified on k3d 2026-06-17**. Slices 2–3 pending. **Slice 4 — the job card: BUILT + partially live-gated 2026-07-29** (API contract verified, three bugs found and fixed; visual pass still owed) — see [Slice 4 — what shipped](#slice-4--what-shipped-2026-07-29) and [Live gate](#live-gate-on-dev-2026-07-29--partial-and-it-found-three-bugs). It is the first card that outlives its tool call and the first to carry actions, so it extends the schema rather than just adding a descriptor.
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

## The job card (slice 4 — design, 2026-07-26)

`create_worker_job` has **no descriptor entry today**
(`core/tools/tool-descriptors.ts` — `delegate_work` is the only delegation entry),
so a session that schedules a job renders it through the generic fallback: tool
name, args as params, "Job created successfully. Job ID: …" as text. For the
workflow in [[session_wake_on_job_completion]] — where a session schedules jobs
continuously and works with results as they land — that card is the primary
surface, and the fallback is not enough.

### Why it doesn't fit the current schema

Every card in slices 1–3 is a **record of something that already happened**. The
tool ran, it returned, the card shows what the model saw. `ToolCardView` is a
snapshot and `ToolCardStatus` describes the *call*.

A job card is a **handle on something still happening**. The tool call returns in
milliseconds ("job created, id=…"); the thing the user cares about runs for
twenty minutes afterwards, changes status several times, and then wants acting
on. Three consequences:

1. **Status must track the job, not the call.** The call is `ok` the instant it
   returns. The card needs `created → processing → pending_review → completed |
   failed | cancelled`.
2. **The card must update after its turn is over** — including after a page
   reload, days later.
3. **It carries actions.** Every other card is inert.

So slice 4 is a schema extension, not a registry entry. Minimum additions:

```ts
interface ToolCardView {
  // …existing fields…
  entity?: { kind: 'job'; id: string };   // what to watch; absent = inert card (all cards today)
  live?: boolean;                          // subscribe/poll while non-terminal
  actions?: ToolCardAction[];              // rendered in the returned state
}

interface ToolCardAction {
  label: string;
  kind: 'approve' | 'resume' | 'cancel' | 'link';
  confirm?: boolean;                       // resume-with-feedback opens a text input
}
```

`entity` is the whole trick: it turns a card from a transcript artifact into a
view onto a row that keeps changing, and it generalizes (a future
`create_project` or `start_session` card would use the same seam).

### Three states

- **Proposed** — the config the agent drafted, reviewable, with Start / Edit /
  Discard. **Deferred, deliberately** — see the 2026-07-26 entry in
  [[session_wake_on_job_completion]]'s decision log. A per-job Start button
  cannot express the ordering or the human gates in a real multi-stage plan, and
  the retired "builder" already established that nobody read generated configs.
  Prose proposal in chat plus "go" is cheaper *and* better.
  **Salvaged piece:** show the *resolved* config read-only on a created card, so
  what actually ran is auditable without any draft state.
- **Running** — the `Delegate-A-Compact-List-Rows` vocabulary: role, mission,
  status chip, elapsed, a live status strip. This is the state the mockups
  already solve.
- **Returned** — summary, plus the actions. This is the state that matters most
  and the one no mockup covers yet.

### The returned state is where the review loop moves

Today, finishing a job means leaving the conversation: go to the Jobs page, read
the diff, approve or resume with feedback. But the session already holds
`approve_worker_job`, `resume_worker_job`, `cancel_worker_job` and
`get_job_workspace_file`, and the *user's* judgement is what the review needs.

Putting Approve / Resume-with-feedback / Open-diff on the returned card puts the
decision where the conversation is. That is the actual product idea here — the
live status display is table stakes; collapsing the review loop into the
transcript is the win.

### The card does not depend on the wake feature

Worth stating plainly, because it is easy to assume otherwise: **the cockpit can
watch a job without the agent knowing anything.** It has the job id from the tool
result and the jobs API already exists — polling or subscribing is a pure
frontend concern.

Two independent consumers of the same fact:

| Consumer | Needs | Delivered by |
|---|---|---|
| The **user** — sees the card go green | job status | this doc; no backend work |
| The **agent** — can act on the result | a wake | [[session_wake_on_job_completion]] |

They ship independently and in either order. `created_by_thread_id` (from the
wake feature) is not required for a per-call card; it is only required to group
or query "the jobs this session created".

### Fan-out: per-call cards or one grouped card

`create_worker_job` makes **one job per call**, so three jobs from one turn are
three tool calls. Options:

- **Three cards.** Zero new concepts. Verbose for a six-job fan-out.
- **One grouped card, three rows** — the `Delegate-A` mockup, with its status
  strip as a sparkline across the batch.

Lean: group **client-side by assistant turn**. That gets the mockup's look with
no backend concept and no `batch_id` column. Open question — needs checking
whether the renderer can see sibling tool calls within a turn, or whether the
existing run-fold/grouping wrapper in `persistent-chat.component.ts` already
provides the seam.

**`Delegate-D` (hierarchical tree) is rejected for this card.** It renders
nesting via `parent_job_id`, and a session-created job has none — the tool only
sets `parent_job_id` when the *creator is itself a worker job*. The tree would
render one level deep forever. Keep D for a future delegation/subjob view where
the nesting is real.

> **Mockups.** `Delegate-A-Compact-List-Rows.html` and
> `Delegate-D-Hierarchical-Tree.html`, untracked in the repo root as of
> 2026-07-26 (~2 MB each, self-contained bundles). They predate this doc and were
> drawn for `delegate_work`, not `create_worker_job`. Move them somewhere tracked
> if this slice proceeds, or this reference goes stale.

### Feasibility audit (2026-07-26)

**Verdict: feasible, no backend change for v1.** Both open questions the design
flagged resolve favorably. The one genuinely hard part is a decision, not code.

**Resolved — sibling visibility exists.** `groupEvents(events)`
(`core/models/turn.model.ts:416`) is a pure function over the **whole turn's**
event array, returning `EventGroup[]`, memoized per turn at
`persistent-chat.component.ts:3202`, with 53 unit tests. Client-side batching by
assistant turn needs no `batch_id` and no backend concept — add a
`{kind: 'job_batch'}` variant and a pass in `groupEvents`.
*Wrinkle the design didn't anticipate:* `pinnedEventIds()`
(`turn.model.ts:391-401`) always pins the turn's **last** tool call, so three
completed job calls would render as a folded 2-call chip plus one inline card.
Job calls must be exempted from `isFoldable`/pinning **before** the grouping
pass — a real edit to a load-bearing function, not a pure addition.

**Resolved — liveness precedent is already inside `<app-tool-card>`.**
`canvasActionAvailable` / `canvasContextLabel` (`tool-card.component.ts:169-176`)
are `computed()`s reading a `CanvasService` signal, so that card already
re-renders long after its turn ended. The better pattern for job *state* is
`citation.verdict` (`persistent-chat.service.ts:2871-2883`), which patches an
already-rendered element through a **separate signal map** rather than mutating
the turn tree — copy that as `jobsById`, leaving `ToolCardView` and its
identity-keyed `WeakMap` memo untouched. (Do **not** route live status through
`ToolCardView`; you would have to bust that cache on every status change.)

**Job id survives history replay.** `tc.result` is repopulated from
`thread_messages` on reload (`persistent-chat.service.ts:3563-3567`), so the id
is recoverable by parsing `"Job ID: <uuid>"` out of the result string — with an
exact in-repo precedent, `parseCanvasResultMetadata()`
(`tool-descriptors.ts:309-346`), which parses a result as JSON with a regex
fallback and mints an action from it. Copy that shape.

**No job-status push channel exists**, so v1 polls. Patterns to copy:
`datasource-list.component.ts:2334` (`timer` + `takeUntilDestroyed`, stops on
terminal status) over `job-list.component.ts:1147` (bare `setInterval`).
`ApiService.getJobProgress()` (`api.service.ts:1725`) exists with **zero
callers** and is the natural feed. A future `job.status` frame would need zero
transport work — every decoded frame is already forwarded into
`PersistentThreadTransportBridge.events$`, which `CanvasService` consumes as a
refetch trigger. **But** with `replicas: 2` the SSE client and the completing
request are on different replicas ~50% of the time, so any such frame must go
through the existing NATS→SSE bridge, not a local broadcast.

**⚠ The hard part: this codebase has a rule against actionable cards in history.**
There is a documented production incident where SSE replay resurrected a dead
approve button and the click 409'd (`docs/issues/session_silent_failure_audit.md:137-143`).
The fix was a journaled `permission.resolved` frame plus treating 409 as benign,
and the resulting rule is stated in code at `persistent-chat.service.ts:3160-3176`:
the card is **live-only**, and the durable transcript gets a text system message
instead, "because the reason is stale." The tail-anchored `.mile` cards
(permission approve/deny, workspace upgrade) are gated on live signals and are
never reconstructed from history.

Slice 4 wants the opposite, and that has to be **argued, not assumed**. The
argument is a real asymmetry: *a permission request is a moment with no durable
addressable state; a job is a row with a stable id that can be re-fetched.* So
the job card must follow the **canvas** precedent, not the permission one —
`canvasToolCardContext()` (`tool-card.component.ts:22-29`) compares the historical
card's recorded revision against current live state and disables the button when
stale. Concretely: render actions strictly from a **fresh `getJob()`**, never from
the transcript, and treat "already approved" as benign exactly as the 409 path
does.

**Everything else is cheap.** All four actions already exist on `ApiService` —
`approveJob` (`:1599`), `resumeJob` (`:1452`, this is resume-with-feedback),
`cancelJob` (`:1421`), `getJobDiff` (`:2097`), plus `getFrozenJobData` (`:1646`)
for the `pending_review` summary. Auth is free: the global `authInterceptor` adds
`withCredentials`, CSRF and `ngsw-bypass` automatically. **Open-diff is nearly
free** — `JobDiffReviewComponent` accepts `jobId` *or* `threadId` and is
**already imported and mounted inside the chat component** as a drawer
(`persistent-chat.component.ts:624`, `:862`); Monaco loads lazily at runtime.
Service worker is a non-issue: there is no `/api/**` dataGroup (deliberately
deleted in `1195b54d`), so a polled status GET passes through uncached.

**Must be built (none large):** a terminal-status predicate — **none exists
anywhere in the frontend** and polling needs it; a shared status chip
(`jobStatusTone()` is copy-pasted at `job-list.component.ts:1187` and
`job-review.component.ts:923` — extract it); and generalizing `ToolCardAction`,
which is a closed union of `kind: 'open_canvas'` today
(`tool-card.model.ts:57-62`) with emission gated on the canvas-specific
`canvasActionAvailable()` (`:262`).

**Constraints to respect:**
- **Bundle budget is the tightest thing here.** 2.25 MB warning / **2.75 MB hard
  error** (`angular.json:87-97`); initial sits ~2.60 MB with the warning already
  breached, and it fired as a CI failure on 2026-07-23. Chat is the **eager**
  landing route, so anything the card pulls in lands in `initial`. Mitigating:
  the jobs page and diff viewer are already eagerly imported, so `ApiService` job
  methods and `JobDiffReviewComponent` are already paid for.
- Put styles in the new component's own scoped block —
  `persistent-chat.component.scss` is ~60 kB, near the 48 kB
  `anyComponentStyle` error ceiling.
- Any new structured `<pre>` inside `.message-body` must opt out of
  `collapseCodeBlocks()` / `addCopyButtons()` — that bug already bit slice 1.
- New i18n keys must be mirrored in `de-DE.json` or `npm run i18n:check` fails.
  `TestBed.createComponent` does not work under vitest here — test exported pure
  functions instead.

### Slice 4 — what shipped (2026-07-29)

Built against the audit above; every seam it named still existed (paths had
drifted — chat is `views/persistent-chat/`, not `views/chat/`).

| Piece | Where |
|---|---|
| `entity?: ToolCardEntity` on the view | `core/models/tool-card.model.ts` |
| `create_worker_job` descriptor (`result: 'none'` — the receipt is not content) | `core/tools/tool-descriptors.ts` |
| `parseJobEntity()` — id recovered from the result so it survives history replay | same |
| `JobWatchService` — `jobsById` signal map + one poller per *job* | `core/services/job-watch.service.ts` |
| `isTerminalJobStatus` / `isRunningJobStatus` / shared `jobStatusTone` | `core/util/job-status.ts` |
| The panel: status chip, summary, Approve / Open-diff / Cancel | `ui/tool-card/job-tool-card-panel.component.ts` |
| Job-diff drawer, separate signal from the cloud-diff one | `views/persistent-chat/persistent-chat.component.ts` |

Decisions worth keeping:

- **The panel renders outside `<details>`.** Every other card hides its body
  behind a disclosure because it is a record of a finished call. A job card is a
  handle on something running, so its status must be readable and its actions
  reachable without expanding.
- **A child component, not code inside `<app-tool-card>`.** Keeps that component
  what its docstring promises — source-agnostic and presentational — and
  confines the polling/ApiService dependency to the one card that needs it.
  Mirrors `<app-canvas-tool-card-presentation>`.
- **One poller per job, not per card.** `watch()` is idempotent by id, so a
  fan-out where several cards point at the same job issues one request, not N.
- **The id parser anchors on the `Job ID:` label.** The receipt also prints an
  owner id and an agent id; grabbing the first uuid would point the card at the
  wrong entity and still look plausible, because every field is a uuid.
- **`pending_review` is deliberately not terminal.** The poller stops at
  terminal; if it counted, the card would freeze on "awaiting review" and miss
  the approval that flips it to `completed` — the one transition the review loop
  exists to show.
- **The actionable-card rule is satisfied structurally, not defensively.** The
  audit called for treating a stale click as benign. Instead each button is gated
  on the *freshly fetched* status, so the resurrected-dead-button failure cannot
  arise; the post-action refresh closes the remaining race. No bespoke error
  handling — `ApiService` already catches, toasts and returns null, so an inline
  error would double-report.

**Deliberately not built** (each is separable, none blocks the above):

- **Turn-level batch grouping.** The audit found the seam (`groupEvents`) but
  also that `pinnedEventIds()` always pins a turn's last tool call, so job calls
  must be exempted from `isFoldable`/pinning *before* the grouping pass — a real
  edit to a load-bearing function with 53 tests. That is a separate change from
  the card itself, and it only matters for fan-outs.
- **Resume-with-feedback.** Needs a text-input affordance; Approve + Open-diff is
  most of the review loop.
- **Proposed state.** Still deferred by design (see the decision log).
- **The audit/debug surface.** The card is shared, so it renders there too, but
  polling from that surface is untested.

**Verification:** 20 unit tests (pure functions — `TestBed` does not work under
vitest here); full cockpit suite 1453 passing; i18n parity green in both
locales; `ng build` clean with initial at 2.65 MB, under the 2.75 MB hard
ceiling (the 2.25 MB warning was already breached before this change).

### Live gate on dev (2026-07-29) — partial, and it found three bugs

Run against the deployed cockpit (`sha-7fad429`, exactly the card commit) with a
real session-created job. **The API-contract half ran; the visual half did
not** — the browser extension was not connected, so nothing below was seen
rendered. What *was* verified is the integration surface, which is where the
bugs were:

1. **`freeze_data`/`context` arrive as JSON *strings*, not objects.** asyncpg
   hands JSONB back as text and the orchestrator passes it through, while the
   cockpit `Job` model typed them as `Record<string, any>`. Indexing straight in
   type-checks, compiles, and yields `undefined` forever — the summary would
   simply never have appeared, with no error anywhere. Fixed with `asRecord()`;
   the model now types both as `… | string | null` so the compiler can see it.
2. **The summary is not in `context` at all.** An earlier draft read
   `context.summary`, which does not exist. It is `freeze_data.summary` — the
   same source the session-wake payload formatter uses.
3. **`Job` had no `freeze_data` field**, despite the API returning it. Added.

Correcting the `context` type immediately surfaced a **pre-existing** instance
of the same class in `job-review.component.ts`: `context?.['snapshot']?.['status']`
indexed a string, so the "Open IDE" button was quietly hidden for
snapshot-only jobs. Fixed there too.

Also confirmed positively: the id parser matches the tool's **verbatim** dev
output (now pinned as a test — note the receipt repeats the id inside a
`get_worker_job(...)` hint, so label-anchoring matters), and `diff_status` was
`None` on the test job, so Open-diff correctly stayed hidden.

**Still owed:** the visual pass — card renders with the descriptor title rather
than the generic fallback, status chip updates live, polling stops at terminal,
Approve appears only at `pending_review`, Open-diff opens the drawer. Dev
currently has **no job with `diff_status='pending'`**, so exercising Open-diff
needs a Mode-A diff job seeded first.

### Open questions (slice 4)

- ~~**Who is speaking when a job reports back?**~~ **Resolved elsewhere, and
  differently than expected.** [[session_wake_on_job_completion]] shipped its own
  answer — a `role='event'` row rendered as a muted system line — so a completion
  produces both a transcript line (for the agent's context) and a card
  transition (for the user). They are not competing surfaces: the message is what
  the *model* reads, the card is what the *user* watches.
- ~~**Live transport.**~~ **Resolved: polling**, 10 s, one poller per job, stopped
  at terminal status. A `job.status` frame remains the upgrade path and needs no
  transport work — but with `replicas: 2` it must route through the NATS→SSE
  bridge, so it is not free either.
- ~~**How far back does `live` apply?**~~ **Resolved by the terminal predicate.**
  A card whose job is already finished polls exactly once and stops, so a
  months-old transcript costs one request per distinct job on load rather than a
  standing subscription. Still worth watching: a transcript with 50 job cards
  issues 50 requests on open.

## Sequencing

1. **Slice 1 (this change)** — schema + registry + chat adapter + `<app-tool-card>` + wire into `persistent-chat.component.ts` (replace the `#toolDetails` card body; keep the run-fold/grouping around it). Ship + verify on k3d.
2. **Slice 2 (follow-up)** — audit adapter (`toolCardViewFromAudit`) is written and unit-tested in slice 1 to prove the schema is source-agnostic; slice 2 points `agent-activity.component.ts`'s tool step at `<app-tool-card>` and deletes its bespoke tool rendering.
3. **Slice 3 (polish)** — Prism syntax highlighting for `code` results (deps already present: `prismjs`); `chat-history.component.ts` as a third consumer; delete the now-dead SCSS/helpers in persistent-chat.
4. **Slice 4 (the job card) — BUILT 2026-07-29.** `entity` on the schema, a `create_worker_job` descriptor, live status + Approve / Open-diff / Cancel. Batch grouping, resume-with-feedback and the proposed state stay deferred; see [what shipped](#slice-4--what-shipped-2026-07-29). Live gate owed.

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
