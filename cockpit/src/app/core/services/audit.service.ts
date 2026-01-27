import { Injectable, inject, signal, computed, effect } from '@angular/core';
import { ApiService } from './api.service';
import { TimeService } from './time.service';
import {
  JobSummary,
  AuditEntry,
  AuditFilterCategory,
  AuditResponse,
} from '../models/audit.model';

/**
 * Signal-based state management service for agent audit data.
 * Manages job selection, audit entries, filtering, and pagination.
 */
@Injectable({ providedIn: 'root' })
export class AuditService {
  private readonly api = inject(ApiService);
  private readonly time = inject(TimeService);

  // Core state signals
  readonly jobs = signal<JobSummary[]>([]);
  readonly selectedJobId = signal<string | null>(null);
  readonly entries = signal<AuditEntry[]>([]);
  readonly activeFilter = signal<AuditFilterCategory>('all');
  readonly expandedIds = signal<Set<string>>(new Set());

  // Scroll target index (set by global time changes)
  readonly targetEntryIndex = signal<number | null>(null);

  // Tracks the last seek version we handled to avoid duplicate API calls
  private lastSeekVersion = 0;
  // Tracks whether we're currently navigating to a page from a seek
  private seekNavigating = false;

  constructor() {
    // When global slider is dragged, navigate to the correct page
    effect(() => {
      const seekVersion = this.time.globalSeekVersion();
      const timestamp = this.time.currentTimestamp();
      const jobId = this.selectedJobId();

      if (
        seekVersion <= this.lastSeekVersion ||
        !timestamp ||
        !jobId
      ) {
        return;
      }
      this.lastSeekVersion = seekVersion;

      // Ask the backend which page this timestamp falls on
      this.seekNavigating = true;
      this.api
        .getPageForTimestamp(
          jobId,
          timestamp,
          this.pageSize(),
          this.activeFilter(),
        )
        .subscribe({
          next: (result) => {
            const targetPage = result.page;
            if (targetPage !== this.currentPage()) {
              // Navigate to the correct page — the entry-level scroll
              // effect below will fire once entries load
              this.currentPage.set(targetPage);
              this.loadAuditEntries();
            }
            // Set the in-page scroll target index from the backend
            this.targetEntryIndex.set(result.index);
            this.seekNavigating = false;
          },
          error: () => {
            this.seekNavigating = false;
          },
        });
    });

    // Watch global time and compute scroll target within current page
    // (handles playback and minor adjustments that don't cross pages)
    effect(() => {
      const timestamp = this.time.currentTimestamp();
      if (!timestamp || this.seekNavigating) {
        if (!this.seekNavigating) {
          this.targetEntryIndex.set(null);
        }
        return;
      }
      const entries = this.entries();
      if (entries.length === 0) {
        this.targetEntryIndex.set(null);
        return;
      }
      const index = this.findEntryIndexAtTimestamp(timestamp, entries);
      this.targetEntryIndex.set(index);
    });
  }

  // Pagination state
  readonly currentPage = signal<number>(1);
  readonly pageSize = signal<number>(50);
  readonly totalEntries = signal<number>(0);
  readonly hasMore = signal<boolean>(false);

  // Loading and error state
  readonly isLoading = signal<boolean>(false);
  readonly error = signal<string | null>(null);

  // Computed values
  readonly selectedJob = computed(() => {
    const jobId = this.selectedJobId();
    return this.jobs().find((j) => j.id === jobId) ?? null;
  });

  readonly totalPages = computed(() => {
    const total = this.totalEntries();
    const size = this.pageSize();
    return Math.ceil(total / size) || 1;
  });

  readonly canGoNext = computed(() => {
    return this.currentPage() < this.totalPages();
  });

  readonly canGoPrev = computed(() => {
    return this.currentPage() > 1;
  });

  readonly paginationSummary = computed(() => {
    const page = this.currentPage();
    const size = this.pageSize();
    const total = this.totalEntries();
    const start = (page - 1) * size + 1;
    const end = Math.min(page * size, total);
    return total > 0 ? `${start}-${end} of ${total}` : 'No entries';
  });

  /**
   * Load list of jobs from the API.
   */
  loadJobs(): void {
    this.isLoading.set(true);
    this.error.set(null);

    this.api.getJobs().subscribe({
      next: (jobs) => {
        this.jobs.set(jobs);
        this.isLoading.set(false);
      },
      error: (err) => {
        this.error.set(err.message || 'Failed to load jobs');
        this.isLoading.set(false);
      },
    });
  }

