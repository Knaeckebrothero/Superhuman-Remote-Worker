import {inject, Injectable} from '@angular/core';
import {HttpErrorResponse} from '@angular/common/http';
import {TranslocoService} from '@jsverse/transloco';

/**
 * Translates HTTP/runtime errors into user-facing messages.
 *
 * Resolution order:
 *   1. `error.code` (structured code from the orchestrator, e.g. `job.not_found`)
 *      looked up under `errors.code.<code>`
 *   2. A structured `error.detail` the server chose to send (see below)
 *   3. HTTP status → `errors.http.<status>` (generic per-status message)
 *   4. Caller-provided fallback key
 *   5. Raw `error.detail` text for anything else
 *   6. `errors.unknown`
 *
 * Step 2 exists because a refusal is the one case where the server knows
 * something the client cannot infer, and `errors.http.4xx` is defined for every
 * 4xx — so a generic "The request couldn't be completed." always won and the
 * explanation below it was unreachable. That is useless on a form the user is
 * meant to correct: "Lite session backends cannot attach clone-based repository
 * connectors" says what to change; the generic string says nothing.
 *
 * Two guards keep this from leaking noise:
 *   - Only STRUCTURED details qualify (a JSON body with `detail`). A proxy
 *     serving an HTML 502/503 arrives as a string body and stays generic —
 *     otherwise a Traefik error page would be rendered as the message.
 *   - Only refusal statuses: every 4xx, plus 503, which this API uses for
 *     deliberate "this deployment can't do that" answers (readiness gate,
 *     "VM provisioning is not available on this deployment"). 500/502/504 keep
 *     the generic line — `create_thread`'s catch-all raises
 *     `HTTPException(500, detail=str(e))`, i.e. raw internal exception text.
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

    // A refusal detail is the server telling the user what to fix. Prefer it
    // over the generic per-status line, which would otherwise always shadow it.
    if (info.structuredDetail && this.isRefusal(info.status)) {
      return info.structuredDetail;
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

  /** A status where the server is refusing on purpose and can say why. */
  private isRefusal(status: number): boolean {
    return (status >= 400 && status < 500) || status === 503;
  }

  private extract(err: unknown): {
    status: number;
    code?: string;
    detail?: string;
    /** Set only when `detail` came from a JSON body, i.e. from this API and
     *  not from a gateway's HTML error page. */
    structuredDetail?: string;
  } {
    if (err instanceof HttpErrorResponse) {
      const body = err.error as {code?: string; detail?: string} | string | null;
      if (body && typeof body === 'object') {
        return {
          status: err.status,
          code: body.code,
          detail: body.detail,
          structuredDetail: body.detail,
        };
      }
      return {status: err.status, detail: typeof body === 'string' ? body : undefined};
    }
    const maybe = err as {status?: number; error?: {code?: string; detail?: string}} | null;
    if (maybe && typeof maybe === 'object') {
      return {
        status: maybe.status ?? 0,
        code: maybe.error?.code,
        detail: maybe.error?.detail,
        structuredDetail: maybe.error?.detail,
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
