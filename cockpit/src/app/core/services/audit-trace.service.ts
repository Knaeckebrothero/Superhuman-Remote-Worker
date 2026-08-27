import { Injectable, computed, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { ApiService } from './api.service';
import { AuditEntry, AuditFilterCategory } from '../models/audit.model';

/**
 * Paged, lazy audit-trace data source for the Agent Activity view.
 *
 * Replaces the old eager "download the whole job into IndexedDB + slider window"
 * path (DataService) for the audit stream. It fetches lean rows from
 * `/api/jobs/{id}/audit?lean=true` a page at a time (infinite scroll), filters
 * server-side, and lazy-loads a step's heavy detail (arguments / traceback /
 * state / metadata) only when the row is expanded. Nothing is downloaded up
 * front and nothing is cached in IndexedDB.
 *
 * See knowledge-base/knowledge/features/debug_audit_view_refactor.md (Phase 2).
 */
@Injectable({ providedIn: 'root' })
export class AuditTraceService {
  private readonly api = inject(ApiService);

  /** Lean rows fetched per page. The audit endpoint caps `limit` at 200. */
  private readonly PAGE_SIZE = 100;

  private readonly _jobId = signal<string | null>(null);
  readonly jobId = this._jobId.asReadonly();

  private readonly _filter = signal<AuditFilterCategory>('all');
  readonly filter = this._filter.asReadonly();

  /** Row order: 'asc' = oldest first (chronological), 'desc' = newest first. */
  private readonly _order = signal<'asc' | 'desc'>('asc');
  readonly order = this._order.asReadonly();

  /** Lean rows loaded so far, in order (oldest first). */
  private readonly _rows = signal<AuditEntry[]>([]);
  readonly rows = this._rows.asReadonly();

  /** Total rows for the active job + filter (drives the count + hasMore). */
  private readonly _total = signal<number>(0);
  readonly total = this._total.asReadonly();

  readonly loading = signal<boolean>(false); // first page
  readonly loadingMore = signal<boolean>(false); // subsequent pages
  readonly error = signal<string | null>(null);

  readonly hasMore = computed(() => this._rows().length < this._total());

  /** Heavy per-step detail, fetched lazily on expand, keyed by string id. */
  private readonly _details = signal<Record<string, AuditEntry>>({});

  /** Monotonic token so a stale in-flight load can't clobber a newer job/filter. */
  private epoch = 0;

  /** Select the job to trace. No-op if unchanged; resets filter to "all". */
  async setJob(jobId: string | null): Promise<void> {
    if (jobId === this._jobId()) return;
    this._jobId.set(jobId);
    this._filter.set('all');
    await this.reload();
  }

  /** Change the server-side filter and reload from the top. */
  async setFilter(filter: AuditFilterCategory): Promise<void> {
    if (filter === this._filter()) return;
    this._filter.set(filter);
    await this.reload();
  }

  /** Change the sort order (server-side) and reload from the top. */
  async setOrder(order: 'asc' | 'desc'): Promise<void> {
    if (order === this._order()) return;
    this._order.set(order);
    await this.reload();
  }

  /** Flip oldest-first ⇄ newest-first. */
  toggleOrder(): Promise<void> {
    return this.setOrder(this._order() === 'asc' ? 'desc' : 'asc');
  }

  /** Re-fetch from scratch (job change, filter change, manual refresh). */
  async reload(): Promise<void> {
    const token = ++this.epoch;
    this._rows.set([]);
    this._total.set(0);
    this._details.set({});
    this.error.set(null);

    const jobId = this._jobId();
    if (!jobId) return;

    this.loading.set(true);
    try {
      const resp = await firstValueFrom(
        this.api.getAuditPage(jobId, 0, this.PAGE_SIZE, this._filter(), this._order()),
      );
      if (token !== this.epoch) return; // superseded by a newer job/filter
      if (resp.error) this.error.set(resp.error);
      this._total.set(resp.total);
      this._rows.set(resp.entries);
    } catch (e) {
      if (token === this.epoch) {
        this.error.set(e instanceof Error ? e.message : 'Failed to load audit');
      }
    } finally {
      if (token === this.epoch) this.loading.set(false);
    }
  }

  /** Fetch the next page and append. Guards against in-flight / end-of-data. */
  async loadMore(): Promise<void> {
    if (this.loading() || this.loadingMore() || !this.hasMore()) return;
    const jobId = this._jobId();
    if (!jobId) return;
    const token = this.epoch;
    const offset = this._rows().length;

    this.loadingMore.set(true);
    try {
      const resp = await firstValueFrom(
        this.api.getAuditPage(jobId, offset, this.PAGE_SIZE, this._filter(), this._order()),
      );
      if (token !== this.epoch) return; // job/filter changed mid-flight
      if (resp.entries.length > 0) {
        this._rows.update((rows) => [...rows, ...resp.entries]);
      }
      if (resp.total) this._total.set(resp.total);
    } catch {
      // Transient; leave rows as-is — scrolling again retries.
    } finally {
      if (token === this.epoch) this.loadingMore.set(false);
    }
  }

  /** Lazily fetch a step's full detail (heavy payload + metadata). Cached. */
  async loadDetail(entry: AuditEntry): Promise<void> {
    const id = String(entry._id);
    if (!id || this._details()[id]) return;
    const jobId = this._jobId();
    if (!jobId) return;
    const full = await firstValueFrom(this.api.getAuditStep(jobId, id));
    if (full && id === String(full._id)) {
      this._details.update((d) => ({ ...d, [id]: full }));
    }
  }

  /** The full detail for a row if loaded, else the lean row itself. */
  detailFor(entry: AuditEntry): AuditEntry {
    return this._details()[String(entry._id)] ?? entry;
  }

  /** Manual refresh (re-fetch the current job + filter from the top). */
  async refresh(): Promise<void> {
    await this.reload();
  }
}
