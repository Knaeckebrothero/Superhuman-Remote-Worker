import DOMPurify from 'dompurify';

/**
 * Sanitize agent-controlled Markdown HTML without permitting passive network
 * requests. The image extension handles Markdown image syntax; this policy is
 * the independent boundary for raw HTML and future parser features.
 */
export function sanitizeMarkdownHtml(html: string): string {
  return DOMPurify.sanitize(html, {
    USE_PROFILES: {html: true},
    ALLOW_DATA_ATTR: true,
    FORBID_TAGS: [
      'audio',
      'base',
      'embed',
      'form',
      'frame',
      'frameset',
      'iframe',
      'img',
      'input',
      'link',
      'meta',
      'object',
      'picture',
      'script',
      'source',
      'style',
      'svg',
      'track',
      'video',
    ],
    FORBID_ATTR: [
      'action',
      'background',
      'formaction',
      'ping',
      'poster',
      'src',
      'srcset',
      'style',
    ],
    SANITIZE_DOM: true,
    SANITIZE_NAMED_PROPS: true,
  });
}
