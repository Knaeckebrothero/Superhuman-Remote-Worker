import {describe, expect, it} from 'vitest';
import {CanvasState} from '../../core/models/canvas.model';
import {
  CANVAS_MARKDOWN_MAX_SOURCE_CHARS,
  CANVAS_MARKDOWN_MAX_TOKENS,
  CANVAS_MATH_MAX_EXPRESSIONS,
  CANVAS_RENDER_MAX_NODES,
  CANVAS_RENDER_MAX_OUTPUT_CHARS,
  CANVAS_STATIC_MAX_TOTAL_STYLE_CHARS,
  STATIC_HTML_CSP,
  renderCanvasMarkdown,
  renderCanvasStaticHtml,
  resolveCanvasContentUrl,
  selectCanvasChromeState,
  selectCanvasRenderer,
  shouldResetCanvasImageZoom,
} from './canvas-rendering';

function state(renderer: CanvasState['renderer'], sourceType = 'workspace_file'): CanvasState {
  return {
    canvas_id: 'main',
    source: sourceType === 'workspace_file'
      ? {type: 'workspace_file', path: 'output/report.md'}
      : {type: sourceType},
    title: 'Report',
    renderer,
    editable: false,
    alt_text: null,
    presentation_revision: 1,
    source_version: 'sha256:one',
    status: 'ready',
    capabilities: {can_edit: false, can_pop_out: false, can_take_control: false},
    updated_at: '2026-07-13T10:00:00Z',
  };
}

