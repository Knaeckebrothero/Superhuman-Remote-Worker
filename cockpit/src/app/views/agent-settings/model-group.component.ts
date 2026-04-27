import {Component, computed, inject, input, output, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {AppIconComponent} from '../../ui/icon';
import {ModelService} from '../../core/services/model.service';
import {SettingsService} from '../../core/services/settings.service';
import {readConfigPath, SettingsMode} from './agent-settings.types';
import {getReasoningOptions} from './reasoning-options';

const STORAGE_KEYS = {
  strategic: 'default_strategic_model',
  tactical: 'default_tactical_model',
  session: 'default_session_model',
} as const;

/**
 * Model settings group: preset chips, strategic/tactical model dropdowns.
 * In session mode, shows a single model dropdown instead of strategic/tactical.
 */
@Component({
  selector: 'app-model-group',
  standalone: true,
  imports: [FormsModule, TranslocoPipe, AppIconComponent],
  template: `
    <div class="settings-group">
      <div class="group-label">{{ 'agentSettings.model.group' | transloco }}</div>

      <!-- Model Preset Chips (job mode only) -->
      @if (mode() === 'job' && availablePresets().length > 0) {
        <div class="field-row preset-row">
          <label class="field-label">{{ 'agentSettings.model.preset' | transloco }}</label>
          <div class="preset-chips">
            @for (preset of availablePresets(); track preset.label) {
              <button
                type="button"
                class="preset-chip"
                [class.active]="strategicModel() === preset.strategic && tacticalModel() === preset.tactical"
                [class.unconfigured]="!preset.configured"
                (click)="applyPreset(preset)"
                [disabled]="disabled()"
                [title]="preset.configured ? '' : ('agentSettings.model.presetUnconfigured' | transloco)"
              >{{ preset.label }}</button>
            }
          </div>
        </div>
      }

      @if (mode() === 'job') {
        <!-- Strategic Model -->
        <div class="field-row" [class.modified]="strategicModel() !== null">
          <label class="field-label">{{ 'agentSettings.model.strategic' | transloco }}</label>
          <div class="field-control">
            <select
              class="form-input"
              [ngModel]="strategicModel() ?? resolvedStrategicModel()"
              (ngModelChange)="onStrategicModelChange($event)"
              [disabled]="disabled()"
            >
              <option [ngValue]="null">{{ 'agentSettings.model.default' | transloco }}</option>
              @for (group of availableModels(); track group.group) {
                <optgroup [label]="providerLabel(group)">
                  @for (model of group.models; track model) {
                    <option [value]="model">{{ model }}</option>
                  }
                </optgroup>
              }
            </select>
            @if (strategicModel() !== null) {
              <button type="button" class="reset-btn" (click)="onStrategicModelChange(null)" [title]="'agentSettings.common.resetToDefault' | transloco"><app-icon size="xs">close</app-icon></button>
            }
          </div>
        </div>

        <!-- Tactical Model -->
        <div class="field-row" [class.modified]="tacticalModel() !== null">
          <label class="field-label">{{ 'agentSettings.model.tactical' | transloco }}</label>
          <div class="field-control">
            <select
              class="form-input"
              [ngModel]="tacticalModel() ?? resolvedTacticalModel()"
              (ngModelChange)="onTacticalModelChange($event)"
              [disabled]="disabled()"
            >
              <option [ngValue]="null">{{ 'agentSettings.model.default' | transloco }}</option>
              @for (group of availableModels(); track group.group) {
                <optgroup [label]="providerLabel(group)">
                  @for (model of group.models; track model) {
                    <option [value]="model">{{ model }}</option>
                  }
                </optgroup>
              }
            </select>
            @if (tacticalModel() !== null) {
              <button type="button" class="reset-btn" (click)="onTacticalModelChange(null)" [title]="'agentSettings.common.resetToDefault' | transloco"><app-icon size="xs">close</app-icon></button>
            }
          </div>
        </div>
      } @else {
        <!-- Session: single model -->
        <div class="field-row" [class.modified]="sessionModel() !== null">
          <label class="field-label">{{ 'agentSettings.model.single' | transloco }}</label>
          <div class="field-control">
            <select
              class="form-input"
              [ngModel]="sessionModel() ?? resolvedSessionModel()"
              (ngModelChange)="onSessionModelChange($event)"
              [disabled]="disabled()"
            >
              <option [ngValue]="null">{{ 'agentSettings.model.default' | transloco }}</option>
              @for (group of availableModels(); track group.group) {
                <optgroup [label]="providerLabel(group)">
                  @for (model of group.models; track model) {
                    <option [value]="model">{{ model }}</option>
                  }
                </optgroup>
              }
            </select>
            @if (sessionModel() !== null) {
              <button type="button" class="reset-btn" (click)="onSessionModelChange(null)" [title]="'agentSettings.common.resetToDefault' | transloco"><app-icon size="xs">close</app-icon></button>
            }
          </div>
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
    .preset-row {
      border-left-color: transparent !important;
    }
    .preset-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .preset-chip {
      padding: 5px 12px;
      border: 1px solid var(--border-color, #45475a);
      border-radius: 16px;
      background: transparent;
      color: var(--text-primary, #cdd6f4);
      font-size: 12px;
      cursor: pointer;
      transition: all 0.15s;
    }
    .preset-chip:hover:not(:disabled) {
      border-color: var(--accent-color, #cba6f7);
      background: rgba(203, 166, 247, 0.08);
    }
    .preset-chip.active {
      border-color: var(--accent-color, #cba6f7);
      background: rgba(203, 166, 247, 0.15);
      color: var(--accent-color, #cba6f7);
    }
    .preset-chip:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
    .preset-chip.unconfigured {
      opacity: 0.5;
      border-style: dashed;
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
      cursor: pointer;
      flex-shrink: 0;
    }
    .reset-btn:hover {
      background: var(--danger-tint);
      color: var(--danger);
    }
  `],
})
export class ModelGroupComponent {
  private readonly modelService = inject(ModelService);
  private readonly settingsService = inject(SettingsService);
  private readonly transloco = inject(TranslocoService);

