import {describe, expect, it} from 'vitest';
import {
    AssistantTurn,
    ConversationState,
    EMPTY_CONVERSATION,
    isAssistantTurn,
    isToolCall,
    TextEvent,
    ThoughtEvent,
    ToolCallEvent,
} from '../models/turn.model';
import {reduce, ReducerAction} from './turn-reducer';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function play(actions: ReducerAction[], initial: ConversationState = EMPTY_CONVERSATION): ConversationState {
    return actions.reduce((s, a) => reduce(s, a), initial);
}

function activeTurn(state: ConversationState): AssistantTurn {
    const t = state.turns.find(isAssistantTurn);
    if (!t) throw new Error('no assistant turn');
    return t;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('turn-reducer — reset / load_history', () => {
    it('reset clears state with optional threadId', () => {
        const state = play([{type: 'reset', threadId: 'abc'}]);
        expect(state).toEqual({threadId: 'abc', turns: [], activeAssistantTurnId: null});
    });

    it('load_history replaces turns and clears active turn', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {
                type: 'load_history',
                threadId: 'thr',
                turns: [{kind: 'user', id: 'u1', content: 'hi', timestamp: 500}],
            },
        ]);
        expect(state.turns).toHaveLength(1);
        expect(state.activeAssistantTurnId).toBeNull();
    });
});

describe('turn-reducer — user_message / system_message', () => {
    it('appends a UserTurn', () => {
        const state = play([
            {type: 'user_message', id: 'u1', content: 'hello', timestamp: 100},
        ]);
        expect(state.turns).toEqual([
            {kind: 'user', id: 'u1', content: 'hello', timestamp: 100, attachments: undefined},
        ]);
    });

    it('appends a SystemTurn', () => {
        const state = play([
            {type: 'system_message', id: 's1', content: 'Session ended.', timestamp: 200},
        ]);
        expect(state.turns).toEqual([
            {kind: 'system', id: 's1', content: 'Session ended.', timestamp: 200},
        ]);
    });
});

describe('turn-reducer — remove_turn', () => {
    it('removes the turn with the matching id and leaves the others', () => {
        const state = play([
            {type: 'user_message', id: 'u1', content: 'first', timestamp: 100},
            {type: 'user_message', id: 'u2', content: 'second', timestamp: 200},
            {type: 'remove_turn', id: 'u1'},
        ]);
        expect(state.turns).toHaveLength(1);
        expect(state.turns[0].id).toBe('u2');
    });

    it('is a no-op when no turn matches the id', () => {
        const state = play([
            {type: 'user_message', id: 'u1', content: 'hi', timestamp: 100},
            {type: 'remove_turn', id: 'nope'},
        ]);
        expect(state.turns).toHaveLength(1);
        expect(state.turns[0].id).toBe('u1');
    });

    it('does not disturb the active assistant turn', () => {
        const state = play([
            {type: 'user_message', id: 'u1', content: 'hi', timestamp: 100},
            {type: 'turn_started', turnId: 't1', startedAt: 200},
            {type: 'remove_turn', id: 'u1'},
        ]);
        expect(state.turns.find((t) => t.id === 'u1')).toBeUndefined();
        expect(state.activeAssistantTurnId).toBe('t1');
    });
});

