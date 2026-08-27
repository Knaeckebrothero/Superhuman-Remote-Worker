import { Injectable, computed, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { ApiService } from './api.service';
import { ChatEntry } from '../models/chat.model';

/**
 * Paged, lazy chat-history data source for the workbench Chat panel.
 *
 * Replaces the old eager "download the whole job into IndexedDB + slider window"
 * path (DataService) for the chat stream: fetches conversation turns from
 * `/api/jobs/{id}/chat` a page at a time (infinite scroll), driven directly by
 * the selected job. Nothing is downloaded up front, so it scales to large jobs
 * that previously 504'd the bulk endpoint.
 *
 * Pages are fetched with `lean=true` (previews + truncated markers only, no
 * full message bodies); when the user expands a truncated message or tool
 * result, `hydrateEntry()` fetches the complete turn from
 * `/api/jobs/{id}/chat/entry/{id}` and swaps it into `rows` in place — same
 * lean-list + detail-fetch split as the audit trace.
 *
 * See knowledge-base/knowledge/features/debug_audit_view_refactor.md (Phase 2c / P3).
 */
@Injectable({ providedIn: 'root' })
export class ChatTraceService {
  private readonly api = inject(ApiService);

  /** Turns fetched per page. The chat endpoint caps pageSize at 200. */
  private readonly PAGE_SIZE = 100;

  private readonly _jobId = signal<string | null>(null);
  readonly jobId = this._jobId.asReadonly();

  /** Turns loaded so far, in order (oldest first). */
  private readonly _rows = signal<ChatEntry[]>([]);
  readonly rows = this._rows.asReadonly();

  /** Total turns for the active job (drives the count + hasMore). */
  private readonly _total = signal<number>(0);
  readonly total = this._total.asReadonly();

  readonly loading = signal<boolean>(false); // first page
  readonly loadingMore = signal<boolean>(false); // subsequent pages
  readonly error = signal<string | null>(null);

  readonly hasMore = computed(() => this._rows().length < this._total());

  /** Last loaded page (1-indexed); 0 = none yet. */
  private _page = 0;

  /** Monotonic token so a stale in-flight load can't clobber a newer job. */
  private epoch = 0;

  /** In-flight per-entry hydrations (dedupe concurrent expands). */
  private hydrations = new Map<string, Promise<boolean>>();

  /** Select the job to show chat for. No-op if unchanged. */
  async setJob(jobId: string | null): Promise<void> {
    if (jobId === this._jobId()) return;
    this._jobId.set(jobId);
    await this.reload();
  }

  /** Re-fetch from the first page (job change, manual refresh). */
  async reload(): Promise<void> {
    const token = ++this.epoch;
    this._rows.set([]);
    this._total.set(0);
    this._page = 0;
    this.error.set(null);
    this.hydrations.clear();

    const jobId = this._jobId();
    if (!jobId) return;

    this.loading.set(true);
    try {
      const resp = await firstValueFrom(
        this.api.getChatHistory(jobId, 1, this.PAGE_SIZE, true),
      );
      if (token !== this.epoch) return; // superseded by a newer job
      if (resp.error) this.error.set(resp.error);
      this._total.set(resp.total);
      this._rows.set(resp.entries);
      this._page = 1;
    } catch (e) {
      if (token === this.epoch) {
        this.error.set(e instanceof Error ? e.message : 'Failed to load chat');
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
    const nextPage = this._page + 1;

    this.loadingMore.set(true);
    try {
      const resp = await firstValueFrom(
        this.api.getChatHistory(jobId, nextPage, this.PAGE_SIZE, true),
      );
      if (token !== this.epoch) return; // job changed mid-flight
      if (resp.entries.length > 0) {
        this._rows.update((rows) => [...rows, ...resp.entries]);
        this._page = nextPage;
      }
      if (resp.total) this._total.set(resp.total);
    } catch {
      // Transient; leave rows as-is — scrolling again retries.
    } finally {
      if (token === this.epoch) this.loadingMore.set(false);
    }
  }

  /**
   * Replace one lean row with its full detail (complete message bodies).
   *
   * Used when the user expands a truncated message/tool result. Resolves true
   * once the row in `rows` carries full content; concurrent calls for the
   * same entry share one request.
   */
  hydrateEntry(entryId: string): Promise<boolean> {
    const jobId = this._jobId();
    if (!jobId) return Promise.resolve(false);
    const inflight = this.hydrations.get(entryId);
    if (inflight) return inflight;

    const token = this.epoch;
    const promise = (async () => {
      try {
        const full = await firstValueFrom(this.api.getChatEntry(jobId, entryId));
        if (!full || token !== this.epoch) return false;
        this._rows.update((rows) =>
          rows.map((r) => (r._id === entryId ? full : r)),
        );
        return true;
      } catch {
        return false; // transient; the preview stays usable
      } finally {
        this.hydrations.delete(entryId);
      }
    })();
    this.hydrations.set(entryId, promise);
    return promise;
  }

  /** Manual refresh (re-fetch the current job from the top). */
  async refresh(): Promise<void> {
    await this.reload();
  }
}
