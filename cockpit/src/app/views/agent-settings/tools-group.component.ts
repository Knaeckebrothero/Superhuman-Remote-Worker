import {Component, computed, input, output, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppIconComponent} from '../../ui/icon';
import {
    JOB_TOOL_CATEGORIES,
    readConfigPath,
    SESSION_TOOL_CATEGORIES,
    SettingsMode,
    ToolCategoryMeta,
} from './agent-settings.types';

/**
 * Tool category toggles.
 * Session mode shows additional categories (knowledge, git).
 * Delegation shows inline params (max_depth, timeout) when enabled.
 */
@Component({
  selector: 'app-tools-group',
  standalone: true,
  imports: [FormsModule, TranslocoPipe, AppIconComponent],
  template: `
    <div class="settings-group">
      <div class="group-label">{{ 'agentSettings.tools.group' | transloco }}</div>
      <div class="tool-toggles">
        @for (cat of categories(); track cat.key) {
          <label
            class="tool-toggle"
            [class.modified]="isModified(cat.key)"
            [class.disabled]="disabled()"
          >
            <input
              type="checkbox"
              [checked]="isCategoryEnabled(cat.key)"
              (change)="toggleCategory(cat.key)"
              [disabled]="disabled()"
            >
            <app-icon size="md" class="tool-toggle-icon">{{ cat.icon }}</app-icon>
            <span class="tool-toggle-info">
              <span class="tool-toggle-name">{{ 'agentSettings.toolCategories.' + cat.key + '.label' | transloco }}</span>
              <span class="tool-toggle-desc">{{ 'agentSettings.toolCategories.' + cat.key + '.description' | transloco }}</span>
            </span>
            @if (isModified(cat.key)) {
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
      border-radius: 6px;
      border-left: 2px solid transparent;
      cursor: pointer;
      transition: background 0.15s, border-color 0.15s;
    }
    .tool-toggle:hover:not(.disabled) {
      background: rgba(255, 255, 255, 0.03);
    }
    .tool-toggle.modified {
      border-left-color: var(--accent-color, #cba6f7);
    }
    .tool-toggle.disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .tool-toggle input[type="checkbox"] {
      accent-color: var(--accent-color, #cba6f7);
      flex-shrink: 0;
    }
    .tool-toggle-icon {
      color: var(--text-muted, #6c7086);
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
      color: var(--text-primary, #cdd6f4);
    }
    .tool-toggle-desc {
      font-size: 11px;
      color: var(--text-muted, #6c7086);
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
      border-left-color: var(--accent-color, #cba6f7);
    }
    .inline-label {
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      white-space: nowrap;
    }
    .inline-input {
      padding: 4px 8px;
      border: 1px solid var(--border-color, #45475a);
      border-radius: 4px;
      background: var(--surface-0, #313244);
      color: var(--text-primary, #cdd6f4);
      font-family: inherit;
      font-size: 12px;
    }
    .inline-input:focus {
      outline: none;
      border-color: var(--accent-color, #cba6f7);
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
  /** Default tool lists from defaults.yaml, used to re-enable expert-disabled categories. */
  defaultsTools = input<Record<string, string[]>>({});

  change = output<void>();

  /** User-toggled disabled categories (key = category key). */
  readonly disabledCategories = signal<Set<string>>(new Set());
  /** Categories the expert config originally disabled. */
  private expertDisabledCategories = new Set<string>();

  /** Delegation inline params. */
  readonly delegationMaxDepth = signal<number | null>(null);
  readonly delegationTimeout = signal<number | null>(null);

  readonly categories = computed<ToolCategoryMeta[]>(() =>
    this.mode() === 'session' ? SESSION_TOOL_CATEGORIES : JOB_TOOL_CATEGORIES
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
    // → restore the defaults' tool list
    const defaults = this.defaultsTools();
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
    const tools = config['tools'] as Record<string, unknown[]> | undefined;
    const disabled = new Set<string>();
    if (tools) {
      for (const cat of this.categories()) {
        const val = tools[cat.key];
        if (Array.isArray(val) && val.length === 0) {
          disabled.add(cat.key);
        }
      }
    }
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
