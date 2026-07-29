import {Component, computed, input, output, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppIconComponent} from '../../ui/icon';
import {
    JOB_TOOL_CATEGORIES,
    LIVE_TOOL_CATEGORIES,
    pinResolvedValue,
    readConfigPath,
    SESSION_TOOL_CATEGORIES,
    SESSION_TOOL_GROUP_NAMES,
    SettingsMode,
    ToolCategoryMeta,
} from './agent-settings.types';
import {PinOnInteractDirective} from './pin-on-interact.directive';

/** True when every selectable category key is enabled (none in the disabled set). */
export function allToolCategoriesSelected(
  selectableKeys: string[],
  disabledCategories: Set<string>,
): boolean {
  return selectableKeys.length > 0 && selectableKeys.every(k => !disabledCategories.has(k));
}

/** Category keys explicitly disabled by a resolved config. */
export function disabledToolCategoriesFromConfig(
  config: Record<string, unknown>,
  categoryKeys: string[],
): Set<string> {
  const tools = config['tools'] as Record<string, unknown[]> | undefined;
  const disabled = new Set<string>();
  if (!tools) return disabled;
  for (const key of categoryKeys) {
    const value = tools[key];
    if (Array.isArray(value) && value.length === 0) disabled.add(key);
  }
  return disabled;
}

/**
 * Tool category toggles.
 * Session mode shows additional categories (knowledge, git).
 * Delegation shows inline params (max_depth, timeout) when enabled.
 */
@Component({
  selector: 'app-tools-group',
  standalone: true,
  imports: [FormsModule, TranslocoPipe, AppIconComponent, PinOnInteractDirective],
  template: `
    <div class="settings-group">
      <div class="group-header">
        <span class="group-label">{{ 'agentSettings.tools.group' | transloco }}</span>
        <button
          type="button"
          class="select-all-btn"
          (click)="toggleAll()"
          [disabled]="disabled() || selectableCategories().length === 0"
        >{{ (allSelected() ? 'agentSettings.common.deselectAll' : 'agentSettings.common.selectAll') | transloco }}</button>
      </div>
      <div class="tool-toggles">
        @for (cat of categories(); track cat.key) {
          <label
            class="tool-toggle"
            [class.modified]="isModified(cat.key)"
            [class.disabled]="disabled() || isCategoryBlocked(cat.key)"
            [title]="isCategoryBlocked(cat.key) ? ('grants.lockedShort' | transloco) : ''"
          >
            <input
              type="checkbox"
              [checked]="isCategoryEnabled(cat.key)"
              (change)="toggleCategory(cat.key)"
              [disabled]="disabled() || isCategoryBlocked(cat.key)"
            >
            <app-icon size="md" class="tool-toggle-icon">{{ cat.icon }}</app-icon>
            <span class="tool-toggle-info">
              <span class="tool-toggle-name">{{ 'agentSettings.toolCategories.' + cat.key + '.label' | transloco }}@if (isCategoryBlocked(cat.key)) { <span class="tool-lock">🔒</span> }</span>
              <span class="tool-toggle-desc">{{ 'agentSettings.toolCategories.' + cat.key + '.description' | transloco }}</span>
            </span>
            @if (isModified(cat.key) && mode() !== 'live') {
              <button
                class="reset-btn"
                (click)="resetCategory(cat.key, $event)"
                [title]="'agentSettings.common.resetToDefault' | transloco"
              ><app-icon size="xs">close</app-icon></button>
            }
          </label>
          @if (cat.key === 'delegation' && isCategoryEnabled('delegation')) {
            <div class="inline-params">
              <div class="inline-field" [class.modified]="delegationMaxDepth() !== null">
                <label class="inline-label">{{ 'agentSettings.tools.maxDepth' | transloco }}</label>
                <select class="inline-input"
                  [ngModel]="delegationMaxDepth() ?? resolvedDelegationMaxDepth()"
              appPinOnInteract (pin)="pinValue(delegationMaxDepth, resolvedDelegationMaxDepth())"
                  (ngModelChange)="onDelegationMaxDepthChange($event)"
                  [disabled]="disabled()">
                  <option [ngValue]="1">1</option>
                  <option [ngValue]="2">2</option>
                  <option [ngValue]="3">3</option>
                </select>
                @if (delegationMaxDepth() !== null) {
                  <button type="button" class="reset-btn" (click)="delegationMaxDepth.set(null); change.emit()">close</button>
                }
              </div>
              <div class="inline-field" [class.modified]="delegationTimeout() !== null">
                <label class="inline-label">{{ 'agentSettings.tools.timeout' | transloco }}</label>
                <input type="number" class="inline-input number-input" min="60" step="60"
                  [ngModel]="delegationTimeout() ?? resolvedDelegationTimeout()"
                  (ngModelChange)="onDelegationTimeoutChange($event)"
                  [disabled]="disabled()">
                @if (delegationTimeout() !== null) {
                  <button type="button" class="reset-btn" (click)="delegationTimeout.set(null); change.emit()">close</button>
                }
              </div>
            </div>
          }
        }
      </div>
    </div>
  `,
  styles: [`
    .settings-group {
      margin-bottom: 20px;
    }
    .group-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 12px;
      padding-bottom: 6px;
      border-bottom: 1px solid var(--border-color, var(--surface-0));
    }
    .group-label {
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted);
    }
    .select-all-btn {
      flex-shrink: 0;
      background: none;
      border: none;
      padding: 0;
      cursor: pointer;
      font-family: inherit;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--accent-color, var(--accent-color));
    }
    .select-all-btn:hover:not(:disabled) {
      text-decoration: underline;
    }
    .select-all-btn:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
    .tool-toggles {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .tool-toggle {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 8px 10px;
      border-radius: var(--radius-control);
      border-left: 2px solid transparent;
      cursor: pointer;
      transition: background 0.15s, border-color 0.15s;
    }
    .tool-toggle:hover:not(.disabled) {
      background: rgba(255, 255, 255, 0.03);
    }
    .tool-toggle.modified {
      border-left-color: var(--accent-color, var(--accent-color));
    }
    .tool-toggle.disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .tool-toggle input[type="checkbox"] {
      accent-color: var(--accent-color, var(--accent-color));
      flex-shrink: 0;
    }
    .tool-toggle-icon {
      color: var(--text-muted);
      flex-shrink: 0;
    }
    .tool-toggle-info {
      display: flex;
      flex-direction: column;
      gap: 1px;
      flex: 1;
      min-width: 0;
    }
    .tool-toggle-name {
      font-size: 13px;
      font-weight: 500;
      color: var(--text-primary, var(--text-primary));
    }
    .tool-toggle-desc {
      font-size: 11px;
      color: var(--text-muted);
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
    .inline-params {
      display: flex;
      gap: 12px;
      padding: 6px 10px 6px 42px;
    }
    .inline-field {
      display: flex;
      align-items: center;
      gap: 6px;
      padding-left: 4px;
      border-left: 2px solid transparent;
    }
    .inline-field.modified {
      border-left-color: var(--accent-color, var(--accent-color));
    }
    .inline-label {
      font-size: 11px;
      color: var(--text-muted);
      white-space: nowrap;
    }
    .inline-input {
      padding: 4px 8px;
      border: 1px solid var(--border-color, var(--surface-1));
      border-radius: var(--radius-control);
      background: var(--surface-0, var(--surface-0));
      color: var(--text-primary, var(--text-primary));
      font-family: inherit;
      font-size: 12px;
    }
    .inline-input:focus {
      outline: none;
      border-color: var(--accent-color, var(--accent-color));
    }
    .number-input {
      max-width: 80px;
    }
  `],
})
export class ToolsGroupComponent {
  config = input<Record<string, unknown>>({});
  mode = input<SettingsMode>('job');
  disabled = input(false);
  /** Tool lists from the selected expert's mode base, used when re-enabling categories. */
  defaultsTools = input<Record<string, string[]>>({});
  /** Author's resolved capability grants for editor greying; null ⇒ no gating
   *  (launch flow / admin). Maps tool categories → catalog keys. */
  gatedCapabilities = input<Record<string, unknown> | null>(null);

