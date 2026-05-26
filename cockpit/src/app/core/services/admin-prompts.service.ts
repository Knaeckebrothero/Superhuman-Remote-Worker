import {inject, Injectable, signal} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {catchError, Observable, of, tap} from 'rxjs';
import {environment} from '../environment';

export type PromptKind = 'prompts' | 'instructions';
export type PromptContentFormat = 'text' | 'markdown' | 'jinja' | 'yaml';

/** A DB-backed override row (mirrors orchestrator prompt_overrides). */
export interface PromptOverride {
  id: string;
  family: string | null; // null = global (all families)
  kind: PromptKind;
  name: string;
  content: string;
  content_format: PromptContentFormat;
  notes: string | null;
  created_by: string | null;
  updated_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/** A human description of an editable prompt key (config/prompts/catalog.yaml). */
export interface PromptCatalogEntry {
  kind: PromptKind;
  name: string;
  title: string;
  description: string;
}

/** The bundled (shipped) default for a key, plus its catalog entry. */
export interface PromptBundled {
  family: string | null;
  kind: string;
  name: string;
  content: string;
  catalog: PromptCatalogEntry | null;
}

export interface PromptOverrideCreate {
  family: string | null;
  kind: PromptKind;
  name: string;
  content: string;
  content_format?: PromptContentFormat;
  notes?: string | null;
}

/**
 * REST client for the admin prompt-overrides surface
 * (`/api/admin/prompts/*`). Every call is gated by `is_admin=TRUE`
 * server-side; the client does not re-check. Auth + CSRF ride along via the
 * global auth interceptor.
 */
@Injectable({providedIn: 'root'})
export class AdminPromptsService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.apiUrl;

  readonly overrides = signal<PromptOverride[]>([]);
  readonly catalog = signal<PromptCatalogEntry[]>([]);

  loadOverrides(): void {
    this.http
      .get<PromptOverride[]>(`${this.baseUrl}/admin/prompts/overrides`)
      .pipe(catchError(() => of([] as PromptOverride[])))
      .subscribe((rows) => this.overrides.set(rows));
  }

  loadCatalog(): void {
    this.http
      .get<PromptCatalogEntry[]>(`${this.baseUrl}/admin/prompts/catalog`)
      .pipe(catchError(() => of([] as PromptCatalogEntry[])))
      .subscribe((rows) => this.catalog.set(rows));
  }

  /** Bundled default for a key. A null family maps to the `_` URL segment. */
  getBundled(family: string | null, kind: string, name: string): Observable<PromptBundled> {
    const fam = family ?? '_';
    return this.http.get<PromptBundled>(
      `${this.baseUrl}/admin/prompts/bundled/${fam}/${kind}/${name}`,
    );
  }

  /** Create or replace the override for (family, kind, name) — backend upserts. */
  createOverride(body: PromptOverrideCreate): Observable<PromptOverride> {
    return this.http
      .post<PromptOverride>(`${this.baseUrl}/admin/prompts/overrides`, body)
      .pipe(tap(() => this.loadOverrides()));
  }

  /** Delete an override (reset to bundled default). */
  deleteOverride(id: string): Observable<{deleted: boolean}> {
    return this.http
      .delete<{deleted: boolean}>(`${this.baseUrl}/admin/prompts/overrides/${id}`)
      .pipe(tap(() => this.loadOverrides()));
  }
}
