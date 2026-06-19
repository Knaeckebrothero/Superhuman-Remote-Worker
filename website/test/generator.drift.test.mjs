// Drift-gate: proves the generator's output renders against the REAL chart and
// that the Secret skeleton carries every key the rendered manifests require.
// Needs `helm` + `kubeconform` on PATH (see the plan's Prerequisites).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { generate } from '../generator.mjs';

const CHART = join(fileURLToPath(new URL('../../', import.meta.url)), 'helm');

// One representative input set per profile, each supplying the fields the chart's
// required/fail guards demand so it renders. Production profiles use the
// recommended external-services config.
const CASES = {
  evaluation: {
    licenseAccepted: true, domain: 'eval.example.com',
    ingressClass: 'traefik', tlsEnabled: false, neo4jEnabled: false,
  },
  production: {
    licenseAccepted: true, domain: 'srw.example.com',
    ingressClass: 'nginx', tlsEnabled: true, tlsIssuer: 'letsencrypt-prod', neo4jEnabled: false,
    postgres: 'external', postgresHost: 'pg.example.com', postgresPort: 5432, postgresDb: 'srw',
    vector: 'external', vectorHost: 'pg.example.com', vectorPort: 5432, vectorDb: 'srw_vector',
    mongodb: 'external', mongodbUrl: 'mongodb://mongo.example.com:27017/srw_logs',
    oidc: 'external', oidcIssuerUrl: 'https://login.example.com/realms/x', cockpitClientId: 'srw-cockpit',
    git: 'external', gitUrl: 'https://git.example.com',
    cloud: 'external', cloudBackend: 'nextcloud', cloudUrl: 'https://cloud.example.com',
  },
};
CASES['production-vms'] = {
  ...CASES.production,
  vm: { transport: 'http', namespace: 'agent-vms', storageClass: 'longhorn', diskSize: '30Gi', sshPublicKey: 'ssh-ed25519 AAAA test@srw' },
};

function renderToFile(profile, inputs) {
  const dir = mkdtempSync(join(tmpdir(), 'srw-gen-'));
  const vf = join(dir, 'values.yaml');
  writeFileSync(vf, generate(profile, inputs).valuesYaml);
  // helm template enforces values.schema.json + the chart's required/fail guards.
  const rendered = execFileSync('helm', ['template', 'srw', CHART, '-f', vf],
    { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
  const rf = join(dir, 'rendered.yaml');
  writeFileSync(rf, rendered);
  return { rendered, renderedFile: rf };
}

// Non-optional secret keys the rendered manifests reference (the set the operator
// MUST provide, or pods crash-loop).
function requiredSecretKeys(rendered) {
  const keys = new Set();
  const lines = rendered.split('\n');
  for (let i = 0; i < lines.length; i++) {
    if (!/secretKeyRef:/.test(lines[i])) continue;
    let key = null, optional = false;
    for (let j = i + 1; j < lines.length && /^\s/.test(lines[j]) && lines[j].trim(); j++) {
      const k = lines[j].match(/^\s*key:\s*(\S+)/);
      if (k) key = k[1].replace(/['"]/g, '');
      if (/^\s*optional:\s*true/.test(lines[j])) optional = true;
      if (/^\s*\S+:/.test(lines[j]) && !/^\s*(name|key|optional):/.test(lines[j])) break;
    }
    if (key && !optional) keys.add(key);
  }
  return keys;
}

const KUBECONFORM = ['-kubernetes-version', '1.28.0', '-ignore-missing-schemas',
  '-schema-location', 'default',
  '-schema-location', 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json',
  '-summary'];

for (const profile of Object.keys(CASES)) {
  test(`drift-gate: ${profile} renders + kubeconform-validates`, () => {
    const { renderedFile } = renderToFile(profile, CASES[profile]); // throws on guard/schema failure
    execFileSync('kubeconform', [...KUBECONFORM, renderedFile], { encoding: 'utf8' });
  });
}

// Secret-completeness: only the operator-owned-Secret profiles. Evaluation is
// chart-created (the chart fills every key itself), so it has no skeleton to check.
// Scope: the recommended external-services config; flipping a service to bundled
// may add keys the chart will report at template time — out of scope for the
// curated skeleton, which targets the recommended path.
for (const profile of ['production', 'production-vms']) {
  test(`drift-gate: ${profile} secret skeleton has every required key`, () => {
    const { rendered } = renderToFile(profile, CASES[profile]);
    const { secretSkeleton } = generate(profile, CASES[profile]);
    const missing = [...requiredSecretKeys(rendered)].filter((k) => !secretSkeleton.includes(k));
    assert.deepEqual(missing, [], `skeleton missing required keys: ${missing.join(', ')}`);
  });
}
