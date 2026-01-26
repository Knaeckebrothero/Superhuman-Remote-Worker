import { Injectable, inject, signal, computed } from '@angular/core';
import { ApiService } from './api.service';
import { ChatEntry, ChatHistoryResponse } from '../models/chat.model';

/**
 * Signal-based state management service for chat history data.
 * Provides a clean sequential view of conversation turns.
 */
@Injectable({ providedIn: 'root' })
export class ChatService {
  private readonly api = inject(ApiService);

  // Core state signals
  readonly entries = signal<ChatEntry[]>([]);
  readonly selectedJobId = signal<string | null>(null);

  // Pagination state
  readonly currentPage = signal<number>(1);
  readonly pageSize = signal<number>(50);
  readonly totalEntries = signal<number>(0);
  readonly hasMore = signal<boolean>(false);

  // Loading and error state
  readonly isLoading = signal<boolean>(false);
  readonly error = signal<string | null>(null);

  // Computed values
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
   * Load chat history for a job.
   */
  loadChatHistory(jobId: string, page: number = -1): void {
    if (jobId !== this.selectedJobId()) {
      this.selectedJobId.set(jobId);
      this.entries.set([]);
    }

    this.currentPage.set(page);
    this.isLoading.set(true);
    this.error.set(null);

    this.api
      .getChatHistory(jobId, page, this.pageSize())
      .subscribe({
        next: (response: ChatHistoryResponse) => {
          this.entries.set(response.entries);
          this.totalEntries.set(response.total);
          this.hasMore.set(response.hasMore);
          this.error.set(response.error ?? null);
          // Update currentPage from response (handles page=-1 -> actual page)
          if (response.page && response.page > 0) {
            this.currentPage.set(response.page);
          }
          this.isLoading.set(false);
        },
        error: (err) => {
          this.error.set(err.message || 'Failed to load chat history');
          this.entries.set([]);
          this.totalEntries.set(0);
          this.hasMore.set(false);
          this.isLoading.set(false);
        },
      });
  }

  /**
   * Navigate to the next page.
   */
  nextPage(): void {
    const jobId = this.selectedJobId();
    if (this.canGoNext() && jobId) {
      this.loadChatHistory(jobId, this.currentPage() + 1);
    }
  }

  /**
   * Navigate to the previous page.
   */
  previousPage(): void {
    const jobId = this.selectedJobId();
    if (this.canGoPrev() && jobId) {
      this.loadChatHistory(jobId, this.currentPage() - 1);
    }
  }

  /**
   * Navigate to the first page.
   */
  firstPage(): void {
    const jobId = this.selectedJobId();
    if (this.currentPage() > 1 && jobId) {
      this.loadChatHistory(jobId, 1);
    }
  }

  /**
   * Navigate to the last page.
   */
  lastPage(): void {
    const jobId = this.selectedJobId();
    const total = this.totalPages();
    if (this.currentPage() < total && jobId) {
      this.loadChatHistory(jobId, total);
    }
  }

  /**
   * Refresh chat history for the current job.
   */
  refresh(): void {
    const jobId = this.selectedJobId();
    if (jobId) {
      this.loadChatHistory(jobId, this.currentPage());
    }
  }

  /**
   * Clear the current selection.
   */
  clear(): void {
    this.selectedJobId.set(null);
    this.entries.set([]);
    this.totalEntries.set(0);
    this.hasMore.set(false);
    this.currentPage.set(1);
    this.error.set(null);
  }
}
