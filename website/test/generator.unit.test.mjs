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
