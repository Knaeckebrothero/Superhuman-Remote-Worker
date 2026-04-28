import {
  ChangeDetectionStrategy,
  Component,
  input,
} from '@angular/core';

export type BadgeTone =
  | 'neutral'
  | 'accent'
  | 'success'
  | 'warning'
  | 'alert'
  | 'info'
  | 'danger';
export type BadgeAppearance = 'subtle' | 'solid';
export type BadgeSize = 'xs' | 'sm' | 'md';
export type BadgeShape = 'rounded' | 'pill';

@Component({
  selector: 'app-badge',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<ng-content></ng-content>`,
  styleUrl: './badge.component.scss',
  host: {
    '[attr.data-tone]': 'tone()',
    '[attr.data-appearance]': 'appearance()',
    '[attr.data-size]': 'size()',
    '[attr.data-shape]': 'shape()',
    '[attr.data-uppercase]': 'uppercase() || null',
  },
})
export class AppBadgeComponent {
  tone = input<BadgeTone>('neutral');
  appearance = input<BadgeAppearance>('subtle');
  size = input<BadgeSize>('sm');
  shape = input<BadgeShape>('rounded');
  uppercase = input<boolean>(false);
}
