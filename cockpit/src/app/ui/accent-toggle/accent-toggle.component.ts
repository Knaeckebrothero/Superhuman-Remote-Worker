import {ChangeDetectionStrategy, Component, computed, inject, input} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppSelectComponent} from '../select';
import {
  ACCENT_OPTIONS,
  isAccentPreference,
  ThemeService,
} from '../../core/services/theme.service';

/**
 * Accent picker — the second appearance axis beside app-theme-toggle.
 * Tyrian (purple) is the default; Porphyry (the original red) and Graphite
 * (no hue) are the alternatives. Option labels come from
 * settings.appearance.accents.<key> so the German UI names them too.
 */
@Component({
  selector: 'app-accent-toggle',
  standalone: true,
  imports: [AppSelectComponent, TranslocoPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <app-select
      [value]="accent()"
      [ariaLabel]="ariaLabel()"
      (changed)="onSelect($event)"
    >
      @for (opt of options; track opt) {
        <option [value]="opt">{{ 'settings.appearance.accents.' + opt | transloco }}</option>
      }
    </app-select>
  `,
  styles: [':host { display: block; }'],
})
export class AppAccentToggleComponent {
  ariaLabel = input<string>('Accent');

  protected readonly options = ACCENT_OPTIONS;

  private readonly theme = inject(ThemeService);
  protected readonly accent = computed(() => this.theme.accent());

  protected onSelect(value: string | null): void {
    if (isAccentPreference(value)) this.theme.setAccent(value);
  }
}
