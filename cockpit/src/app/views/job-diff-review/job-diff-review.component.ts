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
  untracked,
  viewChild,
  viewChildren,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { Subscription } from 'rxjs';
import { TranslocoPipe, TranslocoService } from '@jsverse/transloco';
import { ApiService } from '../../core/services/api.service';
import {
  DiffLoadOutcome,
  DiffRejectOutcome,
  JobAcceptConflict,
  JobAcceptPartialFailure,
  JobDiffFile,
  JobDiffFileEntry,
  JobDiffSummary,
  ThreadCloudApplyResult,
  ThreadCloudDiffFile,
  ThreadCloudDiffSummary,
} from '../../core/models/api.model';
import { AppButtonComponent } from '../../ui/button';
import { AppSpinnerComponent } from '../../ui/spinner';
import { AppIconComponent } from '../../ui/icon';
import { AppToastService } from '../../ui/toast';
import { binaryKindFromPath, isBinaryEntry } from './binary-sniff';
import { folderLinkMatches, ProtectedFolderLink } from './protected-folder-link';
import {
  CloudReviewReceipt,
  readReceipt,
  receiptAppliesTo,
  writeReceipt,
} from './cloud-review-receipt';
import {
  disposeMonacoEditor,
  MonacoDiffEditor,
  MonacoEditorLoaderService,
  MonacoTextModel,
  monacoLanguageFromPath,
  preferredMonacoTheme,
} from '../../core/services/monaco-editor-loader.service';

/** A file-tree entry from either diff summary shape (job or session mode). */
type DiffFileEntry = JobDiffFileEntry | ThreadCloudDiffSummary['files'][number];
/** The loaded summary, from either mode. */
type DiffSummary = JobDiffSummary | ThreadCloudDiffSummary;
/** The loaded per-file content, from either mode. */
type DiffFile = JobDiffFile | ThreadCloudDiffFile;

/**
 * Which product surface this review is presenting.
 *
 * This used to be an implicit `mode()` consulted at two call sites, which is
 * how "changed in this job" and "on accept" ended up in front of someone
 * reviewing a persistent session (protected_cloud_review.md PC-23). It is now
 * the explicit axis every piece of context-varying copy resolves through —
 * see `ck()`.
 */
export type ReviewContext = 'session' | 'job';

/** Header/summary fetch state. `unavailable` and `forbidden` are separated
 *  from `failed` because they are not errors the user can retry away. */
export type LoadState = 'loading' | 'ready' | 'forbidden' | 'unavailable' | 'failed';

/** Per-file fetch state. `missing` = the summary listed it, the backend 404s
 *  it: either the staged set moved under us, or its staged copy is
 *  unreadable. `missingReason` says which, when the backend says. */
export type FileState = 'idle' | 'loading' | 'ready' | 'missing' | 'failed';

/** Which decision is armed for confirmation, or in flight. */
export type Decision = 'apply' | 'reject';

/**
 * The exact review a loaded summary describes — and therefore the only thing
 * Apply and Reject are ever allowed to act on.
 *
 * This is a safety invariant, not bookkeeping. The surface used to display
 * whatever response arrived last and act on whatever `jobId()`/`threadId()`
 * currently held, which are two different things the moment a host re-points
 * the component: a late summary for thread A could paint the view while Apply
 * read thread B's id, and an epoch-5 file body could replace epoch-6 bytes at
 * the same path. Every displayed byte now carries the identity it came from,
 * every action captures that identity up front, and every response is
 * discarded unless the identity is still the one on screen.
 *
 * `generation` is what makes A → B → A safe: the ids match again, but the
 * counter does not, so the stale A response is still rejected.
 */
export interface ReviewIdentity {
  context: ReviewContext;
  jobId: string | null;
  threadId: string | null;
  /** Session context only — the optimistic-concurrency pin. Null in job
   *  context, where the backend has no epoch. */
  epoch: number | null;
  generation: number;
}

/**
 * Exactly one of `jobId`/`threadId` must be bound — this is the host's
 * wiring contract, asserted (thrown) rather than silently defaulted so a
 * mistake fails loudly in dev rather than rendering an empty panel.
 */
export function diffApiFor(jobId: string | null, threadId: string | null): ReviewContext {
  if (jobId && threadId) {
    throw new Error('app-job-diff-review: both jobId and threadId are set — bind exactly one.');
  }
  if (jobId) return 'job';
  if (threadId) return 'session';
  throw new Error('app-job-diff-review: neither jobId nor threadId is set — bind exactly one.');
}

/** Whether two identities describe the same loaded review. Reference equality
 *  would do for the in-component checks, but this is what the tests assert
 *  against and what documents the rule. */
export function sameReview(a: ReviewIdentity | null, b: ReviewIdentity | null): boolean {
  if (!a || !b) return false;
  return (
    a.generation === b.generation &&
    a.context === b.context &&
    a.jobId === b.jobId &&
    a.threadId === b.threadId &&
    a.epoch === b.epoch
  );
}

export { isBinaryEntry } from './binary-sniff';
export { folderLinkMatches, type ProtectedFolderLink } from './protected-folder-link';

let surfaceInstanceId = 0;

/**
 * Every mounted review surface, in mount order.
 *
 * Escape is handled on `document` in the capture phase (see
 * `onDocumentKeydown` for why it cannot be a host listener), which means
 * without a registry *every* mounted review would act on *every* Escape —
 * including one meant for a dialog stacked above, or for an unrelated
 * control while an inline job review sits quietly on the same page. Only the
 * topmost connected surface acts.
 */
