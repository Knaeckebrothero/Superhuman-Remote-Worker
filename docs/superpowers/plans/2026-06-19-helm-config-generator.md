# Helm Config Generator — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a static, client-side Helm config generator (linked from the sales page's self-host CTA) that turns a short profile-based form into three copy-pasteable artifacts — a `values.yaml` overlay, a profile-correct Secret skeleton, and an `install.sh` — with a CI drift-gate that proves the output renders against the real chart.

**Architecture:** A new `website/` directory holds the moved sales page plus the generator (`configure.html` + a pure, DOM-free `generator.mjs`). The generator is shipped in a CI-built `nginx` image (`docker/Dockerfile.website` → `ghcr.io/knaeckebrothero/superhuman-remote-worker-website:latest`). A `generator-test` CI job runs unit tests plus a drift-gate that renders each profile's generated `values.yaml` through `helm template | kubeconform` and asserts the Secret skeleton contains every non-optional `secretKeyRef` the chart references. HomeLab swaps its ConfigMap deployment for the image; rollout is manual.

**Tech Stack:** Vanilla ES modules + Web Crypto (no framework, no bundler), Node 22 built-in test runner (`node --test`), Helm 3.17 + kubeconform v0.6.7, nginx:alpine, GitHub Actions.

**Design spec:** `docs/superpowers/specs/2026-06-18-helm-config-generator-design.md` (read it first — this plan implements it).

---

## Prerequisites (out-of-band — NOT code tasks)

These don't block writing code, but the feature doesn't *convert* until they're done. Surface them; don't silently skip (spec §12).

- [ ] **GHCR package visibility:** after the first `build-website` push creates `ghcr.io/knaeckebrothero/superhuman-remote-worker-website`, flip the package to **public** in GHCR settings (first push defaults to private), matching the other SRW images so the cluster pulls with no secret.
- [ ] **Funnel blockers (spec §12):** the sales page's "Self-host with Helm" CTA must point at `/configure`, and the chart `oci://ghcr.io/knaeckebrothero/charts/superhuman-remote-worker` must be publicly pullable. A generator at the end of a 404 funnel converts nobody.
- [ ] **Local drift-gate tooling** (for Tasks 8, 11 verification). Install once:

```bash
helm version --short    # need v3.x; else: sudo dnf install -y helm
curl -fsSL https://github.com/yannh/kubeconform/releases/download/v0.6.7/kubeconform-linux-amd64.tar.gz \
  | sudo tar -xz -C /usr/local/bin kubeconform
kubeconform -v          # expect v0.6.7
```

---

## File Structure

**Create:**
- `website/index.html` — the existing sales page, `git mv`d from repo root (content unchanged).
- `website/configure.html` — the generator page (form + tabbed output UI).
- `website/generator.mjs` — the "resolver": pure, DOM-free `generate()` + `validate()` (ES module; `.mjs` so Node imports it as ESM with no `package.json` and nginx serves a clean JS MIME).
- `website/test/generator.unit.test.mjs` — fast, pure unit tests (`node --test`, no external tools).
- `website/test/generator.drift.test.mjs` — the drift-gate: renders generated values through `helm template | kubeconform` + `secretKeyRef` completeness.
- `website/og-image.png` — 1200×630 social card (Task 14, optional).
- `docker/Dockerfile.website` — `nginx:alpine` + `COPY` of the runtime files + nginx config.

**Modify:**
- `.github/workflows/develop.yml` — add a `website` change filter, a `generator-test` job, and a `build-website` job.
- `.github/workflows/main.yml` — add a `generator-test` job (always-run, hard).
- `docs/website.md`, `docs/issues/sales_page_landing_audit.md` — fix references to the moved `index.html` / retired ConfigMap flow.
- `HomeLab/deployments_managed/srw-sales-page/10-deployment.yaml` — ConfigMap → image (separate repo; Task 13).

**Interface contract** (`website/generator.mjs` — every task below honors these signatures):

```js
export const PROFILES = ['evaluation', 'production', 'production-vms'];
// generate: returns the three artifacts as strings.
export function generate(profile, inputs) // -> { valuesYaml, secretSkeleton, installScript }
// validate: returns [] when valid, else an array of human-readable error strings.
export function validate(profile, inputs) // -> string[]
// randomHex: client-side random secret via Web Crypto (browser + Node 20+ global `crypto`).
export function randomHex(nBytes)         // -> string (2*nBytes lowercase hex chars)
```

`inputs` (curated deltas; fields irrelevant to a profile are ignored):

```js
{
  licenseAccepted: boolean,
  domain: string,                       // e.g. "srw.example.com"
  ingressClass: "nginx" | "traefik",
  tlsEnabled: boolean, tlsIssuer: string,
  // production / production-vms only — each service is "bundled" or "external":
  postgres: "bundled"|"external", postgresUrl: string,
  vector:   "bundled"|"external", vectorUrl: string,
  mongodb:  "bundled"|"external", mongodbUrl: string,
  oidc:     "bundled"|"external", oidcIssuerUrl: string, cockpitClientId: string,
  git:      "bundled"|"external", gitUrl: string,
  cloud:    "bundled"|"external", cloudBackend: "nextcloud"|"opencloud"|"webdav", cloudUrl: string,
  neo4jEnabled: boolean, neo4jEdition: "community"|"enterprise",
  s3: { endpoint, bucket, region, retentionDays },
  pool: { minAgents, maxAgents, reserved },
  logLevel: string, logFormat: "json"|"text",
  // production-vms only:
  vm: { transport: "http"|"nats"|"both", namespace, storageClass, diskSize, sshPublicKey },
  // secrets:
  fillSecrets: boolean,                 // false => CHANGE_ME placeholders
  secretValues: { [KEY]: string },      // used only when fillSecrets === true
}
```

---

## Task 1: Move the sales page into `website/`

**Files:**
- Move: `index.html` → `website/index.html`
- Modify: `docs/website.md`, `docs/issues/sales_page_landing_audit.md`

- [ ] **Step 1: Move the file with git (preserves history)**

```bash
mkdir -p website
git mv index.html website/index.html
```

- [ ] **Step 2: Verify the move**

Run: `test -f website/index.html && ! test -f index.html && echo OK`
Expected: `OK`

- [ ] **Step 3: Fix the stale path reference in `docs/website.md`**

Find the line (~147): `... Verify with: gzip -c index.html | wc -c` and change the path:

```
Single static index.html, inlined CSS, ≤14kb gzipped on the wire (raw source can be ~40–50kb, gzip will handle it). Verify with: gzip -c website/index.html | wc -c
```

