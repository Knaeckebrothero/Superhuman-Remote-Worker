import {Marked} from 'marked';
import {describe, expect, it} from 'vitest';
import {
  classifyMarkdownLink,
  markdownLinkExtension,
  normalizeWorkspacePath,
} from './link-extension';
import {sanitizeMarkdownHtml} from './markdown-sanitizer';

function render(markdown: string): string {
  const marked = new Marked();
  marked.use(markdownLinkExtension());
  return sanitizeMarkdownHtml(marked.parse(markdown, {async: false}) as string);
}

describe('normalizeWorkspacePath', () => {
  it('collapses the spellings an agent writes for one file', () => {
    for (const spelling of [
      'output/report.md',
      './output/report.md',
      '/output/report.md',
      'output//report.md',
      'output/./report.md',
      'output\\report.md',
      '  output/report.md  ',
    ]) {
      expect(normalizeWorkspacePath(spelling), spelling).toBe('output/report.md');
    }
  });

  it('drops a query or fragment the path does not need', () => {
    expect(normalizeWorkspacePath('output/report.md?v=2')).toBe('output/report.md');
    expect(normalizeWorkspacePath('output/report.md#top')).toBe('output/report.md');
  });

  it('refuses anything that is not a path inside the workspace', () => {
    expect(normalizeWorkspacePath('')).toBeNull();
    expect(normalizeWorkspacePath('   ')).toBeNull();
    expect(normalizeWorkspacePath('/')).toBeNull();
    expect(normalizeWorkspacePath('.')).toBeNull();
    expect(normalizeWorkspacePath('?q=1')).toBeNull();
    expect(normalizeWorkspacePath('../secrets.env')).toBeNull();
    expect(normalizeWorkspacePath('output/../../secrets.env')).toBeNull();
    expect(normalizeWorkspacePath(`a/${'b'.repeat(5000)}`)).toBeNull();
  });
});

describe('classifyMarkdownLink', () => {
  it('sends http(s) and protocol-relative targets out of the document', () => {
    expect(classifyMarkdownLink('https://example.test/a').kind).toBe('external');
    expect(classifyMarkdownLink('http://example.test/a').kind).toBe('external');
    expect(classifyMarkdownLink('//example.test/a').kind).toBe('external');
  });

  it('keeps contact and in-page targets as ordinary anchors', () => {
    expect(classifyMarkdownLink('mailto:someone@example.test').kind).toBe('plain');
    expect(classifyMarkdownLink('tel:+4915112345678').kind).toBe('plain');
    expect(classifyMarkdownLink('#section').kind).toBe('plain');
  });

  it('treats every other scheme as inert', () => {
    for (const href of [
      'javascript:alert(1)',
      'JavaScript:alert(1)',
      'data:text/html,<script>alert(1)</script>',
      'file:///etc/passwd',
      'blob:https://example.test/uuid',
    ]) {
      expect(classifyMarkdownLink(href).kind, href).toBe('inert');
    }
  });

  it('reads a bare path as the workspace file it addresses', () => {
    expect(classifyMarkdownLink('output/report.md')).toEqual({
      kind: 'workspace-file',
      path: 'output/report.md',
    });
    expect(classifyMarkdownLink('/output/report.md')).toEqual({
      kind: 'workspace-file',
      path: 'output/report.md',
    });
  });
});

describe('markdownLinkExtension', () => {
  it('never gives an agent-written workspace path an href', () => {
    // The regression this exists for: `<base href="/">` turned this link into
    // a same-tab navigation to /output/…, which the SPA catch-all answered
    // with a fresh draft session.
    const html = render('- [Master prospect CSV](output/regional_prospects.csv)');

    expect(html).not.toContain('href');
    expect(html).toContain('data-workspace-file-path="output/regional_prospects.csv"');
    expect(html).toContain('class="workspace-file-link"');
    expect(html).toContain('Master prospect CSV');
  });

  it('opens an external link in a new tab with no opener', () => {
    const html = render('[TechQuartier](https://techquartier.test/news)');

    expect(html).toContain('href="https://techquartier.test/news"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it('renders a scripted target as text, keeping the words', () => {
    const html = render('[click me](javascript:alert(1))');

    expect(html).toContain('click me');
    expect(html).not.toContain('<a');
    expect(html).not.toContain('javascript:');
  });

  it('keeps inline formatting inside the label', () => {
    const html = render('[**Ranked** report](output/report.md)');

    expect(html).toContain('<strong>Ranked</strong>');
    expect(html).toContain('data-workspace-file-path="output/report.md"');
  });

  it('falls back to the path when the link has no label', () => {
    expect(render('[](output/report.md)')).toContain('>output/report.md</button>');
  });

  it('escapes a path that tries to close its own attribute', () => {
    const html = render('[x](out"><img src=x onerror=alert(1)>/a.md)');

    expect(html).not.toContain('onerror');
    expect(html).not.toContain('<img');
  });
});
