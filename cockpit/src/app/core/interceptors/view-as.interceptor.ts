import {HttpEvent, HttpHandlerFn, HttpInterceptorFn, HttpRequest} from '@angular/common/http';
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
 * Must register AFTER `authInterceptor` in `app.config.ts`: cookies +
 * CSRF first, then the view-mode header.
 *
 * Design: `docs/features/admin_view_as_user.md`.
 */
export const VIEW_AS_HEADER = 'X-Admin-View-As';
export const VIEW_AS_USER = 'user';

export const viewAsInterceptor: HttpInterceptorFn = (
  req: HttpRequest<unknown>,
  next: HttpHandlerFn,
): Observable<HttpEvent<unknown>> => {
  if (!isOrchestratorRequest(req.url)) {
    return next(req);
  }

  const userService = inject(UserService);
  if (!userService.currentUser()?.is_admin) {
    return next(req);
  }

  const viewMode = inject(ViewModeService);
  if (viewMode.viewMode() !== 'me') {
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
