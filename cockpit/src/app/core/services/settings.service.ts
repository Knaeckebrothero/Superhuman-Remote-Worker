import {inject, Injectable, signal} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {catchError, Observable, of, tap} from 'rxjs';
import {
  ApiKeyEntry,
  ApiKeySetRequest,
  CodexStatus,
  LlmEndpoint,
  LlmEndpointCreateRequest,
  LlmEndpointTestResult,
  LlmEndpointUpdateRequest,
  ResolvedDefaults,
  UserSettings,
} from '../models/api.model';
import {environment} from '../environment';
import {AdminProvidersService} from './admin-providers.service';
import {ReadinessService} from './readiness.service';

@Injectable({ providedIn: 'root' })
export class SettingsService {
  private readonly http = inject(HttpClient);
  private readonly readiness = inject(ReadinessService);
  private readonly adminProviders = inject(AdminProvidersService);
  private readonly baseUrl = environment.apiUrl;

  /** Current user's API keys (prefix only, no full keys). */
  readonly apiKeys = signal<ApiKeyEntry[]>([]);

  /** Current user's registered LLM endpoints with nested models. */
  readonly llmEndpoints = signal<LlmEndpoint[]>([]);

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

  // ── User LLM Endpoints ────────────────────────────────────────────

  loadLlmEndpoints(): void {
    this.http
      .get<LlmEndpoint[]>(`${this.baseUrl}/settings/llm-endpoints`)
      .pipe(catchError(() => of([])))
      .subscribe((endpoints) => this.llmEndpoints.set(endpoints));
  }

  createLlmEndpoint(body: LlmEndpointCreateRequest): Observable<LlmEndpoint> {
    return this.http
      .post<LlmEndpoint>(`${this.baseUrl}/settings/llm-endpoints`, body)
      .pipe(tap(() => this.loadLlmEndpoints()));
  }

  updateLlmEndpoint(endpointId: string, body: LlmEndpointUpdateRequest): Observable<LlmEndpoint> {
    return this.http
      .patch<LlmEndpoint>(`${this.baseUrl}/settings/llm-endpoints/${endpointId}`, body)
      .pipe(tap(() => this.loadLlmEndpoints()));
  }

  deleteLlmEndpoint(endpointId: string): Observable<{ status: string }> {
    return this.http
      .delete<{ status: string }>(`${this.baseUrl}/settings/llm-endpoints/${endpointId}`)
      .pipe(tap(() => this.loadLlmEndpoints()));
  }

  testLlmEndpoint(endpointId: string): Observable<LlmEndpointTestResult> {
    return this.http.post<LlmEndpointTestResult>(
      `${this.baseUrl}/settings/llm-endpoints/${endpointId}/test`,
      {},
    );
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

  // ── Project API Keys ──────────────────────────────────────────────

  getProjectApiKeys(projectId: string): Observable<ApiKeyEntry[]> {
    return this.http
      .get<ApiKeyEntry[]>(`${this.baseUrl}/projects/${projectId}/api-keys`)
      .pipe(catchError(() => of([])));
  }

  setProjectApiKey(projectId: string, provider: string, body: ApiKeySetRequest): Observable<ApiKeyEntry> {
    return this.http
      .put<ApiKeyEntry>(`${this.baseUrl}/projects/${projectId}/api-keys/${provider}`, body);
  }

  deleteProjectApiKey(projectId: string, provider: string): Observable<{ status: string }> {
    return this.http
      .delete<{ status: string }>(`${this.baseUrl}/projects/${projectId}/api-keys/${provider}`);
  }

  // ── Codex Proxy (Admin) ───────────────────────────────────────

  getCodexStatus(): Observable<CodexStatus> {
    return this.http
      .get<CodexStatus>(`${this.baseUrl}/codex/status`)
      .pipe(catchError(() => of({ connected: false, accounts: [], model_count: 0 })));
  }

  getCodexModels(): Observable<{ models: string[] }> {
    return this.http
      .get<{ models: string[] }>(`${this.baseUrl}/codex/models`)
      .pipe(catchError(() => of({ models: [] })));
  }

  startCodexLogin(): Observable<{ auth_url: string; state: string }> {
    return this.http
      .post<{ auth_url: string; state: string }>(`${this.baseUrl}/codex/login`, {});
  }

  pollCodexLogin(state: string): Observable<{ status: string }> {
    return this.http
      .get<{ status: string }>(`${this.baseUrl}/codex/login/poll`, { params: { state } });
  }

  completeCodexLogin(callbackUrl: string): Observable<Record<string, unknown>> {
    return this.http
      .post<Record<string, unknown>>(`${this.baseUrl}/codex/callback`, { url: callbackUrl })
      .pipe(
        tap(() => {
          this.readiness.load();
          this.adminProviders.loadCodexAvailability();
        }),
      );
  }

  deleteCodexCredential(name: string): Observable<{ status: string }> {
    return this.http
      .delete<{ status: string }>(`${this.baseUrl}/codex/credentials/${encodeURIComponent(name)}`)
      .pipe(
        tap(() => {
          this.readiness.load();
          this.adminProviders.loadCodexAvailability();
        }),
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

  reloadMainCloudSettings(): Observable<{ status: string; backend_id: string }> {
    return this.http.post<{ status: string; backend_id: string }>(
      `${this.baseUrl}/admin/system-settings/main_cloud/reload`,
      {},
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
  length: number;
}

export interface MainCloudSettingsResponse {
  effective: MainCloudEffectiveConfig;
  overlay: MainCloudOverlay;
  secrets: Record<string, MainCloudSecretProvenance>;
  allowed_backends: string[];
}

export interface MainCloudSettingsRequest {
  value: Record<string, unknown>;
  credentials_ref: string | null;
}

export interface MainCloudPutResponse {
  status: string;
  backend_id: string;
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
