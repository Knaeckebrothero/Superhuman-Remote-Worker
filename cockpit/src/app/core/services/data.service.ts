import { Injectable, inject, signal, computed, effect } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { ApiService, JobVersionInfo } from './api.service';
import { IndexedDbService } from './indexed-db.service';
import { AuditEntry, AuditFilterCategory, JobSummary } from '../models/audit.model';
import { ChatEntry } from '../models/chat.model';
import { GraphDelta } from '../models/graph.model';
import { JobCacheMetadata } from '../models/cache.model';

/** Filter step types mapping */
const FILTER_STEP_TYPES: Record<AuditFilterCategory, string[] | null> = {
  all: null, // No filtering
  messages: ['llm'],
  tools: ['tool'],
  errors: ['error'],
};

/**
 * Central data service for the cockpit.
 * Replaces fragmented services with a unified index-based data access layer.
 *
 * Key features:
 * - IndexedDB caching for fast repeat access
 * - Sliding window of ~1000 entries in memory
 * - Index-based slider (not timestamp-based)
 * - Computed filtered views for components
 */
@Injectable({ providedIn: 'root' })
export class DataService {
  private readonly api = inject(ApiService);
  private readonly db = inject(IndexedDbService);

  // ===== Window Configuration =====
  private readonly WINDOW_SIZE = 1000;
  private readonly WINDOW_PADDING = 200; // Load more when within 200 of edge
  private readonly BULK_FETCH_SIZE = 5000;

  // ===== Core State Signals =====

  /** Currently selected job ID */
  private readonly _currentJobId = signal<string | null>(null);
  readonly currentJobId = this._currentJobId.asReadonly();

  /** Current slider position (index into total entries) */
  private readonly _sliderIndex = signal<number>(0);
  readonly sliderIndex = this._sliderIndex.asReadonly();

  /** Maximum valid index (total entries - 1) */
  private readonly _maxIndex = signal<number>(0);
  readonly maxIndex = this._maxIndex.asReadonly();

  /** Start index of current window in memory */
  private readonly _windowStart = signal<number>(0);

  /** Audit entries currently in memory (the window) */
  private readonly _liveAuditEntries = signal<AuditEntry[]>([]);

  /** Chat entries currently in memory */
  private readonly _liveChatEntries = signal<ChatEntry[]>([]);

  /** Graph deltas currently in memory */
  private readonly _liveGraphDeltas = signal<GraphDelta[]>([]);

  /** Active filter category */
  private readonly _activeFilter = signal<AuditFilterCategory>('all');
  readonly activeFilter = this._activeFilter.asReadonly();

  /** List of jobs */
  readonly jobs = signal<JobSummary[]>([]);

  // ===== Loading State =====
  readonly isLoading = signal<boolean>(false);
  readonly loadingProgress = signal<number>(0); // 0-100
  readonly error = signal<string | null>(null);

  // ===== Cache State =====
  readonly isCached = signal<boolean>(false);
  readonly cacheMetadata = signal<JobCacheMetadata | null>(null);

  constructor() {
    // Effect to reload window when slider moves outside current window
    effect(() => {
      const index = this._sliderIndex();
      const windowStart = this._windowStart();
      const windowEntries = this._liveAuditEntries();
      const windowEnd = windowStart + windowEntries.length;

      // Check if slider is near window edges
      if (
        windowEntries.length > 0 &&
        (index < windowStart + this.WINDOW_PADDING || index > windowEnd - this.WINDOW_PADDING)
      ) {
        // Need to recenter window - but only if we have a job loaded
        const jobId = this._currentJobId();
        if (jobId) {
          this.loadWindow(index);
        }
      }
    });
  }

  // ===== Computed Values =====

  /** Total number of audit entries for current job */
  readonly totalAuditEntries = computed(() => {
    return this._maxIndex() + 1;
  });

  /**
   * Audit entries visible at current slider position.
   * Returns entries from index 0 to sliderIndex (inclusive).
   * Applies active filter.
   */
  readonly visibleAuditEntries = computed(() => {
    const entries = this._liveAuditEntries();
    const windowStart = this._windowStart();
    const sliderIndex = this._sliderIndex();
    const filter = this._activeFilter();
    const stepTypes = FILTER_STEP_TYPES[filter];

    // Calculate visible range within window
    const visibleStart = Math.max(0, 0 - windowStart);
    const visibleEnd = Math.min(entries.length, sliderIndex - windowStart + 1);

    if (visibleEnd <= visibleStart) {
      return [];
    }

    let visible = entries.slice(visibleStart, visibleEnd);

    // Apply filter if needed
    if (stepTypes) {
      visible = visible.filter((e) => stepTypes.includes(e.step_type));
    }

    return visible;
  });

