import {
    AssistantTurn,
    ConversationState,
    EMPTY_CONVERSATION,
    TextEvent,
    ThoughtEvent,
    ToolCallEvent,
    Turn,
    TurnEvent,
} from '../models/turn.model';
import {ChatAttachment} from './persistent-chat.service';

/**
 * Pure reducer for ConversationState.
 *
 * One assistant "turn" in the UI is one bubble; internally it's an ordered
 * list of typed events (thoughts, plain-text blocks, tool calls). The
 * reducer maps SSE wire events to event-list mutations.
 *
 * Idempotency: every mutation is keyed by stable ids (turnId,
 * `${turnId}.b<index>`, or backend tool_use_id), so replaying the same
 * event sequence converges to the same final state. SSE replay via
 * `Last-Event-ID` cursor is therefore safe.
 *
 * ThoughtEvent boundary rule: each `thinking` action is a delta. If the
 * most recent event in the active turn is an open ThoughtEvent, the delta
 * appends. Otherwise (the previous event was non-thinking, or there's no
 * prior thought yet), a new ThoughtEvent opens. This produces correct
 * boundaries for both interleaved-thinking models (think → tool → think →
 * tool) and traditional-thinking-per-API-turn models, without requiring
 * server-side markers.
 */

export type ReducerAction =
    | { type: 'reset'; threadId?: string | null }
    | { type: 'load_history'; threadId: string; turns: Turn[] }
    | {
        type: 'user_message';
        id: string;
        content: string;
        attachments?: ChatAttachment[];
        timestamp: number;
    }
    | {
        type: 'system_message';
        id: string;
        content: string;
        timestamp: number;
    }
    | { type: 'turn_started'; turnId: string; startedAt: number; model?: string }
    | { type: 'turn_completed'; turnId: string; finishedAt: number }
    | { type: 'turn_interrupted'; turnId: string; finishedAt: number }
    | { type: 'token'; content: string; timestamp: number }
    | { type: 'thinking'; content: string; timestamp: number }
    | {
        type: 'tool_started';
        toolUseId: string;
        tool: string;
        args: Record<string, unknown>;
        category?: string;
        timestamp: number;
    }
    | {
        type: 'tool_completed';
        toolUseId: string;
        result?: string;
        isError?: boolean;
        timestamp: number;
    }
    | {
        type: 'permission_request';
        toolUseId: string;
        tool: string;
        args: Record<string, unknown>;
        timestamp: number;
    }
    | {
        type: 'permission_decision';
        toolUseId: string;
        decision: 'approved' | 'denied';
        timestamp: number;
    }
    | { type: 'remove_turn'; id: string }
    | { type: 'add_compaction'; id: string; summary: string; timestamp: number };