describe('Dynamic Canvas rendering trust boundary', () => {
  it.each(['markdown', 'text', 'html', 'image'] as const)(
    'selects the trusted %s renderer only for workspace files',
    renderer => {
      expect(selectCanvasRenderer(state(renderer))).toBe(renderer);
      expect(selectCanvasRenderer(state(renderer, 'workspace_app'))).toBe('unsupported');
    },
  );

  it('fails closed when the backend leaves a presented renderer as auto', () => {
    expect(selectCanvasRenderer(state('auto'))).toBe('unsupported');
  });

  it('preserves image zoom for a same-source refresh and resets on replacement', () => {
    expect(shouldResetCanvasImageZoom(null, 'workspace_file:chart.png')).toBe(false);
    expect(shouldResetCanvasImageZoom('workspace_file:chart.png', 'workspace_file:chart.png'))
      .toBe(false);
    expect(shouldResetCanvasImageZoom('workspace_file:chart.png', 'workspace_file:other.png'))
      .toBe(true);
  });

  it('keeps preserved edit chrome attached to the original source after replacement', () => {
    const original = state('markdown');
    const replacement = {
      ...state('text'),
      source: {type: 'workspace_file', path: 'output/replacement.txt'},
      title: 'Replacement',
      presentation_revision: 2,
    } as CanvasState;

    expect(selectCanvasChromeState(replacement, original, true)).toBe(original);
    expect(selectCanvasChromeState(replacement, original, false)).toBe(replacement);
  });

  it('accepts only complete, versioned server URLs on the configured API origin', () => {
    const pointer =
      '/api/persistent/threads/t-1/canvases/main/content' +
      '?presentation_revision=7&source_fingerprint=fp&source_version=sha256%3Aone&ngsw-bypass=true';
    expect(resolveCanvasContentUrl(pointer, 'https://api.example.test/api', 'https://cockpit.test/'))
      .toBe(`https://api.example.test${pointer}`);

    expect(resolveCanvasContentUrl('https://evil.test/file', 'https://api.example.test/api')).toBeNull();
    expect(resolveCanvasContentUrl('//evil.test/file', 'https://api.example.test/api')).toBeNull();
    expect(resolveCanvasContentUrl('/api/%255c%255cevil.test/file?presentation_revision=1&source_fingerprint=x&source_version=y&ngsw-bypass=true', 'https://api.example.test/api')).toBeNull();
    expect(resolveCanvasContentUrl('/api/file?ngsw-bypass=true', 'https://api.example.test/api')).toBeNull();
  });

  it('uses isolated Markdown semantics with bounded math and inert untrusted resources', () => {
    const result = renderCanvasMarkdown(
      '# Report\n\n$E=mc^2$\n\n' +
      '<script>alert(1)</script>\n\n' +
      '![tracking pixel](https://tracker.test/pixel.png)\n\n' +
      '[external](https://example.test/path) [relative](./other.md) [1]',
    );
    expect(result.errorCode).toBeNull();
    const html = result.html;

    expect(html).toContain('class="katex"');
    expect(html).toContain('&lt;script&gt;');
    expect(html).not.toContain('<script');
    expect(html).not.toContain('<img');
    expect(html).not.toContain('tracker.test');
    expect(html).toContain('aria-label="tracking pixel"');
    expect(html).toContain('data-canvas-external="https://example.test/path"');
    expect(html).not.toContain('href="./other.md"');
    expect(html).not.toContain('citation-ref');
    expect(html).toContain('[1]');
  });

  it('bounds adversarial KaTeX work and falls back to escaped text', () => {
    const manyExpressions = Array.from(
      {length: CANVAS_MATH_MAX_EXPRESSIONS + 20},
      (_, index) => `$x_${index}$`,
    ).join(' ');
    const manyResult = renderCanvasMarkdown(manyExpressions);
    expect(manyResult.errorCode).toBeNull();
    const manyHtml = manyResult.html;
    expect((manyHtml.match(/class="katex"/g) ?? []).length).toBe(CANVAS_MATH_MAX_EXPRESSIONS);
    expect(manyHtml).toContain('canvas-math-fallback');

    const oversizedResult = renderCanvasMarkdown(`$${'<script>'.repeat(700)}$`);
    expect(oversizedResult.errorCode).toBeNull();
    const oversized = oversizedResult.html;
    expect(oversized).toContain('canvas-math-fallback');
    expect(oversized).not.toContain('<script>');
    expect(oversized).toContain('&lt;script&gt;');
  });

  it('makes static HTML script-free, non-navigating, and opaque-srcdoc ready', () => {
    const result = renderCanvasStaticHtml(`
      <base href="https://evil.test/">
      <script>parent.location='https://evil.test/'</script>
      <form action="https://evil.test/"><input name="secret"><button>Send</button></form>
      <a id="outside" href="https://evil.test/" onclick="steal()">outside</a>
      <a id="inside" href="#section">inside</a>
      <img id="remote" src="https://tracker.test/pixel.png" onerror="steal()">
      <img id="inline" alt="chart" src="data:image/png;base64,iVBORw0KGgo=">
      <style>@import 'https://evil.test/x.css'; .x { background:url(https://evil.test/x) }</style>
      <div class="x" style="color:red;animation:spin 1s;filter:blur(2px);background-image:url(https://evil.test/y)">content</div>
    `);
    expect(result.errorCode).toBeNull();
    const srcdoc = result.html;

    expect(srcdoc).toContain(STATIC_HTML_CSP.replace(/'/g, '&#39;'));
    expect(srcdoc).not.toMatch(/<script|<form|<input|<button|<base/i);
    expect(srcdoc).not.toMatch(/onerror|onclick|https:\/\/evil\.test|tracker\.test|@import/i);
    const parsed = new DOMParser().parseFromString(srcdoc, 'text/html');
    const outside = parsed.querySelector('#user-content-outside');
    const inside = parsed.querySelector('#user-content-inside');
    const remote = parsed.querySelector('#user-content-remote');
    const inline = parsed.querySelector('#user-content-inline');
    expect(outside?.hasAttribute('href')).toBe(false);
    expect(inside?.getAttribute('href')).toBe('#section');
    expect(remote?.hasAttribute('src')).toBe(false);
    expect(inline?.hasAttribute('src')).toBe(false);
    const content = parsed.querySelector('.x');
    expect(parsed.querySelector('style')).toBeNull();
    expect(content?.getAttribute('style')).toBe('color: red;');
    expect(srcdoc).not.toMatch(/animation|filter|background-image/i);
  });

  it('rejects Markdown token bombs before lexing while accepting plain text at the file ceiling', () => {
    const listBomb = '- x\n'.repeat(Math.floor(CANVAS_MARKDOWN_MAX_SOURCE_CHARS / 4));
    expect(listBomb.length).toBe(CANVAS_MARKDOWN_MAX_SOURCE_CHARS);
    expect(renderCanvasMarkdown(listBomb)).toEqual({
      html: '',
      errorCode: 'canvas_content_too_complex',
    });

    const plainText = 'x'.repeat(CANVAS_MARKDOWN_MAX_SOURCE_CHARS - 1);
    const accepted = renderCanvasMarkdown(plainText);
    expect(accepted.errorCode).toBeNull();
    expect(accepted.html.length).toBeGreaterThanOrEqual(plainText.length);
    expect(accepted.html.length).toBeLessThan(CANVAS_RENDER_MAX_OUTPUT_CHARS);
  });

  it('allows only bounded inert inline CSS and rejects an aggregate style bomb', () => {
    const unsafe = renderCanvasStaticHtml(
      `<div style="position:fixed;color:blue;transform:scale(100);width:var(--secret)">safe</div>`,
    );
    expect(unsafe.errorCode).toBeNull();
    const parsed = new DOMParser().parseFromString(unsafe.html, 'text/html');
    expect(parsed.querySelector('div')?.getAttribute('style')).toBe('color: blue;');

    const style = `font-family:${'a'.repeat(500)}`;
    const elementCount = Math.ceil(CANVAS_STATIC_MAX_TOTAL_STYLE_CHARS / style.length) + 2;
    const styleBomb = `<span style="${style}">x</span>`.repeat(elementCount);
    expect(renderCanvasStaticHtml(styleBomb)).toEqual({
      html: '',
      errorCode: 'canvas_content_too_complex',
    });
  });

  it('accepts the exact sanitized node ceiling and rejects one additional text node', () => {
    const exact = '<span>x</span>'.repeat(CANVAS_RENDER_MAX_NODES / 2);
    const accepted = renderCanvasStaticHtml(exact);
    expect(accepted.errorCode).toBeNull();
    expect(accepted.html.length).toBeGreaterThan(0);

    const rejected = renderCanvasStaticHtml(`${exact}x`);
    expect(rejected).toEqual({html: '', errorCode: 'canvas_content_too_complex'});
  });

  it('allows a near-2 MiB text node without multiplying it into DOM nodes', () => {
    const source = 'x'.repeat(2 * 1024 * 1024 - 1);
    const result = renderCanvasStaticHtml(source);
    expect(result.errorCode).toBeNull();
    expect(result.html.length).toBeGreaterThanOrEqual(source.length);
    expect(result.html.length).toBeLessThan(CANVAS_RENDER_MAX_OUTPUT_CHARS);
  });

  it('rejects pathological Markdown and HTML with a fixed, source-free result', () => {
    const marker = '<script>do-not-reflect-me</script>';
    const markdown = Array.from(
      {length: CANVAS_MARKDOWN_MAX_TOKENS + 1},
      (_, index) => `- item ${index}`,
    ).join('\n') + marker;
    expect(renderCanvasMarkdown(markdown)).toEqual({
      html: '',
      errorCode: 'canvas_content_too_complex',
    });

    const html = '<span>x</span>'.repeat(CANVAS_RENDER_MAX_NODES + 1) + marker;
    const rejected = renderCanvasStaticHtml(html);
    expect(rejected).toEqual({html: '', errorCode: 'canvas_content_too_complex'});
    expect(rejected.html).not.toContain('do-not-reflect-me');
  });
});
