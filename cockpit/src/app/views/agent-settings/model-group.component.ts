import {Component, computed, inject, input, output, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {AppIconComponent} from '../../ui/icon';
import {ModelService} from '../../core/services/model.service';
import {SettingsService} from '../../core/services/settings.service';
import {computeModelMismatch, ModelMismatch, readConfigPath, SettingsMode} from './agent-settings.types';
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

      <!-- Strategic+tactical preset chips were removed in chunk 7 of
           models_yaml_removal — the legacy presets block in models.yaml
           has no DB-backed successor. The catalog carries enough
           metadata to rebuild presets if demand surfaces. -->

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

        <!-- Phase-model mismatch advisory: the two models share one context
             history, so the budget collapses to the smaller window and image
             support to the AND (mirrors backend resolve_phase_model_budget).
             Only shown when settings actually differ. -->
        @if (modelMismatch(); as mm) {
          <div class="model-mismatch" [class.prominent]="mm.prominent">
            <app-icon size="xs">warning</app-icon>
            <div class="mismatch-text">
              @if (mm.window) {
                <div>{{ 'agentSettings.model.mismatchWindow' | transloco: { min: fmtTokens(mm.window.min) } }}</div>
              }
              @if (mm.multimodal) {
                <div>{{ 'agentSettings.model.mismatchMultimodal' | transloco }}</div>
              }
            </div>
          </div>
        }
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
      border-bottom: 1px solid var(--border-color, var(--surface-0));
    }
    .field-row {
      margin-bottom: 12px;
      padding-left: 8px;
      border-left: 2px solid transparent;
      transition: border-color 0.15s;
    }
    .field-row.modified {
      border-left-color: var(--accent-color, var(--accent-color));
    }
    .field-label {
      display: block;
      font-size: 12px;
      font-weight: 500;
      color: var(--text-primary, var(--text-primary));
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
      border: 1px solid var(--border-color, var(--surface-1));
      border-radius: var(--radius-control);
      background: var(--surface-0, var(--surface-0));
      color: var(--text-primary, var(--text-primary));
      font-family: inherit;
      font-size: 13px;
    }
    .form-input:focus {
      outline: none;
      border-color: var(--accent-color, var(--accent-color));
    }
    .form-input:disabled {
      opacity: 0.6;
      cursor: not-allowed;
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
    .model-mismatch {
      display: flex;
      gap: 8px;
      align-items: flex-start;
      margin: 4px 0 12px 8px;
      padding: 8px 10px;
      border-radius: var(--radius-control);
      background: var(--surface-1, rgba(255, 255, 255, 0.04));
      border-left: 2px solid var(--warning, #f5a623);
      color: var(--text-secondary, #a6adc8);
      font-size: 12px;
      line-height: 1.4;
    }
    .model-mismatch.prominent {
      background: var(--warning-tint, rgba(245, 166, 35, 0.12));
      color: var(--text-primary, var(--text-primary));
    }
    .model-mismatch app-icon {
      color: var(--warning, #f5a623);
      flex-shrink: 0;
      margin-top: 1px;
    }
    .mismatch-text > div + div {
      margin-top: 4px;
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
  /** Raw settings_matrix (family → window/multimodal/params) for the mismatch hint. */
  settingsMatrix = input<Record<string, Record<string, unknown>>>({});

  change = output<void>();

  readonly availableModels = this.modelService.models;

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

  /**
   * Advisory: do the two phase models' family settings differ enough to matter?
   * Mirrors the backend's min-window / multimodal-AND reconciliation
   * (`resolve_phase_model_budget`). Job mode only; `null` when they agree.
   */
  readonly modelMismatch = computed<ModelMismatch | null>(() => {
    if (this.mode() !== 'job') return null;
    const strat = this.strategicModel() ?? this.resolvedStrategicModel();
    const tact = this.tacticalModel() ?? this.resolvedTacticalModel();
    return computeModelMismatch(this.settingsMatrix(), strat, tact);
  });

  /** Compact token-count label, e.g. 131072 → "131k", 1050000 → "1.05M". */
  fmtTokens(n: number): string {
    if (n >= 1_000_000) {
      const m = n / 1_000_000;
      return (Number.isInteger(m) ? String(m) : m.toFixed(2).replace(/\.?0+$/, '')) + 'M';
    }
    if (n >= 1000) return Math.round(n / 1000) + 'k';
    return String(n);
  }

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
      strat?.['model'] || baseModel ? null : this.loadSavedModel('strategic'),
    );
    this.tacticalModel.set(
      tact?.['model'] || baseModel ? null : this.loadSavedModel('tactical'),
    );
    this.sessionModel.set(baseModel ? null : this.loadSavedModel('session'));
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
