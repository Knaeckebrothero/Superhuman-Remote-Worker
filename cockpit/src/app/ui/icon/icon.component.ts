import {ChangeDetectionStrategy, Component, computed, input} from '@angular/core';

export type IconSize = 'xs' | 'sm' | 'md' | 'lg' | 'xl' | 'inherit';

@Component({
  selector: 'app-icon',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<ng-content></ng-content>`,
  styleUrl: './icon.component.scss',
  host: {
    '[attr.data-size]': 'size()',
    '[attr.data-filled]': 'filled() || null',
    '[attr.aria-hidden]': 'ariaHidden()',
    '[attr.aria-label]': 'ariaLabel() || null',
    '[attr.role]': 'ariaLabel() ? "img" : null',
  },
})
export class AppIconComponent {
  size = input<IconSize>('md');
  filled = input<boolean>(false);
  ariaLabel = input<string>('');

  protected ariaHidden = computed(() => (this.ariaLabel() ? null : 'true'));
}
