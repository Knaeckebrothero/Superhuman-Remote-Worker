import { Injectable, inject, signal, computed } from '@angular/core';
import { ApiService } from './api.service';
import { LLMRequest } from '../models/request.model';

/**
 * Service for managing LLM request viewer state.
 * Uses Angular signals for reactive state management.
 */
@Injectable({ providedIn: 'root' })
export class RequestService {
  private readonly api = inject(ApiService);

  // State signals
  readonly currentDocId = signal<string | null>(null);
  readonly request = signal<LLMRequest | null>(null);
  readonly isLoading = signal<boolean>(false);
  readonly error = signal<string | null>(null);

  // Computed values
  readonly hasRequest = computed(() => this.request() !== null);
  readonly messageCount = computed(() => this.request()?.request?.message_count ?? 0);

  readonly tokenSummary = computed(() => {
    const req = this.request();
    if (!req?.metrics?.token_usage) return null;

    const usage = req.metrics.token_usage;
    return {
      prompt: usage.prompt_tokens ?? 0,
      completion: usage.completion_tokens ?? 0,
      reasoning: usage.reasoning_tokens ?? 0,
    };
  });

  /**
   * Load a request by document ID.
   */
  loadRequest(docId: string): void {
    if (!docId || docId.trim() === '') {
      this.error.set('Please enter a document ID');
      return;
    }

    // Validate ObjectId format (24 hex characters)
    if (!/^[a-fA-F0-9]{24}$/.test(docId)) {
      this.error.set('Invalid document ID format (expected 24 hex characters)');
      return;
    }

    this.currentDocId.set(docId);
    this.isLoading.set(true);
    this.error.set(null);

    this.api.getRequest(docId).subscribe({
      next: (request) => {
        this.isLoading.set(false);
        if (request) {
          this.request.set(request);
          this.error.set(null);
        } else {
          this.request.set(null);
          this.error.set(`Request '${docId}' not found`);
        }
      },
      error: (err) => {
        this.isLoading.set(false);
        this.request.set(null);
        this.error.set(err.message || 'Failed to load request');
      },
    });
  }

  /**
   * Clear the current request.
   */
  clear(): void {
    this.currentDocId.set(null);
    this.request.set(null);
    this.error.set(null);
  }
}
