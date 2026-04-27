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
import {FocusableOption} from '@angular/cdk/a11y';

export type MenuItemTone = 'default' | 'danger';

@Component({
  selector: 'app-menu-item',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<ng-content></ng-content>`,
  styleUrl: './menu-item.component.scss',
  host: {
    role: 'menuitem',
    '[attr.tabindex]': '-1',
    '[attr.aria-disabled]': 'isDisabled() || null',
    '[attr.data-tone]': 'tone()',
    '[attr.data-disabled]': 'isDisabled() || null',
  },
})
export class AppMenuItemComponent implements FocusableOption {
  tone = input<MenuItemTone>('default');
  isDisabled = input<boolean>(false, {alias: 'disabled'});

  activated = output<Event>();

  get disabled(): boolean {
    return this.isDisabled();
  }

  protected disabledAttr = computed(() => this.isDisabled() || null);

  private host = inject(ElementRef<HTMLElement>);

  focus(): void {
    this.host.nativeElement.focus();
  }

  getLabel(): string {
    return this.host.nativeElement.textContent?.trim() ?? '';
  }

  @HostListener('click', ['$event'])
  protected onClick(event: MouseEvent): void {
    if (this.isDisabled()) {
      event.stopPropagation();
      return;
    }
    this.activated.emit(event);
  }

  @HostListener('keydown.enter', ['$event'])
  @HostListener('keydown.space', ['$event'])
  protected onActivateKey(event: Event): void {
    if (this.isDisabled()) return;
    event.preventDefault();
    this.activated.emit(event);
  }
}
