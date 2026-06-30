import {describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {of, throwError} from 'rxjs';
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
      available: false,
    });
    expect(service.loading()).toBe(false);
  });

  it('passes the requested window as the days query param', () => {
    const http = {
      get: vi.fn().mockReturnValue(
        of({by_category: [], total_cost_usd: 0, available: true}),
      ),
    };
    const service = createService(http);
    service.loadUsage(90);
    const params = http.get.mock.calls[0][1].params;
    expect(params.get('days')).toBe('90');
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
    const http = {get: vi.fn().mockReturnValue(of({by_category: [], total_cost_usd: 0, available: true}))};
    const service = createService(http);
    service.loadUsage(30);
    const options = http.get.mock.calls[0][1];
    expect(options.context.get(VIEW_AS_OVERRIDE)).toBeNull();
  });
});
