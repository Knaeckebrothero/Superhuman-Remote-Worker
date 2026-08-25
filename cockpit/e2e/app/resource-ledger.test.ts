import { describe, expect, it } from 'vitest';
import { persistedResourceLedgerCanBeReplaced } from './resource-ledger';

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
