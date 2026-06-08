import {inject, Injectable, signal} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {catchError, forkJoin, map, Observable, of, tap} from 'rxjs';
import {environment} from '../environment';

export type ConfigKind = 'prompts' | 'instructions' | 'settings' | 'guardrails';
export type ConfigContentFormat = 'text' | 'markdown' | 'jinja' | 'yaml';
export type ConfigValueType = 'number' | 'integer' | 'boolean' | 'enum' | 'json';

/**
 * A DB-backed override row (mirrors orchestrator config_overrides). Text kinds
 * (prompts, instructions) carry `content`; structured kinds (settings,
 * guardrails) carry `value_json`.
 */
export interface ConfigOverride {
  id: string;
  family: string | null; // null = global (all families)
  kind: ConfigKind;
  name: string;
  content: string | null;
  content_format: ConfigContentFormat | null;
  value_json?: unknown;
  notes: string | null;
  created_by: string | null;
  updated_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/**
 * A human description of an editable config key (config/prompts/catalog.yaml).
 * Settings/guardrails entries also carry `type` (+ optional bounds/enum) so the
 * UI renders typed editors. `group` is the <optgroup> the key is shown under in
 * the picker (separates worker-only, persistent-only, and shared keys).
 */
export interface ConfigCatalogEntry {
  kind: ConfigKind;
  name: string;
  title: string;
  description: string;
  group?: string;
  type?: ConfigValueType;
  min?: number;
  max?: number;
  enum?: unknown[];
}

/**
 * The bundled (shipped) default for a key, plus its catalog entry. `content` is
 * text for prompts/instructions and a JSON value for settings/guardrails.
 */
export interface ConfigBundled {
  family: string | null;
  kind: string;
  name: string;
  content: unknown;
  catalog: ConfigCatalogEntry | null;
}

export interface ConfigOverrideCreate {
  family: string | null;
  kind: ConfigKind;
  name: string;
  content?: string | null;
  content_format?: ConfigContentFormat;
  value_json?: unknown;
  notes?: string | null;
}

/**
 * The overrides API returns `value_json` as a JSON-encoded string (asyncpg
 * hands JSONB back as text), e.g. `"true"`, `"1"`, `'{"nudges":[]}'`. Decode it
 * once on read so consumers (settings form, guardrails editor) see real
 * booleans / numbers / objects instead of strings.
 */
export function coerceOverrideValue(o: ConfigOverride): ConfigOverride {
  if (typeof o.value_json === 'string') {
    try {
      return {...o, value_json: JSON.parse(o.value_json) as unknown};
    } catch {
      return o; // not JSON — leave as-is
    }
  }
  return o;
}

/**
 * REST client for the admin prompt-overrides surface
 * (`/api/admin/config/*`). Every call is gated by `is_admin=TRUE`
 * server-side; the client does not re-check. Auth + CSRF ride along via the
 * global auth interceptor.
 */
@Injectable({providedIn: 'root'})
export class AdminConfigService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.apiUrl;

  readonly overrides = signal<ConfigOverride[]>([]);
  readonly catalog = signal<ConfigCatalogEntry[]>([]);

  loadOverrides(): void {
    this.http
      .get<ConfigOverride[]>(`${this.baseUrl}/admin/config/overrides`)
      .pipe(
        map((rows) => rows.map(coerceOverrideValue)),
        catchError(() => of([] as ConfigOverride[])),
      )
      .subscribe((rows) => this.overrides.set(rows));
  }

  loadCatalog(): void {
    this.http
      .get<ConfigCatalogEntry[]>(`${this.baseUrl}/admin/config/catalog`)
      .pipe(catchError(() => of([] as ConfigCatalogEntry[])))
      .subscribe((rows) => this.catalog.set(rows));
  }

  /** Bundled default for a key. A null family maps to the `_` URL segment. */
  getBundled(family: string | null, kind: string, name: string): Observable<ConfigBundled> {
    const fam = family ?? '_';
    return this.http.get<ConfigBundled>(
      `${this.baseUrl}/admin/config/bundled/${fam}/${kind}/${name}`,
    );
  }

  /**
   * Bundled defaults for several `settings` leaves at once, keyed by name.
   * Powers the typed settings form (one round-trip per leaf, fanned out). An
   * empty `names` resolves to `{}` without hitting the network.
   */
  getBundledSettings(
    family: string | null,
    names: string[],
  ): Observable<Record<string, unknown>> {
    if (names.length === 0) return of({});
    const calls: Record<string, Observable<unknown>> = {};
    for (const n of names) {
      calls[n] = this.getBundled(family, 'settings', n).pipe(map((b) => b.content));
    }
    return forkJoin(calls);
  }

  /** Create or replace the override for (family, kind, name) — backend upserts. */
  createOverride(body: ConfigOverrideCreate): Observable<ConfigOverride> {
    return this.http
      .post<ConfigOverride>(`${this.baseUrl}/admin/config/overrides`, body)
      .pipe(tap(() => this.loadOverrides()));
  }

  /** Delete an override (reset to bundled default). */
  deleteOverride(id: string): Observable<{deleted: boolean}> {
    return this.http
      .delete<{deleted: boolean}>(`${this.baseUrl}/admin/config/overrides/${id}`)
      .pipe(tap(() => this.loadOverrides()));
  }
}
