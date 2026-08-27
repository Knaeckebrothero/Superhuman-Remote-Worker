import {Component, computed, inject, input, output, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {AppIconComponent} from '../../ui/icon';
import {ModelService} from '../../core/services/model.service';
import {EffectiveModels, EffectiveModelSlot} from '../../core/models/api.model';
import {computeModelMismatch, ModelMismatch, pinResolvedValue, readConfigPath, SettingsMode} from './agent-settings.types';
import {PinOnInteractDirective} from './pin-on-interact.directive';
import {defaultSelectableReasoning, getSelectableReasoningOptions, reasoningOptionsForModel} from './reasoning-options';
import {formatTokens} from '../../core/util/format-tokens';

// UI-only "last selected model" preselect keys (localStorage). Deliberately NOT
// the account-settings keys (`default_*_model`): a per-job/session control must
// not write a global account preference. See Layer 2 in the issue doc
// loop_ran_codex_spark_not_selected_model_then_hung_on_cooldown.md.
const STORAGE_KEYS = {
  strategic: 'ui.lastModel.strategic',
  tactical: 'ui.lastModel.tactical',
  subagent: 'ui.lastModel.subagent',
  session: 'ui.lastModel.session',
} as const;

/**
 * Model settings group: preset chips, strategic/tactical model dropdowns.
 * In session mode, shows a single model dropdown instead of strategic/tactical.
 */
@Component({
  selector: 'app-model-group',
  standalone: true,
  imports: [FormsModule, TranslocoPipe, AppIconComponent, PinOnInteractDirective],
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
              [ngModel]="strategicModel()"
              (ngModelChange)="onStrategicModelChange($event)"
              [disabled]="disabled()"
            >
              <option [ngValue]="null">{{ defaultLabel(effectiveStrategic(), resolvedStrategicModel()) }}</option>
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
              [ngModel]="tacticalModel()"
              (ngModelChange)="onTacticalModelChange($event)"
              [disabled]="disabled()"
            >
              <option [ngValue]="null">{{ defaultLabel(effectiveTactical(), resolvedTacticalModel()) }}</option>
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

        @if (showSubagent()) {
          <!-- Subagent Model — the reader model spawn_subagent delegates to.
               Runs in fresh reader windows (separate from strategic/tactical's
               shared history), so it's outside the mismatch advisory above.
               Resolves subagent → tactical → base, mirroring the agent. -->
          <div class="field-row" [class.modified]="subagentModel() !== null">
            <label class="field-label">{{ 'agentSettings.model.subagent' | transloco }}</label>
            <div class="field-control">
              <select
                class="form-input"
                [ngModel]="subagentModel()"
                (ngModelChange)="onSubagentModelChange($event)"
                [disabled]="disabled()"
              >
                <option [ngValue]="null">{{ defaultLabel(effectiveSubagent(), resolvedSubagentModel()) }}</option>
                @for (group of availableModels(); track group.group) {
                  <optgroup [label]="providerLabel(group)">
                    @for (model of group.models; track model) {
                      <option [value]="model">{{ model }}</option>
                    }
                  </optgroup>
                }
              </select>
              @if (subagentModel() !== null) {
                <button type="button" class="reset-btn" (click)="onSubagentModelChange(null)" [title]="'agentSettings.common.resetToDefault' | transloco"><app-icon size="xs">close</app-icon></button>
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
              [ngModel]="sessionModel()"
              (ngModelChange)="onSessionModelChange($event)"
              [disabled]="disabled()"
            >
              <option [ngValue]="null">{{ defaultLabel(effectiveSession(), resolvedSessionModel()) }}</option>
              @for (group of availableModels(); track group.group) {
                <optgroup [label]="providerLabel(group)">
                  @for (model of group.models; track model) {
                    <option [value]="model">{{ model }}</option>
                  }
                </optgroup>
              }
            </select>
            @if (sessionModel() !== null && mode() !== 'live') {
              <button type="button" class="reset-btn" (click)="onSessionModelChange(null)" [title]="'agentSettings.common.resetToDefault' | transloco"><app-icon size="xs">close</app-icon></button>
            }
          </div>
        </div>

        <!-- Live sessions: temperature (creation forms leave it to Advanced /
             the family matrix; live mode surfaces it because it has always
             been runtime-mutable and the retired header popover exposed it) -->
        @if (mode() === 'live') {
          <div class="field-row" [class.modified]="temperature() !== null">
            <label class="field-label">
              {{ 'agentSettings.model.temperature' | transloco }}
              <span class="temp-value">{{ temperature() ?? resolvedTemperature() }}</span>
            </label>
            <div class="field-control">
              <input type="range" class="form-input temp-slider" min="0" max="2" step="0.1"
                     [ngModel]="temperature() ?? resolvedTemperature()"
                     (ngModelChange)="onTemperatureChange($event)"
                     [disabled]="disabled()">
            </div>
          </div>
        }

        <!-- Session: reasoning level for the model in effect (options are
             capability-driven per family; the value rides llm.reasoning_level).
             No "Default" sentinel — the resolved default is preselected and
             marked, like the model picker; hidden when the model offers no
             choice (method none / always_on). -->
        @if (sessionReasoningOptions().length) {
          <div class="field-row" [class.modified]="sessionReasoning() !== null">
            <label class="field-label">{{ 'agentSettings.model.reasoning' | transloco }}</label>
            <div class="field-control">
              <select
                class="form-input"
                [ngModel]="sessionReasoning() ?? resolvedSessionReasoning()"
              appPinOnInteract (pin)="pinReasoning()"
                (ngModelChange)="onSessionReasoningChange($event)"
                [disabled]="disabled()"
              >
                @for (opt of sessionReasoningOptions(); track opt.value) {
                  <option [value]="opt.value">
                    {{ opt.value === resolvedSessionReasoning()
                      ? ('agentSettings.model.reasoningDefaultOption' | transloco: {label: opt.label})
                      : opt.label }}
                  </option>
                }
              </select>
              @if (sessionReasoning() !== null && mode() !== 'live') {
                <button type="button" class="reset-btn" (click)="onSessionReasoningChange(null)" [title]="'agentSettings.common.resetToDefault' | transloco"><app-icon size="xs">close</app-icon></button>
              }
            </div>
            <!-- task-3: the reset stays (per-family vocabularies don't
                 translate) but must no longer be silent — the select alone
                 looks identical to "never touched", and gpt-5.6-sol's default
                 (High) reads a lot like a forgotten Max at a glance. -->
            @if (reasoningResetNotice()) {
              <div class="reasoning-reset-notice">
                <app-icon size="xs">info</app-icon>
                <span>{{ 'agentSettings.model.reasoningResetNotice' | transloco }}</span>
              </div>
            }
          </div>
        }
      }
    </div>
  `,
  styles: [`
    .settings-group {
      margin-bottom: 20px;
    }
    .temp-value {
      font-weight: 400;
      color: var(--text-muted);
      margin-left: 6px;
    }
    .temp-slider {
      padding: 0;
      accent-color: var(--accent-color, var(--accent-color));
    }
    .group-label {
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted);
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
      color: var(--text-muted);
      cursor: pointer;
      flex-shrink: 0;
    }
    .reset-btn:hover {
      background: var(--danger-tint);
      color: var(--danger);
    }
    .reasoning-reset-notice {
      display: flex;
      align-items: center;
      gap: 5px;
      margin-top: 5px;
      color: var(--text-muted);
      font-size: 11px;
      line-height: 1.4;
    }
    .reasoning-reset-notice app-icon {
      flex-shrink: 0;
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
      color: var(--text-secondary);
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
  /** Server-resolved effective model + provenance per slot (what runs if the
   *  picker is left untouched). Null → fall back to the config-derived default. */
  effectiveModels = input<EffectiveModels | null>(null);
  /** Whether to show the Subagent (delegation reader) model picker. The parent
   *  gates this on the Delegation tool being enabled; defaults to shown so the
   *  component works standalone. */
  showSubagent = input(true);

  change = output<void>();

  readonly availableModels = this.modelService.models;

  // Job mode: per-phase overrides
  readonly strategicModel = signal<string | null>(null);
  readonly tacticalModel = signal<string | null>(null);
  readonly subagentModel = signal<string | null>(null);

  // Session mode: single model override
  readonly sessionModel = signal<string | null>(null);

  // Live mode: temperature override (creation forms don't render this row)
  readonly temperature = signal<number | null>(null);

  // Session mode: reasoning-level override for the model in effect. Owned here
  // (not in the Advanced accordion) so session mode has exactly one writer of
  // llm.reasoning_level; cleared on every model change so a level picked for
  // one model can't silently ride into another family's option set.
  readonly sessionReasoning = signal<string | null>(null);

  // True after a model change or a config prefill has cleared an existing
  // reasoning pick out from under the user — task-3 fix. The reset itself is
  // correct and stays (reasoning vocabularies are per-family and don't
  // translate), but until now it was silent: the select just snapped back to
  // the family default, indistinguishable from never having picked at all.
  // The prefill path in particular does not require a deliberate expert
  // switch — SessionCreateComponent's effective-default-expert resolution can
  // invoke it well after initial render (an `/experts` or `/expert-defaults`
  // response landing late, a project selection), so the user may never have
  // touched Model or the expert grid at all. Cleared by the next deliberate
  // reasoning interaction (a fresh pick, or re-confirming the shown default
  // via pinReasoning) or by resetAll.
  readonly reasoningResetNotice = signal(false);

  // Config-derived default (fallback when the server `effective_models` is
  // absent — e.g. older API or the "defaults" virtual expert). The server value
  // is authoritative because it includes the account/system default floor this
  // config-only view can't see.
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
  // Reader model: subagent pin → tactical pin → base, mirroring the agent's
  // _resolve_subagent_config fallback and the server effective-model slot.
  readonly resolvedSubagentModel = computed(() =>
    (readConfigPath(this.config(), 'llm.subagent.model') as string)
    ?? (readConfigPath(this.config(), 'llm.tactical.model') as string)
    ?? (readConfigPath(this.config(), 'llm.model') as string)
    ?? null
  );
  readonly resolvedSessionModel = computed(() =>
    (readConfigPath(this.config(), 'llm.model') as string) ?? null
  );
  readonly resolvedTemperature = computed(() =>
    (readConfigPath(this.config(), 'llm.temperature') as number) ?? 0.7
  );
  // The reasoning value in effect when the user picks nothing: a config-pinned
  // level (when the model's option set actually contains it) else the family
  // default. Never an unmatched value — a native select with no matching
  // option renders blank.
  readonly resolvedSessionReasoning = computed(() => {
    const options = this.sessionReasoningOptions();
    if (!options.length) return null;
    const pin = (readConfigPath(this.config(), 'llm.reasoning_level') as string) ?? null;
    if (pin && options.some((o) => o.value === pin)) return pin;
    return defaultSelectableReasoning(this.sessionReasoningCap());
  });

  // Server-resolved effective slot ({model, source}) or null when unavailable.
  readonly effectiveStrategic = computed(() => this.effectiveModels()?.strategic ?? null);
  readonly effectiveTactical = computed(() => this.effectiveModels()?.tactical ?? null);
  readonly effectiveSubagent = computed(() => this.effectiveModels()?.subagent ?? null);
  readonly effectiveSession = computed(() => this.effectiveModels()?.session ?? null);

  // The model actually in effect for a slot: explicit override > server
  // effective > config-derived fallback. Drives reasoning options + mismatch.
  readonly strategicInEffect = computed(() =>
    this.strategicModel() ?? this.effectiveStrategic()?.model ?? this.resolvedStrategicModel(),
  );
  readonly tacticalInEffect = computed(() =>
    this.tacticalModel() ?? this.effectiveTactical()?.model ?? this.resolvedTacticalModel(),
  );
  readonly sessionInEffect = computed(() =>
    this.sessionModel() ?? this.effectiveSession()?.model ?? this.resolvedSessionModel(),
  );

  /** Reasoning options for the strategic model in effect. */
  readonly strategicReasoningOptions = computed(() =>
    reasoningOptionsForModel(this.strategicInEffect(), this.modelService.reasoningByModel()),
  );
  /** Reasoning options for the tactical model in effect. */
  readonly tacticalReasoningOptions = computed(() =>
    reasoningOptionsForModel(this.tacticalInEffect(), this.modelService.reasoningByModel()),
  );
  /** Reasoning capability of the session model in effect (null when unknown). */
  private readonly sessionReasoningCap = computed(() => {
    const model = this.sessionInEffect();
    return model ? (this.modelService.reasoningByModel()[model] ?? null) : null;
  });
  /** Concrete reasoning options (no "Default" sentinel — the resolved default
   *  is preselected instead, like the model picker). Empty hides the field. */
  readonly sessionReasoningOptions = computed(() =>
    getSelectableReasoningOptions(this.sessionReasoningCap()),
  );

  /**
   * Label for the unset "Default" option: names the model that will run if the
   * picker is left alone, with provenance. `eff` is the server-resolved slot;
   * `fallback` the config-derived model used when the server didn't resolve one.
   */
  defaultLabel(eff: EffectiveModelSlot | null, fallback: string | null): string {
    const model = eff?.model ?? fallback;
    if (!model) return this.transloco.translate('agentSettings.model.default');
    if (eff?.source) {
      const src = this.transloco.translate('agentSettings.model.source.' + eff.source);
      return this.transloco.translate('agentSettings.model.defaultResolved', {model, source: src});
    }
    return this.transloco.translate('agentSettings.model.defaultResolvedNoSource', {model});
  }

  /**
   * Advisory: do the two phase models' family settings differ enough to matter?
   * Mirrors the backend's min-window / multimodal-AND reconciliation
   * (`resolve_phase_model_budget`). Job mode only; `null` when they agree.
   */
  readonly modelMismatch = computed<ModelMismatch | null>(() => {
    if (this.mode() !== 'job') return null;
    return computeModelMismatch(
      this.settingsMatrix(),
      this.strategicInEffect(),
      this.tacticalInEffect(),
    );
  });

  /** Compact token-count label (shared util), e.g. 131072 → "131k". */
  readonly fmtTokens = formatTokens;

  readonly modifiedCount = computed(() => {
    let count = 0;
    if (this.mode() === 'job') {
      if (this.strategicModel() !== null) count++;
      if (this.tacticalModel() !== null) count++;
      if (this.subagentModel() !== null) count++;
    } else {
      if (this.sessionModel() !== null) count++;
      if (this.sessionReasoning() !== null) count++;
      if (this.temperature() !== null) count++;
    }
    return count;
  });

  /** Commit a displayed-but-inherited value on deliberate interaction.
   *  See PinOnInteractDirective — a <select> emits no change event when the
   *  option already showing is re-picked, so without this the resolved default
   *  is the one value the form cannot express. */
  pinValue<T>(target: {(): T | null; set(value: T | null): void}, resolved: T): void {
    if (pinResolvedValue(target, resolved)) this.change.emit();
  }

  onStrategicModelChange(value: string | null): void {
    this.strategicModel.set(value);
    this.persistModel('strategic', value);
    this.change.emit();
  }

  onTacticalModelChange(value: string | null): void {
    this.tacticalModel.set(value);
    this.persistModel('tactical', value);
    this.change.emit();
  }

  onSubagentModelChange(value: string | null): void {
    this.subagentModel.set(value);
    this.persistModel('subagent', value);
    this.change.emit();
  }

  onSessionModelChange(value: string | null): void {
    this.sessionModel.set(value);
    // A reasoning pick is model-specific intent — drop it with the model so a
    // stale level never leaks into another family's option set. Only raise
    // the notice if there was actually a pick to lose (task-3 fix).
    if (this.sessionReasoning() !== null) this.reasoningResetNotice.set(true);
    this.sessionReasoning.set(null);
    this.persistModel('session', value);
    this.change.emit();
  }

  onSessionReasoningChange(value: string | null): void {
    // Only the explicit "Default" option clears the override; picking the
    // concrete level that happens to be the default pins it.
    this.reasoningResetNotice.set(false);
    this.sessionReasoning.set(value);
    this.change.emit();
  }

  /** Reasoning select's PinOnInteract handler: re-confirming the shown
   *  (resolved-default) value is also the user acknowledging the field, so it
   *  dismisses a pending reset notice the same as a fresh pick would. */
  pinReasoning(): void {
    this.reasoningResetNotice.set(false);
    this.pinValue(this.sessionReasoning, this.resolvedSessionReasoning());
  }

  onTemperatureChange(value: number): void {
    this.temperature.set(value);
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
      const subm = this.subagentModel();
      if (subm) llm['subagent'] = { ...(llm['subagent'] as any ?? {}), model: subm };
    } else {
      const m = this.sessionModel();
      if (m) llm['model'] = m;
      const r = this.sessionReasoning();
      if (r !== null) llm['reasoning_level'] = r;
      const t = this.temperature();
      if (t !== null) llm['temperature'] = t;
    }

    return Object.keys(llm).length > 0 ? { llm } : {};
  }

  resetAll(): void {
    this.strategicModel.set(null);
    this.tacticalModel.set(null);
    this.subagentModel.set(null);
    this.sessionModel.set(null);
    this.sessionReasoning.set(null);
    this.reasoningResetNotice.set(false);
    this.temperature.set(null);
  }

  /** Prefill from expert config (called by parent when expert changes).
   *  Falls back to saved user preferences when config doesn't specify a model. */
  prefillFromConfig(config: Record<string, unknown>): void {
    const llm = config['llm'] as Record<string, unknown> | undefined;
    const strat = llm?.['strategic'] as Record<string, unknown> | undefined;
    const tact = llm?.['tactical'] as Record<string, unknown> | undefined;
    const sub = llm?.['subagent'] as Record<string, unknown> | undefined;
    const baseModel = (llm?.['model'] as string) ?? null;

    this.strategicModel.set(
      strat?.['model'] || baseModel ? null : this.loadSavedModel('strategic'),
    );
    this.tacticalModel.set(
      tact?.['model'] || baseModel ? null : this.loadSavedModel('tactical'),
    );
    // Subagent inherits the tactical pin, so a tactical (or base) model is also
    // "already resolved" — don't preselect a saved subagent model in that case.
    this.subagentModel.set(
      sub?.['model'] || tact?.['model'] || baseModel ? null : this.loadSavedModel('subagent'),
    );
    this.sessionModel.set(baseModel ? null : this.loadSavedModel('session'));
    // Reasoning picks don't survive a config prefill — the new expert's
    // config (and possibly family) makes the old level meaningless. This
    // fires on more than a deliberate expert switch: SessionCreateComponent
    // also calls it from applyEffectiveDefault()'s automatic resolution of
    // the effective default expert, which nothing in this component's own
    // controls gates — so surface it the same as the model-change clearer
    // above (task-3 fix), only when there was a pick to lose.
    if (this.sessionReasoning() !== null) this.reasoningResetNotice.set(true);
    this.sessionReasoning.set(null);
  }

  private loadSavedModel(key: keyof typeof STORAGE_KEYS): string | null {
    try {
      return localStorage.getItem(STORAGE_KEYS[key]) ?? null;
    } catch {
      return null;
    }
  }

  // UI-only: remember the last picked model in localStorage to preselect the
  // dropdown next time. Does NOT write account preferences — that silent global
  // write (default_strategic_model / default_tactical_model) is what shadowed an
  // explicit loop/job model. Account defaults are edited only on the Settings page.
  private persistModel(key: keyof typeof STORAGE_KEYS, value: string | null): void {
    const storageKey = STORAGE_KEYS[key];
    try {
      if (value) {
        localStorage.setItem(storageKey, value);
      } else {
        localStorage.removeItem(storageKey);
      }
    } catch {
      // localStorage may be unavailable
    }
  }
}
