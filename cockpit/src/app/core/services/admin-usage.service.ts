import {inject, Injectable, signal} from '@angular/core';
import {HttpClient, HttpParams} from '@angular/common/http';
import {catchError, of} from 'rxjs';
import {environment} from '../environment';

/** One (category, unit) usage aggregate row from `/api/usage`. */
export interface UsageCategoryRow {
  category: string;
  unit: string;
  quantity: number;
  cost_usd: number;
  events: number;
}

/** Response of `GET /api/usage` (Slice 4 usage ledger). */
export interface UsageSummary {
  by_category: UsageCategoryRow[];
  total_cost_usd: number;
  available: boolean;
  from?: string;
  to?: string;
}

const EMPTY: UsageSummary = {by_category: [], total_cost_usd: 0, available: false};

/**
 * REST client for the admin Usage view (`GET /api/usage`, Slice 4). Reads the
 * usage_events ledger aggregated by (category, unit), scoped server-side to the
 * caller's visibility (admins see the fleet). Auth rides the global interceptor;
 * a failed call degrades to an empty, unavailable summary (non-load-bearing tier).
 */
@Injectable({providedIn: 'root'})
export class AdminUsageService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.apiUrl;

  readonly usage = signal<UsageSummary | null>(null);
  readonly loading = signal(false);

  loadUsage(days = 30): void {
    this.loading.set(true);
    const params = new HttpParams().set('days', String(days));
    this.http
      .get<UsageSummary>(`${this.baseUrl}/usage`, {params})
      .pipe(catchError(() => of(EMPTY)))
      .subscribe((res) => {
        this.usage.set(res ?? EMPTY);
        this.loading.set(false);
      });
  }
}
