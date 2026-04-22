import {inject, Injectable, signal} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {catchError, Observable, of, tap} from 'rxjs';
import {
  ApiKeyProvider,
  ApiKeySetRequest,
  LlmEndpoint,
  LlmEndpointCreateRequest,
  LlmEndpointModel,
  LlmEndpointModelCreateRequest,
  LlmEndpointModelUpdateRequest,
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

export type DefaultModelKind = 'builder' | 'browser' | 'citation';

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
  readonly defaults = signal<Record<DefaultModelKind, string | null>>({
    builder: null,
    browser: null,
    citation: null,
  });

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

  createSystemEndpointModel(
    endpointId: string,
    body: LlmEndpointModelCreateRequest,
  ): Observable<LlmEndpointModel> {
    return this.http
      .post<LlmEndpointModel>(
        `${this.baseUrl}/admin/providers/endpoints/${endpointId}/models`,
        body,
      )
      .pipe(tap(() => this.loadSystemEndpoints()));
  }

  updateSystemEndpointModel(
    endpointId: string,
    modelId: string,
    body: LlmEndpointModelUpdateRequest,
  ): Observable<LlmEndpointModel> {
    return this.http
      .patch<LlmEndpointModel>(
        `${this.baseUrl}/admin/providers/endpoints/${endpointId}/models/${encodeURIComponent(modelId)}`,
        body,
      )
      .pipe(tap(() => this.loadSystemEndpoints()));
  }

  deleteSystemEndpointModel(
    endpointId: string,
    modelId: string,
  ): Observable<{status: string}> {
    return this.http
      .delete<{status: string}>(
        `${this.baseUrl}/admin/providers/endpoints/${endpointId}/models/${encodeURIComponent(modelId)}`,
      )
      .pipe(tap(() => this.loadSystemEndpoints()));
  }

  testSystemEndpoint(endpointId: string): Observable<LlmEndpointTestResult> {
    return this.http.post<LlmEndpointTestResult>(
      `${this.baseUrl}/admin/providers/endpoints/${endpointId}/test`,
      {},
    );
  }

  // ── System Defaults ───────────────────────────────────────────────

  loadDefaults(): void {
    this.http
      .get<Record<DefaultModelKind, string | null>>(`${this.baseUrl}/admin/providers/defaults`)
      .pipe(catchError(() => of({builder: null, browser: null, citation: null})))
      .subscribe((rec) => this.defaults.set(rec));
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