export function reduce(state: ConversationState, action: ReducerAction): ConversationState {
    switch (action.type) {
        case 'reset':
            return {...EMPTY_CONVERSATION, threadId: action.threadId ?? null};

        case 'load_history':
            return {
                threadId: action.threadId,
                turns: action.turns,
                activeAssistantTurnId: null,
            };

        case 'user_message':
            return {
                ...state,
                turns: [
                    ...state.turns,
                    {
                        kind: 'user',
                        id: action.id,
                        content: action.content,
                        attachments: action.attachments,
                        timestamp: action.timestamp,
                    },
                ],
            };

        case 'remove_turn':
            // Roll back an optimistic turn (e.g. a user message whose POST
            // hard-failed) by id. Safe: every Turn carries a unique id and
            // activeAssistantTurnId is untouched (the removed turn is a user
            // turn, never the streaming assistant turn).
            return {
                ...state,
                turns: state.turns.filter((t) => t.id !== action.id),
            };

        case 'add_compaction': {
            // A compaction boundary banner. Idempotent by id (stable
            // `compaction-<turn>`), so SSE replay replaces rather than
            // duplicates. activeAssistantTurnId is untouched.
            const turn: Turn = {
                kind: 'compaction',
                id: action.id,
                summary: action.summary,
                timestamp: action.timestamp,
            };
            const idx = state.turns.findIndex((t) => t.id === action.id);
            if (idx >= 0) {
                const turns = [...state.turns];
                turns[idx] = turn;
                return {...state, turns};
            }
            return {...state, turns: [...state.turns, turn]};
        }

        case 'system_message':
            return {
                ...state,
                turns: [
                    ...state.turns,
                    {
                        kind: 'system',
                        id: action.id,
                        content: action.content,
                        timestamp: action.timestamp,
                    },
                ],
            };

        case 'turn_started': {
            // Defensive: if a prior turn is still marked active (e.g. its
            // `turn.completed` event was lost), close it before opening the
            // new one.
            let turns = state.turns;
            if (state.activeAssistantTurnId && state.activeAssistantTurnId !== action.turnId) {
                turns = turns.map((t) => {
                    if (t.kind !== 'assistant' || t.id !== state.activeAssistantTurnId) return t;
                    if (t.status !== 'streaming') return t;
                    return {
                        ...t,
                        events: closeOpenEvents(t.events, action.startedAt),
                        status: 'done' as const,
                        finishedAt: action.startedAt,
                    };
                });
            }

            // Idempotent: replayed turn_started just re-activates the existing turn.
            const existing = turns.find(
                (t): t is AssistantTurn => t.kind === 'assistant' && t.id === action.turnId,
            );
            if (existing) {
                return {...state, turns, activeAssistantTurnId: action.turnId};
            }

            const newTurn: AssistantTurn = {
                kind: 'assistant',
                id: action.turnId,
                events: [],
                status: 'streaming',
                model: action.model,
                startedAt: action.startedAt,
            };
            return {
                ...state,
                turns: [...turns, newTurn],
                activeAssistantTurnId: action.turnId,
            };
        }

        case 'turn_completed':
        case 'turn_interrupted': {
            const finalStatus = action.type === 'turn_interrupted' ? 'interrupted' : 'done';
            const directMatch = state.turns.some(
                (t) => t.kind === 'assistant' && t.id === action.turnId,
            );
            // Defense-in-depth: if no turn matches the real turnId but the
            // active turn is a placeholder synthesised by appendDelta /
            // updateActiveTurn (recovered === true), promote it to the real
            // id before closing — otherwise the streaming bubble would
            // hang forever (see
            // docs/issues/persistent_chat_lost_assistant_turn_on_mid_turn_reload.md
            // §Approach 2).
            const activeId = state.activeAssistantTurnId;
            const activeTurn =
                activeId
                    ? state.turns.find(
                          (t): t is AssistantTurn =>
                              t.kind === 'assistant' && t.id === activeId,
                      )
                    : null;
            const shouldPromote =
                !directMatch && activeTurn != null && activeTurn.recovered === true;
            return {
                ...state,
                turns: state.turns.map((t) => {
                    if (t.kind !== 'assistant') return t;
                    if (t.id === action.turnId) {
                        return {
                            ...t,
                            events: closeOpenEvents(t.events, action.finishedAt),
                            status: finalStatus,
                            finishedAt: action.finishedAt,
                        };
                    }
                    if (shouldPromote && t.id === activeId) {
                        return {
                            ...t,
                            id: action.turnId,
                            recovered: undefined,
                            events: closeOpenEvents(t.events, action.finishedAt),
                            status: finalStatus,
                            finishedAt: action.finishedAt,
                        };
                    }
                    return t;
                }),
                activeAssistantTurnId:
                    state.activeAssistantTurnId === action.turnId || shouldPromote
                        ? null
                        : state.activeAssistantTurnId,
            };
        }

        case 'token':
            return appendDelta(state, 'text', action.content, action.timestamp);

        case 'thinking':
            return appendDelta(state, 'thought', action.content, action.timestamp);

        case 'tool_started':
            return updateActiveTurn(ensurePlaceholderTurn(state, action.timestamp), (turn) => {
                const closed = closeOpenEvents(turn.events, action.timestamp);
                const idx = closed.findIndex(
                    (e) => e.kind === 'tool_call' && e.id === action.toolUseId,
                );
                if (idx >= 0) {
                    // Existing entry (e.g. created by a prior permission.request) — promote.
                    const existing = closed[idx] as ToolCallEvent;
                    const updated: ToolCallEvent = {
                        ...existing,
                        tool: action.tool,
                        args: action.args,
                        category: action.category ?? existing.category,
                        status: 'running',
                        startedAt: action.timestamp,
                    };
                    return {...turn, events: replaceAt(closed, idx, updated)};
                }
                const newCall: ToolCallEvent = {
                    kind: 'tool_call',
                    id: action.toolUseId,
                    tool: action.tool,
                    args: action.args,
                    category: action.category,
                    status: 'running',
                    startedAt: action.timestamp,
                };
                return {...turn, events: [...closed, newCall]};
            });

        case 'tool_completed':
            return updateActiveTurn(ensurePlaceholderTurn(state, action.timestamp), (turn) => {
                const idx = turn.events.findIndex(
                    (e) => e.kind === 'tool_call' && e.id === action.toolUseId,
                );
                if (idx < 0) {
                    // Orphan tool_completed (replay quirk) — fabricate a finished entry.
                    const synthetic: ToolCallEvent = {
                        kind: 'tool_call',
                        id: action.toolUseId,
                        tool: '<unknown>',
                        args: {},
                        status: action.isError ? 'error' : 'completed',
                        result: action.result,
                        resultStatus: action.isError ? 'error' : 'ok',
                        startedAt: action.timestamp,
                        durationMs: 0,
                    };
                    return {...turn, events: [...turn.events, synthetic]};
                }
                const existing = turn.events[idx] as ToolCallEvent;
                const updated: ToolCallEvent = {
                    ...existing,
                    status: action.isError ? 'error' : 'completed',
                    result: action.result,
                    resultStatus: action.isError ? 'error' : 'ok',
                    durationMs: Math.max(0, action.timestamp - existing.startedAt),
                };
                return {...turn, events: replaceAt(turn.events, idx, updated)};
            });

        case 'permission_request':
            return updateActiveTurn(ensurePlaceholderTurn(state, action.timestamp), (turn) => {
                const idx = turn.events.findIndex(
                    (e) => e.kind === 'tool_call' && e.id === action.toolUseId,
                );
                if (idx >= 0) {
                    const existing = turn.events[idx] as ToolCallEvent;
                    const updated: ToolCallEvent = {...existing, status: 'pending'};
                    return {...turn, events: replaceAt(turn.events, idx, updated)};
                }
                const newCall: ToolCallEvent = {
                    kind: 'tool_call',
                    id: action.toolUseId,
                    tool: action.tool,
                    args: action.args,
                    status: 'pending',
                    startedAt: action.timestamp,
                };
                return {...turn, events: [...turn.events, newCall]};
            });

        case 'permission_decision':
            return updateActiveTurn(ensurePlaceholderTurn(state, action.timestamp), (turn) => {
                const idx = turn.events.findIndex(
                    (e) => e.kind === 'tool_call' && e.id === action.toolUseId,
                );
                if (idx < 0) {
                    if (action.decision === 'denied') {
                        // Deny-before-start: synthetic denied entry so the user sees the marker.
                        const synthetic: ToolCallEvent = {
                            kind: 'tool_call',
                            id: action.toolUseId,
                            tool: '<unknown>',
                            args: {},
                            status: 'denied',
                            decision: 'denied',
                            startedAt: action.timestamp,
                            durationMs: 0,
                        };
                        return {...turn, events: [...turn.events, synthetic]};
                    }
                    return turn;
                }
                const existing = turn.events[idx] as ToolCallEvent;
                const newStatus =
                    action.decision === 'denied' && existing.status === 'pending'
                        ? ('denied' as const)
                        : existing.status;
                const updated: ToolCallEvent = {
                    ...existing,
                    decision: action.decision,
                    status: newStatus,
                };
                return {...turn, events: replaceAt(turn.events, idx, updated)};
            });

        default: {
            // Exhaustiveness check.
            const _exhaustive: never = action;
            void _exhaustive;
            return state;
        }
    }
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function closeOpenEvents(events: TurnEvent[], timestamp: number): TurnEvent[] {
    return events.map((e) => {
        if (e.kind === 'thought' && e.status === 'streaming') {
            const closed: ThoughtEvent = {
                ...e,
                status: 'done',
                durationMs: Math.max(0, timestamp - e.startedAt),
            };
            return closed;
        }
        if (e.kind === 'text' && e.status === 'streaming') {
            const closed: TextEvent = {...e, status: 'done'};
            return closed;
        }
        return e;
    });
}

/**
 * Open a synthetic placeholder assistant turn when streaming events arrive
 * without an `activeAssistantTurnId` — typically because `connect()` reset
 * state on a mid-turn reconnect and the SSE replay cursor is past the
 * `turn.started` event (so the reducer never saw it).
 *
 * The placeholder absorbs subsequent token / thinking / tool events so the
 * UI surfaces partial state instead of silently losing the turn. When the
 * real `turn.completed` (or `turn.interrupted`) finally arrives, the
 * placeholder is promoted to the real turn id and closed — see the
 * `turn_completed` case above. See
 * docs/issues/persistent_chat_lost_assistant_turn_on_mid_turn_reload.md
 * §Approach 2.
 */
function ensurePlaceholderTurn(
    state: ConversationState,
    timestamp: number,
): ConversationState {
    if (state.activeAssistantTurnId) return state;
    const placeholderId = `recovered:${timestamp}`;
    const placeholder: AssistantTurn = {
        kind: 'assistant',
        id: placeholderId,
        events: [],
        status: 'streaming',
        startedAt: timestamp,
        recovered: true,
    };
    return {
        ...state,
        turns: [...state.turns, placeholder],
        activeAssistantTurnId: placeholderId,
    };
}

function appendDelta(
    state: ConversationState,
    eventKind: 'text' | 'thought',
    content: string,
    timestamp: number,
): ConversationState {
    const seeded = ensurePlaceholderTurn(state, timestamp);
    return updateActiveTurn(seeded, (turn) => {
        const last = turn.events[turn.events.length - 1];
        if (last && last.kind === eventKind) {
            const lastTyped = last as TextEvent | ThoughtEvent;
            if (lastTyped.status === 'streaming') {
                const merged: TurnEvent =
                    eventKind === 'text'
                        ? {...(last as TextEvent), content: (last as TextEvent).content + content}
                        : {...(last as ThoughtEvent), content: (last as ThoughtEvent).content + content};
                return {...turn, events: replaceAt(turn.events, turn.events.length - 1, merged)};
            }
        }
        const closed = closeOpenEvents(turn.events, timestamp);
        const blockIndex = closed.filter((e) => e.kind === 'thought' || e.kind === 'text').length;
        const id = `${turn.id}.b${blockIndex}`;
        const newEvent: TurnEvent =
            eventKind === 'text'
                ? {kind: 'text', id, content, status: 'streaming', startedAt: timestamp}
                : {kind: 'thought', id, content, status: 'streaming', startedAt: timestamp};
        return {...turn, events: [...closed, newEvent]};
    });
}

function updateActiveTurn(
    state: ConversationState,
    updater: (t: AssistantTurn) => AssistantTurn,
): ConversationState {
    if (!state.activeAssistantTurnId) return state;
    return {
        ...state,
        turns: state.turns.map((t) => {
            if (t.kind !== 'assistant' || t.id !== state.activeAssistantTurnId) return t;
            return updater(t);
        }),
    };
}

function replaceAt<T>(arr: T[], idx: number, value: T): T[] {
    const next = arr.slice();
    next[idx] = value;
    return next;
}
