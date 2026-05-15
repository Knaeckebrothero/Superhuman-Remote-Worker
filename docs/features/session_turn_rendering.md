---
tags:
  - feature
  - sessions
  - cockpit
  - ux
aliases:
  - turn rendering
  - ai message structure
  - collapsible turns
  - chat turn cards
related:
  - "[[sessions]]"
  - "[[persistent_chat_ui_redesign]]"
  - "[[persistent_chat_visual_refresh]]"
  - "[[session_narration]]"
  - "[[headless_persistent_sessions]]"
  - "[[dynamic_canvas]]"
---

# Session Turn Rendering

> One AI "turn" in a persistent session can contain reasoning blocks, plain text, and 100+ tool calls — all interleaved. We need a rendering model that surfaces what's happening live, but collapses sanely once the turn is done.

**Status:** Design — staged plan agreed; Phase 1 ready to implement.
**Filed:** 2026-05-14

## Motivation

Persistent sessions are not a classical chat. A single user prompt typically produces a long AI response composed of:

- One or more **reasoning/thinking** blocks (Claude 4.x with extended thinking; hidden-but-summarized on GPT-5).
- Interleaved **plain text** (preamble, narration, final answer — see [[session_narration]]).
- Many **tool calls**, sometimes 100+ per turn, sometimes batched in parallel.
- Further **thinking** after each tool result, especially with Anthropic's interleaved-thinking mode.

The current persistent chat surfaces these as a flat sequence with no per-turn grouping and no progressive disclosure beyond "expand individual tool result." A long-running turn becomes a wall of cards the user has to scroll past to find the next message. [[persistent_chat_ui_redesign]] called this out; this doc proposes the structural fix.

The goal is a **CLI-agent ↔ chat hybrid**: the running session reads like a CLI agent (live cards as work happens), but once a turn finishes it behaves like a chat message (collapsible bubble, scannable in seconds).

## End-state vision

One AI turn → one **collapsible chat bubble**. Within the bubble: an ordered list of typed "step cards" the user can collapse at two levels.

### Two-level collapse

1. **Per-step**: each thought card, each tool card collapses independently (current behavior, refined).
2. **Whole-turn**: a single chevron at the top of the bubble collapses *every* step into one dropdown, leaving only the final text visible. Expanding restores the full sequence.

While the turn is **streaming**, everything is expanded by default so the user can watch the work. Once `stop_reason=end_turn` lands, long turns auto-collapse (with a configurable threshold) — short ones stay open. The user can override either way.

### Eventual compaction (Phase 2+, not yet)

Once Phase 1 ships and we have lived with it, we'll experiment with merging a short rationale into the tool card that follows it (one combined "why + what + result" card), with nested lists for parallel tool batches. This is captured below but deliberately deferred — we want to learn from the discrete-card version first.

## Model behavior reference

Important to internalize before designing the data model: what the providers actually emit.

| Model class | Reasoning content | Where it appears |
|---|---|---|
| Claude 4.x with extended thinking, no interleaved | Visible `thinking` blocks | Once per API turn, at the start (so once after every tool_result the agent loop sends back) |
| Claude 4.x with interleaved thinking | Visible `thinking` blocks | Can appear *between* tool calls within a single API response |
| GPT-5 / 5.5 reasoning models | Hidden (summary only, if any) | Indicated as "thinking…" while streaming, then collapses to nothing renderable |
| Non-thinking models (Haiku 4.5, older GPT, Sonnet without thinking) | None | No reasoning block at all |

**Key consequence:** the renderer must treat "thought with content" and "thought present but content hidden" as distinct cases. The latter still gets a marker so the user sees that the model deliberated.

### API turn vs. UI turn

An **API turn** ends every time the model emits tool calls — the agent loop sends `tool_result` back and calls again, producing a new assistant response. One **UI turn** (one bubble) is N API responses concatenated, terminating when the model returns text with `stop_reason=end_turn` (or whatever finalization signal the persistent loop uses; see [[headless_persistent_sessions]]).

Users don't care about the API plumbing — they care about logical turns. The renderer hides the API turn boundary entirely.

## Data model

A UI turn is a flat ordered list of typed events:

```ts
type TurnEvent =
  | { type: 'thought';   id, content, status: 'streaming' | 'done' | 'hidden' }
  | { type: 'text';      id, content, status }
  | { type: 'tool_call'; id, name, args, result, status, duration, exit_code? };

type Turn = {
  id;
  events: TurnEvent[];
  status: 'streaming' | 'done';
  startedAt; finishedAt?;
};
```

A reducer consumes the SSE event stream and appends/updates events in place. Each event's `id` is stable across streaming updates so the UI can patch in place without flicker.

### Rationale for flat-with-types vs. nested

We considered grouping thoughts and their following tool(s) into a parent `Step` node. We chose flat because:

- Thought-to-tool cardinality is **1:N** in practice (one thought often precedes batched tools; some thoughts precede only text; some tools follow no thought).
- A flat sequence preserves the structural truth at the data layer; *visual* grouping can happen in the renderer in Phase 2 without changing the data model.
- It keeps the streaming reducer trivial — events arrive in order, the reducer appends.

