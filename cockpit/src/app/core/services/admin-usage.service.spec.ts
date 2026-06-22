import {describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {of, throwError} from 'rxjs';
import {AdminUsageService} from './admin-usage.service';

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
});
