import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  HostListener,
  effect,
  inject,
  input,
  model,
  output,
  viewChild,
} from '@angular/core';
import { CdkTrapFocus } from '@angular/cdk/a11y';

export type DialogSize = 'sm' | 'md' | 'lg' | 'xl';
export type DialogCloseReason = 'backdrop' | 'escape' | 'close-button' | 'programmatic';

let dialogIdCounter = 0;

@Component({
  selector: 'app-dialog',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CdkTrapFocus],
  template: `
    @if (open()) {
      <div
        class="app-dialog__backdrop"
        (click)="onBackdropClick()"
      >
        <div
          #container
          class="app-dialog__container"
          role="dialog"
          aria-modal="true"
          [attr.aria-labelledby]="title() ? titleId : null"
          [attr.aria-label]="!title() ? ariaLabel() || null : null"
          [attr.data-size]="size()"
          cdkTrapFocus
          [cdkTrapFocusAutoCapture]="true"
          (click)="$event.stopPropagation()"
        >
          @if (title() || closable()) {
            <header class="app-dialog__header">
              @if (title()) {
                <h2 [id]="titleId" class="app-dialog__title">{{ title() }}</h2>
              } @else {
                <span class="app-dialog__title-spacer"></span>
              }
              @if (closable()) {
                <button
                  type="button"
                  class="app-dialog__close"
                  aria-label="Close dialog"
                  (click)="requestClose('close-button')"
                >×</button>
              }
            </header>
          }
          <div class="app-dialog__body">
            <ng-content></ng-content>
          </div>
          <footer class="app-dialog__actions">
            <ng-content select="[appDialogActions]"></ng-content>
          </footer>
        </div>
      </div>
    }
  `,
  styleUrl: './dialog.component.scss',
})
export class AppDialogComponent {
  open = model<boolean>(false);

  title = input<string>('');
  size = input<DialogSize>('md');
  closable = input<boolean>(true);
  closeOnBackdrop = input<boolean>(true);
  closeOnEsc = input<boolean>(true);
  ariaLabel = input<string>('');

  closed = output<DialogCloseReason>();

  protected readonly titleId = `app-dialog-title-${++dialogIdCounter}`;
  protected container = viewChild<ElementRef<HTMLElement>>('container');

  private host = inject(ElementRef<HTMLElement>);
  private previousActiveElement: Element | null = null;
  private originalBodyOverflow: string | null = null;

  constructor() {
    // Body scroll lock + restore-focus lifecycle.
    effect(() => {
      if (this.open()) {
        this.previousActiveElement = document.activeElement;
        if (this.originalBodyOverflow === null) {
          this.originalBodyOverflow = document.body.style.overflow;
          document.body.style.overflow = 'hidden';
        }
      } else {
        if (this.originalBodyOverflow !== null) {
          document.body.style.overflow = this.originalBodyOverflow;
          this.originalBodyOverflow = null;
        }
        // Restore focus to the trigger when closing.
        const prev = this.previousActiveElement as HTMLElement | null;
        if (prev && typeof prev.focus === 'function' && document.contains(prev)) {
          prev.focus();
        }
        this.previousActiveElement = null;
      }
    });
  }

  ngOnDestroy() {
    if (this.originalBodyOverflow !== null) {
      document.body.style.overflow = this.originalBodyOverflow;
    }
  }

  close(reason: DialogCloseReason = 'programmatic') {
    if (!this.open()) return;
    this.open.set(false);
    this.closed.emit(reason);
  }

  protected onBackdropClick() {
    if (!this.closeOnBackdrop()) return;
    this.requestClose('backdrop');
  }

  protected requestClose(reason: DialogCloseReason) {
    this.close(reason);
  }

  @HostListener('document:keydown.escape', ['$event'])
  protected onEscape(event: Event) {
    if (!this.open() || !this.closeOnEsc()) return;
    event.preventDefault();
    event.stopPropagation();
    this.requestClose('escape');
  }
}