const mountedReviews: JobDiffReviewComponent[] = [];

/** Below this width the surface switches to its single-column composition. */
const COMPACT_QUERY = '(max-width: 767.98px)';

/**
 * Redesigned protected-cloud / Mode-A diff review surface.
 *
 * Session context (`threadId` bound): a persistent session's staged
 * protected-cloud overlay diff. The agent worked against a read-only mount of
 * the real folder and its writes were captured; nothing has reached the cloud
 * yet. Apply writes the whole reviewed set; Reject discards the whole staged
 * set and touches nothing.
 *
 * Job context (`jobId` bound): a project-attached job in `pending_review`,
 * diffed against its Gitea baseline commit.
 *
 * The component is a *surface*: it fills whatever height its host gives it and
 * has no chrome of its own, so the job-review page can embed it inline while
 * the session hosts it in a full-height dialog. It owns the whole-diff
 * decision — there are no per-file checkboxes, because neither backend
 * supports partial apply.
 */
@Component({
  selector: 'app-job-diff-review',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    CommonModule,
    TranslocoPipe,
    AppButtonComponent,
    AppSpinnerComponent,
    AppIconComponent,
  ],
  templateUrl: './job-diff-review.component.html',
  styleUrl: './job-diff-review.component.scss',
})
export class JobDiffReviewComponent {
  private api = inject(ApiService);
  private destroy = inject(DestroyRef);
  private toast = inject(AppToastService);
  private translocoService = inject(TranslocoService);
  private monaco = inject(MonacoEditorLoaderService);
  private host = inject(ElementRef<HTMLElement>);

  /** Bind exactly one of jobId/threadId — see `diffApiFor`. */
  jobId = input<string | null>(null);
  threadId = input<string | null>(null);
  /** Session context only: a verified link to the protected project folder.
   *  Null (the default) simply omits the action. */
  projectFolder = input<ProtectedFolderLink | null>(null);

  /** Fired once the staged set has been applied or rejected. The host should
   *  refresh its pending count — but NOT unmount the surface, which now shows
   *  the outcome receipt until the user dismisses it. */
  resolved = output<'accepted' | 'rejected'>();
  /** True while an apply/reject is in flight. A hosting dialog must refuse to
   *  close while this is true — losing the surface mid-apply is exactly how
   *  PC-20's owner ended up unable to tell whether the write had landed. */
  busyChange = output<boolean>();
  /** The user is done with the surface (terminal-state "Done"). */
  closeRequested = output<void>();

  /**
   * The context of the review on screen.
   *
   * `loaded` governs whenever there is one, and the inputs only fill in
   * before the first summary lands. That matters while a write is in flight:
   * the host can re-point the inputs, the busy latch keeps target A on
   * screen, and reading the inputs here would flip every piece of
   * context-varying copy to B's wording over A's bytes mid-write.
   */
  protected context = computed<ReviewContext>(
    () => this.loaded()?.context ?? diffApiFor(this.jobId(), this.threadId()),
  );

  /** Per-instance id prefix for the aria wiring. Two review surfaces can be
   *  mounted at once (a session's own diff and a job card's diff), and
   *  duplicate ids would silently cross-wire their `aria-labelledby` /
   *  `aria-controls` references. */
  protected uid = `cloud-review-${++surfaceInstanceId}`;

  protected summary = signal<DiffSummary | null>(null);
  protected loadState = signal<LoadState>('loading');
  protected loadError = signal<string>('');
  protected selectedPath = signal<string | null>(null);
  protected selectedEntry = signal<DiffFileEntry | null>(null);
  protected selectedFile = signal<DiffFile | null>(null);
  protected fileState = signal<FileState>('idle');
  protected fileError = signal<string>('');
  /** Backend `code` behind a per-file 404, when it sent one. Drives which of
   *  the three truthful explanations the viewer shows — see `missingBodyKey`. */
  protected missingReason = signal<string | null>(null);
  protected conflict = signal<JobAcceptConflict | null>(null);
  protected partial = signal<JobAcceptPartialFailure | null>(null);
  protected pendingDecision = signal<Decision | null>(null);
  protected submitting = signal<Decision | null>(null);
  /** A decision that was refused (stale epoch, invalid pin, backend error).
   *  Rendered in the decision bar so the controls are never left looking
   *  live over a staged set the backend just declined to touch. */
  protected decisionError = signal<string>('');
  protected monacoFailed = signal<boolean>(false);
  protected receipt = signal<CloudReviewReceipt | null>(null);
  /** Whether the displayed receipt actually reached localStorage. The
   *  "recorded in this browser only" line is a claim about storage, and it is
   *  false in job context (no thread key) and in a private window. */
  protected receiptStored = signal<boolean>(false);
  /** True once THIS surface completed a decision, as opposed to having read a
   *  receipt back from a previous page view. The distinction matters: a
   *  just-made decision always shows its outcome, whereas a stored one only
   *  shows while it is still the current state of the thread. */
  protected resolvedNow = signal<boolean>(false);
  /** Screen-reader announcement for async transitions, held as key+params
   *  and translated in the template. Storing the translated *string* looked
   *  simpler but resolved through `TranslocoService.translate()` at the
   *  moment of the transition — which during the first change detection can
   *  precede the language load and bakes the raw key into the live region. */
  protected liveMessage = signal<{ key: string; params?: Record<string, unknown> } | null>(null);

  /**
   * The identity of what is currently on screen, or null while loading /
   * after a failed load. Set once, when a summary lands; never mutated.
   */
  protected loaded = signal<ReviewIdentity | null>(null);
  /** Session context only: the epoch the loaded summary is pinned to. */
  protected epoch = computed<number | null>(() => this.loaded()?.epoch ?? null);

