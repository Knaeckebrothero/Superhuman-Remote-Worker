import {describe, expect, it} from 'vitest';
import {groupDriftForDisplay} from './config-drift-dialog.component';

describe('groupDriftForDisplay', () => {
    it('collapses identical labels into one row with a count', () => {
        const rows = groupDriftForDisplay([
            {id: 'connector:a', kind: 'connector', reason: 'revoked',
             label: 'a connector you no longer have access to'},
            {id: 'connector:b', kind: 'connector', reason: 'revoked',
             label: 'a connector you no longer have access to'},
        ]);

        expect(rows).toEqual([{
            kind: 'connector', reason: 'revoked',
            label: 'a connector you no longer have access to', count: 2,
        }]);
    });

    it('keeps distinct labels separate and preserves order', () => {
        const rows = groupDriftForDisplay([
            {id: 'connector:a', kind: 'connector', reason: 'deleted',
             label: 'KurortEngine'},
            {id: 'grant:shell_tools', kind: 'grant', reason: 'revoked',
             label: 'shell tools'},
        ]);

        expect(rows.map(r => r.label)).toEqual(['KurortEngine', 'shell tools']);
        expect(rows.every(r => r.count === 1)).toBe(true);
    });

    it('returns nothing for an empty list', () => {
        expect(groupDriftForDisplay([])).toEqual([]);
    });
});
