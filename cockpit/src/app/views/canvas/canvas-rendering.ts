import DOMPurify from 'dompurify';
import katex from 'katex';
import {Marked, MarkedExtension, Renderer} from 'marked';
import {CanvasState} from '../../core/models/canvas.model';
import {environment} from '../../core/environment';
import {isCanonicalCanvasUuid} from './canvas-viewer-protocol';

export type CanvasTrustedRenderer =
  | 'markdown'
  | 'text'
  | 'html'
  | 'html-interactive'
  | 'image'
  | 'office'
  | 'app'
  | 'browser'
  | 'unsupported';

const MARKDOWN_TAGS = [
  'a', 'annotation', 'blockquote', 'br', 'code', 'del', 'div', 'em', 'h1', 'h2',
  'h3', 'h4', 'h5', 'h6', 'hr', 'li', 'math', 'mfrac', 'mi', 'mn', 'mo', 'mover',
  'mpadded', 'mroot', 'mrow', 'mspace', 'msqrt', 'mstyle', 'msub', 'msubsup', 'msup',
  'mtable', 'mtd', 'mtext', 'mtr', 'munder', 'munderover', 'ol', 'p', 'pre',
  'semantics', 'span', 'strong', 'table', 'tbody', 'td', 'th', 'thead', 'tr', 'ul',
];

const MARKDOWN_ATTRIBUTES = [
  'aria-hidden', 'aria-label', 'class', 'data-canvas-external', 'href', 'role',
  'style',
];

const STATIC_HTML_TAGS = [
  'a', 'abbr', 'article', 'aside', 'b', 'blockquote', 'body', 'br', 'caption', 'code',
  'col', 'colgroup', 'dd', 'del', 'details', 'dfn', 'div', 'dl', 'dt', 'em',
  'figcaption', 'figure', 'footer', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header',
  'hr', 'i', 'img', 'ins', 'kbd', 'li', 'main', 'mark', 'nav', 'ol', 'p', 'pre',
  's', 'samp', 'section', 'small', 'span', 'strong', 'sub', 'summary',
  'sup', 'table', 'tbody', 'td', 'tfoot', 'th', 'thead', 'time', 'tr', 'u', 'ul',
  'var',
];

const STATIC_HTML_ATTRIBUTES = [
  'alt', 'aria-label', 'aria-labelledby', 'aria-describedby', 'aria-hidden', 'class',
  'colspan', 'datetime', 'height', 'href', 'id', 'open', 'role', 'rowspan', 'scope',
  'src', 'style', 'title', 'width',
];

export const STATIC_HTML_CSP =
  "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; " +
  "img-src 'none'; font-src 'none'; connect-src 'none'; form-action 'none'; " +
  "base-uri 'none'; object-src 'none';";

export const INTERACTIVE_HTML_CSP =
  "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; " +
  "img-src data:; font-src data:; media-src data:; connect-src 'none'; " +
  "form-action 'none'; base-uri 'none'; object-src 'none'; child-src 'none'; " +
  "frame-src 'none'; webrtc 'block';";

const CANVAS_FRAME_WRAPPER_CSP =
  "default-src 'none'; style-src 'unsafe-inline'; frame-src blob:; child-src blob:;";

export const CANVAS_MATH_MAX_EXPRESSIONS = 256;
export const CANVAS_MATH_MAX_EXPRESSION_CHARS = 4_096;
export const CANVAS_MATH_MAX_TOTAL_CHARS = 32_768;
export const CANVAS_MARKDOWN_MAX_TOKENS = 10_000;
export const CANVAS_MARKDOWN_MAX_LINES = 10_000;
export const CANVAS_MARKDOWN_MAX_SYNTAX_MARKERS = 40_000;
export const CANVAS_MARKDOWN_MAX_SOURCE_CHARS = 2 * 1024 * 1024;
export const CANVAS_RENDER_MAX_NODES = 20_000;
export const CANVAS_RENDER_MAX_OUTPUT_CHARS = 4 * 1024 * 1024;
export const CANVAS_STATIC_MAX_STYLE_ATTRIBUTE_CHARS = 2_048;
export const CANVAS_STATIC_MAX_TOTAL_STYLE_CHARS = 64 * 1024;

