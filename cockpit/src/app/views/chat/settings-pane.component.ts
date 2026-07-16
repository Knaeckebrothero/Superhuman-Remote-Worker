import {
    Component,
    DestroyRef,
    ElementRef,
    computed,
    effect,
    inject,
    input,
    output,
    signal,
    viewChild,
} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';
import {AgentSettingsComponent} from '../agent-settings/agent-settings.component';
import {
    LIVE_TOOL_CATEGORIES,
    SESSION_TOOL_GROUP_NAMES,
    readConfigPath,
} from '../agent-settings/agent-settings.types';
import {deepMergeConfig} from '../agent-settings/config-merge';
import {PersistentChatService, NarrationMode, PermissionMode} from '../../core/services/persistent-chat.service';
import {ApiService} from '../../core/services/api.service';
import {CapabilitiesService} from '../../core/services/capabilities.service';
import {ModelService} from '../../core/services/model.service';
import {Datasource} from '../../core/models/api.model';
import {AppIconComponent} from '../../ui/icon';
import {AppIconButtonComponent} from '../../ui/icon-button';
import {AppButtonComponent} from '../../ui/button';

/** Paths tracked by the desired-state diff. Everything the live path honors
 *  except tool groups (which diff as enabled-flags) and workspace (its own
 *  upgrade verb). */
const TRACKED_LLM_PATHS = ['llm.model', 'llm.temperature', 'llm.reasoning_level'] as const;

/** How long to coalesce control changes before applying. One batch = one
 *  config.update = one prompt-cache invalidation, however many toggles the
 *  user flips in quick succession (live_session_settings.md, principle 4).
 *  Also debounces the per-tick temperature slider. */
const APPLY_DEBOUNCE_MS = 400;

/**
 * Live session settings pane (live_session_settings.md, Slice A).
 *
 * Hosts the shared AgentSettingsComponent in `live` mode next to the chat
 * (content-switched with the canvas pane). Every control edit is collected
 * through the sub-groups' existing override state, then this host diffs the
 * desired state against what was last applied and dispatches only the delta:
 * permission/narration via their dedicated verbs (they broadcast + persist),
 * everything else via one coalesced `config.update`. Pin-only — live mode
 * renders no reset-to-default affordances (principle 6).
 */