describe('turn-reducer — turn lifecycle', () => {
    it('turn_started opens an empty assistant turn and marks it active', () => {
        const state = play([{type: 'turn_started', turnId: 't1', startedAt: 1000, model: 'opus'}]);
        const turn = activeTurn(state);
        expect(turn.status).toBe('streaming');
        expect(turn.events).toEqual([]);
        expect(turn.model).toBe('opus');
        expect(state.activeAssistantTurnId).toBe('t1');
    });

    it('turn_started is idempotent — replayed event keeps the same turn', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {type: 'token', content: 'hi', timestamp: 1100},
            {type: 'turn_started', turnId: 't1', startedAt: 1000}, // replay
        ]);
        expect(state.turns.filter(isAssistantTurn)).toHaveLength(1);
        const turn = activeTurn(state);
        expect((turn.events[0] as TextEvent).content).toBe('hi');
    });

    it('turn_completed flips status to done and clears activeAssistantTurnId', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {type: 'turn_completed', turnId: 't1', finishedAt: 2000},
        ]);
        const turn = activeTurn(state);
        expect(turn.status).toBe('done');
        expect(turn.finishedAt).toBe(2000);
        expect(state.activeAssistantTurnId).toBeNull();
    });

    it('turn_interrupted flips status to interrupted', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {type: 'turn_interrupted', turnId: 't1', finishedAt: 1500},
        ]);
        const turn = activeTurn(state);
        expect(turn.status).toBe('interrupted');
    });

    it('turn_completed mid-stream closes open text/thinking events', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {type: 'thinking', content: 'pondering', timestamp: 1100},
            {type: 'turn_completed', turnId: 't1', finishedAt: 2000},
        ]);
        const turn = activeTurn(state);
        expect(turn.events).toHaveLength(1);
        expect((turn.events[0] as ThoughtEvent).status).toBe('done');
        expect((turn.events[0] as ThoughtEvent).durationMs).toBe(900);
    });

    it('a new turn_started while a prior turn is still streaming closes the prior turn', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {type: 'token', content: 'partial', timestamp: 1100},
            {type: 'turn_started', turnId: 't2', startedAt: 2000},
        ]);
        const t1 = state.turns.find((t) => isAssistantTurn(t) && t.id === 't1') as AssistantTurn;
        expect(t1.status).toBe('done');
        expect(t1.finishedAt).toBe(2000);
        expect(state.activeAssistantTurnId).toBe('t2');
    });
});

describe('turn-reducer — text/thinking deltas', () => {
    it('consecutive tokens append to the same TextEvent', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {type: 'token', content: 'Hello ', timestamp: 1100},
            {type: 'token', content: 'world', timestamp: 1200},
        ]);
        const turn = activeTurn(state);
        expect(turn.events).toHaveLength(1);
        expect((turn.events[0] as TextEvent).content).toBe('Hello world');
        expect((turn.events[0] as TextEvent).status).toBe('streaming');
    });

    it('consecutive thinking deltas append to the same ThoughtEvent', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {type: 'thinking', content: 'I should ', timestamp: 1100},
            {type: 'thinking', content: 'plan first', timestamp: 1200},
        ]);
        const turn = activeTurn(state);
        expect(turn.events).toHaveLength(1);
        expect((turn.events[0] as ThoughtEvent).content).toBe('I should plan first');
    });

    it('token after thinking opens a new TextEvent and closes the ThoughtEvent', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {type: 'thinking', content: 'planning', timestamp: 1100},
            {type: 'token', content: 'Sure!', timestamp: 1200},
        ]);
        const turn = activeTurn(state);
        expect(turn.events).toHaveLength(2);
        expect(turn.events[0].kind).toBe('thought');
        expect((turn.events[0] as ThoughtEvent).status).toBe('done');
        expect(turn.events[1].kind).toBe('text');
    });

    it('thinking after a tool_call opens a new ThoughtEvent (interleaved-thinking pattern)', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {type: 'thinking', content: 'plan', timestamp: 1100},
            {
                type: 'tool_started',
                toolUseId: 'tc1',
                tool: 'read_file',
                args: {},
                timestamp: 1200,
            },
            {type: 'tool_completed', toolUseId: 'tc1', result: 'ok', timestamp: 1300},
            {type: 'thinking', content: 'reflect', timestamp: 1400},
        ]);
        const turn = activeTurn(state);
        const thoughts = turn.events.filter((e) => e.kind === 'thought') as ThoughtEvent[];
        expect(thoughts).toHaveLength(2);
        expect(thoughts[0].content).toBe('plan');
        expect(thoughts[1].content).toBe('reflect');
    });

    it('deltas without an active turn open a recovered placeholder turn (Approach 2)', () => {
        // Defense-in-depth from
        // docs/issues/persistent_chat_lost_assistant_turn_on_mid_turn_reload.md:
        // when streaming events arrive with activeAssistantTurnId === null
        // (e.g. SSE replay cursor is past turn.started after a mid-turn
        // reconnect), the reducer now synthesises a placeholder turn so the
        // partial state is visible instead of silently lost.
        const state = play([{type: 'token', content: 'orphan', timestamp: 1000}]);
        expect(state.turns).toHaveLength(1);
        const turn = state.turns[0];
        expect(turn.kind).toBe('assistant');
        if (turn.kind !== 'assistant') throw new Error('expected assistant turn');
        expect(turn.recovered).toBe(true);
        expect(turn.id).toBe('recovered:1000');
        expect(turn.status).toBe('streaming');
        expect(turn.events).toHaveLength(1);
        expect(turn.events[0].kind).toBe('text');
        expect(state.activeAssistantTurnId).toBe('recovered:1000');
    });

    it('turn_completed promotes a recovered placeholder to the real turn id', () => {
        // When the real turn.completed event finally arrives after a
        // placeholder was synthesised by orphan deltas, the placeholder is
        // renamed to the real turn id and closed — so the bubble doesn't
        // hang in "streaming" forever.
        const state = play([
            {type: 'token', content: 'orphan content', timestamp: 1000},
            {type: 'turn_completed', turnId: 'real-turn-id', finishedAt: 2000},
        ]);
        expect(state.turns).toHaveLength(1);
        const turn = state.turns[0];
        if (turn.kind !== 'assistant') throw new Error('expected assistant turn');
        expect(turn.id).toBe('real-turn-id');
        expect(turn.status).toBe('done');
        expect(turn.finishedAt).toBe(2000);
        expect(state.activeAssistantTurnId).toBeNull();
    });

    it('block ids are stable: ${turnId}.b<index>', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {type: 'thinking', content: 'a', timestamp: 1100},
            {type: 'token', content: 'b', timestamp: 1200},
            {type: 'thinking', content: 'c', timestamp: 1300},
        ]);
        const turn = activeTurn(state);
        expect(turn.events.map((e) => e.id)).toEqual(['t1.b0', 't1.b1', 't1.b2']);
    });
});

