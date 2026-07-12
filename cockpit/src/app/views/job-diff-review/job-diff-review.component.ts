import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { TranslocoPipe, TranslocoService } from '@jsverse/transloco';
import { ApiService } from '../../core/services/api.service';
import {
  JobAcceptConflict,
  JobAcceptPartialFailure,
  JobDiffFile,
  JobDiffFileEntry,
  JobDiffSummary,
  ThreadCloudDiffFile,
  ThreadCloudDiffSummary,
} from '../../core/models/api.model';
import { AppButtonComponent } from '../../ui/button';
import { AppBadgeComponent, type BadgeTone } from '../../ui/badge';
import { AppSpinnerComponent } from '../../ui/spinner';
import { AppDialogComponent } from '../../ui/dialog';
import { AppToastService } from '../../ui/toast';

/** A file-tree entry from either diff summary shape (job or thread mode). */
type DiffFileEntry = JobDiffFileEntry | ThreadCloudDiffSummary['files'][number];
/** The loaded summary, from either mode. */
type DiffSummary = JobDiffSummary | ThreadCloudDiffSummary;
/** The loaded per-file content, from either mode. */
type DiffFile = JobDiffFile | ThreadCloudDiffFile;

/**
 * Exactly one of `jobId`/`threadId` must be bound — this is the host's
 * wiring contract, asserted (thrown) rather than silently defaulted so a
 * mistake fails loudly in dev rather than rendering an empty panel.
 */
export function diffApiFor(jobId: string | null, threadId: string | null): 'job' | 'thread' {
  if (jobId && threadId) {
    throw new Error('app-job-diff-review: both jobId and threadId are set — bind exactly one.');
  }
  if (jobId) return 'job';
  if (threadId) return 'thread';
  throw new Error('app-job-diff-review: neither jobId nor threadId is set — bind exactly one.');
}

/**
 * Whether the currently-selected entry should render the binary
 * placeholder instead of the Monaco diff editor. True when the file-tree
 * entry itself is flagged binary (thread mode's summary carries this
 * up-front) OR the loaded file content reports either side as binary
 * (belt-and-suspenders — job mode's `JobDiffFile` has neither field, so
 * this is always false there).
 */
export function isBinaryEntry(
  sum: { binary?: boolean },
  file: { old_binary?: boolean; new_binary?: boolean } | null,
): boolean {
  return !!(sum.binary || file?.old_binary || file?.new_binary);
}

/**
 * Mode A / protected-cloud-mode diff review.
 *
 * Job mode (`jobId` bound): project-attached jobs in `pending_review`,
 * diffed against the Gitea baseline commit (docs/done/job_cloud_export.md
 * §3.4–§3.6). Thread mode (`threadId` bound): a persistent session's staged
 * protected-cloud overlay diff (Slice C, Task 8/10 — see
 * docs/design/cloud_access_unification.md §5/§11). Both modes share this
 * file tree (left) + Monaco diff editor (right) + accept/reject actions
 * (footer) shell; `diffApiFor` picks the backend calls per mode. Handles the
 * external-mod 409 (both modes) and the epoch-stale 409 (thread mode only)
 * by surfacing inline banners/notices.
 */
@Component({
  selector: 'app-job-diff-review',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    CommonModule,
    TranslocoPipe,
    AppButtonComponent,
    AppBadgeComponent,
    AppSpinnerComponent,
    AppDialogComponent,
  ],
  templateUrl: './job-diff-review.component.html',
  styleUrl: './job-diff-review.component.scss',
})
export class JobDiffReviewComponent {
  private api = inject(ApiService);
  private destroy = inject(DestroyRef);
  private toast = inject(AppToastService);
  private translocoService = inject(TranslocoService);

  /** Bind exactly one of jobId/threadId — see `diffApiFor`. */
  jobId = input<string | null>(null);
  threadId = input<string | null>(null);
  resolved = output<'accepted' | 'rejected'>();

  protected mode = computed<'job' | 'thread'>(() => diffApiFor(this.jobId(), this.threadId()));

