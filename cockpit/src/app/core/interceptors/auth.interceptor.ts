import {HttpEvent, HttpHandlerFn, HttpInterceptorFn, HttpRequest} from '@angular/common/http';
import {inject} from '@angular/core';
import {Observable, catchError, throwError} from 'rxjs';
import {environment} from '../environment';
import {SessionService} from '../services/session.service';

/**
 * Cookie BFF interceptor.
 *
 * Replaces the previous Keycloak Bearer-in-memory model. For every request
 * to the orchestrator origin (`environment.apiUrl`):
 *   - Sets `withCredentials: true` so the HttpOnly `srw_session` cookie
 *     rides along.
 *   - Adds `X-CSRF: 1` on non-safe methods. The orchestrator's CSRF
 *     middleware requires it (alongside `Sec-Fetch-Site != cross-site`)
 *     before accepting state-changing cookie-authenticated requests.
 *   - Adds `ngsw-bypass: 1` on non-safe methods so the Angular service
 *     worker hands them to the browser untouched (its re-issue breaks
 *     multipart uploads; mutations are never SW-cached anyway).
 *   - On 401, redirects to the BFF login endpoint with `return_to` set to
 *     the current URL. The orchestrator either honors an existing KC SSO
 *     session and bounces back authenticated, or shows the KC login form.
 *
 * Requests to other origins (Keycloak's `/realms/`, OpenCloud, MCP) are
 * passed through unmodified — the SPA no longer manages tokens for them.
 */

let isRedirecting = false;

export const authInterceptor: HttpInterceptorFn = (
  req: HttpRequest<unknown>,
  next: HttpHandlerFn,
): Observable<HttpEvent<unknown>> => {
  const session = inject(SessionService);

  if (!isOrchestratorRequest(req.url)) {
    return next(req);
  }

  const isSafe = req.method === 'GET' || req.method === 'HEAD' || req.method === 'OPTIONS';

  let modified = req.clone({withCredentials: true});
  if (!isSafe) {
    // ngsw-bypass keeps the Angular service worker from re-issuing the
    // request via scope.fetch() — that re-issue corrupts multipart bodies
    // (file uploads die with net::ERR_FAILED) and buys nothing since ngsw
    // never caches non-GET/HEAD anyway. It is ngsw's only real opt-out;
    // dataGroup config only controls caching, not handling. See
    // knowledge-base/knowledge/issues/cockpit_service_worker_breaks_file_uploads.md.
    modified = modified.clone({
      headers: modified.headers.set('X-CSRF', '1').set('ngsw-bypass', '1'),
    });
  }

  return next(modified).pipe(
    catchError((err) => {
      if (err?.status === 401 && !isAuthEndpoint(req.url) && !isRedirecting) {
        isRedirecting = true;
        session.login();
      }
      return throwError(() => err);
    }),
  );
};

function isOrchestratorRequest(url: string): boolean {
  if (url.startsWith('/')) return true;
  return url.startsWith(environment.apiUrl) || url.startsWith(apiOrigin());
}

function isAuthEndpoint(url: string): boolean {
  // Only suppress the 401-redirect for /auth/logout — a 401 there means the
  // cookie was already invalid, and we'd race the manual KC logout redirect.
  // /auth/me 401 should redirect (bootstrap entry); /auth/refresh 401 should
  // redirect (session is dead, force re-login).
  const origin = apiOrigin();
  return url.startsWith(`${origin}/auth/logout`) || url.startsWith('/auth/logout');
}

function apiOrigin(): string {
  return environment.apiUrl.replace(/\/api\/?$/, '');
}
