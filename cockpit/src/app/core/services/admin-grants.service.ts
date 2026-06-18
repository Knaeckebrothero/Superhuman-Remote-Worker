import {inject, Injectable, signal} from '@angular/core';
import {HttpClient, HttpParams} from '@angular/common/http';
import {catchError, Observable, of} from 'rxjs';
import {Grant, GrantCatalog, GrantListResponse} from '../models/api.model';
import {environment} from '../environment';

export type ScopeKind = 'user' | 'project' | 'global';

/**
 * REST client for the capability-grants admin surface (GET/PUT/DELETE
 * /api/admin/grants). Every call is gated by `is_admin=TRUE` server-side; the
 * client does not re-check. `load()` refreshes the `grants`/`catalog` signals
 * for the currently-selected scope.
 */
@Injectable({providedIn: 'root'})
export class AdminGrantsService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.apiUrl;

  readonly grants = signal<Grant[]>([]);
  readonly catalog = signal<GrantCatalog>({});

  /** Load the explicitly-set grants for one scope into the signals. */
  load(scopeKind: ScopeKind, scopeId: string | null): void {
    let params = new HttpParams().set('scope_kind', scopeKind);
    if (scopeKind !== 'global' && scopeId) params = params.set('scope_id', scopeId);
    this.http
      .get<GrantListResponse>(`${this.baseUrl}/admin/grants`, {params})
      .pipe(catchError(() => of<GrantListResponse>({grants: [], catalog: {}})))
      .subscribe((res) => {
        this.grants.set(res.grants ?? []);
        if (res.catalog && Object.keys(res.catalog).length) this.catalog.set(res.catalog);
      });
  }

  setGrant(
    scopeKind: ScopeKind,
    scopeId: string | null,
    key: string,
    valueJson: unknown,
    reason?: string,
  ): Observable<unknown> {
    const sid = scopeKind === 'global' ? 'global' : scopeId;
    return this.http.put(`${this.baseUrl}/admin/grants/${scopeKind}/${sid}/${key}`, {
      value_json: valueJson,
      reason: reason ?? null,
    });
  }

  deleteGrant(scopeKind: ScopeKind, scopeId: string | null, key: string): Observable<unknown> {
    const sid = scopeKind === 'global' ? 'global' : scopeId;
    return this.http.delete(`${this.baseUrl}/admin/grants/${scopeKind}/${sid}/${key}`);
  }
}
