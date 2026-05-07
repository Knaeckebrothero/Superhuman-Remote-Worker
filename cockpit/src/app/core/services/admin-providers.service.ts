import {inject, Injectable, signal} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {catchError, Observable, of, tap} from 'rxjs';
import {
  ApiKeyProvider,
  ApiKeySetRequest,
  DiscoveryResponse,
  LlmEndpoint,
  LlmEndpointCreateRequest,
  LlmEndpointDiscoveryResult,
  LlmEndpointTestResult,
  LlmEndpointUpdateRequest,
} from '../models/api.model';
import {environment} from '../environment';
import {ModelService} from './model.service';
import {ReadinessService} from './readiness.service';

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
 * server-side. The `chat` slot is the cluster-wide chat default used by
 * the dispatcher when a job/session has no override; surfacing it here
 * fulfills the readiness gate's `Pin a default for: chat` requirement.
 */
export type DefaultModelKind =
  | 'chat'
  | 'builder'
  | 'browser'
  | 'citation'
  | 'embedding'
  | 'vision'
  | 'auxiliary'
  | 'whisper'
  | 'tts';

export const DEFAULT_MODEL_KINDS: DefaultModelKind[] = [
  'chat',
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
  chat: null,
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
 * Reported by `GET /api/admin/providers/codex/availability`. Used by Admin →
 * Models to decide whether the seeded `codex-proxy` system endpoint is usable
 * right now (proxy reachable + at least one active subscription).
 */
export interface CodexAvailability {
  available: boolean;
  account_count: number;
  models: string[];
  proxy_url: string | null;
  endpoint_id: string | null;
}

const EMPTY_CODEX_AVAILABILITY: CodexAvailability = {
  available: false,
  account_count: 0,
  models: [],
  proxy_url: null,
  endpoint_id: null,
};

/**
 * REST client for the `/api/admin/providers/*` surface. Every call is gated
 * by the `srw-admin` role server-side; the client does not re-check.
 */
@Injectable({providedIn: 'root'})
export class AdminProvidersService {
  private readonly http = inject(HttpClient);
  private readonly readiness = inject(ReadinessService);
  private readonly modelService = inject(ModelService);
  private readonly baseUrl = environment.apiUrl;

  readonly systemApiKeys = signal<SystemApiKeyEntry[]>([]);
  readonly systemEndpoints = signal<LlmEndpoint[]>([]);
  readonly defaults = signal<Record<DefaultModelKind, string | null>>({...EMPTY_DEFAULTS});
  readonly codexAvailability = signal<CodexAvailability>({...EMPTY_CODEX_AVAILABILITY});

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
      .pipe(
        tap(() => {
          this.loadSystemApiKeys();
          this.readiness.load();
          this.modelService.load(undefined, true);
        }),
      );
  }

  deleteSystemApiKey(provider: string): Observable<{status: string}> {
    return this.http
      .delete<{status: string}>(`${this.baseUrl}/admin/providers/keys/${provider}`)
      .pipe(
        tap(() => {
          this.loadSystemApiKeys();
          this.readiness.load();
          this.modelService.load(undefined, true);
        }),
      );
  }

  /**
   * Read the staged discovery payload for a provider key. Returns
   * ``ready=false`` while the orchestrator's async probe is still
   * in-flight (post-save) — the cockpit polls this until the dialog can
   * render. The route never throws; transient backend failures resolve
   * to ``ready=false`` so the UI degrades gracefully.
   */
  getDiscoveryPayload(provider: string): Observable<DiscoveryResponse> {
    return this.http
      .get<DiscoveryResponse>(
        `${this.baseUrl}/admin/providers/keys/${provider}/discovery`,
      )
      .pipe(
        catchError(() =>
          of<DiscoveryResponse>({ready: false, fresh: false, payload: null, cached_at: null}),
        ),
      );
  }

  /**
   * Force-refresh the discovery cache for a provider key (synchronous —
   * the route blocks until the probe returns). Used by the explicit
   * "Rediscover" button; the response carries the freshly-cached payload
   * so the dialog can re-render without an extra GET.
   */
  rediscoverProvider(provider: string): Observable<DiscoveryResponse> {
    return this.http.post<DiscoveryResponse>(
      `${this.baseUrl}/admin/providers/keys/${provider}/rediscover`,
      {},
    );
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
      .pipe(
        tap(() => {
          this.loadSystemEndpoints();
          this.readiness.load();
          this.modelService.load(undefined, true);
        }),
      );
  }

  updateSystemEndpoint(
    endpointId: string,
    body: LlmEndpointUpdateRequest,
  ): Observable<LlmEndpoint> {
    return this.http
      .patch<LlmEndpoint>(`${this.baseUrl}/admin/providers/endpoints/${endpointId}`, body)
      .pipe(
        tap(() => {
          this.loadSystemEndpoints();
          this.readiness.load();
          this.modelService.load(undefined, true);
        }),
      );
  }

  deleteSystemEndpoint(endpointId: string): Observable<{status: string}> {
    return this.http
      .delete<{status: string}>(`${this.baseUrl}/admin/providers/endpoints/${endpointId}`)
      .pipe(
        tap(() => {
          this.loadSystemEndpoints();
          this.readiness.load();
          this.modelService.load(undefined, true);
        }),
      );
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
      .pipe(
        tap(() => {
          this.loadDefaults();
          this.readiness.load();
        }),
      );
  }

  // ── Codex Proxy Availability ──────────────────────────────────────

  /**
   * Refresh the codex-proxy availability signal. Admin → Models reads this
   * to decide whether to surface a "log in via Settings → Codex" hint when
   * the user picks the codex-proxy endpoint without an active subscription.
   */
  loadCodexAvailability(): void {
    this.http
      .get<CodexAvailability>(`${this.baseUrl}/admin/providers/codex/availability`)
      .pipe(catchError(() => of({...EMPTY_CODEX_AVAILABILITY})))
      .subscribe((info) => this.codexAvailability.set(info));
  }
}
