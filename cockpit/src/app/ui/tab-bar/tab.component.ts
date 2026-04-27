import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  HostListener,
  computed,
  inject,
  input,
} from '@angular/core';
import { FocusableOption } from '@angular/cdk/a11y';
import { AppTabBarComponent } from './tab-bar.component';

@Component({
  selector: 'app-tab',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<ng-content></ng-content>`,
  styleUrl: './tab.component.scss',
  host: {
    'role': 'tab',
    '[attr.aria-selected]': 'isActive()',
    '[attr.tabindex]': 'isActive() ? 0 : -1',
    '[attr.data-active]': 'isActive() || null',
    '[attr.data-disabled]': 'isDisabled() || null',
  },
})
export class AppTabComponent<T = unknown> implements FocusableOption {
  value = input.required<T>();

  // Aliased: template binding [disabled] still works; internal name avoids
  // clashing with the `disabled` getter required by FocusableOption.
  isDisabled = input<boolean>(false, { alias: 'disabled' });

  // FocusableOption contract — FocusKeyManager reads this to skip disabled tabs.
  get disabled(): boolean {
    return this.isDisabled();
  }

  private bar = inject(AppTabBarComponent, { optional: true });
  private host = inject(ElementRef<HTMLElement>);

  protected isActive = computed(() => this.bar?.value() === this.value());

  focus() {
    this.host.nativeElement.focus();
  }

  getLabel(): string {
    return this.host.nativeElement.textContent?.trim() ?? '';
  }

  @HostListener('click')
  protected onClick() {
    if (this.isDisabled()) return;
    this.bar?.select(this.value());
  }
}