  private readonly CAT_TO_GRANT: Record<string, string> = {
    shell: 'shell_tools',
    delegation: 'delegation',
    browser_direct: 'browser',
  };

  /** True if a category is blocked by a missing grant (disable-only — never
   *  mutates the fragment, so opening an admin-authored expert can't strip it). */
  isCategoryBlocked(catKey: string): boolean {
    const g = this.gatedCapabilities();
    if (g === null) return false;
    const grantKey = this.CAT_TO_GRANT[catKey];
    return !!grantKey && g[grantKey] !== true;
  }

  change = output<void>();

  /** User-toggled disabled categories (key = category key). */
  readonly disabledCategories = signal<Set<string>>(new Set());
  /** Categories the expert config originally disabled. */
  private expertDisabledCategories = new Set<string>();

  /** Delegation inline params. */
  readonly delegationMaxDepth = signal<number | null>(null);
  readonly delegationTimeout = signal<number | null>(null);

  readonly categories = computed<ToolCategoryMeta[]>(() => {
    // Live mode renders ONLY the four validated closed groups — the other 8
    // session categories are silently dropped by the live config.update
    // path's closed vocabulary and would no-op as toggles.
    if (this.mode() === 'live') return LIVE_TOOL_CATEGORIES;
    return this.mode() === 'session' ? SESSION_TOOL_CATEGORIES : JOB_TOOL_CATEGORIES;
  });

  /** Categories the user can actually toggle (not grant-blocked). */
  readonly selectableCategories = computed<ToolCategoryMeta[]>(() =>
    this.categories().filter(cat => !this.isCategoryBlocked(cat.key))
  );

  /** True when every selectable category is currently enabled. */
  readonly allSelected = computed(() =>
    allToolCategoriesSelected(
      this.selectableCategories().map(cat => cat.key),
      this.disabledCategories(),
    )
  );

  readonly modifiedCount = computed(() => {
    let count = 0;
    for (const cat of this.categories()) {
      if (this.isModified(cat.key)) count++;
    }
    if (this.delegationMaxDepth() !== null) count++;
    if (this.delegationTimeout() !== null) count++;
    return count;
  });

