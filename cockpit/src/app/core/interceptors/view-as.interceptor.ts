import {
  HttpContextToken,
  HttpEvent,
  HttpHandlerFn,
  HttpInterceptorFn,
  HttpRequest,
} from '@angular/common/http';
import {inject} from '@angular/core';
import {Observable} from 'rxjs';
import {environment} from '../environment';
import {UserService} from '../services/user.service';
import {ViewModeService} from '../services/view-mode.service';

/**
 * Admin "View as user" header injector.
 *
 * Adds `X-Admin-View-As: user` to orchestrator requests when the active
 * user is an admin AND the persisted toggle is `'me'`. The orchestrator's
 * `require_approved_user` interprets the header by flipping `is_admin`
 * to false on the resolved user dict, so visibility helpers narrow as if
 * the caller were a regular user. Non-admins never get the header — for
 * them the backend already treats it as a no-op, but skipping the header
 * keeps audit logs cleaner.
 *
 * A request can override the global toggle per-call via `VIEW_AS_OVERRIDE`
 * in its `HttpContext` (used by the Admin→Usage page's "All data" switch):
 * `'all'` forces the fleet view (never send the header, even in `'me'` mode),
 * `'mine'` forces self-scope (send the header for an admin, even in `'all'`
 * mode), and the default `null` defers to `ViewModeService`. This lets one
 * page scope itself without touching the account-wide setting or the backend.
 *
 * Must register AFTER `authInterceptor` in `app.config.ts`: cookies +
 * CSRF first, then the view-mode header.
 *
 * Design: `knowledge-base/knowledge/features/admin_view_as_user.md`.
 */
export const VIEW_AS_HEADER = 'X-Admin-View-As';
export const VIEW_AS_USER = 'user';

/** Per-request scope override; defaults to `null` (defer to the global toggle). */
export const VIEW_AS_OVERRIDE = new HttpContextToken<'all' | 'mine' | null>(() => null);

export const viewAsInterceptor: HttpInterceptorFn = (
  req: HttpRequest<unknown>,
  next: HttpHandlerFn,
): Observable<HttpEvent<unknown>> => {
  if (!isOrchestratorRequest(req.url)) {
    return next(req);
  }

  const override = req.context.get(VIEW_AS_OVERRIDE);
  const isAdmin = inject(UserService).currentUser()?.is_admin === true;

  let selfScope: boolean;
  if (override === 'all') {
    selfScope = false; // force fleet — ignore the global toggle
  } else if (override === 'mine') {
    selfScope = isAdmin; // force self for admins; a no-op header for non-admins
  } else {
    if (!isAdmin) {
      return next(req);
    }
    selfScope = inject(ViewModeService).viewMode() === 'me';
  }

  if (!selfScope) {
    return next(req);
  }

  return next(
    req.clone({
      headers: req.headers.set(VIEW_AS_HEADER, VIEW_AS_USER),
    }),
  );
};

function isOrchestratorRequest(url: string): boolean {
  if (url.startsWith('/')) return true;
  return url.startsWith(environment.apiUrl) || url.startsWith(apiOrigin());
}

function apiOrigin(): string {
  return environment.apiUrl.replace(/\/api\/?$/, '');
}
