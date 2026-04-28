#!/usr/bin/env node
/**
 * Flag hardcoded user-facing English strings in user-facing code.
 *
 * Patterns flagged:
 *   - toast.{error,success,show,warning,info}('literal', ...)
 *   - alert('literal'), confirm('literal'), prompt('literal')
 *
 * Escape hatch: add `// i18n-exempt` on the same line to silence.
 *
 * Allowlisted paths (admin / non-user-facing):
 *   - src/app/debug/**
 *   - src/app/shared/components/statistics/**
 *   - *.spec.ts, test-setup.ts
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, relative, resolve, sep } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const scanRoot = resolve(root, 'src', 'app');

const ALLOWLIST_DIRS = [
  resolve(scanRoot, 'debug'),
  resolve(scanRoot, 'shared', 'components', 'statistics'),
];

const PATTERNS = [
  {
    name: 'toast',
    // toast.error('...'), toast.success("..."), etc.
    // Allows whitespace and leading `this.` or service-alias qualifiers.
    re: /\btoast\.(error|success|show|warning|info)\s*\(\s*(['"])([^'"]+)\2/g,
  },
  {
    name: 'dialog',
    // alert('...'), confirm("..."), prompt('...').
    // Only matches when called as a top-level function (avoids matching
    // property accesses like `something.confirm(` which are app-defined).
    re: /(?<![.\w])(alert|confirm|prompt)\s*\(\s*(['"])([^'"]+)\2/g,
  },
];

function isAllowlisted(absPath) {
  return ALLOWLIST_DIRS.some((dir) => absPath === dir || absPath.startsWith(dir + sep));
}

function walk(dir, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const abs = resolve(dir, entry.name);
    if (entry.isDirectory()) {
      if (isAllowlisted(abs)) continue;
      walk(abs, out);
    } else if (entry.isFile()) {
      if (isAllowlisted(abs)) continue;
      if (/\.spec\.ts$/.test(entry.name)) continue;
      if (entry.name === 'test-setup.ts') continue;
      if (/\.(ts|html)$/.test(entry.name)) out.push(abs);
    }
  }
  return out;
}

function hasExemptionComment(line) {
  return /\/\/\s*i18n-exempt\b/.test(line) || /<!--\s*i18n-exempt\b/.test(line);
}

const violations = [];
for (const absFile of walk(scanRoot)) {
  const text = readFileSync(absFile, 'utf8');
  const lines = text.split('\n');
  for (const { name, re } of PATTERNS) {
    re.lastIndex = 0;
    let match;
    while ((match = re.exec(text)) !== null) {
      // Find the 1-indexed line number of the match.
      const prefix = text.slice(0, match.index);
      const lineNo = prefix.split('\n').length;
      const line = lines[lineNo - 1] ?? '';
      if (hasExemptionComment(line)) continue;
      violations.push({
        file: relative(root, absFile),
        line: lineNo,
        rule: name,
        sample: match[0].replace(/\s+/g, ' ').slice(0, 80),
      });
    }
  }
}

if (violations.length === 0) {
  console.log('✓ No hardcoded user-facing strings found');
  process.exit(0);
}

console.error(`\n✗ Found ${violations.length} hardcoded user-facing string(s):\n`);
for (const v of violations) {
  console.error(`  ${v.file}:${v.line}  [${v.rule}]  ${v.sample}`);
}
console.error(
  `\nUse the 'transloco' pipe/service, or add '// i18n-exempt' on the line if intentional.`,
);
process.exit(1);
