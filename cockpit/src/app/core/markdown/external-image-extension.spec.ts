import {Marked} from 'marked';
import {describe, expect, it} from 'vitest';
import {
  EXTERNAL_IMAGE_PLACEHOLDER_CLASS,
  externalImageExtension,
} from './external-image-extension';

function render(markdown: string): HTMLElement {
  const marked = new Marked({gfm: true});
  marked.use(externalImageExtension());
  const template = document.createElement('template');
  template.innerHTML = marked.parse(markdown) as string;
  return template.content.firstElementChild as HTMLElement;
}

describe('externalImageExtension', () => {
  it('turns an inline image into inert URL review data without an img element', () => {
    const url = 'https://images.example/chart.png?account=private';
    const root = render(`![Quarterly chart](${url})`);
    const placeholder = root.querySelector<HTMLElement>(
      `.${EXTERNAL_IMAGE_PLACEHOLDER_CLASS}`,
    );

    expect(root.querySelector('img')).toBeNull();
    expect(placeholder?.dataset['externalImageUrl']).toBe(url);
    expect(placeholder?.dataset['externalImageAlt']).toBe('Quarterly chart');
    expect(placeholder?.textContent).toContain(url);
  });

  it('also neutralizes reference-style images', () => {
    const root = render(
      '![Remote diagram][diagram]\n\n[diagram]: https://cdn.example/diagram.webp?q=1',
    );
    const placeholder = root.querySelector<HTMLElement>(
      `.${EXTERNAL_IMAGE_PLACEHOLDER_CLASS}`,
    );

    expect(root.querySelector('img')).toBeNull();
    expect(placeholder?.dataset['externalImageUrl']).toBe(
      'https://cdn.example/diagram.webp?q=1',
    );
  });

  it('HTML-escapes attacker-controlled URL and alt attributes', () => {
    const root = render(
      `![A "quoted" image](https://images.example/a.png?x=1&y=2)`,
    );
    const placeholder = root.querySelector<HTMLElement>(
      `.${EXTERNAL_IMAGE_PLACEHOLDER_CLASS}`,
    );

    expect(root.querySelector('script')).toBeNull();
    expect(placeholder?.dataset['externalImageAlt']).toBe('A "quoted" image');
    expect(placeholder?.dataset['externalImageUrl']).toBe(
      'https://images.example/a.png?x=1&y=2',
    );
  });
});
