import { inject, Injectable, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { catchError, Observable, of, tap } from 'rxjs';
import {
  ApiKeyEntry,
  ApiKeySetRequest,
  ResolvedDefaults,
  SubscriptionAccount,
  SubscriptionLogin,
  SubscriptionsStatus,
  SubscriptionUsage,
  UserSettings,
} from '../models/api.model';
import { environment } from '../environment';
import { AdminProvidersService } from './admin-providers.service';
import { ReadinessService } from './readiness.service';

@Injectable({ providedIn: 'root' })
export class SettingsService {
  private readonly http = inject(HttpClient);
  private readonly readiness = inject(ReadinessService);
  private readonly adminProviders = inject(AdminProvidersService);
  private readonly baseUrl = environment.apiUrl;

  /** Current user's API keys (prefix only, no full keys). */
  readonly apiKeys = signal<ApiKeyEntry[]>([]);

  /** Current user's preference settings (user overrides only). */
  readonly preferences = signal<UserSettings>({});

  /** Resolved framework/env defaults for every preference field. */
  readonly resolvedDefaults = signal<ResolvedDefaults>({});

  // ── User API Keys ──────────────────────────────────────────────────

  loadApiKeys(): void {
    this.http
      .get<ApiKeyEntry[]>(`${this.baseUrl}/settings/api-keys`)
      .pipe(catchError(() => of([])))
      .subscribe((keys) => this.apiKeys.set(keys));
  }

  setApiKey(provider: string, body: ApiKeySetRequest): Observable<ApiKeyEntry> {
    return this.http
      .put<ApiKeyEntry>(`${this.baseUrl}/settings/api-keys/${provider}`, body)
      .pipe(tap(() => this.loadApiKeys()));
  }

  deleteApiKey(provider: string): Observable<{ status: string }> {
    return this.http
      .delete<{ status: string }>(`${this.baseUrl}/settings/api-keys/${provider}`)
      .pipe(tap(() => this.loadApiKeys()));
  }

  // ── User Preferences ──────────────────────────────────────────────

  loadPreferences(): void {
    this.http
      .get<UserSettings>(`${this.baseUrl}/settings/preferences`)
      .pipe(catchError(() => of({} as UserSettings)))
      .subscribe((prefs) => {
        const resolved = prefs._resolved ?? {};
        delete prefs._resolved;
        this.resolvedDefaults.set(resolved);
        this.preferences.set(prefs);
      });
  }

  updatePreferences(settings: Partial<UserSettings>): Observable<{ status: string }> {
    return this.http
      .patch<{ status: string }>(`${this.baseUrl}/settings/preferences`, settings)
      .pipe(tap(() => this.loadPreferences()));
  }

  // Per-project LLM provider keys (`/api/projects/{id}/api-keys`) have no
  // cockpit surface; the endpoints and their place in the system > project >
  // user precedence chain are exercised at dispatch, not from the browser.

  // ── AI Subscriptions (Admin) ──────────────────────────────────
  //
  // One surface for every subscription product the proxy can sign in to.
  // The orchestrator owns the provider registry, the login sessions and the
  // management credential; the browser only ever sees an SRW login id.

  /** Proxy reachability, supported providers and connected accounts. */
  getSubscriptionsStatus(): Observable<SubscriptionsStatus> {
    return this.http.get<SubscriptionsStatus>(`${this.baseUrl}/subscriptions/status`).pipe(
      catchError(() =>
        of<SubscriptionsStatus>({
          reachable: false,
          connected: false,
          proxy_url: null,
          error: null,
          accounts: [],
          model_count: 0,
          providers: [],
        }),
      ),
    );
  }

  getSubscriptionAccounts(): Observable<{ accounts: SubscriptionAccount[] }> {
    return this.http
      .get<{ accounts: SubscriptionAccount[] }>(`${this.baseUrl}/subscriptions/accounts`)
      .pipe(catchError(() => of({ accounts: [] })));
  }

  /**
   * Usage for one account. Degrades to `available: false` rather than throwing,
   * and never renders a zero for a provider without a reader.
   */
  getSubscriptionUsage(accountId: string): Observable<SubscriptionUsage> {
    return this.http
      .get<SubscriptionUsage>(
        `${this.baseUrl}/subscriptions/accounts/${encodeURIComponent(accountId)}/usage`,
      )
      .pipe(catchError(() => of<SubscriptionUsage>({ available: false, reason: 'unavailable' })));
  }

  disconnectSubscriptionAccount(accountId: string): Observable<{ status: string }> {
    return this.http
      .delete<{ status: string }>(
        `${this.baseUrl}/subscriptions/accounts/${encodeURIComponent(accountId)}`,
      )
      .pipe(
        tap(() => {
          this.readiness.load();
          this.adminProviders.loadSubscriptionAvailability();
        }),
      );
  }

  startSubscriptionLogin(provider: string): Observable<SubscriptionLogin> {
    return this.http.post<SubscriptionLogin>(`${this.baseUrl}/subscriptions/logins`, { provider });
  }

  pollSubscriptionLogin(loginId: string): Observable<SubscriptionLogin> {
    return this.http.get<SubscriptionLogin>(
      `${this.baseUrl}/subscriptions/logins/${encodeURIComponent(loginId)}`,
    );
  }

  /** Relay a pasted browser callback. Parsed server-side; never used as a URL. */
  submitSubscriptionCallback(loginId: string, url: string): Observable<SubscriptionLogin> {
    return this.http
      .post<SubscriptionLogin>(
        `${this.baseUrl}/subscriptions/logins/${encodeURIComponent(loginId)}/callback`,
        { url },
      )
      .pipe(
        tap(() => {
          this.readiness.load();
          this.adminProviders.loadSubscriptionAvailability();
        }),
      );
  }

  cancelSubscriptionLogin(loginId: string): Observable<SubscriptionLogin> {
    return this.http.delete<SubscriptionLogin>(
      `${this.baseUrl}/subscriptions/logins/${encodeURIComponent(loginId)}`,
    );
  }

  // ── Main Cloud System Settings (Admin — Phase 4) ──────────────

  getMainCloudSettings(): Observable<MainCloudSettingsResponse> {
    return this.http.get<MainCloudSettingsResponse>(
      `${this.baseUrl}/admin/system-settings/main_cloud`,
    );
  }

  putMainCloudSettings(body: MainCloudSettingsRequest): Observable<MainCloudPutResponse> {
    return this.http.put<MainCloudPutResponse>(
      `${this.baseUrl}/admin/system-settings/main_cloud`,
      body,
    );
  }

  testMainCloudSettings(body: MainCloudSettingsRequest): Observable<MainCloudTestResponse> {
    return this.http.post<MainCloudTestResponse>(
      `${this.baseUrl}/admin/system-settings/main_cloud/test`,
      body,
    );
  }

  deleteMainCloudSettings(): Observable<{ status: string; existed: boolean; backend_id?: string }> {
    return this.http.delete<{ status: string; existed: boolean; backend_id?: string }>(
      `${this.baseUrl}/admin/system-settings/main_cloud`,
    );
  }
}

// ── Main cloud settings types ──────────────────────────────────

export interface MainCloudEffectiveConfig {
  backend_id: string;
  backend_instance_id?: string | null;
  is_initialized: boolean;
  is_configured: boolean;
  base_url?: string | null;
  public_url?: string | null;
  admin_user?: string | null;
  agent_user?: string | null;
  keycloak_issuer?: string | null;
  keycloak_client_id?: string | null;
  admin_role_claim_value?: string | null;
  default_quota_bytes?: number | null;
}

export interface MainCloudOverlay {
  present: boolean;
  value: Record<string, unknown>;
  credentials_ref: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

export interface MainCloudSecretProvenance {
  env_var: string;
  set: boolean;
}

export interface MainCloudSettingsResponse {
  effective: MainCloudEffectiveConfig;
  activation_revision: number;
  backend_instance: {
    id: string;
    routing_sha256: string;
    installation_proof_sha256: string;
    secret_revision: number;
  } | null;
  overlay: MainCloudOverlay;
  secrets: Record<string, MainCloudSecretProvenance>;
  allowed_backends: string[];
}

export interface MainCloudSettingsRequest {
  value: Record<string, unknown>;
  credentials_ref: string | null;
  expected_activation_revision?: number;
}

export interface MainCloudPutResponse {
  status: string;
  backend_id: string;
  backend_instance_id: string;
  activation_revision: number;
  reloaded: boolean;
}

// Form state for the admin Cloud Storage section. Explicit named fields
// (rather than `Record<string, unknown>`) so the template can dot-access
// them under TypeScript's `noPropertyAccessFromIndexSignature` rule.
// Every field is always present — strings default to '', quota to null —
// so the template doesn't need optional-chaining gymnastics.
export interface MainCloudFormState {
  backend_id: string;
  base_url: string;
  public_url: string;
  // Nextcloud-only
  admin_user: string;
  agent_user: string;
  // OpenCloud-only
  keycloak_issuer: string;
  keycloak_client_id: string;
  admin_role_claim_value: string;
  default_quota_bytes: number | null;
}

export interface MainCloudTestResponse {
  ok: boolean;
  detail: string;
  latency_ms?: number;
}