  /** Monotonic load counter. Every in-flight request carries the value it was
   *  issued under, and any response whose value is not current is dropped. */
  private generation = 0;
  private summarySub: Subscription | null = null;
  private fileSub: Subscription | null = null;
  /**
   * A target the host asked for while a decision was in flight, applied once
   * the write settles. See `loadDiff`'s latch.
   */
  private deferredTarget: { jobId: string | null; threadId: string | null } | null = null;

  /** True below the `md` breakpoint, where the surface switches to a single
   *  column with a collapsible file chooser. Driven by `matchMedia` rather
   *  than CSS alone because the composition differs structurally, not just
   *  visually — a `<details>` is open or closed, which no media query can
   *  decide. */
  protected compact = signal(false);
  /** Mobile only: whether the file chooser is expanded. Collapsing it after a
   *  pick is what leaves the viewer a usable height at 375×667. */
  protected filesExpanded = signal(false);
  protected filesVisible = computed(() => !this.compact() || this.filesExpanded());

  protected diffContainer = viewChild<ElementRef<HTMLDivElement>>('diffContainer');
  protected fileOptions = viewChildren<ElementRef<HTMLElement>>('fileOption');

  protected files = computed<DiffFileEntry[]>(() => this.summary()?.files ?? []);
  protected fileCount = computed(() => this.files().length);
  protected hasFiles = computed(() => this.fileCount() > 0);
  protected busy = computed(() => this.submitting() !== null);

  /** 1-based position of the selected file, for the mobile chooser's label. */
  protected selectedIndex = computed(() => {
    const path = this.selectedPath();
    if (!path) return 0;
    return this.files().findIndex((f) => f.path === path) + 1;
  });

  /**
   * Per-status totals for the header. Session summaries carry `counts`
   * straight from the manifest; job summaries have no counts field, so they
   * are derived from the file list. Both were previously collapsed into one
   * number and the breakdown thrown away.
   */
  protected tallies = computed<{ added: number; modified: number; deleted: number }>(() => {
    const sum = this.summary();
    if (sum && 'counts' in sum && sum.counts) return sum.counts;
    const t = { added: 0, modified: 0, deleted: 0 };
    for (const f of this.files()) t[f.status]++;
    return t;
  });

  /**
   * The folder this review acts on, for the header and the confirmation copy.
   *
   * Prefers the project's own name once the resolved link has been verified
   * against the summary — PC-01 asks the UI to name the protected mount the
   * way a person thinks of it ("Project X / Main folder"), and the raw value
   * is a workspace target path that is usually the literal string "cloud".
   * The path itself stays visible under the technical details disclosure.
   */
  protected subject = computed<string | null>(() => {
    const verified = this.folderLink();
    if (verified) return verified.name;
    const sum = this.summary();
    if (sum && 'protected_mount' in sum && sum.protected_mount) return sum.protected_mount;
    return null;
  });

  /** The workspace mount path, shown alongside the friendly name. */
  protected mountPath = computed<string | null>(() => {
    const sum = this.summary();
    return sum && 'protected_mount' in sum ? sum.protected_mount : null;
  });

  /** Only shown when it provably names the folder this diff applies to. */
  protected folderLink = computed<ProtectedFolderLink | null>(() => {
    const link = this.projectFolder();
    const sum = this.summary();
    const mount = sum && 'protected_mount' in sum ? sum.protected_mount : null;
    return folderLinkMatches(link, mount) ? link : null;
  });

  /**
   * Binary entries render a placeholder instead of Monaco. Three signals are
   * OR'd — including a content sniff — because the summary's `binary` flag is
   * a NUL-byte heuristic that a UTF-8-decodable PDF slips past (PC-17), and
   * job mode has no binary flags at all.
   */
  protected isBinary = computed(() =>
    isBinaryEntry(
      (this.selectedEntry() ?? {}) as { binary?: boolean },
      this.selectedFile() as DiffFile | null,
    ),
  );

  protected binaryKind = computed(() => binaryKindFromPath(this.selectedPath() ?? ''));

  /** The receipt is shown unconditionally right after a decision; a receipt
   *  restored from storage is shown only while it is still the thread's
   *  current state (nothing newer staged since). */
  protected showReceipt = computed<CloudReviewReceipt | null>(() => {
    const r = this.receipt();
    if (this.resolvedNow()) return r;
    return receiptAppliesTo(r, this.epoch(), this.fileCount()) ? r : null;
  });

  /** Roving tabindex anchor: exactly one option is in the tab order. */
  protected rovingPath = computed(() => this.selectedPath() ?? this.files()[0]?.path ?? null);

  private diffEditor: MonacoDiffEditor | null = null;
  private diffModels: MonacoTextModel[] = [];