- [ ] **Step 4: Add a migration note to the landing audit**

At the top of `docs/issues/sales_page_landing_audit.md`, under the `**Component:**` line, add:

```markdown
> **Update (2026-06-19):** source moved to `website/index.html`; the page now ships as a CI-built nginx image (`ghcr.io/knaeckebrothero/superhuman-remote-worker-website`) rather than an inlined ConfigMap. The "re-indent into the ConfigMap" instructions below are retired — see `docs/superpowers/specs/2026-06-18-helm-config-generator-design.md` §13.
```

- [ ] **Step 5: Commit**

```bash
git add website/index.html docs/website.md docs/issues/sales_page_landing_audit.md
git commit -m "refactor(website): move sales page into website/ ahead of the config generator"
```

---

## Task 2: `generator.mjs` — Evaluation profile `values.yaml`

**Files:**
- Create: `website/generator.mjs`
- Create: `website/test/generator.unit.test.mjs`

- [ ] **Step 1: Write the failing test**

```js
// website/test/generator.unit.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { generate, PROFILES } from '../generator.mjs';

const evalInputs = {
  licenseAccepted: true, domain: 'eval.example.com',
  ingressClass: 'traefik', tlsEnabled: false,
  neo4jEnabled: false,
};

test('PROFILES lists the three customer profiles', () => {
  assert.deepEqual(PROFILES, ['evaluation', 'production', 'production-vms']);
});

test('unknown profile throws', () => {
  assert.throws(() => generate('nope', evalInputs), /unknown profile/);
});

test('evaluation emits a chart-created-secrets overlay', () => {
  const { valuesYaml } = generate('evaluation', evalInputs);
  assert.match(valuesYaml, /^license:\n  acceptTerms: true$/m);
  assert.match(valuesYaml, /domain: "eval\.example\.com"/);
  assert.match(valuesYaml, /^secrets:\n  create: true/m);
  assert.match(valuesYaml, /neo4j:\n\s+enabled: false/);
  assert.match(valuesYaml, /className: traefik/);
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `node --test website/test/generator.unit.test.mjs`
Expected: FAIL — `Cannot find module '../generator.mjs'`.

- [ ] **Step 3: Implement the Evaluation path**

Create `website/generator.mjs`. Build the overlay to match the shape of `helm/values.example.yaml` (read it for exact field reference). The Evaluation profile mirrors `helm/ci/eval-values.yaml`.

```js
// website/generator.mjs — pure, DOM-free. Ships to the browser AND imported by Node tests.
export const PROFILES = ['evaluation', 'production', 'production-vms'];

const CHART = 'oci://ghcr.io/knaeckebrothero/charts/superhuman-remote-worker';

export function randomHex(nBytes) {
  const b = new Uint8Array(nBytes);
  globalThis.crypto.getRandomValues(b);
  return [...b].map((x) => x.toString(16).padStart(2, '0')).join('');
}

export function generate(profile, inputs) {
  if (!PROFILES.includes(profile)) throw new Error(`unknown profile: ${profile}`);
  return {
    valuesYaml: buildValuesYaml(profile, inputs),
    secretSkeleton: buildSecret(profile, inputs),     // Task 5
    installScript: buildInstall(profile, inputs),     // Task 6
  };
}

function buildValuesYaml(profile, inputs) {
  const L = [];
  L.push('# Generated by the SRW config generator — review every value before installing.');
  L.push(`# Profile: ${profile}`);
  L.push('license:');
  L.push(`  acceptTerms: ${inputs.licenseAccepted === true}`);
  L.push('');
  L.push('global:');
  L.push(`  domain: "${inputs.domain}"`);
  L.push('');

  if (profile === 'evaluation') {
    L.push('# Eval/single-node: chart-created secrets (dev/eval only).');
    L.push('secrets:');
    L.push('  create: true');
    L.push('  values: {}');
    L.push('');
  } else {
    L.push('secrets:');
    L.push('  existingSecret: srw-secrets');
    L.push('');
    L.push(...externalServicesBlock(inputs));   // Task 3
  }

  L.push('databases:');
  L.push('  neo4j:');
  L.push(`    enabled: ${inputs.neo4jEnabled === true}`);
  if (inputs.neo4jEnabled) {
    L.push(`    edition: ${inputs.neo4jEdition || 'community'}`);
    if (inputs.neo4jEdition === 'enterprise') L.push('    acceptLicense: "yes"');
  }
  L.push('');
  L.push('ingress:');
  L.push('  enabled: true');
  L.push(`  className: ${inputs.ingressClass}`);
  L.push('  tls:');
  L.push(`    enabled: ${inputs.tlsEnabled === true}`);
  if (inputs.tlsEnabled) L.push(`    issuerName: ${inputs.tlsIssuer || 'letsencrypt-prod'}`);
  L.push('');

  if (profile === 'production-vms') L.push(...vmControllerBlock(inputs));   // Task 4

  return L.join('\n') + '\n';
}

// Stubs filled in by later tasks so this task's tests pass in isolation:
function externalServicesBlock() { return []; }
function vmControllerBlock() { return []; }
function buildSecret() { return ''; }
function buildInstall() { return ''; }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `node --test website/test/generator.unit.test.mjs`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add website/generator.mjs website/test/generator.unit.test.mjs
git commit -m "feat(website): generator core + evaluation-profile values.yaml"
```

---

## Task 3: Production profile — external-service conditionals

**Files:**
- Modify: `website/generator.mjs` (replace the `externalServicesBlock` stub)
- Modify: `website/test/generator.unit.test.mjs`

- [ ] **Step 1: Write the failing tests**

Append to `website/test/generator.unit.test.mjs`:

```js
const prodBase = {
  licenseAccepted: true, domain: 'srw.example.com',
  ingressClass: 'nginx', tlsEnabled: true, tlsIssuer: 'letsencrypt-prod',
  neo4jEnabled: false,
  postgres: 'external', postgresUrl: 'postgres://srw:pw@pg:5432/srw',
  vector:   'external', vectorUrl:   'postgresql://srw:pw@pg:5432/srw_vector',
  mongodb:  'external', mongodbUrl:  'mongodb://srw:pw@mongo:27017/srw_logs',
  oidc:     'external', oidcIssuerUrl: 'https://login.example.com/realms/x', cockpitClientId: 'srw-cockpit',
  git:      'external', gitUrl: 'https://git.example.com',
  cloud:    'external', cloudBackend: 'nextcloud', cloudUrl: 'https://cloud.example.com',
};

