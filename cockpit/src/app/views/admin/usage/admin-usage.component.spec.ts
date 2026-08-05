import {TestBed} from '@angular/core/testing';
import {provideHttpClient} from '@angular/common/http';
import {provideHttpClientTesting} from '@angular/common/http/testing';
import {TranslocoService} from '@jsverse/transloco';
import {vi} from 'vitest';
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

  it('offers 8h and 24h window presets before the existing day filters', () => {
    const c = TestBed.inject(AdminUsageComponent);
    const reload = vi.spyOn(c, 'reloadAll').mockImplementation(() => {});
    expect(c.windows.map((w) => w.label)).toEqual(['8h', '24h', '7d', '30d', '90d']);
    c.setWindow(8);
    expect(c.windowHours()).toBe(8);
    expect(c.windowDays()).toBe(1);
    expect(reload).toHaveBeenCalledOnce();
  });

  it('keeps token, vCPU, and compute-memory quantities dimensionally separate', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).usage.usage.set({available: true, total_cost_usd: 0, cache_hit_ratio: 0.2, by_category: [
      {category: 'llm', unit: 'prompt-token', quantity: 100, cost_usd: 0, events: 1},
      {category: 'llm', unit: 'cached-prompt-token', quantity: 25, cost_usd: 0, events: 1},
      {category: 'llm', unit: 'completion-token', quantity: 25, cost_usd: 0, events: 1},
      {category: 'compute', unit: 'vcpu-hour', quantity: 2, cost_usd: 0, events: 1},
      {category: 'compute', unit: 'gib-hour', quantity: 4, cost_usd: 0, events: 1},
      {category: 'storage', unit: 'gib-hour', quantity: 40, cost_usd: 0, events: 1},
    ]});
    expect(c.tokensTotal()).toBe(150);
    expect(c.cacheHitRatio()).toBe(0.2);
    expect(c.vcpuHours()).toBe(2);
    expect(c.memoryGibHours()).toBe(4);
    expect((c as any).computeHours).toBeUndefined();
  });

  it('keeps claim demand and physical volume storage separate in v2', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).usage.usage.set({available: true, total_cost_usd: 0, cache_hit_ratio: 0, by_category: [
      {category: 'storage', unit: 'gib-hour', quantity: 60, cost_usd: 0, events: 1},
    ]});
    expect(c.hasClaimStorage()).toBe(false);
    expect(c.hasVolumeStorage()).toBe(false);

    const row = (basis: string, unit: string, quantity: string) => ({
      category: 'storage', measurement_basis: basis, unit, quantity,
    });
    (c as any).usage.usageV2.set({rows: [
      row('claim-requested', 'gib-hour', '60'),
      row('claim-requested', 'claim-hour', '3'),
      row('volume-provisioned', 'gib-hour', '75'),
      row('volume-provisioned', 'volume-hour', '2.5'),
    ]});
    expect(c.hasClaimStorage()).toBe(true);
    expect(c.claimGibHours()).toBe(60);
    expect(c.claimHours()).toBe(3);
    expect(c.hasVolumeStorage()).toBe(true);
    expect(c.volumeGibHours()).toBe(75);
    expect(c.volumeHours()).toBe(2.5);
  });

  it('prefers reconciled v2 workspace rows for compute KPIs when available', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).usage.usage.set({available: true, total_cost_usd: 0, by_category: [
      {category: 'compute', unit: 'vcpu-hour', quantity: 2, cost_usd: 0, events: 1},
      {category: 'compute', unit: 'gib-hour', quantity: 4, cost_usd: 0, events: 1},
    ]});
    const row = (unit: string, quantity: string) => ({
      category: 'compute', measurement_basis: 'scheduler-request', unit, quantity,
    });
    (c as any).usage.usageV2.set({rows: [
      row('vcpu-hour', '2.5'), row('gib-hour', '5.5'),
    ]});
    expect(c.vcpuHours()).toBe(2.5);
    expect(c.memoryGibHours()).toBe(5.5);
  });

  it('does not turn missing rows in a partial v2 response into zero usage', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).usage.usage.set({available: true, total_cost_usd: 0, by_category: [
      {category: 'compute', unit: 'vcpu-hour', quantity: 2, cost_usd: 0, events: 1},
      {category: 'compute', unit: 'gib-hour', quantity: 4, cost_usd: 0, events: 1},
    ]});
    (c as any).usage.usageV2.set({
      rows: [],
      coverage: {status: 'partial'},
    });

    expect(c.vcpuHours()).toBe(2);
    expect(c.memoryGibHours()).toBe(4);
  });

  it('exposes provider compute estimates without folding them into canonical cost', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).usage.usage.set({
      available: true,
      total_cost_usd: 1.25,
      cache_hit_ratio: 0,
      by_category: [],
      cloud_estimates: [{
        id: 'stackit', provider: 'stackit', display_name: 'STACKIT', region: 'EU01',
        currency: 'EUR', aggregation: 'max', estimate: 0.42, priced_at: 'now',
        source_url: 'https://example.test', source_label: 'Price list',
        description: 'Node share', exclusions: 'Control plane excluded', components: [],
      }],
    });
    expect(c.cloudEstimates()[0].estimate).toBe(0.42);
    expect(c.summary()?.total_cost_usd).toBe(1.25);
    expect(c.fmtCurrency(0.42, 'EUR')).toMatch(/0[.,]42/);
  });

  it('userRows derives role and per-unit columns with a share fraction', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).usage.breakdown = () => ({available: true, group_by: 'user', rows: [
      {key: 'u1', label: 'Alice', is_admin: true, events: 4, cost_usd: 0, units: {
        'prompt-token': {quantity: 75, cost_usd: 0, events: 1},
        'cached-prompt-token': {quantity: 25, cost_usd: 0, events: 1},
        'completion-token': {quantity: 30, cost_usd: 0, events: 2}}},
      {key: 'u2', label: 'Bob', is_admin: false, events: 2, cost_usd: 0, units: {
        'vcpu-hour': {quantity: 1.5, cost_usd: 0, events: 1},
        'gib-hour': {quantity: 3, cost_usd: 0, events: 1}}},
    ]});
    const rows = c.userRows();
    expect(rows[0].role).toBe('Admin');
    expect(rows[0].prompt).toBe(100);
    expect(rows[0].share).toBe(1);     // max events
    expect(rows[1].share).toBe(0.5);
    expect(rows[1].vcpu).toBe(1.5);
    expect(rows[1].memory).toBe(3);
    expect((rows[1] as any).compute).toBeUndefined();
  });

  it('does not label a categoryless mixed GiB-hour breakdown as memory', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).usage.usage.set({available: true, total_cost_usd: 0, cache_hit_ratio: 0, by_category: [
      {category: 'compute', unit: 'gib-hour', quantity: 2, cost_usd: 0, events: 1},
      {category: 'storage', unit: 'gib-hour', quantity: 10, cost_usd: 0, events: 1},
    ]});
    (c as any).usage.breakdown = () => ({available: true, group_by: 'user', rows: [
      {key: 'u1', label: 'Alice', is_admin: false, events: 2, cost_usd: 0, units: {
        'gib-hour': {quantity: 12, cost_usd: 0, events: 2},
      }},
    ]});
    expect(c.userRows()[0].memory).toBeNull();
  });

  it('splits project vCPU-hours and memory GiB-hours into separate fields', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).usage.breakdown = (dim: string) => dim === 'project' ? ({available: true,
      group_by: 'project', rows: [{key: 'p1', label: 'Project One', events: 2, cost_usd: 0,
      units: {
        'prompt-token': {quantity: 50, cost_usd: 0, events: 1},
        'vcpu-hour': {quantity: 2, cost_usd: 0, events: 1},
        'gib-hour': {quantity: 8, cost_usd: 0, events: 1},
      }}]}) : null;
    const row = c.projectRows()[0];
    expect(row.tokens).toBe(50);
    expect(row.vcpu).toBe(2);
    expect(row.memory).toBe(8);
    expect((row as any).compute).toBeUndefined();
  });

  it('modelRows lists per-model token and cache columns', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).usage.breakdown = (dim: string) => dim === 'model' ? ({available: true,
      group_by: 'model', rows: [{key: 'gemma', label: 'gemma', events: 2, cost_usd: 0,
      cache_hit_ratio: 0.25,
      units: {'prompt-token': {quantity: 75, cost_usd: 0, events: 1},
              'cached-prompt-token': {quantity: 25, cost_usd: 0, events: 1},
              'completion-token': {quantity: 20, cost_usd: 0, events: 1}}}]}) : null;
    expect(c.modelRows()[0].prompt).toBe(100);
    expect(c.modelRows()[0].cached).toBe(25);
    expect(c.modelRows()[0].cacheHit).toBe(0.25);
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

  it('chart() builds stacked columns, legend and grand total for the active dim+metric', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).usage.timeseries = (dim: string) => dim === 'model' ? ({
      available: true, group_by: 'model', days: ['2026-06-01', '2026-06-02'],
      series: [
        {key: 'opus', label: 'Opus', events: 3, points: [
          {day: '2026-06-01', tokens: 100, cost_usd: 0, events: 2},
          {day: '2026-06-02', tokens: 60, cost_usd: 0, events: 1}]},
        {key: 'gemma', label: 'gemma', events: 1, points: [
          {day: '2026-06-01', tokens: 40, cost_usd: 0, events: 1}]},
      ],
    }) : null;
    const chart = c.chart()!;
    expect(chart).not.toBeNull();
    expect(chart.grandTotal).toBe(200); // tokens: 100+60+40
    expect(chart.grandLabel).toBe('200');
    expect(chart.legend.map((l) => l.key)).toEqual(['opus', 'gemma']); // by total desc
    expect(chart.bars.length).toBe(3); // day1: opus+gemma, day2: opus
  });

  it('chart() retotals when the metric toggles to events', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).usage.timeseries = () => ({
      available: true, group_by: 'model', days: ['2026-06-01'],
      series: [{key: 'opus', label: 'Opus', events: 2, points: [
        {day: '2026-06-01', tokens: 100, cost_usd: 0, events: 2}]}],
    });
    c.tsMetric.set('events');
    expect(c.chart()!.grandTotal).toBe(2);
  });

  it('donut() emits one segment per legend entry, offsets accumulating', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).usage.timeseries = () => ({
      available: true, group_by: 'model', days: ['2026-06-01'],
      series: [
        {key: 'a', label: 'A', events: 1, points: [{day: '2026-06-01', tokens: 75, cost_usd: 0, events: 1}]},
        {key: 'b', label: 'B', events: 1, points: [{day: '2026-06-01', tokens: 25, cost_usd: 0, events: 1}]},
      ],
    });
    const segs = c.donut();
    expect(segs.length).toBe(2);
    expect(segs[0].offset).toBe(0);
    expect(segs[1].offset).toBeLessThan(0); // advanced by A's 75% arc
  });

  it('chart() is null when the window has no series', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).usage.timeseries = () => ({available: true, group_by: 'model', days: [], series: []});
    expect(c.chart()).toBeNull();
  });

  it('scopeOverride reflects admin + the All-data switch', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).users.currentUser.set({id: 'a1', is_admin: true});
    c.viewAllData.set(true);
    expect(c.scopeOverride()).toBe('all');
    c.viewAllData.set(false);
    expect(c.scopeOverride()).toBe('mine');
  });

  it('scopeOverride is null for non-admins (already self-scoped)', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).users.currentUser.set({id: 'u1', is_admin: false});
    c.viewAllData.set(false);
    expect(c.scopeOverride()).toBeNull();
  });

  it('setViewAllData flips the signal and persists per-user to localStorage', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).users.currentUser.set({id: 'a1', is_admin: true});
    c.setViewAllData({target: {checked: false}} as unknown as Event);
    expect(c.viewAllData()).toBe(false);
    expect(localStorage.getItem('srw.usageViewAll.a1')).toBe('false');
  });
});
