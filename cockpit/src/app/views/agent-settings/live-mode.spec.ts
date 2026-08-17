import {describe, expect, it} from 'vitest';
import {Injector, runInInjectionContext, signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {of} from 'rxjs';
import {TranslocoService} from '@jsverse/transloco';
import {ToolsGroupComponent} from './tools-group.component';
import {ExecutionGroupComponent} from './execution-group.component';
import {UserService} from '../../core/services/user.service';
import {SESSION_TOOL_CATEGORIES} from './agent-settings.types';
import type {SessionToolGroupsResponse} from '../../core/services/api.service';

/**
 * Live-mode behavior of the shared settings surface
 * (knowledge-history/done/2026-07-16-live-session-settings.md, Slice A).
 *
 * The tool half of this changed shape: live mode used to render four
 * hand-picked closed groups and re-enable them by echoing a mirrored tool-name
 * list. It now renders whatever the resolved read returns and re-enables with
 * a policy the write boundary expands against the registry. The narration
 * assertions below are unchanged.
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

/**
 * Built under TestBed, not a bare Injector: the component owns the workspace
 * backend selector, whose VM-eligibility guard is an effect() and so needs a
 * ChangeDetectionScheduler.
 */
function createExecutionGroup(mode: string): ExecutionGroupComponent {
  TestBed.configureTestingModule({
    providers: [
      ExecutionGroupComponent,
      {
        provide: UserService,
        useValue: {currentUser: signal(null)},
      },
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
  const component = TestBed.inject(ExecutionGroupComponent);
  stubInput(component, 'mode', mode);
  return component;
}

/** Attach a resolved answer to a component built outside a template. */
function withResolved(
  component: ToolsGroupComponent,
  categories: Record<string, {state: 'on' | 'off' | 'unavailable'; settable?: boolean; reason?: string | null}>,
  enumerateOnly?: Record<string, string[]>,
): ToolsGroupComponent {
  const response: SessionToolGroupsResponse = {
    thread_id: 't1',
    source: 'resolved',
    origin: 'agent',
    observed_at: '2026-08-03T10:00:00Z',
    enumerate_only: enumerateOnly ?? {shell: ['run_command', 'shell_read']},
    tool_groups: {},
    categories: Object.fromEntries(
      Object.entries(categories).map(([key, value]) => [
        key,
        {
          state: value.state,
          settable: value.settable ?? value.state !== 'unavailable',
          reason: value.reason ?? null,
          decided_by: 'base',
          tools: value.state === 'on' ? ['x'] : [],
        },
      ]),
    ),
  };
  stubInput(component, 'resolved', response);
  return component;
}

describe('ToolsGroupComponent live mode', () => {
  it('renders every category the answer returns, not a hand-picked four', () => {
    // The live pane showed 4 of 25 because that was all its transport carried.
    // Filtering a complete answer down to a curated list is the same untruth.
    const component = withResolved(createToolsGroup('live'), {
      canvas: {state: 'on'},
      shell: {state: 'unavailable', reason: 'requires the shell_tools capability grant'},
      knowledge: {state: 'off'},
      unclassified: {state: 'on'},
    });
    expect(component.rows().map((r) => r.key).sort()).toEqual([
      'canvas',
      'knowledge',
      'shell',
      'unclassified',
    ]);
  });

  it('with no answer the live pane renders NOTHING, and asserts nothing', () => {
    // Not "the static list, as before". The live pane has no config to derive
    // a baseline from — a stock session's config_override carries no `tools`
    // key — so the static list came out twelve categories ALL TICKED, six of
    // which ship `[]` in session_base, with every switch dead because the
    // pane's dispatch is keyed off the resolved answer. An empty section under
    // an explicit "could not be read" banner is the only honest fallback here.
    const component = createToolsGroup('live');
    expect(component.rows()).toEqual([]);
  });

  it('a CREATION surface with no answer does still fall back to its list', () => {
    // There the baseline comes from prefillFromConfig, so the static list is
    // as honest as it ever was and the form stays usable.
    const component = createToolsGroup('session');
    expect(component.rows().map((r) => r.key)).toEqual(
      SESSION_TOOL_CATEGORIES.map((c) => c.key),
    );
    expect(SESSION_TOOL_CATEGORIES.map((c) => c.key)).toContain('job_control');
    expect(SESSION_TOOL_CATEGORIES.map((c) => c.key)).toContain('job_inspection');
  });

  it('re-enable payload is a POLICY, never names copied out of another layer', () => {
    const component = withResolved(createToolsGroup('live'), {canvas: {state: 'off'}});
    component.toggleCategory('canvas');

    expect(component.getOverrides()).toEqual({tools: {canvas: true}});
  });

  it('re-enabling shell sends the enumeration the server served for it', () => {
    // `shell` refuses `true` at the write boundary, and this is the whole
    // reason the response carries `enumerate_only`.
    const component = withResolved(createToolsGroup('live'), {shell: {state: 'off'}});
    component.toggleCategory('shell');

    expect(component.getOverrides()).toEqual({
      tools: {shell: {only: ['run_command', 'shell_read']}},
    });
  });

  it('disable payload is the empty list', () => {
    const component = withResolved(createToolsGroup('live'), {workflows: {state: 'on'}});
    component.toggleCategory('workflows');

    expect(component.getOverrides()).toEqual({tools: {workflows: []}});
  });

  it('an untouched surface emits nothing at all', () => {
    const component = withResolved(createToolsGroup('live'), {
      canvas: {state: 'on'},
      knowledge: {state: 'off'},
      shell: {state: 'unavailable', reason: 'no grant'},
    });
    expect(component.getOverrides()).toEqual({});
  });

  it('an unsettable category is never written, in either direction', () => {
    const component = withResolved(createToolsGroup('live'), {
      shell: {state: 'unavailable', reason: 'requires the shell_tools capability grant'},
    });
    component.toggleCategory('shell');
    expect(component.getOverrides()).toEqual({});
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

  // Selecting the resolved value is a choice, not a retraction — it now pins.
  // Live mode leans on this: PATCHing the value you can see is the only way to
  // state "keep this one" against an expert edit landing underneath you.
  it('selecting the resolved narration value pins it', () => {
    const component = createExecutionGroup('live');
    stubInput(component, 'config', {interactive: {narration_mode: 'auto'}});
    component.onNarrationModeChange('silent');
    component.onNarrationModeChange('auto');

    expect(component.narrationMode()).toBe('auto');
    expect(component.getOverrides()).toEqual({
      interactive: {narration_mode: 'auto'},
    });
  });

  it('resetAll is what clears a pin back to inherit', () => {
    const component = createExecutionGroup('live');
    stubInput(component, 'config', {interactive: {narration_mode: 'auto'}});
    component.onNarrationModeChange('auto');
    component.resetAll();

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
