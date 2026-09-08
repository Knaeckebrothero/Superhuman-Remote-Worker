import DOMPurify from 'dompurify';
import {
  classifyMarkdownLink,
  WORKSPACE_FILE_LINK_CLASS,
  WORKSPACE_FILE_PATH_ATTR,
} from './link-extension';

/**
 * Sanitize agent-controlled Markdown HTML without permitting passive network
 * requests. The image extension handles Markdown image syntax; this policy is
 * the independent boundary for raw HTML and future parser features.
 *
 * It is also where the link policy is enforced rather than merely rendered:
 * `markdownLinkExtension` classifies Markdown link syntax, but raw `<a href>`
 * in agent HTML never passes through a Marked renderer. Both meet here, so no
 * agent-written anchor can navigate the Cockpit document — a relative href
 * resolves against `<base href="/">` and used to land the reader on the SPA
 * catch-all, discarding the session they were reading.
 */
export function sanitizeMarkdownHtml(html: string): string {
  const fragment = DOMPurify.sanitize(html, {
    USE_PROFILES: {html: true},
    ALLOW_DATA_ATTR: true,
    // `target` is not in DOMPurify's default set; external links need it to
    // open beside the session instead of replacing it. Agent-chosen values
    // never survive — applyAnchorPolicy rewrites every anchor below.
    ADD_ATTR: ['target'],
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
    RETURN_DOM_FRAGMENT: true,
    SANITIZE_DOM: true,
    SANITIZE_NAMED_PROPS: true,
  }) as unknown as DocumentFragment;

  for (const anchor of Array.from(fragment.querySelectorAll('a'))) {
    applyAnchorPolicy(anchor);
  }

  const template = (fragment.ownerDocument ?? document).createElement('template');
  template.content.appendChild(fragment);
  return template.innerHTML;
}

/**
 * Give one anchor the only shape it is allowed to keep.
 *
 * Remote targets open in a new tab with no opener; a workspace path becomes
 * the same inert control the Markdown parser emits; anything else keeps its
 * words and loses its target.
 */
function applyAnchorPolicy(anchor: HTMLAnchorElement): void {
  const link = classifyMarkdownLink(anchor.getAttribute('href'));

  if (link.kind === 'external') {
    anchor.setAttribute('href', link.href ?? '');
    anchor.setAttribute('target', '_blank');
    anchor.setAttribute('rel', 'noopener noreferrer');
    return;
  }

  if (link.kind === 'plain') {
    anchor.setAttribute('href', link.href ?? '');
    anchor.removeAttribute('target');
    return;
  }

  if (link.kind === 'workspace-file' && link.path) {
    const control = (anchor.ownerDocument ?? document).createElement('button');
    control.type = 'button';
    control.className = WORKSPACE_FILE_LINK_CLASS;
    control.setAttribute(WORKSPACE_FILE_PATH_ATTR, link.path);
    control.setAttribute('title', link.path);
    control.append(...Array.from(anchor.childNodes));
    if (!control.textContent?.trim()) control.textContent = link.path;
    anchor.replaceWith(control);
    return;
  }

  anchor.removeAttribute('href');
  anchor.removeAttribute('target');
}
