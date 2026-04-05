import {Component, computed, input, output, signal} from '@angular/core';
import {JOB_TOOL_CATEGORIES, SESSION_TOOL_CATEGORIES, SettingsMode, ToolCategoryMeta,} from './agent-settings.types';

/**
 * Tool category toggles.
 * Session mode shows additional categories (knowledge, git).
 */
@Component({
  selector: 'app-tools-group',
  standalone: true,
  imports: [],
  template: `
    <div class="settings-group">
      <div class="group-label">Tools</div>
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
            <span class="tool-toggle-icon">{{ cat.icon }}</span>
            <span class="tool-toggle-info">
              <span class="tool-toggle-name">{{ cat.label }}</span>
              <span class="tool-toggle-desc">{{ cat.description }}</span>
            </span>
            @if (isModified(cat.key)) {
              <button
                class="reset-btn"
                (click)="resetCategory(cat.key, $event)"
                title="Reset to default"
              >close</button>
            }
          </label>
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
      font-family: 'Material Symbols Outlined', sans-serif;
      font-size: 18px;
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
      font-family: 'Material Symbols Outlined', sans-serif;
      font-size: 14px;
      cursor: pointer;
      flex-shrink: 0;
    }
    .reset-btn:hover {
      background: rgba(243, 139, 168, 0.2);
      color: #f38ba8;
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

  readonly categories = computed<ToolCategoryMeta[]>(() =>
    this.mode() === 'session' ? SESSION_TOOL_CATEGORIES : JOB_TOOL_CATEGORIES
  );

  readonly modifiedCount = computed(() => {
    let count = 0;
    for (const cat of this.categories()) {
      if (this.isModified(cat.key)) count++;
    }
    return count;
  });

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
    this.change.emit();
  }

  /** Build the tools config_override fragment. */
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

    return Object.keys(tools).length > 0 ? { tools } : {};
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
  }

  resetAll(): void {
    this.disabledCategories.set(new Set(this.expertDisabledCategories));
  }
}
