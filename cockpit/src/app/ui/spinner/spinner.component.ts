import {ChangeDetectionStrategy, Component, input} from '@angular/core';

export type SpinnerSize = 'xs' | 'sm' | 'md' | 'lg';
export type SpinnerTone = 'inherit' | 'accent' | 'muted';

@Component({
  selector: 'app-spinner',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: ``,
  styleUrl: './spinner.component.scss',
  host: {
    role: 'status',
    '[attr.aria-label]': 'ariaLabel() || null',
    '[attr.aria-live]': 'ariaLabel() ? "polite" : null',
    '[attr.data-size]': 'size()',
    '[attr.data-tone]': 'tone()',
  },
})
export class AppSpinnerComponent {
  size = input<SpinnerSize>('md');
  tone = input<SpinnerTone>('inherit');
  ariaLabel = input<string>('');
}