## Staged implementation

### Phase 1 — discrete cards per event *(start here)*

Each `TurnEvent` renders as its own card inside the turn bubble. No merging, no nesting.

**Card types**:

- **Thought card** — collapsed by default once `done`, expanded while `streaming`. Shows a one-line preview (first ~80 chars) when collapsed. For `hidden` thoughts (GPT-5 etc.), card shows only "Thought for Ns" with no expandable body.
- **Tool call card** — already exists in current chat (`edit_file`, `run_command`, etc.). Keep current rendering; only difference is it now lives inside a turn bubble.
- **Text** — rendered inline as prose (not in a card). Markdown-formatted. Streams character-by-character.

**Turn bubble**:

- Wraps the event list. Has a single header strip with the turn-level chevron, a timestamp, and the model used.
- Two-level collapse:
  - **Expanded (default while streaming, default for short turns)**: full event list visible.
  - **Collapsed**: chevron + summary + final text only. Summary is per-type badges, e.g. `◐ 3 · ✎ 7 · ▶ 2 · ⊙ 4`. Final text is the last `text` event.
- Auto-collapse threshold: turns with > N steps (start with N=8) collapse on `done`. Streaming turns never auto-collapse.

**Out of Phase 1**:

- No merging of thoughts into tool cards.
- No nested parallel-batch rendering.
- No keyboard shortcuts (basic mouse only).
- No first-text-vs-last-text decisions — just show last text.

**Acceptance**:

- A turn with 50 tool calls renders without UI jank.
- Collapsing the turn leaves a single-line summary; expanding restores everything.
- A streaming turn shows cards appearing in order with no layout shift on completion.
- Hidden-reasoning thought cards render meaningfully (no empty bodies).

### Phase 2 — smart merging

After Phase 1 has been in use long enough to learn what feels right:

- **Merge a short thought into the next tool card** when the thought is under a length threshold AND directly precedes a single tool AND no text comes between. The thought appears as a one-line rationale strip inside the tool card.
- **Nest parallel tool batches** inside one merged card when multiple tools follow a single thought (e.g., parallel reads, glob + grep). The card body becomes a list of tools rather than one.
- **Standalone thought cards** remain for: initial planning thoughts (before any tool), reflective thoughts that precede only text, and long thoughts above the length threshold.

The rule:

1. Thought → 1 tool, under length threshold, no intervening text → **merged card**
2. Thought → N tools (batch), under length threshold, no intervening text → **merged card with nested tools**
3. Thought with no following tool → **standalone**
4. Thought above length threshold → **standalone**

### Phase 3 — polish

- **Thought titles in collapsed summary**: instead of just `◐ 3 thoughts`, show first-line snippets of each thought in the collapsed dropdown so the user can read the agent's narrative beats at a glance.
- **First-text-as-subtitle**: when the turn is collapsed, the final text is the headline but the first text (the agent's plan, e.g. "I'll do this in 4 passes") appears as a small subtitle near the chevron.
- **Keyboard shortcuts**: collapse all / expand all (`Ctrl+[` / `Ctrl+]`), navigate between turns, jump to last turn.
- **Hover affordances**: hovering a collapsed turn previews the event list in a tooltip-like overlay without committing to expand.
- **Per-turn metadata strip**: tokens used, cost, model, duration — folded into the header.

## Decisions deferred

These came up in design discussion and were intentionally not resolved:

1. **First-text vs. last-text on collapse.** Final answer or initial plan as the headline? Tentative default: last text (outcome) as headline, first text as subtitle in Phase 3.
2. **Merged-card length threshold.** What constitutes a "short" rationale that can fold into a tool header — 200 chars? 1 line? Decide empirically once Phase 1 ships.
3. **Auto-collapse threshold.** How many events before a finished turn auto-collapses. Starting guess: 8. Revisit after Phase 1.
4. **Visual treatment of hidden thoughts** (GPT-5 case). Do they render as a marker card, or get hidden entirely with a single "thought for Ns" annotation on the next event? Start with marker card, revisit.
5. **Whether tool_result content gets its own collapse level inside the tool card** (current behavior) or is always visible. Out of scope for this doc; touched on by [[persistent_chat_ui_redesign]].

## Out of scope

- **Backend event stream format.** Assumes the existing SSE shape from the persistent loop (see [[headless_persistent_sessions]]). If new fields are needed, those land separately.
- **Between-tool narration content.** What text the model emits is a prompting question handled by [[session_narration]]; this doc only addresses how to render whatever it produces.
- **Canvas / artifact surface.** Long-form outputs that belong in a side panel are [[dynamic_canvas]]'s problem.
- **History view rendering.** The chat history viewer (`chat-history.component.ts`) needs the same model but its interaction patterns differ; address in a follow-up if needed.

## Why this ordering

Phase 1 is the load-bearing piece — it establishes the event-typed data model and the turn-bubble container. Everything in Phase 2 and Phase 3 is a *renderer* change on top of the same data, which means we can experiment and roll back individual ideas without touching state management. Starting with discrete cards also lets us learn which compactions are genuinely useful (and which look clever in mockups but feel cluttered in practice) before committing UI surface to them.
