import {ChangeDetectionStrategy, Component, computed, inject, input} from '@angular/core';
import {AppIconComponent} from '../icon';
import {ThemeService, type ThemePreference} from '../../core/services/theme.service';

interface ThemeOption {
  value: ThemePreference;
  label: string;
  group: 'light' | 'dark';
}

// 'system' is rendered separately as a top-level option above the optgroups —
// it's a meta-choice ("follow OS"), not a member of either Light or Dark.
// Pattern follows shadcn/Slack/Discord: System as a peer to the theme groups.
const LIGHT_OPTIONS: readonly ThemeOption[] = [
  {value: 'travertine', label: 'Travertine', group: 'light'},
] as const;

const DARK_OPTIONS: readonly ThemeOption[] = [
  {value: 'senate',     label: 'Senate',     group: 'dark'},
  {value: 'praetorian', label: 'Praetorian', group: 'dark'},
] as const;

@Component({
  selector: 'app-theme-toggle',
  standalone: true,
  imports: [AppIconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <label class="app-theme-toggle" [attr.aria-label]="ariaLabel()">
      <select
        class="app-theme-toggle__select"
        [value]="preference()"
        [attr.aria-label]="ariaLabel()"
        (change)="onChange($event)"
      >
        <option value="system" [selected]="preference() === 'system'">System</option>
        <optgroup label="Light">
          @for (opt of lightOptions; track opt.value) {
            <option [value]="opt.value" [selected]="opt.value === preference()">{{ opt.label }}</option>
          }
        </optgroup>
        <optgroup label="Dark">
          @for (opt of darkOptions; track opt.value) {
            <option [value]="opt.value" [selected]="opt.value === preference()">{{ opt.label }}</option>
          }
        </optgroup>
      </select>
      <app-icon class="app-theme-toggle__chevron" size="sm" aria-hidden="true">expand_more</app-icon>
    </label>
  `,
  styleUrl: './theme-toggle.component.scss',
})
export class AppThemeToggleComponent {
  showLabels = input<boolean>(false);
  ariaLabel = input<string>('Theme');

  protected readonly lightOptions = LIGHT_OPTIONS;
  protected readonly darkOptions = DARK_OPTIONS;

  private readonly theme = inject(ThemeService);
  protected readonly preference = computed(() => this.theme.preference());

  protected onChange(event: Event): void {
    const value = (event.target as HTMLSelectElement).value as ThemePreference;
    this.theme.setPreference(value);
  }
}
