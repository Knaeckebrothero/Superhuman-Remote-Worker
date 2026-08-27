import {describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {of, Subject, throwError} from 'rxjs';
import {AdminUsageService} from './admin-usage.service';
import {VIEW_AS_OVERRIDE} from '../interceptors/view-as.interceptor';

function createService(http: any): AdminUsageService {
  const injector = Injector.create({
    providers: [{provide: HttpClient, useValue: http}],
  });
  return runInInjectionContext(injector, () => new AdminUsageService());
}

describe('AdminUsageService', () => {
  it('loads usage from /usage and sets the signal', () => {
    const payload = {
      by_category: [
        {category: 'llm', unit: 'prompt-token', quantity: 100, cost_usd: 0, events: 5},
      ],
      total_cost_usd: 0,
      cache_hit_ratio: 0,
      available: true,
      from: 'a',
      to: 'b',
    };
    const http = {get: vi.fn().mockReturnValue(of(payload))};
    const service = createService(http);
    service.loadUsage(7);
    expect(http.get).toHaveBeenCalledWith(
      expect.stringContaining('/usage'),
      expect.objectContaining({params: expect.anything()}),
    );
    expect(service.usage()?.by_category.length).toBe(1);
    expect(service.usage()?.available).toBe(true);
    expect(service.loading()).toBe(false);
  });

  it('degrades to an empty, unavailable summary on error', () => {
    const http = {get: vi.fn().mockReturnValue(throwError(() => new Error('boom')))};
    const service = createService(http);
    service.loadUsage();
    expect(service.usage()).toEqual({
      by_category: [],
      total_cost_usd: 0,
      cache_hit_ratio: 0,
      cloud_estimates: [],
      available: false,
    });
    expect(service.loading()).toBe(false);
  });

  it('loads the decimal-safe v2 row contract without changing legacy state', () => {
    const payload = {
      schema_version: 2,
      window: {start: 'a', end: 'b', as_of: 'b', data_through: 'b'},
      rows: [{
        category: 'compute', measurement_basis: 'scheduler-request',
        cost_domain: 'workload-allocation', resource_class: 'kubernetes-pod',
        measurement_algorithm: 'fixture-v1', resource: 'workspace_pod',
        unit: 'vcpu-hour', attribution_scope: 'customer', quantity: '8',
        finalized_quantity: '8', confirmed_provisional_quantity: '0',
        unverified_projected_quantity: null,
        ledger_cost: {status: 'unpriced', currency: 'USD', amount: null,
          priced_quantity: '0', unpriced_quantity: '8'}, events: 1,
      }],
      coverage: {status: 'partial', includes_provisional: false,
        required_sources_ok: 0, required_sources_total: 0,
        unknown_ranges: [], excluded_domains: ['live-resource-inventory']},
    } as const;
    const http = {get: vi.fn().mockReturnValue(of(payload))};
    const service = createService(http);

    service.loadUsageV2(7, 'all', true);

    const [url, options] = http.get.mock.calls[0];
    expect(url).toContain('/usage/v2');
    expect(options.params.get('days')).toBe('7');
    expect(options.params.get('include_non_customer')).toBe('true');
    expect(options.context.get(VIEW_AS_OVERRIDE)).toBe('all');
    expect(service.usageV2()?.rows[0].quantity).toBe('8');
    expect(service.usage()).toBeNull();
  });

  it('clears stale v2 data and ignores an older window response', () => {
    const older = new Subject<any>();
    const newer = new Subject<any>();
    const http = {get: vi.fn().mockReturnValueOnce(older).mockReturnValueOnce(newer)};
    const service = createService(http);
    const payload = (quantity: string) => ({
      schema_version: 2,
      window: {start: 'a', end: 'b', as_of: 'b', data_through: null},
      rows: [{quantity}],
      coverage: {status: 'partial'},
    });

    service.loadUsageV2(7);
    service.loadUsageV2(30);
    expect(service.usageV2()).toBeNull();

    newer.next(payload('30'));
    older.next(payload('7'));
    expect(service.usageV2()?.rows[0].quantity).toBe('30');
  });

  it('passes the requested window as the days query param', () => {
    const http = {
      get: vi.fn().mockReturnValue(
        of({by_category: [], total_cost_usd: 0, cache_hit_ratio: 0, available: true}),
      ),
    };
    const service = createService(http);
    service.loadUsage(90);
    const params = http.get.mock.calls[0][1].params;
    expect(params.get('days')).toBe('90');
  });

  it('passes exact ISO windows with from_date and to_date query params', () => {
    const http = {
      get: vi.fn().mockReturnValue(
        of({by_category: [], total_cost_usd: 0, cache_hit_ratio: 0, available: true}),
      ),
    };
    const service = createService(http);
    service.loadUsage({
      days: 1,
      fromIso: '2026-07-08T00:00:00.000Z',
      toIso: '2026-07-08T08:00:00.000Z',
    });
    const params = http.get.mock.calls[0][1].params;
    expect(params.get('days')).toBe('1');
    expect(params.get('from_date')).toBe('2026-07-08T00:00:00.000Z');
    expect(params.get('to_date')).toBe('2026-07-08T08:00:00.000Z');
  });

  it('loadBreakdown populates the breakdown signal by groupBy', () => {
    const payload = {
      available: true,
      group_by: 'user',
      rows: [
        {key: 'u1', label: 'Alice', is_admin: true, events: 3, cost_usd: 0,
         units: {'prompt-token': {quantity: 100, cost_usd: 0, events: 2}}},
      ],
    };
    const http = {get: vi.fn().mockReturnValue(of(payload))};
    const service = createService(http);
    service.loadBreakdown('user', 30);
    const [url, options] = http.get.mock.calls[0];
    expect(url).toContain('/usage/breakdown');
    expect(options.params.get('group_by')).toBe('user');
    expect(options.params.get('days')).toBe('30');
    expect(service.breakdown('user')?.rows[0].label).toBe('Alice');
  });

  it('loadTimeseries populates the timeseries signal by groupBy', () => {
    const payload = {
      available: true,
      group_by: 'model',
      days: ['2026-06-01', '2026-06-02'],
      series: [
        {key: 'opus', label: 'Opus', events: 3, points: [
          {day: '2026-06-01', tokens: 100, cost_usd: 0, events: 2},
          {day: '2026-06-02', tokens: 50, cost_usd: 0, events: 1}]},
      ],
    };
    const http = {get: vi.fn().mockReturnValue(of(payload))};
    const service = createService(http);
    service.loadTimeseries('model', 30);
    const [url, options] = http.get.mock.calls[0];
    expect(url).toContain('/usage/timeseries');
    expect(options.params.get('group_by')).toBe('model');
    expect(options.params.get('days')).toBe('30');
    expect(service.timeseries('model')?.series[0].label).toBe('Opus');
  });

  it('degrades timeseries to an empty, unavailable series on error', () => {
    const http = {get: vi.fn().mockReturnValue(throwError(() => new Error('boom')))};
    const service = createService(http);
    service.loadTimeseries('user');
    expect(service.timeseries('user')?.available).toBe(false);
    expect(service.timeseries('user')?.series).toEqual([]);
  });

  it('forwards the page scope override on the request HttpContext', () => {
    const http = {get: vi.fn().mockReturnValue(of({available: true, group_by: 'model', days: [], series: []}))};
    const service = createService(http);
    service.loadTimeseries('model', 30, 'all');
    const options = http.get.mock.calls[0][1];
    expect(options.context.get(VIEW_AS_OVERRIDE)).toBe('all');
  });

  it('defaults the scope override to null (defer to the global view-as toggle)', () => {
    const http = {get: vi.fn().mockReturnValue(of({
      by_category: [],
      total_cost_usd: 0,
      cache_hit_ratio: 0,
      available: true,
    }))};
    const service = createService(http);
    service.loadUsage(30);
    const options = http.get.mock.calls[0][1];
    expect(options.context.get(VIEW_AS_OVERRIDE)).toBeNull();
  });
});
