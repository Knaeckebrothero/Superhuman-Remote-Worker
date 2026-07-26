import {signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {Observable, Subject, of} from 'rxjs';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {ApiService} from '../../core/services/api.service';
import {CapabilitiesService} from '../../core/services/capabilities.service';
import {ModelService} from '../../core/services/model.service';
import {PersistentChatService} from '../../core/services/persistent-chat.service';
import {
  SESSION_TOOL_GROUP_BASE_ENABLED,
  SESSION_TOOL_GROUP_NAMES,
} from '../agent-settings/agent-settings.types';
import {SettingsPaneComponent} from './settings-pane.component';

/**
 * The live settings pane's desired-state diff (live_session_settings.md
 * Slice A): control edits collect through the hosted settings surface, the
 * pane diffs against what was last applied, and dispatches only the delta —
 * permission/narration through their dedicated verbs, everything else through
 * one coalesced config.update.
 */

function createPane(options: {
  override?: Record<string, unknown>;
  grants?: Record<string, unknown> | null;
  attachedIds?: string[];
  eligible?: Array<{id: string; type: string; name: string}>;
  /** Server-resolved tool-group enablement; null models an orchestrator that
   *  predates the endpoint (the pane falls back to its session_base mirror). */
  toolGroups?: Record<string, boolean> | null;
  /** Observable for the tool-groups call, to drive the load-ordering test. */
  toolGroups$?: Observable<Record<string, boolean> | null>;
} = {}) {
  const chat = {
    threadId: signal<string | null>('thread-1'),
    isConnected: signal(true),
    modelName: signal<string | null>('gemma-4-moe'),
    temperature: signal<number | null>(0.7),
    permissionMode: signal('supervised'),
    narrationMode: signal('auto'),
    workspaceTier: signal<string | null>(null),
    workspaceUpgradeInProgress: signal<{tier: string; elapsed?: number} | null>(null),
    updateConfig: vi.fn().mockReturnValue('req-1'),
    setMode: vi.fn(),
    setNarrationMode: vi.fn(),
    upgradeWorkspace: vi.fn(),
  };
  const api = {
    getPersistentThread: vi.fn().mockReturnValue(
      of({
        metadata: {
          config_override: options.override ?? {},
          datasource_ids: options.attachedIds ?? [],
        },
        project_ids: [],
      }),
    ),
    getEligibleDatasources: vi.fn().mockReturnValue(of(options.eligible ?? [])),
    getSessionToolGroups: vi
      .fn()
      .mockReturnValue(options.toolGroups$ ?? of(options.toolGroups ?? null)),
  };
  const capabilities = {grants: signal(options.grants ?? null)};
  const modelService = {load: vi.fn()};

  TestBed.configureTestingModule({
    providers: [
      SettingsPaneComponent,
      {provide: PersistentChatService, useValue: chat},
      {provide: ApiService, useValue: api},
      {provide: CapabilitiesService, useValue: capabilities},
      {provide: ModelService, useValue: modelService},
    ],
  });
  const component = TestBed.inject(SettingsPaneComponent);
  // Pane is open; templates aren't rendered, so stub the input + viewChild.
  Object.defineProperty(component, 'active', {value: () => true});
  const fakeSettings = {
    prefillFromConfig: vi.fn(),
    getOverrides: vi.fn().mockReturnValue({}),
    // Real group defaults its selection to the attached set (via
    // initialSelectedIds); mirror that default here.
    getSelectedDatasourceIds: vi.fn(
      () =>
        (options.attachedIds ?? []).filter((id) =>
          (options.eligible ?? []).some((ds) => ds.id === id),
        ),
    ),
    resetDatasourceSelection: vi.fn(),
  };
  Object.defineProperty(component, 'settings', {value: () => fakeSettings});
  TestBed.tick(); // fire the load effect
  return {component, chat, api, fakeSettings};
}

describe('SettingsPaneComponent apply diff', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.useRealTimers();
    TestBed.resetTestingModule();
  });

  it('prefills from thread metadata and dispatches nothing without edits', () => {
    const {component, chat, fakeSettings} = createPane();
    expect(fakeSettings.prefillFromConfig).toHaveBeenCalled();

    component.onSettingsChange();
    vi.runAllTimers();
    expect(chat.updateConfig).not.toHaveBeenCalled();
    expect(chat.setMode).not.toHaveBeenCalled();
  });

  it('a model pin dispatches exactly the llm delta', () => {
    const {component, chat, fakeSettings} = createPane();
    fakeSettings.getOverrides.mockReturnValue({llm: {model: 'minimax-m3'}});

    component.onSettingsChange();
    vi.runAllTimers();

    expect(chat.updateConfig).toHaveBeenCalledExactlyOnceWith({llm: {model: 'minimax-m3'}});
    expect(chat.setMode).not.toHaveBeenCalled();
  });

  it('permission mode rides its dedicated verb, never config.update', () => {
    const {component, chat, fakeSettings} = createPane();
    fakeSettings.getOverrides.mockReturnValue({interactive: {permission_mode: 'autonomous'}});

    component.onSettingsChange();
    vi.runAllTimers();

    expect(chat.setMode).toHaveBeenCalledExactlyOnceWith('autonomous');
    expect(chat.updateConfig).not.toHaveBeenCalled();
  });

  it('tool toggles send [] to disable and the vocabulary mirror to re-enable', () => {
    // Baseline must have canvas OFF and workflows ON for both directions to
    // diff. The server is the source of that baseline now — an override of
    // `{tools: {canvas: []}}` alone would leave workflows disabled by the
    // session_base default, so "disable workflows" would be a no-op.
    const {component, chat, fakeSettings} = createPane({
      toolGroups: {
        orchestrator: false,
        agent_catalog: false,
        workflows: true,
        canvas: false,
      },
    });
    // Re-enable canvas, disable workflows in one batch.
    fakeSettings.getOverrides.mockReturnValue({
      tools: {canvas: SESSION_TOOL_GROUP_NAMES['canvas'], workflows: []},
    });

    component.onSettingsChange();
    vi.runAllTimers();

    expect(chat.updateConfig).toHaveBeenCalledExactlyOnceWith({
      tools: {
        canvas: SESSION_TOOL_GROUP_NAMES['canvas'],
        workflows: [],
      },
    });
  });

  it('an unset group reads as disabled from the server defaults', () => {
    // The reported bug: config_override says nothing about orchestrator, so
    // the pane used to render it ticked while the agent had zero fleet tools.
    const {fakeSettings} = createPane({
      override: {},
      toolGroups: {
        orchestrator: false,
        agent_catalog: false,
        workflows: false,
        canvas: true,
      },
    });

    const prefilled = fakeSettings.prefillFromConfig.mock.calls[0][0] as {
      tools: Record<string, string[]>;
    };
    expect(prefilled.tools['orchestrator']).toEqual([]);
    expect(prefilled.tools['agent_catalog']).toEqual([]);
    expect(prefilled.tools['workflows']).toEqual([]);
    expect(prefilled.tools['canvas']).toEqual(SESSION_TOOL_GROUP_NAMES['canvas']);
  });

  it('falls back to the session_base mirror when the endpoint is unavailable', () => {
    // Older orchestrator (404) or a failed request → getSessionToolGroups
    // yields null, and the client's own defaults must still be correct.
    const {fakeSettings} = createPane({override: {}, toolGroups: null});

    const prefilled = fakeSettings.prefillFromConfig.mock.calls[0][0] as {
      tools: Record<string, string[]>;
    };
    for (const [group, enabled] of Object.entries(SESSION_TOOL_GROUP_BASE_ENABLED)) {
      expect(prefilled.tools[group]).toEqual(
        enabled ? SESSION_TOOL_GROUP_NAMES[group] : [],
      );
    }
  });

  it('a thread override wins over the server defaults', () => {
    const {fakeSettings} = createPane({
      override: {tools: {canvas: []}},
      toolGroups: {
        orchestrator: false,
        agent_catalog: false,
        workflows: false,
        canvas: true,
      },
    });

    const prefilled = fakeSettings.prefillFromConfig.mock.calls[0][0] as {
      tools: Record<string, string[]>;
    };
    expect(prefilled.tools['canvas']).toEqual([]);
  });

  it('re-enabling a base-disabled group dispatches the vocabulary mirror', () => {
    // This path was dead before the fix: the group rendered ticked, so its
    // baseline was "enabled" and turning it on could never produce a delta.
    const {component, chat, fakeSettings} = createPane({
      override: {},
      toolGroups: {
        orchestrator: false,
        agent_catalog: false,
        workflows: false,
        canvas: true,
      },
    });
    fakeSettings.getOverrides.mockReturnValue({
      tools: {orchestrator: SESSION_TOOL_GROUP_NAMES['orchestrator']},
    });

    component.onSettingsChange();
    vi.runAllTimers();

    expect(chat.updateConfig).toHaveBeenCalledExactlyOnceWith({
      tools: {orchestrator: SESSION_TOOL_GROUP_NAMES['orchestrator']},
    });
  });

  it('late-arriving server defaults never fire a spurious config.update', () => {
    // The race this design exists to prevent: if the tool-group answer landed
    // after the lastApplied anchor, liveConfig() would shift under a stale
    // baseline and the next change would silently disable three tool groups
    // the user never touched.
    const toolGroups$ = new Subject<Record<string, boolean> | null>();
    const {component, chat, fakeSettings} = createPane({override: {}, toolGroups$});

    // Nothing is prefilled or anchored while the join is still open.
    expect(fakeSettings.prefillFromConfig).not.toHaveBeenCalled();
    component.onSettingsChange();
    // Bounded advance, not runAllTimers: with no baseline yet, applyChanges
    // deliberately re-arms the debounce rather than absorbing the edit, so the
    // queue never drains until the fetch lands (400 ms re-arm, self-limiting).
    vi.advanceTimersByTime(2000);
    expect(chat.updateConfig).not.toHaveBeenCalled();

    // Deliberately DIFFERENT from SESSION_TOOL_GROUP_BASE_ENABLED (orchestrator
    // is on here): if this landed after the anchor, the baseline would still
    // hold the mirror's values and the next change would diff against them.
    toolGroups$.next({
      orchestrator: true,
      agent_catalog: false,
      workflows: false,
      canvas: true,
    });
    toolGroups$.complete();

    expect(fakeSettings.prefillFromConfig).toHaveBeenCalled();
    component.onSettingsChange();
    vi.runAllTimers();
    expect(chat.updateConfig).not.toHaveBeenCalled();
  });

  it('a response for a superseded thread is ignored', () => {
    const toolGroups$ = new Subject<Record<string, boolean> | null>();
    const {chat, fakeSettings} = createPane({override: {}, toolGroups$});

    chat.threadId.set('thread-2');
    toolGroups$.next(null);
    toolGroups$.complete();

    expect(fakeSettings.prefillFromConfig).not.toHaveBeenCalled();
  });

  it('rapid edits coalesce into one config.update (one cache invalidation)', () => {
    const {component, chat, fakeSettings} = createPane();
    fakeSettings.getOverrides.mockReturnValue({llm: {model: 'a'}});
    component.onSettingsChange();
    fakeSettings.getOverrides.mockReturnValue({llm: {model: 'a', temperature: 1.2}});
    component.onSettingsChange();

    vi.runAllTimers();

    expect(chat.updateConfig).toHaveBeenCalledExactlyOnceWith({
      llm: {model: 'a', temperature: 1.2},
    });
  });

  it('an unchanged batch after an applied one dispatches nothing new', () => {
    const {component, chat, fakeSettings} = createPane();
    fakeSettings.getOverrides.mockReturnValue({llm: {model: 'a'}});
    component.onSettingsChange();
    vi.runAllTimers();
    expect(chat.updateConfig).toHaveBeenCalledTimes(1);

    component.onSettingsChange();
    vi.runAllTimers();
    expect(chat.updateConfig).toHaveBeenCalledTimes(1);
  });
});

