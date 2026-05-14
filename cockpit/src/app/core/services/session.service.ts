import {computed, inject, Injectable, signal} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {firstValueFrom} from 'rxjs';
import {TranslocoService} from '@jsverse/transloco';
import {environment} from '../environment';

/**
 * Cookie BFF session surface.
 *
 * Replaces the previous keycloak-js wrapper. The orchestrator now issues an
 * HttpOnly `srw_session` cookie at `/auth/callback`; this service handles the
 * remaining browser-side actions: kicking off login (302 to KC via the BFF),
 * logging out (revoke server-side, then redirect to KC RP-initiated logout),
 * and forcing a token refresh (rare — used after role/group changes so the
 * next downstream call carries fresh claims).
 *
 * The session itself is observable only via `/auth/me` — the cookie is
 * HttpOnly, so JS can't read it directly. `authenticated` is therefore
 * derived from the presence of a loaded current-user payload, populated by
 * the APP_INITIALIZER in `app.config.ts`.
 */
@Injectable({providedIn: 'root'})
export class SessionService {
  private readonly http = inject(HttpClient);
  private readonly transloco = inject(TranslocoService);

  /** API base for cookie-authenticated calls (.../api). */
  private readonly apiUrl = environment.apiUrl;

  /** API origin (no `/api`) — `/auth/*` BFF endpoints live here. */
  private readonly apiOrigin = environment.apiUrl.replace(/\/api\/?$/, '');

  /** Set true once /auth/me succeeded during bootstrap (or after a successful refresh). */
  readonly authenticated = signal<boolean>(false);

  /** Convenience computed kept for parity with old call sites. */
  readonly isAuthenticated = computed(() => this.authenticated());

  /**
   * Redirect the browser to the BFF login endpoint.
   *
   * The orchestrator generates state+PKCE, sets `srw_pre_auth`, and 302s on
   * to Keycloak. After successful auth, KC redirects back to /auth/callback
   * which sets `srw_session` and 302s us to `return_to` inside the cockpit.
   */
  login(returnTo?: string): void {
    const target = returnTo ?? window.location.pathname + window.location.search;
    const url = `${this.apiOrigin}/auth/login`
      + `?return_to=${encodeURIComponent(target)}`
      + `&ui_locales=${encodeURIComponent(this.cockpitLocale())}`;
    window.location.href = url;
  }

  /**
   * Log out.
   *
   * Calls the BFF `/auth/logout` which revokes the server-side session and
   * returns a Keycloak RP-initiated logout URL (with `id_token_hint`
   * embedded so KC skips the confirmation screen). We then navigate the
   * browser to that URL; KC clears its SSO cookie and bounces back to the
   * cockpit, which will trigger the bootstrap → /auth/me → /auth/login loop
   * for re-login.
   */
  async logout(): Promise<void> {
    let kcLogoutUrl: string | null = null;
    try {
      const resp = await firstValueFrom(
        this.http.post<{ status: string; kc_logout_url: string | null }>(
          `${this.apiOrigin}/auth/logout`,
          {},
        ),
      );
      kcLogoutUrl = resp?.kc_logout_url ?? null;
    } catch {
      // Logout shouldn't fail loud — the cookie is already cleared
      // client-side by the response when the server-side delete succeeded,
      // and on transport failure we still want the user out of the UI.
    }
    this.authenticated.set(false);
    if (kcLogoutUrl) {
      window.location.href = kcLogoutUrl;
    } else {
      // Fallback: no KC URL (rare — KC unreachable, missing id_token).
      // Drop the user back at the BFF login screen which will redirect to
      // KC; if KC's SSO cookie is still valid, they'll be auto-logged-in
      // again, which is the lesser evil over a blank page.
      this.login('/');
    }
  }

  /**
   * Force-refresh the access token bound to the current session.
   *
   * Needed after server-side changes to the user's Keycloak groups (e.g.
   * project membership). Until the access token rolls forward, downstream
   * services that read group claims (OpenCloud's LibreGraph) won't see the
   * change. The transparent refresh in the validator handles the normal
   * case; this endpoint exists for the rare "refresh now" path.
   *
   * Non-fatal on failure — the next natural refresh or login picks up the
   * new claims.
   */
  async forceRefresh(): Promise<void> {
    try {
      await firstValueFrom(
        this.http.post(`${this.apiOrigin}/auth/refresh`, {}),
      );
    } catch {
      // Non-fatal — see docstring.
    }
  }

  private cockpitLocale(): string {
    const lang = this.transloco.getActiveLang();
    return lang === 'de-DE' || lang === 'de' ? 'de' : 'en';
  }
}
