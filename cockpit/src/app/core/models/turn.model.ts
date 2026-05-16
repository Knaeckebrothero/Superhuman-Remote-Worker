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

export type TurnEvent = ThoughtEvent | TextEvent | ToolCallEvent;

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

export type Turn = AssistantTurn | UserTurn | SystemTurn;

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
        else tools++;
    }
    return {thoughts, texts, tools};
}
