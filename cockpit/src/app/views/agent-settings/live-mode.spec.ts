import {describe, expect, it} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {of} from 'rxjs';
import {TranslocoService} from '@jsverse/transloco';
import {ToolsGroupComponent} from './tools-group.component';
import {ExecutionGroupComponent} from './execution-group.component';
import {
  LIVE_TOOL_CATEGORIES,
  SESSION_TOOL_GROUP_BASE_ENABLED,
  SESSION_TOOL_GROUP_NAMES,
  toolGroupDefaultsConfig,
} from './agent-settings.types';

/**
 * Live-mode behavior of the shared settings surface
 * (docs/done/2026-07-16-live-session-settings.md, Slice A): only the four validated
 * closed tool groups render, re-enables carry the closed-vocabulary lists,
 * and the live-only narration control rides the interactive fragment.
 */

/** Stub an input() signal on an instance (templates aren't rendered here). */
function stubInput(component: object, name: string, value: unknown): void {
  Object.defineProperty(component, name, {value: () => value});
}

function createToolsGroup(mode: string): ToolsGroupComponent {
  const injector = Injector.create({providers: []});
  const component = runInInjectionContext(injector, () => new ToolsGroupComponent());
  stubInput(component, 'mode', mode);
  return component;
}

function createExecutionGroup(mode: string): ExecutionGroupComponent {
  const injector = Injector.create({
    providers: [
      {
        provide: TranslocoService,
        useValue: {
          translate: (key: string) => key,
          langChanges$: of('en'),
          getActiveLang: () => 'en',
        },
      },
    ],
  });
  const component = runInInjectionContext(injector, () => new ExecutionGroupComponent());
  stubInput(component, 'mode', mode);
  return component;
}

describe('LIVE_TOOL_CATEGORIES', () => {
  it('contains exactly the four validated closed groups', () => {
    expect(LIVE_TOOL_CATEGORIES.map((c) => c.key).sort()).toEqual([
      'agent_catalog',
      'canvas',
      'orchestrator',
      'workflows',
    ]);
  });
});

describe('ToolsGroupComponent live mode', () => {
  it('renders only the four validated groups (the other 8 session categories no-op live)', () => {
    const component = createToolsGroup('live');
    expect(component.categories()).toEqual(LIVE_TOOL_CATEGORIES);
  });

  it('session mode still renders the full session list', () => {
    const component = createToolsGroup('session');
    expect(component.categories().length).toBeGreaterThan(LIVE_TOOL_CATEGORIES.length);
  });

  it('re-enable payload uses the closed-vocabulary mirror, not mode-base lists', () => {
    const component = createToolsGroup('live');
    // Baseline: canvas currently disabled (as the live config would show).
    component.prefillFromConfig({tools: {canvas: []}});
    // User re-enables it.
    component.toggleCategory('canvas');

    const overrides = component.getOverrides() as {tools?: Record<string, string[]>};
    expect(overrides.tools?.['canvas']).toEqual(SESSION_TOOL_GROUP_NAMES['canvas']);
  });

  it('disable payload is the empty list', () => {
    const component = createToolsGroup('live');
    component.prefillFromConfig({tools: {}});
    component.toggleCategory('workflows');

    const overrides = component.getOverrides() as {tools?: Record<string, string[]>};
    expect(overrides.tools?.['workflows']).toEqual([]);
  });
});

describe('toolGroupDefaultsConfig', () => {
  it('expands the session_base mirror when the server answer is unavailable', () => {
    const config = toolGroupDefaultsConfig(null) as {tools: Record<string, string[]>};

    expect(Object.keys(config.tools).sort()).toEqual([
      'agent_catalog',
      'canvas',
      'orchestrator',
      'workflows',
    ]);
    for (const [group, enabled] of Object.entries(SESSION_TOOL_GROUP_BASE_ENABLED)) {
      expect(config.tools[group]).toEqual(
        enabled ? SESSION_TOOL_GROUP_NAMES[group] : [],
      );
    }
  });

  it('a partial server answer falls back per key, and unknown keys are ignored', () => {
    const config = toolGroupDefaultsConfig({
      orchestrator: true,
      not_a_group: true,
    }) as {tools: Record<string, string[]>};

    expect(config.tools['orchestrator']).toEqual(SESSION_TOOL_GROUP_NAMES['orchestrator']);
    // Unanswered groups keep the base default (disabled), and the bogus key
    // never reaches the fragment.
    expect(config.tools['workflows']).toEqual([]);
    expect(config.tools['canvas']).toEqual(SESSION_TOOL_GROUP_NAMES['canvas']);
    expect(config.tools['not_a_group']).toBeUndefined();
  });
});

describe('ExecutionGroupComponent live mode', () => {
  it('narration override rides interactive.narration_mode', () => {
    const component = createExecutionGroup('live');
    stubInput(component, 'config', {interactive: {narration_mode: 'auto'}});
    component.onNarrationModeChange('silent');

    expect(component.getOverrides()).toEqual({
      interactive: {narration_mode: 'silent'},
    });
  });

  it('selecting the resolved narration value clears the pin', () => {
    const component = createExecutionGroup('live');
    stubInput(component, 'config', {interactive: {narration_mode: 'auto'}});
    component.onNarrationModeChange('silent');
    component.onNarrationModeChange('auto');

    expect(component.narrationMode()).toBeNull();
    expect(component.getOverrides()).toEqual({});
  });

  it('permission + narration combine into one interactive fragment', () => {
    const component = createExecutionGroup('live');
    stubInput(component, 'config', {});
    component.onPermissionModeChange('autonomous');
    component.onNarrationModeChange('verbose');

    expect(component.getOverrides()).toEqual({
      interactive: {permission_mode: 'autonomous', narration_mode: 'verbose'},
    });
  });
});