  /**
   * All audit entries in current window (for rendering).
   */
  readonly windowedAuditEntries = computed(() => {
    return this._liveAuditEntries();
  });

  /**
   * Chat entries visible at current slider position.
   */
  readonly visibleChatEntries = computed(() => {
    const entries = this._liveChatEntries();
    const sliderIndex = this._sliderIndex();
    const windowStart = this._windowStart();

    // Chat entries are indexed by sequence_number
    // Filter to those that occurred before current audit index
    // For simplicity, show all loaded chat entries up to slider position
    return entries.filter((e) => e.sequence_number <= sliderIndex - windowStart + 1);
  });

  /**
   * Graph deltas visible at current slider position.
   */
  readonly visibleGraphDeltas = computed(() => {
    const deltas = this._liveGraphDeltas();
    const sliderIndex = this._sliderIndex();

    // Filter to deltas that occurred at or before current index
    return deltas.filter((d) => d.toolCallIndex <= sliderIndex);
  });

  /**
   * Current timestamp from the entry at slider position.
   */
  readonly currentTimestamp = computed(() => {
    const entries = this._liveAuditEntries();
    const sliderIndex = this._sliderIndex();
    const windowStart = this._windowStart();
    const localIndex = sliderIndex - windowStart;

    if (localIndex >= 0 && localIndex < entries.length) {
      return entries[localIndex].timestamp;
    }
    return null;
  });

  /**
   * Time range of loaded data.
   */
  readonly timeRange = computed(() => {
    const metadata = this.cacheMetadata();
    if (!metadata) return null;
    return {
      start: metadata.firstTimestamp,
      end: metadata.lastTimestamp,
    };
  });

  // ===== Public Methods =====

