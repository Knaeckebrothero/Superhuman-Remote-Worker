import {Component, computed, effect, ElementRef, inject, input, output, signal, ViewChild} from '@angular/core';
import {Datasource} from '../../core/models/api.model';
import {ViewportService} from '../../core/services/viewport.service';
import {SettingsMode} from './agent-settings.types';
import {ExecutionGroupComponent} from './execution-group.component';
import {ModelGroupComponent} from './model-group.component';
import {ToolsGroupComponent} from './tools-group.component';
import {DatasourcesGroupComponent} from './datasources-group.component';
import {InstructionsTabComponent} from './instructions-tab.component';
import {AdvancedAccordionComponent} from './advanced-accordion.component';
import {AppTabNavComponent, AppTabNavItemComponent} from '../../ui/tab-nav';
import {AppBadgeComponent} from '../../ui/badge';

type AgentSettingsTab = 'settings' | 'instructions' | 'advanced' | 'resolved';

/**
 * Host component for agent settings. Composes all sub-components with a tabbed layout.
 *
 * - **Job creation**: vertical tabs (Settings, Instructions, Advanced)
 * - **Session creation**: horizontal tabs (Settings, Advanced) — no Instructions tab
 *
 * Each sub-component manages its own override state. The parent calls `getOverrides()`
 * at submit time to collect all overrides, and `resetAll()` to clear everything.
 */
@Component({
  selector: 'app-agent-settings',
  standalone: true,
  imports: [
    ExecutionGroupComponent,
    ModelGroupComponent,
    ToolsGroupComponent,
    DatasourcesGroupComponent,
    InstructionsTabComponent,
    AdvancedAccordionComponent,
    AppTabNavComponent,
    AppTabNavItemComponent,
    AppBadgeComponent,
  ],
  template: `
    <div class="settings-root" [class.vertical]="useVerticalTabs()" [class.horizontal]="!useVerticalTabs()">
      <!-- Tab navigation -->
      <app-tab-nav
        [orientation]="useVerticalTabs() ? 'vertical' : 'horizontal'"
        [value]="activeTab()"
        (valueChange)="onTabChange($event)"
      >
        <app-tab-nav-item value="settings">
          Settings
          @if (settingsModifiedCount() > 0) {
            <app-badge tone="accent" size="xs" shape="pill">{{ settingsModifiedCount() }}</app-badge>
          }
        </app-tab-nav-item>
        @if (mode() === 'job') {
          <app-tab-nav-item value="instructions">
            Instructions
            @if (instructionsTab?.isModified()) {
              <span class="tab-modified-dot" aria-hidden="true"></span>
            }
          </app-tab-nav-item>
        }
        <app-tab-nav-item value="advanced">Advanced</app-tab-nav-item>
        <app-tab-nav-item value="resolved">Resolved</app-tab-nav-item>
      </app-tab-nav>

      <!-- Tab content -->
      <div class="tab-content" #tabContent>
        <div class="tab-panel settings-panel" [class.tab-hidden]="activeTab() !== 'settings'">
          <app-execution-group
            [config]="config()"
            [mode]="mode()"
            [disabled]="disabled()"
            [showProjectMemory]="showProjectMemory()"
            (change)="onChange()"
          />
          <app-model-group
            [config]="config()"
            [mode]="mode()"
            [disabled]="disabled()"
            [settingsMatrix]="settingsMatrix()"
            (change)="onChange()"
          />
          <app-tools-group
            [config]="config()"
            [mode]="mode()"
            [disabled]="disabled()"
            [defaultsTools]="defaultsTools()"
            (change)="onChange()"
          />
          <app-datasources-group
            [datasources]="datasources()"
            [loading]="loadingDatasources()"
            [disabled]="disabled()"
            [isLiteBackend]="advancedAccordion?.isLiteBackend() ?? false"
            (change)="onChange()"
          />

          <!-- Modified count summary -->
          @if (settingsModifiedCount() > 0) {
            <div class="modified-summary">
              {{ settingsModifiedCount() }} setting{{ settingsModifiedCount() > 1 ? 's' : '' }} modified
            </div>
          }
        </div>

        @if (mode() === 'job') {
          <div class="tab-panel instructions-panel" [class.tab-hidden]="activeTab() !== 'instructions'">
            <app-instructions-tab
              [disabled]="disabled()"
              [loadingExpert]="loadingExpert()"
              (contentChange)="onInstructionsChange($event)"
            />
          </div>
        }

        <div class="tab-panel advanced-panel" [class.tab-hidden]="activeTab() !== 'advanced'">
          <app-advanced-accordion
            [config]="config()"
            [mode]="mode()"
            [disabled]="disabled()"
            [settingsMatrix]="settingsMatrix()"
            [strategicModelOverride]="modelGroup?.strategicModel() ?? null"
            [tacticalModelOverride]="modelGroup?.tacticalModel() ?? null"
            [sessionModelOverride]="modelGroup?.sessionModel() ?? null"
            (change)="onChange()"
          />
        </div>

        <div class="tab-panel resolved-panel" [class.tab-hidden]="activeTab() !== 'resolved'">
          <pre class="config-json">{{ resolvedConfigJson() }}</pre>
        </div>
      </div>
    </div>
  `,
  styles: [`
    .settings-root {
      display: flex;
      border: 1px solid var(--border-color, var(--surface-0));
      border-radius: var(--radius-surface);
      overflow: hidden;
      background: var(--panel-bg, var(--panel-bg));
      min-height: 300px;
    }

    .settings-root.vertical { flex-direction: row; }
    .settings-root.horizontal { flex-direction: column; }

    .tab-modified-dot {
      display: inline-block;
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--accent-color, var(--accent-color));
    }

    .tab-content {
      flex: 1;
      overflow: auto;
      min-width: 0;
    }
    .tab-panel {
      padding: 16px;
    }
    .tab-hidden {
      display: none !important;
    }
    .instructions-panel {
      display: flex;
      flex-direction: column;
      height: 100%;
    }

    .resolved-panel {
      padding: 0;
    }
    .resolved-panel .config-json {
      margin: 0;
      padding: 12px 14px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      line-height: 1.5;
      color: var(--text-primary);
      background: var(--surface-0);
      white-space: pre-wrap;
      word-break: break-all;
    }

    .modified-summary {
      text-align: right;
      font-size: 11px;
      color: var(--accent-color, var(--accent-color));
      padding-top: 8px;
      border-top: 1px solid var(--border-color, var(--surface-0));
    }
  `],
})
export class AgentSettingsComponent {
  /** Merged expert/framework config for resolved defaults. */
  config = input<Record<string, unknown>>({});
  mode = input<SettingsMode>('job');
  disabled = input(false);

