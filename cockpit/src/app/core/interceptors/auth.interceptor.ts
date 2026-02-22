import { HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { ApiService } from '../services/api.service';

/**
 * HTTP interceptor that:
 * 1. Sets withCredentials on all requests (sends session cookie)
 * 2. Attaches X-CSRF-Token header from the ApiService
 */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const api = inject(ApiService);

  let authReq = req.clone({ withCredentials: true });

  const csrfToken = api.csrfToken;
  if (csrfToken && !req.headers.has('X-CSRF-Token')) {
    authReq = authReq.clone({
      headers: authReq.headers.set('X-CSRF-Token', csrfToken),
    });
  }

  return next(authReq);
};