  /**
   * Select a job and load its audit entries (starting from the last page).
   * Also loads the time range for the global timeline.
   */
  selectJob(jobId: string | null): void {
    if (jobId === this.selectedJobId()) {
      return;
    }

    this.selectedJobId.set(jobId);
    this.currentPage.set(-1); // Request last page (most recent entries)
    this.expandedIds.set(new Set());
    this.targetEntryIndex.set(null);

    if (jobId) {
      this.loadAuditEntries();
      // Load time range for global timeline
      this.time.loadTimeRange(jobId);
    } else {
      this.entries.set([]);
      this.totalEntries.set(0);
      this.hasMore.set(false);
      // Clear time service when no job selected
      this.time.clear();
    }
  }

  /**
   * Set the active filter category and reload entries.
   */
  setFilter(filter: AuditFilterCategory): void {
    if (filter === this.activeFilter()) {
      return;
    }

    this.activeFilter.set(filter);
    this.currentPage.set(1);
    this.expandedIds.set(new Set());

    if (this.selectedJobId()) {
      this.loadAuditEntries();
    }
  }

  /**
   * Toggle expanded state for an audit entry.
   */
  toggleExpanded(entryId: string): void {
    const current = this.expandedIds();
    const updated = new Set(current);

    if (updated.has(entryId)) {
      updated.delete(entryId);
    } else {
      updated.add(entryId);
    }

    this.expandedIds.set(updated);
  }

  /**
   * Check if an entry is expanded.
   */
  isExpanded(entryId: string): boolean {
    return this.expandedIds().has(entryId);
  }

  /**
   * Navigate to the next page.
   */
  nextPage(): void {
    if (this.canGoNext()) {
      this.currentPage.update((p) => p + 1);
      this.loadAuditEntries();
    }
  }

  /**
   * Navigate to the previous page.
   */
  previousPage(): void {
    if (this.canGoPrev()) {
      this.currentPage.update((p) => p - 1);
      this.loadAuditEntries();
    }
  }

  /**
   * Navigate to the first page.
   */
  firstPage(): void {
    if (this.currentPage() > 1) {
      this.currentPage.set(1);
      this.loadAuditEntries();
    }
  }

  /**
   * Navigate to the last page.
   */
  lastPage(): void {
    const total = this.totalPages();
    if (this.currentPage() < total) {
      this.currentPage.set(total);
      this.loadAuditEntries();
    }
  }

  /**
   * Refresh jobs list and audit entries for the current job.
   */
  refresh(): void {
    // Always reload jobs list
    this.loadJobs();

    // Also reload entries if a job is selected
    if (this.selectedJobId()) {
      this.loadAuditEntries();
      // Refresh time range as well
      this.time.loadTimeRange(this.selectedJobId()!);
    }
  }

  /**
   * Load audit entries from the API.
   */
  private loadAuditEntries(): void {
    const jobId = this.selectedJobId();
    if (!jobId) {
      return;
    }

    this.isLoading.set(true);
    this.error.set(null);

    this.api
      .getJobAudit(
        jobId,
        this.currentPage(),
        this.pageSize(),
        this.activeFilter(),
      )
      .subscribe({
        next: (response: AuditResponse) => {
          this.entries.set(response.entries);
          this.totalEntries.set(response.total);
          this.hasMore.set(response.hasMore);
          this.error.set(response.error ?? null);
          // Update currentPage from response (handles page=-1 → actual page)
          if (response.page && response.page > 0) {
            this.currentPage.set(response.page);
          }
          this.isLoading.set(false);
        },
        error: (err) => {
          this.error.set(err.message || 'Failed to load audit entries');
          this.entries.set([]);
          this.totalEntries.set(0);
          this.hasMore.set(false);
          this.isLoading.set(false);
        },
      });
  }

  /**
   * Find the entry index at or after a given timestamp.
   * Returns the index of the first entry with timestamp >= target.
   */
  private findEntryIndexAtTimestamp(
    targetIso: string,
    entries: AuditEntry[],
  ): number {
    if (entries.length === 0) return 0;

    const targetMs = new Date(targetIso).getTime();

    // Linear scan since entries are sorted by step_number
    for (let i = 0; i < entries.length; i++) {
      const entryMs = new Date(entries[i].timestamp).getTime();
      if (entryMs >= targetMs) {
        return i;
      }
    }

    // If target is after all entries, return last index
    return entries.length - 1;
  }
}
