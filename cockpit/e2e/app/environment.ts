import { randomUUID } from 'node:crypto';
import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  renameSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { dirname, isAbsolute, parse, relative, resolve } from 'node:path';

const APP_DIRECTORY = resolve(__dirname);

export const RESULTS_DIRECTORY = resolve(APP_DIRECTORY, '../../test-results/app');
const DEFAULT_REPORT_DIRECTORY = resolve(APP_DIRECTORY, '../../playwright-report');

function assertContained(root: string, target: string, label: string, allowRoot: boolean): void {
  const relation = relative(root, target);
  if ((!allowRoot && relation === '') || relation.startsWith('..') || isAbsolute(relation)) {
    throw new Error(`${label} must stay within its explicitly approved output root.`);
  }
}

function assertNoExistingSymlinks(target: string, leaf: 'directory' | 'file'): void {
  const parsed = parse(target);
  let cursor = parsed.root;
  const segments = target.slice(parsed.root.length).split('/').filter(Boolean);
  segments.forEach((segment, index) => {
    cursor = resolve(cursor, segment);
    if (!existsSync(cursor)) return;
    const stat = lstatSync(cursor);
    if (stat.isSymbolicLink()) {
      throw new Error('E2E output paths must not contain symbolic links.');
    }
    const isLeaf = index === segments.length - 1;
    if ((!isLeaf || leaf === 'directory') && !stat.isDirectory()) {
      throw new Error('An E2E output path component is not a directory.');
    }
    if (isLeaf && leaf === 'file' && !stat.isFile()) {
      throw new Error('An E2E private-state target exists but is not a regular file.');
    }
  });
}

export const RUN_DIRECTORY = resolve(process.env['APP_E2E_RUN_DIR'] ?? RESULTS_DIRECTORY);
assertContained(RESULTS_DIRECTORY, RUN_DIRECTORY, 'APP_E2E_RUN_DIR', true);
assertNoExistingSymlinks(RUN_DIRECTORY, 'directory');

// Playwright clears outputDir at startup. Keep that destructive boundary in a
// dedicated child: the run root can also contain harness-owned kubeconfig,
// generated credentials, auth state, and the crash-recovery resource ledger.
export const ARTIFACT_DIRECTORY = resolve(RUN_DIRECTORY, 'artifacts');
assertContained(RUN_DIRECTORY, ARTIFACT_DIRECTORY, 'Playwright artifact directory', false);
assertNoExistingSymlinks(ARTIFACT_DIRECTORY, 'directory');

export const REPORT_DIRECTORY = process.env['APP_E2E_REPORT_DIR']
  ? resolve(process.env['APP_E2E_REPORT_DIR'])
  : DEFAULT_REPORT_DIRECTORY;
assertContained(
  process.env['APP_E2E_REPORT_DIR'] ? RUN_DIRECTORY : DEFAULT_REPORT_DIRECTORY,
  REPORT_DIRECTORY,
  'APP_E2E_REPORT_DIR',
  !process.env['APP_E2E_REPORT_DIR'],
);
assertNoExistingSymlinks(REPORT_DIRECTORY, 'directory');

export const AUTH_STATE_PATH = resolve(
  process.env['APP_E2E_AUTH_STATE'] ?? resolve(RUN_DIRECTORY, '.auth/journey.json'),
);
export const AUTH_STATE_CANDIDATE_PATH = `${AUTH_STATE_PATH}.candidate`;
assertContained(RUN_DIRECTORY, AUTH_STATE_PATH, 'APP_E2E_AUTH_STATE', false);
assertContained(RUN_DIRECTORY, AUTH_STATE_CANDIDATE_PATH, 'APP_E2E auth candidate', false);
assertNoExistingSymlinks(AUTH_STATE_PATH, 'file');
assertNoExistingSymlinks(AUTH_STATE_CANDIDATE_PATH, 'file');

export const ATTACH_MODE = process.env['APP_E2E_ATTACH_MODE'] === '1';
export const DEFER_FAILED_CLEANUP = process.env['APP_E2E_DEFER_FAILED_CLEANUP'] === '1';
const browserAttempt = Number(process.env['APP_E2E_BROWSER_ATTEMPT'] ?? '1');
if (!Number.isSafeInteger(browserAttempt) || browserAttempt < 1) {
  throw new Error('APP_E2E_BROWSER_ATTEMPT must be a positive integer.');
}
export const OWNED_BROWSER_RERUN =
  process.env['APP_E2E_OWNED_CLUSTER'] === '1' && browserAttempt > 1;

function validateAttachMode(): void {
  if (ATTACH_MODE && process.env['APP_E2E_ALLOW_ATTACH'] !== '1') {
    throw new Error(
      'APP_E2E_ATTACH_MODE=1 requires APP_E2E_ALLOW_ATTACH=1. ' +
        'Attach mode targets an existing stack and must be explicitly acknowledged.',
    );
  }
}

function isLocalHostname(hostname: string): boolean {
  const normalized = hostname.replace(/^\[|\]$/g, '').toLowerCase();
  return (
    normalized === 'localhost' ||
    normalized === '127.0.0.1' ||
    normalized === '::1' ||
    normalized.endsWith('.localhost')
  );
}

function parseHttpUrl(raw: string, label: string): URL {
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new Error(`${label} must be an absolute HTTP(S) URL.`);
  }
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error(`${label} must use http:// or https://.`);
  }
  if (parsed.username || parsed.password) {
    throw new Error(`${label} must not contain credentials.`);
  }
  if (parsed.search || parsed.hash) {
    throw new Error(`${label} must not contain a query string or fragment.`);
  }
  return parsed;
}

