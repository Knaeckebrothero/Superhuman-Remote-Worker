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

export interface UsageUnitAgg { quantity: number; cost_usd: number; events: number; }
export interface UsageBreakdownRow {
  key: string;
  label: string;
  is_admin?: boolean | null;
  events: number;
  cost_usd: number;
  units: Record<string, UsageUnitAgg>;
}
export interface UsageBreakdown {
  available: boolean;
  group_by: 'user' | 'model' | 'project';
  from?: string;
  to?: string;
  rows: UsageBreakdownRow[];
}
export type BreakdownDim = 'user' | 'model' | 'project';

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

  private readonly breakdowns = signal<Partial<Record<BreakdownDim, UsageBreakdown>>>({});
  breakdown(dim: BreakdownDim): UsageBreakdown | null { return this.breakdowns()[dim] ?? null; }

  loadBreakdown(groupBy: BreakdownDim, days = 30): void {
    const params = new HttpParams().set('group_by', groupBy).set('days', String(days));
    this.http
      .get<UsageBreakdown>(`${this.baseUrl}/usage/breakdown`, {params})
      .pipe(catchError(() => of({available: false, group_by: groupBy, rows: []} as UsageBreakdown)))
      .subscribe((res) => this.breakdowns.update((m) => ({...m, [groupBy]: res})));
  }

  /** One-shot windowed fetch (used by the KPI trend's current-vs-previous pair). */
  loadUsageWindow(days: number, fromIso?: string, toIso?: string) {
    let params = new HttpParams().set('days', String(days));
    if (fromIso) params = params.set('from_date', fromIso);
    if (toIso) params = params.set('to_date', toIso);
    return this.http
      .get<UsageSummary>(`${this.baseUrl}/usage`, {params})
      .pipe(catchError(() => of(EMPTY)));
  }
}
