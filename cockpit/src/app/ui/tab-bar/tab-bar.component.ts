import {
  AfterContentInit,
  ChangeDetectionStrategy,
  Component,
  HostListener,
  contentChildren,
  effect,
  model,
} from '@angular/core';
import { FocusKeyManager } from '@angular/cdk/a11y';
import { AppTabComponent } from './tab.component';

@Component({
  selector: 'app-tab-bar',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<ng-content select="app-tab"></ng-content>`,
  styleUrl: './tab-bar.component.scss',
  host: {
    'role': 'tablist',
  },
})
export class AppTabBarComponent<T = unknown> implements AfterContentInit {
  value = model<T | null>(null);

  protected tabs = contentChildren(AppTabComponent);

  private keyManager?: FocusKeyManager<AppTabComponent>;

  constructor() {
    effect(() => {
      // Re-create the key manager whenever the tab set changes so newly
      // added tabs are immediately keyboard-reachable.
      const tabs = this.tabs();
      if (tabs.length === 0) return;
      this.keyManager = new FocusKeyManager<AppTabComponent>(tabs as readonly AppTabComponent[])
        .withHorizontalOrientation('ltr')
        .withWrap();
    });
  }

  ngAfterContentInit() {
    // No-op — effect above handles initial setup.
  }

  select(value: T) {
    this.value.set(value);
  }

  @HostListener('keydown', ['$event'])
  protected onKeydown(event: KeyboardEvent) {
    this.keyManager?.onKeydown(event);
  }
}
