import {inject, Injectable, signal} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {catchError, Observable, of, tap} from 'rxjs';
import {
  CatalogModel,
  CatalogModelCreateRequest,
  CatalogModelDeleteResult,
  CatalogModelTestResult,
  CatalogModelUpdateRequest,
} from '../models/api.model';
import {environment} from '../environment';

/**
 * REST client for the `/api/admin/providers/models` surface and the
 * sibling `/api/admin/families` family-list endpoint. Every call is gated
 * by the `srw-admin` role server-side; the client does not re-check.
 *
 * Mirrors the shape of `AdminProvidersService` — signal-cached lists,
 * `tap(() => loadModels())` after mutations so subscribers see the new
 * state without a manual refresh.
 */
@Injectable({providedIn: 'root'})
export class AdminModelsService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.apiUrl;

  readonly models = signal<CatalogModel[]>([]);
  readonly families = signal<string[]>([]);
  readonly loading = signal(false);

  loadModels(): void {
    this.loading.set(true);
    this.http
      .get<CatalogModel[]>(`${this.baseUrl}/admin/providers/models`)
      .pipe(catchError(() => of([] as CatalogModel[])))
      .subscribe((rows) => {
        this.models.set(rows);
        this.loading.set(false);
      });
  }

  loadFamilies(): void {
    this.http
      .get<{families: string[]}>(`${this.baseUrl}/admin/families`)
      .pipe(catchError(() => of({families: [] as string[]})))
      .subscribe((res) => this.families.set(res.families));
  }

  createModel(body: CatalogModelCreateRequest): Observable<CatalogModel> {
    return this.http
      .post<CatalogModel>(`${this.baseUrl}/admin/providers/models`, body)
      .pipe(tap(() => this.loadModels()));
  }

  updateModel(id: string, body: CatalogModelUpdateRequest): Observable<CatalogModel> {
    return this.http
      .patch<CatalogModel>(`${this.baseUrl}/admin/providers/models/${id}`, body)
      .pipe(tap(() => this.loadModels()));
  }

  deleteModel(id: string): Observable<CatalogModelDeleteResult> {
    return this.http
      .delete<CatalogModelDeleteResult>(`${this.baseUrl}/admin/providers/models/${id}`)
      .pipe(tap(() => this.loadModels()));
  }

  testModel(id: string): Observable<CatalogModelTestResult> {
    return this.http.post<CatalogModelTestResult>(
      `${this.baseUrl}/admin/providers/models/${id}/test`,
      {},
    );
  }
}
