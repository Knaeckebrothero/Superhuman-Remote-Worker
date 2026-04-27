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
import { AppTooltipDirective } from '../tooltip';

export type IconButtonVariant = 'ghost' | 'primary' | 'danger';
export type IconButtonSize = 'sm' | 'md' | 'lg';
export type IconButtonType = 'button' | 'submit' | 'reset';

@Component({
  selector: 'app-icon-button',
  standalone: true,
  imports: [AppTooltipDirective],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      class="app-icon-button__btn"
      [type]="type()"
      [disabled]="isDisabled()"
      [attr.aria-label]="ariaLabel()"
      [attr.aria-busy]="loading() || null"
      [appTooltip]="tooltip()"
      [attr.data-variant]="variant()"
      [attr.data-size]="size()"
      [attr.data-loading]="loading() || null"
    >
      @if (loading()) {
        <span class="app-icon-button__spinner" aria-hidden="true"></span>
      } @else {
        <span class="app-icon-button__content"><ng-content></ng-content></span>
      }
    </button>
  `,
  styleUrl: './icon-button.component.scss',
})
export class AppIconButtonComponent {
  variant = input<IconButtonVariant>('ghost');
  size = input<IconButtonSize>('md');
  type = input<IconButtonType>('button');
  disabled = input<boolean>(false);
  loading = input<boolean>(false);
  ariaLabel = input.required<string>();
  tooltip = input<string>('');

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
