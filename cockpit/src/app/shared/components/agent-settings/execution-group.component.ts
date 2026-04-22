import {Component, computed, inject, input, output, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {toSignal} from '@angular/core/rxjs-interop';
import {
  AUTONOMY_LEVELS,
  CRITIC_ROUND_OPTIONS,
  PERMISSION_MODES,
  readConfigPath,
  SettingsMode,
} from './agent-settings.types';

/**
 * Execution settings group: autonomy, scholar, critic, project memory.
 * In session mode, shows permission mode instead of autonomy.
 */
@Component({
  selector: 'app-execution-group',
  standalone: true,
  imports: [FormsModule, TranslocoPipe],
  template: `
    <div class="settings-group">
      <div class="group-label">{{ 'agentSettings.execution.group' | transloco }}</div>

      <!-- Autonomy (job) or Permission Mode (session) -->
      @if (mode() === 'job') {
        <div class="field-row" [class.modified]="autonomy() !== null">
          <label class="field-label">{{ 'agentSettings.execution.autonomy' | transloco }}</label>
          <div class="field-control">
            <select
              class="form-input"
              [ngModel]="autonomy() ?? resolvedAutonomy()"
              (ngModelChange)="onAutonomyChange($event)"
              [disabled]="disabled()"
            >
              @for (level of autonomyLevels; track level.value) {
                <option [value]="level.value">{{ 'agentSettings.autonomy.' + level.value + '.label' | transloco }}</option>
              }
            </select>
            @if (autonomy() !== null) {
              <button type="button" class="reset-btn" (click)="autonomy.set(null)" [title]="'agentSettings.common.resetToDefault' | transloco">
                close
              </button>
            }
          </div>
          <span class="field-hint">{{ effectiveAutonomyDesc() }}</span>
        </div>
      } @else {
        <div class="field-row" [class.modified]="permissionMode() !== null">
          <label class="field-label">{{ 'agentSettings.execution.permissionMode' | transloco }}</label>
          <div class="field-control">
            <select
              class="form-input"
              [ngModel]="permissionMode() ?? resolvedPermissionMode()"
              (ngModelChange)="onPermissionModeChange($event)"
              [disabled]="disabled()"
            >
              @for (pm of permissionModes; track pm.value) {
                <option [value]="pm.value">{{ 'agentSettings.permissionModes.' + pm.value + '.label' | transloco }}</option>
              }
            </select>
            @if (permissionMode() !== null) {
              <button type="button" class="reset-btn" (click)="permissionMode.set(null)" [title]="'agentSettings.common.resetToDefault' | transloco">
                close
              </button>
            }
          </div>
          <span class="field-hint">{{ effectivePermissionDesc() }}</span>
        </div>
      }

      <!-- Scholar toggle -->
      @if (mode() === 'job') {
        <div class="field-row toggle-row" [class.modified]="scholar() !== null">
          <label class="toggle-label">
            <input
              type="checkbox"
              [checked]="scholar() ?? resolvedScholar()"
              (change)="onScholarChange($event)"
              [disabled]="disabled()"
            >
            <span>{{ 'agentSettings.execution.scholar' | transloco }}</span>
          </label>
          @if (scholar() !== null) {
            <button type="button" class="reset-btn" (click)="scholar.set(null)" [title]="'agentSettings.common.resetToDefault' | transloco">
              close
            </button>
          }
        </div>
      }

      <!-- Critic toggle + rounds -->
      @if (mode() === 'job') {
        <div class="field-row toggle-row" [class.modified]="critic() !== null || criticRounds() !== null">
          <label class="toggle-label">
            <input
              type="checkbox"
              [checked]="critic() ?? resolvedCritic()"
              (change)="onCriticChange($event)"
              [disabled]="disabled()"
            >
            <span>{{ 'agentSettings.execution.critic' | transloco }}</span>
          </label>
          @if (effectiveCritic()) {
            <select
              class="form-input compact-select"
              [ngModel]="criticRounds() ?? resolvedCriticRounds()"
              (ngModelChange)="onCriticRoundsChange($event)"
              [disabled]="disabled()"
            >
              @for (opt of criticRoundOptions; track opt.value) {
                <option [ngValue]="opt.value">{{ 'agentSettings.criticRounds.' + opt.value | transloco }}</option>
              }
            </select>
          }
          @if (critic() !== null || criticRounds() !== null) {
            <button type="button" class="reset-btn" (click)="resetCritic()" [title]="'agentSettings.common.resetToDefault' | transloco">
              close
            </button>
          }
        </div>
      }

      <!-- Project memory toggle -->
      @if (mode() === 'job' && showProjectMemory()) {
        <div class="field-row toggle-row" [class.modified]="projectMemory() !== null">
          <label class="toggle-label">
            <input
              type="checkbox"
              [checked]="projectMemory() ?? resolvedProjectMemory()"
              (change)="onProjectMemoryChange($event)"
              [disabled]="disabled()"
            >
            <span>{{ 'agentSettings.execution.projectMemory' | transloco }}</span>
          </label>
          @if (projectMemory() !== null) {
            <button type="button" class="reset-btn" (click)="projectMemory.set(null)" [title]="'agentSettings.common.resetToDefault' | transloco">
              close
            </button>
          }
        </div>
      }
    </div>
  `,
  styles: [`
    .settings-group {
      margin-bottom: 20px;
    }
    .group-label {
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted, #6c7086);
      margin-bottom: 12px;
      padding-bottom: 6px;
      border-bottom: 1px solid var(--border-color, #313244);
    }
    .field-row {
      margin-bottom: 12px;
      padding-left: 8px;
      border-left: 2px solid transparent;
      transition: border-color 0.15s;
    }
    .field-row.modified {
      border-left-color: var(--accent-color, #cba6f7);
    }
    .field-label {
      display: block;
      font-size: 12px;
      font-weight: 500;
      color: var(--text-primary, #cdd6f4);
      margin-bottom: 4px;
    }
    .field-control {
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .form-input {
      flex: 1;
      padding: 7px 10px;
      border: 1px solid var(--border-color, #45475a);
      border-radius: 6px;
      background: var(--surface-0, #313244);
      color: var(--text-primary, #cdd6f4);
      font-family: inherit;
      font-size: 13px;
    }
    .form-input:focus {
      outline: none;
      border-color: var(--accent-color, #cba6f7);
    }
    .form-input:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .compact-select {
      width: auto;
      min-width: 120px;
      padding: 4px 8px;
      font-size: 12px;
      margin-left: 8px;
    }
    .toggle-row {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .toggle-label {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 13px;
      color: var(--text-primary, #cdd6f4);
      cursor: pointer;
    }
    .toggle-label input[type="checkbox"] {
      accent-color: var(--accent-color, #cba6f7);
    }
    .reset-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 20px;
      height: 20px;
      border: none;
      border-radius: 50%;
      background: rgba(255, 255, 255, 0.08);
      color: var(--text-muted, #6c7086);
      font-family: 'Material Symbols Outlined', sans-serif;
      font-size: 14px;
      cursor: pointer;
      flex-shrink: 0;
    }
    .reset-btn:hover {
      background: rgba(243, 139, 168, 0.2);
      color: #f38ba8;
    }
    .field-hint {
      display: block;
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      margin-top: 2px;
    }
  `],
})
export class ExecutionGroupComponent {
  private readonly transloco = inject(TranslocoService);
  private readonly activeLang = toSignal(this.transloco.langChanges$, {
    initialValue: this.transloco.getActiveLang(),
  });

  /** Merged expert/framework config. */
  config = input<Record<string, unknown>>({});
  mode = input<SettingsMode>('job');
  disabled = input(false);
  /** Whether the selected project has shared memory enabled. */
  showProjectMemory = input(false);

  change = output<void>();

  // Constants
  readonly autonomyLevels = AUTONOMY_LEVELS;
  readonly criticRoundOptions = CRITIC_ROUND_OPTIONS;
  readonly permissionModes = PERMISSION_MODES;

  // User overrides (null = use default)
  readonly autonomy = signal<string | null>(null);
  readonly permissionMode = signal<string | null>(null);
  readonly scholar = signal<boolean | null>(null);
  readonly critic = signal<boolean | null>(null);
  readonly criticRounds = signal<number | null>(null);
  readonly projectMemory = signal<boolean | null>(null);

  // Resolved defaults from config
  readonly resolvedAutonomy = computed(() =>
    (readConfigPath(this.config(), 'autonomy') as string) ?? 'review'
  );
  readonly resolvedPermissionMode = computed(() =>
    (readConfigPath(this.config(), 'interactive.permission_mode') as string) ?? 'supervised'
  );
  readonly resolvedScholar = computed(() =>
    (readConfigPath(this.config(), 'scholar.enabled') as boolean) ?? true
  );
  readonly resolvedCritic = computed(() =>
    (readConfigPath(this.config(), 'verification.enabled') as boolean) ?? true
  );
  readonly resolvedCriticRounds = computed(() =>
    (readConfigPath(this.config(), 'verification.max_rounds') as number) ?? 5
  );
  readonly resolvedProjectMemory = computed(() =>
    (readConfigPath(this.config(), 'memory.project_scoped') as boolean) ?? true
  );

  readonly effectiveAutonomyDesc = computed(() => {
    this.activeLang();
    const val = this.autonomy() ?? this.resolvedAutonomy();
    const known = this.autonomyLevels.find(l => l.value === val);
    if (!known) return this.transloco.translate('agentSettings.execution.autonomyDefaultHint');
    return this.transloco.translate(`agentSettings.autonomy.${val}.description`);
  });

  readonly effectivePermissionDesc = computed(() => {
    this.activeLang();
    const val = this.permissionMode() ?? this.resolvedPermissionMode();
    const known = this.permissionModes.find(p => p.value === val);
    if (!known) return '';
    return this.transloco.translate(`agentSettings.permissionModes.${val}.description`);
  });

  readonly effectiveCritic = computed(() =>
    this.critic() ?? this.resolvedCritic()
  );

  /** Number of fields that differ from defaults. */
  readonly modifiedCount = computed(() => {
    let count = 0;
    if (this.autonomy() !== null) count++;
    if (this.permissionMode() !== null) count++;
    if (this.scholar() !== null) count++;
    if (this.critic() !== null) count++;
    if (this.criticRounds() !== null) count++;
    if (this.projectMemory() !== null) count++;
    return count;
  });

  onAutonomyChange(value: string): void {
    this.autonomy.set(value === this.resolvedAutonomy() ? null : value);
    this.change.emit();
  }

  onPermissionModeChange(value: string): void {
    this.permissionMode.set(value === this.resolvedPermissionMode() ? null : value);
    this.change.emit();
  }

  onScholarChange(event: Event): void {
    const checked = (event.target as HTMLInputElement).checked;
    this.scholar.set(checked === this.resolvedScholar() ? null : checked);
    this.change.emit();
  }

  onCriticChange(event: Event): void {
    const checked = (event.target as HTMLInputElement).checked;
    this.critic.set(checked === this.resolvedCritic() ? null : checked);
    this.change.emit();
  }

  onCriticRoundsChange(value: number): void {
    this.criticRounds.set(value === this.resolvedCriticRounds() ? null : value);
    this.change.emit();
  }

  onProjectMemoryChange(event: Event): void {
    const checked = (event.target as HTMLInputElement).checked;
    this.projectMemory.set(checked === this.resolvedProjectMemory() ? null : checked);
    this.change.emit();
  }

  resetCritic(): void {
    this.critic.set(null);
    this.criticRounds.set(null);
    this.change.emit();
  }

  /** Build the execution-related config_override fragment. */
  getOverrides(): Record<string, unknown> {
    const o: Record<string, unknown> = {};

    if (this.mode() === 'job') {
      if (this.autonomy() !== null) o['autonomy'] = this.autonomy();
      const s = this.scholar();
      if (s !== null) o['scholar'] = { enabled: s };
      const c = this.critic();
      const r = this.criticRounds();
      if (c !== null || r !== null) {
        o['verification'] = {
          enabled: c ?? this.resolvedCritic(),
          max_rounds: r ?? this.resolvedCriticRounds(),
        };
      }
      const pm = this.projectMemory();
      if (pm !== null) o['memory'] = { project_scoped: pm };
    } else {
      if (this.permissionMode() !== null) {
        o['interactive'] = { permission_mode: this.permissionMode() };
      }
    }

    return o;
  }

  /** Reset all fields to defaults. */
  resetAll(): void {
    this.autonomy.set(null);
    this.permissionMode.set(null);
    this.scholar.set(null);
    this.critic.set(null);
    this.criticRounds.set(null);
    this.projectMemory.set(null);
  }
}
