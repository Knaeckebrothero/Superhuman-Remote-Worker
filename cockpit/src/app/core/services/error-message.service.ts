import {inject, Injectable} from '@angular/core';
import {HttpErrorResponse} from '@angular/common/http';
import {TranslocoService} from '@jsverse/transloco';

/**
 * Translates HTTP/runtime errors into user-facing messages.
 *
 * Resolution order:
 *   1. `error.code` (structured code from the orchestrator, e.g. `job.not_found`)
 *      looked up under `errors.code.<code>`
 *   2. HTTP status → `errors.http.<status>` (generic per-status message)
 *   3. Caller-provided fallback key
 *   4. Raw `error.detail` text (English, as emitted by the server)
 *   5. `errors.unknown`
 */
@Injectable({providedIn: 'root'})
export class ErrorMessageService {
  private readonly transloco = inject(TranslocoService);

  translate(err: unknown, fallbackKey?: string, params?: Record<string, unknown>): string {
    const info = this.extract(err);

    if (info.code) {
      const key = `errors.code.${info.code}`;
      const translated = this.transloco.translate(key, params);
      if (translated !== key) return translated;
    }

    const statusKey = this.statusKey(info.status);
    if (statusKey) {
      const translated = this.transloco.translate(statusKey, params);
      if (translated !== statusKey) return translated;
    }

    if (fallbackKey) {
      const translated = this.transloco.translate(fallbackKey, params);
      if (translated !== fallbackKey) return translated;
    }

    if (info.detail) return info.detail;

    return this.transloco.translate('errors.unknown');
  }

  private extract(err: unknown): {status: number; code?: string; detail?: string} {
    if (err instanceof HttpErrorResponse) {
      const body = err.error as {code?: string; detail?: string} | string | null;
      if (body && typeof body === 'object') {
        return {status: err.status, code: body.code, detail: body.detail};
      }
      return {status: err.status, detail: typeof body === 'string' ? body : undefined};
    }
    const maybe = err as {status?: number; error?: {code?: string; detail?: string}} | null;
    if (maybe && typeof maybe === 'object') {
      return {
        status: maybe.status ?? 0,
        code: maybe.error?.code,
        detail: maybe.error?.detail,
      };
    }
    return {status: 0};
  }

  private statusKey(status: number): string | null {
    if (status === 0) return 'errors.http.network';
    if (status === 401) return 'errors.http.401';
    if (status === 403) return 'errors.http.403';
    if (status === 404) return 'errors.http.404';
    if (status === 408 || status === 504) return 'errors.http.timeout';
    if (status === 409) return 'errors.http.409';
    if (status === 429) return 'errors.http.429';
    if (status >= 500) return 'errors.http.5xx';
    if (status >= 400) return 'errors.http.4xx';
    return null;
  }
}
