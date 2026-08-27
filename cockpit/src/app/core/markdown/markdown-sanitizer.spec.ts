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
