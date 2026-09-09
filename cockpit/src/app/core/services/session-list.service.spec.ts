import {describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {of, throwError} from 'rxjs';
import {SessionListService} from './session-list.service';
import type {Thread} from '../models/api.model';

function thread(id: string, lastActivity: string): Thread {
  return {
    id, title: `Session ${id}`, status: 'active', config_name: 'session_base',
    permission_mode: 'autonomous', created_at: lastActivity, last_activity: lastActivity,
  } as Thread;
}

function create(threads: Thread[]) {
  const http = {get: vi.fn(() => of({threads}))};
  const injector = Injector.create({providers: [{provide: HttpClient, useValue: http}]});
  const service = runInInjectionContext(injector, () => new SessionListService());
  return {service, http};
}

describe('SessionListService', () => {
  it('exposes threads after refresh', async () => {
    const {service} = create([thread('a', new Date().toISOString())]);
    await service.refresh();
    expect(service.threads().map((t) => t.id)).toEqual(['a']);
  });

  it('groups by recency into today, yesterday and earlier', async () => {
    const now = new Date();
    const yesterday = new Date(now.getTime() - 26 * 3600_000);
    const lastWeek = new Date(now.getTime() - 8 * 24 * 3600_000);
    const {service} = create([
      thread('t', now.toISOString()),
      thread('y', yesterday.toISOString()),
      thread('e', lastWeek.toISOString()),
    ]);
    await service.refresh();
    expect(service.grouped().map((g) => g.label)).toEqual(['today', 'yesterday', 'earlier']);
    expect(service.grouped()[0].threads.map((t) => t.id)).toEqual(['t']);
  });

  it('omits a group with no threads', async () => {
    const {service} = create([thread('t', new Date().toISOString())]);
    await service.refresh();
    expect(service.grouped().map((g) => g.label)).toEqual(['today']);
  });

  it('resolves rather than rejects when the HTTP call errors, so fire-and-forget callers never see an unhandled rejection', async () => {
    const http = {get: vi.fn(() => throwError(() => new Error('boom')))};
    const injector = Injector.create({providers: [{provide: HttpClient, useValue: http}]});
    const service = runInInjectionContext(injector, () => new SessionListService());

    await expect(service.refresh()).resolves.toBeUndefined();

    expect(service.threads()).toEqual([]);
    expect(service.loading()).toBe(false);
  });
});
