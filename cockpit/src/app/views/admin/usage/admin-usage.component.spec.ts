import {TestBed} from '@angular/core/testing';
import {provideHttpClient} from '@angular/common/http';
import {provideHttpClientTesting} from '@angular/common/http/testing';
import {TranslocoService} from '@jsverse/transloco';
import {AdminUsageComponent} from './admin-usage.component';

// Note: uses TestBed.inject (not createComponent) to avoid JIT styleUrl
// resolution for AppIconComponent — the established pattern in this codebase
// when a component's deep dep has an external stylesheet. See admin-config.component.spec.ts.
describe('AdminUsageComponent refresh shell', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        AdminUsageComponent,
        provideHttpClient(),
        provideHttpClientTesting(),
        {provide: TranslocoService, useValue: {translate: (k: string) => k}},
      ],
    });
  });

  it('setRefresh updates the interval signal', () => {
    const c = TestBed.inject(AdminUsageComponent);
    expect(c.refreshIntervalMs()).toBe(0);
    c.setRefresh(30000);
    expect(c.refreshIntervalMs()).toBe(30000);
  });

  it('tokensTotal sums prompt + completion token quantities', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).usage.usage.set({available: true, total_cost_usd: 0, by_category: [
      {category: 'llm', unit: 'prompt-token', quantity: 100, cost_usd: 0, events: 1},
      {category: 'llm', unit: 'completion-token', quantity: 25, cost_usd: 0, events: 1},
      {category: 'compute', unit: 'vcpu-hour', quantity: 2, cost_usd: 0, events: 1},
    ]});
    expect(c.tokensTotal()).toBe(125);
    expect(c.computeHours()).toBe(2);
  });

  it('userRows derives role and per-unit columns with a share fraction', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).usage.breakdown = () => ({available: true, group_by: 'user', rows: [
      {key: 'u1', label: 'Alice', is_admin: true, events: 4, cost_usd: 0, units: {
        'prompt-token': {quantity: 100, cost_usd: 0, events: 2},
        'completion-token': {quantity: 30, cost_usd: 0, events: 2}}},
      {key: 'u2', label: 'Bob', is_admin: false, events: 2, cost_usd: 0, units: {
        'vcpu-hour': {quantity: 1.5, cost_usd: 0, events: 2}}},
    ]});
    const rows = c.userRows();
    expect(rows[0].role).toBe('Admin');
    expect(rows[0].prompt).toBe(100);
    expect(rows[0].share).toBe(1);     // max events
    expect(rows[1].share).toBe(0.5);
  });

  it('modelRows lists per-model token columns', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).usage.breakdown = (dim: string) => dim === 'model' ? ({available: true,
      group_by: 'model', rows: [{key: 'gemma', label: 'gemma', events: 2, cost_usd: 0,
      units: {'prompt-token': {quantity: 100, cost_usd: 0, events: 1},
              'completion-token': {quantity: 20, cost_usd: 0, events: 1}}}]}) : null;
    expect(c.modelRows()[0].prompt).toBe(100);
    expect(c.modelRows()[0].label).toBe('gemma');
  });

  it('dailyBars scales bar height to the busiest day', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).daily.set([
      {date: '2026-06-24', jobs_created: 0, jobs_completed: 5, jobs_failed: 0, jobs_cancelled: 0},
      {date: '2026-06-25', jobs_created: 0, jobs_completed: 10, jobs_failed: 0, jobs_cancelled: 0},
    ]);
    const bars = c.dailyBars();
    expect(bars[1].height).toBe(100);
    expect(bars[0].height).toBe(50);
  });
});