@Component({
    selector: 'app-settings-pane',
    standalone: true,
    imports: [
        AgentSettingsComponent,
        TranslocoPipe,
        AppIconComponent,
        AppIconButtonComponent,
        AppButtonComponent,
    ],
    template: `
      <div class="pane-root" #paneRoot>
        <div class="pane-header">
          <app-icon size="sm">tune</app-icon>
          <span class="pane-title">{{ 'chat.settingsPane.title' | transloco }}</span>
          <span class="pane-spacer"></span>
          <app-icon-button size="sm"
                           [ariaLabel]="'chat.settingsPane.close' | transloco"
                           (clicked)="closeRequested.emit()">
            <app-icon size="sm">close</app-icon>
          </app-icon-button>
        </div>

        <div class="pane-body">
          <app-agent-settings
            mode="live"
            [config]="liveConfig()"
            [disabled]="!chat.isConnected()"
            [gatedCapabilities]="capabilities.grants() ?? null"
            [datasources]="pickerDatasources()"
            [loadingDatasources]="loadingThread()"
            [initialDatasourceIds]="attachedIds()"
            [lockedDatasourceIds]="lockedDatasourceIds()"
            [liteBackend]="isLiteBackend()"
            (change)="onSettingsChange()"
          />

          <!-- Workspace tier: its own upgrade verb, not config.update -->
          <div class="workspace-group">
            <div class="group-label">{{ 'chat.settingsPane.workspace' | transloco }}</div>
            <div class="workspace-row">
              <span class="workspace-tier">{{ 'chat.settingsPane.tier.' + workspaceTier() | transloco }}</span>
              @if (chat.workspaceUpgradeInProgress(); as prog) {
                <span class="workspace-progress">
                  <span class="progress-spinner"></span>
                  {{ 'chat.settingsPane.upgrading' | transloco:{ tier: prog.tier } }}
                  @if (prog.elapsed) { ({{ prog.elapsed }}s) }
                </span>
              } @else {
                @if (canUpgradeToSandbox()) {
                  <app-button variant="secondary" size="sm" (clicked)="upgrade('sandbox')">
                    {{ 'chat.settingsPane.upgradeSandbox' | transloco }}
                  </app-button>
                }
                @if (canUpgradeToVm()) {
                  <app-button variant="secondary" size="sm" (clicked)="upgrade('vm')">
                    {{ 'chat.settingsPane.upgradeVm' | transloco }}
                  </app-button>
                }
              }
            </div>
            <span class="workspace-hint">{{ 'chat.settingsPane.workspaceHint' | transloco }}</span>
          </div>

          <!-- Set-at-creation surfaces, shown for honesty (criterion 7) -->
          <p class="fixed-note">{{ 'chat.settingsPane.fixedNote' | transloco }}</p>
        </div>
      </div>
    `,
    styles: `
      .pane-root {
        display: flex;
        flex-direction: column;
        width: 100%;
        height: 100%;
        min-width: 0;
        background: var(--panel-bg);
      }
      .pane-header {
        display: flex;
        align-items: center;
        gap: 8px;
        flex: 0 0 auto;
        padding: 8px 12px;
        border-bottom: 1px solid var(--border-color);
        color: var(--text-primary);
      }
      .pane-title { font-weight: 600; font-size: 13px; }
      .pane-spacer { flex: 1; }
      .pane-body {
        flex: 1;
        overflow-y: auto;
        padding: 12px;
        min-height: 0;
      }
      .workspace-group { margin-top: 16px; }
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
      .workspace-row {
        display: flex;
        align-items: center;
        gap: 10px;
        flex-wrap: wrap;
      }
      .workspace-tier {
        font-size: 13px;
        color: var(--text-primary);
        font-weight: 500;
      }
      .workspace-progress {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        color: var(--text-muted);
      }
      .progress-spinner {
        width: 12px;
        height: 12px;
        border: 2px solid var(--border-color);
        border-top-color: var(--accent-color);
        border-radius: 50%;
        animation: settings-pane-spin 0.8s linear infinite;
      }
      @keyframes settings-pane-spin { to { transform: rotate(360deg); } }
      .workspace-hint, .fixed-note {
        display: block;
        margin-top: 6px;
        font-size: 11px;
        line-height: 1.4;
        color: var(--text-muted);
      }
      .fixed-note { margin-top: 16px; }
    `,
})
export class SettingsPaneComponent {
    readonly chat = inject(PersistentChatService);
    readonly capabilities = inject(CapabilitiesService);
    private readonly api = inject(ApiService);
    private readonly modelService = inject(ModelService);
    private readonly destroyRef = inject(DestroyRef);

    /** Whether the pane is currently shown (drives the lazy thread fetch). */
    readonly active = input(false);
    readonly closeRequested = output<void>();

    private readonly settings = viewChild(AgentSettingsComponent);
    private readonly paneRoot = viewChild<unknown, ElementRef<HTMLElement>>('paneRoot', {read: ElementRef});

    /** Redacted config_override from thread metadata (fetched on open). */
    private readonly threadOverride = signal<Record<string, unknown>>({});
    readonly loadingThread = signal(false);
    /** The session's currently attached datasource ids (durable selection;
     *  optimistically advanced when the pane dispatches a change). */
    readonly attachedIds = signal<string[]>([]);
    /** Eligible datasources shown in the picker: the create-flow eligible
     *  union, minus unattached kb entries (not addable live — v1). */
    readonly pickerDatasources = signal<Datasource[]>([]);
    /** kb entries render frozen: knowledge bindings only rewire on attach. */
    readonly lockedDatasourceIds = computed(() =>
        this.pickerDatasources().filter((ds) => ds.type === 'kb').map((ds) => ds.id),
    );
    /** Thread id the pane last prefilled for (re-prefill on session switch). */
    private prefilledThread: string | null = null;
    /** Desired state actually dispatched last — the diff baseline. */
    private lastApplied: Record<string, unknown> | null = null;
    private applyTimer: ReturnType<typeof setTimeout> | null = null;

    /** The session's current effective config: durable overrides overlaid
     *  with the live signals (which win — they track config.changed acks, so
     *  the sub-groups' "resolved default" is always the running state). */
    readonly liveConfig = computed(() => {
        const live: Record<string, unknown> = {
            llm: {
                ...(this.chat.modelName() ? {model: this.chat.modelName()} : {}),
                ...(this.chat.temperature() != null ? {temperature: this.chat.temperature()} : {}),
            },
            interactive: {
                permission_mode: this.chat.permissionMode(),
                narration_mode: this.chat.narrationMode(),
            },
        };
        return deepMergeConfig(this.threadOverride(), live);
    });

