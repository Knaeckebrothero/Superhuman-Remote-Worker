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
import {AppRadioComponent} from './radio.component';

export type RadioGroupOrientation = 'horizontal' | 'vertical';

let nextRadioGroupId = 0;

@Component({
  selector: 'app-radio-group',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<ng-content select="app-radio"></ng-content>`,
  styleUrl: './radio-group.component.scss',
  host: {
    role: 'radiogroup',
    '[attr.aria-orientation]': 'orientation()',
    '[attr.data-orientation]': 'orientation()',
  },
})
export class AppRadioGroupComponent<T = unknown> {
  value = model<T | null>(null);
  orientation = input<RadioGroupOrientation>('horizontal');
  disabled = input<boolean>(false);

  readonly name = `app-radio-group-${++nextRadioGroupId}`;

  readonly items = contentChildren(AppRadioComponent);
  private keyManager?: FocusKeyManager<AppRadioComponent>;

  constructor() {
    effect(() => {
      const items = this.items();
      const orientation = this.orientation();
      if (items.length === 0) return;
      const km = new FocusKeyManager<AppRadioComponent>(
        items as readonly AppRadioComponent[],
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