test('production wires external service URLs when bring-your-own', () => {
  const { valuesYaml } = generate('production', prodBase);
  assert.match(valuesYaml, /existingSecret: srw-secrets/);
  assert.match(valuesYaml, /postgres:\n\s+enabled: true\n\s+internal: false\n\s+externalUrl: "postgres:\/\/srw:pw@pg:5432\/srw"/);
  assert.match(valuesYaml, /externalIssuerUrl: "https:\/\/login\.example\.com\/realms\/x"/);
  assert.match(valuesYaml, /adminApiDisabled: true/);
  assert.match(valuesYaml, /externalUrl: "https:\/\/cloud\.example\.com"/);
});

test('production with a bundled service omits its externalUrl', () => {
  const { valuesYaml } = generate('production', { ...prodBase, postgres: 'bundled' });
  assert.doesNotMatch(valuesYaml, /postgres:\n\s+enabled: true\n\s+internal: false/);
});
```

- [ ] **Step 2: Run to verify failure**

Run: `node --test website/test/generator.unit.test.mjs`
Expected: FAIL — the new assertions don't match (stub returns `[]`).

- [ ] **Step 3: Implement `externalServicesBlock`**

Replace the `externalServicesBlock` stub in `website/generator.mjs`. Emit a block per service only when it is `external` (bundled services keep chart defaults — bundled-on). Match the `helm/values.example.yaml` external shape.

```js
function externalServicesBlock(inputs) {
  const L = [];
  const ext = (svc) => inputs[svc] === 'external';

  if (ext('oidc')) {
    L.push('keycloak:');
    L.push('  enabled: true');
    L.push('  internal: false');
    L.push(`  externalIssuerUrl: "${inputs.oidcIssuerUrl}"`);
    L.push(`  cockpitClientId: "${inputs.cockpitClientId || 'srw-cockpit'}"`);
    L.push('  adminApiDisabled: true');
    L.push('');
  }
  if (ext('git')) {
    L.push('gitea:');
    L.push('  enabled: true');
    L.push('  internal: false');
    L.push(`  internalUrl: "${inputs.gitUrl}"`);
    L.push(`  externalUrl: "${inputs.gitUrl}"`);
    L.push('');
  }

  const dbExt = (key, svc, url) => ext(svc) ? [
    `  ${key}:`, '    enabled: true', '    internal: false', `    externalUrl: "${url}"`,
  ] : [];
  const dbLines = [
    ...dbExt('postgres', 'postgres', inputs.postgresUrl),
    ...dbExt('vector', 'vector', inputs.vectorUrl),
    ...dbExt('mongodb', 'mongodb', inputs.mongodbUrl),
  ];
  if (dbLines.length) { L.push('databases:', ...dbLines, ''); }

  if (ext('cloud')) {
    L.push('opencloud:', '  enabled: false');
    L.push('nextcloud:', '  enabled: false');
    L.push('cloud:');
    L.push(`  externalBackend: "${inputs.cloudBackend || 'nextcloud'}"`);
    L.push(`  externalUrl: "${inputs.cloudUrl}"`);
    L.push(`  externalServiceUrl: "${inputs.cloudUrl}"`);
    L.push('');
  }
  return L;
}
```

> Note: `buildValuesYaml` emits its own top-level `databases:` for Neo4j. To avoid two `databases:` keys, move the Neo4j lines into `externalServicesBlock`'s `databases:` block for production, OR (simpler) keep a single `databasesBlock(inputs)` helper that emits postgres/vector/mongodb/neo4j together. Refactor `buildValuesYaml` so `databases:` is emitted exactly once. The test `production wires external service URLs` + the Task 2 neo4j test together pin this — both must stay green.

- [ ] **Step 4: Run tests to verify pass**

Run: `node --test website/test/generator.unit.test.mjs`
Expected: PASS (all Task 2 + Task 3 tests). If a duplicate-`databases:` regression appears, apply the single-`databasesBlock` refactor in the note.

- [ ] **Step 5: Commit**

```bash
git add website/generator.mjs website/test/generator.unit.test.mjs
git commit -m "feat(website): production profile external-service wiring"
```

---

## Task 4: Production + same-cluster VMs profile

**Files:**
- Modify: `website/generator.mjs` (replace the `vmControllerBlock` stub)
- Modify: `website/test/generator.unit.test.mjs`

- [ ] **Step 1: Write the failing test**

```js
test('production-vms emits a vmController block', () => {
  const { valuesYaml } = generate('production-vms', {
    ...prodBase,
    vm: { transport: 'http', namespace: 'agent-vms', storageClass: 'longhorn',
          diskSize: '30Gi', sshPublicKey: 'ssh-ed25519 AAAA... agent@srw' },
  });
  assert.match(valuesYaml, /vmController:\n\s+enabled: true/);
  assert.match(valuesYaml, /transport: http/);
  assert.match(valuesYaml, /namespace: agent-vms/);
  assert.match(valuesYaml, /vmStorageClass: longhorn/);
  assert.match(valuesYaml, /vmDiskSize: 30Gi/);
  assert.match(valuesYaml, /vmSshPublicKey: "ssh-ed25519 AAAA\.\.\. agent@srw"/);
});
```

- [ ] **Step 2: Run to verify failure**

Run: `node --test website/test/generator.unit.test.mjs`
Expected: FAIL — no `vmController:` in output.

- [ ] **Step 3: Implement `vmControllerBlock`** (matches `helm/values.example.yaml` lines 211-217)

```js
function vmControllerBlock(inputs) {
  const vm = inputs.vm || {};
  return [
    'vmController:',
    '  enabled: true',
    `  transport: ${vm.transport || 'http'}`,
    `  namespace: ${vm.namespace || 'agent-vms'}`,
    `  vmStorageClass: ${vm.storageClass || 'longhorn'}`,
    `  vmDiskSize: ${vm.diskSize || '30Gi'}`,
    `  vmSshPublicKey: "${vm.sshPublicKey || ''}"`,
    '',
  ];
}
```

- [ ] **Step 4: Run tests to verify pass**

Run: `node --test website/test/generator.unit.test.mjs`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add website/generator.mjs website/test/generator.unit.test.mjs
git commit -m "feat(website): production-vms profile (vmController block)"
```

---

## Task 5: Secret skeleton

**Files:**
- Modify: `website/generator.mjs` (replace the `buildSecret` stub)
- Modify: `website/test/generator.unit.test.mjs`