  private readonly viewport = inject(ViewportService);
  /**
   * Vertical tab rail only in job mode on wide screens. On mobile the rail would
   * eat ~130px and squeeze the content into ~219px (horizontal scroll, clipped
   * Delegation controls), so collapse to horizontal tabs with full-width content.
   */
  readonly useVerticalTabs = computed(() => this.mode() === 'job' && !this.viewport.isMobile());
  /** Whether the selected project has shared memory. */
  showProjectMemory = input(false);
  /** Default tool lists from defaults.yaml. */
  defaultsTools = input<Record<string, string[]>>({});
  /** Raw settings_matrix for client-side model-family resolution. */
  settingsMatrix = input<Record<string, Record<string, unknown>>>({});
  /** Available datasources. */
  datasources = input<Datasource[]>([]);
  loadingDatasources = input(false);
  /** Expert detail loading state. */
  loadingExpert = input(false);

  /** Emitted whenever any setting changes. */
  change = output<void>();
  /** Emitted when instructions content changes. */
  instructionsChange = output<string | null>();

  @ViewChild(ExecutionGroupComponent) executionGroup?: ExecutionGroupComponent;
  @ViewChild(ModelGroupComponent) modelGroup?: ModelGroupComponent;
  @ViewChild(ToolsGroupComponent) toolsGroup?: ToolsGroupComponent;
  @ViewChild(DatasourcesGroupComponent) datasourcesGroup?: DatasourcesGroupComponent;
  @ViewChild(InstructionsTabComponent) instructionsTab?: InstructionsTabComponent;
  @ViewChild(AdvancedAccordionComponent) advancedAccordion?: AdvancedAccordionComponent;
  @ViewChild('tabContent') private tabContentEl?: ElementRef<HTMLElement>;