  protected summary = signal<DiffSummary | null>(null);
  protected loadingDiff = signal<boolean>(true);
  protected selectedPath = signal<string | null>(null);
  protected selectedEntry = signal<DiffFileEntry | null>(null);
  protected selectedFile = signal<DiffFile | null>(null);
  protected loadingFile = signal<boolean>(false);
  protected fileLoadFailed = signal<boolean>(false);
  protected conflict = signal<JobAcceptConflict | null>(null);
  protected partial = signal<JobAcceptPartialFailure | null>(null);
  protected accepting = signal<boolean>(false);
  protected rejecting = signal<boolean>(false);
  protected showAcceptConfirm = signal<boolean>(false);
  protected showRejectConfirm = signal<boolean>(false);
  protected monacoFailed = signal<boolean>(false);
  /** Thread mode only: the epoch the last-loaded summary was pinned to —
   *  threaded back into apply/reject as the optimistic-concurrency pin. */
  protected epoch = signal<number | null>(null);

  protected diffContainer = viewChild<ElementRef<HTMLDivElement>>('diffContainer');

  protected fileCount = computed(() => this.summary()?.files.length ?? 0);
  protected hasFiles = computed(() => this.fileCount() > 0);
  /** Binary entries (either side) render a placeholder instead of Monaco —
   *  a diff editor over binary content is meaningless, and thread mode's
   *  UpperdirDiffSource never even reads binary bytes into old/new_content. */
  protected isBinary = computed(() =>
    isBinaryEntry(
      (this.selectedEntry() ?? {}) as { binary?: boolean },
      this.selectedFile() as { old_binary?: boolean; new_binary?: boolean } | null,
    ),
  );

  // Monaco diff editor instance; lazily attached when a file is selected.
  // We type Monaco as `any` here because we load it via the AMD-based
  // ``@monaco-editor/loader`` (rather than the ESM module) — that path
  // avoids esbuild trying to bundle Monaco's .ttf-importing CSS.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private diffEditor: any = null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private monacoModule: any = null;

  constructor() {
    // Host wiring guard: exactly one of jobId/threadId must be bound. Runs
    // on every input change so a host that flips between modes re-asserts
    // too, not just at first render.
    effect(() => {
      diffApiFor(this.jobId(), this.threadId());
    });

    // Reload whenever the parent points us at a different job or thread.
    effect(() => {
      const jobId = this.jobId();
      const threadId = this.threadId();
      if (jobId || threadId) this.loadDiff(jobId, threadId);
    });

    // Mount Monaco when a file becomes selected and the container exists —
    // skipped for binary entries (no text to diff; the template renders the
    // placeholder branch instead of `#diffContainer` for those).
    effect(() => {
      const file = this.selectedFile();
      const container = this.diffContainer()?.nativeElement;
      if (file && container && !this.isBinary()) {
        this.renderDiff(file, container);
      }
    });

    this.destroy.onDestroy(() => this.disposeEditor());
  }

  private loadDiff(jobId: string | null, threadId: string | null): void {
    this.loadingDiff.set(true);
    this.summary.set(null);
    this.selectedPath.set(null);
    this.selectedEntry.set(null);
    this.selectedFile.set(null);
    this.epoch.set(null);
    this.conflict.set(null);
    this.partial.set(null);
    const onSummary = (summary: DiffSummary | null) => {
      this.summary.set(summary);
      this.loadingDiff.set(false);
      // Auto-select first file so the user sees something immediately.
      const first = summary?.files[0];
      if (first) this.selectFile(first);
    };
    if (threadId) {
      this.api.getThreadCloudDiff(threadId).subscribe((summary) => {
        this.epoch.set(summary?.epoch ?? null);
        onSummary(summary);
      });
    } else if (jobId) {
      this.api.getJobDiff(jobId).subscribe(onSummary);
    }
  }