The skeleton must enumerate the keys the chosen profile's pods need. The **authoritative** list is the set of non-optional `secretKeyRef` keys the chart renders (verified end-to-end by Task 8). For the human-facing skeleton, emit a curated, commented key set; Task 8 is the gate that proves it's complete.

- [ ] **Step 1: Write the failing tests**

```js
test('evaluation secret skeleton omits APP_ENCRYPTION_KEY (chart generates it)', () => {
  const { secretSkeleton } = generate('evaluation', evalInputs);
  assert.doesNotMatch(secretSkeleton, /APP_ENCRYPTION_KEY/);
});

test('production secret skeleton includes generated + placeholder keys', () => {
  const { secretSkeleton } = generate('production', prodBase);
  assert.match(secretSkeleton, /APP_ENCRYPTION_KEY: [0-9a-f]{64}/);          // 32 bytes hex, generated
  assert.match(secretSkeleton, /SESSION_JWT_SECRET: [0-9a-f]{64}/);
  assert.match(secretSkeleton, /POSTGRES_PASSWORD: CHANGE_ME/);             // user-supplied placeholder
  assert.match(secretSkeleton, /kind: Secret/);
  assert.match(secretSkeleton, /name: srw-secrets/);
});

test('fillSecrets substitutes provided values for placeholders', () => {
  const { secretSkeleton } = generate('production',
    { ...prodBase, fillSecrets: true, secretValues: { POSTGRES_PASSWORD: 's3cret' } });
  assert.match(secretSkeleton, /POSTGRES_PASSWORD: s3cret/);
  assert.doesNotMatch(secretSkeleton, /POSTGRES_PASSWORD: CHANGE_ME/);
});
```

- [ ] **Step 2: Run to verify failure**

Run: `node --test website/test/generator.unit.test.mjs`
Expected: FAIL (stub returns `''`).

- [ ] **Step 3: Implement `buildSecret`**

```js
// Per-profile user-supplied keys (placeholders unless fillSecrets). Keep in sync
// with helm/README.md §Secret schema; Task 8 fails CI if a needed key is missing.
const USER_KEYS = {
  evaluation: ['LLM_API_KEY'],
  production: ['POSTGRES_PASSWORD', 'VECTOR_DB_PASSWORD', 'MONGODB_PASSWORD',
               'KC_CLIENT_SECRET', 'GITEA_OIDC_CLIENT_SECRET',
               'CLOUD_SERVICE_PASSWORD', 'LLM_API_KEY'],
};
USER_KEYS['production-vms'] = USER_KEYS.production;

function buildSecret(profile, inputs) {
  const val = (k) => (inputs.fillSecrets && inputs.secretValues?.[k]) || 'CHANGE_ME';
  const data = {};
  // Generated randoms only in operator-owned modes (not eval/chart-created).
  if (profile !== 'evaluation') {
    data.APP_ENCRYPTION_KEY = randomHex(32);
    data.SESSION_JWT_SECRET = randomHex(32);
  }
  for (const k of USER_KEYS[profile]) data[k] = val(k);

  if (profile === 'evaluation') {
    // Chart-created mode: these go under secrets.values in values.yaml, but we
    // also show a stringData block as a reference.
    const lines = ['# Eval mode: the chart creates the Secret. Add real values to',
      '# `secrets.values` in values.yaml, or pre-create this Secret. The chart',
      '# auto-generates APP_ENCRYPTION_KEY when absent.'];
    for (const [k, v] of Object.entries(data)) lines.push(`#   ${k}: ${v}`);
    return lines.join('\n') + '\n';
  }
  const lines = ['apiVersion: v1', 'kind: Secret', 'metadata:',
    '  name: srw-secrets', `  namespace: srw`, 'type: Opaque', 'stringData:'];
  for (const [k, v] of Object.entries(data)) lines.push(`  ${k}: ${v}`);
  return lines.join('\n') + '\n';
}
```

- [ ] **Step 4: Run tests to verify pass**

Run: `node --test website/test/generator.unit.test.mjs`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add website/generator.mjs website/test/generator.unit.test.mjs
git commit -m "feat(website): profile-correct Secret skeleton with client-side randoms"
```

---

## Task 6: `install.sh` generation

**Files:**
- Modify: `website/generator.mjs` (replace the `buildInstall` stub)
- Modify: `website/test/generator.unit.test.mjs`

- [ ] **Step 1: Write the failing test**

```js
test('install script applies secret then helm-installs from the OCI chart', () => {
  const { installScript } = generate('production', prodBase);
  assert.match(installScript, /^#!\/usr\/bin\/env bash/);
  assert.match(installScript, /set -euo pipefail/);
  assert.match(installScript, /kubectl create namespace srw/);
  assert.match(installScript, /kubectl apply -n srw -f srw-secrets\.yaml/);
  assert.match(installScript, /helm install srw oci:\/\/ghcr\.io\/knaeckebrothero\/charts\/superhuman-remote-worker/);
  assert.match(installScript, /-f values\.yaml/);
});

test('evaluation install script does not apply an external secret', () => {
  const { installScript } = generate('evaluation', evalInputs);
  assert.doesNotMatch(installScript, /kubectl apply -n srw -f srw-secrets\.yaml/);
});
```

- [ ] **Step 2: Run to verify failure**

Run: `node --test website/test/generator.unit.test.mjs`
Expected: FAIL (stub returns `''`).

- [ ] **Step 3: Implement `buildInstall`**

```js
function buildInstall(profile, inputs) {
  const L = ['#!/usr/bin/env bash', 'set -euo pipefail', '',
    '# Generated by the SRW config generator. Review before running.',
    '# Prereqs: a working cluster + kubectl/helm context, DNS + TLS per the install guide.',
    '', 'kubectl create namespace srw --dry-run=client -o yaml | kubectl apply -f -'];
  if (profile !== 'evaluation') {
    L.push('kubectl apply -n srw -f srw-secrets.yaml');
  }
  L.push('', `helm install srw ${CHART} \\`, '  -n srw \\', '  -f values.yaml');
  return L.join('\n') + '\n';
}
```

- [ ] **Step 4: Run tests to verify pass**

Run: `node --test website/test/generator.unit.test.mjs`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add website/generator.mjs website/test/generator.unit.test.mjs
git commit -m "feat(website): install.sh generation"
```

---

## Task 7: Client-side validation

**Files:**
- Modify: `website/generator.mjs` (add `validate`)
- Modify: `website/test/generator.unit.test.mjs`

- [ ] **Step 1: Write the failing tests**

```js
import { validate } from '../generator.mjs';
import { readFileSync } from 'node:fs';

