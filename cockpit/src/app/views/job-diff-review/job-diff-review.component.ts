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
} from '../../core/models/api.model';
import { AppButtonComponent } from '../../ui/button';
import { AppBadgeComponent, type BadgeTone } from '../../ui/badge';
import { AppSpinnerComponent } from '../../ui/spinner';
import { AppDialogComponent } from '../../ui/dialog';
import { AppToastService } from '../../ui/toast';

/**
 * Mode A diff review for project-attached jobs in `pending_review`.
 * Shows a file tree (left) + Monaco diff editor (right) + accept/reject
 * actions (footer). Handles the external-mod 409 by surfacing an inline
 * banner with the diverged paths.
 *
 * See docs/features/job_cloud_export.md §3.4–§3.6.
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

  jobId = input.required<string>();
  resolved = output<'accepted' | 'rejected'>();

  protected summary = signal<JobDiffSummary | null>(null);
  protected loadingDiff = signal<boolean>(true);
  protected selectedPath = signal<string | null>(null);
  protected selectedFile = signal<JobDiffFile | null>(null);
  protected loadingFile = signal<boolean>(false);
  protected fileLoadFailed = signal<boolean>(false);
  protected conflict = signal<JobAcceptConflict | null>(null);
  protected partial = signal<JobAcceptPartialFailure | null>(null);
  protected accepting = signal<boolean>(false);
  protected rejecting = signal<boolean>(false);
  protected showAcceptConfirm = signal<boolean>(false);
  protected showRejectConfirm = signal<boolean>(false);
  protected monacoFailed = signal<boolean>(false);

  protected diffContainer = viewChild<ElementRef<HTMLDivElement>>('diffContainer');

  protected fileCount = computed(() => this.summary()?.files.length ?? 0);
  protected hasFiles = computed(() => this.fileCount() > 0);

  // Monaco diff editor instance; lazily attached when a file is selected.
  // We type Monaco as `any` here because we load it via the AMD-based
  // ``@monaco-editor/loader`` (rather than the ESM module) — that path
  // avoids esbuild trying to bundle Monaco's .ttf-importing CSS.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private diffEditor: any = null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private monacoModule: any = null;

  constructor() {
    // Reload whenever the parent points us at a different job.
    effect(() => {
      const id = this.jobId();
      if (id) this.loadDiff(id);
    });

    // Mount Monaco when a file becomes selected and the container exists.
    effect(() => {
      const file = this.selectedFile();
      const container = this.diffContainer()?.nativeElement;
      if (file && container) {
        this.renderDiff(file, container);
      }
    });

    this.destroy.onDestroy(() => this.disposeEditor());
  }

  private loadDiff(jobId: string): void {
    this.loadingDiff.set(true);
    this.summary.set(null);
    this.selectedPath.set(null);
    this.selectedFile.set(null);
    this.conflict.set(null);
    this.partial.set(null);
    this.api.getJobDiff(jobId).subscribe((summary) => {
      this.summary.set(summary);
      this.loadingDiff.set(false);
      // Auto-select first file so the user sees something immediately.
      const first = summary?.files[0];
      if (first) this.selectFile(first);
    });
  }

  protected selectFile(entry: JobDiffFileEntry): void {
    if (this.selectedPath() === entry.path) return;
    this.selectedPath.set(entry.path);
    this.selectedFile.set(null);
    this.fileLoadFailed.set(false);
    this.loadingFile.set(true);
    this.api.getJobDiffFile(this.jobId(), entry.path).subscribe((file) => {
      this.loadingFile.set(false);
      if (!file) {
        this.fileLoadFailed.set(true);
        return;
      }
      this.selectedFile.set(file);
    });
  }

  /**
   * Lazy-load Monaco on first use, then render the diff. Monaco is a
   * heavy module so we keep it out of the initial cockpit bundle; the
   * dynamic import becomes its own chunk.
   */
  private async renderDiff(file: JobDiffFile, container: HTMLDivElement): Promise<void> {
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
    this.api.acceptJobDiff(this.jobId()).subscribe((outcome) => {
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
        case 'error':
          this.toast.danger(outcome.detail);
          break;
      }
    });
  }

  protected confirmReject(): void {
    this.showRejectConfirm.set(false);
    this.rejecting.set(true);
    this.api.rejectJobDiff(this.jobId()).subscribe((result) => {
      this.rejecting.set(false);
      if (result) {
        this.toast.success(this.translocoService.translate('toasts.jobs.diffRejected'));
        this.resolved.emit('rejected');
      }
    });
  }

  protected dismissConflict(): void {
    this.conflict.set(null);
  }

  // ---------- helpers used in template ----------

  protected statusTone(status: JobDiffFileEntry['status']): BadgeTone {
    switch (status) {
      case 'added':
        return 'success';
      case 'modified':
        return 'info';
      case 'deleted':
        return 'danger';
    }
  }

  protected statusGlyph(status: JobDiffFileEntry['status']): string {
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
