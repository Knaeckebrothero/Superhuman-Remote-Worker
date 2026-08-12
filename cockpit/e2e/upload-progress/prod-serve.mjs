/**
 * Serves the **production** Cockpit bundle on a loopback origin and proxies
 * `/api`, `/auth` and `/ws` straight through to a live SRW backend.
 *
 * Why this exists — the upload-progress gate needs three things at once that
 * no other harness in this repo gives it together:
 *
 *   1. **A production build.** `provideServiceWorker(..., {enabled:
 *      !isDevMode()})` (app.config.ts) means ngsw only registers in a prod
 *      build, and the k3d cockpit pod runs `ng serve` — a dev build with no
 *      service worker at all. A dev-server run therefore proves nothing about
 *      the SW × upload-progress interaction.
 *   2. **A real, activated service worker.** A SW that answers with
 *      `respondWith()` destroys XHR upload progress outright
 *      (angular/angular#24683, #24716). `ngsw-bypass` is the only opt-out, and
 *      whether it holds can only be observed with the SW actually controlling
 *      the page.
 *   3. **A real backend socket.** Upload progress is a property of bytes
 *      leaving the browser. Any stub, route interception or mock backend
 *      re-introduces exactly the blindness this gate exists to remove.
 *
 * `http://localhost` is a trustworthy origin, so the SW registers here without
 * TLS. Everything the page talks to is same-origin, so no CORS/SameSite
 * question can confound the result.
 *
 * The `srw_session` cookie is held server-side here (pushed in by the spec
 * after it drives a real Keycloak login upstream) and injected on proxied
 * requests. That keeps the browser out of the cross-origin cookie business;
 * auth fidelity is not what this gate measures.
 */
import {createReadStream, existsSync, readFileSync, statSync} from 'node:fs';
import {createServer} from 'node:http';
import {request as httpsRequest, Agent as HttpsAgent} from 'node:https';
import {connect as tlsConnect} from 'node:tls';
import {extname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const COCKPIT_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const DIST_ROOT = resolve(COCKPIT_ROOT, 'dist/cockpit/browser');
const HOST = process.env['UPLOAD_E2E_HOST'] || 'localhost';
const PORT = Number.parseInt(process.env['UPLOAD_E2E_PORT'] || '4174', 10);
const SELF_ORIGIN = `http://${HOST}:${PORT}`;

/** Live backend. `https://localhost` is the k3d traefik ingress. */
const UPSTREAM = process.env['UPLOAD_E2E_UPSTREAM'] || 'https://localhost';
const upstreamUrl = new URL(UPSTREAM);
const UPSTREAM_HOST = upstreamUrl.hostname;
const UPSTREAM_PORT = Number.parseInt(upstreamUrl.port || '443', 10);
const PROXY_PREFIXES = ['/api', '/auth', '/ws'];

if (!Number.isInteger(PORT) || PORT < 1 || PORT > 65535) {
  throw new Error('UPLOAD_E2E_PORT must be a valid TCP port');
}
if (!existsSync(resolve(DIST_ROOT, 'index.html'))) {
  throw new Error(
    `Cockpit production output is missing at ${DIST_ROOT}. Run \`npm run build\` first.`,
  );
}
// A dev build never emits these, so their presence is what distinguishes the
// bundle under test from the one `ng serve` hands the k3d pod.
for (const required of ['ngsw-worker.js', 'ngsw.json']) {
  if (!existsSync(resolve(DIST_ROOT, required))) {
    throw new Error(
      `${required} is missing from ${DIST_ROOT}: this is not a service-worker-enabled production build.`,
    );
  }
}

/** Injected on proxied requests; set by the spec via POST /__e2e/session. */
let sessionCookie = process.env['UPLOAD_E2E_SESSION_COOKIE'] || '';

const MIME_TYPES = new Map([
  ['.css', 'text/css; charset=utf-8'],
  ['.html', 'text/html; charset=utf-8'],
  ['.ico', 'image/x-icon'],
  ['.js', 'text/javascript; charset=utf-8'],
  ['.json', 'application/json; charset=utf-8'],
  ['.map', 'application/json; charset=utf-8'],
  ['.png', 'image/png'],
  ['.svg', 'image/svg+xml'],
  ['.ttf', 'font/ttf'],
  ['.webmanifest', 'application/manifest+json'],
  ['.woff', 'font/woff'],
  ['.woff2', 'font/woff2'],
]);

const agent = new HttpsAgent({rejectUnauthorized: false, keepAlive: true});

/**
 * Same-origin `apiUrl`, so every request the SPA makes is a plain same-origin
 * request this server proxies. The rest mirrors the k3d ConfigMap's env.js:
 * the nulls keep dark-shipped features dark.
 */
function envJs() {
  return (
    `(function(window){window['env']=window['env']||{};` +
    `window['env']['apiUrl']=${JSON.stringify(`${SELF_ORIGIN}/api`)};` +
    `window['env']['canvasViewerHostSuffix']=null;` +
    `window['env']['canvasOfficeOrigin']=null;` +
    `})(this);`
  );
}

function json(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    'Cache-Control': 'no-store',
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(payload),
  });
  res.end(payload);
}