const STATIC_HTML_STYLE_PROPERTIES = new Set([
  'align-content', 'align-items', 'align-self',
  'background-color',
  'border', 'border-bottom', 'border-bottom-color', 'border-bottom-style',
  'border-bottom-width', 'border-collapse', 'border-color', 'border-left',
  'border-left-color', 'border-left-style', 'border-left-width', 'border-radius',
  'border-right', 'border-right-color', 'border-right-style', 'border-right-width',
  'border-spacing', 'border-style', 'border-top', 'border-top-color',
  'border-top-style', 'border-top-width', 'border-width',
  'box-sizing',
  'color', 'column-gap',
  'display',
  'flex', 'flex-basis', 'flex-direction', 'flex-grow', 'flex-shrink', 'flex-wrap',
  'font-family', 'font-size', 'font-style', 'font-weight',
  'gap', 'grid-auto-flow', 'grid-column', 'grid-row', 'grid-template-columns',
  'grid-template-rows',
  'height',
  'justify-content', 'justify-items', 'justify-self',
  'letter-spacing', 'line-height', 'list-style-type',
  'margin', 'margin-bottom', 'margin-left', 'margin-right', 'margin-top',
  'max-height', 'max-width', 'min-height', 'min-width',
  'opacity', 'order', 'overflow', 'overflow-wrap', 'overflow-x', 'overflow-y',
  'padding', 'padding-bottom', 'padding-left', 'padding-right', 'padding-top',
  'place-content', 'place-items', 'place-self',
  'row-gap',
  'text-align', 'text-decoration', 'text-transform',
  'vertical-align',
  'white-space', 'width', 'word-break',
]);

export type CanvasRenderErrorCode = 'canvas_content_too_complex';

export interface CanvasRenderResult {
  html: string;
  errorCode: CanvasRenderErrorCode | null;
}

const CANVAS_CONTENT_TOO_COMPLEX: CanvasRenderResult = Object.freeze({
  html: '',
  errorCode: 'canvas_content_too_complex',
});