describe('SettingsPaneComponent datasources (Slice B)', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.useRealTimers();
    TestBed.resetTestingModule();
  });

  const PG = {id: 'ds-pg', type: 'postgresql', name: 'PG'};
  const WEB = {id: 'ds-web', type: 'webdav', name: 'Cloud'};

  it('a datasource toggle dispatches the FULL desired set, never a delta', () => {
    const {component, chat, fakeSettings} = createPane({
      attachedIds: ['ds-pg'],
      eligible: [PG, WEB],
    });
    fakeSettings.getSelectedDatasourceIds.mockReturnValue(['ds-pg', 'ds-web']);

    component.onSettingsChange();
    vi.runAllTimers();

    expect(chat.updateConfig).toHaveBeenCalledExactlyOnceWith({}, ['ds-pg', 'ds-web']);
  });

  it('attached ids the picker cannot show survive every dispatch', () => {
    const {component, chat, fakeSettings} = createPane({
      attachedIds: ['ds-pg', 'kb-hidden'],
      eligible: [PG, WEB],
    });
    // Uncheck postgres; kb-hidden never rendered (not eligible/kb-hidden) —
    // dropping it silently would detach it server-side.
    fakeSettings.getSelectedDatasourceIds.mockReturnValue([]);

    component.onSettingsChange();
    vi.runAllTimers();

    expect(chat.updateConfig).toHaveBeenCalledExactlyOnceWith({}, ['kb-hidden']);
  });

  it('a config-only edit sends no datasource_ids at all', () => {
    const {component, chat, fakeSettings} = createPane({
      attachedIds: ['ds-pg'],
      eligible: [PG],
    });
    fakeSettings.getOverrides.mockReturnValue({llm: {model: 'minimax-m3'}});

    component.onSettingsChange();
    vi.runAllTimers();

    expect(chat.updateConfig).toHaveBeenCalledExactlyOnceWith({llm: {model: 'minimax-m3'}});
  });

  it('pairwise preservation: model pin then datasource toggle — each dispatch carries only its own change', () => {
    const {component, chat, fakeSettings} = createPane({
      attachedIds: ['ds-pg'],
      eligible: [PG, WEB],
    });
    fakeSettings.getOverrides.mockReturnValue({llm: {model: 'minimax-m3'}});
    component.onSettingsChange();
    vi.runAllTimers();
    expect(chat.updateConfig).toHaveBeenNthCalledWith(1, {llm: {model: 'minimax-m3'}});

    fakeSettings.getSelectedDatasourceIds.mockReturnValue(['ds-pg', 'ds-web']);
    component.onSettingsChange();
    vi.runAllTimers();
    // The datasource dispatch must not re-send (or reset) the model pin.
    expect(chat.updateConfig).toHaveBeenNthCalledWith(2, {}, ['ds-pg', 'ds-web']);
    expect(chat.updateConfig).toHaveBeenCalledTimes(2);
  });

  it('pairwise preservation: datasource toggle then permission mode — the mode verb fires without a datasource resend', () => {
    const {component, chat, fakeSettings} = createPane({
      attachedIds: [],
      eligible: [PG],
    });
    fakeSettings.getSelectedDatasourceIds.mockReturnValue(['ds-pg']);
    component.onSettingsChange();
    vi.runAllTimers();
    expect(chat.updateConfig).toHaveBeenCalledExactlyOnceWith({}, ['ds-pg']);

    fakeSettings.getOverrides.mockReturnValue({interactive: {permission_mode: 'autonomous'}});
    component.onSettingsChange();
    vi.runAllTimers();
    expect(chat.setMode).toHaveBeenCalledExactlyOnceWith('autonomous');
    expect(chat.updateConfig).toHaveBeenCalledTimes(1);
  });

  it('unattached kb datasources are hidden from the picker; attached ones render locked', () => {
    const KB_ATTACHED = {id: 'kb-1', type: 'kb', name: 'Docs KB'};
    const KB_FOREIGN = {id: 'kb-2', type: 'kb', name: 'Other KB'};
    const {component} = createPane({
      attachedIds: ['kb-1'],
      eligible: [PG, KB_ATTACHED, KB_FOREIGN],
    });

    expect(component.pickerDatasources().map((d) => d.id)).toEqual(['ds-pg', 'kb-1']);
    expect(component.lockedDatasourceIds()).toEqual(['kb-1']);
  });
});

describe('SettingsPaneComponent workspace tier', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('initializes the tier from thread metadata and gates upgrades', () => {
    const {component} = createPane({
      override: {workspace: {backend: 'sandbox'}},
      grants: {vm_workspace: true},
    });
    expect(component.workspaceTier()).toBe('sandbox');
    expect(component.canUpgradeToSandbox()).toBe(false);
    expect(component.canUpgradeToVm()).toBe(true);
  });

  it('denies the VM upgrade without the vm grant; admin (null grants) allows', () => {
    const {component} = createPane({grants: {vm_workspace: false}});
    expect(component.workspaceTier()).toBe('virtual');
    expect(component.canUpgradeToSandbox()).toBe(true);
    expect(component.canUpgradeToVm()).toBe(false);

    TestBed.resetTestingModule();
    const {component: admin} = createPane({grants: null});
    expect(admin.canUpgradeToVm()).toBe(true);
  });

  it('vm tier offers no further upgrades (upgrade-only, no downgrades)', () => {
    const {component} = createPane({
      override: {workspace: {backend: 'vm'}},
      grants: null,
    });
    expect(component.canUpgradeToSandbox()).toBe(false);
    expect(component.canUpgradeToVm()).toBe(false);
  });
});
