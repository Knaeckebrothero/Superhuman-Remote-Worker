import {ChatAttachment, ToolCallInfo} from '../services/persistent-chat.service';

/**
 * Turn-based conversation model for the persistent chat UI.
 *
 * A "turn" is one collapsible chat bubble — for an assistant, that's the
 * span between a user prompt and the next `end_turn` (final text with no
 * further tool calls). A turn carries an ordered list of typed events
 * (thoughts, plain-text blocks, and tool calls) as the agent emits them.
 *
 * See `docs/features/session_turn_rendering.md` for the design rationale.
 * The model is intentionally flat-with-types rather than nested: thoughts
 * and tools have a 1:N (not 1:1) cardinality at the event level, and the
 * renderer is free to merge them visually without distorting the data.
 */

export type ThoughtStatus = 'streaming' | 'done' | 'hidden';
export type TextStatus = 'streaming' | 'done';

export interface ThoughtEvent {
    kind: 'thought';
    /** Stable id: `${turnId}.b${blockIndex}`. Survives SSE replay. */
    id: string;
    /**
     * Id of the AI message this reasoning belongs to (the thread_messages row
     * id; matches the live `thinking` frame's `message_id`). Set for reasoning
     * delivered via `reasoning_content` (gemma/DeepSeek/OpenRouter). Used to
     * dedupe a frame replayed after history already rendered the bubble. May be
     * undefined for older rows or interleaved Anthropic/Responses thinking.
     */
    messageId?: string;
    /** Accumulated content. Empty when status === 'hidden'. */
    content: string;
    status: ThoughtStatus;
    startedAt: number;
    durationMs?: number;
}

export interface TextEvent {
    kind: 'text';
    id: string;
    content: string;
    status: TextStatus;
    startedAt: number;
}

/**
 * Tool-call event. Extends ToolCallInfo so the existing component helpers
 * (toolLabel, groupToolCallsHuman, etc.) keep working unchanged.
 */
export interface ToolCallEvent extends ToolCallInfo {
    kind: 'tool_call';
    startedAt: number;
    durationMs?: number;
    /** Optional secondary status for nicer rendering (exit code, error class). */
    resultStatus?: 'ok' | 'error' | 'denied';
    exitCode?: number;
}

/**
 * Mid-turn context-compaction marker. A `role='summary'` row whose
 * `turn_number` falls inside a grouped assistant turn renders as an inline
 * event at its true position in the event stream — the turn block anchors at
 * its first row, so a top-level CompactionTurn divider would otherwise trail
 * the whole turn's content (the "summary rendered below the reply" bug).
 */
export interface CompactionEvent {
    kind: 'compaction';
    id: string;
    /** The summary the agent produced. May be empty when unavailable. */
    summary: string;
    startedAt: number;
}

export type TurnEvent = ThoughtEvent | TextEvent | ToolCallEvent | CompactionEvent;

export type AssistantTurnStatus = 'streaming' | 'done' | 'interrupted' | 'error';

export interface AssistantTurn {
    kind: 'assistant';
    id: string;
    events: TurnEvent[];
    status: AssistantTurnStatus;
    /** Model identifier, populated from the backend `turn.started.model` when present. */
    model?: string;
    startedAt: number;
    finishedAt?: number;
    /** Optional totals — populated in Phase 3, ignored in Phase 1. */
    totals?: { inputTokens?: number; outputTokens?: number; costUsd?: number };
    /** True for turns rehydrated from REST history (not streamed live this session). */
    historical?: boolean;
    /** True for turns synthesised by the reducer to absorb streaming events
     * that arrived without a preceding `turn.started` (e.g. SSE replay
     * cursor past the start event after a mid-turn reconnect). See
     * docs/issues/persistent_chat_lost_assistant_turn_on_mid_turn_reload.md
     * §Approach 2. The turn gets promoted to the real id (or closed) when
     * `turn.completed` / `turn.interrupted` finally arrives. */
    recovered?: boolean;
}

export interface UserTurn {
    kind: 'user';
    id: string;
    content: string;
    attachments?: ChatAttachment[];
    timestamp: number;
    historical?: boolean;
}

/**
 * System-emitted line — slash-command echoes, session lifecycle markers,
 * VM-upgrade notices. Renders as a single muted line, not a bubble.
 */
export interface SystemTurn {
    kind: 'system';
    id: string;
    content: string;
    timestamp: number;
}

/**
 * Compaction boundary — emitted when the agent summarized the conversation
 * (manual `/compact` or automatic `ensure_within_limits`). Renders as a
 * centered divider banner (like the session-ended marker), expandable to the
 * summary text, so the user can see the exact state the agent works from.
 */
export interface CompactionTurn {
    kind: 'compaction';
    id: string;
    /** The summary the agent produced. May be empty when unavailable. */
    summary: string;
    timestamp: number;
}

export type Turn = AssistantTurn | UserTurn | SystemTurn | CompactionTurn;