/** Resolve only the server-issued, versioned file URL on the configured API origin. */
export function resolveCanvasContentUrl(
  contentUrl: string | null | undefined,
  apiUrl = environment.apiUrl,
  documentBase = typeof document === 'undefined' ? 'http://canvas.invalid/' : document.baseURI,
): string | null {
  if (!contentUrl || !contentUrl.startsWith('/') || contentUrl.startsWith('//')) return null;
  if (/\\|[\u0000-\u001f\u007f]/.test(contentUrl)) return null;

  // Canonical content routes never encode path separators. Reject nested
  // encodings as well so a proxy/browser disagreement cannot change the host.
  let decodedPath = contentUrl.split(/[?#]/, 1)[0];
  for (let i = 0; i < 3; i++) {
    if (/%(?:2f|5c)/i.test(decodedPath)) return null;
    try {
      const next = decodeURIComponent(decodedPath);
      if (next === decodedPath) break;
      decodedPath = next;
    } catch {
      return null;
    }
    if (/\\|^\/\//.test(decodedPath)) return null;
  }

  try {
    const api = new URL(apiUrl, documentBase);
    const resolved = new URL(contentUrl, api.origin);
    if (resolved.origin !== api.origin || resolved.username || resolved.password) return null;
    if (resolved.protocol !== 'http:' && resolved.protocol !== 'https:') return null;
    if (resolved.hash) return null;
    if (!/^\/api\/persistent\/threads\/[^/]+\/canvases\/main\/content$/.test(resolved.pathname)) {
      return null;
    }

    // These values are minted and verified by the server. Validation here is
    // deliberately fail-closed; Cockpit never fills in a missing parameter.
    const query = resolved.searchParams;
    const allowedQueryKeys = new Set([
      'ngsw-bypass',
      'presentation_revision',
      'source_fingerprint',
      'source_version',
    ]);
    if (
      query.get('ngsw-bypass') !== 'true' ||
      !/^\d+$/.test(query.get('presentation_revision') ?? '') ||
      !(query.get('source_fingerprint') ?? '').trim() ||
      !(query.get('source_version') ?? '').trim() ||
      Array.from(query.keys()).some(key => !allowedQueryKeys.has(key)) ||
      Array.from(allowedQueryKeys).some(key => query.getAll(key).length !== 1)
    ) {
      return null;
    }
    return resolved.href;
  } catch {
    return null;
  }
}

/** The backend normally resolves `auto`; an unknown/new source fails closed. */
/**
 * What the server says this Canvas IS, for labelling only.
 *
 * Distinct from {@link selectCanvasRenderer}, which answers "what can this
 * Cockpit mount right now" and therefore degrades to `'unsupported'` whenever
 * bytes or a runtime capability are missing. Using that answer as a label
 * produced the reported bug: a sleeping workspace rendered a chip reading
 * "File · Unsupported source" over a perfectly valid file path, which reads as
 * a corrupt document rather than a workspace that is simply not running.
 *
 * This never consults capabilities or loaded content, so the label stays true
 * while the stage is empty.
 */
export function declaredCanvasRenderer(state: CanvasState | null): CanvasTrustedRenderer {
  const source = state?.source;
  if (!source) return 'unsupported';
  if (source.type === 'browser') return 'browser';
  if (source.type === 'workspace_app') return 'app';
  if (source.type !== 'workspace_file') return 'unsupported';
  switch (state?.renderer) {
    case 'markdown':
    case 'text':
    case 'html':
    case 'html-interactive':
    case 'image':
    case 'office':
      return state.renderer;
    default:
      return 'unsupported';
  }
}

export function selectCanvasRenderer(state: CanvasState | null): CanvasTrustedRenderer {
  if (
    state?.source?.type === 'browser' &&
    state.renderer === 'auto' &&
    state.capabilities.can_stream_browser === true
  ) {
    return 'browser';
  }
  if (
    state?.source?.type === 'workspace_app' &&
    state.renderer === 'auto' &&
    state.capabilities.can_create_viewer_session === true
  ) {
    return 'app';
  }
  if (!state?.source || state.source.type !== 'workspace_file') return 'unsupported';
  if (state.renderer === 'office') {
    return state.capabilities.can_view_office === true ? 'office' : 'unsupported';
  }
  switch (state.renderer) {
    case 'markdown':
    case 'text':
    case 'html':
    case 'html-interactive':
    case 'image':
      return state.renderer;
    default:
      return 'unsupported';
  }
}

/**
 * Validate the server-minted, cross-origin viewer bootstrap before it reaches
 * Angular's resource-URL trust boundary. Cockpit never derives this URL from a
 * port, entry path, or origin-generation label.
 */
export function resolveCanvasViewerBootstrapUrl(
  bootstrapUrl: string,
  origin: string,
  attachmentId: string,
  hostSuffix = environment.canvasViewerHostSuffix,
  documentBase = typeof document === 'undefined' ? 'https://cockpit.invalid/' : document.baseURI,
): string | null {
  if (
    !isCanonicalCanvasUuid(attachmentId) ||
    !hostSuffix ||
    !/^\.[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/.test(hostSuffix) ||
    hostSuffix.includes('..') ||
    hostSuffix !== hostSuffix.toLowerCase()
  ) {
    return null;
  }

  try {
    const expected = new URL(origin);
    const bootstrap = new URL(bootstrapUrl);
    const parent = new URL(documentBase);
    if (
      expected.protocol !== 'https:' ||
      expected.port !== '' ||
      expected.origin !== origin ||
      expected.pathname !== '/' ||
      expected.search ||
      expected.hash ||
      expected.username ||
      expected.password ||
      expected.origin === parent.origin
    ) {
      return null;
    }

    const hostname = expected.hostname;
    if (!hostname.endsWith(hostSuffix)) return null;
    const originLabel = hostname.slice(0, -hostSuffix.length);
    if (
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(originLabel)
    ) {
      return null;
    }

    if (
      bootstrap.protocol !== 'https:' ||
      bootstrap.origin !== expected.origin ||
      bootstrap.username ||
      bootstrap.password ||
      bootstrap.hash ||
      bootstrap.pathname !== '/_canvas/bootstrap'
    ) {
      return null;
    }
    const attachmentIds = bootstrap.searchParams.getAll('attachment_id');
    if (
      attachmentIds.length !== 1 ||
      attachmentIds[0] !== attachmentId ||
      bootstrap.search !== `?attachment_id=${attachmentId}` ||
      Array.from(bootstrap.searchParams.keys()).some(key => key !== 'attachment_id')
    ) {
      return null;
    }
    return bootstrap.href;
  } catch {
    return null;
  }
}

export function canvasSourceKey(state: CanvasState | null): string | null {
  if (!state?.source) return null;
  if (state.source.type === 'workspace_file') return `workspace_file:${state.source.path}`;
  if (state.source.type === 'workspace_app') {
    return `workspace_app:${state.source.manifest_path ?? ''}:${state.source.entry_path ?? ''}`;
  }
  if (state.source.type === 'browser') {
    return `browser:${state.presentation_revision}`;
  }
  return state.source.type;
}

/** Keep trusted chrome attached to the source that owns a preserved buffer. */
export function selectCanvasChromeState(
  current: CanvasState | null,
  editSession: CanvasState | null,
  preserveEditSession: boolean,
): CanvasState | null {
  return preserveEditSession && editSession ? editSession : current;
}

export function shouldResetCanvasImageZoom(
  previousSourceKey: string | null,
  currentSourceKey: string,
): boolean {
  return previousSourceKey !== null && previousSourceKey !== currentSourceKey;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

interface CanvasMathBudget {
  expressions: number;
  characters: number;
}

function mathExtension(budget: CanvasMathBudget): MarkedExtension {
  return {
    extensions: [
      {
        name: 'canvasMath',
        level: 'inline',
        start(source: string) {
          const match = source.match(/\$\$|\\\[|\\\(|\$/);
          return match?.index;
        },
        tokenizer(source: string) {
          const display =
            /^\$\$([\s\S]+?)\$\$/.exec(source) ?? /^\\\[([\s\S]+?)\\\]/.exec(source);
          if (display) {
            return {type: 'canvasMath', raw: display[0], body: display[1], display: true};
          }
          const inline = /^\\\(([^\n]+?)\\\)/.exec(source) ?? /^\$([^$\n]+?)\$/.exec(source);
          if (inline) {
            return {type: 'canvasMath', raw: inline[0], body: inline[1], display: false};
          }
          return undefined;
        },
        renderer(token) {
          const math = token as unknown as {body: string; display: boolean};
          const withinBudget =
            math.body.length <= CANVAS_MATH_MAX_EXPRESSION_CHARS &&
            budget.expressions < CANVAS_MATH_MAX_EXPRESSIONS &&
            budget.characters + math.body.length <= CANVAS_MATH_MAX_TOTAL_CHARS;
          if (!withinBudget) {
            return `<code class="canvas-math-fallback">${escapeHtml(math.body)}</code>`;
          }
          budget.expressions++;
          budget.characters += math.body.length;
          try {
            return katex.renderToString(math.body, {
              displayMode: math.display,
              throwOnError: false,
              trust: false,
              strict: 'warn',
              maxExpand: 1_000,
              maxSize: 20,
              output: 'htmlAndMathml',
            });
          } catch {
            return `<code>${escapeHtml(math.body)}</code>`;
          }
        },
      },
    ],
  };
}

/** A Canvas-local parser: it does not inherit chat citations or raw HTML behavior. */
function createCanvasMarked(mathBudget: CanvasMathBudget): Marked {
  const renderer = new Renderer();
  renderer.html = ({text}) => escapeHtml(text);
  renderer.image = ({text}) => {
    const alt = text.trim();
    return alt
      ? `<span class="canvas-image-alt" role="img" aria-label="${escapeHtml(alt)}">${escapeHtml(alt)}</span>`
      : '';
  };
  renderer.link = function ({href, tokens}) {
    const label = this.parser.parseInline(tokens);
    if (href.startsWith('#') && /^#[A-Za-z0-9_.:-]*$/.test(href)) {
      return `<a href="${escapeHtml(href)}">${label}</a>`;
    }
    try {
      const target = new URL(href);
      if (target.protocol === 'http:' || target.protocol === 'https:' || target.protocol === 'mailto:') {
        return `<a data-canvas-external="${escapeHtml(target.href)}">${label}</a>`;
      }
    } catch {
      // Relative workspace links are intentionally inert in Slice 1.
    }
    return `<span class="canvas-inert-link">${label}</span>`;
  };

  const marked = new Marked({gfm: true, breaks: true, renderer});
  marked.use(mathExtension(mathBudget));
  return marked;
}

export function renderCanvasMarkdown(markdown: string): CanvasRenderResult {
  // The budget belongs to one render. A singleton counter would make output
  // depend on previously viewed files; a singleton parser with a resettable
  // counter would be vulnerable to re-entrant renders.
  if (markdownPreflightTooComplex(markdown)) return CANVAS_CONTENT_TOO_COMPLEX;
  const marked = createCanvasMarked({expressions: 0, characters: 0});
  const tokens = marked.lexer(markdown);
  let tokenCount = 0;
  try {
    marked.walkTokens(tokens, () => {
      tokenCount++;
      if (tokenCount > CANVAS_MARKDOWN_MAX_TOKENS) throw CANVAS_CONTENT_TOO_COMPLEX;
    });
  } catch (error) {
    if (error === CANVAS_CONTENT_TOO_COMPLEX) return CANVAS_CONTENT_TOO_COMPLEX;
    throw error;
  }

  const parsed = marked.parser(tokens) as string;
  if (parsed.length > CANVAS_RENDER_MAX_OUTPUT_CHARS) return CANVAS_CONTENT_TOO_COMPLEX;
  const fragment = DOMPurify.sanitize(parsed, {
    ALLOWED_TAGS: MARKDOWN_TAGS,
    ALLOWED_ATTR: MARKDOWN_ATTRIBUTES,
    ALLOW_ARIA_ATTR: true,
    ALLOW_DATA_ATTR: false,
    FORBID_TAGS: ['form', 'iframe', 'img', 'object', 'script', 'style', 'svg'],
    RETURN_DOM_FRAGMENT: true,
    SANITIZE_DOM: true,
    SANITIZE_NAMED_PROPS: true,
  }) as unknown as DocumentFragment;
  return serializeSanitizedFragment(fragment);
}

function markdownPreflightTooComplex(source: string): boolean {
  if (source.length > CANVAS_MARKDOWN_MAX_SOURCE_CHARS) return true;
  let lines = 1;
  let syntaxMarkers = 0;
  for (let index = 0; index < source.length; index++) {
    const char = source.charCodeAt(index);
    if (char === 10) {
      lines++;
      if (lines > CANVAS_MARKDOWN_MAX_LINES) return true;
    }
    // Cheap peak-work guard before Marked allocates its token tree. These are
    // the characters which can repeatedly split block/inline Markdown tokens;
    // ordinary prose is unaffected, while a 2 MiB tiny-list/emphasis/link bomb
    // is rejected during this single allocation-free scan.
    if (
      char === 33 || char === 35 || char === 36 || char === 40 || char === 41 ||
      char === 42 || char === 43 || char === 45 || char === 60 || char === 62 ||
      char === 91 || char === 92 || char === 93 || char === 95 || char === 96 ||
      char === 124 || char === 126
    ) {
      syntaxMarkers++;
      if (syntaxMarkers > CANVAS_MARKDOWN_MAX_SYNTAX_MARKERS) return true;
    }
  }
  return false;
}

function sanitizeInlineStyle(element: Element): number {
  const raw = element.getAttribute('style');
  if (raw === null) return 0;
  if (
    raw.length > CANVAS_STATIC_MAX_STYLE_ATTRIBUTE_CHARS ||
    !(element instanceof HTMLElement)
  ) {
    element.removeAttribute('style');
    return 0;
  }

  const style = element.style;
  for (const property of Array.from(style)) {
    const value = style.getPropertyValue(property);
    if (
      !STATIC_HTML_STYLE_PROPERTIES.has(property) ||
      value.length > 512 ||
      /(?:url|expression|var)\s*\(/i.test(value)
    ) {
      style.removeProperty(property);
    }
  }
  const sanitized = style.cssText;
  if (sanitized) element.setAttribute('style', sanitized);
  else element.removeAttribute('style');
  return sanitized.length;
}

function serializeSanitizedFragment(fragment: DocumentFragment): CanvasRenderResult {
  if (typeof document === 'undefined' || typeof NodeFilter === 'undefined') {
    return CANVAS_CONTENT_TOO_COMPLEX;
  }
  const walker = document.createTreeWalker(
    fragment,
    NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
  );
  let nodes = 0;
  while (walker.nextNode()) {
    nodes++;
    if (nodes > CANVAS_RENDER_MAX_NODES) return CANVAS_CONTENT_TOO_COMPLEX;
  }
  const template = document.createElement('template');
  template.content.append(fragment);
  const html = template.innerHTML;
  if (html.length > CANVAS_RENDER_MAX_OUTPUT_CHARS) return CANVAS_CONTENT_TOO_COMPLEX;
  return {html, errorCode: null};
}

function exceedsStaticHtmlDelimiterBudget(source: string): boolean {
  let delimiters = 0;
  for (let offset = source.indexOf('<'); offset !== -1; offset = source.indexOf('<', offset + 1)) {
    delimiters++;
    if (delimiters > CANVAS_RENDER_MAX_NODES) return true;
  }
  return false;
}

/**
 * Strict static HTML becomes an opaque, script-free Blob document. Sanitization and
 * CSP are both required; neither is treated as a substitute for the other.
 */
export function renderCanvasStaticHtml(source: string): CanvasRenderResult {
  if (
    source.length > CANVAS_RENDER_MAX_OUTPUT_CHARS ||
    exceedsStaticHtmlDelimiterBudget(source)
  ) {
    return CANVAS_CONTENT_TOO_COMPLEX;
  }
  const fragment = DOMPurify.sanitize(source, {
    ALLOWED_TAGS: STATIC_HTML_TAGS,
    ALLOWED_ATTR: STATIC_HTML_ATTRIBUTES,
    ALLOW_ARIA_ATTR: true,
    ALLOW_DATA_ATTR: false,
    FORBID_TAGS: [
      'base', 'button', 'embed', 'form', 'iframe', 'input', 'link', 'meta', 'object',
      'script', 'select', 'style', 'svg', 'textarea', 'video',
    ],
    RETURN_DOM_FRAGMENT: true,
    SANITIZE_DOM: true,
    SANITIZE_NAMED_PROPS: true,
  }) as unknown as DocumentFragment;

  let totalStyleChars = 0;
  for (const element of Array.from(fragment.querySelectorAll('*'))) {
    // DOMPurify already removes on* attributes; the loop is a second explicit
    // boundary for future sanitizer/config changes.
    for (const attribute of Array.from(element.attributes)) {
      const name = attribute.name.toLowerCase();
      if (name.startsWith('on')) element.removeAttribute(attribute.name);
    }

    if (element instanceof HTMLAnchorElement) {
      const href = element.getAttribute('href') ?? '';
      if (!/^#[A-Za-z0-9_.:-]*$/.test(href)) element.removeAttribute('href');
      element.removeAttribute('target');
      element.removeAttribute('download');
    } else {
      element.removeAttribute('href');
    }

    if (element instanceof HTMLImageElement) {
      // Inline compressed raster bytes cannot be dimension-checked here.
      // Present images as their own validated Canvas source instead.
      element.removeAttribute('src');
      element.removeAttribute('srcset');
    } else {
      element.removeAttribute('src');
    }

    totalStyleChars += sanitizeInlineStyle(element);
    if (totalStyleChars > CANVAS_STATIC_MAX_TOTAL_STYLE_CHARS) {
      return CANVAS_CONTENT_TOO_COMPLEX;
    }
  }
  const sanitized = serializeSanitizedFragment(fragment);
  if (sanitized.errorCode) return sanitized;
  const html = wrapStaticHtml(sanitized.html);
  if (html.length > CANVAS_RENDER_MAX_OUTPUT_CHARS) return CANVAS_CONTENT_TOO_COMPLEX;
  return {html, errorCode: null};
}

function wrapStaticHtml(body: string): string {
  return (
    '<!doctype html><html><head>' +
    `<meta http-equiv="Content-Security-Policy" content="${escapeHtml(STATIC_HTML_CSP)}">` +
    '<meta name="referrer" content="no-referrer"></head><body>' +
    body +
    '</body></html>'
  );
}

/**
 * Full-fidelity, self-contained HTML runs under isolation rather than
 * sanitization. The CSP is inserted before every agent-provided head child so
 * the preload scanner cannot start an external request first.
 */
export function renderCanvasInteractiveHtml(source: string): CanvasRenderResult {
  if (
    source.length > CANVAS_RENDER_MAX_OUTPUT_CHARS ||
    typeof DOMParser === 'undefined'
  ) {
    return CANVAS_CONTENT_TOO_COMPLEX;
  }

  const parsed = new DOMParser().parseFromString(source, 'text/html');
  const csp = parsed.createElement('meta');
  csp.httpEquiv = 'Content-Security-Policy';
  csp.content = INTERACTIVE_HTML_CSP;
  const referrer = parsed.createElement('meta');
  referrer.name = 'referrer';
  referrer.content = 'no-referrer';
  parsed.head.insertBefore(referrer, parsed.head.firstChild);
  parsed.head.insertBefore(csp, parsed.head.firstChild);

  const html = `<!doctype html>${parsed.documentElement.outerHTML}`;
  if (html.length > CANVAS_RENDER_MAX_OUTPUT_CHARS) return CANVAS_CONTENT_TOO_COMPLEX;
  return {html, errorCode: null};
}

/**
 * Build the trusted outer document for a Blob-backed Canvas document. The
 * nested frame gives fragment links a real document URL while the outer CSP
 * blocks any attempted navigation away from Blob documents.
 */
export function buildCanvasFrameWrapper(
  contentUrl: string,
  title: string,
  allowScripts: boolean,
): string {
  if (typeof document === 'undefined' || !contentUrl.startsWith('blob:')) return '';

  const wrapper = document.implementation.createHTMLDocument('');
  const csp = wrapper.createElement('meta');
  csp.httpEquiv = 'Content-Security-Policy';
  csp.content = CANVAS_FRAME_WRAPPER_CSP;
  const referrer = wrapper.createElement('meta');
  referrer.name = 'referrer';
  referrer.content = 'no-referrer';
  const style = wrapper.createElement('style');
  style.textContent =
    'html,body{width:100%;height:100%;margin:0;overflow:hidden;background:#fff}' +
    'iframe{display:block;width:100%;height:100%;border:0;background:#fff}';
  wrapper.head.replaceChildren(csp, referrer, style);

  const frame = wrapper.createElement('iframe');
  frame.src = contentUrl;
  frame.title = title.trim() || 'Canvas preview';
  frame.loading = 'lazy';
  frame.referrerPolicy = 'no-referrer';
  frame.setAttribute('sandbox', allowScripts ? 'allow-scripts' : '');
  frame.setAttribute(
    'allow',
    "camera 'none'; microphone 'none'; geolocation 'none'; " +
      "clipboard-read 'none'; clipboard-write 'none'",
  );
  wrapper.body.replaceChildren(frame);
  return `<!doctype html>${wrapper.documentElement.outerHTML}`;
}
