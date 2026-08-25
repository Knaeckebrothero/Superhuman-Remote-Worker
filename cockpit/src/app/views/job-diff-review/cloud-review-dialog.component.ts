import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';
import { TranslocoPipe } from '@jsverse/transloco';
import { AppDialogComponent } from '../../ui/dialog';
import {
  JobDiffReviewComponent,
  ProtectedFolderLink,
  ReviewContext,
} from './job-diff-review.component';

/**
 * Modal host for the diff review surface.
 *
 * The session flow used to render the review as an inline block inside the
 * chat column with `height: 70vh` and six hard-coded inline `style=`
 * attributes — no `role`, no `aria-modal`, no focus trap, no Escape, no focus
 * restore (protected_cloud_review.md PC-23). Promoting it to `app-dialog`
 * inherits all of that correctly instead of reimplementing it, and stops the
 * review from shoving the transcript down by 70vh.
 *
 * The one behaviour this wrapper adds over a plain dialog is the busy latch:
 * while an apply or reject is in flight the dialog refuses every close path —
 * Escape, backdrop, and the close button all become inert. PC-20's owner lost
 * a 34-second apply's outcome because the surface went away underneath the
 * request; a dismissal must not be able to race a cloud write.
 */
@Component({
  selector: 'app-cloud-review-dialog',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TranslocoPipe, AppDialogComponent, JobDiffReviewComponent],
  template: `
    <app-dialog
      [open]="open()"
      (openChange)="onOpenChange($event)"
      size="xl"
      [fullHeight]="true"
      [flushBody]="true"
      [closable]="!busy()"
      [closeOnBackdrop]="!busy()"
      [closeOnEsc]="!busy()"
      [title]="'jobDiffReview.' + context() + '.dialogLabel' | transloco"
      [closeLabel]="'jobDiffReview.actions.close' | transloco"
    >
      <app-job-diff-review
        [jobId]="jobId()"
        [threadId]="threadId()"
        [projectFolder]="projectFolder()"
        (resolved)="resolved.emit($event)"
        (busyChange)="busy.set($event)"
        (closeRequested)="requestClose()"
      />
    </app-dialog>
  `,
  styles: [
    `
      :host {
        display: contents;
      }

      app-job-diff-review {
        display: flex;
        flex: 1 1 auto;
        min-height: 0;
      }
    `,
  ],
})
export class CloudReviewDialogComponent {
  open = input<boolean>(false);
  jobId = input<string | null>(null);
  threadId = input<string | null>(null);
  projectFolder = input<ProtectedFolderLink | null>(null);

  /** Emitted when the user (or the surface) wants the dialog closed. The host
   *  owns the open flag, so it decides — this never closes itself. */
  closed = output<void>();
  resolved = output<'accepted' | 'rejected'>();

  protected busy = signal(false);
  protected context = computed<ReviewContext>(() => (this.threadId() ? 'session' : 'job'));

  protected onOpenChange(open: boolean): void {
    if (open) return;
    if (this.busy()) return; // belt-and-braces; the dialog is already inert
    this.closed.emit();
  }

  protected requestClose(): void {
    this.closed.emit();
  }
}
