import {
  Injectable,
  inject,
  signal,
  effect,
  Injector,
  runInInjectionContext,
  untracked,
} from '@angular/core';
import { ApiService } from './api.service';
import { IndexedDbService } from './indexed-db.service';
import { JobContextService } from './job-context.service';
import { JobSummary } from '../models/audit.model';

/**
 * Job-selection state for the workbench dashboard.
 *
 * Historically this also eagerly downloaded a job's entire audit/chat/graph
 * streams into IndexedDB and exposed a slider-windowed view — that bulk path
 * OOM'd the orchestrator on large jobs (see
 * knowledge-base/knowledge/features/debug_audit_view_refactor.md). The streams now load lazily via
 * their own paged services (AuditTraceService / ChatTraceService) and graph via
 * GraphService, all keyed off `currentJobId`. What remains here is the thin
 * selection state + the job-list auto-refresh that the panels react to.
 */
@Injectable({ providedIn: 'root' })
export class DataService {
  private readonly jobContext!: JobContextService;
  private injector: Injector | null = null;

  /**
   * Optional args put the service in test mode (skips Angular DI + effects).
   * Production construction takes no args and resolves deps via `inject()`.
   */
  constructor(api?: ApiService, db?: IndexedDbService) {
    if (api && db) {
      this.jobs = signal<JobSummary[]>([]);
    } else {
      this.injector = inject(Injector);
      this.jobContext = inject(JobContextService);
      this.jobs = this.jobContext.jobs;
      this.setupEffects();
    }
  }

  /** Currently selected job ID — the signal every workbench panel reacts to. */
  private readonly _currentJobId = signal<string | null>(null);
  readonly currentJobId = this._currentJobId.asReadonly();

  readonly isLoading = signal<boolean>(false);
  readonly error = signal<string | null>(null);

  /** List of jobs — delegates to JobContextService (fallback signal for test mode). */
  readonly jobs: ReturnType<typeof signal<JobSummary[]>>;

  // ===== Auto-Refresh State =====
  private readonly AUTO_REFRESH_INTERVAL = 15000; // 15 seconds
  private autoRefreshTimer: ReturnType<typeof setInterval> | null = null;
  readonly autoRefreshEnabled = signal<boolean>(false);

  private setupEffects(): void {
    if (!this.injector) return;
    runInInjectionContext(this.injector, () => {
      // Sync job selection from JobContextService.
      effect(() => {
        const jobId = this.jobContext.activeJobId();
        untracked(() => {
          if (jobId) {
            this.loadJob(jobId);
          } else {
            this.clearInternal();
          }
        });
      });
    });
  }

  /**
   * Set the current job. In production mode, delegates to JobContextService
   * (the effect handles calling loadJob). In test mode, calls loadJob directly.
   */
  setCurrentJob(jobId: string | null): void {
    if (this.jobContext) {
      this.jobContext.selectJob(jobId);
    }
    if (jobId) {
      this.loadJob(jobId);
    } else {
      this.clearInternal();
    }
  }

  /** Load list of jobs — delegates to JobContextService. */
  async loadJobs(): Promise<void> {
    if (this.jobContext) {
      return this.jobContext.loadJobs();
    }
  }

  /**
   * Select a job. The audit + chat streams load lazily via their own paged
   * trace services (driven by `currentJobId`); graph via GraphService. Nothing
   * is eagerly downloaded here anymore — that bulk path OOM'd large jobs.
   */
  async loadJob(jobId: string): Promise<void> {
    if (jobId === this._currentJobId()) {
      return; // Already selected
    }
    this._currentJobId.set(jobId);
    this.error.set(null);
  }

  /** Re-select the current job (panels expose their own content refresh). */
  async refresh(): Promise<void> {
    const jobId = this._currentJobId();
    if (!jobId) return;
    this._currentJobId.set(null);
    await this.loadJob(jobId);
  }

  /** Deselect the current job. */
  clear(): void {
    if (this.jobContext) {
      this.jobContext.selectJob(null);
    } else {
      this.clearInternal();
    }
  }

  private clearInternal(): void {
    this._currentJobId.set(null);
    this.error.set(null);
    this.stopAutoRefresh();
  }

  // ===== Auto-Refresh (job list only) =====

  startAutoRefresh(): void {
    if (this.autoRefreshTimer) return;
    this.autoRefreshEnabled.set(true);
    this.autoRefreshTimer = setInterval(() => {
      void this.autoRefreshTick();
    }, this.AUTO_REFRESH_INTERVAL);
  }

  stopAutoRefresh(): void {
    if (this.autoRefreshTimer) {
      clearInterval(this.autoRefreshTimer);
      this.autoRefreshTimer = null;
    }
    this.autoRefreshEnabled.set(false);
  }

  toggleAutoRefresh(): void {
    if (this.autoRefreshEnabled()) {
      this.stopAutoRefresh();
    } else {
      this.startAutoRefresh();
    }
  }

  /**
   * Refresh the job list. The panels' own paged services pick up new content on
   * the next interaction; per-stream live-tail is future work (P3).
   */
  private async autoRefreshTick(): Promise<void> {
    if (this.jobContext) {
      try {
        await this.jobContext.loadJobs();
      } catch (err) {
        console.warn('Auto-refresh error:', err);
      }
    }
  }
}
