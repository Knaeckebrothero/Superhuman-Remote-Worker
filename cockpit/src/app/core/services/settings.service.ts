import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, catchError, of, tap } from 'rxjs';
import { ApiKeyEntry, ApiKeySetRequest, UserSettings, CodexStatus } from '../models/api.model';
import { environment } from '../environment';

@Injectable({ providedIn: 'root' })
export class SettingsService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.apiUrl;

  /** Current user's API keys (prefix only, no full keys). */
  readonly apiKeys = signal<ApiKeyEntry[]>([]);

  /** Current user's preference settings. */
  readonly preferences = signal<UserSettings>({});

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
      .pipe(catchError(() => of({})))
      .subscribe((prefs) => this.preferences.set(prefs));
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

  deleteCodexCredential(name: string): Observable<{ status: string }> {
    return this.http
      .delete<{ status: string }>(`${this.baseUrl}/codex/credentials/${encodeURIComponent(name)}`);
  }
}
