import {MarkedExtension, Tokens} from 'marked';

export const EXTERNAL_IMAGE_PLACEHOLDER_CLASS = 'external-image-placeholder';

/**
 * Converts every Markdown image token into inert review data.
 *
 * This happens inside Marked, before ngx-markdown writes HTML into the DOM.
 * A post-render rewrite would be too late: browsers may start fetching an
 * `<img src>` as soon as it is parsed. Marked resolves both inline and
 * reference-style images through this renderer.
 */
export function externalImageExtension(): MarkedExtension {
  return {
    renderer: {
      image({href, text}: Tokens.Image): string {
        const safeUrl = escapeHtml(href);
        const safeAlt = escapeHtml(text);
        return (
          `<span class="${EXTERNAL_IMAGE_PLACEHOLDER_CLASS}" ` +
          `data-external-image-url="${safeUrl}" ` +
          `data-external-image-alt="${safeAlt}">` +
          `<span class="external-image-placeholder__fallback">` +
          `<code>${safeUrl}</code>` +
          `</span></span>`
        );
      },
    },
  };
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
