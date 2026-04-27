import {
  ChangeDetectionStrategy,
  Component,
  HostListener,
  computed,
  input,
  output,
} from '@angular/core';

export type CardVariant = 'surface' | 'panel' | 'outlined' | 'ghost';
export type CardPadding = 'none' | 'sm' | 'md' | 'lg';

@Component({
  selector: 'app-card',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<ng-content></ng-content>`,
  styleUrl: './card.component.scss',
  host: {
    '[attr.data-variant]': 'variant()',
    '[attr.data-padding]': 'padding()',
    '[attr.data-interactive]': 'interactive() || null',
    '[attr.data-selected]': 'selected() || null',
    '[attr.data-disabled]': 'disabled() || null',
    '[attr.role]': 'roleAttr()',
    '[attr.tabindex]': 'tabIndexAttr()',
    '[attr.aria-pressed]': 'ariaPressed()',
    '[attr.aria-disabled]': 'disabled() || null',
  },
})
export class AppCardComponent {
  variant = input<CardVariant>('surface');
  padding = input<CardPadding>('md');
  interactive = input<boolean>(false);
  selected = input<boolean>(false);
  disabled = input<boolean>(false);

  activated = output<Event>();

  protected roleAttr = computed(() => (this.interactive() ? 'button' : null));
  protected tabIndexAttr = computed(() =>
    this.interactive() ? (this.disabled() ? -1 : 0) : null,
  );
  protected ariaPressed = computed(() =>
    this.interactive() ? (this.selected() ? 'true' : 'false') : null,
  );

  @HostListener('click', ['$event'])
  protected onClick(event: MouseEvent): void {
    if (!this.interactive() || this.disabled()) return;
    this.activated.emit(event);
  }

  @HostListener('keydown.enter', ['$event'])
  @HostListener('keydown.space', ['$event'])
  protected onActivateKey(event: Event): void {
    if (!this.interactive() || this.disabled()) return;
    event.preventDefault();
    this.activated.emit(event);
  }
}
