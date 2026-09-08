import {describe, expect, it} from 'vitest';
import {sanitizeMarkdownHtml} from './markdown-sanitizer';

function fragment(html: string): DocumentFragment {
  const template = document.createElement('template');
  template.innerHTML = sanitizeMarkdownHtml(html);
  return template.content;
}

describe('sanitizeMarkdownHtml', () => {
  it('removes raw HTML passive-resource loading paths', () => {
    const result = fragment(`
      <img src="https://attacker.example/pixel">
      <picture><source srcset="https://attacker.example/a 1x"></picture>
      <video src="https://attacker.example/v" poster="https://attacker.example/p"></video>
      <svg><image href="https://attacker.example/s"></image></svg>
      <div style="background-image:url(https://attacker.example/css)">safe text</div>
    `);

    expect(result.querySelector('img,picture,source,video,svg,image')).toBeNull();
    expect(result.querySelector('[src],[srcset],[poster],[style]')).toBeNull();
    expect(result.textContent).toContain('safe text');
  });

  it('preserves inert image-card data and ordinary safe links', () => {
    const result = fragment(`
      <span class="external-image-placeholder"
            data-external-image-url="https://images.example/a.png?q=1"
            data-external-image-alt="chart"></span>
      <a href="https://docs.example/report">report</a>
    `);
    const placeholder = result.querySelector<HTMLElement>(
      '.external-image-placeholder',
    );

    expect(placeholder?.dataset['externalImageUrl']).toBe(
      'https://images.example/a.png?q=1',
    );
    expect(result.querySelector('a')?.getAttribute('href')).toBe(
      'https://docs.example/report',
    );
  });
});

describe('sanitizeMarkdownHtml link policy', () => {
  it('sends a remote anchor to a new tab with no opener', () => {
    const anchor = fragment('<a href="https://docs.example/report">report</a>')
      .querySelector('a');

    expect(anchor?.getAttribute('href')).toBe('https://docs.example/report');
    expect(anchor?.getAttribute('target')).toBe('_blank');
    expect(anchor?.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('never lets raw HTML keep a same-document target', () => {
    const result = fragment(
      '<a href="https://docs.example/a" target="_self">a</a>' +
        '<a href="mailto:someone@example.test" target="_top">b</a>',
    );
    const [remote, mail] = Array.from(result.querySelectorAll('a'));

    expect(remote.getAttribute('target')).toBe('_blank');
    expect(mail.getAttribute('target')).toBeNull();
    expect(mail.getAttribute('href')).toBe('mailto:someone@example.test');
  });

  it('turns a raw-HTML workspace path into the inert control, not a navigation', () => {
    // Raw HTML never passes through the Marked renderer, so the parser-side
    // policy cannot see this one: `/output/…` would resolve against
    // <base href="/"> and replace the running session with a fresh draft.
    const result = fragment('<a href="/output/prospects.csv">Master prospect CSV</a>');

    expect(result.querySelector('a')).toBeNull();
    const control = result.querySelector<HTMLButtonElement>('button.workspace-file-link');
    expect(control?.getAttribute('data-workspace-file-path')).toBe('output/prospects.csv');
    expect(control?.textContent).toBe('Master prospect CSV');
  });

  it('strips an unusable target but keeps the words', () => {
    const result = fragment('<a href="javascript:alert(1)">click me</a>');

    expect(result.querySelector('a')?.getAttribute('href')).toBeNull();
    expect(result.textContent).toContain('click me');
  });
});
