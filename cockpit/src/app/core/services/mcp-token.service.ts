import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, catchError, of, tap } from 'rxjs';
import { McpToken, McpTokenCreateRequest, McpTokenCreateResponse } from '../models/api.model';
import { environment } from '../environment';

@Injectable({ providedIn: 'root' })
export class McpTokenService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.apiUrl;

  /** All tokens for the current user. */
  readonly tokens = signal<McpToken[]>([]);

  /** Load the current user's MCP tokens. */
  loadTokens(): void {
    this.http
      .get<McpToken[]>(`${this.baseUrl}/mcp-tokens`, { withCredentials: true })
      .pipe(catchError(() => of([])))
      .subscribe((tokens) => this.tokens.set(tokens));
  }

  /** Create a new MCP token. Returns the full response including plaintext. */
  createToken(body: McpTokenCreateRequest): Observable<McpTokenCreateResponse> {
    return this.http
      .post<McpTokenCreateResponse>(`${this.baseUrl}/mcp-tokens`, body, {
        withCredentials: true,
      })
      .pipe(tap(() => this.loadTokens()));
  }

  /** Revoke (soft-delete) an MCP token. */
  revokeToken(tokenId: string): Observable<{ status: string }> {
    return this.http
      .delete<{ status: string }>(`${this.baseUrl}/mcp-tokens/${tokenId}`, {
        withCredentials: true,
      })
      .pipe(tap(() => this.loadTokens()));
  }
}