async function readJson(req) {
  const chunks = [];
  let length = 0;
  for await (const chunk of req) {
    length += chunk.length;
    if (length > 64 * 1024) throw new Error('control request body too large');
    chunks.push(chunk);
  }
  if (length === 0) return null;
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

function isProxied(pathname) {
  return PROXY_PREFIXES.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

/** Upstream request headers: same-origin illusion, cookie injected. */
function upstreamHeaders(req) {
  const headers = {...req.headers};
  headers.host = UPSTREAM_HOST;
  // The orchestrator's CSRF middleware pairs `X-CSRF: 1` (set by
  // auth.interceptor) with a non-cross-site Sec-Fetch-Site, which the browser
  // already reports as same-origin because the page and the API share this
  // origin. Origin/Referer are rewritten so nothing upstream sees :4174.
  if (headers.origin) headers.origin = UPSTREAM;
  if (typeof headers.referer === 'string') {
    headers.referer = headers.referer.replace(SELF_ORIGIN, UPSTREAM);
  }
  delete headers['accept-encoding']; // keep bodies untransformed end to end
  const existing = typeof headers.cookie === 'string' ? headers.cookie : '';
  if (sessionCookie && !/(^|;\s*)srw_session=/.test(existing)) {
    headers.cookie = existing ? `${existing}; srw_session=${sessionCookie}` : `srw_session=${sessionCookie}`;
  }
  return headers;
}

/** Make upstream cookies/redirects usable on this plain-HTTP loopback origin. */
function rewriteResponseHeaders(headers) {
  const out = {...headers};
  if (out['set-cookie']) {
    out['set-cookie'] = out['set-cookie'].map((c) =>
      c
        .replace(/;\s*Secure/gi, '')
        .replace(/;\s*Domain=[^;]*/gi, ''),
    );
  }
  if (typeof out.location === 'string' && out.location.startsWith(UPSTREAM)) {
    out.location = out.location.replace(UPSTREAM, SELF_ORIGIN);
  }
  return out;
}

function proxy(req, res) {
  const upstream = httpsRequest(
    {
      agent,
      host: UPSTREAM_HOST,
      port: UPSTREAM_PORT,
      servername: UPSTREAM_HOST,
      method: req.method,
      path: req.url,
      headers: upstreamHeaders(req),
      rejectUnauthorized: false,
    },
    (upRes) => {
      res.writeHead(upRes.statusCode || 502, rewriteResponseHeaders(upRes.headers));
      upRes.pipe(res);
    },
  );
  upstream.on('error', (error) => {
    if (!res.headersSent) json(res, 502, {detail: `upstream: ${error.message}`});
    else res.destroy();
  });
  req.on('aborted', () => upstream.destroy());
  // Never buffer: the browser's UploadProgress reflects bytes accepted by this
  // socket, and a buffering hop here would flatten exactly what we measure.
  req.pipe(upstream);
}

const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url || '/', SELF_ORIGIN);
    const pathname = url.pathname;

    if (pathname === '/__e2e/health') {
      return json(res, 200, {ok: true, upstream: UPSTREAM, hasSession: !!sessionCookie});
    }
    if (pathname === '/__e2e/session' && req.method === 'POST') {
      const body = await readJson(req);
      sessionCookie = typeof body?.cookie === 'string' ? body.cookie : '';
      return json(res, 200, {ok: true, hasSession: !!sessionCookie});
    }

    if (isProxied(pathname)) return proxy(req, res);

    if (pathname === '/assets/env.js') {
      const body = envJs();
      res.writeHead(200, {
        'Cache-Control': 'no-store',
        'Content-Type': 'text/javascript; charset=utf-8',
        'Content-Length': Buffer.byteLength(body),
      });
      return res.end(body);
    }

    return serveStatic(req, res, pathname);
  } catch (error) {
    const message = error instanceof Error ? error.message : 'fixture failure';
    json(res, 500, {detail: message});
  }
});