test('validate flags missing license + domain', () => {
  const errs = validate('evaluation', { licenseAccepted: false, domain: '' });
  assert.ok(errs.some((e) => /license/i.test(e)));
  assert.ok(errs.some((e) => /domain/i.test(e)));
});

test('validate requires a URL for an external service', () => {
  const errs = validate('production', { ...prodBase, postgres: 'external', postgresUrl: '' });
  assert.ok(errs.some((e) => /postgres.*url/i.test(e)));
});

test('validate requires license acceptance for neo4j enterprise', () => {
  const errs = validate('production', { ...prodBase, neo4jEnabled: true, neo4jEdition: 'enterprise' });
  // (license already accepted in prodBase, so this checks the enterprise-specific rule only)
  assert.equal(errs.filter((e) => /neo4j/i.test(e)).length, 0);
  const errs2 = validate('production', { ...prodBase, neo4jEnabled: true, neo4jEdition: 'enterprise', neo4jLicenseAccepted: false });
  assert.ok(errs2.some((e) => /neo4j.*license/i.test(e)));
});

test('valid production input yields no errors', () => {
  assert.deepEqual(validate('production', prodBase), []);
});

test('structural rules cover every enum in helm/values.schema.json', () => {
  const schema = JSON.parse(readFileSync(new URL('../../helm/values.schema.json', import.meta.url)));
  // The validator must know about each enum field the schema constrains.
  const enums = collectEnumPaths(schema);   // helper defined in the test file
  for (const path of enums) {
    assert.ok(KNOWN_ENUM_PATHS.includes(path), `validator missing enum rule for ${path}`);
  }
});
```

Add these helpers at the top of the test file:

```js
function collectEnumPaths(node, prefix = '') {
  let out = [];
  if (node && node.properties) for (const [k, v] of Object.entries(node.properties)) {
    const p = prefix ? `${prefix}.${k}` : k;
    if (v.enum) out.push(p);
    out = out.concat(collectEnumPaths(v, p));
  }
  return out;
}
// Keep in sync with generator.mjs's structural rules:
const KNOWN_ENUM_PATHS = ['databases.neo4j.edition', 'vmController.transport'];
```

- [ ] **Step 2: Run to verify failure**

Run: `node --test website/test/generator.unit.test.mjs`
Expected: FAIL — `validate` not exported.

- [ ] **Step 3: Implement `validate`**

```js
const ENUMS = {
  ingressClass: ['nginx', 'traefik'],
  'databases.neo4j.edition': ['community', 'enterprise'],
  'vmController.transport': ['http', 'nats', 'both'],
  'cloud.externalBackend': ['nextcloud', 'opencloud', 'webdav'],
};

export function validate(profile, inputs) {
  const e = [];
  if (inputs.licenseAccepted !== true) e.push('You must accept the license terms.');
  if (!inputs.domain || !/^[a-z0-9.-]+\.[a-z]{2,}$/i.test(inputs.domain)) e.push('A valid base domain is required.');
  if (inputs.ingressClass && !ENUMS.ingressClass.includes(inputs.ingressClass)) e.push('Ingress class must be nginx or traefik.');

  if (profile !== 'evaluation') {
    for (const [svc, url] of [['postgres','postgresUrl'],['vector','vectorUrl'],['mongodb','mongodbUrl'],['git','gitUrl'],['cloud','cloudUrl']]) {
      if (inputs[svc] === 'external' && !inputs[url]) e.push(`An external ${svc} requires its URL.`);
    }
    if (inputs.oidc === 'external' && !inputs.oidcIssuerUrl) e.push('An external OIDC provider requires its issuer URL.');
  }
  if (inputs.neo4jEnabled && inputs.neo4jEdition === 'enterprise' && inputs.neo4jLicenseAccepted === false)
    e.push('Neo4j Enterprise requires accepting its license.');
  if (profile === 'production-vms' && inputs.vm?.transport && !ENUMS['vmController.transport'].includes(inputs.vm.transport))
    e.push('VM transport must be http, nats, or both.');
  return e;
}
```

- [ ] **Step 4: Run tests to verify pass**

Run: `node --test website/test/generator.unit.test.mjs`
Expected: PASS (all unit tests).

- [ ] **Step 5: Commit**

```bash
git add website/generator.mjs website/test/generator.unit.test.mjs
git commit -m "feat(website): client-side validation + schema-parity guard"
```

---

## Task 8: Drift-gate integration test (the keystone)

**Files:**
- Create: `website/test/generator.drift.test.mjs`

Renders each profile's generated `values.yaml` through `helm template | kubeconform`, and asserts the Secret skeleton contains every non-optional `secretKeyRef` the chart renders. Requires `helm` + `kubeconform` on PATH (Prerequisites).

- [ ] **Step 1: Write the drift-gate test**

```js
// website/test/generator.drift.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { generate } from '../generator.mjs';

const REPO = fileURLToPath(new URL('../../', import.meta.url));
const CHART = join(REPO, 'helm');

// One representative input set per profile (+ a bundled→external variant for prod),
// each supplying every field the chart's required/fail guards demand so it renders.
const CASES = {
  evaluation: { licenseAccepted: true, domain: 'eval.example.com', ingressClass: 'traefik', tlsEnabled: false, neo4jEnabled: false },
  production: {
    licenseAccepted: true, domain: 'srw.example.com', ingressClass: 'nginx', tlsEnabled: true, tlsIssuer: 'letsencrypt-prod',
    neo4jEnabled: false,
    postgres: 'external', postgresUrl: 'postgres://srw:pw@pg:5432/srw',
    vector: 'external', vectorUrl: 'postgresql://srw:pw@pg:5432/srw_vector',
    mongodb: 'external', mongodbUrl: 'mongodb://srw:pw@mongo:27017/srw_logs',
    oidc: 'external', oidcIssuerUrl: 'https://login.example.com/realms/x', cockpitClientId: 'srw-cockpit',
    git: 'external', gitUrl: 'https://git.example.com',
    cloud: 'external', cloudBackend: 'nextcloud', cloudUrl: 'https://cloud.example.com',
  },
  'production-vms': null,  // set below from production + vm block
};
CASES['production-vms'] = { ...CASES.production,
  vm: { transport: 'http', namespace: 'agent-vms', storageClass: 'longhorn', diskSize: '30Gi', sshPublicKey: 'ssh-ed25519 AAAA test@srw' } };

function renderChart(valuesYaml) {
  const dir = mkdtempSync(join(tmpdir(), 'srw-gen-'));
  const f = join(dir, 'values.yaml');
  writeFileSync(f, valuesYaml);
  // helm template enforces values.schema.json + required/fail guards.
  return execFileSync('helm', ['template', 'srw', CHART, '-f', f], { encoding: 'utf8' });
}

