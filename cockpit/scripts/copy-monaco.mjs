// Copy Monaco's prebuilt AMD bundle into public/monaco/vs so Angular's
// build serves it verbatim (no hashing, no esbuild processing) at
// /monaco/vs/. The @monaco-editor/loader picks it up via the
// `paths.vs` config in JobDiffReviewComponent.
//
// Why this exists: Angular's `outputHashing: all` rewrites `define()`
// module IDs inside any .js file copied through the `assets` config,
// which corrupts Monaco's AMD module graph. The public/ folder is
// copied untouched, so it's the right home for prebundled deps.

import { cp, mkdir, stat } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(__dirname, '..', 'node_modules', 'monaco-editor', 'min', 'vs');
const DEST = resolve(__dirname, '..', 'public', 'monaco', 'vs');

async function main() {
  if (!existsSync(SRC)) {
    console.error(
      '[copy-monaco] Monaco not installed at', SRC,
      '\nRun `npm install` first.',
    );
    process.exit(1);
  }
  if (existsSync(DEST)) {
    // Idempotent: only re-copy if the source has been updated. Cheap
    // mtime compare against the dest root.
    const [srcStat, destStat] = await Promise.all([stat(SRC), stat(DEST)]);
    if (destStat.mtimeMs >= srcStat.mtimeMs) {
      return;
    }
  }
  await mkdir(dirname(DEST), { recursive: true });
  await cp(SRC, DEST, { recursive: true });
  console.log('[copy-monaco] Copied Monaco assets to', DEST);
}

main().catch((err) => {
  console.error('[copy-monaco] failed:', err);
  process.exit(1);
});
