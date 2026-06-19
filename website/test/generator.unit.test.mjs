import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { generate, validate, PROFILES } from '../generator.mjs';

// Walk a JSON Schema collecting dotted paths of every property with an `enum`.
function collectEnumPaths(node, prefix = '') {
  let out = [];
  if (node && node.properties) for (const [k, v] of Object.entries(node.properties)) {
    const p = prefix ? `${prefix}.${k}` : k;
    if (v.enum) out.push(p);
    out = out.concat(collectEnumPaths(v, p));
  }
  return out;
}
// Keep in sync with the structural rules in generator.mjs's validate().
const KNOWN_ENUM_PATHS = ['databases.neo4j.edition', 'vmController.transport'];

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

test('evaluation secret skeleton omits APP_ENCRYPTION_KEY (chart generates it)', () => {
  const { secretSkeleton } = generate('evaluation', evalInputs);
  // No APP_ENCRYPTION_KEY *entry* for the user to fill (the explanatory comment
  // may still name it — hence the trailing colon to match a key line only).
  assert.doesNotMatch(secretSkeleton, /APP_ENCRYPTION_KEY:/);
});

test('production secret skeleton includes generated + placeholder keys', () => {
  const { secretSkeleton } = generate('production', prodBase);
  assert.match(secretSkeleton, /APP_ENCRYPTION_KEY: [0-9a-f]{64}/);
  assert.match(secretSkeleton, /SESSION_JWT_SECRET: [0-9a-f]{64}/);
  assert.match(secretSkeleton, /POSTGRES_PASSWORD: CHANGE_ME/);
  assert.match(secretSkeleton, /kind: Secret/);
  assert.match(secretSkeleton, /name: srw-secrets/);
});

test('fillSecrets substitutes provided values for placeholders', () => {
  const { secretSkeleton } = generate('production',
    { ...prodBase, fillSecrets: true, secretValues: { POSTGRES_PASSWORD: 's3cret' } });
  assert.match(secretSkeleton, /POSTGRES_PASSWORD: s3cret/);
  assert.doesNotMatch(secretSkeleton, /POSTGRES_PASSWORD: CHANGE_ME/);
});

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
  assert.equal(errs.filter((e) => /neo4j/i.test(e)).length, 0);
  const errs2 = validate('production', { ...prodBase, neo4jEnabled: true, neo4jEdition: 'enterprise', neo4jLicenseAccepted: false });
  assert.ok(errs2.some((e) => /neo4j.*license/i.test(e)));
});

test('valid production input yields no errors', () => {
  assert.deepEqual(validate('production', prodBase), []);
});

test('structural rules cover every enum in helm/values.schema.json', () => {
  const schema = JSON.parse(readFileSync(new URL('../../helm/values.schema.json', import.meta.url)));
  for (const path of collectEnumPaths(schema)) {
    assert.ok(KNOWN_ENUM_PATHS.includes(path), `validator missing enum rule for ${path}`);
  }
});
