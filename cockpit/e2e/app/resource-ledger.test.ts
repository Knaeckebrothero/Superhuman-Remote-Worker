import { mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import type { APIRequestContext, APIResponse } from '@playwright/test';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { RUN_DIRECTORY } from './environment';
import {
  ResourceLedger,
  persistedResourceLedgerCanBeReplaced,
  remainingCleanupRequestTimeout,
} from './resource-ledger';

const THREAD_ID = '123e4567-e89b-42d3-a456-426614174000';

function ledger(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema: 1,
    run_id: 'previous-browser-run',
    resources: [{ kind: 'thread', id: THREAD_ID, created_at: '2026-08-24T00:00:00Z' }],
    finalized: true,
    cleanup_complete: true,
    ...overrides,
  };
}

describe('persistedResourceLedgerCanBeReplaced', () => {
  it('allows a finalized ledger only after exact resource and provider cleanup', () => {
    expect(persistedResourceLedgerCanBeReplaced(ledger())).toBe(true);
  });

  it.each([
    ['registration is unfinished', { finalized: false }],
    ['cleanup is unfinished', { cleanup_complete: false }],
    ['the cleanup marker is absent', { cleanup_complete: undefined }],
    ['a resource id is not exact', { resources: [{ kind: 'thread', id: 'all' }] }],
  ])('rejects replacement when %s', (_label, override) => {
    expect(persistedResourceLedgerCanBeReplaced(ledger(override))).toBe(false);
  });
});

describe('remainingCleanupRequestTimeout', () => {
  it('uses the lifecycle phase remainder instead of a short fixed request cap', () => {
    expect(remainingCleanupRequestTimeout(180_000, 0)).toBe(180_000);
    expect(remainingCleanupRequestTimeout(180_000, 75_250)).toBe(104_750);
    expect(remainingCleanupRequestTimeout(180_000, 180_000)).toBe(1);
  });
});

describe('ResourceLedger exact cleanup', () => {
  let directory: string;
  let resources: ResourceLedger;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    mkdirSync(RUN_DIRECTORY, { recursive: true });
    directory = mkdtempSync(join(RUN_DIRECTORY, 'ledger-unit-'));
    resources = new ResourceLedger('cleanup-unit-run', join(directory, 'resources.json'));
    resources.registerThread(THREAD_ID);
  });

  afterEach(() => {
    vi.useRealTimers();
    rmSync(directory, { recursive: true });
  });

  function response(status: number): APIResponse {
    return {
      status: () => status,
      ok: () => status >= 200 && status < 300,
      headers: () => ({ 'content-type': 'application/json' }),
      json: async () => ({ status: 'ending' }),
    } as unknown as APIResponse;
  }

  it.each([200, 202, 204])(
    'waits for exact absence after HTTP %s accepts retirement',
    async (status) => {
      const request = {
        delete: vi.fn().mockResolvedValue(response(status)),
        get: vi.fn().mockResolvedValueOnce(response(200)).mockResolvedValueOnce(response(404)),
      };
      const cleanup = resources
        .cleanup(request as unknown as APIRequestContext)
        .catch((error) => error);
      await vi.runAllTimersAsync();
      expect(await cleanup).toBeUndefined();

      const path = `/api/persistent/threads/${THREAD_ID}`;
      expect(request.delete.mock.calls.map(([url]) => url)).toEqual([
        `${path}?permanent=true`,
        `${path}?permanent=true`,
      ]);
      expect(request.get.mock.calls.map(([url]) => url)).toEqual([path, path]);
      expect(Date.now()).toBeGreaterThan(0);
      expect(Date.now()).toBeLessThan(180_000);
    },
  );

  it('exhausts both bounded windows when accepted retirement never deletes the exact thread', async () => {
    const mutations: { url: string; at: number }[] = [];
    const request = {
      delete: vi.fn(async (url: string) => {
        mutations.push({ url, at: Date.now() });
        return response(200);
      }),
      get: vi.fn().mockResolvedValue(response(200)),
    };
    const cleanup = resources
      .cleanup(request as unknown as APIRequestContext)
      .catch((error) => error);
    await vi.runAllTimersAsync();
    const failure = await cleanup;
    expect(failure).toBeInstanceOf(Error);
    expect(failure.message).toContain('Bounded force delete');

    const path = `/api/persistent/threads/${THREAD_ID}`;
    const forced = mutations.filter(({ url }) => url.endsWith('&force=true'));
    expect(forced.length).toBeGreaterThan(1);
    expect(forced[0].at).toBe(180_000);
    expect(
      mutations.every(({ url }) =>
        url === `${path}?permanent=true` || url === `${path}?permanent=true&force=true`,
      ),
    ).toBe(true);
    expect(request.get.mock.calls.every(([url]) => url === path)).toBe(true);
    expect(Date.now()).toBe(240_000);
    expect(JSON.parse(readFileSync(resources.path, 'utf8')).cleanup_complete).toBe(false);
  });

  it('allows exact force to converge only after the full graceful deadline', async () => {
    const forcedAt: number[] = [];
    const request = {
      delete: vi.fn(async (url: string) => {
        if (!url.endsWith('&force=true')) return response(409);
        forcedAt.push(Date.now());
        return response(200);
      }),
      get: vi.fn().mockResolvedValueOnce(response(200)).mockResolvedValueOnce(response(404)),
    };
    const cleanup = resources
      .cleanup(request as unknown as APIRequestContext)
      .catch((error) => error);
    await vi.runAllTimersAsync();
    expect(await cleanup).toBeUndefined();

    expect(forcedAt).toEqual([180_000, 180_250]);
    expect(
      request.delete.mock.calls.every(([url]) =>
        url.startsWith(`/api/persistent/threads/${THREAD_ID}?permanent=true`),
      ),
    ).toBe(true);
    expect(request.get).toHaveBeenCalledTimes(2);
  });

  it('does not turn an authorization failure during absence verification into force cleanup', async () => {
    const request = {
      delete: vi.fn().mockResolvedValue(response(200)),
      get: vi.fn().mockResolvedValue(response(403)),
    };
    await expect(resources.cleanup(request as unknown as APIRequestContext)).rejects.toThrow(
      'returned HTTP 403',
    );
    expect(request.delete).toHaveBeenCalledTimes(1);
    expect(request.delete.mock.calls[0][0]).not.toContain('force=true');
  });

  it('rejects broad registration and a UUID absent from this exact resource ledger', async () => {
    expect(() => resources.registerThread('all')).toThrow('non-UUID');
    const request = { delete: vi.fn(), get: vi.fn() };
    const privateCleanup = resources as unknown as {
      deleteThread(request: APIRequestContext, threadId: string): Promise<void>;
    };
    await expect(
      privateCleanup.deleteThread(
        request as unknown as APIRequestContext,
        '223e4567-e89b-42d3-a456-426614174000',
      ),
    ).rejects.toThrow('absent from this test resource ledger');
    expect(request.delete).not.toHaveBeenCalled();
    expect(request.get).not.toHaveBeenCalled();
  });
});
