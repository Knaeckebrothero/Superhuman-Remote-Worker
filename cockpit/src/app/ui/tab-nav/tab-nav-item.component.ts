import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  HostListener,
  computed,
  inject,
  input,
} from '@angular/core';
import {FocusableOption} from '@angular/cdk/a11y';
import {AppTabNavComponent} from './tab-nav.component';

@Component({
  selector: 'app-tab-nav-item',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<ng-content></ng-content>`,
  styleUrl: './tab-nav-item.component.scss',
  host: {
    role: 'tab',
    '[attr.aria-selected]': 'isActive()',
    '[attr.tabindex]': 'isActive() ? 0 : -1',
    '[attr.data-active]': 'isActive() || null',
    '[attr.data-disabled]': 'isDisabled() || null',
    '[attr.data-orientation]': 'orientation()',
  },
})
export class AppTabNavItemComponent<T = unknown> implements FocusableOption {
  value = input.required<T>();
  isDisabled = input<boolean>(false, {alias: 'disabled'});

  get disabled(): boolean {
    return this.isDisabled();
  }

  private nav = inject(AppTabNavComponent, {optional: true});
  private host = inject(ElementRef<HTMLElement>);

  protected isActive = computed(() => this.nav?.value() === this.value());
  protected orientation = computed(() => this.nav?.orientation() ?? 'horizontal');

  focus(): void {
    this.host.nativeElement.focus();
  }

  getLabel(): string {
    return this.host.nativeElement.textContent?.trim() ?? '';
  }

  @HostListener('click')
  protected onClick(): void {
    if (this.isDisabled()) return;
    this.nav?.select(this.value());
  }
}
