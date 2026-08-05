import { Injectable, inject, signal, computed } from '@angular/core';
import { ApiService } from '../../core/services/api.service';
import { LLMRequest } from '../request.model';

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
    const usage = req?.metrics?.token_usage;

    // Real data available
    if (usage && Object.keys(usage).length > 0) {
      return {
        prompt: usage.prompt_tokens ?? 0,
        completion: usage.completion_tokens ?? 0,
        reasoning: usage.reasoning_tokens ?? 0,
        estimated: false,
      };
    }

    // Fallback: estimate from char counts (~4 chars per token)
    const inputChars = req?.metrics?.input_chars ?? 0;
    const outputChars = req?.metrics?.output_chars ?? 0;
    if (inputChars === 0 && outputChars === 0) return null;

    return {
      prompt: Math.ceil(inputChars / 4),
      completion: Math.ceil(outputChars / 4),
      reasoning: 0,
      estimated: true,
    };
  });

  /**
   * Load a request by document ID.
   */
  loadRequest(docId: string): void {
    const id = String(docId ?? '').trim();
    if (id === '') {
      this.error.set('Please enter a document ID');
      return;
    }

    // Accept a Mongo ObjectId (24 hex) or a Postgres integer id.
    if (!/^([a-fA-F0-9]{24}|\d+)$/.test(id)) {
      this.error.set(
        'Invalid document ID format (expected 24 hex chars or a numeric id)',
      );
      return;
    }

    this.currentDocId.set(id);
    this.isLoading.set(true);
    this.error.set(null);

    this.api.getRequest(id).subscribe({
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
