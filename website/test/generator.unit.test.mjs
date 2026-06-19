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