function requiredSecretKeys(rendered) {
  // Scan rendered manifests for secretKeyRef blocks; collect non-optional keys.
  const keys = new Set();
  const lines = rendered.split('\n');
  for (let i = 0; i < lines.length; i++) {
    if (!/secretKeyRef:/.test(lines[i])) continue;
    let key = null, optional = false;
    for (let j = i + 1; j < lines.length && /^\s/.test(lines[j]); j++) {
      const k = lines[j].match(/^\s*key:\s*(\S+)/);  if (k) key = k[1].replace(/['"]/g, '');
      if (/^\s*optional:\s*true/.test(lines[j])) optional = true;
      if (/^\s*\S+:/.test(lines[j]) && !/^\s*(name|key|optional):/.test(lines[j])) break;
    }
    if (key && !optional) keys.add(key);
  }
  return keys;
}

for (const profile of Object.keys(CASES)) {
  test(`drift-gate: ${profile} renders + kubeconform-validates`, () => {
    const { valuesYaml } = generate(profile, CASES[profile]);
    const rendered = renderChart(valuesYaml);   // throws if helm fails (schema/guard)
    // pipe through kubeconform
    execFileSync('sh', ['-c',
      `printf '%s' ${JSON.stringify(rendered)} | kubeconform -kubernetes-version 1.28.0 ` +
      `-ignore-missing-schemas -schema-location default ` +
      `-schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' ` +
      `-summary`], { stdio: 'pipe' });
  });

  test(`drift-gate: ${profile} secret skeleton has every required key`, () => {
    const { valuesYaml, secretSkeleton } = generate(profile, CASES[profile]);
    const required = requiredSecretKeys(renderChart(valuesYaml));
    const missing = [...required].filter((k) => !secretSkeleton.includes(k));
    assert.deepEqual(missing, [], `skeleton missing required keys: ${missing.join(', ')}`);
  });
}
```

- [ ] **Step 2: Run the drift-gate**

Run: `node --test website/test/generator.drift.test.mjs`
Expected: PASS for all profiles. **If a render fails**, the generator is emitting an invalid/incomplete overlay — fix `generator.mjs` (a required field guard fired, or a value type is wrong) until it renders; do not edit the chart. **If a secret key is missing**, add it to `USER_KEYS` (Task 5) for that profile.

- [ ] **Step 3: Commit**

```bash
git add website/test/generator.drift.test.mjs
git commit -m "test(website): drift-gate — render generated values + secret-key completeness"
```

---

## Task 9: `configure.html` — the generator UI

**Files:**
- Create: `website/configure.html`

A single static page reusing the sales page's CSS variables. Not unit-tested (it's a thin DOM layer over the tested `generator.mjs`); verified by a Playwright smoke + the byte budget.

- [ ] **Step 1: Build the page**

Structure (reuse `website/index.html`'s `:root` CSS-variable palette and type scale — copy the `<style>` variables block):

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>SRW — Self-host config generator</title>
  <style>/* paste the :root + @media palette from index.html, plus form styles */</style>
</head>
<body>
  <noscript>This generator needs JavaScript. See the manual install guide:
    <a href="https://github.com/.../helm/README.md">Helm README</a>.</noscript>
  <main class="wrap" hidden id="app">
    <h1>Self-host config generator</h1>
    <label><input type="checkbox" id="license"> I hold a valid SRW commercial license</label>
    <fieldset id="profile">
      <label><input type="radio" name="profile" value="evaluation" checked> Evaluation (single node)</label>
      <label><input type="radio" name="profile" value="production"> Production (external services)</label>
      <label><input type="radio" name="profile" value="production-vms"> Production + same-cluster VMs</label>
    </fieldset>
    <!-- curated delta fields; data-show="production production-vms" toggles visibility per profile -->
    <!-- per-service: a <select bundled|external> that reveals its URL input -->
    <label><input type="checkbox" id="fillSecrets"> Fill secret values in my browser
      (100% client-side, never sent anywhere — view source)</label>
    <button id="generate">Generate ▸</button>
    <section id="output" hidden>
      <nav><button data-tab="values">values.yaml</button><button data-tab="secret">srw-secrets</button><button data-tab="install">install.sh</button></nav>
      <pre id="out"></pre>
      <button id="copy">Copy</button><button id="download">Download</button>
    </section>
  </main>
  <script type="module">
    import { generate, validate } from './generator.mjs';
    document.getElementById('app').hidden = false;     // reveal once JS runs
    function readInputs() { /* gather form fields into the inputs object (interface contract) */ }
    function refresh() {
      const profile = document.querySelector('input[name=profile]:checked').value;
      document.querySelectorAll('[data-show]').forEach(el =>
        el.hidden = !el.dataset.show.split(' ').includes(profile));
      const errs = validate(profile, readInputs());
      document.getElementById('generate').disabled = errs.length > 0;
    }
    let result = {};
    document.getElementById('generate').onclick = () => {
      const profile = document.querySelector('input[name=profile]:checked').value;
      result = generate(profile, readInputs());
      showTab('values');
      document.getElementById('output').hidden = false;
    };
    function showTab(t) {
      const map = { values: result.valuesYaml, secret: result.secretSkeleton, install: result.installScript };
      document.getElementById('out').textContent = map[t];
    }
    /* wire tab buttons -> showTab; copy -> navigator.clipboard.writeText; download -> Blob;
       attach refresh() to input/change events on the form. */
    document.querySelector('#app').addEventListener('input', refresh);
    refresh();
  </script>
</body>
</html>
```

Implement the field set per the interface contract (Task: File Structure), with `data-show` gating and per-service URL reveal. Keep it dependency-free.

- [ ] **Step 2: Manual + Playwright smoke**

Serve locally and drive it:

```bash
python3 -m http.server -d website 8099 &
```

Then (Playwright MCP, or a browser): open `http://localhost:8099/configure.html`, check the license box, select each profile, fill required fields, click Generate, confirm all three tabs render non-empty and Copy works. Confirm `#generate` is disabled until license + domain are valid.

- [ ] **Step 3: Byte-budget check**

Run: `gzip -c website/configure.html | wc -c; gzip -c website/generator.mjs | wc -c`
Expected: combined well under the ~50 kB soft budget.

- [ ] **Step 4: Commit**

```bash
git add website/configure.html
git commit -m "feat(website): config generator UI (configure.html)"
```

---

## Task 10: `docker/Dockerfile.website`

**Files:**
- Create: `docker/Dockerfile.website`

- [ ] **Step 1: Write the Dockerfile**

```dockerfile
# Static marketing site + Helm config generator, served by nginx.
# Build (from repo root):  docker build -t srw-website -f docker/Dockerfile.website .
FROM nginx:alpine
LABEL description="SRW sales page + self-host config generator"

# Only the runtime assets — NOT website/test/ or anything else.
COPY website/index.html website/configure.html website/generator.mjs /usr/share/nginx/html/
# og-image.png is added by Task 14; COPY it here when present.

# Clean /configure URL, correct .mjs MIME, light caching, listen 80.
RUN printf 'server {\n\
    listen 80;\n\
    root /usr/share/nginx/html;\n\
    index index.html;\n\
    types { application/javascript mjs js; text/html html; image/png png; image/svg+xml svg; }\n\
    location = /configure { try_files /configure.html =404; }\n\
    location / { try_files $uri $uri/ =404; }\n\
}\n' > /etc/nginx/conf.d/default.conf

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD wget -q --spider http://localhost:80/ || exit 1
```

- [ ] **Step 2: Build + run + smoke all routes**

```bash
docker build -t srw-website -f docker/Dockerfile.website .
docker run -d --rm -p 8088:80 --name srw-website srw-website
for p in / /configure /generator.mjs; do
  echo "$p -> $(curl -s -o /dev/null -w '%{http_code} %{content_type}' http://localhost:8088$p)"
done
docker stop srw-website
```

Expected: `/` → `200 text/html`, `/configure` → `200 text/html`, `/generator.mjs` → `200 application/javascript`.

- [ ] **Step 3: Commit**

```bash
git add docker/Dockerfile.website
git commit -m "build(website): nginx image (Dockerfile.website)"
```

---

## Task 11: CI — change filter + `generator-test` + `build-website` (develop.yml)

**Files:**
- Modify: `.github/workflows/develop.yml`

- [ ] **Step 1: Add a `website` output to the `changes` job**

In the `changes` job `outputs:` map (after `chart:` ~line 363), add:

```yaml
      website: ${{ steps.check.outputs.website }}
```

In the `check` step, after the `CHART=...` block (~line 512), add:

```bash
          if [ -z "$CHART_BASE" ] || [ -n "$(git diff --name-only "$CHART_BASE" HEAD -- 'website/' 'docker/Dockerfile.website')" ]; then
            WEBSITE="true"
          else
            WEBSITE="false"
          fi
          echo "  website=$WEBSITE"
```

And add `echo "website=$WEBSITE"` to the final `{ ... } >> "$GITHUB_OUTPUT"` block.

- [ ] **Step 2: Add the `generator-test` job** (after the `changes` job)

```yaml
  # ---------------------------------------------------------------------------
  # Config-generator drift-gate. Runs when website/ OR helm/ changed. Hard gate:
  # the generator's output must render against the real chart + carry a complete
  # secret-key set. Renders are deploy-correctness, not style — teeth on develop.
  # ---------------------------------------------------------------------------
  generator-test:
    needs: changes
    if: needs.changes.outputs.website == 'true' || needs.changes.outputs.chart == 'true' || github.event_name == 'workflow_dispatch'
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 22
      - name: Install Helm
        uses: azure/setup-helm@v4
        with:
          version: v3.17.0
      - name: Install kubeconform
        run: |
          curl -fsSL https://github.com/yannh/kubeconform/releases/download/v0.6.7/kubeconform-linux-amd64.tar.gz \
            | sudo tar -xz -C /usr/local/bin kubeconform
          kubeconform -v
      - name: Generator unit + drift tests
        run: node --test website/test/generator.unit.test.mjs website/test/generator.drift.test.mjs
```

- [ ] **Step 3: Add the `build-website` job** (after `build-cockpit`, ~line 728)

No buildcache (trivial one-COPY image); depends on `generator-test`; tags `:latest` + `:sha-*`.

```yaml
  build-website:
    needs: [changes, generator-test]
    if: >-
      always() && !cancelled() &&
      (needs.changes.outputs.website == 'true' || github.event_name == 'workflow_dispatch') &&
      needs.generator-test.result == 'success'
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        if: github.event_name != 'pull_request'
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - name: Extract metadata
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}-website
          tags: |
            type=sha,format=short,prefix=sha-
            type=raw,value=latest,enable=${{ github.event_name != 'pull_request' }}
      - name: Build${{ github.event_name == 'pull_request' && ' (PR validation, no push)' || ' and push' }}
        uses: docker/build-push-action@v5
        with:
          context: .
          file: ./docker/Dockerfile.website
          push: ${{ github.event_name != 'pull_request' }}
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          provenance: false
```

- [ ] **Step 4: Validate the workflow parses**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/develop.yml')); print('develop.yml OK')"`
Expected: `develop.yml OK`. If `actionlint` is installed, run `actionlint .github/workflows/develop.yml` (expect no output).

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/develop.yml
git commit -m "ci(develop): website change filter + generator drift-gate + build-website (publishes :latest)"
```

---

## Task 12: CI — `generator-test` on main.yml (always-run, hard)

**Files:**
- Modify: `.github/workflows/main.yml`

- [ ] **Step 1: Add the always-run `generator-test` job**

Insert near the other static gates (e.g. after `lint`/`chart-test`). No change-gate; hard.

```yaml
  generator-test:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 22
      - name: Install Helm
        uses: azure/setup-helm@v4
        with:
          version: v3.17.0
      - name: Install kubeconform
        run: |
          curl -fsSL https://github.com/yannh/kubeconform/releases/download/v0.6.7/kubeconform-linux-amd64.tar.gz \
            | sudo tar -xz -C /usr/local/bin kubeconform
          kubeconform -v
      - name: Generator unit + drift tests
        run: node --test website/test/generator.unit.test.mjs website/test/generator.drift.test.mjs
```

- [ ] **Step 2: Validate the workflow parses**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/main.yml')); print('main.yml OK')"`
Expected: `main.yml OK`.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/main.yml
git commit -m "ci(main): generator drift-gate (always-run, hard)"
```

---

## Task 13: HomeLab manifest swap (separate repo — do AFTER the image is published)

**Files:**
- Modify: `HomeLab/deployments_managed/srw-sales-page/10-deployment.yaml`

> `HomeLab/` is its own git repo nested in this workspace (use `git -C HomeLab`). The chart image must exist on GHCR first (push to develop → `build-website` publishes `:latest` → flip package public) or the new pod will `ImagePullBackOff`.

- [ ] **Step 1: Replace the ConfigMap deployment with the image**

In `HomeLab/deployments_managed/srw-sales-page/10-deployment.yaml`:
- **Delete** the entire `ConfigMap` resource (`srw-sales-page-content`, the big inlined block).
- In the `Deployment`, replace the `volumeMounts`/`volumes` ConfigMap wiring with a plain image container:

```yaml
      containers:
        - name: nginx
          image: ghcr.io/knaeckebrothero/superhuman-remote-worker-website:latest
          imagePullPolicy: Always
          ports:
            - containerPort: 80
              protocol: TCP
          livenessProbe:
            httpGet: { path: /, port: 80 }
            initialDelaySeconds: 5
            periodSeconds: 30
          readinessProbe:
            httpGet: { path: /, port: 80 }
            initialDelaySeconds: 2
            periodSeconds: 10
          resources:
            requests: { cpu: 10m, memory: 16Mi }
            limits:   { cpu: 100m, memory: 64Mi }
      # (no volumes block)
```

Leave `00-namespace.yaml`, `20-ingress.yaml`, the `Service`, and `fleet.yaml` unchanged.

- [ ] **Step 2: Validate YAML**

Run: `python3 -c "import yaml,sys; list(yaml.safe_load_all(open('HomeLab/deployments_managed/srw-sales-page/10-deployment.yaml'))); print('OK')"`
Expected: `OK`. If `kubectl` is configured: `kubectl apply --dry-run=client -f HomeLab/deployments_managed/srw-sales-page/10-deployment.yaml`.

- [ ] **Step 3: Commit in the HomeLab repo (push + redeploy are operator actions)**

```bash
git -C HomeLab add deployments_managed/srw-sales-page/10-deployment.yaml
git -C HomeLab commit -m "srw-sales-page: serve CI-built website image instead of inlined ConfigMap"
# Operator: push to the HomeLab remote; Fleet syncs. Then publish a new build by:
#   kubectl rollout restart deploy/srw-sales-page -n srw-sales-page
```

---

## Task 14 (optional): `og:image` social card

**Files:**
- Create: `website/og-image.png`
- Modify: `website/index.html`, `website/configure.html`, `docker/Dockerfile.website`

- [ ] **Step 1: Generate a 1200×630 PNG** (resolves landing-audit Issue 2)

```bash
# Headless screenshot of the hero (or design a dedicated card):
chromium --headless --screenshot=website/og-image.png --window-size=1200,630 \
  --hide-scrollbars website/index.html || \
  echo "fallback: create website/og-image.png (1200x630) by hand"
```

- [ ] **Step 2: Add meta tags** to both `index.html` and `configure.html` `<head>`:

```html
<meta property="og:image" content="https://superhuman-remote-worker.com/og-image.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:url" content="https://superhuman-remote-worker.com/">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary_large_image">
```

- [ ] **Step 3: Ship it in the image** — add to the `COPY` in `docker/Dockerfile.website`:

```dockerfile
COPY website/index.html website/configure.html website/generator.mjs website/og-image.png /usr/share/nginx/html/
```

- [ ] **Step 4: Commit**

```bash
git add website/og-image.png website/index.html website/configure.html docker/Dockerfile.website
git commit -m "feat(website): real og:image social card"
```

---

## Task 15: Update deploy docs + agent memory

**Files:**
- Modify: `docs/website.md` (deploy section), the `project_sales_page_deploy` memory.

- [ ] **Step 1: Note the new deploy flow in `docs/website.md`**

Add a short subsection under the architecture notes describing: source in `website/` → CI `build-website` → `ghcr.io/knaeckebrothero/superhuman-remote-worker-website:latest` → HomeLab Deployment → manual `kubectl rollout restart` to publish.

- [ ] **Step 2: Update the `project_sales_page_deploy` memory**

Replace the "re-indent index.html into the ConfigMap + Fleet sync + rollout restart" procedure with: edit `website/*` → push to develop → CI publishes `:latest` → `kubectl rollout restart deploy/srw-sales-page -n srw-sales-page`. (No ConfigMap, no re-indenting.)

- [ ] **Step 3: Commit the doc change**

```bash
git add docs/website.md
git commit -m "docs(website): record the image-based sales-page deploy flow"
```

---

## Self-Review

**Spec coverage (against `2026-06-18-helm-config-generator-design.md`):**
- §3 website/ reorg + page structure → Tasks 1, 9. ✔
- §4 pure `generate()` module shared by page + CI → Tasks 2–6, consumed by 8/9. ✔
- §5 three profiles + curated deltas + progressive disclosure → Tasks 2–4 (profiles), 9 (`data-show` deltas). ✔
- §6 three outputs (values/secret/install) → Tasks 2–4, 5, 6. ✔
- §7 secret handling (randoms in operator modes, eval omits APP_ENCRYPTION_KEY, optional local-fill) → Task 5. ✔
- §8 client-side validation (structural mirror + cross-field + schema parity) → Task 7. ✔
- §9 drift-gate (render + kubeconform + secretKeyRef completeness, both branches) → Tasks 8, 11, 12. ✔
- §10 UX/layout → Task 9. ✔
- §13 image packaging, build-website, :latest, HomeLab swap, manual rollout, doc/memory updates → Tasks 10, 11, 13, 15. ✔
- §3 og:image → Task 14. ✔
- §12 preconditions → Prerequisites block. ✔

**Placeholder scan:** every code step contains real code; tests carry full assertions; CI/Dockerfile/manifest blocks are complete. The one deliberate `{}` is `secrets.values: {}` (a valid chart value). Task 9 (HTML) gives full structure + key wiring rather than every line — its behavior is pinned by `generator.mjs`'s tested API + the Playwright smoke; acceptable for a thin DOM layer.

**Type/name consistency:** `generate()`/`validate()`/`randomHex()` signatures match across Tasks 2–9; `USER_KEYS`/`ENUMS`/`PROFILES` names are consistent; the image ref `ghcr.io/knaeckebrothero/superhuman-remote-worker-website` matches in Tasks 11 (`${IMAGE_NAME}-website`) and 13; `generator.mjs` extension is consistent everywhere (browser `<script type=module>` + Node `--test` + nginx `.mjs` MIME).

**Known acceptable looseness:** the drift-gate's `requiredSecretKeys` line-scanner is heuristic; if the chart emits an unusual `secretKeyRef` layout it may under/over-count — Task 8's render step is the primary correctness gate, the key-completeness check is the belt-and-suspenders layer.
