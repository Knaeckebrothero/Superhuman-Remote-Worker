/**
 * Minimal line-level diff for rendering file-edit tool cards (#7 of
 * docs/features/persistent_chat_polish.md).
 *
 * `edit_file` replace calls carry both `old_string` and `new_string` in their
 * args, so we can show a real before/after diff with no backend round-trip.
 * `write_file` / append / prepend have no "before", so they diff against ''
 * and render as all-additions. Kept dependency-free (no diff2html) — these
 * snippets are small and a tiny LCS is plenty.
 */

export interface DiffLine {
    type: 'add' | 'del' | 'ctx';
    text: string;
}

/** Split into lines, ignoring a single trailing newline so a final blank
 *  line doesn't surface as a phantom change. Empty string → no lines. */
function splitLines(s: string): string[] {
    if (s === '') return [];
    return s.replace(/\n$/, '').split('\n');
}

/**
 * Longest-common-subsequence line diff. Returns an ordered list of lines
 * tagged add / del / ctx (context) for a unified-diff style render.
 */
export function lineDiff(before: string, after: string): DiffLine[] {
    const a = splitLines(before);
    const b = splitLines(after);
    const m = a.length;
    const n = b.length;

    // lcs[i][j] = length of the LCS of a[i:] and b[j:].
    const lcs: number[][] = Array.from({length: m + 1}, () => new Array<number>(n + 1).fill(0));
    for (let i = m - 1; i >= 0; i--) {
        for (let j = n - 1; j >= 0; j--) {
            lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
        }
    }

    const out: DiffLine[] = [];
    let i = 0;
    let j = 0;
    while (i < m && j < n) {
        if (a[i] === b[j]) {
            out.push({type: 'ctx', text: a[i]});
            i++;
            j++;
        } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
            out.push({type: 'del', text: a[i]});
            i++;
        } else {
            out.push({type: 'add', text: b[j]});
            j++;
        }
    }
    while (i < m) out.push({type: 'del', text: a[i++]});
    while (j < n) out.push({type: 'add', text: b[j++]});
    return out;
}
