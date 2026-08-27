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
 * knowledge-base/knowledge/issues/cockpit_service_worker_breaks_file_uploads.md.
 *
 * Since Slice 2 the header is load-bearing for a SECOND, independent reason:
 * a service worker that answers with respondWith() destroys XHR upload
 * progress, and the send bubble's percentage is computed from exactly those
 * events. Pinned below on the uploads URL.
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
    run(new HttpRequest('POST', apiUrl('/persistent/threads/t1/input'), {content: 'hi'})).subscribe();

    const fwd = forwarded(next);
    expect(fwd.headers.has('ngsw-bypass')).toBe(true);
    // X-CSRF must survive alongside it — both ride the same clone.
    expect(fwd.headers.get('X-CSRF')).toBe('1');
  });

  it('stamps ngsw-bypass on thread uploads — the SW both corrupts multipart bodies and kills upload progress', () => {
    // Two independent reasons, and only the first is recorded anywhere else:
    //   1. ngsw re-issues intercepted requests via scope.fetch(), which
    //      corrupts multipart bodies (the net::ERR_FAILED outage in
    //      knowledge-history/done/cockpit_service_worker_breaks_file_uploads.md).
    //   2. A service worker that calls respondWith() destroys XHR upload
    //      progress outright: the browser reports progress on the request it
    //      actually sends, and the SW's re-issued fetch is not that request.
    //      Since Slice 2 the send bubble renders a real percentage off those
    //      events, so dropping this header would silently flatten every
    //      attachment upload back to an opaque indeterminate wait.
    // Pinned on the uploads URL specifically so a future narrowing of the
    // bypass (e.g. "only JSON mutations need it") fails here first.
    const {run, next} = setup();
    run(new HttpRequest('POST', apiUrl('/persistent/threads/t1/uploads'), new FormData())).subscribe();

    expect(forwarded(next).headers.get('ngsw-bypass')).toBe('1');
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