describe('turn-reducer — tool calls', () => {
    it('tool_started pushes a ToolCallEvent in running state', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {
                type: 'tool_started',
                toolUseId: 'tc1',
                tool: 'read_file',
                args: {path: 'x'},
                category: 'workspace',
                timestamp: 1100,
            },
        ]);
        const turn = activeTurn(state);
        const tc = turn.events[0] as ToolCallEvent;
        expect(tc.kind).toBe('tool_call');
        expect(tc.tool).toBe('read_file');
        expect(tc.status).toBe('running');
        expect(tc.category).toBe('workspace');
    });

    it('tool_completed updates by id, sets durationMs', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {
                type: 'tool_started',
                toolUseId: 'tc1',
                tool: 'read_file',
                args: {},
                timestamp: 1100,
            },
            {type: 'tool_completed', toolUseId: 'tc1', result: 'data', timestamp: 1500},
        ]);
        const turn = activeTurn(state);
        const tc = turn.events[0] as ToolCallEvent;
        expect(tc.status).toBe('completed');
        expect(tc.result).toBe('data');
        expect(tc.durationMs).toBe(400);
        expect(tc.resultStatus).toBe('ok');
    });

    it('tool_completed with isError flips to error status', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {
                type: 'tool_started',
                toolUseId: 'tc1',
                tool: 'run_command',
                args: {},
                timestamp: 1100,
            },
            {
                type: 'tool_completed',
                toolUseId: 'tc1',
                result: 'boom',
                isError: true,
                timestamp: 1200,
            },
        ]);
        const tc = activeTurn(state).events[0] as ToolCallEvent;
        expect(tc.status).toBe('error');
        expect(tc.resultStatus).toBe('error');
    });

    it('orphan tool_completed (no prior tool_started) creates a synthetic completed entry', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {type: 'tool_completed', toolUseId: 'tc-orphan', result: 'r', timestamp: 1100},
        ]);
        const turn = activeTurn(state);
        const tc = turn.events.find(isToolCall);
        expect(tc).toBeDefined();
        expect(tc!.status).toBe('completed');
        expect(tc!.id).toBe('tc-orphan');
    });

    it('tool_started closes any open thinking event before pushing the tool call', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {type: 'thinking', content: 'let me check', timestamp: 1100},
            {
                type: 'tool_started',
                toolUseId: 'tc1',
                tool: 'read_file',
                args: {},
                timestamp: 1200,
            },
        ]);
        const turn = activeTurn(state);
        expect(turn.events).toHaveLength(2);
        expect((turn.events[0] as ThoughtEvent).status).toBe('done');
        expect(turn.events[1].kind).toBe('tool_call');
    });
});

