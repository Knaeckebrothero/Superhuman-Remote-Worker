import {describe, expect, it} from 'vitest';
import {lineDiff} from './line-diff';

describe('lineDiff', () => {
    it('treats an empty before as all additions (write_file / append case)', () => {
        expect(lineDiff('', 'a\nb')).toEqual([
            {type: 'add', text: 'a'},
            {type: 'add', text: 'b'},
        ]);
    });

    it('treats an empty after as all deletions', () => {
        expect(lineDiff('a\nb', '')).toEqual([
            {type: 'del', text: 'a'},
            {type: 'del', text: 'b'},
        ]);
    });

    it('marks identical content entirely as context', () => {
        const d = lineDiff('a\nb\nc', 'a\nb\nc');
        expect(d).toHaveLength(3);
        expect(d.every((l) => l.type === 'ctx')).toBe(true);
    });

    it('shows a single-line replacement with surrounding context', () => {
        expect(lineDiff('a\nb\nc', 'a\nB\nc')).toEqual([
            {type: 'ctx', text: 'a'},
            {type: 'del', text: 'b'},
            {type: 'add', text: 'B'},
            {type: 'ctx', text: 'c'},
        ]);
    });

    it('shows a mid-block insertion as a single addition', () => {
        expect(lineDiff('a\nc', 'a\nb\nc')).toEqual([
            {type: 'ctx', text: 'a'},
            {type: 'add', text: 'b'},
            {type: 'ctx', text: 'c'},
        ]);
    });

    it('ignores a single trailing newline so it is not a phantom change', () => {
        expect(lineDiff('a\nb\n', 'a\nb')).toEqual([
            {type: 'ctx', text: 'a'},
            {type: 'ctx', text: 'b'},
        ]);
    });

    it('returns nothing for two empty strings', () => {
        expect(lineDiff('', '')).toEqual([]);
    });
});