  constructor() {
    // Host wiring guard: exactly one of jobId/threadId must be bound. Runs
    // on every input change so a host that flips between modes re-asserts
    // too, not just at first render.
    effect(() => {
      diffApiFor(this.jobId(), this.threadId());
    });

    // Reload whenever the parent points us at a different job or thread.
    //
    // `untracked` is load-bearing, not defensive. `loadDiff` synchronously
    // calls `selectFile`, which both READS `selectedPath`/`fileState` (its
    // no-op guard) and WRITES them. Without this, those reads register as
    // dependencies of this effect, the writes invalidate it, and it re-runs
    // forever. It only escapes notice in production because the HTTP response
    // arrives in a later task, outside the tracking context — a cached or
    // otherwise synchronous emission loops the browser. The effect's real
    // dependencies are the two ids read above, and nothing else.
    effect(() => {
      const jobId = this.jobId();
      const threadId = this.threadId();
      if (jobId || threadId) untracked(() => this.loadDiff(jobId, threadId));
    });

    // Mount Monaco when a text file becomes selected and the container
    // exists. Binary entries render the placeholder branch instead, so
    // `#diffContainer` does not exist for them and no bytes can reach the
    // editor even if this effect were to run.
    effect(() => {
      const file = this.selectedFile();
      const container = this.diffContainer()?.nativeElement;
      if (file && container && !this.isBinary()) {
        untracked(() => this.renderDiff(file, container));
      }
    });

    effect(() => {
      const busy = this.busy();
      untracked(() => this.busyChange.emit(busy));
    });

    this.watchViewport();

    mountedReviews.push(this);
    document.addEventListener('keydown', this.onDocumentKeydown, true);
    this.destroy.onDestroy(() => {
      document.removeEventListener('keydown', this.onDocumentKeydown, true);
      const at = mountedReviews.indexOf(this);
      if (at >= 0) mountedReviews.splice(at, 1);
      // Cancel every read in flight. An action in flight is deliberately NOT
      // cancelled: the cloud write is already happening server-side and
      // aborting the XHR would only throw away the one record of its result.
      this.summarySub?.unsubscribe();
      this.fileSub?.unsubscribe();
      this.disposeEditor();
    });
  }

  private watchViewport(): void {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
    const mql = window.matchMedia(COMPACT_QUERY);
    this.compact.set(!!mql.matches);
    if (typeof mql.addEventListener !== 'function') return;
    const onChange = (event: MediaQueryListEvent) => this.compact.set(event.matches);
    mql.addEventListener('change', onChange);
    this.destroy.onDestroy(() => mql.removeEventListener('change', onChange));
  }

  // ---------- loading ----------

  private loadDiff(jobId: string | null, threadId: string | null): void {
    if (this.submitting()) {
      // FAIL CLOSED. An irreversible write is in flight against the review on
      // screen. Dropping the busy latch here — which is what this used to do —
      // made the surface actionable again while that write continued: a second
      // Apply could be issued, and in job context the original result had
      // nowhere to land (the receipt store is keyed by thread id, so a job has
      // none) and was simply lost.
      //
      // So the load is deferred rather than performed. Target A stays on
      // screen with its progress state, `busy` stays true, no decision control
      // is mounted, and the latest requested target is loaded once the write
      // settles — see `settleAction`.
      this.deferredTarget = { jobId, threadId };
      return;
    }
    this.deferredTarget = null;

    // Anything still in flight is for a review that is no longer on screen.
    this.summarySub?.unsubscribe();
    this.fileSub?.unsubscribe();
    const generation = ++this.generation;

    this.loadState.set('loading');
    this.loadError.set('');
    this.summary.set(null);
    this.loaded.set(null);
    this.selectedPath.set(null);
    this.selectedEntry.set(null);
    this.selectedFile.set(null);
    this.fileState.set('idle');
    this.missingReason.set(null);
    this.conflict.set(null);
    this.partial.set(null);
    this.pendingDecision.set(null);
    this.decisionError.set('');
    this.resolvedNow.set(false);
    this.filesExpanded.set(false);
    const stored = readReceipt(threadId);
    this.receipt.set(stored);
    this.receiptStored.set(!!stored);

    const onOutcome = (outcome: DiffLoadOutcome<DiffSummary>) => {
      if (generation !== this.generation) return; // superseded target
      switch (outcome.kind) {
        case 'ok': {
          const sum = outcome.data;
          this.summary.set(sum);
          this.loaded.set({
            context: threadId ? 'session' : 'job',
            jobId,
            threadId,
            epoch: 'epoch' in sum ? (sum.epoch ?? null) : null,
            generation,
          });
          this.loadState.set('ready');
          this.announce(
            sum.files.length === 0
              ? 'jobDiffReview.a11y.loadedEmpty'
              : sum.files.length === 1
                ? 'jobDiffReview.a11y.loadedSingular'
                : 'jobDiffReview.a11y.loaded',
            { count: sum.files.length },
          );
          // Auto-select the first file so the surface is never a blank pane.
          const first = sum.files[0];
          if (first) this.selectFile(first);
          break;
        }
        case 'forbidden':
          this.loadState.set('forbidden');
          break;
        case 'missing':
        case 'unavailable':
          this.loadState.set('unavailable');
          break;
        case 'error':
          this.loadError.set(outcome.detail);
          this.loadState.set('failed');
          break;
      }
    };

    if (threadId) {
      this.summarySub = this.api.getThreadCloudDiffOutcome(threadId).subscribe(onOutcome);
    } else if (jobId) {
      this.summarySub = this.api.getJobDiffOutcome(jobId).subscribe(onOutcome);
    }
  }

  /** Retry after a transient read failure. */
  protected reload(): void {
    this.loadDiff(this.jobId(), this.threadId());
  }

