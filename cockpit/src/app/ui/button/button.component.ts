import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  HostListener,
  computed,
  inject,
  input,
  output,
} from '@angular/core';
import { FocusMonitor } from '@angular/cdk/a11y';

export type ButtonVariant =
  | 'primary'
  | 'secondary'
  | 'ghost'
  | 'danger'
  | 'success'
  | 'warning'
  | 'info';
export type ButtonSize = 'sm' | 'md' | 'lg';
export type ButtonType = 'button' | 'submit' | 'reset';

@Component({
  selector: 'app-button',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      class="app-button__btn"
      [type]="type()"
      [disabled]="isDisabled()"
      [attr.aria-label]="ariaLabel() || null"
      [attr.aria-busy]="loading() || null"
      [attr.data-variant]="variant()"
      [attr.data-size]="size()"
      [attr.data-loading]="loading() || null"
      [attr.data-full-width]="fullWidth() || null"
    >
      @if (loading()) {
        <span class="app-button__spinner" aria-hidden="true"></span>
      }
      <span class="app-button__content" [attr.data-loading]="loading() || null">
        <ng-content></ng-content>
      </span>
    </button>
  `,
  styleUrl: './button.component.scss',
  host: {
    '[attr.data-full-width]': 'fullWidth() || null',
  },
})
export class AppButtonComponent {
  variant = input<ButtonVariant>('primary');
  size = input<ButtonSize>('md');
  type = input<ButtonType>('button');
  disabled = input<boolean>(false);
  loading = input<boolean>(false);
  fullWidth = input<boolean>(false);
  ariaLabel = input<string>('');

  clicked = output<MouseEvent>();

  protected isDisabled = computed(() => this.disabled() || this.loading());

  private focusMonitor = inject(FocusMonitor);
  private host = inject(ElementRef<HTMLElement>);

  constructor() {
    this.focusMonitor.monitor(this.host.nativeElement, true);
  }

  ngOnDestroy() {
    this.focusMonitor.stopMonitoring(this.host.nativeElement);
  }

  @HostListener('click', ['$event'])
  protected onHostClick(event: MouseEvent) {
    if (this.isDisabled()) {
      event.stopImmediatePropagation();
      event.preventDefault();
      return;
    }
    this.clicked.emit(event);
  }
}
