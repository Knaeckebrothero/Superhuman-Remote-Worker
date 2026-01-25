import { Injectable, inject, signal, computed } from '@angular/core';
import { ApiService } from './api.service';
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

  // Core state signals
  readonly jobs = signal<JobSummary[]>([]);
  readonly selectedJobId = signal<string | null>(null);
  readonly entries = signal<AuditEntry[]>([]);
  readonly activeFilter = signal<AuditFilterCategory>('all');
  readonly expandedIds = signal<Set<string>>(new Set());

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
   */
  selectJob(jobId: string | null): void {
    if (jobId === this.selectedJobId()) {
      return;
    }

    this.selectedJobId.set(jobId);
    this.currentPage.set(-1); // Request last page (most recent entries)
    this.expandedIds.set(new Set());

    if (jobId) {
      this.loadAuditEntries();
    } else {
      this.entries.set([]);
      this.totalEntries.set(0);
      this.hasMore.set(false);
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
   * Refresh audit entries for the current job.
   */
  refresh(): void {
    if (this.selectedJobId()) {
      this.loadAuditEntries();
    } else {
      this.loadJobs();
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
}
