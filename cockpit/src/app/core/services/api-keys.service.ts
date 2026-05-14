import {Injectable, inject, signal} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {Observable, catchError, of, tap} from 'rxjs';
import {environment} from '../environment';

/**
 * Personal Access Token (PAT).
 *
 * Returned by `GET /api/api-keys`. The plaintext token only ever shows
 * up in the `CreateApiKeyResponse` returned by `POST /api/api-keys` or
 * `POST /api/api-keys/{id}/rotate` — never on the list endpoint.
 */
export interface ApiKey {
  id: string;
  user_id: string;
  name: string;
  token_prefix: string;
  last_four: string | null;
  scopes: string[];
  expires_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
  last_used_ip: string | null;
  created_at: string;
  superseded_by: string | null;
}

export interface CreateApiKeyResponse extends ApiKey {
  token: string;
}

export interface CreateApiKeyRequest {
  name: string;
  scopes: string[];
  /** Days until expiry. `null` = never (only offered with explicit confirm in UI). */
  expires_in_days: number | null;
}

@Injectable({providedIn: 'root'})
export class ApiKeysService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.apiUrl;

  /** All known PATs for the current user. */
  readonly keys = signal<ApiKey[]>([]);
  readonly isLoading = signal(false);

  /** Load the current user's API keys into the signal. */
  loadKeys(): void {
    this.isLoading.set(true);
    this.http
      .get<ApiKey[]>(`${this.baseUrl}/api-keys`)
      .pipe(catchError(() => of([] as ApiKey[])))
      .subscribe((keys) => {
        this.keys.set(keys);
        this.isLoading.set(false);
      });
  }

  /** Create a new API key. Plaintext token is returned ONCE — UI must
   *  surface it immediately. List is refreshed in the tap. */
  createKey(body: CreateApiKeyRequest): Observable<CreateApiKeyResponse> {
    return this.http
      .post<CreateApiKeyResponse>(`${this.baseUrl}/api-keys`, body)
      .pipe(tap(() => this.loadKeys()));
  }

  /** Soft-revoke a key. */
  revokeKey(id: string): Observable<{ status: string }> {
    return this.http
      .delete<{ status: string }>(`${this.baseUrl}/api-keys/${id}`)
      .pipe(tap(() => this.loadKeys()));
  }

  /** Issue a successor key. Old key remains valid for 24h. Plaintext
   *  token is returned ONCE. */
  rotateKey(id: string): Observable<CreateApiKeyResponse> {
    return this.http
      .post<CreateApiKeyResponse>(`${this.baseUrl}/api-keys/${id}/rotate`, {})
      .pipe(tap(() => this.loadKeys()));
  }
}