    readonly workspaceTier = computed(() =>
        this.chat.workspaceTier()
        ?? (readConfigPath(this.threadOverride(), 'workspace.backend') as string)
        ?? 'virtual',
    );
    readonly isLiteBackend = computed(() =>
        ['virtual', 'none'].includes(this.workspaceTier()),
    );
    readonly canUpgradeToSandbox = computed(() => this.workspaceTier() === 'virtual');
    readonly canUpgradeToVm = computed(() => {
        if (!['virtual', 'sandbox'].includes(this.workspaceTier())) return false;
        const g = this.capabilities.grants();
        // Fail closed while loading; null = admin/unrestricted. The PDP key
        // is `vm_workspace` (src/core/capability_grants.py) — the server-side
        // upgrade gate enforces the same grant either way.
        return g === null ? true : g?.['vm_workspace'] === true;
    });

    constructor() {
        // The model picker needs the catalog; the chat page never loads it
        // (the retired popover's host did).
        this.modelService.load();

        effect(() => {
            const threadId = this.chat.threadId();
            if (!this.active() || !threadId) return;
            if (this.prefilledThread === threadId) return;
            this.prefilledThread = threadId;
            this.loadThread(threadId);
        });

        this.destroyRef.onDestroy(() => {
            if (this.applyTimer) clearTimeout(this.applyTimer);
        });
    }

    /** Scroll the model control into view (status-chip entry point). */
    scrollToModel(): void {
        this.paneRoot()?.nativeElement
            .querySelector('app-model-group')
            ?.scrollIntoView({block: 'start', behavior: 'smooth'});
    }

    upgrade(tier: 'sandbox' | 'vm'): void {
        this.chat.upgradeWorkspace(tier);
    }

    onSettingsChange(): void {
        if (this.applyTimer) clearTimeout(this.applyTimer);
        this.applyTimer = setTimeout(() => {
            this.applyTimer = null;
            this.applyChanges();
        }, APPLY_DEBOUNCE_MS);
    }

    private loadThread(threadId: string): void {
        this.loadingThread.set(true);
        this.api.getPersistentThread(threadId).subscribe((thread) => {
            this.loadingThread.set(false);
            const metadata = (thread?.['metadata'] ?? {}) as Record<string, unknown>;
            const override = (metadata['config_override'] ?? {}) as Record<string, unknown>;
            this.threadOverride.set(override);
            if (this.chat.workspaceTier() === null) {
                const tier = readConfigPath(override, 'workspace.backend') as string | null;
                if (tier) this.chat.workspaceTier.set(tier);
            }

            const ids = ((metadata['datasource_ids'] ?? []) as string[]).map(String);
            this.attachedIds.set(ids);

            // Prefill the sub-groups' baselines from the effective config
            // (tools group derives its enabled/disabled baseline here; this
            // also clears any pins made while the fetch was in flight — the
            // datasource picker gets the same reset), then anchor the diff
            // baseline to the CONFIG-ONLY state — never to getOverrides(),
            // which would silently absorb a pending pin.
            this.settings()?.prefillFromConfig(this.liveConfig());
            this.settings()?.resetDatasourceSelection();
            this.lastApplied = this.desiredState({});

            // Eligible list = the create-flow union for the thread's projects
            // (per-project failures collapse to [] in the service; attached
            // ids missing from the picker are preserved invisibly in every
            // dispatched set — see canonicalDatasourceIds). Unattached kb
            // entries are hidden: knowledge bindings don't rewire live (v1).
            const projectIds = ((thread?.['project_ids'] ?? []) as string[]).map(String);
            this.api.getEligibleDatasources(projectIds).subscribe((eligible) => {
                this.pickerDatasources.set(
                    (eligible ?? []).filter(
                        (ds) => ds.type !== 'kb' || ids.includes(ds.id),
                    ),
                );
            });
        });
    }

    /** The full desired selection: the picker's checked set plus attached ids
     *  the picker doesn't show (kb hidden entries, revoked-visibility rows,
     *  eligible-fetch failures) — those must survive every dispatch, or an
     *  unrelated toggle would silently detach them. Sorted for stable
     *  comparison. */
    private canonicalDatasourceIds(): string[] {
        const groupIds = this.settings()?.getSelectedDatasourceIds() ?? [];
        const pickerIds = new Set(this.pickerDatasources().map((ds) => ds.id));
        const hidden = this.attachedIds().filter((id) => !pickerIds.has(id));
        return [...new Set([...groupIds, ...hidden])].sort();
    }

