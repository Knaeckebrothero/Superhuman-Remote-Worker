import {describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {HttpRequest, HttpResponse} from '@angular/common/http';
import {of} from 'rxjs';
import {SessionService} from '../services/session.service';
import {environment} from '../environment';
import {authInterceptor} from './auth.interceptor';

/**
 * Spec for `authInterceptor`. Same `Injector.create` + manual-mock pattern
 * as `view-as.interceptor.spec.ts`.
 *
 * The `ngsw-bypass` cases pin the service-worker opt-out for mutations:
 * the Angular SW re-issues every intercepted request via `scope.fetch()`,
 * which corrupts multipart bodies (file uploads fail with net::ERR_FAILED).
 * `ngsw-bypass` is ngsw's only real opt-out — dataGroup config only
 * controls caching, not handling. See
 * docs/issues/cockpit_service_worker_breaks_file_uploads.md.
 */

function setup() {
  const session = {login: vi.fn()} as unknown as SessionService;

  const injector = Injector.create({
    providers: [{provide: SessionService, useValue: session}],
  });

  const next = vi.fn((req: HttpRequest<unknown>) =>
    of(new HttpResponse({status: 200, url: req.url})),
  );

  const run = (req: HttpRequest<unknown>) =>
    runInInjectionContext(injector, () => authInterceptor(req, next));

  return {run, next, session};
}

function forwarded(next: ReturnType<typeof setup>['next']): HttpRequest<unknown> {
  return next.mock.calls[0]![0] as HttpRequest<unknown>;
}

describe('authInterceptor', () => {
  const apiUrl = (path: string) => `${environment.apiUrl}${path}`;

  it('sets withCredentials on orchestrator requests', () => {
    const {run, next} = setup();
    run(new HttpRequest('GET', apiUrl('/jobs'))).subscribe();

    expect(forwarded(next).withCredentials).toBe(true);
  });

  it('adds X-CSRF on non-safe methods', () => {
    const {run, next} = setup();
    run(new HttpRequest('POST', apiUrl('/projects'), {})).subscribe();

    expect(forwarded(next).headers.get('X-CSRF')).toBe('1');
  });

  it('adds ngsw-bypass on non-safe methods so the service worker never re-issues mutations', () => {
    const {run, next} = setup();
    run(new HttpRequest('POST', apiUrl('/persistent/threads/t1/uploads'), new FormData())).subscribe();

    const fwd = forwarded(next);
    expect(fwd.headers.has('ngsw-bypass')).toBe(true);
    // X-CSRF must survive alongside it — both ride the same clone.
    expect(fwd.headers.get('X-CSRF')).toBe('1');
  });

  it('adds ngsw-bypass on DELETE too (all non-safe methods, not just POST)', () => {
    const {run, next} = setup();
    run(new HttpRequest('DELETE', apiUrl('/projects/p1'))).subscribe();

    expect(forwarded(next).headers.has('ngsw-bypass')).toBe(true);
  });

  it('does NOT add ngsw-bypass or X-CSRF on GET (cacheable reads stay SW-managed for the PWA)', () => {
    const {run, next} = setup();
    run(new HttpRequest('GET', apiUrl('/jobs'))).subscribe();

    const fwd = forwarded(next);
    expect(fwd.headers.has('ngsw-bypass')).toBe(false);
    expect(fwd.headers.has('X-CSRF')).toBe(false);
  });

  it('passes through non-orchestrator requests unmodified', () => {
    const {run, next} = setup();
    const req = new HttpRequest('POST', 'https://auth.example.com/realms/srw/token', {});
    run(req).subscribe();

    const fwd = forwarded(next);
    expect(fwd).toBe(req);
    expect(fwd.headers.has('ngsw-bypass')).toBe(false);
    expect(fwd.withCredentials).toBe(false);
  });
});
