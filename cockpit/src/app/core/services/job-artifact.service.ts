import {inject, Injectable, signal} from '@angular/core';
import {JobContextService} from './job-context.service';

/**
 * Form-state container for the job-create form: the single source of truth for
 * the `instructions` / `config` / `description` a user is composing, plus the
 * shared active-job selection.
 *
 * This service used to double as the instruction builder's AI-sync target
 * (applyToolCall, the streaming lock, builder session/model tracking, workspace
 * proposals). That half was parked in `_parked/job-artifact.service.ts` when the
 * builder was removed — see docs/features/builder_to_sessions_consolidation.md.
 * The dynamic canvas will revive it.
 */
@Injectable({ providedIn: 'root' })
export class JobArtifactService {
  private readonly jobContext = inject(JobContextService);

  /** Current instructions content — single source of truth. */
  readonly instructions = signal<string | null>(null);

  /** Current config override settings. */
  readonly config = signal<Record<string, unknown> | null>(null);

  /** Current job description. */
  readonly description = signal<string | null>(null);

  /** Active job context — delegates to JobContextService. */
  readonly activeJobId = this.jobContext.activeJobId;

  /** Reset all form state for a new job creation. */
  reset(): void {
    this.instructions.set(null);
    this.config.set(null);
    this.description.set(null);
  }
}