function applicationBaseUrl(): string {
  const parsed = parseHttpUrl(
    process.env['APP_E2E_BASE_URL'] ?? 'https://localhost',
    'APP_E2E_BASE_URL',
  );
  if (parsed.pathname !== '/' && parsed.pathname !== '') {
    throw new Error('APP_E2E_BASE_URL must be an origin without a path.');
  }
  if (!isLocalHostname(parsed.hostname) && process.env['APP_E2E_ALLOW_REMOTE'] !== '1') {
    throw new Error(
      'The application E2E suite is destructive and accepts only loopback/.localhost by default. ' +
        'Set APP_E2E_ALLOW_REMOTE=1 only for a disposable environment owned by this run.',
    );
  }
  return parsed.origin;
}

validateAttachMode();

export const BASE_URL = applicationBaseUrl();
export const BASE_ORIGIN = new URL(BASE_URL).origin;

export function appUrl(pathname: string): string {
  return new URL(pathname, `${BASE_URL}/`).toString();
}

export function requiredEnvironment(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required; this suite has no credential defaults.`);
  return value;
}

export function privateOutputPath(raw: string, label: string): string {
  const target = resolve(raw);
  assertContained(RUN_DIRECTORY, target, label, false);
  assertNoExistingSymlinks(target, 'file');
  return target;
}

export const RESOURCE_LEDGER_OVERRIDE = process.env['APP_E2E_RESOURCE_LEDGER']
  ? privateOutputPath(process.env['APP_E2E_RESOURCE_LEDGER'], 'APP_E2E_RESOURCE_LEDGER')
  : undefined;

function preparePrivateFile(target: string): void {
  privateOutputPath(target, 'private E2E output');
  assertNoExistingSymlinks(dirname(target), 'directory');
  mkdirSync(dirname(target), { recursive: true, mode: 0o700 });
  assertNoExistingSymlinks(dirname(target), 'directory');
  assertNoExistingSymlinks(target, 'file');
}

export function removePrivateFile(target: string): void {
  preparePrivateFile(target);
  if (existsSync(target)) rmSync(target);
}

export function writePrivateJsonFile(target: string, value: unknown): void {
  preparePrivateFile(target);
  const temporary = `${target}.${process.pid}.${randomUUID()}.tmp`;
  privateOutputPath(temporary, 'temporary private E2E output');
  try {
    writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, {
      encoding: 'utf8',
      flag: 'wx',
      mode: 0o600,
    });
    renameSync(temporary, target);
    chmodSync(target, 0o600);
  } finally {
    if (existsSync(temporary)) rmSync(temporary);
  }
}

export interface RuntimeEnvironment {
  username: string;
  password: string;
  adminUsername: string;
  adminPassword: string;
  controlUrl: string;
  controlToken: string;
  providerBaseUrl: string;
  chatModel: string;
  embeddingModel: string;
  workspaceBackend: 'virtual' | 'sandbox';
  expectedExecutionLane: 'pinned' | 'stateless';
}

function expectedChoice<T extends string>(name: string, fallback: T, allowed: readonly T[]): T {
  const value = process.env[name]?.trim() || fallback;
  if (!allowed.includes(value as T)) {
    throw new Error(`${name} must be one of: ${allowed.join(', ')}.`);
  }
  return value as T;
}

export function runtimeEnvironment(): RuntimeEnvironment {
  const control = parseHttpUrl(requiredEnvironment('APP_E2E_CONTROL_URL'), 'APP_E2E_CONTROL_URL');
  if (control.pathname !== '/' && control.pathname !== '') {
    throw new Error('APP_E2E_CONTROL_URL must be an origin without a path.');
  }

  const provider = parseHttpUrl(
    requiredEnvironment('APP_E2E_PROVIDER_BASE_URL'),
    'APP_E2E_PROVIDER_BASE_URL',
  );
  if (!provider.pathname.replace(/\/$/, '').endsWith('/v1')) {
    throw new Error('APP_E2E_PROVIDER_BASE_URL must end in /v1.');
  }

  const workspaceBackend = expectedChoice('APP_E2E_WORKSPACE_BACKEND', 'virtual', [
    'virtual',
    'sandbox',
  ] as const);
  const expectedExecutionLane = expectedChoice('APP_E2E_EXPECT_EXECUTION_LANE', 'pinned', [
    'pinned',
    'stateless',
  ] as const);
  if (
    (workspaceBackend === 'virtual' && expectedExecutionLane !== 'pinned') ||
    (workspaceBackend === 'sandbox' && expectedExecutionLane !== 'stateless')
  ) {
    throw new Error('The application E2E workspace and execution-lane profile is invalid.');
  }

  return {
    username: requiredEnvironment('APP_E2E_USERNAME'),
    password: requiredEnvironment('APP_E2E_PASSWORD'),
    adminUsername: requiredEnvironment('APP_E2E_ADMIN_USERNAME'),
    adminPassword: requiredEnvironment('APP_E2E_ADMIN_PASSWORD'),
    controlUrl: control.origin,
    controlToken: requiredEnvironment('APP_E2E_CONTROL_TOKEN'),
    providerBaseUrl: provider.toString().replace(/\/$/, ''),
    chatModel: process.env['APP_E2E_CHAT_MODEL']?.trim() || 'e2e-chat',
    embeddingModel: process.env['APP_E2E_EMBEDDING_MODEL']?.trim() || 'e2e-embedding',
    workspaceBackend,
    expectedExecutionLane,
  };
}
