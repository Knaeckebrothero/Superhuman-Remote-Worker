import { ChangeDetectionStrategy, Component, computed, inject, input, output } from '@angular/core';
import { TranslocoPipe, TranslocoService } from '@jsverse/transloco';
import { AppButtonComponent } from '../../../ui/button';
import { AppIconComponent } from '../../../ui/icon';
import { readReceipt } from '../../job-diff-review/cloud-review-receipt';

/** Outcome of the hidden pending-count probe the chat service runs. */
export type CloudReviewProbe = 'idle' | 'loading' | 'ready' | 'error';

/** What, if anything, the banner should render. */
export type CloudReviewBannerMode = 'hidden' | 'pending' | 'unknown' | 'receipt';

/**
 * Whether the pending-review call to action should render.
 *
 * Both conditions are needed and neither is `isConnected()`:
 *
 * - `protectedCloud` alone doesn't mean anything is staged yet, and a nonzero
 *   count must not survive a switch to an unprotected thread (a stale signal
 *   from the previous session);
 * - the review API serves ENDED threads on purpose
 *   (`orchestrator/main.py:43451` — "Deliberately does NOT require
 *   `row["status"] == "active"`"), and gating the only entry point on the live
 *   agent connection is what turned a recoverable duplicate stage into a trap
 *   the owner could only escape by resuming a session with known stale-input
 *   replay (protected_cloud_review.md PC-25, PC-18).
 */
export function cloudReviewBannerVisible(protectedCloud: boolean, count: number): boolean {
  return protectedCloud && count > 0;
}

/**
 * Which of the banner's three states applies.
 *
 * The `unknown` state is the repair for a real gap: the banner rendered only
 * after a *successful* count probe, so one transient failure on load left a
 * protected ended session with no entry point to the review at all and no way
 * to ask again — the same dead end PC-25 was about, reached by a different
 * road. It must not claim changes exist (nobody knows), but it must keep the
 * door open.
 *
 * `receipt` is the post-decision state: nothing is pending, but this browser
 * holds the last result and the review surface can still show it. Deliberately
 * quiet — it is a convenience, not an audit trail (PC-20 stays open).
 */
export function cloudReviewBannerMode(input: {
  protectedCloud: boolean;
  count: number;
  probe: CloudReviewProbe;
  hasReceipt: boolean;
}): CloudReviewBannerMode {
  if (!input.protectedCloud) return 'hidden';
  if (input.count > 0) return 'pending';
  // A failed probe outranks a stored receipt: "we could not check" is more
  // important than "here is what happened last time".
  if (input.probe === 'error') return 'unknown';
  if (input.probe === 'ready' && input.hasReceipt) return 'receipt';
  return 'hidden';
}

/**
 * Prominent, always-reachable entry point to a session's staged cloud review.
 *
 * Replaces the `Cloud changes (N)` status-bar badge, which PC-23 found read as
 * a passive status chip rather than an action, was keyboard-unreachable
 * (`role="button"` with no `tabindex`), and lived inside the connection-gated
 * status bar.
 *
 * Its own component (rather than markup in persistent-chat) for a measured
 * reason: `persistent-chat.component.scss` builds to 41.14 kB against a 48 kB
 * `anyComponentStyle` error budget, which is why the old drawer's chrome was
 * six inline `style=` attributes. A separate component gets its own budget.
 */
