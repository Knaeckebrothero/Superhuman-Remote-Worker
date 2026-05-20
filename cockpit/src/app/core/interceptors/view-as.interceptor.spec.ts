import {describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext, signal} from '@angular/core';
import {HttpRequest, HttpResponse} from '@angular/common/http';
import {of} from 'rxjs';
import {UserService} from '../services/user.service';
import {ViewModeService} from '../services/view-mode.service';
import {environment} from '../environment';
import {VIEW_AS_HEADER, VIEW_AS_USER, viewAsInterceptor} from './view-as.interceptor';
import {User} from '../models/api.model';

/**
 * Spec for `viewAsInterceptor`. Uses `Injector.create` + manual mocks
 * matching the pattern in `admin-providers.service.spec.ts`. The
 * interceptor is a pure function over its three injections, so we don't
 * need TestBed/effects here.
 */

const ADMIN_USER = {id: 'admin-1', is_admin: true, is_approved: true} as unknown as User;
const REGULAR_USER = {id: 'user-1', is_admin: false, is_approved: true} as unknown as User;

function setup(opts: {
  user?: User | null;
  viewMode?: 'me' | 'all';
}) {
  const currentUser = signal<User | null>(opts.user ?? null);
  const viewMode = signal<'me' | 'all'>(opts.viewMode ?? 'all');

  const userService = {currentUser} as unknown as UserService;
  const viewModeService = {viewMode} as unknown as ViewModeService;

  const injector = Injector.create({
    providers: [
      {provide: UserService, useValue: userService},
      {provide: ViewModeService, useValue: viewModeService},
    ],
  });

  const next = vi.fn((req: HttpRequest<unknown>) => of(new HttpResponse({status: 200, url: req.url})));

  const run = (req: HttpRequest<unknown>) =>
    runInInjectionContext(injector, () => viewAsInterceptor(req, next));

  return {run, next};
}

describe('viewAsInterceptor', () => {
  const apiReq = (path = '/jobs', method: 'GET' | 'POST' = 'GET') =>
    new HttpRequest(method, `${environment.apiUrl}${path}`, method === 'POST' ? {} : null);

  it("adds the header for an admin in 'me' mode hitting the orchestrator", () => {
    const {run, next} = setup({user: ADMIN_USER, viewMode: 'me'});
    run(apiReq()).subscribe();

    expect(next).toHaveBeenCalledOnce();
    const fwd = next.mock.calls[0]![0] as HttpRequest<unknown>;
    expect(fwd.headers.get(VIEW_AS_HEADER)).toBe(VIEW_AS_USER);
  });

  it("does NOT add the header when toggle is 'all'", () => {
    const {run, next} = setup({user: ADMIN_USER, viewMode: 'all'});
    run(apiReq()).subscribe();

    const fwd = next.mock.calls[0]![0] as HttpRequest<unknown>;
    expect(fwd.headers.has(VIEW_AS_HEADER)).toBe(false);
  });

  it('does NOT add the header for a non-admin user (even if their localStorage somehow has me)', () => {
    const {run, next} = setup({user: REGULAR_USER, viewMode: 'me'});
    run(apiReq()).subscribe();

    const fwd = next.mock.calls[0]![0] as HttpRequest<unknown>;
    expect(fwd.headers.has(VIEW_AS_HEADER)).toBe(false);
  });

  it('does NOT add the header when no user is logged in', () => {
    const {run, next} = setup({user: null, viewMode: 'me'});
    run(apiReq()).subscribe();

    const fwd = next.mock.calls[0]![0] as HttpRequest<unknown>;
    expect(fwd.headers.has(VIEW_AS_HEADER)).toBe(false);
  });

  it('passes through cross-origin requests unmodified', () => {
    const {run, next} = setup({user: ADMIN_USER, viewMode: 'me'});
    const req = new HttpRequest('GET', 'https://auth.example.com/realms/srw/.well-known');
    run(req).subscribe();

    const fwd = next.mock.calls[0]![0] as HttpRequest<unknown>;
    expect(fwd.headers.has(VIEW_AS_HEADER)).toBe(false);
    expect(fwd).toBe(req);
  });

  it('handles relative orchestrator URLs (/api/jobs)', () => {
    const {run, next} = setup({user: ADMIN_USER, viewMode: 'me'});
    const req = new HttpRequest('GET', '/api/jobs');
    run(req).subscribe();

    const fwd = next.mock.calls[0]![0] as HttpRequest<unknown>;
    expect(fwd.headers.get(VIEW_AS_HEADER)).toBe(VIEW_AS_USER);
  });

  it('applies to non-safe methods too (POST etc.)', () => {
    const {run, next} = setup({user: ADMIN_USER, viewMode: 'me'});
    run(apiReq('/projects', 'POST')).subscribe();

    const fwd = next.mock.calls[0]![0] as HttpRequest<unknown>;
    expect(fwd.headers.get(VIEW_AS_HEADER)).toBe(VIEW_AS_USER);
  });

  it("preserves existing headers when injecting (clone, don't replace)", () => {
    const {run, next} = setup({user: ADMIN_USER, viewMode: 'me'});
    const req = new HttpRequest('POST', `${environment.apiUrl}/projects`, {}, {
      headers: new HttpRequest('POST', '/dummy', {}).headers.set('X-CSRF', '1'),
    });
    run(req).subscribe();

    const fwd = next.mock.calls[0]![0] as HttpRequest<unknown>;
    expect(fwd.headers.get('X-CSRF')).toBe('1');
    expect(fwd.headers.get(VIEW_AS_HEADER)).toBe(VIEW_AS_USER);
  });
});
