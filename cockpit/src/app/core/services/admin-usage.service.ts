import {inject, Injectable, signal} from '@angular/core';
import {HttpClient, HttpContext, HttpParams} from '@angular/common/http';
import {catchError, of} from 'rxjs';
import {environment} from '../environment';
import {VIEW_AS_OVERRIDE} from '../interceptors/view-as.interceptor';

/** Page-level visibility override forwarded to the view-as interceptor. */
export type UsageScope = 'all' | 'mine' | null;
export type UsageWindow = number | {
  days?: number;
  fromIso?: string;
  toIso?: string;
};

/** One legacy (category, unit) usage aggregate row from `/api/usage`.
 * Category is required when interpreting reused units such as `gib-hour`. */
export interface UsageCategoryRow {
  category: string;
  unit: string;
  quantity: number;
  cost_usd: number;
  events: number;
}

export interface CloudEstimateComponent {
  category: string;
  unit: string;
  quantity: number;
  rate: number;
  capacity_per_billing_unit: number;
  amount: number;
  source_sku?: string | null;
  effective_from: string;
}

/** Current provider list-price revaluation of the measured compute quantities. */
export interface CloudEstimate {
  id: string;
  provider: string;
  display_name: string;
  region: string;
  currency: string;
  aggregation: 'sum' | 'max';
  estimate: number;
  priced_at: string;
  source_url: string;
  source_label: string;
  source_checked_at?: string | null;
  description: string;
  exclusions: string;
  components: CloudEstimateComponent[];
}

/** Response of `GET /api/usage` (Slice 4 usage ledger). */
export interface UsageSummary {
  by_category: UsageCategoryRow[];
  total_cost_usd: number;
  cache_hit_ratio: number;
  cloud_estimates?: CloudEstimate[];
  available: boolean;
  from?: string;
  to?: string;
}

/** Decimal-safe row model returned by the dark-launched `/api/usage/v2`. */
export interface UsageRowV2 {
  category: string;
  measurement_basis: 'api-consumed' | 'scheduler-request' | 'guest-provisioned'
    | 'claim-requested' | 'volume-provisioned' | 'actual' | 'legacy-unknown';
  cost_domain: 'external-service' | 'workload-allocation' | 'physical-asset'
    | 'idle' | 'overhead' | 'unknown';
  resource_class: 'llm-model' | 'kubernetes-pod' | 'virtual-machine'
    | 'persistent-volume-claim' | 'persistent-volume' | 'unknown';
  measurement_algorithm: string;
  resource: string;
  unit: string;
  attribution_scope: 'customer' | 'shared-platform' | 'unknown';
  quantity: string;
  finalized_quantity: string;
  confirmed_provisional_quantity: string;
  unverified_projected_quantity: string | null;
  ledger_cost: {
    status: 'priced' | 'partially-priced' | 'unpriced';
    currency: 'USD';
    amount: string | null;
    priced_quantity: string;
    unpriced_quantity: string;
  };
  events: number;
}

export interface UsageSummaryV2 {
  schema_version: 2;
  window: {
    start: string;
    end: string;
    as_of: string;
    data_through: string | null;
  };
  rows: UsageRowV2[];
  coverage: {
    status: 'complete' | 'partial' | 'unavailable';
    includes_provisional: boolean;
    required_sources_ok: number;
    required_sources_total: number;
    unknown_ranges: Array<{start: string; end: string | null}>;
    excluded_domains: string[];
  };
}

const EMPTY: UsageSummary = {
  by_category: [],
  total_cost_usd: 0,
  cache_hit_ratio: 0,
  cloud_estimates: [],
  available: false,
};

export interface UsageUnitAgg { quantity: number; cost_usd: number; events: number; }
export interface UsageBreakdownRow {
  key: string;
  label: string;
  is_admin?: boolean | null;
  events: number;
  cost_usd: number;
  cache_hit_ratio?: number;
  /** Legacy unit-only aggregation. It cannot distinguish the same unit emitted
   * by multiple categories; callers must avoid assigning such a value a more
   * specific meaning until the typed row API is available. */
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
  readonly usageV2 = signal<UsageSummaryV2 | null>(null);
  readonly loading = signal(false);
  private usageRequestGeneration = 0;
  private usageV2RequestGeneration = 0;

