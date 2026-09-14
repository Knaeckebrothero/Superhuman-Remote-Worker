import {MarkedExtension, Tokens} from 'marked';

export const WORKSPACE_FILE_LINK_CLASS = 'workspace-file-link';
export const WORKSPACE_FILE_PATH_ATTR = 'data-workspace-file-path';

/** Mirrors the orchestrator's Canvas path bound (`canonical_workspace_path`). */
export const MAX_WORKSPACE_PATH_CHARS = 4096;

export type MarkdownLinkKind = 'external' | 'plain' | 'workspace-file' | 'inert';

export interface MarkdownLinkClassification {
  readonly kind: MarkdownLinkKind;
  /** Present for 'external' and 'plain' — the href to render. */
  readonly href?: string;
  /** Present for 'workspace-file' — the normalized workspace path. */
  readonly path?: string;
}

const SCHEME = /^[a-z][a-z0-9+.-]*:/i;
const CONTROL_CHARS = /[\u0000-\u001f\u007f]/;

/**
 * Normalize an agent-written path into the one spelling the Canvas uses.
 *
 * Returns null for anything that does not address a file inside the workspace
 * (empty, over-long, query-only, or escaping upwards through `..`).
 */
export function normalizeWorkspacePath(raw: string): string | null {
  if (!raw) return null;
  let path = raw.trim();
  if (!path || path.length > MAX_WORKSPACE_PATH_CHARS) return null;
  if (path.startsWith('?')) return null;
  const cut = Math.min(
    ...[path.indexOf('#'), path.indexOf('?')].filter((i) => i >= 0),
    path.length,
  );
  path = path.slice(0, cut).replace(/\\/g, '/');
  const segments = path.split('/').filter((segment) => segment !== '' && segment !== '.');
  if (segments.length === 0 || segments.includes('..')) return null;
  return segments.join('/');
}

/**
 * Decide what an agent-written Markdown link may become in the trusted host.
 *
 * The rule that matters: nothing an agent writes may perform a same-tab
 * navigation of the Cockpit document. A relative href resolves against
 * `<base href="/">`, so `[report](output/report.md)` used to leave the session
 * and land on the SPA catch-all — a fresh draft chat. Those hrefs address the
 * workspace, not a route, so they render as an inert in-app control instead.
 */
export function classifyMarkdownLink(rawHref: string | null | undefined): MarkdownLinkClassification {
  const href = (rawHref ?? '').trim();
  if (!href || CONTROL_CHARS.test(href)) return {kind: 'inert'};
  // In-page anchor: moves the hash, never the document.
  if (href.startsWith('#')) return {kind: 'plain', href};
  // Protocol-relative — the browser resolves it to a remote origin.
  if (href.startsWith('//')) return {kind: 'external', href};

  const scheme = SCHEME.exec(href)?.[0].toLowerCase();
  if (scheme) {
    if (scheme === 'http:' || scheme === 'https:') return {kind: 'external', href};
    if (scheme === 'mailto:' || scheme === 'tel:') return {kind: 'plain', href};
    // javascript:, data:, file:, blob:, … — no agent-chosen scheme gets an href.
    return {kind: 'inert'};
  }

  const path = normalizeWorkspacePath(href);
  return path ? {kind: 'workspace-file', path} : {kind: 'inert'};
}

/**
 * Replaces Marked's default link renderer for all agent-authored Markdown.
 *
 * External links open in a new tab (`noopener`), so following one never
 * discards the running session. Workspace paths become a `<button>` carrying
 * the path as data: `WorkspaceFileLinkDirective` upgrades it where a host
 * knows what to do with a file, and everywhere else it stays inert — which is
 * the fail-closed direction, unlike an href.
 */
export function markdownLinkExtension(): MarkedExtension {
  return {
    renderer: {
      link(token: Tokens.Link): string {
        let label = '';
        try {
          label = this.parser.parseInline(token.tokens);
        } catch {
          label = escapeHtml(token.text ?? '');
        }
        const link = classifyMarkdownLink(token.href);

        if (link.kind === 'workspace-file') {
          const path = escapeHtml(link.path ?? '');
          return (
            `<button type="button" class="${WORKSPACE_FILE_LINK_CLASS}" ` +
            `${WORKSPACE_FILE_PATH_ATTR}="${path}" title="${path}">` +
            `${label.trim() ? label : path}</button>`
          );
        }

        if (link.kind === 'external' || link.kind === 'plain') {
          const href = escapeHtml(link.href ?? '');
          const target =
            link.kind === 'external' ? ' target="_blank" rel="noopener noreferrer"' : '';
          const title = token.title ? ` title="${escapeHtml(token.title)}"` : '';
          return `<a href="${href}"${target}${title}>${label.trim() ? label : href}</a>`;
        }

        // Inert: the words survive, the target does not.
        return label;
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