  /**
   * Load one file's content into the viewer.
   *
   * `force` bypasses the same-path no-op guard. Without it the Retry button in
   * the failure state was inert: it called back in with the same entry, the
   * guard saw a matching path and a non-idle state, and returned — so a
   * transient read failure was permanent until the whole summary was
   * reloaded.
   */
  protected selectFile(entry: DiffFileEntry, force = false): void {
    if (!force && this.selectedPath() === entry.path && this.fileState() !== 'idle') return;
    // The endpoint is chosen from the LOADED identity, never from the live
    // inputs. Those diverge for a window: the host can set a new id and the
    // reload effect has not run yet, and reading `threadId()` here would fetch
    // target B's bytes and paint them under target A's still-loaded summary.
    // No loaded review means no request at all, rather than a guess.
    const target = this.loaded();
    if (!target) return;
    this.fileSub?.unsubscribe();
    this.selectedPath.set(entry.path);
    this.selectedEntry.set(entry);
    this.selectedFile.set(null);
    // Reset per-selection, so one Monaco failure no longer poisons the panel
    // for the rest of its life.
    this.monacoFailed.set(false);
    this.fileError.set('');
    this.missingReason.set(null);
    this.fileState.set('loading');

    // Two branches call two different API methods returning two different
    // Observable<T> element types — kept as separate .subscribe() calls
    // (rather than a ternary-picked `obs` variable) because RxJS's
    // overloaded `subscribe` doesn't type-check against a union of
    // Observables (TS2349); a shared handler avoids duplicating the body.
    const onFile = (outcome: DiffLoadOutcome<DiffFile>) => {
      // Both guards are needed, and the first is against the identity the
      // request was ISSUED under. It rejects a body from a review that is no
      // longer loaded — including the same path at a different epoch, which is
      // why a path check alone is not enough. The path rejects a body
      // superseded by a newer selection within this review.
      if (!sameReview(this.loaded(), target)) return;
      if (this.selectedPath() !== entry.path) return;
      switch (outcome.kind) {
        case 'ok':
          this.selectedFile.set(outcome.data);
          this.fileState.set('ready');
          this.announce('jobDiffReview.a11y.fileLoaded', { path: entry.path });
          break;
        case 'missing':
          this.missingReason.set(outcome.code ?? null);
          this.fileState.set('missing');
          break;
        case 'forbidden':
        case 'unavailable':
          this.fileState.set('missing');
          break;
        case 'error':
          this.fileError.set(outcome.detail);
          this.fileState.set('failed');
          break;
      }
    };
    if (target.threadId) {
      this.fileSub = this.api
        .getThreadCloudDiffFileOutcome(target.threadId, entry.path)
        .subscribe(onFile);
    } else {
      this.fileSub = this.api
        .getJobDiffFileOutcome(target.jobId!, entry.path)
        .subscribe(onFile);
    }
  }

  /** Re-request the currently selected file after a read failure. */
  protected retrySelectedFile(): void {
    const entry = this.selectedEntry();
    if (entry) this.selectFile(entry, true);
  }

  /**
   * Pick a file by pointer or Enter/Space. On a phone this also collapses the
   * chooser, which is what gives the viewer a usable height — the arrow-key
   * path deliberately does NOT collapse, because the list has to stay open to
   * keep arrowing through it.
   */
  protected chooseFile(entry: DiffFileEntry): void {
    this.selectFile(entry);
    if (this.compact()) this.filesExpanded.set(false);
  }

  protected toggleFiles(): void {
    this.filesExpanded.update((open) => !open);
  }

  // ---------- file list keyboard model (listbox) ----------

  /**
   * ArrowUp/Down/Home/End move both selection and focus. Combined with the
   * roving tabindex this makes an N-file diff one tab stop instead of N —
   * the old tree put every file in the tab order, so a 40-file review meant
   * 40 stops before reaching the decision controls.
   */
  protected onListKeydown(event: KeyboardEvent): void {
    const files = this.files();
    if (!files.length) return;
    const currentPath = this.rovingPath();
    const current = Math.max(
      0,
      files.findIndex((f) => f.path === currentPath),
    );
    let next = current;
    switch (event.key) {
      case 'ArrowDown':
        next = Math.min(files.length - 1, current + 1);
        break;
      case 'ArrowUp':
        next = Math.max(0, current - 1);
        break;
      case 'Home':
        next = 0;
        break;
      case 'End':
        next = files.length - 1;
        break;
      default:
        return;
    }
    event.preventDefault();
    this.selectFile(files[next]);
    this.fileOptions()[next]?.nativeElement.focus();
  }

  // ---------- Monaco ----------

  /**
   * Lazy-load Monaco on first use, then render the diff. Monaco is a
   * heavy module so we keep it out of the initial cockpit bundle; the
   * dynamic import becomes its own chunk.
   */
  private async renderDiff(file: DiffFile, container: HTMLDivElement): Promise<void> {
    try {
      const monaco = await this.monaco.load();
      this.disposeEditor();
      container.innerHTML = '';
      const original = file.old_content ?? '';
      const modified = file.new_content ?? '';
      // Best-effort language inference from extension; Monaco falls
      // back to plain text when it doesn't recognize one.
      const language = monacoLanguageFromPath(file.path);
      const originalModel = monaco.editor.createModel(original, language);
      const modifiedModel = monaco.editor.createModel(modified, language);
      const editor = monaco.editor.createDiffEditor(container, {
        readOnly: true,
        renderSideBySide: true,
        automaticLayout: true,
        ignoreTrimWhitespace: false,
        scrollBeyondLastLine: false,
        minimap: { enabled: false },
        theme: preferredMonacoTheme(),
        // Tab moves focus out instead of indenting: the editor is read-only,
        // so trapping Tab inside it buys nothing and strands keyboard users
        // short of the decision controls.
        tabFocusMode: true,
      });
      editor.setModel({ original: originalModel, modified: modifiedModel });
      this.diffEditor = editor;
      this.diffModels = [originalModel, modifiedModel];
    } catch (err) {
      console.error('Monaco diff editor failed to render:', err);
      this.monacoFailed.set(true);
    }
  }

