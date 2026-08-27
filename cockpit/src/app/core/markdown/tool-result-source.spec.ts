import {describe, expect, it} from 'vitest';
import {fenceFrontmatter, stripLineNumbers, toRenderableMarkdown} from './tool-result-source';

/** How `read_file` formats every line: `f"{i:6}\t{line}"` (src/tools/workspace/files.py:502). */
function numbered(lines: string[], start = 1): string {
  return lines.map((line, i) => `${String(i + start).padStart(6, ' ')}\t${line}`).join('\n');
}

describe('stripLineNumbers', () => {
  it('removes the cat -n prefix from a uniformly numbered block', () => {
    const source = numbered(['# Title', '', 'Some prose.']);

    expect(stripLineNumbers(source)).toBe('# Title\n\nSome prose.');
  });

  it('keeps numbering past the six-column pad width', () => {
    const source = numbered(['line a', 'line b'], 999_999);

    expect(stripLineNumbers(source)).toBe('line a\nline b');
  });

  it('leaves the block untouched when the numbers do not increase', () => {
    const source = '     7\tseven\n     3\tthree\n     9\tnine';

    expect(stripLineNumbers(source)).toBe(source);
  });

  it('leaves the block untouched when most lines are unnumbered', () => {
    const source = `${numbered(['1. first', '2. second'])}\nprose\nmore\nyet more\nand more\nstill more`;

    expect(stripLineNumbers(source)).toBe(source);
  });

  it('strips a numbered body that carries the tool’s unnumbered footer', () => {
    const source = `${numbered(['# Title', '', 'a', 'b', 'c'])}\n\n[Lines 1-5 of 900.]`;

    expect(stripLineNumbers(source)).toBe('# Title\n\na\nb\nc\n\n[Lines 1-5 of 900.]');
  });

  it('preserves indentation that follows the tab', () => {
    const source = numbered(['- top', '    - nested']);

    expect(stripLineNumbers(source)).toBe('- top\n    - nested');
  });

  it('returns unnumbered content unchanged, so web_search results pass through', () => {
    const source = '## Result\n\nA paragraph with a\ttab in it.';

    expect(stripLineNumbers(source)).toBe(source);
  });
});

describe('fenceFrontmatter', () => {
  it('wraps leading YAML frontmatter in a yaml fence', () => {
    const source = '---\nname: cite-as-you-write\ntags:\n  - citations\n---\n\n# Cite As You Write';

    expect(fenceFrontmatter(source)).toBe(
      '```yaml\nname: cite-as-you-write\ntags:\n  - citations\n```\n\n# Cite As You Write',
    );
  });

  it('leaves a document without frontmatter unchanged', () => {
    const source = '# Title\n\nProse that mentions --- in passing.';

    expect(fenceFrontmatter(source)).toBe(source);
  });

  it('leaves an unclosed leading --- alone, so a horizontal rule stays a rule', () => {
    const source = '---\n\n# Title\n\nProse.';

    expect(fenceFrontmatter(source)).toBe(source);
  });

  it('ignores a --- fence that does not start the document', () => {
    const source = '# Title\n\n---\nnot: frontmatter\n---\n';

    expect(fenceFrontmatter(source)).toBe(source);
  });
});

describe('toRenderableMarkdown', () => {
  it('strips numbering before recognising frontmatter at position 0', () => {
    const source = numbered(['---', 'name: cite-as-you-write', '---', '', '# Cite As You Write']);

    expect(toRenderableMarkdown(source)).toBe(
      '```yaml\nname: cite-as-you-write\n```\n\n# Cite As You Write',
    );
  });

  it('keeps the trailing truncation note the tool appends', () => {
    const body = numbered(['# Title', '', 'One.', 'Two.', 'Three.']);
    const source = `${body}\n\n[Lines 1-5 of 900. Use offset to read more.]`;

    expect(toRenderableMarkdown(source)).toBe(
      '# Title\n\nOne.\nTwo.\nThree.\n\n[Lines 1-5 of 900. Use offset to read more.]',
    );
  });
});
