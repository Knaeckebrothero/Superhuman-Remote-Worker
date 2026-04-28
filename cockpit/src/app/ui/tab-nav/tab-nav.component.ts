import {
  ChangeDetectionStrategy,
  Component,
  HostListener,
  contentChildren,
  effect,
  input,
  model,
} from '@angular/core';
import {FocusKeyManager} from '@angular/cdk/a11y';
import {AppTabNavItemComponent} from './tab-nav-item.component';

export type TabNavOrientation = 'horizontal' | 'vertical';

@Component({
  selector: 'app-tab-nav',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<ng-content select="app-tab-nav-item"></ng-content>`,
  styleUrl: './tab-nav.component.scss',
  host: {
    role: 'tablist',
    '[attr.aria-orientation]': 'orientation()',
    '[attr.data-orientation]': 'orientation()',
  },
})
export class AppTabNavComponent<T = unknown> {
  value = model<T | null>(null);
  orientation = input<TabNavOrientation>('horizontal');

  protected items = contentChildren(AppTabNavItemComponent);

  private keyManager?: FocusKeyManager<AppTabNavItemComponent>;

  constructor() {
    effect(() => {
      const items = this.items();
      const orientation = this.orientation();
      if (items.length === 0) return;
      const km = new FocusKeyManager<AppTabNavItemComponent>(
        items as readonly AppTabNavItemComponent[],
      ).withWrap();
      if (orientation === 'vertical') {
        km.withVerticalOrientation();
      } else {
        km.withHorizontalOrientation('ltr');
      }
      this.keyManager = km;
    });
  }

  select(value: T): void {
    this.value.set(value);
  }

  @HostListener('keydown', ['$event'])
  protected onKeydown(event: KeyboardEvent): void {
    this.keyManager?.onKeydown(event);
  }
}
