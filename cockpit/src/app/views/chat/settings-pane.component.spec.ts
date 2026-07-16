import {signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {of} from 'rxjs';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {ApiService} from '../../core/services/api.service';
import {CapabilitiesService} from '../../core/services/capabilities.service';
import {ModelService} from '../../core/services/model.service';
import {PersistentChatService} from '../../core/services/persistent-chat.service';
import {SESSION_TOOL_GROUP_NAMES} from '../agent-settings/agent-settings.types';
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
      of({metadata: {config_override: options.override ?? {}, datasource_ids: []}}),
    ),
    getDatasources: vi.fn().mockReturnValue(of([])),
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
    const {component, chat, fakeSettings} = createPane({
      override: {tools: {canvas: []}},
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

describe('SettingsPaneComponent workspace tier', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('initializes the tier from thread metadata and gates upgrades', () => {
    const {component} = createPane({
      override: {workspace: {backend: 'sandbox'}},
      grants: {vm: true},
    });
    expect(component.workspaceTier()).toBe('sandbox');
    expect(component.canUpgradeToSandbox()).toBe(false);
    expect(component.canUpgradeToVm()).toBe(true);
  });

  it('denies the VM upgrade without the vm grant; admin (null grants) allows', () => {
    const {component} = createPane({grants: {vm: false}});
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