  /**
   * Load list of jobs from the API.
   */
  async loadJobs(): Promise<void> {
    this.isLoading.set(true);
    this.error.set(null);

    try {
      const jobs = await firstValueFrom(this.api.getJobs());
      this.jobs.set(jobs);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : 'Failed to load jobs');
    } finally {
      this.isLoading.set(false);
    }
  }

  /**
   * Load a job's data. Checks cache first, fetches from API if needed.
   */
  async loadJob(jobId: string): Promise<void> {
    if (jobId === this._currentJobId()) {
      return; // Already loaded
    }

    this._currentJobId.set(jobId);
    this.isLoading.set(true);
    this.loadingProgress.set(0);
    this.error.set(null);

    try {
      // Check if we have cached data
      const metadata = await this.db.getJobMetadata(jobId);
      const versionInfo = await firstValueFrom(this.api.getJobVersion(jobId));

      const isCacheValid =
        metadata && versionInfo && metadata.auditEntryCount === versionInfo.auditEntryCount;

      if (isCacheValid) {
        // Use cached data
        this.isCached.set(true);
        this.cacheMetadata.set(metadata);
        this._maxIndex.set(metadata.auditEntryCount - 1);

        // Load window at the end (most recent entries)
        await this.loadWindow(metadata.auditEntryCount - 1);
        this._sliderIndex.set(metadata.auditEntryCount - 1);
      } else {
        // Fetch from API and cache
        this.isCached.set(false);
        await this.fetchAndCacheJob(jobId);
      }
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : 'Failed to load job');
    } finally {
      this.isLoading.set(false);
    }
  }

  /**
   * Set the slider to a specific index.
   */
  setSliderIndex(index: number): void {
    const max = this._maxIndex();
    const clamped = Math.max(0, Math.min(index, max));
    this._sliderIndex.set(clamped);
  }

  /**
   * Move slider to the end (most recent).
   */
  seekToEnd(): void {
    this._sliderIndex.set(this._maxIndex());
  }

  /**
   * Move slider to the start.
   */
  seekToStart(): void {
    this._sliderIndex.set(0);
  }

  /**
   * Find the index of an entry at or after a given timestamp.
   * Used for timestamp-based navigation.
   */
  async seekToTimestamp(targetIso: string): Promise<void> {
    const jobId = this._currentJobId();
    if (!jobId) return;

    const targetMs = new Date(targetIso).getTime();

    // First check current window
    const entries = this._liveAuditEntries();
    const windowStart = this._windowStart();

    for (let i = 0; i < entries.length; i++) {
      const entryMs = new Date(entries[i].timestamp).getTime();
      if (entryMs >= targetMs) {
        this._sliderIndex.set(windowStart + i);
        return;
      }
    }

    // If not in current window, need to search in IndexedDB
    // For now, load the end of the data as fallback
    this._sliderIndex.set(this._maxIndex());
  }

  /**
   * Set the active filter category.
   */
  setFilter(filter: AuditFilterCategory): void {
    this._activeFilter.set(filter);
  }

  /**
   * Refresh the current job's data from the API.
   */
  async refresh(): Promise<void> {
    const jobId = this._currentJobId();
    if (!jobId) return;

    // Clear cache for this job
    await this.db.clearJob(jobId);
    this.isCached.set(false);

    // Reload
    this._currentJobId.set(null); // Force reload
    await this.loadJob(jobId);
  }

  /**
   * Clear all state.
   */
  clear(): void {
    this._currentJobId.set(null);
    this._sliderIndex.set(0);
    this._maxIndex.set(0);
    this._windowStart.set(0);
    this._liveAuditEntries.set([]);
    this._liveChatEntries.set([]);
    this._liveGraphDeltas.set([]);
    this.isCached.set(false);
    this.cacheMetadata.set(null);
    this.error.set(null);
  }

  // ===== Private Methods =====

  /**
   * Fetch all job data from API and cache in IndexedDB.
   */
  private async fetchAndCacheJob(jobId: string): Promise<void> {
    let totalFetched = 0;
    let hasMore = true;
    let offset = 0;

    // Fetch audit entries in chunks
    while (hasMore) {
      const response = await firstValueFrom(
        this.api.getJobAuditBulk(jobId, offset, this.BULK_FETCH_SIZE),
      );

      if (response.entries.length > 0) {
        await this.db.cacheAuditEntries(jobId, response.entries, offset);
        totalFetched += response.entries.length;
        this.loadingProgress.set(Math.min(90, (totalFetched / response.total) * 90));
      }

      hasMore = response.hasMore;
      offset += response.entries.length;

      // Update max index as we fetch
      this._maxIndex.set(totalFetched - 1);
    }

    // Fetch chat entries
    offset = 0;
    hasMore = true;
    while (hasMore) {
      const response = await firstValueFrom(this.api.getChatHistoryBulk(jobId, offset, this.BULK_FETCH_SIZE));

      if (response.entries.length > 0) {
        await this.db.cacheChatEntries(jobId, response.entries);
      }

      hasMore = response.hasMore;
      offset += response.entries.length;
    }

    // Fetch graph deltas
    offset = 0;
    hasMore = true;
    while (hasMore) {
      const response = await firstValueFrom(this.api.getGraphDeltasBulk(jobId, offset, this.BULK_FETCH_SIZE));

      if (response.deltas.length > 0) {
        await this.db.cacheGraphDeltas(jobId, response.deltas);
      }

      hasMore = response.hasMore;
      offset += response.deltas.length;
    }

    this.loadingProgress.set(100);

    // Update metadata
    const metadata = await this.db.getJobMetadata(jobId);
    if (metadata) {
      this.cacheMetadata.set(metadata);
      this._maxIndex.set(metadata.auditEntryCount - 1);
    }

    // Load window at the end
    await this.loadWindow(this._maxIndex());
    this._sliderIndex.set(this._maxIndex());
    this.isCached.set(true);
  }

  /**
   * Load a window of entries centered around the target index.
   */
  private async loadWindow(centerIndex: number): Promise<void> {
    const jobId = this._currentJobId();
    if (!jobId) return;

    const maxIndex = this._maxIndex();

    // Calculate window bounds
    const halfWindow = Math.floor(this.WINDOW_SIZE / 2);
    let start = Math.max(0, centerIndex - halfWindow);
    let end = Math.min(maxIndex, start + this.WINDOW_SIZE - 1);

    // Adjust start if we hit the end
    if (end === maxIndex) {
      start = Math.max(0, end - this.WINDOW_SIZE + 1);
    }

    // Load from IndexedDB
    const [auditEntries, chatEntries, graphDeltas] = await Promise.all([
      this.db.getAuditEntries(jobId, start, end),
      this.db.getChatEntries(jobId, start, end),
      this.db.getGraphDeltas(jobId, start, end),
    ]);

    this._windowStart.set(start);
    this._liveAuditEntries.set(auditEntries);
    this._liveChatEntries.set(chatEntries);
    this._liveGraphDeltas.set(graphDeltas);
  }
}