  providerLabel(group: {group: string; configured: boolean}): string {
    return group.configured
      ? group.group
      : this.transloco.translate('agentSettings.model.providerNoKey', {provider: group.group});
  }

  config = input<Record<string, unknown>>({});
  mode = input<SettingsMode>('job');
  disabled = input(false);

  change = output<void>();

  readonly availableModels = this.modelService.models;
  readonly availablePresets = this.modelService.presets;

  // Job mode: per-phase overrides
  readonly strategicModel = signal<string | null>(null);
  readonly tacticalModel = signal<string | null>(null);

  // Session mode: single model override
  readonly sessionModel = signal<string | null>(null);

  // Resolved defaults
  readonly resolvedStrategicModel = computed(() =>
    (readConfigPath(this.config(), 'llm.strategic.model') as string)
    ?? (readConfigPath(this.config(), 'llm.model') as string)
    ?? null
  );
  readonly resolvedTacticalModel = computed(() =>
    (readConfigPath(this.config(), 'llm.tactical.model') as string)
    ?? (readConfigPath(this.config(), 'llm.model') as string)
    ?? null
  );
  readonly resolvedSessionModel = computed(() =>
    (readConfigPath(this.config(), 'llm.model') as string) ?? null
  );

  /** Reasoning options for the currently selected strategic model. */
  readonly strategicReasoningOptions = computed(() =>
    getReasoningOptions(this.strategicModel() ?? this.resolvedStrategicModel())
  );
  /** Reasoning options for the currently selected tactical model. */
  readonly tacticalReasoningOptions = computed(() =>
    getReasoningOptions(this.tacticalModel() ?? this.resolvedTacticalModel())
  );
  /** Reasoning options for the session model. */
  readonly sessionReasoningOptions = computed(() =>
    getReasoningOptions(this.sessionModel() ?? this.resolvedSessionModel())
  );

  readonly modifiedCount = computed(() => {
    let count = 0;
    if (this.mode() === 'job') {
      if (this.strategicModel() !== null) count++;
      if (this.tacticalModel() !== null) count++;
    } else {
      if (this.sessionModel() !== null) count++;
    }
    return count;
  });

  applyPreset(preset: { label: string; strategic: string; tactical: string }): void {
    this.strategicModel.set(preset.strategic);
    this.tacticalModel.set(preset.tactical);
    this.persistModel('strategic', preset.strategic);
    this.persistModel('tactical', preset.tactical);
    this.change.emit();
  }

  onStrategicModelChange(value: string | null): void {
    const resolved = value === this.resolvedStrategicModel() ? null : value;
    this.strategicModel.set(resolved);
    this.persistModel('strategic', resolved);
    this.change.emit();
  }

  onTacticalModelChange(value: string | null): void {
    const resolved = value === this.resolvedTacticalModel() ? null : value;
    this.tacticalModel.set(resolved);
    this.persistModel('tactical', resolved);
    this.change.emit();
  }

  onSessionModelChange(value: string | null): void {
    const resolved = value === this.resolvedSessionModel() ? null : value;
    this.sessionModel.set(resolved);
    this.persistModel('session', resolved);
    this.change.emit();
  }

  /** Build the model-related config_override fragment. */
  getOverrides(): Record<string, unknown> {
    const llm: Record<string, unknown> = {};

    if (this.mode() === 'job') {
      const sm = this.strategicModel();
      if (sm) llm['strategic'] = { ...(llm['strategic'] as any ?? {}), model: sm };
      const tm = this.tacticalModel();
      if (tm) llm['tactical'] = { ...(llm['tactical'] as any ?? {}), model: tm };
    } else {
      const m = this.sessionModel();
      if (m) llm['model'] = m;
    }

    return Object.keys(llm).length > 0 ? { llm } : {};
  }

  resetAll(): void {
    this.strategicModel.set(null);
    this.tacticalModel.set(null);
    this.sessionModel.set(null);
  }

  /** Prefill from expert config (called by parent when expert changes).
   *  Falls back to saved user preferences when config doesn't specify a model. */
  prefillFromConfig(config: Record<string, unknown>): void {
    const llm = config['llm'] as Record<string, unknown> | undefined;
    const strat = llm?.['strategic'] as Record<string, unknown> | undefined;
    const tact = llm?.['tactical'] as Record<string, unknown> | undefined;
    const baseModel = (llm?.['model'] as string) ?? null;

    this.strategicModel.set(
      (strat?.['model'] as string) ?? baseModel ?? this.loadSavedModel('strategic'),
    );
    this.tacticalModel.set(
      (tact?.['model'] as string) ?? baseModel ?? this.loadSavedModel('tactical'),
    );
    this.sessionModel.set(baseModel ?? this.loadSavedModel('session'));
  }

  private loadSavedModel(key: keyof typeof STORAGE_KEYS): string | null {
    try {
      return localStorage.getItem(STORAGE_KEYS[key]) ?? null;
    } catch {
      return null;
    }
  }

  private persistModel(key: keyof typeof STORAGE_KEYS, value: string | null): void {
    const storageKey = STORAGE_KEYS[key];
    const settingsKey = `default_${key}_model` as const;
    try {
      if (value) {
        localStorage.setItem(storageKey, value);
      } else {
        localStorage.removeItem(storageKey);
      }
    } catch {
      // localStorage may be unavailable
    }
    this.settingsService.updatePreferences({ [settingsKey]: value ?? null }).subscribe();
  }
}