@Component({
  selector: 'app-cloud-review-banner',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TranslocoPipe, AppButtonComponent, AppIconComponent],
  template: `
    @switch (mode()) {
      @case ('pending') {
        <div
          class="crb"
          data-mode="pending"
          role="region"
          [attr.aria-label]="'chat.cloudReview.regionLabel' | transloco"
        >
          <app-icon size="sm" class="crb__icon" aria-hidden="true">gpp_maybe</app-icon>
          <div class="crb__body">
            <p class="crb__title">
              @if (count() === 1) {
                {{ 'chat.cloudReview.titleSingular' | transloco: {count: count()} }}
              } @else {
                {{ 'chat.cloudReview.title' | transloco: {count: count()} }}
              }
            </p>
            <p class="crb__meta">
              {{ 'chat.cloudReview.subtitle' | transloco }}
              <!-- The project's own name, or nothing. The raw mount is the
                   workspace target path — usually the literal string "cloud" —
                   which names nothing a user recognises. -->
              @if (folderName(); as name) {
                <span class="crb__sep" aria-hidden="true">·</span>
                <span class="crb__folder">{{ name }}</span>
              }
              @if (stagedAtLabel()) {
                <span class="crb__sep" aria-hidden="true">·</span>
                <span [attr.title]="stagedAt()">{{
                  'chat.cloudReview.staged' | transloco: {at: stagedAtLabel()}
                }}</span>
              }
            </p>
          </div>
          <div class="crb__actions">
            <app-button variant="primary" size="sm" (clicked)="review.emit()">
              {{ 'chat.cloudReview.action' | transloco }}
            </app-button>
          </div>
        </div>
      }

      @case ('unknown') {
        <div
          class="crb"
          data-mode="unknown"
          role="region"
          [attr.aria-label]="'chat.cloudReview.regionLabel' | transloco"
        >
          <app-icon size="sm" class="crb__icon" aria-hidden="true">cloud_off</app-icon>
          <div class="crb__body">
            <p class="crb__title">{{ 'chat.cloudReview.unknownTitle' | transloco }}</p>
            <p class="crb__meta">{{ 'chat.cloudReview.unknownBody' | transloco }}</p>
          </div>
          <div class="crb__actions">
            <app-button variant="ghost" size="sm" (clicked)="recheck.emit()">
              {{ 'chat.cloudReview.recheck' | transloco }}
            </app-button>
            <app-button variant="secondary" size="sm" (clicked)="review.emit()">
              {{ 'chat.cloudReview.openReview' | transloco }}
            </app-button>
          </div>
        </div>
      }

      @case ('receipt') {
        <!-- Quiet by design. Without it the browser-local record had no entry
             point once nothing was pending, which made keeping it close to
             pointless; with a loud one it would read as a durable receipt,
             which it is not. -->
        <div
          class="crb crb--quiet"
          data-mode="receipt"
          role="region"
          [attr.aria-label]="'chat.cloudReview.receiptRegionLabel' | transloco"
        >
          <app-icon size="sm" class="crb__icon" aria-hidden="true">
            {{ receipt()?.decision === 'applied' ? 'cloud_done' : 'undo' }}
          </app-icon>
          <div class="crb__body">
            <p class="crb__meta">
              @if (receipt(); as r) {
                {{
                  (r.decision === 'applied'
                    ? 'chat.cloudReview.receiptApplied'
                    : 'chat.cloudReview.receiptRejected'
                  ) | transloco: {applied: r.applied, deleted: r.deleted}
                }}
                <span class="crb__sep" aria-hidden="true">·</span>
                <span [attr.title]="r.at">{{ receiptAtLabel() }}</span>
                <span class="crb__sep" aria-hidden="true">·</span>
                <span class="crb__provenance">{{ 'chat.cloudReview.receiptLocal' | transloco }}</span>
              }
            </p>
          </div>
          <div class="crb__actions">
            <app-button variant="ghost" size="sm" (clicked)="review.emit()">
              {{ 'chat.cloudReview.viewResult' | transloco }}
            </app-button>
          </div>
        </div>
      }
    }
  `,
  styleUrl: './cloud-review-banner.component.scss',
})
export class CloudReviewBannerComponent {
  private transloco = inject(TranslocoService);

  protectedCloud = input<boolean>(false);
  count = input<number>(0);
  /** Outcome of the chat service's hidden pending-count probe. */
  probe = input<CloudReviewProbe>('idle');
  /** Verified project-folder name, or null. Never the raw mount path. */
  folderName = input<string | null>(null);
  stagedAt = input<string | null>(null);
  /** Needed to look up this browser's record of the last decision. */
  threadId = input<string | null>(null);

  /** Open the review surface. */
  review = output<void>();
  /** Re-run the pending-count probe after a failure. */
  recheck = output<void>();

  /**
   * The browser-local record, re-read whenever the thread or the pending
   * count changes. Reading storage inside a computed is safe precisely
   * because those two inputs are what can invalidate it: a decision drives
   * the count to zero, which re-runs this.
   */
  protected receipt = computed(() => {
    this.count();
    return readReceipt(this.threadId());
  });

  protected mode = computed<CloudReviewBannerMode>(() =>
    cloudReviewBannerMode({
      protectedCloud: this.protectedCloud(),
      count: this.count(),
      probe: this.probe(),
      hasReceipt: !!this.receipt(),
    }),
  );

  protected stagedAtLabel = computed<string>(() => this.formatTime(this.stagedAt()));
  protected receiptAtLabel = computed<string>(() => this.formatTime(this.receipt()?.at ?? null));

  private formatTime(value: string | null): string {
    if (!value) return '';
    try {
      return new Intl.DateTimeFormat(this.transloco.getActiveLang(), {
        dateStyle: 'medium',
        timeStyle: 'short',
      }).format(new Date(value));
    } catch {
      return value;
    }
  }
}
