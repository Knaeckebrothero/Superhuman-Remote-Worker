import {effect, inject, Injectable, signal} from '@angular/core';
import {environment} from '../environment';
import {JobContextService} from './job-context.service';
import {ModelService} from './model.service';
import {SettingsService} from './settings.service';
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
const BUILDER_MODEL_KEY = 'builder_model';

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
  private readonly settingsService = inject(SettingsService);

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

  /** Selected builder model (persisted to localStorage + server settings) */
  readonly builderModel = signal<string>(this.loadBuilderModel());

  /** Pending workspace edit proposals awaiting user approval */
  readonly pendingWorkspaceEdits = signal<PendingWorkspaceEdit[]>([]);

  private editCounter = 0;
  private serverSynced = false;

  constructor() {
    // Hydrate builder model from server preferences when they load (if no local override)
    effect(() => {
      const prefs = this.settingsService.preferences();
      if (this.serverSynced || !prefs || Object.keys(prefs).length === 0) return;
      this.serverSynced = true;
      const serverModel = prefs.default_builder_model;
      if (serverModel && !this.loadBuilderModelFromStorage()) {
        this.builderModel.set(serverModel);
        this.saveBuilderModelToStorage(serverModel);
      }
    });
  }

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

  /** Persist builder model to localStorage and server settings. */
  persistBuilderModel(modelId: string): void {
    this.builderModel.set(modelId);
    this.saveBuilderModelToStorage(modelId);
    this.settingsService.updatePreferences({ default_builder_model: modelId }).subscribe();
  }

  /** Reset all state for a new job creation session (preserves builder model + job selection) */
  reset(): void {
    this.instructions.set(null);
    this.config.set(null);
    this.description.set(null);
    this.persistSessionId(null);
    this.streaming.set(false);
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

  private loadBuilderModel(): string {
    return this.loadBuilderModelFromStorage() ?? environment.builderModels[0]?.id ?? 'openai/gpt-oss-120b';
  }

  private loadBuilderModelFromStorage(): string | null {
    try {
      return localStorage.getItem(BUILDER_MODEL_KEY);
    } catch {
      return null;
    }
  }

  private saveBuilderModelToStorage(modelId: string): void {
    try {
      localStorage.setItem(BUILDER_MODEL_KEY, modelId);
    } catch {
      // localStorage may be unavailable
    }
  }
}
