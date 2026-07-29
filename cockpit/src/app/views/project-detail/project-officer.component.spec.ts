import {describe, it, expect, vi, afterEach} from 'vitest';

import {
  buildSlotsSpec,
  nextWakeLabel,
  type SlotDraft,
} from './project-officer.component';

/**
 * Centurion card (centurion.md S9) — extracted-function tests, following the
 * project-loop convention (pure logic, no TestBed). The slot spec assembled
 * here is hard-validated again server-side (officer_slots.validate_slots_spec);
 * these tests only pin what the FORM may emit.
 */

describe('buildSlotsSpec', () => {
  const row = (over: Partial<SlotDraft> = {}): SlotDraft => ({
    name: 'line',
    count: 2,
    model: 'MiniMax-M3',
    backend: 'vm',
    ...over,
  });

  it('assembles named rows into the officer.slots shape', () => {
    expect(buildSlotsSpec([row()])).toEqual({
      line: {count: 2, model: 'MiniMax-M3', backend: 'vm'},
    });
  });

  it('returns null with no usable rows — flat cap, not an empty roster', () => {
    expect(buildSlotsSpec([])).toBeNull();
    expect(buildSlotsSpec([row({name: '   '})])).toBeNull();
  });

  it('drops blank names and omits blank model/backend', () => {
    const spec = buildSlotsSpec([
      row({name: 'heavy', count: 1, model: '', backend: ''}),
      row({name: ''}),
    ]);
    expect(spec).toEqual({heavy: {count: 1}});
  });

  it('normalizes names to lowercase and floors/clamps counts', () => {
    const spec = buildSlotsSpec([
      row({name: '  Line ', count: 2.9}),
      row({name: 'scout', count: 0}),
    ]);
    expect(spec).toEqual({
      line: {count: 2, model: 'MiniMax-M3', backend: 'vm'},
      scout: {count: 1, model: 'MiniMax-M3', backend: 'vm'},
    });
  });

  it('last row wins a duplicate name (the server 400s ambiguity anyway)', () => {
    const spec = buildSlotsSpec([
      row({name: 'line', count: 1}),
      row({name: 'line', count: 3}),
    ]);
    expect(spec).toEqual({line: {count: 3, model: 'MiniMax-M3', backend: 'vm'}});
  });
});

describe('nextWakeLabel', () => {
  afterEach(() => vi.useRealTimers());

  it('reads event-driven when no timer is pending', () => {
    expect(nextWakeLabel(null)).toContain('not scheduled');
    expect(nextWakeLabel(undefined)).toContain('not scheduled');
  });

  it('renders a future fire_at as minutes from now', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-30T04:00:00Z'));
    expect(nextWakeLabel('2026-07-30T04:42:00Z')).toBe('in 42 min');
  });

  it('renders a just-due timer as due now', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-30T04:00:30Z'));
    expect(nextWakeLabel('2026-07-30T04:00:00Z')).toBe('due now');
  });

  it('renders a stale timer as overdue (the watchdog signal)', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-30T04:10:00Z'));
    expect(nextWakeLabel('2026-07-30T04:00:00Z')).toBe('overdue 10 min');
  });

  it('falls back to the raw value on garbage', () => {
    expect(nextWakeLabel('not-a-date')).toBe('not-a-date');
  });
});
