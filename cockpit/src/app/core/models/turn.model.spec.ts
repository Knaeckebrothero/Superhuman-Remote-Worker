import {describe, expect, it} from 'vitest';
import {
    AssistantTurn,
    firstSentence,
    firstTextOf,
    groupEvents,
    lastTextOf,
    TextEvent,
    ThoughtEvent,
    ToolCallEvent,
} from './turn.model';

function mkTurn(events: AssistantTurn['events']): AssistantTurn {
    return {kind: 'assistant', id: 't1', events, status: 'done', startedAt: 0};
}
const txt = (id: string, content: string): TextEvent => ({kind: 'text', id, content, status: 'done', startedAt: 0});
const tht = (id: string): ThoughtEvent => ({kind: 'thought', id, content: '...', status: 'done', startedAt: 0});
const tool = (id: string): ToolCallEvent =>
    ({kind: 'tool_call', id, tool: 'read_file', args: {}, status: 'completed', startedAt: 0});

describe('firstTextOf', () => {
    it('returns the first text event, skipping leading thoughts', () => {
        const t = mkTurn([tht('b0'), txt('b1', 'first'), txt('b2', 'second')]);
        expect(firstTextOf(t)?.content).toBe('first');
    });

    it('returns undefined when the turn has no text', () => {
        expect(firstTextOf(mkTurn([tht('b0')]))).toBeUndefined();
    });

    it('is the opposite end from lastTextOf', () => {
        const t = mkTurn([txt('b0', 'first'), txt('b1', 'last')]);
        expect(firstTextOf(t)?.content).toBe('first');
        expect(lastTextOf(t)?.content).toBe('last');
    });
});

describe('firstSentence', () => {
    it('returns the first period-terminated sentence', () => {
        expect(firstSentence('I will read the file. Then edit it.')).toBe('I will read the file.');
    });

    it('strips a leading markdown heading marker', () => {
        expect(firstSentence('## Plan\nDo the thing.')).toBe('Plan Do the thing.');
    });

    it('strips an opening and closing code fence', () => {
        expect(firstSentence('```python\ndef greet():\n    pass\n```')).toBe('def greet(): pass');
    });

    it('strips the fence even when no closing fence is present', () => {
        expect(firstSentence('```ts\nconst x = 1')).toBe('const x = 1');
    });

    it('collapses internal whitespace and newlines', () => {
        expect(firstSentence('Looking   into\nthis now.')).toBe('Looking into this now.');
    });

    it('caps an over-long sentence with an ellipsis', () => {
        const out = firstSentence('x'.repeat(200), 140);
        expect(out).toHaveLength(140);
        expect(out.endsWith('…')).toBe(true);
    });

    it('returns empty string for blank or empty input', () => {
        expect(firstSentence('   ')).toBe('');
        expect(firstSentence('')).toBe('');
    });
});

describe('groupEvents', () => {
    it('returns [] for no events', () => {
        expect(groupEvents([])).toEqual([]);
    });

    it('coalesces a run of consecutive tool calls into one tools group', () => {
        const g = groupEvents([tool('b0'), tool('b1'), tool('b2')]);
        expect(g).toHaveLength(1);
        expect(g[0]).toMatchObject({kind: 'tools', id: 'b0'});
        expect((g[0] as {tools: unknown[]}).tools).toHaveLength(3);
    });

    it('keeps a lone tool call as a one-member tools group', () => {
        const g = groupEvents([tool('b0')]);
        expect(g).toHaveLength(1);
        expect(g[0].kind).toBe('tools');
        expect((g[0] as {tools: unknown[]}).tools).toHaveLength(1);
    });

    it('emits thoughts and text as single groups', () => {
        const g = groupEvents([tht('b0'), txt('b1', 'hi')]);
        expect(g.map(x => x.kind)).toEqual(['single', 'single']);
        expect(g[0]).toMatchObject({kind: 'single', id: 'b0'});
    });

    it('does NOT merge tool runs across an interleaved thought', () => {
        const g = groupEvents([tool('b0'), tool('b1'), tht('b2'), tool('b3'), tool('b4')]);
        expect(g.map(x => x.kind)).toEqual(['tools', 'single', 'tools']);
        expect((g[0] as {tools: unknown[]}).tools).toHaveLength(2);
        expect(g[1]).toMatchObject({kind: 'single', id: 'b2'});
        expect((g[2] as {tools: unknown[]}).tools).toHaveLength(2);
        expect(g[2].id).toBe('b3');
    });

    it('breaks runs on text too', () => {
        const g = groupEvents([txt('b0', 'plan'), tool('b1'), txt('b2', 'done')]);
        expect(g.map(x => x.kind)).toEqual(['single', 'tools', 'single']);
    });
});
