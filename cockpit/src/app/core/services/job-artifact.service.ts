import { Injectable, signal } from '@angular/core';

/**
 * Deep merge two objects. Objects merge recursively, arrays replace entirely, null clears.
 */
function deepMerge(base: Record<string, unknown>, override: Record<string, unknown>): Record<string, unknown> {
  const result = { ...base };
  for (const [key, value] of Object.entries(override)) {
    if (value === null) {
      delete result[key];
    } else if (
      typeof value === 'object' &&
      !Array.isArray(value) &&
      typeof result[key] === 'object' &&
      !Array.isArray(result[key]) &&
      result[key] !== null
    ) {
      result[key] = deepMerge(result[key] as Record<string, unknown>, value as Record<string, unknown>);
    } else {
      result[key] = value;
    }
  }
  return result;
}

/**
 * Shared signal service for bidirectional artifact state between the
 * instruction builder chat and the job creation form.
 *
 * - Builder → Form: SSE tool-call events call applyToolCall(), form reactively updates.
 * - Form → Builder: User edits update signals directly (only when streaming is false).
 *   On the next chat message, the current artifact state is sent in the request payload.
 */
@Injectable({ providedIn: 'root' })
export class JobArtifactService {
  /** Current instructions content — single source of truth */
  readonly instructions = signal<string | null>(null);

  /** Current config override settings */
  readonly config = signal<Record<string, unknown> | null>(null);

  /** Current job description */
  readonly description = signal<string | null>(null);

  /** Builder session ID (null if no builder session started) */
  readonly sessionId = signal<string | null>(null);

  /** Whether AI is currently streaming (locks editor to prevent conflicts) */
  readonly streaming = signal<boolean>(false);

  /**
   * Apply an artifact mutation from a builder tool call.
   * Called by BuilderStreamService when tool_call SSE events arrive.
   */
  applyToolCall(tool: string, args: Record<string, unknown>): void {
    switch (tool) {
      case 'update_instructions':
        this.instructions.set(args['content'] as string);
        break;

      case 'edit_instructions':
        this.instructions.update((current) =>
          current?.replace(args['old_text'] as string, args['new_text'] as string) ?? current,
        );
        break;

      case 'insert_instructions':
        this.instructions.update((current) => {
          const content = args['content'] as string;
          if (!current) return content;
          if (args['line'] == null) return current + '\n' + content;
          const lines = current.split('\n');
          lines.splice((args['line'] as number) - 1, 0, content);
          return lines.join('\n');
        });
        break;

      case 'update_config':
        this.config.update((current) => deepMerge(current ?? {}, args));
        break;

      case 'update_description':
        this.description.set(args['content'] as string);
        break;
    }
  }

  /** Reset all state for a new job creation session */
  reset(): void {
    this.instructions.set(null);
    this.config.set(null);
    this.description.set(null);
    this.sessionId.set(null);
    this.streaming.set(false);
  }
}
