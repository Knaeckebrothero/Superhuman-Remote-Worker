import {signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {Observable, Subject, of} from 'rxjs';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {ApiService} from '../../core/services/api.service';
import {CapabilitiesService} from '../../core/services/capabilities.service';
import {ModelService} from '../../core/services/model.service';
import {PersistentChatService} from '../../core/services/persistent-chat.service';
import type {
  SessionToolCategory,
  SessionToolGroupsResponse,
} from '../../core/services/api.service';
import {SettingsPaneComponent} from './settings-pane.component';

/** Build the resolved tool-groups answer the pane now reads. */
function toolsetAnswer(
  states: Record<string, 'on' | 'off' | 'unavailable'>,
  over: Partial<SessionToolGroupsResponse> = {},
): SessionToolGroupsResponse {
  const categories: Record<string, SessionToolCategory> = {};
  for (const [key, state] of Object.entries(states)) {
    categories[key] = {
      state,
      settable: state !== 'unavailable',
      reason: state === 'unavailable' ? 'the agent bound none' : null,
      decided_by: 'base',
      tools: state === 'on' ? ['x'] : [],
    };
  }
  return {
    thread_id: 'thread-1',
    source: 'resolved',
    origin: 'agent',
    observed_at: '2026-08-03T10:00:00Z',
    enumerate_only: {shell: ['run_command', 'shell_read']},
    tool_groups: null,
    categories,
    ...over,
  };
}

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
  /** The resolved toolset; null models an orchestrator that predates the
   *  endpoint (the tools surface then degrades to its static list). */
  toolGroups?: SessionToolGroupsResponse | null;
  /** Observable for the tool-groups call, to drive the load-ordering test. */
  toolGroups$?: Observable<SessionToolGroupsResponse | null>;
  /** Thread ids whose metadata differs, for the thread-switch test. */
  threadsById?: Record<string, Record<string, unknown>>;
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
    prefillFromResolvedToolset: vi.fn(),
    hasToolEdits: vi.fn().mockReturnValue(false),
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

  it('tool toggles send [] to disable and a policy to re-enable', () => {
    // Baseline must have canvas OFF and workflows ON for both directions to
    // diff. The AGENT's answer is that baseline now — not a config merge,
    // which cannot see the runtime injection layer or the capability gate.
    const {component, chat, fakeSettings} = createPane({
      toolGroups: toolsetAnswer({
        orchestrator: 'off',
        agent_catalog: 'off',
        workflows: 'on',
        canvas: 'off',
      }),
    });
    // Re-enable canvas, disable workflows in one batch.
    fakeSettings.getOverrides.mockReturnValue({
      tools: {canvas: true, workflows: []},
    });

    component.onSettingsChange();
    vi.runAllTimers();

    expect(chat.updateConfig).toHaveBeenCalledExactlyOnceWith({
      tools: {canvas: true, workflows: []},
    });
  });

  it('the tools baseline is anchored to the AGENT answer, not the config', () => {
    // The reported bug: config_override says nothing about orchestrator, so
    // the pane used to render it ticked while the agent had zero fleet tools.
    const {fakeSettings} = createPane({
      override: {},
      toolGroups: toolsetAnswer({
        orchestrator: 'off',
        agent_catalog: 'off',
        workflows: 'off',
        canvas: 'on',
      }),
    });

    const anchored = fakeSettings.prefillFromResolvedToolset.mock.calls[0][0] as Record<
      string,
      SessionToolCategory
    >;
    expect(anchored['orchestrator'].state).toBe('off');
    expect(anchored['canvas'].state).toBe('on');
    // And the config prefill no longer carries a synthesised tools layer.
    const prefilled = fakeSettings.prefillFromConfig.mock.calls[0][0] as Record<string, unknown>;
    expect(prefilled['tools']).toBeUndefined();
  });

  it('the surface degrades honestly when the endpoint is unavailable', () => {
    // Older orchestrator (404) or a failed request → null. The pane must not
    // invent a baseline; it anchors nothing and dispatches no tool fragment.
    const {component, chat, fakeSettings} = createPane({override: {}, toolGroups: null});

    expect(fakeSettings.prefillFromResolvedToolset).not.toHaveBeenCalled();
    fakeSettings.getOverrides.mockReturnValue({llm: {model: 'a'}});
    component.onSettingsChange();
    vi.runAllTimers();
    expect(chat.updateConfig).toHaveBeenCalledExactlyOnceWith({llm: {model: 'a'}});
  });

  it('every category the answer returns is diffable, not just four', () => {
    // The live pane could only carry canvas/orchestrator/agent_catalog/
    // workflows. `knowledge` is one of the eight it could not express.
    const {component, chat, fakeSettings} = createPane({
      toolGroups: toolsetAnswer({canvas: 'on', knowledge: 'on', git: 'off'}),
    });
    fakeSettings.getOverrides.mockReturnValue({tools: {knowledge: []}});

    component.onSettingsChange();
    vi.runAllTimers();

    expect(chat.updateConfig).toHaveBeenCalledExactlyOnceWith({tools: {knowledge: []}});
  });

  it('an unavailable category is never dispatched, however the surface reports it', () => {
    // The server refused. Writing config against a grant or a workspace tier
    // produces a fragment the PDP will reject and changes no binding.
    const {component, chat, fakeSettings} = createPane({
      toolGroups: toolsetAnswer({shell: 'unavailable', canvas: 'on'}),
    });
    fakeSettings.getOverrides.mockReturnValue({tools: {shell: true}});

    component.onSettingsChange();
    vi.runAllTimers();

    expect(chat.updateConfig).not.toHaveBeenCalled();
  });

  it('re-enabling a base-disabled group dispatches a policy', () => {
    // This path was dead before the fix: the group rendered ticked, so its
    // baseline was "enabled" and turning it on could never produce a delta.
    const {component, chat, fakeSettings} = createPane({
      override: {},
      toolGroups: toolsetAnswer({
        orchestrator: 'off',
        agent_catalog: 'off',
        workflows: 'off',
        canvas: 'on',
      }),
    });
    fakeSettings.getOverrides.mockReturnValue({tools: {orchestrator: true}});

    component.onSettingsChange();
    vi.runAllTimers();

    expect(chat.updateConfig).toHaveBeenCalledExactlyOnceWith({
      tools: {orchestrator: true},
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

    // If this landed after the anchor, the baseline would hold a different
    // set from what the surface renders and the next change would diff
    // against it.
    toolGroups$.next(
      toolsetAnswer({
        orchestrator: 'on',
        agent_catalog: 'off',
        workflows: 'off',
        canvas: 'on',
      }),
    );
    toolGroups$.complete();

    expect(fakeSettings.prefillFromConfig).toHaveBeenCalled();
    component.onSettingsChange();
    vi.runAllTimers();
    expect(chat.updateConfig).not.toHaveBeenCalled();
  });

  it('a thread switch drops the diff baseline BEFORE the fetch', () => {
    // The race: `lastApplied` used to survive `loadThread`. For the whole
    // request window — sub-second once, now up to 8s because the read probes
    // the agent pod — an edit would diff the NEW session's desired state
    // against the OLD session's baseline and dispatch the difference into the
    // new session. Concretely: thread-1 has canvas ON, thread-2 has it OFF, so
    // a stale baseline turns an unrelated model pin into "disable canvas".
    const toolGroups$ = new Subject<SessionToolGroupsResponse | null>();
    const {component, chat, api, fakeSettings} = createPane({
      toolGroups: toolsetAnswer({canvas: 'on'}),
    });
    // Baseline for thread-1 is anchored.
    fakeSettings.getOverrides.mockReturnValue({llm: {model: 'a'}});
    component.onSettingsChange();
    vi.runAllTimers();
    expect(chat.updateConfig).toHaveBeenCalledTimes(1);

    // Switch threads; the new read is still in flight.
    api.getSessionToolGroups.mockReturnValue(toolGroups$);
    chat.threadId.set('thread-2');
    TestBed.tick();

    // An edit lands inside the window.
    fakeSettings.getOverrides.mockReturnValue({llm: {model: 'b'}});
    component.onSettingsChange();
    vi.advanceTimersByTime(2000);
    expect(chat.updateConfig).toHaveBeenCalledTimes(1);

    // Only once the new thread's answer lands does anything dispatch, and it
    // dispatches against the NEW baseline.
    toolGroups$.next(toolsetAnswer({canvas: 'off'}));
    toolGroups$.complete();
    vi.runAllTimers();
    expect(chat.updateConfig).toHaveBeenCalledTimes(2);
    expect(chat.updateConfig).toHaveBeenNthCalledWith(2, {llm: {model: 'b'}});
  });

  it('a thread switch drops the previous thread config too', () => {
    // `threadOverride` had the same shape of bug: liveConfig() kept serving
    // the previous session's durable config while the pane already pointed
    // elsewhere, so every control read from the wrong session for the window.
    const toolGroups$ = new Subject<SessionToolGroupsResponse | null>();
    const {component, chat, api} = createPane({
      override: {workspace: {backend: 'vm'}},
      attachedIds: ['ds-pg'],
    });
    expect(component.workspaceTier()).toBe('vm');

    chat.workspaceTier.set(null);
    api.getSessionToolGroups.mockReturnValue(toolGroups$);
    chat.threadId.set('thread-2');
    TestBed.tick();

    expect(component.workspaceTier()).toBe('virtual');
    expect(component.attachedIds()).toEqual([]);
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
