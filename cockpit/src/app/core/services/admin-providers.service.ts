import {inject, Injectable, signal} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {catchError, Observable, of, tap} from 'rxjs';
import {
  ApiKeyProvider,
  ApiKeySetRequest,
  LlmEndpoint,
  LlmEndpointCreateRequest,
  LlmEndpointDiscoveryResult,
  LlmEndpointTestResult,
  LlmEndpointUpdateRequest,
} from '../models/api.model';
import {environment} from '../environment';

/**
 * A system-scoped provider API key row. Mirrors `user_api_keys` but with a
 * `seeded_from` breadcrumb identifying rows created by `helm.llm.seed`.
 */
export interface SystemApiKeyEntry {
  id: string;
  provider: ApiKeyProvider;
  key_prefix: string | null;
  label: string | null;
  seeded_from: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/**
 * Admins can pin cluster-wide defaults for each of these slots via the
 * Admin → Providers → Defaults section. Keep this in sync with the
 * orchestrator's `VALID_DEFAULT_MODEL_KINDS` — unknown kinds are rejected
 * server-side.
 */
export type DefaultModelKind =
  | 'builder'
  | 'browser'
  | 'citation'
  | 'embedding'
  | 'vision'
  | 'auxiliary'
  | 'whisper'
  | 'tts';

export const DEFAULT_MODEL_KINDS: DefaultModelKind[] = [
  'builder',
  'browser',
  'citation',
  'embedding',
  'vision',
  'auxiliary',
  'whisper',
  'tts',
];

const EMPTY_DEFAULTS: Record<DefaultModelKind, string | null> = {
  builder: null,
  browser: null,
  citation: null,
  embedding: null,
  vision: null,
  auxiliary: null,
  whisper: null,
  tts: null,
};

/**
 * REST client for the `/api/admin/providers/*` surface. Every call is gated
 * by the `srw-admin` role server-side; the client does not re-check.
 */
@Injectable({providedIn: 'root'})
export class AdminProvidersService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.apiUrl;

  readonly systemApiKeys = signal<SystemApiKeyEntry[]>([]);
  readonly systemEndpoints = signal<LlmEndpoint[]>([]);
  readonly defaults = signal<Record<DefaultModelKind, string | null>>({...EMPTY_DEFAULTS});

  // ── System API Keys ───────────────────────────────────────────────

  loadSystemApiKeys(): void {
    this.http
      .get<SystemApiKeyEntry[]>(`${this.baseUrl}/admin/providers/keys`)
      .pipe(catchError(() => of([] as SystemApiKeyEntry[])))
      .subscribe((rows) => this.systemApiKeys.set(rows));
  }

  setSystemApiKey(provider: string, body: ApiKeySetRequest): Observable<SystemApiKeyEntry> {
    return this.http
      .put<SystemApiKeyEntry>(`${this.baseUrl}/admin/providers/keys/${provider}`, body)
      .pipe(tap(() => this.loadSystemApiKeys()));
  }

  deleteSystemApiKey(provider: string): Observable<{status: string}> {
    return this.http
      .delete<{status: string}>(`${this.baseUrl}/admin/providers/keys/${provider}`)
      .pipe(tap(() => this.loadSystemApiKeys()));
  }

  // ── System Endpoints ──────────────────────────────────────────────

  loadSystemEndpoints(): void {
    this.http
      .get<LlmEndpoint[]>(`${this.baseUrl}/admin/providers/endpoints`)
      .pipe(catchError(() => of([] as LlmEndpoint[])))
      .subscribe((rows) => this.systemEndpoints.set(rows));
  }

  createSystemEndpoint(body: LlmEndpointCreateRequest): Observable<LlmEndpoint> {
    return this.http
      .post<LlmEndpoint>(`${this.baseUrl}/admin/providers/endpoints`, body)
      .pipe(tap(() => this.loadSystemEndpoints()));
  }

  updateSystemEndpoint(
    endpointId: string,
    body: LlmEndpointUpdateRequest,
  ): Observable<LlmEndpoint> {
    return this.http
      .patch<LlmEndpoint>(`${this.baseUrl}/admin/providers/endpoints/${endpointId}`, body)
      .pipe(tap(() => this.loadSystemEndpoints()));
  }

  deleteSystemEndpoint(endpointId: string): Observable<{status: string}> {
    return this.http
      .delete<{status: string}>(`${this.baseUrl}/admin/providers/endpoints/${endpointId}`)
      .pipe(tap(() => this.loadSystemEndpoints()));
  }

  testSystemEndpoint(endpointId: string): Observable<LlmEndpointTestResult> {
    return this.http.post<LlmEndpointTestResult>(
      `${this.baseUrl}/admin/providers/endpoints/${endpointId}/test`,
      {},
    );
  }

  /**
   * Read-only probe of `GET {base_url}/models`. Admin → Models uses this as
   * a quick-fill helper after the admin picks an endpoint provider.
   */
  discoverSystemEndpointModels(endpointId: string): Observable<LlmEndpointDiscoveryResult> {
    return this.http.post<LlmEndpointDiscoveryResult>(
      `${this.baseUrl}/admin/providers/endpoints/${endpointId}/discover`,
      {},
    );
  }

  // ── System Defaults ───────────────────────────────────────────────

  loadDefaults(): void {
    this.http
      .get<Record<DefaultModelKind, string | null>>(`${this.baseUrl}/admin/providers/defaults`)
      .pipe(catchError(() => of({...EMPTY_DEFAULTS})))
      .subscribe((rec) => this.defaults.set({...EMPTY_DEFAULTS, ...rec}));
  }

  setDefault(kind: DefaultModelKind, model: string): Observable<{kind: string; model: string | null}> {
    return this.http
      .put<{kind: string; model: string | null}>(
        `${this.baseUrl}/admin/providers/defaults/${kind}`,
        {model},
      )
      .pipe(tap(() => this.loadDefaults()));
  }
}
