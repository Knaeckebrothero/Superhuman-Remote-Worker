import {inject, Injectable, signal} from '@angular/core';
import {HttpClient, HttpContext, HttpParams} from '@angular/common/http';
import {catchError, of} from 'rxjs';
import {environment} from '../environment';
import {VIEW_AS_OVERRIDE} from '../interceptors/view-as.interceptor';

/** Page-level visibility override forwarded to the view-as interceptor. */
export type UsageScope = 'all' | 'mine' | null;

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

/** One daily bucket within a usage time series. */
export interface UsageTsPoint { day: string; tokens: number; cost_usd: number; events: number; }
/** One series (a single user/model/project) of the stacked usage-over-time chart. */
export interface UsageSeries {
  key: string;
  label: string;
  is_admin?: boolean | null;
  events: number;
  points: UsageTsPoint[];
}
/** Response of `GET /api/usage/timeseries` — sorted day axis + per-key series. */
export interface UsageTimeseries {
  available: boolean;
  group_by: BreakdownDim;
  from?: string;
  to?: string;
  days: string[];
  series: UsageSeries[];
}

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

  /** HttpContext carrying the page-level scope override for the view-as interceptor. */
  private scopeCtx(scope: UsageScope): HttpContext {
    return new HttpContext().set(VIEW_AS_OVERRIDE, scope);
  }

  loadUsage(days = 30, scope: UsageScope = null): void {
    this.loading.set(true);
    const params = new HttpParams().set('days', String(days));
    this.http
      .get<UsageSummary>(`${this.baseUrl}/usage`, {params, context: this.scopeCtx(scope)})
      .pipe(catchError(() => of(EMPTY)))
      .subscribe((res) => {
        this.usage.set(res ?? EMPTY);
        this.loading.set(false);
      });
  }

  private readonly breakdowns = signal<Partial<Record<BreakdownDim, UsageBreakdown>>>({});
  breakdown(dim: BreakdownDim): UsageBreakdown | null { return this.breakdowns()[dim] ?? null; }

  loadBreakdown(groupBy: BreakdownDim, days = 30, scope: UsageScope = null): void {
    const params = new HttpParams().set('group_by', groupBy).set('days', String(days));
    this.http
      .get<UsageBreakdown>(`${this.baseUrl}/usage/breakdown`, {params, context: this.scopeCtx(scope)})
      .pipe(catchError(() => of({available: false, group_by: groupBy, rows: []} as UsageBreakdown)))
      .subscribe((res) => this.breakdowns.update((m) => ({...m, [groupBy]: res})));
  }

  private readonly timeseriesSig = signal<Partial<Record<BreakdownDim, UsageTimeseries>>>({});
  timeseries(dim: BreakdownDim): UsageTimeseries | null { return this.timeseriesSig()[dim] ?? null; }

  loadTimeseries(groupBy: BreakdownDim, days = 30, scope: UsageScope = null): void {
    const params = new HttpParams().set('group_by', groupBy).set('days', String(days));
    this.http
      .get<UsageTimeseries>(`${this.baseUrl}/usage/timeseries`, {params, context: this.scopeCtx(scope)})
      .pipe(catchError(() =>
        of({available: false, group_by: groupBy, days: [], series: []} as UsageTimeseries)))
      .subscribe((res) => this.timeseriesSig.update((m) => ({...m, [groupBy]: res})));
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
