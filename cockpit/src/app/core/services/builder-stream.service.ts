import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, catchError, of } from 'rxjs';
import { environment } from '../environment';
import { JobArtifactService } from './job-artifact.service';

/** A builder chat message (stored in builder_messages table). */
export interface BuilderMessage {
  id: string;
  session_id: string;
  role: 'user' | 'assistant';
  content: string | null;
  tool_calls: { tool: string; args: Record<string, unknown> }[] | null;
  created_at: string;
}

/** Builder session metadata. */
export interface BuilderSession {
  id: string;
  job_id: string | null;
  expert_id: string | null;
  created_at: string;
  updated_at: string;
  summary: string | null;
}

/** Callback interface for SSE stream events. */
export interface StreamCallbacks {
  onToken?: (text: string) => void;
  onToolCall?: (tool: string, args: Record<string, unknown>) => void;
  onDone?: () => void;
  onError?: (message: string) => void;
}

/**
 * SSE client for the builder chat endpoint.
 *
 * Handles session creation, message sending with SSE streaming,
 * and message history retrieval. Tool-call events are automatically
 * forwarded to JobArtifactService.
 */
@Injectable({ providedIn: 'root' })
export class BuilderStreamService {
  private readonly http = inject(HttpClient);
  private readonly artifacts = inject(JobArtifactService);
  private readonly baseUrl = environment.apiUrl;

  private abortController: AbortController | null = null;

  /** Create a new builder session. */
  createSession(expertId?: string): Observable<BuilderSession | null> {
    return this.http
      .post<BuilderSession>(`${this.baseUrl}/builder/sessions`, {
        expert_id: expertId ?? null,
      })
      .pipe(catchError(() => of(null)));
  }

  /** Get session details. */
  getSession(sessionId: string): Observable<BuilderSession | null> {
    return this.http.get<BuilderSession>(`${this.baseUrl}/builder/sessions/${sessionId}`).pipe(
      catchError(() => of(null)),
    );
  }

  /** Get message history for a session. */
  getMessages(sessionId: string): Observable<BuilderMessage[]> {
    return this.http
      .get<BuilderMessage[]>(`${this.baseUrl}/builder/sessions/${sessionId}/messages`)
      .pipe(catchError(() => of([])));
  }

  /**
   * Send a message and stream the AI response via SSE.
   *
   * Uses fetch() directly (not HttpClient) because Angular's HttpClient
   * doesn't support streaming response bodies. The SSE events are parsed
   * manually from the response stream.
   *
   * Tool-call events are automatically applied to JobArtifactService.
   */
  async sendMessage(
    sessionId: string,
    message: string,
    callbacks: StreamCallbacks,
  ): Promise<void> {
    // Abort any in-flight stream
    this.abort();
    this.abortController = new AbortController();

    this.artifacts.streaming.set(true);

    const body = {
      message,
      instructions: this.artifacts.instructions(),
      config: this.artifacts.config(),
      description: this.artifacts.description(),
    };

    try {
      const response = await fetch(
        `${this.baseUrl}/builder/sessions/${sessionId}/message`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
          signal: this.abortController.signal,
        },
      );

      if (!response.ok) {
        const text = await response.text();
        callbacks.onError?.(`HTTP ${response.status}: ${text}`);
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) {
        callbacks.onError?.('No response body');
        return;
      }

      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // Parse SSE events from buffer
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? ''; // Keep incomplete line in buffer

        let currentEvent = '';
        for (const line of lines) {
          if (line.startsWith('event: ')) {
            currentEvent = line.slice(7).trim();
          } else if (line.startsWith('data: ') && currentEvent) {
            const dataStr = line.slice(6);
            try {
              const data = JSON.parse(dataStr);
              this.handleEvent(currentEvent, data, callbacks);
            } catch {
              // Ignore malformed JSON
            }
            currentEvent = '';
          } else if (line === '') {
            currentEvent = '';
          }
        }
      }
    } catch (err: unknown) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        // User-initiated abort, not an error
        return;
      }
      callbacks.onError?.((err as Error).message ?? 'Stream failed');
    } finally {
      this.artifacts.streaming.set(false);
      this.abortController = null;
    }
  }

  /** Abort an in-flight stream. */
  abort(): void {
    if (this.abortController) {
      this.abortController.abort();
      this.abortController = null;
      this.artifacts.streaming.set(false);
    }
  }

  private handleEvent(
    event: string,
    data: Record<string, unknown>,
    callbacks: StreamCallbacks,
  ): void {
    switch (event) {
      case 'token':
        callbacks.onToken?.(data['text'] as string);
        break;

      case 'tool_call': {
        const tool = data['tool'] as string;
        const args = data['args'] as Record<string, unknown>;
        // Apply to artifact state
        this.artifacts.applyToolCall(tool, args);
        // Notify UI
        callbacks.onToolCall?.(tool, args);
        break;
      }

      case 'done':
        callbacks.onDone?.();
        break;

      case 'error':
        callbacks.onError?.(data['message'] as string);
        break;
    }
  }
}
