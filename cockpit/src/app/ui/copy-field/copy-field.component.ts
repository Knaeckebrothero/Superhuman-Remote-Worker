import {ChangeDetectionStrategy, Component, OnDestroy, input, signal} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppIconButtonComponent} from '../icon-button';
import {AppIconComponent} from '../icon';
import {copyText} from './copy-text';

/** How long the "copied" check stays visible after a successful copy. */
const COPIED_RESET_MS = 2500;

/**
 * Displays a (optionally labelled) full value in monospace with a
 * copy-to-clipboard button. Used to surface full, copyable job/session IDs in
 * the Action Center detail panels and the job review page.
 */
@Component({
  selector: 'app-copy-field',
  standalone: true,
  imports: [TranslocoPipe, AppIconButtonComponent, AppIconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (label()) {
      <span class="app-copy-field__label">{{ label() }}</span>
    }
    <span class="app-copy-field__value" [title]="value()">{{ value() }}</span>
    <app-icon-button
      variant="ghost"
      size="sm"
      [ariaLabel]="(copied() ? 'common.copied' : 'common.copy') | transloco"
      [tooltip]="(copied() ? 'common.copied' : 'common.copy') | transloco"
      (clicked)="copy(value())"
    >
      <app-icon size="sm">{{ copied() ? 'check' : 'content_copy' }}</app-icon>
    </app-icon-button>
  `,
  styleUrl: './copy-field.component.scss',
})
export class AppCopyFieldComponent implements OnDestroy {
  /** Optional, already-translated label shown before the value. */
  readonly label = input<string>('');
  /** The full value to display and copy. */
  readonly value = input.required<string>();

  readonly copied = signal(false);
  private resetTimer: ReturnType<typeof setTimeout> | null = null;

  async copy(text: string): Promise<void> {
    if (!(await copyText(text))) return;
    this.copied.set(true);
    if (this.resetTimer) clearTimeout(this.resetTimer);
    this.resetTimer = setTimeout(() => this.copied.set(false), COPIED_RESET_MS);
  }

  ngOnDestroy(): void {
    if (this.resetTimer) clearTimeout(this.resetTimer);
  }
}
