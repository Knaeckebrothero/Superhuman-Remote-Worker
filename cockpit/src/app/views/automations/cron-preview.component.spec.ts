import {describe, expect, it} from 'vitest';
import {computeCronSummary} from './cron-preview.component';

describe('computeCronSummary', () => {
  it('returns valid + humanized + N next runs for a recognized expression', () => {
    const summary = computeCronSummary(
      '0 9 * * *',
      'UTC',
      5,
      new Date('2026-05-18T12:00:00Z'),
    );
    expect(summary.valid).toBe(true);
    expect(summary.error).toBeNull();
    expect(summary.humanized.toLowerCase()).toContain('09:00');
    expect(summary.nextRuns).toHaveLength(5);
  });

  it('marks an empty expression as invalid without an error string', () => {
    const summary = computeCronSummary('', 'UTC', 5);
    expect(summary.valid).toBe(false);
    expect(summary.humanized).toBe('');
    expect(summary.nextRuns).toEqual([]);
    expect(summary.error).toBeNull();
  });

  it('marks a malformed expression as invalid with an error message', () => {
    const summary = computeCronSummary('not a cron', 'UTC', 5);
    expect(summary.valid).toBe(false);
    expect(typeof summary.error).toBe('string');
    expect(summary.error?.length ?? 0).toBeGreaterThan(0);
  });

  it('honors previewCount', () => {
    const summary = computeCronSummary(
      '*/15 * * * *',
      'UTC',
      3,
      new Date('2026-05-18T12:00:00Z'),
    );
    expect(summary.valid).toBe(true);
    expect(summary.nextRuns).toHaveLength(3);
  });

  it('respects DST in the timezone when computing next runs', () => {
    // 09:00 Europe/Berlin daily — on the morning of 2026-03-29 (spring
    // forward) the next run should be 09:00 CEST (= 07:00 UTC), not
    // 09:00 CET. Verified locally via Date.toLocaleString.
    const before = new Date('2026-03-28T08:01:00Z'); // just after the 09:00 CET fire
    const summary = computeCronSummary('0 9 * * *', 'Europe/Berlin', 1, before);
    expect(summary.valid).toBe(true);
    expect(summary.nextRuns[0]).toContain('29');
  });

  it('defaults to UTC when timezone is an empty string', () => {
    const summary = computeCronSummary('0 9 * * *', '', 1, new Date('2026-05-18T12:00:00Z'));
    expect(summary.valid).toBe(true);
    expect(summary.nextRuns).toHaveLength(1);
  });
});