  private disposeEditor(): void {
    disposeMonacoEditor(this.diffEditor, this.diffModels);
    this.diffEditor = null;
    this.diffModels = [];
  }

  // ---------- decisions ----------

  /**
   * Arm a confirmation. Deliberately inline in the decision bar rather than a
   * nested dialog: the session hosts this surface inside `app-dialog`, and two
   * stacked dialogs both listening for Escape on `document` would both close
   * on one keypress (`stopPropagation` does not stop a sibling listener on the
   * same node).
   */
  protected arm(decision: Decision): void {
    if (this.busy()) return;
    if (decision === 'apply' && this.conflict()) return;
    this.decisionError.set('');
    this.pendingDecision.set(decision);
    // Move focus onto the confirm action. Arming replaces the button the user
    // just pressed, so without this focus falls back to <body> and the second
    // step is unreachable by keyboard.
    setTimeout(() => this.focusIn('[data-decision-confirm]'));
  }

  protected cancelDecision(): void {
    if (this.busy()) return;
    const armed = this.pendingDecision();
    this.pendingDecision.set(null);
    // Put focus back on the control that armed it, not on <body>. Cancelling
    // otherwise drops a keyboard user out of the decision bar entirely.
    if (armed) setTimeout(() => this.focusIn(`[data-decision="${armed}"]`));
  }

  /** Focus the real `<button>` inside a marked `app-button` host. */
  private focusIn(selector: string): void {
    const host = this.host.nativeElement as HTMLElement;
    const el =
      host.querySelector<HTMLElement>(`${selector} button`) ??
      host.querySelector<HTMLElement>(selector);
    el?.focus();
  }

  /**
   * Whether this surface owns an Escape press.
   *
   * Three ways it does not:
   *
   * - it is not mounted, or another review mounted after it is still up;
   * - it is an inline review (the job page) and any modal is open — the modal
   *   is above the whole page, so the key is the modal's;
   * - it is in a dialog and another dialog sits above ours in document order,
   *   or is nested inside it.
   *
   * Without this, a single `document` capture listener made every mounted
   * review swallow every Escape, including ones aimed at whatever was
   * actually on top.
   */
  private ownsEscape(): boolean {
    const host = this.host.nativeElement as HTMLElement;
    if (!host.isConnected) return false;
    const live = mountedReviews.filter((r) => r.isConnected());
    if (live[live.length - 1] !== this) return false;

    const ownDialog = host.closest('[role="dialog"]');
    for (const dialog of Array.from(document.querySelectorAll('[role="dialog"]'))) {
      if (dialog === ownDialog || dialog.contains(host)) continue;
      if (!ownDialog) return false;
      const relation = ownDialog.compareDocumentPosition(dialog);
      // FOLLOWING = opened after ours in document order; CONTAINED_BY = a
      // dialog nested inside ours. Either way it is on top.
      const following = relation & Node.DOCUMENT_POSITION_FOLLOWING;
      const nested = relation & Node.DOCUMENT_POSITION_CONTAINED_BY;
      if (following || nested) return false;
    }
    return true;
  }

  /** Public only so `ownsEscape` can consult sibling instances. */
  isConnected(): boolean {
    return (this.host.nativeElement as HTMLElement).isConnected;
  }

  /**
   * Escape cancels an armed confirmation, or is swallowed outright while a
   * write is in flight, before a hosting dialog can act on it.
   *
   * Registered on `document` in the CAPTURE phase, deliberately. A host-element
   * listener looked equivalent and is not: arming the confirmation removes the
   * button the user pressed, focus falls back to `<body>`, and the keydown then
   * never travels through this component at all — so the dialog's own
   * document-level handler would be the only one to see it and would close the
   * whole review. Capturing at the document means this always runs first, and
   * `stopPropagation` there keeps the event from ever reaching the dialog's
   * bubble-phase listener on the same node.
   */
  private onDocumentKeydown = (event: KeyboardEvent): void => {
    if (event.key !== 'Escape') return;
    if (!this.ownsEscape()) return;
    if (this.busy()) {
      // Never let a dismissal race an in-flight cloud write.
      event.stopPropagation();
      event.preventDefault();
      return;
    }
    if (this.pendingDecision()) {
      event.stopPropagation();
      event.preventDefault();
      this.cancelDecision();
    }
  };

  protected confirmDecision(): void {
    const decision = this.pendingDecision();
    if (!decision) return;
    // Double-submit guard. The buttons are disabled while submitting, but a
    // second activation inside one change-detection tick (double-click, or
    // Enter+click) would otherwise reach here twice and issue two writes.
    if (this.busy()) return;
    // Never act on inputs. `loaded` is the identity of the bytes the user has
    // been looking at; if there is none, there is nothing to decide about.
    const target = this.loaded();
    if (!target) return;
    this.pendingDecision.set(null);
    this.decisionError.set('');
    if (decision === 'apply') this.runApply(target);
    else this.runReject(target);
  }