  protected selectFile(entry: DiffFileEntry): void {
    if (this.selectedPath() === entry.path) return;
    this.selectedPath.set(entry.path);
    this.selectedEntry.set(entry);
    this.selectedFile.set(null);
    this.fileLoadFailed.set(false);
    this.loadingFile.set(true);
    const threadId = this.threadId();
    // Two branches call two different API methods returning two different
    // Observable<T> element types — kept as separate .subscribe() calls
    // (rather than a ternary-picked `obs` variable) because RxJS's
    // overloaded `subscribe` doesn't type-check against a union of
    // Observables (TS2349); a shared handler avoids duplicating the body.
    const onFile = (file: DiffFile | null) => {
      this.loadingFile.set(false);
      if (!file) {
        this.fileLoadFailed.set(true);
        return;
      }
      this.selectedFile.set(file);
    };
    if (threadId) {
      this.api.getThreadCloudDiffFile(threadId, entry.path).subscribe(onFile);
    } else {
      this.api.getJobDiffFile(this.jobId()!, entry.path).subscribe(onFile);
    }
  }

  /**
   * Lazy-load Monaco on first use, then render the diff. Monaco is a
   * heavy module so we keep it out of the initial cockpit bundle; the
   * dynamic import becomes its own chunk.
   */
  private async renderDiff(file: DiffFile, container: HTMLDivElement): Promise<void> {
    try {
      const monaco = await this.loadMonaco();
      this.disposeEditor();
      container.innerHTML = '';
      const original = file.old_content ?? '';
      const modified = file.new_content ?? '';
      // Best-effort language inference from extension; Monaco falls
      // back to plain text when it doesn't recognize one.
      const language = languageFromPath(file.path);
      const originalModel = monaco.editor.createModel(original, language);
      const modifiedModel = monaco.editor.createModel(modified, language);
      const editor = monaco.editor.createDiffEditor(container, {
        readOnly: true,
        renderSideBySide: true,
        automaticLayout: true,
        ignoreTrimWhitespace: false,
        scrollBeyondLastLine: false,
        minimap: { enabled: false },
        theme: prefersDarkTheme() ? 'vs-dark' : 'vs',
      });
      editor.setModel({ original: originalModel, modified: modifiedModel });
      this.diffEditor = editor;
    } catch (err) {
      console.error('Monaco diff editor failed to render:', err);
      this.monacoFailed.set(true);
    }
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private async loadMonaco(): Promise<any> {
    if (this.monacoModule) return this.monacoModule;
    // ``@monaco-editor/loader`` injects Monaco's AMD loader, then asks
    // it to require the editor module from our static asset path.
    // angular.json copies node_modules/monaco-editor/min/vs into
    // assets/monaco/vs, so the path below is served by the same host
    // as the cockpit (no CDN dependency).
    const loaderModule = await import('@monaco-editor/loader');
    const loader = loaderModule.default;
    // ``monaco/vs`` is copied verbatim into ``public/`` by
    // scripts/copy-monaco.mjs at prebuild time; Angular's static asset
    // pipeline leaves the AMD bundle untouched (no output hashing
    // rewriting Monaco's ``define()`` IDs).
    loader.config({ paths: { vs: 'monaco/vs' } });
    this.monacoModule = await loader.init();
    return this.monacoModule;
  }

  private disposeEditor(): void {
    if (this.diffEditor && typeof (this.diffEditor as { dispose?: () => void }).dispose === 'function') {
      (this.diffEditor as { dispose: () => void }).dispose();
    }
    this.diffEditor = null;
  }

  // ---------- actions ----------

  protected onAcceptClick(): void {
    if (this.conflict()) return;
    this.showAcceptConfirm.set(true);
  }

  protected onRejectClick(): void {
    this.showRejectConfirm.set(true);
  }

  protected confirmAccept(): void {
    this.showAcceptConfirm.set(false);
    this.accepting.set(true);
    this.partial.set(null);
    const threadId = this.threadId();
    const obs = threadId
      ? this.api.applyThreadCloudDiff(threadId, this.epoch() ?? -1)
      : this.api.acceptJobDiff(this.jobId()!);
    obs.subscribe((outcome) => {
      this.accepting.set(false);
      switch (outcome.kind) {
        case 'ok': {
          const t = this.translocoService.translate('toasts.jobs.diffAccepted', {
            applied: outcome.data.applied,
            deleted: outcome.data.deleted,
          });
          this.toast.success(t);
          this.resolved.emit('accepted');
          break;
        }
        case 'conflict':
          this.conflict.set(outcome.data);
          break;
        case 'partial':
          this.partial.set(outcome.data);
          break;
        case 'stale':
          // Someone else applied/rejected/restaged since we read the
          // summary — reload against the fresh epoch and let the user
          // re-decide rather than silently applying stale content.
          this.toast.info(this.translocoService.translate('jobDiffReview.staleNotice'));
          this.loadDiff(this.jobId(), this.threadId());
          break;
        case 'error':
          this.toast.danger(outcome.detail);
          break;
      }
    });
  }

  protected confirmReject(): void {
    this.showRejectConfirm.set(false);
    this.rejecting.set(true);
    const threadId = this.threadId();
    // Same union-of-Observables issue as selectFile — separate .subscribe()
    // calls per branch, shared handler.
    const onResult = (result: unknown) => {
      this.rejecting.set(false);
      if (result) {
        this.toast.success(this.translocoService.translate('toasts.jobs.diffRejected'));
        this.resolved.emit('rejected');
      }
    };
    if (threadId) {
      this.api.rejectThreadCloudDiff(threadId, this.epoch() ?? -1).subscribe(onResult);
    } else {
      this.api.rejectJobDiff(this.jobId()!).subscribe(onResult);
    }
  }

  protected dismissConflict(): void {
    this.conflict.set(null);
  }

  // ---------- helpers used in template ----------

  /** Idle-state accept-button label; switches copy in thread mode ("Apply
   *  to cloud" — there's no Gitea commit to fall back on, so "accept" reads
   *  wrong once the action writes straight to the user's cloud folder). */
  protected acceptLabel(): string {
    if (this.accepting()) return 'jobDiffReview.actions.accepting';
    return this.mode() === 'thread'
      ? 'jobDiffReview.actions.applyToCloud'
      : 'jobDiffReview.actions.accept';
  }

  protected statusTone(status: DiffFileEntry['status']): BadgeTone {
    switch (status) {
      case 'added':
        return 'success';
      case 'modified':
        return 'info';
      case 'deleted':
        return 'danger';
    }
  }

  protected statusGlyph(status: DiffFileEntry['status']): string {
    return status === 'added' ? '+' : status === 'deleted' ? '−' : 'M';
  }

  protected conflictLabel(kind: JobAcceptConflict['diverged'][number]['kind']): string {
    switch (kind) {
      case 'etag_mismatch':
        return this.translocoService.translate('jobDiffReview.conflict.etagMismatch');
      case 'missing_at_cloud':
        return this.translocoService.translate('jobDiffReview.conflict.missingAtCloud');
      case 'unexpected_at_cloud':
        return this.translocoService.translate('jobDiffReview.conflict.unexpectedAtCloud');
    }
  }
}

function languageFromPath(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() ?? '';
  const map: Record<string, string> = {
    md: 'markdown',
    markdown: 'markdown',
    json: 'json',
    yaml: 'yaml',
    yml: 'yaml',
    ts: 'typescript',
    tsx: 'typescript',
    js: 'javascript',
    jsx: 'javascript',
    py: 'python',
    sh: 'shell',
    bash: 'shell',
    sql: 'sql',
    html: 'html',
    css: 'css',
    scss: 'scss',
    xml: 'xml',
    txt: 'plaintext',
  };
  return map[ext] ?? 'plaintext';
}

function prefersDarkTheme(): boolean {
  if (typeof document === 'undefined') return false;
  // Cockpit applies `theme-senate` (dark) or `theme-travertine` (light)
  // to <body>; see src/index.html pre-paint script.
  return document.body.classList.contains('theme-senate');
}
