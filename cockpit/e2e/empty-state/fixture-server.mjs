import {readFileSync} from 'node:fs';
import {createServer} from 'node:http';
import {fileURLToPath} from 'node:url';

const COCKPIT_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const SCSS = `${COCKPIT_ROOT}src/app/views/persistent-chat/chat-empty-state/chat-empty-state.component.scss`;
const HOST = process.env['EMPTY_STATE_E2E_HOST'] || '127.0.0.1';
const PORT = Number.parseInt(process.env['EMPTY_STATE_E2E_PORT'] || '4174', 10);

// The component's SCSS is written flat and plain-CSS-compatible on purpose
// (see the plan, Task 1 Step 4), so it is served verbatim. Reading the real
// file is the point: a hand-copied duplicate would silently stop matching.
const componentCss = () => readFileSync(SCSS, 'utf8').replace(':host', '.host');

// Structural rules that live in the PARENT component, reproduced here because
// the fixture has no chat shell. Keep in sync with persistent-chat.component.scss
// .messages (:314), .messages-inner (:367) and .empty-state (:378).
const SHELL_CSS = `
  *{box-sizing:border-box} html,body{margin:0;padding:0;background:#16161e;color:#cdd0e0;
    font-family:ui-sans-serif,system-ui,sans-serif}
  .chat{display:flex;flex-direction:column;height:100vh}
  .chat-header{flex:none;height:56px}
  .composer{flex:none;height:150px}
  .messages{flex:1;overflow-y:auto;overflow-x:hidden;padding:16px;display:flex;
    flex-direction:column;overflow-anchor:none}
  .messages-inner{flex:1;width:100%;max-width:var(--chat-content-width,700px);
    margin-inline:auto;min-width:0;display:flex;flex-direction:column;gap:16px}
  .empty-state{flex:1;display:flex;align-items:center;justify-content:center}
  @media (max-width:600px){.messages{padding:10px}}
`;

// Read the real copy, never a hardcoded copy of it: Task 4 rewrites these
// strings, and chip text length is exactly what drives the overflow this lane
// exists to catch. A fixture with its own strings would stay green while the
// real landing overflowed.
//
// Cap at 4 and take the 4 LONGEST: persistent-chat.component.ts picks 4 of
// this pool AT RANDOM per mount (shuffled.slice(0, Math.min(4, ...))) and
// renders the same picked set for both variants — production never shows
// all of them at once. Rendering the full pool here measured 610/393/93px
// of overflow (phone/short-laptop/desktop) instead of the reported
// ~269/~259/0 and even failed desktop, which the bug report says fits. The
// 4 longest strings are the worst combination any real random draw could
// produce, so a fixture built from them is the only fixed 4-chip subset
// that still holds as a guarantee once the real draw is randomized.
const SUGGESTIONS = `${COCKPIT_ROOT}src/assets/suggestions.json`;
const chips = () =>
  JSON.parse(readFileSync(SUGGESTIONS, 'utf8'))
    .map((s) => s.en)
    .sort((a, b) => b.length - a.length)
    .slice(0, 4);

const page = () => `<!doctype html><html><head><meta charset="utf-8">
<style>${SHELL_CSS}${componentCss()}</style></head><body>
<div class="chat">
  <div class="chat-header"></div>
  <div class="messages" id="messages"><div class="messages-inner"><div class="empty-state">
    <div class="host"><div class="empty-inner">
      <img class="empty-mark" src="/mark.svg" alt="">
      <h2 class="empty-title">What shall we conquer today?</h2>
      <p class="empty-subtitle">Just start typing — your session spins up when you send.</p>
      <div class="draft-connectors"><label class="draft-connectors-toggle">
        <input type="checkbox"><span>Default connectors (1)</span></label></div>
      <div class="suggestion-grid">${chips()
        .map(
          (c) => `<button class="suggestion-chip"><span class="suggestion-icon"></span>
          <span class="suggestion-text">${c}</span></button>`,
        )
        .join('')}</div>
      <a class="draft-advanced" id="advanced">Advanced options</a>
    </div></div>
  </div></div></div>
  <div class="composer"></div>
</div></body></html>`;

const MARK = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 144 144"><rect width="144" height="144" fill="#a01e28"/></svg>';

createServer((req, res) => {
  if (req.url === '/__e2e/health') return res.writeHead(200).end('ok');
  if (req.url === '/mark.svg') {
    res.writeHead(200, {'content-type': 'image/svg+xml'});
    return res.end(MARK);
  }
  res.writeHead(200, {'content-type': 'text/html; charset=utf-8'});
  res.end(page());
}).listen(PORT, HOST, () => console.log(`empty-state fixture on http://${HOST}:${PORT}`));