  private runApply(target: ReviewIdentity): void {
    this.submitting.set('apply');
    this.partial.set(null);
    this.announce('jobDiffReview.a11y.applying');
    const pinnedEpoch = target.epoch;
    const obs = target.threadId
      ? this.api.applyThreadCloudDiff(target.threadId, pinnedEpoch ?? -1)
      : this.api.acceptJobDiff(target.jobId!);
    obs.subscribe((outcome) => {
      // The busy latch means `loaded` cannot have moved while this was in
      // flight — `loadDiff` defers instead of re-pointing. This stays as the
      // last line of defence: if it ever did move, the receipt is still
      // persisted and nothing is painted onto another review.
      const mine = this.adopt(target);
      switch (outcome.kind) {
        case 'ok': {
          const data = outcome.data as Partial<ThreadCloudApplyResult> & {
            applied: number;
            deleted: number;
          };
          // Written before the identity check on purpose: a write that landed
          // must be recorded even when the surface has moved on, or the
          // outcome is lost exactly the way PC-20 lost one.
          this.finishDecision(target, mine, {
            decision: 'applied',
            epoch: pinnedEpoch ?? data.epoch ?? null,
            applied: data.applied ?? 0,
            deleted: data.deleted ?? 0,
            // Job mode has no overlay to reset; treat it as reset so the
            // session-only warning never fires there.
            overlayReset: data.overlay_reset ?? true,
            at: new Date().toISOString(),
          });
          if (mine) {
            // Job context only. In a session the surface stays mounted and
            // shows the receipt, so a toast adds nothing and — being
            // bottom-right — lands on top of the receipt's own Done button.
            if (target.context === 'job') {
              this.toast.success(
                this.t('toasts.jobs.diffAccepted', {
                  applied: data.applied,
                  deleted: data.deleted,
                }),
              );
            }
            this.announce('jobDiffReview.a11y.applied', {
              applied: data.applied,
              deleted: data.deleted,
            });
            this.resolved.emit('accepted');
          }
          this.settleAction();
          break;
        }
        case 'conflict':
          if (mine) this.conflict.set(outcome.data);
          this.settleAction({
            tone: 'danger',
            message: this.t('jobDiffReview.conflict.title'),
          });
          break;
        case 'partial':
          if (mine) this.partial.set(outcome.data);
          this.settleAction({
            tone: 'danger',
            message: this.t('jobDiffReview.partial.title'),
          });
          break;
        case 'stale':
          // Someone else applied/rejected/restaged since we read the
          // summary — reload against the fresh epoch and let the user
          // re-decide rather than silently applying stale content.
          if (mine) this.toast.info(this.t('jobDiffReview.staleNotice'));
          if (!this.settleAction() && mine) this.reload();
          break;
        case 'error':
          this.handleActionError(outcome.status, outcome.detail, mine);
          break;
      }
    });
  }

  private runReject(target: ReviewIdentity): void {
    this.submitting.set('reject');
    this.announce('jobDiffReview.a11y.rejecting');
    const pinnedEpoch = target.epoch;
    // Same union-of-Observables issue as selectFile — separate .subscribe()
    // calls per branch, shared handler. The element types differ (a thread
    // returns `{rejected, epoch, overlay_reset}`, a job returns
    // `{job_id, diff_status, status}`), so the shared handler takes the
    // tagged outcome over `unknown` data.
    const onResult = (outcome: DiffRejectOutcome<unknown>) => {
      const mine = this.adopt(target);
      switch (outcome.kind) {
        case 'ok': {
          const body = outcome.data as { overlay_reset?: boolean; epoch?: number };
          this.finishDecision(target, mine, {
            decision: 'rejected',
            epoch: pinnedEpoch ?? body.epoch ?? null,
            applied: 0,
            deleted: 0,
            overlayReset: body.overlay_reset ?? true,
            at: new Date().toISOString(),
          });
          if (mine) {
            if (target.context === 'job') {
              this.toast.success(this.t('toasts.jobs.diffRejected'));
            }
            this.announce('jobDiffReview.a11y.rejected');
            this.resolved.emit('rejected');
          }
          this.settleAction();
          break;
        }
        case 'stale':
          // Identical treatment to apply's: the pin the controls were built
          // on is gone, so reloading is the only honest next step. Leaving
          // the old Reject button live over a refused epoch is what this
          // replaces.
          if (mine) this.toast.info(this.t('jobDiffReview.staleNotice'));
          if (!this.settleAction() && mine) this.reload();
          break;
        case 'nothing_staged':
          // Resolved somewhere else while this surface was open. Not an
          // error — reload into the resolved state.
          if (!this.settleAction() && mine) this.reload();
          break;
        case 'error':
          this.handleActionError(outcome.status, outcome.detail, mine);
          break;
      }
    };
    if (target.threadId) {
      this.api.rejectThreadCloudDiff(target.threadId, pinnedEpoch ?? -1).subscribe(onResult);
    } else {
      this.api.rejectJobDiff(target.jobId!).subscribe(onResult);
    }
  }

  /**
   * Whether an action response may still touch this surface.
   *
   * False means the host re-pointed the component while the request was in
   * flight: the response describes a review that is no longer on screen, and
   * painting a receipt from it would attribute one review's outcome to
   * another's bytes. The response is dropped from the view — but a successful
   * one is still written to its own thread's storage first (see
   * `finishDecision`), so the outcome is recoverable rather than lost.
   */
  private adopt(target: ReviewIdentity): boolean {
    return sameReview(this.loaded(), target);
  }

  /**
   * Release the busy latch and, if the host asked for a different target
   * while the write was in flight, load it now. Returns whether it did.
   *
   * `escalate` carries a message to raise as a toast — but only when a deferred
   * load is about to replace the surface that would otherwise have carried
   * the report. Conflict and partial notices and the decision-bar error line
   * all live on the review being replaced, so without this a job whose apply
   * conflicted while the host moved on would report nothing at all. With no
   * deferred target nothing is escalated and the ordinary flow is unchanged.
   */
  private settleAction(escalate?: { tone: 'danger' | 'info'; message: string }): boolean {
    const next = this.deferredTarget;
    this.submitting.set(null);
    if (!next) return false;
    this.deferredTarget = null;
    if (escalate) {
      if (escalate.tone === 'info') this.toast.info(escalate.message);
      else this.toast.danger(escalate.message);
    }
    // Never issue a fetch for a surface that has already been torn down.
    if (!this.isConnected()) return true;
    this.loadDiff(next.jobId, next.threadId);
    return true;
  }

