import {describe, expect, it} from 'vitest';
import {AssistantTurn, firstSentence, firstTextOf, lastTextOf, TextEvent, ThoughtEvent} from './turn.model';

function mkTurn(events: AssistantTurn['events']): AssistantTurn {
    return {kind: 'assistant', id: 't1', events, status: 'done', startedAt: 0};
}
const txt = (id: string, content: string): TextEvent => ({kind: 'text', id, content, status: 'done', startedAt: 0});
const tht = (id: string): ThoughtEvent => ({kind: 'thought', id, content: '...', status: 'done', startedAt: 0});

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
