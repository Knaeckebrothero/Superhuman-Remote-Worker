import {describe, expect, it} from 'vitest';
import {
  costDisplay,
  formatCount,
  formatDurationSeconds,
  formatUsd,
  jobModelLabel,
  workspaceBackendLabel,
} from './job-detail-panel.component';
import type {Job, JobUsage} from '../../core/models/api.model';

/**
 * The panel's job is to be honest about what it does not know, so that is what
 * is pinned here. `GET /api/jobs/{id}/usage` distinguishes four states and a
 * nullable price precisely because "unknown" and "zero" are different claims —
 * on the dev cluster a job with 1.05M tokens legitimately has no cost at all,
 * because the model it ran on has no rate card. Rendering that as $0.00 is the
 * failure this component exists to avoid.
 *
 * Pure helpers, tested directly: vitest runs Angular JIT, where signal inputs
 * cannot be property-bound (NG0303/NG0950) and `componentRef.setInput()` does
 * not work around it, so a rendered test would assert the harness.
 */

function usage(over: Partial<JobUsage> = {}): JobUsage {
  return {
    job_id: 'j',
    scope: 'job',
    job_count: 1,
    state: 'measured',
    window: {from: '2026-08-01T00:00:00Z', to: '2026-08-23T00:00:00Z'},
    freshness: {as_of: '2026-08-23T00:00:00Z', live: false, lag_seconds: 180},
    rows: [],
    llm: {
      prompt_tokens: 0,
      cached_prompt_tokens: 0,
      completion_tokens: 0,
      total_tokens: 0,
      cache_hit_ratio: 0,
    },
    by_category: [],
    cost: {usd: 0.94, complete: true, priced_events: 10, events: 10},
    ...over,
  };
}

describe('costDisplay', () => {
  it('shows a complete price plainly', () => {
    expect(costDisplay(usage())).toEqual({amount: 0.94, isFloor: false, reasonKey: null});
  });

  it('marks a partially priced job as a floor', () => {
    // Real shape from job 5c0ed235: 599 of 827 events priced, because part of
    // the run used a model with no rate card. $0.94 is a lower bound.
    const display = costDisplay(
      usage({cost: {usd: 0.94, complete: false, priced_events: 599, events: 827}}),
    );
    expect(display.amount).toBe(0.94);
    expect(display.isFloor).toBe(true);
  });

  it('refuses to show a number when nothing was priced', () => {
    // Job 77bbb5bd: 1,053,666 tokens, 0 of 136 events priced.
    const display = costDisplay(
      usage({cost: {usd: null, complete: false, priced_events: 0, events: 136}}),
    );
    expect(display.amount).toBeNull();
    expect(display.reasonKey).toBe('jobs.detail.costUnpriced');
  });

  it.each([
    ['no_usage', 'jobs.detail.costNoUsage'],
    ['predates_ledger', 'jobs.detail.costPredatesLedger'],
    ['unavailable', 'jobs.detail.costMeteringOff'],
  ] as const)('explains the %s state instead of showing zero', (state, key) => {
    const display = costDisplay(usage({state}));
    expect(display.amount).toBeNull();
    expect(display.reasonKey).toBe(key);
  });

  it('does not let a stale cost leak through a non-measured state', () => {
    // The endpoint zeroes these itself, but the guard must not depend on that:
    // a state that says "no figures" outranks any number in the payload.
    const display = costDisplay(
      usage({state: 'predates_ledger', cost: {usd: 9.99, complete: true, priced_events: 1, events: 1}}),
    );
    expect(display.amount).toBeNull();
  });

  it('treats a failed load as unknown, not free', () => {
    expect(costDisplay(null)).toEqual({
      amount: null,
      isFloor: false,
      reasonKey: 'jobs.detail.costUnknown',
    });
  });
});

describe('formatUsd', () => {
  it('keeps sub-cent amounts visible instead of rounding them to $0.00', () => {
    // Most jobs on the cluster cost fractions of a cent; toFixed(2) would render
    // every one of them as free, which is the same lie by a different route.
    expect(formatUsd(0.0004)).not.toBe('$0.00');
    expect(formatUsd(0.0004)).toBe('$0.00040');
  });

  it('formats ordinary amounts to two decimals', () => {
    expect(formatUsd(0.94011774)).toBe('$0.94');
    expect(formatUsd(12)).toBe('$12.00');
  });

  it('renders a genuine zero as zero', () => {
    // A measured zero is a real answer and must be distinguishable from unknown,
    // which never reaches this function at all.
    expect(formatUsd(0)).toBe('$0.00');
  });
});

describe('jobModelLabel', () => {
  it('reads the override the job actually ran with', () => {
    expect(jobModelLabel({config_override: {llm: {model: 'MiniMax-M3'}}} as unknown as Job)).toBe(
      'MiniMax-M3',
    );
  });

  it('survives config_override arriving as a JSON string', () => {
    // JSONB comes back from asyncpg as text and is passed through, so indexing
    // straight in type-checks and then silently yields undefined at runtime.
    expect(
      jobModelLabel({config_override: '{"llm":{"model":"gemma-4-moe"}}'} as unknown as Job),
    ).toBe('gemma-4-moe');
  });

  it('returns null rather than inventing a model when none is pinned', () => {
    // resolved_config comes back empty for ordinary jobs, so the client cannot
    // know what the expert default resolved to. The panel says so.
    expect(jobModelLabel({config_override: {autonomy: 'full'}} as unknown as Job)).toBeNull();
    expect(jobModelLabel(null)).toBeNull();
    expect(jobModelLabel({config_override: 'not json'} as unknown as Job)).toBeNull();
  });
});

describe('workspaceBackendLabel', () => {
  it('prefers effective over assigned over requested', () => {
    expect(
      workspaceBackendLabel({
        requested_backend: 'vm',
        assigned_backend: 'sandbox',
        effective_backend: 'virtual',
        state: 'ready',
      }),
    ).toBe('virtual');
    expect(workspaceBackendLabel({requested_backend: 'vm', state: 'waiting'})).toBe('vm');
  });

  it('is null when there is no contract at all', () => {
    expect(workspaceBackendLabel(null)).toBeNull();
    expect(workspaceBackendLabel({state: 'waiting'})).toBeNull();
  });
});

describe('formatDurationSeconds', () => {
  it('scales the unit to the magnitude', () => {
    expect(formatDurationSeconds(42)).toBe('42s');
    expect(formatDurationSeconds(90)).toBe('1m 30s');
    expect(formatDurationSeconds(3700)).toBe('1h 1m');
    expect(formatDurationSeconds(90000)).toBe('1d 1h');
  });

  it('returns null for absent or nonsensical input rather than "0s"', () => {
    expect(formatDurationSeconds(null)).toBeNull();
    expect(formatDurationSeconds(undefined)).toBeNull();
    expect(formatDurationSeconds(-1)).toBeNull();
    expect(formatDurationSeconds(NaN)).toBeNull();
  });
});

describe('formatCount', () => {
  it('separates thousands and dashes absent values', () => {
    expect(formatCount(1053666)).toBe((1053666).toLocaleString());
    expect(formatCount(0)).toBe('0');
    expect(formatCount(null)).toBe('—');
    expect(formatCount(undefined)).toBe('—');
  });
});