describe('turn-reducer — permissions', () => {
    it('permission_request before tool_started creates a pending ToolCallEvent', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {
                type: 'permission_request',
                toolUseId: 'tc1',
                tool: 'run_command',
                args: {cmd: 'rm'},
                timestamp: 1100,
            },
        ]);
        const tc = activeTurn(state).events[0] as ToolCallEvent;
        expect(tc.status).toBe('pending');
        expect(tc.tool).toBe('run_command');
    });

    it('permission_request followed by tool_started promotes the entry to running', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {
                type: 'permission_request',
                toolUseId: 'tc1',
                tool: 'run_command',
                args: {cmd: 'ls'},
                timestamp: 1100,
            },
            {
                type: 'tool_started',
                toolUseId: 'tc1',
                tool: 'run_command',
                args: {cmd: 'ls'},
                timestamp: 1200,
            },
        ]);
        const turn = activeTurn(state);
        expect(turn.events).toHaveLength(1);
        expect((turn.events[0] as ToolCallEvent).status).toBe('running');
    });

    it('permission_decision (denied) on a pending call flips status to denied', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {
                type: 'permission_request',
                toolUseId: 'tc1',
                tool: 'x',
                args: {},
                timestamp: 1100,
            },
            {
                type: 'permission_decision',
                toolUseId: 'tc1',
                decision: 'denied',
                timestamp: 1200,
            },
        ]);
        const tc = activeTurn(state).events[0] as ToolCallEvent;
        expect(tc.status).toBe('denied');
        expect(tc.decision).toBe('denied');
    });

    it('permission_decision without a prior request creates a synthetic denied entry', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {
                type: 'permission_decision',
                toolUseId: 'tc-ghost',
                decision: 'denied',
                timestamp: 1100,
            },
        ]);
        const tc = activeTurn(state).events[0] as ToolCallEvent;
        expect(tc).toBeDefined();
        expect(tc.status).toBe('denied');
        expect(tc.id).toBe('tc-ghost');
    });

    it('permission_decision (approved) records the decision but does not change a running call', () => {
        const state = play([
            {type: 'turn_started', turnId: 't1', startedAt: 1000},
            {
                type: 'tool_started',
                toolUseId: 'tc1',
                tool: 'x',
                args: {},
                timestamp: 1100,
            },
            {
                type: 'permission_decision',
                toolUseId: 'tc1',
                decision: 'approved',
                timestamp: 1150,
            },
        ]);
        const tc = activeTurn(state).events[0] as ToolCallEvent;
        expect(tc.status).toBe('running');
        expect(tc.decision).toBe('approved');
    });
});

describe('turn-reducer — replay idempotency', () => {
    const sequence: ReducerAction[] = [
        {type: 'reset', threadId: 'thr'},
        {type: 'user_message', id: 'u1', content: 'go', timestamp: 100},
        {type: 'turn_started', turnId: 't1', startedAt: 200},
        {type: 'thinking', content: 'planning', timestamp: 250},
        {type: 'token', content: 'Sure ', timestamp: 300},
        {type: 'token', content: 'thing.', timestamp: 350},
        {
            type: 'tool_started',
            toolUseId: 'tc1',
            tool: 'read_file',
            args: {p: 'a'},
            timestamp: 400,
        },
        {type: 'tool_completed', toolUseId: 'tc1', result: 'r', timestamp: 500},
        {type: 'thinking', content: 'reflect', timestamp: 550},
        {type: 'turn_completed', turnId: 't1', finishedAt: 600},
    ];

    it('replaying the full sequence twice yields the same final state', () => {
        const once = play(sequence);
        const twice = play([...sequence, ...sequence]);
        expect(twice).toEqual(once);
    });

    it('produces the expected event structure', () => {
        const state = play(sequence);
        const turn = state.turns.find(isAssistantTurn)!;
        const kinds = turn.events.map((e) => e.kind);
        // thought, text, tool_call, thought — boundary detection at work
        expect(kinds).toEqual(['thought', 'text', 'tool_call', 'thought']);
        expect(turn.status).toBe('done');
    });
});
