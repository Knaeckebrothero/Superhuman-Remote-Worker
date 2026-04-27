import {ChangeDetectionStrategy, Component, computed, inject, input} from '@angular/core';
import {AppIconComponent} from '../icon';
import {AppTooltipDirective} from '../tooltip';
import {ThemeService, type ThemePreference} from '../../core/services/theme.service';

interface ThemeOption {
  value: ThemePreference;
  icon: string;
  labelKey: string;
  fallbackLabel: string;
}

const OPTIONS: readonly ThemeOption[] = [
  {value: 'light', icon: 'light_mode', labelKey: 'theme.light', fallbackLabel: 'Light'},
  {value: 'system', icon: 'contrast', labelKey: 'theme.system', fallbackLabel: 'System'},
  {value: 'dark', icon: 'dark_mode', labelKey: 'theme.dark', fallbackLabel: 'Dark'},
] as const;

@Component({
  selector: 'app-theme-toggle',
  standalone: true,
  imports: [AppIconComponent, AppTooltipDirective],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="app-theme-toggle__group" role="radiogroup" [attr.aria-label]="ariaLabel()">
      @for (opt of options; track opt.value) {
        <button
          type="button"
          class="app-theme-toggle__btn"
          role="radio"
          [attr.aria-checked]="opt.value === preference()"
          [attr.data-active]="opt.value === preference() ? '' : null"
          [appTooltip]="opt.fallbackLabel"
          (click)="select(opt.value)"
        >
          <app-icon size="sm" aria-hidden="true">{{ opt.icon }}</app-icon>
          @if (showLabels()) {
            <span class="app-theme-toggle__label">{{ opt.fallbackLabel }}</span>
          }
        </button>
      }
    </div>
  `,
  styleUrl: './theme-toggle.component.scss',
})
export class AppThemeToggleComponent {
  showLabels = input<boolean>(false);
  ariaLabel = input<string>('Theme');

  protected readonly options = OPTIONS;
  private readonly theme = inject(ThemeService);

  protected readonly preference = computed(() => this.theme.preference());

  protected select(value: ThemePreference): void {
    this.theme.setPreference(value);
  }
}