    /** Flatten the pane's tracked surface into path → value. Tool groups
     *  normalize to booleans so list-content differences never masquerade as
     *  changes; dispatch re-expands them through the vocabulary mirror. */
    private currentDesiredState(): Record<string, unknown> {
        return this.desiredState(this.settings()?.getOverrides() ?? {});
    }

    private desiredState(overrides: Record<string, unknown>): Record<string, unknown> {
        const config = this.liveConfig();
        const state: Record<string, unknown> = {};
        for (const path of TRACKED_LLM_PATHS) {
            state[path] = readConfigPath(overrides, path) ?? readConfigPath(config, path);
        }
        state['interactive.permission_mode'] =
            readConfigPath(overrides, 'interactive.permission_mode')
            ?? readConfigPath(config, 'interactive.permission_mode');
        state['interactive.narration_mode'] =
            readConfigPath(overrides, 'interactive.narration_mode')
            ?? readConfigPath(config, 'interactive.narration_mode');
        for (const cat of LIVE_TOOL_CATEGORIES) {
            const pinned = readConfigPath(overrides, `tools.${cat.key}`) as unknown[] | null;
            if (pinned !== null) {
                state[`tools.${cat.key}`] = pinned.length > 0;
            } else {
                const current = readConfigPath(config, `tools.${cat.key}`) as unknown[] | null;
                state[`tools.${cat.key}`] = !(Array.isArray(current) && current.length === 0);
            }
        }
        // Canonical joined form so the diff is a plain string compare. The
        // picker's untouched default IS the attached set, so this holds the
        // baseline value until the user actually toggles a datasource.
        state['datasource_ids'] = this.canonicalDatasourceIds().join(',');
        return state;
    }

    private applyChanges(): void {
        if (!this.chat.isConnected()) return;
        const previous = this.lastApplied;
        if (!previous) {
            // Baseline not anchored yet (thread fetch in flight) — retry
            // instead of absorbing the edit into the baseline.
            this.onSettingsChange();
            return;
        }
        const desired = this.currentDesiredState();

        const fragment: Record<string, unknown> = {};
        const llm: Record<string, unknown> = {};
        const tools: Record<string, unknown> = {};

        for (const path of TRACKED_LLM_PATHS) {
            if (desired[path] !== previous[path] && desired[path] != null) {
                llm[path.split('.')[1]] = desired[path];
            }
        }
        for (const cat of LIVE_TOOL_CATEGORIES) {
            const key = `tools.${cat.key}`;
            if (desired[key] !== previous[key]) {
                tools[cat.key] = desired[key] ? [...SESSION_TOOL_GROUP_NAMES[cat.key]] : [];
            }
        }
        if (Object.keys(llm).length) fragment['llm'] = llm;
        if (Object.keys(tools).length) fragment['tools'] = tools;

        // Permission + narration ride their dedicated verbs — they broadcast
        // mode.changed/narration.changed and persist server-side; duplicating
        // them through config.update would double-apply.
        const pm = desired['interactive.permission_mode'];
        if (pm !== previous['interactive.permission_mode'] && typeof pm === 'string') {
            this.chat.setMode(pm as PermissionMode);
        }
        const nm = desired['interactive.narration_mode'];
        if (nm !== previous['interactive.narration_mode'] && typeof nm === 'string') {
            this.chat.setNarrationMode(nm as NarrationMode);
        }

        // Datasources ride the same coalesced frame as a sibling key — the
        // desired FULL selection (never a delta; matches create semantics).
        let datasourceIds: string[] | undefined;
        const dsDesired = desired['datasource_ids'] as string;
        if (dsDesired !== previous['datasource_ids']) {
            datasourceIds = dsDesired === '' ? [] : dsDesired.split(',');
        }

        if (datasourceIds !== undefined) {
            this.chat.updateConfig(fragment, datasourceIds);
            // Advance the durable-selection mirror optimistically (same
            // policy as lastApplied); a rejection error frame leaves the
            // server state unchanged and a pane reopen re-syncs.
            this.attachedIds.set(datasourceIds);
        } else if (Object.keys(fragment).length) {
            this.chat.updateConfig(fragment);
        }
        this.lastApplied = desired;
    }
}
