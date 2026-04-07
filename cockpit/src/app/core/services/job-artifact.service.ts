import {inject, Injectable, signal} from '@angular/core';
import {environment} from '../environment';
import {JobContextService} from './job-context.service';
import {ModelService} from './model.service';
import type {WorkspaceProposal} from './builder-stream.service';

/** A pending workspace edit awaiting user approval. */
export interface PendingWorkspaceEdit {
  id: string;
  proposal: WorkspaceProposal;
  status: 'pending' | 'approved' | 'dismissed';
}

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

const SESSION_STORAGE_KEY = 'builder_session_id';

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
  private readonly jobContext = inject(JobContextService);
  private readonly modelService = inject(ModelService);

  /** Current instructions content — single source of truth */
  readonly instructions = signal<string | null>(null);

  /** Current config override settings */
  readonly config = signal<Record<string, unknown> | null>(null);

  /** Current job description */
  readonly description = signal<string | null>(null);

  /** Builder session ID (null if no builder session started) */
  readonly sessionId = signal<string | null>(this.loadSessionId());

  /** Current session title (auto-generated after first exchange) */
  readonly sessionTitle = signal<string | null>(null);

  /** Active job context — delegates to JobContextService */
  readonly activeJobId = this.jobContext.activeJobId;

  /** Whether AI is currently streaming (locks editor to prevent conflicts) */
  readonly streaming = signal<boolean>(false);

  /** Selected builder model */
  readonly builderModel = signal<string>(environment.builderModels[0]?.id ?? 'openai/gpt-oss-120b');

  /** Pending workspace edit proposals awaiting user approval */
  readonly pendingWorkspaceEdits = signal<PendingWorkspaceEdit[]>([]);

  private editCounter = 0;

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

  /** Add a workspace edit proposal for user approval. */
  addWorkspaceProposal(proposal: WorkspaceProposal): string {
    const id = `ws-edit-${++this.editCounter}`;
    const edit: PendingWorkspaceEdit = { id, proposal, status: 'pending' };
    this.pendingWorkspaceEdits.update((edits) => [...edits, edit]);
    return id;
  }

  /** Resolve a pending workspace edit as approved or dismissed. */
  resolveWorkspaceEdit(id: string, status: 'approved' | 'dismissed'): void {
    this.pendingWorkspaceEdits.update((edits) =>
      edits.map((e) => (e.id === id ? { ...e, status } : e)),
    );
  }

  /** Persist sessionId to localStorage so it survives page refresh. */
  persistSessionId(id: string | null): void {
    this.sessionId.set(id);
    if (!id) {
      this.sessionTitle.set(null);
    }
    try {
      if (id) {
        localStorage.setItem(SESSION_STORAGE_KEY, id);
      } else {
        localStorage.removeItem(SESSION_STORAGE_KEY);
      }
    } catch {
      // localStorage may be unavailable (e.g. private browsing)
    }
  }

  /** Reset all state for a new job creation session (preserves job selection) */
  reset(): void {
    this.instructions.set(null);
    this.config.set(null);
    this.description.set(null);
    this.persistSessionId(null);
    this.streaming.set(false);
    const models = this.modelService.builderModels();
    this.builderModel.set(models[0]?.id ?? environment.builderModels[0]?.id ?? 'openai/gpt-oss-120b');
    this.pendingWorkspaceEdits.set([]);
    this.editCounter = 0;
  }

  /** Load sessionId from localStorage on service init. */
  private loadSessionId(): string | null {
    try {
      return localStorage.getItem(SESSION_STORAGE_KEY);
    } catch {
      return null;
    }
  }
}