  /**
   * `nothing_staged` means the diff was resolved somewhere else while this
   * surface was open — reloading is the honest response, not an error toast
   * about a diff that no longer exists. Anything else is rendered in the
   * decision bar, where the controls it refers to are.
   */
  private handleActionError(status: number, detail: string, mine: boolean): void {
    if (status === 409) {
      if (mine) this.toast.info(detail);
      if (!this.settleAction() && mine) this.reload();
      return;
    }
    if (mine) this.decisionError.set(detail);
    // The decision bar is about to be replaced by the deferred target, so the
    // failure has to leave the surface with it.
    this.settleAction({ tone: 'danger', message: detail });
  }

  /**
   * Persist the outcome, then — only if this surface still shows the review
   * the decision was made against — put it on screen.
   *
   * Returns whether the record reached storage, which is what licenses the
   * "recorded in this browser only" line.
   */
  private finishDecision(
    target: ReviewIdentity,
    mine: boolean,
    receipt: CloudReviewReceipt,
  ): boolean {
    const stored = writeReceipt(target.threadId, receipt);
    if (!mine) return stored;
    this.receipt.set(receipt);
    this.receiptStored.set(stored);
    this.resolvedNow.set(true);
    // The staged set is gone. Drop the selection and tear the editor down so
    // the surface shows the outcome, not a diff that no longer exists.
    this.selectedPath.set(null);
    this.selectedEntry.set(null);
    this.selectedFile.set(null);
    this.fileState.set('idle');
    this.conflict.set(null);
    this.disposeEditor();
    return stored;
  }

  protected onDone(): void {
    this.closeRequested.emit();
  }

  // ---------- helpers used in template ----------

  private t(key: string, params?: Record<string, unknown>): string {
    return this.translocoService.translate(key, params);
  }

  private announce(key: string, params?: Record<string, unknown>): void {
    this.liveMessage.set({ key, params });
  }

  /**
   * Context-scoped translation key. Every string that differs between a
   * session review and a job review resolves through here, so job wording
   * cannot leak into a session by omission — a missing leaf renders as a raw
   * key and fails a spec, rather than silently falling back to the other
   * context's copy.
   */
  protected ck(leaf: string): string {
    return `jobDiffReview.${this.context()}.${leaf}`;
  }

  /**
   * Which explanation a per-file 404 gets.
   *
   * The endpoint returns one 404 for three different situations — the path
   * left the staged set, nothing is staged at all, and the staged tar is
   * unreadable (a torn manifest/tar pair). Saying "the session has re-staged"
   * for all three was a guess that is wrong two thirds of the time. The
   * backend now tags the first two cases; older orchestrators send no code,
   * and job mode has no equivalent, so the untagged copy is written to be
   * true whichever it was.
   */
  protected missingBodyKey(): string {
    const reason = this.missingReason();
    if (reason === 'not_in_staged_diff') return this.ck('missingBodyGone');
    if (reason === 'staged_content_unreadable') return this.ck('missingBodyUnreadable');
    return this.ck('missingBody');
  }

  protected statusGlyph(status: DiffFileEntry['status']): string {
    return status === 'added' ? '+' : status === 'deleted' ? '−' : '~';
  }

  protected statusLabelKey(status: DiffFileEntry['status']): string {
    return status === 'added'
      ? 'jobDiffReview.fileTree.statusAdded'
      : status === 'deleted'
        ? 'jobDiffReview.fileTree.statusDeleted'
        : 'jobDiffReview.fileTree.statusModified';
  }

  protected conflictLabel(kind: JobAcceptConflict['diverged'][number]['kind']): string {
    switch (kind) {
      case 'etag_mismatch':
        return this.t('jobDiffReview.conflict.etagMismatch');
      case 'missing_at_cloud':
        return this.t('jobDiffReview.conflict.missingAtCloud');
      case 'unexpected_at_cloud':
        return this.t('jobDiffReview.conflict.unexpectedAtCloud');
    }
  }

  /** Absolute time for the staged-at line; the tooltip carries the raw ISO. */
  protected formatStagedAt(value: string | null | undefined): string {
    if (!value) return '';
    try {
      return new Intl.DateTimeFormat(this.translocoService.getActiveLang(), {
        dateStyle: 'medium',
        timeStyle: 'short',
      }).format(new Date(value));
    } catch {
      return value;
    }
  }

  protected stagedAt = computed<string | null>(() => {
    const sum = this.summary();
    return sum && 'staged_at' in sum ? sum.staged_at : null;
  });

  /** Whether the technical-details disclosure has anything to disclose. */
  protected hasDetails = computed(
    () =>
      !!this.stagedAt() ||
      !!this.mountPath() ||
      this.epoch() !== null ||
      !!this.commitRange(),
  );

  /** Job context only — the baseline the diff is measured from. */
  protected commitRange = computed<string | null>(() => {
    const sum = this.summary();
    if (!sum || !('baseline_commit' in sum)) return null;
    if (!sum.baseline_commit || !sum.head_commit) return null;
    return `${sum.baseline_commit.slice(0, 7)} → ${sum.head_commit.slice(0, 7)}`;
  });
}