  /** HttpContext carrying the page-level scope override for the view-as interceptor. */
  private scopeCtx(scope: UsageScope): HttpContext {
    return new HttpContext().set(VIEW_AS_OVERRIDE, scope);
  }

  private windowParams(window: UsageWindow = 30): HttpParams {
    if (typeof window === 'number') return new HttpParams().set('days', String(window));

    let params = new HttpParams();
    if (window.days !== undefined) params = params.set('days', String(window.days));
    if (window.fromIso) params = params.set('from_date', window.fromIso);
    if (window.toIso) params = params.set('to_date', window.toIso);
    return params;
  }

  loadUsage(window: UsageWindow = 30, scope: UsageScope = null): void {
    const generation = ++this.usageRequestGeneration;
    this.usage.set(null);
    this.loading.set(true);
    const params = this.windowParams(window);
    this.http
      .get<UsageSummary>(`${this.baseUrl}/usage`, {params, context: this.scopeCtx(scope)})
      .pipe(catchError(() => of(EMPTY)))
      .subscribe((res) => {
        if (generation !== this.usageRequestGeneration) return;
        this.usage.set(res ?? EMPTY);
        this.loading.set(false);
      });
  }

  /** Exercise the typed contract during dark launch. The legacy dashboard does
   * not switch sources until v2 bootstrap/reconciliation has passed. */
  loadUsageV2(
    window: UsageWindow = 30,
    scope: UsageScope = null,
    includeNonCustomer = false,
  ): void {
    const generation = ++this.usageV2RequestGeneration;
    this.usageV2.set(null);
    let params = this.windowParams(window);
    if (includeNonCustomer) params = params.set('include_non_customer', 'true');
    this.http
      .get<UsageSummaryV2>(`${this.baseUrl}/usage/v2`, {
        params,
        context: this.scopeCtx(scope),
      })
      .pipe(catchError(() => of(null)))
      .subscribe((res) => {
        if (generation === this.usageV2RequestGeneration) this.usageV2.set(res);
      });
  }

  private readonly breakdowns = signal<Partial<Record<BreakdownDim, UsageBreakdown>>>({});
  breakdown(dim: BreakdownDim): UsageBreakdown | null { return this.breakdowns()[dim] ?? null; }

  loadBreakdown(groupBy: BreakdownDim, window: UsageWindow = 30, scope: UsageScope = null): void {
    const params = this.windowParams(window).set('group_by', groupBy);
    this.http
      .get<UsageBreakdown>(`${this.baseUrl}/usage/breakdown`, {params, context: this.scopeCtx(scope)})
      .pipe(catchError(() => of({available: false, group_by: groupBy, rows: []} as UsageBreakdown)))
      .subscribe((res) => this.breakdowns.update((m) => ({...m, [groupBy]: res})));
  }

  private readonly timeseriesSig = signal<Partial<Record<BreakdownDim, UsageTimeseries>>>({});
  timeseries(dim: BreakdownDim): UsageTimeseries | null { return this.timeseriesSig()[dim] ?? null; }

  loadTimeseries(groupBy: BreakdownDim, window: UsageWindow = 30, scope: UsageScope = null): void {
    const params = this.windowParams(window).set('group_by', groupBy);
    this.http
      .get<UsageTimeseries>(`${this.baseUrl}/usage/timeseries`, {params, context: this.scopeCtx(scope)})
      .pipe(catchError(() =>
        of({available: false, group_by: groupBy, days: [], series: []} as UsageTimeseries)))
      .subscribe((res) => this.timeseriesSig.update((m) => ({...m, [groupBy]: res})));
  }

  /** One-shot windowed fetch (used by the KPI trend's current-vs-previous pair). */
  loadUsageWindow(days: number, fromIso?: string, toIso?: string) {
    const params = this.windowParams({days, fromIso, toIso});
    return this.http
      .get<UsageSummary>(`${this.baseUrl}/usage`, {params})
      .pipe(catchError(() => of(EMPTY)));
  }
}
