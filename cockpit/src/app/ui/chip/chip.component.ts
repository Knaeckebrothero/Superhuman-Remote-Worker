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

export type ChipVariant = 'default' | 'accent' | 'danger';
export type ChipSize = 'sm' | 'md';

@Component({
  selector: 'app-chip',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      class="app-chip__btn"
      type="button"
      [disabled]="disabled()"
      [attr.aria-pressed]="selectable() ? selected() : null"
      [attr.aria-label]="ariaLabel() || null"
      [attr.data-variant]="variant()"
      [attr.data-size]="size()"
      [attr.data-selected]="selected() || null"
      [attr.data-selectable]="selectable() || null"
    >
      <span class="app-chip__content">
        <ng-content></ng-content>
      </span>
    </button>
  `,
  styleUrl: './chip.component.scss',
})
export class AppChipComponent {
  variant = input<ChipVariant>('default');
  size = input<ChipSize>('sm');
  selected = input<boolean>(false);
  selectable = input<boolean>(true);
  disabled = input<boolean>(false);
  ariaLabel = input<string>('');

  clicked = output<MouseEvent>();
  toggled = output<boolean>();

  protected isInteractive = computed(() => !this.disabled());

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
    if (this.disabled()) {
      event.stopImmediatePropagation();
      event.preventDefault();
      return;
    }
    this.clicked.emit(event);
    if (this.selectable()) {
      this.toggled.emit(!this.selected());
    }
  }
}