  // --- Resolved defaults ---
  private r(path: string): unknown { return readConfigPath(this.config(), path); }

  readonly resolvedDelegationMaxDepth = computed(() => (this.r('delegation.max_depth') ?? 1) as number);
  readonly resolvedDelegationTimeout = computed(() => (this.r('delegation.default_timeout') ?? 7200) as number);

  /** Commit a displayed-but-inherited value on deliberate interaction.
   *  See PinOnInteractDirective — a <select> emits no change event when the
   *  option already showing is re-picked, so without this the resolved default
   *  is the one value the form cannot express. */
  pinValue<T>(target: {(): T | null; set(value: T | null): void}, resolved: T): void {
    if (pinResolvedValue(target, resolved)) this.change.emit();
  }

  isCategoryEnabled(key: string): boolean {
    return !this.disabledCategories().has(key);
  }

  isModified(key: string): boolean {
    const disabled = this.disabledCategories().has(key);
    const wasDisabledByExpert = this.expertDisabledCategories.has(key);
    // Modified if: user disabled something that was enabled, or enabled something that was disabled
    return disabled !== wasDisabledByExpert;
  }

  toggleCategory(key: string): void {
    this.disabledCategories.update(current => {
      const next = new Set(current);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
    this.change.emit();
  }

  /** Enable every selectable category, or disable them all if already all on.
   *  Grant-blocked categories are left untouched (they can't be toggled). */
  toggleAll(): void {
    const keys = this.selectableCategories().map(cat => cat.key);
    const selectAll = !allToolCategoriesSelected(keys, this.disabledCategories());
    this.disabledCategories.update(current => {
      const next = new Set(current);
      for (const key of keys) {
        if (selectAll) next.delete(key);
        else next.add(key);
      }
      return next;
    });
    this.change.emit();
  }

  resetCategory(key: string, event: Event): void {
    event.preventDefault();
    event.stopPropagation();
    this.disabledCategories.update(current => {
      const next = new Set(current);
      if (this.expertDisabledCategories.has(key)) {
        next.add(key);
      } else {
        next.delete(key);
      }
      return next;
    });
    if (key === 'delegation') {
      this.delegationMaxDepth.set(null);
      this.delegationTimeout.set(null);
    }
    this.change.emit();
  }

  onDelegationMaxDepthChange(v: number): void { this.delegationMaxDepth.set(v); this.change.emit(); }
  onDelegationTimeoutChange(v: number): void { this.delegationTimeout.set(v); this.change.emit(); }

  /** Build the tools + delegation config_override fragment. */
  getOverrides(): Record<string, unknown> {
    const tools: Record<string, unknown> = {};
    const disabled = this.disabledCategories();

    // User-disabled categories → empty array
    disabled.forEach(cat => {
      tools[cat] = [];
    });

    // Re-enabled categories (expert had them disabled, user toggled ON)
    // → restore a full tool list. Live mode uses the closed-vocabulary
    // mirror (enablement is keyed off empty-vs-non-empty agent-side, but the
    // payload must survive the closed-group validation); creation modes keep
    // the mode-base lists.
    const defaults = this.mode() === 'live' ? SESSION_TOOL_GROUP_NAMES : this.defaultsTools();
    for (const cat of this.expertDisabledCategories) {
      if (!disabled.has(cat) && defaults[cat]?.length) {
        tools[cat] = [...defaults[cat]];
      }
    }

    const result: Record<string, unknown> = {};
    if (Object.keys(tools).length > 0) result['tools'] = tools;

    // Delegation config: sync delegation.enabled with the tool toggle,
    // and include inline param overrides
    const delegationEnabled = this.isCategoryEnabled('delegation');
    const wasEnabledByExpert = !this.expertDisabledCategories.has('delegation');
    const hasParamOverrides = this.delegationMaxDepth() !== null || this.delegationTimeout() !== null;

    if (delegationEnabled !== wasEnabledByExpert || hasParamOverrides) {
      const d: Record<string, unknown> = {};
      if (delegationEnabled !== wasEnabledByExpert) d['enabled'] = delegationEnabled;
      if (this.delegationMaxDepth() !== null) d['max_depth'] = this.delegationMaxDepth();
      if (this.delegationTimeout() !== null) d['default_timeout'] = this.delegationTimeout();
      result['delegation'] = d;
    }

    return result;
  }

  /** Called by parent when expert changes to sync disabled state. */
  prefillFromConfig(config: Record<string, unknown>): void {
    const disabled = disabledToolCategoriesFromConfig(
      config,
      this.categories().map((category) => category.key),
    );
    this.disabledCategories.set(disabled);
    this.expertDisabledCategories = new Set(disabled);

    // Reset delegation inline params on expert change
    this.delegationMaxDepth.set(null);
    this.delegationTimeout.set(null);
  }

  resetAll(): void {
    this.disabledCategories.set(new Set(this.expertDisabledCategories));
    this.delegationMaxDepth.set(null);
    this.delegationTimeout.set(null);
  }
}
