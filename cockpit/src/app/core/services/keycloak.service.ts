import { inject, Injectable, PLATFORM_ID } from '@angular/core';
import { isPlatformBrowser } from '@angular/common';
import Keycloak from 'keycloak-js';
import { environment } from '../environment';

/**
 * Thin wrapper around keycloak-js.
 *
 * Initialised via APP_INITIALIZER before the Angular app boots, so
 * `authenticated` is resolved before any component renders.
 */
@Injectable({ providedIn: 'root' })
export class KeycloakService {
  private readonly platformId = inject(PLATFORM_ID);
  private keycloak!: Keycloak;
  private _authenticated = false;

  /** Whether the user has a valid Keycloak session. */
  get authenticated(): boolean {
    return this._authenticated;
  }

  /**
   * Initialise the Keycloak adapter (called from APP_INITIALIZER).
   * Uses check-sso to silently detect existing sessions without forcing
   * a redirect on every page load.
   */
  async init(): Promise<void> {
    if (!isPlatformBrowser(this.platformId)) return;

    this.keycloak = new Keycloak({
      url: environment.keycloakUrl,
      realm: environment.keycloakRealm,
      clientId: environment.keycloakClientId,
    });

    try {
      this._authenticated = await this.keycloak.init({
        onLoad: 'check-sso',
        pkceMethod: 'S256',
        silentCheckSsoRedirectUri: `${window.location.origin}/assets/silent-check-sso.html`,
        checkLoginIframe: false,
      });
    } catch (err) {
      console.error('Keycloak init failed', err);
      this._authenticated = false;
    }

    // Auto-refresh token when it's about to expire
    this.keycloak.onTokenExpired = () => {
      this.keycloak.updateToken(30).catch(() => {
        console.warn('Token refresh failed — session expired');
        this._authenticated = false;
      });
    };
  }

  /** Redirect to Keycloak login page. */
  login(): void {
    this.keycloak.login({ redirectUri: window.location.href });
  }

  /** Redirect to Keycloak logout endpoint. */
  logout(): void {
    this._authenticated = false;
    this.keycloak.logout({ redirectUri: window.location.origin });
  }

  /**
   * Get the current access token, refreshing it if it expires within 30s.
   * Returns null if not authenticated.
   */
  async getToken(): Promise<string | null> {
    if (!this._authenticated) return null;
    try {
      await this.keycloak.updateToken(30);
      return this.keycloak.token ?? null;
    } catch {
      this._authenticated = false;
      return null;
    }
  }
}