/**
 * Top-level conversation state held in PersistentChatService.
 * One object replaces the old `messages`, `streamingText`, `streamingThinking`,
 * and `currentToolCalls` signals.
 */
export interface ConversationState {
    threadId: string | null;
    turns: Turn[];
    /** Id of the in-flight assistant turn, or null when no turn is streaming. */
    activeAssistantTurnId: string | null;
}

export const EMPTY_CONVERSATION: ConversationState = {
    threadId: null,
    turns: [],
    activeAssistantTurnId: null,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

export function isAssistantTurn(t: Turn): t is AssistantTurn {
    return t.kind === 'assistant';
}

export function isUserTurn(t: Turn): t is UserTurn {
    return t.kind === 'user';
}

export function isSystemTurn(t: Turn): t is SystemTurn {
    return t.kind === 'system';
}

export function isCompactionTurn(t: Turn): t is CompactionTurn {
    return t.kind === 'compaction';
}

export function isThought(e: TurnEvent): e is ThoughtEvent {
    return e.kind === 'thought';
}

export function isText(e: TurnEvent): e is TextEvent {
    return e.kind === 'text';
}

export function isToolCall(e: TurnEvent): e is ToolCallEvent {
    return e.kind === 'tool_call';
}

/**
 * Returns the last text event in a turn (the "headline" used in the
 * collapsed-turn view). Returns undefined for turns without any text.
 */
export function lastTextOf(turn: AssistantTurn): TextEvent | undefined {
    for (let i = turn.events.length - 1; i >= 0; i--) {
        const e = turn.events[i];
        if (isText(e)) return e;
    }
    return undefined;
}

/**
 * Returns the first text event in a turn. The agent's opening line usually
 * states what it's about to do, which makes a far more useful collapsed
 * headline than the trailing text (often just "Done."). Symmetric with
 * lastTextOf.
 */
export function firstTextOf(turn: AssistantTurn): TextEvent | undefined {
    for (const e of turn.events) {
        if (isText(e)) return e;
    }
    return undefined;
}

/**
 * The turn's "final answer": the trailing run of text events with no tool call
 * or thought after them — the prose the model ended the turn on. This is the
 * part worth keeping fully visible even when the turn is collapsed; collapsing
 * folds only the lead-up (opening text, reasoning, tool calls), never the
 * answer. Multiple trailing text blocks are joined with a blank line.
 *
 * Returns '' when the turn ends on a tool call or thought (no closing prose) or
 * has no events — callers fall back to the one-line headline in that case.
 */
export function trailingText(turn: AssistantTurn): string {
    const parts: string[] = [];
    for (let i = turn.events.length - 1; i >= 0; i--) {
        const e = turn.events[i];
        if (!isText(e)) break;
        parts.push(e.content);
    }
    return parts.reverse().join('\n\n').trim();
}

/**
 * First sentence of a (possibly markdown) block, for a one-line headline.
 * Drops leading markdown markers, collapses whitespace, cuts at the first
 * sentence terminator at/after a sensible minimum length, and caps the
 * result. Returns '' for empty/whitespace input.
 */
export function firstSentence(text: string, maxLen = 140): string {
    if (!text) return '';
    // Strip a leading/trailing code fence (```lang … ```) so a turn whose first
    // text is a fenced code block reads as the code's first line rather than
    // literal backticks. Then drop a leading markdown marker (#, >, bullet) and
    // collapse whitespace so the headline reads as prose, not raw source.
    let s = text
        .trim()
        .replace(/^`{3,}[^\n]*\r?\n?/, '')
        .replace(/\r?\n?`{3,}\s*$/, '')
        .replace(/^[#>\s]*[-*]?\s*/, '')
        .replace(/\s+/g, ' ')
        .trim();
    if (!s) return '';
    for (let i = 11; i < s.length; i++) {
        const ch = s[i];
        if ((ch === '.' || ch === '!' || ch === '?') && (i + 1 >= s.length || s[i + 1] === ' ')) {
            s = s.slice(0, i + 1);
            break;
        }
    }
    if (s.length > maxLen) s = s.slice(0, maxLen - 1).trimEnd() + '…';
    return s;
}

/**
 * Per-type event counts for the collapsed-turn badge.
 */
export interface TurnEventCounts {
    thoughts: number;
    texts: number;
    tools: number;
}

export function countEvents(turn: AssistantTurn): TurnEventCounts {
    let thoughts = 0;
    let texts = 0;
    let tools = 0;
    for (const e of turn.events) {
        if (isThought(e)) thoughts++;
        else if (isText(e)) texts++;
        else if (isToolCall(e)) tools++;
        // compaction markers don't count toward the badge
    }
    return {thoughts, texts, tools};
}

// ---------------------------------------------------------------------------
// Event grouping (render-time, Slice 3 / Phase 2 of session_turn_rendering)
// ---------------------------------------------------------------------------

/** An event that may be folded away into a summary chip. */
export type FoldableEvent = ToolCallEvent | ThoughtEvent;

/**
 * A view-time grouping of a turn's events, built around the *live edge*: the
 * work happening right now stays visible as cards, and everything already
 * finished collapses into one chip.
 *
 * This supersedes the earlier "runs of 4+ consecutive tool calls fold" rule,
 * which barely helped in practice — a thought between two tool batches broke
 * the run, so a turn that alternated thought/tools/thought/tools rendered as a
 * dozen separate rows even though every run was individually folded. Thoughts
 * are foldable here, and a run is only broken by text, so the whole lead-up
 * collapses to a single chip.
 */
export type EventGroup =
    | {kind: 'single'; id: string; event: TurnEvent}
    | {kind: 'folded'; id: string; events: FoldableEvent[]};

/**
 * Below this many foldable events in a row, render them as plain cards instead
 * of a chip. A "1× thought" chip is the same height as the thought card it
 * replaces and strictly less informative, so folding one event never pays.
 */
export const MIN_FOLD_RUN = 2;

export function isFoldable(e: TurnEvent): e is FoldableEvent {
    return e.kind === 'tool_call' || e.kind === 'thought';
}

/**
 * Whether this event is still in flight. Tool results can land out of order, so
 * this is a per-event property, not "is it near the end of the array".
 */
export function isEventInFlight(e: TurnEvent): boolean {
    if (e.kind === 'tool_call') return e.status === 'pending' || e.status === 'running';
    if (e.kind === 'thought') return e.status === 'streaming';
    return false;
}

/**
 * The events that must stay visible as cards:
 *   - anything in flight — the whole point is watching work happen. Fire five
 *     tools at once and all five show; each drops into the chip as it returns.
 *   - the turn's most recent tool call, always, so a finished turn still shows
 *     what it last did rather than collapsing to a bare counter.
 */
export function pinnedEventIds(events: TurnEvent[]): Set<string> {
    const pinned = new Set<string>();
    for (const e of events) {
        if (isEventInFlight(e)) pinned.add(e.id);
    }
    for (let i = events.length - 1; i >= 0; i--) {
        if (isToolCall(events[i])) {
            pinned.add(events[i].id);
            break;
        }
    }
    return pinned;
}

/**
 * Partition a turn's flat event list into render groups.
 *
 * Text and compaction never fold — text is the answer, and a compaction marker
 * is too significant to hide. Every other event folds unless it's pinned, and
 * contiguous unpinned stretches merge into one chip. Order is preserved, so a
 * completed call sandwiched between two in-flight ones simply renders inline.
 *
 * A group's `id` (= its first member's event id, stable across SSE replay) is
 * suitable for `@for (… ; track group.id)`.
 */
export function groupEvents(events: TurnEvent[]): EventGroup[] {
    const pinned = pinnedEventIds(events);
    const groups: EventGroup[] = [];
    let run: FoldableEvent[] = [];
    const flush = () => {
        if (run.length === 0) return;
        if (run.length < MIN_FOLD_RUN) {
            for (const e of run) groups.push({kind: 'single', id: e.id, event: e});
        } else {
            groups.push({kind: 'folded', id: run[0].id, events: run});
        }
        run = [];
    };
    for (const e of events) {
        if (isFoldable(e) && !pinned.has(e.id)) {
            run.push(e);
        } else {
            flush();
            groups.push({kind: 'single', id: e.id, event: e});
        }
    }
    flush();
    return groups;
}

/**
 * Category-count summary for a folded chip: "24× searches · 20× citations ·
 * 6× thoughts". Counting by category rather than by type is deliberate — a
 * total plus per-category counts double-counts, because commands *are* tool
 * calls. Thoughts get their own bucket.
 *
 * Category resolution is left to the caller (it needs i18n); this returns raw
 * category keys with `'thought'` for reasoning and `'other'` for tool calls
 * whose category is absent (older history rows).
 */
export interface FoldedSummaryPart {
    /** Tool category key, or the literal 'thought' / 'other'. */
    category: string;
    count: number;
}

export interface FoldedSummary {
    parts: FoldedSummaryPart[];
    /** Calls that errored or were denied — surfaced as a chip badge. */
    failed: number;
}

export function summarizeFolded(events: FoldableEvent[]): FoldedSummary {
    const counts = new Map<string, number>();
    let failed = 0;
    for (const e of events) {
        let key: string;
        if (e.kind === 'thought') {
            key = 'thought';
        } else {
            key = e.category || 'other';
            if (e.status === 'error' || e.status === 'denied' || e.resultStatus === 'error') failed++;
        }
        counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    // Highest count first; ties keep insertion (= first-appearance) order, which
    // Map iteration gives us for free.
    const parts = [...counts.entries()]
        .map(([category, count]) => ({category, count}))
        .sort((a, b) => b.count - a.count);
    return {parts, failed};
}
