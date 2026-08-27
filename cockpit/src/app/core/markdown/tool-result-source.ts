/**
 * Turns a raw tool result into something a markdown parser can render.
 *
 * Two defects in `read_file` output make the naive `<markdown [data]="content">`
 * unusable, and both are fixed here rather than in the component:
 *
 * 1. Every line arrives as `f"{i:6}\t{line}"` (`src/tools/workspace/files.py:502`).
 *    Six leading spaces is an indented code block in CommonMark, so the whole
 *    document would render as one grey blob.
 * 2. YAML frontmatter renders as an `<hr>` followed by a *setext H2* — the
 *    closing `---` underlines the collapsed `name:`/`description:` paragraph —
 *    so a skill file would open with its own metadata as a giant heading.
 *
 * Both functions are deliberately conservative: given content they do not
 * recognise they return it byte-identical, so results from tools that never
 * number their output (`web_search`, `kb_read`) pass straight through.
 */

/** A `cat -n` prefix: right-padded number, then a literal tab. */
const NUMBERED_LINE = /^ *(\d+)\t/;

/**
 * Share of non-empty lines that must be numbered before we believe the block
 * really is `cat -n` output. Below 1 the tool's own unnumbered footers
 * (`[Lines 1-200 of 900. …]`, `[TRUNCATED at word limit …]`) would veto the
 * strip on exactly the truncated reads that need it most.
 */
const MIN_NUMBERED_SHARE = 0.7;

/**
 * Removes `cat -n` prefixes, but only from a block that convincingly *is*
 * numbered output: at least two numbered lines, strictly increasing, and a
 * clear majority of the non-empty lines. Monotonicity is what separates real
 * numbering from a markdown file that happens to contain tab-separated digits.
 */
export function stripLineNumbers(content: string): string {
  const lines = content.split('\n');
  let nonEmpty = 0;
  let numbered = 0;
  let previous = Number.NEGATIVE_INFINITY;
  let increasing = true;

  for (const line of lines) {
    if (line.trim() !== '') nonEmpty++;
    const match = NUMBERED_LINE.exec(line);
    if (!match) continue;
    numbered++;
    const value = Number(match[1]);
    if (value <= previous) increasing = false;
    previous = value;
  }

  if (numbered < 2 || !increasing) return content;
  if (numbered < nonEmpty * MIN_NUMBERED_SHARE) return content;
  return lines.map((line) => line.replace(NUMBERED_LINE, '')).join('\n');
}

/**
 * Wraps leading YAML frontmatter in a ```yaml fence so it renders as a small
 * code block. An opening `---` with no closing `---` is left alone: that is an
 * ordinary horizontal rule and rewriting it would change the document.
 */
export function fenceFrontmatter(content: string): string {
  if (!content.startsWith('---\n')) return content;

  const lines = content.split('\n');
  const close = lines.findIndex((line, i) => i > 0 && line === '---');
  if (close === -1) return content;

  return ['```yaml', ...lines.slice(1, close), '```', ...lines.slice(close + 1)].join('\n');
}

/**
 * Strip first: frontmatter can only be recognised once the numbering is gone,
 * because `---` sits at column 7 until then.
 */
export function toRenderableMarkdown(content: string): string {
  return fenceFrontmatter(stripLineNumbers(content));
}
