#!/usr/bin/env node
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const i18nDir = resolve(here, '..', 'src', 'assets', 'i18n');
const BASE_LOCALE = 'en';
const TARGET_LOCALES = ['de-DE'];

function flatten(obj, prefix = '', out = new Map()) {
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      flatten(v, key, out);
    } else {
      out.set(key, typeof v);
    }
  }
  return out;
}

function load(locale) {
  const path = resolve(i18nDir, `${locale}.json`);
  return flatten(JSON.parse(readFileSync(path, 'utf8')));
}

const base = load(BASE_LOCALE);
let failed = false;

for (const locale of TARGET_LOCALES) {
  const target = load(locale);
  const missing = [...base.keys()].filter((k) => !target.has(k));
  const extra = [...target.keys()].filter((k) => !base.has(k));
  const typeMismatch = [...base.keys()]
    .filter((k) => target.has(k) && target.get(k) !== base.get(k))
    .map((k) => `${k} (${BASE_LOCALE}: ${base.get(k)}, ${locale}: ${target.get(k)})`);

  if (missing.length || extra.length || typeMismatch.length) {
    failed = true;
    console.error(
      `\n✗ ${locale} vs ${BASE_LOCALE}: ${missing.length} missing, ${extra.length} extra, ${typeMismatch.length} type mismatch`,
    );
    if (missing.length) {
      console.error(`  Missing in ${locale}.json (present in ${BASE_LOCALE}.json):`);
      for (const k of missing) console.error(`    - ${k}`);
    }
    if (extra.length) {
      console.error(`  Extra in ${locale}.json (not in ${BASE_LOCALE}.json):`);
      for (const k of extra) console.error(`    + ${k}`);
    }
    if (typeMismatch.length) {
      console.error(`  Type mismatch:`);
      for (const k of typeMismatch) console.error(`    ! ${k}`);
    }
  } else {
    console.log(`✓ ${locale} matches ${BASE_LOCALE} (${base.size} keys)`);
  }
}

process.exit(failed ? 1 : 0);
