/**
 * Strip Markdown syntax down to readable plain text for compact previews
 * (e.g. the one-line snippet on a knowledge-note card). The full note renders
 * as real Markdown in its detail view; previews are truncated, and truncating
 * raw Markdown both looks noisy (`#`, `**`, backticks) and can cut a construct
 * in half, so we flatten to prose first.
 *
 * This is deliberately a lightweight regex pass, not a real parser: previews
 * are short and lossy by nature, so perfect fidelity isn't the goal. Underscore
 * emphasis is only unwrapped at word boundaries so `snake_case` identifiers and
 * file paths — which these notes are full of — survive intact.
 */
export function stripMarkdown(md: string | undefined | null): string {
    if (!md) return '';
    let s = md;

    // Fenced code blocks: drop the ``` fences (and any info string), keep the code.
    s = s.replace(/```[^\n]*\n?/g, '');
    // Images first (their syntax contains a link): ![alt](url) -> alt
    s = s.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1');
    // Links: [text](url) -> text
    s = s.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1');
    // Inline code: `code` -> code
    s = s.replace(/`([^`]+)`/g, '$1');
    // Bold/italic with asterisks (* is never part of an identifier): **x** *x* -> x
    s = s.replace(/(\*\*\*|\*\*|\*)(\S(?:.*?\S)?)\1/g, '$2');
    // Strikethrough: ~~x~~ -> x
    s = s.replace(/~~(\S(?:.*?\S)?)~~/g, '$1');
    // Underscore emphasis only when flanked by non-word chars, so snake_case survives.
    s = s.replace(/(?<![A-Za-z0-9])(___|__|_)(\S(?:.*?\S)?)\1(?![A-Za-z0-9])/g, '$2');
    // Leading heading markers: ## Title -> Title
    s = s.replace(/^\s{0,3}#{1,6}\s+/gm, '');
    // Blockquote markers: > quote -> quote
    s = s.replace(/^\s*>+\s?/gm, '');
    // Horizontal rules (---, ***, ___ on their own line) -> gone
    s = s.replace(/^\s*([-*_])(?:\s*\1){2,}\s*$/gm, '');
    // List markers at line start: -, *, +, or 1. -> gone
    s = s.replace(/^\s*[-*+]\s+/gm, '');
    s = s.replace(/^\s*\d+\.\s+/gm, '');
    // Collapse all runs of whitespace (incl. newlines) into single spaces for a one-line preview.
    s = s.replace(/\s+/g, ' ').trim();

    return s;
}