  readonly activeTab = signal<AgentSettingsTab>('settings');

  protected onTabChange(value: AgentSettingsTab | null): void {
    if (value === 'settings' || value === 'instructions' || value === 'advanced' || value === 'resolved') {
      this.activeTab.set(value);
    }
  }

  readonly resolvedConfigJson = computed(() => {
    try {
      return JSON.stringify(this.config(), null, 2);
    } catch {
      return '{}';
    }
  });

  constructor() {
    effect(() => {
      this.activeTab(); // track tab changes
      this.tabContentEl?.nativeElement.scrollTo(0, 0);
    });
  }

  readonly settingsModifiedCount = computed(() => {
    const exec = this.executionGroup?.modifiedCount() ?? 0;
    const model = this.modelGroup?.modifiedCount() ?? 0;
    const tools = this.toolsGroup?.modifiedCount() ?? 0;
    const ds = this.datasourcesGroup?.modifiedCount() ?? 0;
    return exec + model + tools + ds;
  });

  onChange(): void {
    this.change.emit();
  }

  onInstructionsChange(value: string | null): void {
    this.instructionsChange.emit(value);
  }

  /**
   * Collect all overrides from sub-components into a single config_override object.
   * Called by the parent component at form submission time.
   */
  getOverrides(): Record<string, unknown> {
    const parts = [
      this.executionGroup?.getOverrides() ?? {},
      this.modelGroup?.getOverrides() ?? {},
      this.toolsGroup?.getOverrides() ?? {},
      this.advancedAccordion?.getOverrides() ?? {},
    ];

    // Deep merge all parts
    const result: Record<string, unknown> = {};
    for (const part of parts) {
      this.deepMerge(result, part);
    }
    return Object.keys(result).length > 0 ? result : {};
  }

  /** Return selected datasource IDs (not part of config_override). */
  getSelectedDatasourceIds(): string[] {
    return this.datasourcesGroup?.getSelectedIds() ?? [];
  }

  /** Return current instructions content (not part of config_override). */
  getInstructions(): string | null {
    return this.instructionsTab?.content() ?? null;
  }

  /**
   * Called by parent when expert changes. Propagates to all sub-components.
   */
  prefillFromConfig(config: Record<string, unknown>): void {
    this.modelGroup?.prefillFromConfig(config);
    this.toolsGroup?.prefillFromConfig(config);
    this.advancedAccordion?.prefillFromConfig(config);

    // Execution group reads from the config input directly (via computed signals),
    // but we should reset user overrides since the expert changed
    this.executionGroup?.resetAll();

    // Prefill execution overrides from expert
    const autonomy = config['autonomy'] as string | undefined;
    if (autonomy) this.executionGroup?.autonomy.set(autonomy);

    const scholar = (config['scholar'] as Record<string, unknown>)?.['enabled'] as boolean | undefined;
    if (scholar !== undefined) this.executionGroup?.scholar.set(scholar);

    const verification = config['verification'] as Record<string, unknown> | undefined;
    if (verification?.['enabled'] !== undefined) {
      this.executionGroup?.critic.set(verification['enabled'] as boolean);
    }
    if (verification?.['max_rounds'] !== undefined) {
      this.executionGroup?.criticRounds.set(verification['max_rounds'] as number);
    }
  }

  /** Reset all sub-components to defaults. */
  resetAll(): void {
    this.executionGroup?.resetAll();
    this.modelGroup?.resetAll();
    this.toolsGroup?.resetAll();
    this.datasourcesGroup?.resetAll();
    this.instructionsTab?.resetAll();
    this.advancedAccordion?.resetAll();
    this.activeTab.set('settings');
  }

  private deepMerge(target: Record<string, unknown>, source: Record<string, unknown>): void {
    for (const key of Object.keys(source)) {
      const sv = source[key];
      const tv = target[key];
      if (
        typeof sv === 'object' && sv !== null && !Array.isArray(sv) &&
        typeof tv === 'object' && tv !== null && !Array.isArray(tv)
      ) {
        this.deepMerge(tv as Record<string, unknown>, sv as Record<string, unknown>);
      } else {
        target[key] = sv;
      }
    }
  }
}