// The chat's control channel is a WebSocket on /ws.
server.on('upgrade', (req, socket, head) => {
  const pathname = new URL(req.url || '/', SELF_ORIGIN).pathname;
  if (!isProxied(pathname)) return socket.destroy();
  const upstreamSocket = tlsConnect(
    {host: UPSTREAM_HOST, port: UPSTREAM_PORT, servername: UPSTREAM_HOST, rejectUnauthorized: false},
    () => {
      const headers = upstreamHeaders(req);
      const lines = Object.entries(headers)
        .flatMap(([k, v]) => (Array.isArray(v) ? v.map((x) => `${k}: ${x}`) : [`${k}: ${v}`]))
        .join('\r\n');
      upstreamSocket.write(`${req.method} ${req.url} HTTP/1.1\r\n${lines}\r\n\r\n`);
      if (head?.length) upstreamSocket.write(head);
      upstreamSocket.pipe(socket);
      socket.pipe(upstreamSocket);
    },
  );
  upstreamSocket.on('error', () => socket.destroy());
  socket.on('error', () => upstreamSocket.destroy());
});

function serveStatic(req, res, pathname) {
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    return json(res, 405, {detail: 'method not allowed'});
  }
  let relativePath;
  try {
    relativePath = decodeURIComponent(pathname).replace(/^\/+/, '');
  } catch {
    return json(res, 400, {detail: 'invalid path'});
  }
  let filePath = resolve(DIST_ROOT, relativePath || 'index.html');
  if (!filePath.startsWith(`${DIST_ROOT}/`) && filePath !== DIST_ROOT) {
    return json(res, 403, {detail: 'path escapes production output'});
  }
  try {
    if (statSync(filePath).isDirectory()) filePath = resolve(filePath, 'index.html');
  } catch {
    filePath = resolve(DIST_ROOT, 'index.html'); // SPA deep links
  }

  let info;
  try {
    info = statSync(filePath);
  } catch {
    return json(res, 404, {detail: 'not found'});
  }
  if (!info.isFile()) return json(res, 404, {detail: 'not found'});

  res.writeHead(200, {
    // no-store everywhere: ngsw does its own caching, and a stale 304 from
    // this server would make one run's bundle leak into the next.
    'Cache-Control': 'no-store',
    'Content-Length': info.size,
    'Content-Type': MIME_TYPES.get(extname(filePath)) || 'application/octet-stream',
  });
  if (req.method === 'HEAD') return res.end();
  createReadStream(filePath).pipe(res);
}

function close() {
  server.close(() => process.exit(0));
}
process.on('SIGINT', close);
process.on('SIGTERM', close);

server.listen(PORT, HOST, () => {
  const ngsw = JSON.parse(readFileSync(resolve(DIST_ROOT, 'ngsw.json'), 'utf8'));
  process.stdout.write(
    `Cockpit production bundle on ${SELF_ORIGIN} -> ${UPSTREAM} ` +
      `(ngsw hash table: ${Object.keys(ngsw.hashTable || {}).length} files)\n`,
  );
});
