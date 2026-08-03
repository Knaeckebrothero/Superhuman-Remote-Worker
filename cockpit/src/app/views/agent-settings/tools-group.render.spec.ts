import {beforeAll, describe, expect, it} from 'vitest';
import {ɵresolveComponentResources} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {TranslocoTestingModule} from '@jsverse/transloco';
import en from '../../../assets/i18n/en.json';
import type {
  SessionToolCategory,
  SessionToolGroupsResponse,
} from '../../core/services/api.service';
import {ToolsGroupComponent} from './tools-group.component';

/**
 * What the user actually SEES.
 *
 * The three-state control is the deliverable, and a control that resolves its
 * copy through transloco can pass every logic test while rendering
 * `agentSettings.toolCategories.mcp.label` on screen — AOT does not catch a
 * missing key, and neither does a spec that only reads component state. So
 * these mount the component, run change detection, and assert on RESOLVED
 * text: the reason sentence, the provenance headline, the labels, and the
 * absence of any raw key.
 */

function cat(over: Partial<SessionToolCategory> = {}): SessionToolCategory {
  return {state: 'on', settable: true, reason: null, decided_by: 'base', tools: [], ...over};
}

function response(over: Partial<SessionToolGroupsResponse> = {}): SessionToolGroupsResponse {
  return {
    thread_id: 't1',
    source: 'resolved',
    origin: 'agent',
    observed_at: '2026-08-03T10:00:00Z',
    prediction_reason: null,
    degraded_reason: null,
    enumerate_only: {shell: ['cancel_command', 'run_command', 'shell_execute', 'shell_read']},
    tool_groups: {},
    categories: {},
    ...over,
  };
}

/**
 * Mount with `mode` and `resolved` replaced directly.
 *
 * Signal inputs cannot be set through `setInput()` in this pipeline — the repo
 * does not run components through ngtsc for vitest, so `ɵcmp.inputs` is empty
 * and every binding throws NG0303. Same workaround, and same reason, as
 * model-group.component.spec.ts.
 */
function mount(options: {
  mode?: string;
  resolved?: SessionToolGroupsResponse | null;
  gatedCapabilities?: Record<string, unknown> | null;
} = {}) {
  TestBed.configureTestingModule({
    imports: [
      ToolsGroupComponent,
      TranslocoTestingModule.forRoot({
        langs: {en},
        translocoConfig: {availableLangs: ['en'], defaultLang: 'en'},
      }),
    ],
  });
  const fixture = TestBed.createComponent(ToolsGroupComponent);
  const instance = fixture.componentInstance;
  Object.defineProperty(instance, 'mode', {value: () => options.mode ?? 'session'});
  Object.defineProperty(instance, 'resolved', {value: () => options.resolved ?? null});
  Object.defineProperty(instance, 'gatedCapabilities', {
    value: () => options.gatedCapabilities ?? null,
  });
  fixture.detectChanges();
  return fixture;
}

function text(fixture: {nativeElement: unknown}): string {
  return ((fixture.nativeElement as HTMLElement).textContent ?? '').replace(/\s+/g, ' ');
}

function rows(fixture: {nativeElement: unknown}): HTMLElement[] {
  return Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('.tool-toggle'));
}

function rowFor(fixture: {nativeElement: unknown}, label: string): HTMLElement {
  const found = rows(fixture).find((row) => (row.textContent ?? '').includes(label));
  if (!found) throw new Error(`no row containing ${label!}; saw: ${text(fixture)}`);
  return found;
}

describe('ToolsGroupComponent rendering', () => {
  beforeAll(async () => {
    // The component declares no external templateUrl/styleUrls, but the shared
    // icon component in its imports is resolved the same way the other mounted
    // specs in this folder do it.
    await ɵresolveComponentResources(() => Promise.resolve(''));
  });

  it('renders EVERY category the answer returns, not a curated four', () => {
    // The live pane showed 4 of 25 because that was all its transport carried.
    const fixture = mount({
      mode: 'live',
      resolved: response({
        categories: {
          canvas: cat(),
          orchestrator: cat({state: 'off'}),
          shell: cat({state: 'unavailable', settable: false, reason: 'requires the shell_tools capability grant'}),
          knowledge: cat({state: 'off'}),
          sql: cat({state: 'unavailable', settable: false, reason: 'a postgresql datasource decides'}),
        },
      }),
    });
    expect(rows(fixture)).toHaveLength(5);
    const body = text(fixture);
    expect(body).toContain('Canvas');
    expect(body).toContain('Fleet Management');
    expect(body).toContain('Shell');
    expect(body).toContain('Knowledge');
    expect(body).toContain('SQL');
  });

  it("renders the server's reason sentence, verbatim, next to the control", () => {
    const fixture = mount({
      resolved: response({
        categories: {
          shell: cat({
            state: 'unavailable',
            settable: false,
            reason: 'requires the shell_tools capability grant',
          }),
        },
      }),
    });
    const row = rowFor(fixture, 'Shell');
    expect(row.querySelector('.tool-toggle-reason')?.textContent?.trim()).toBe(
      'requires the shell_tools capability grant',
    );
  });

  it('an unavailable row has NO checkbox — three states, not a box that lies', () => {
    const fixture = mount({
      resolved: response({
        categories: {
          shell: cat({state: 'unavailable', settable: false, reason: 'this workspace tier has no shell'}),
          canvas: cat({state: 'on'}),
        },
      }),
    });
    const shell = rowFor(fixture, 'Shell');
    expect(shell.querySelector('input[type="checkbox"]')).toBeNull();
    expect(shell.querySelector('.tool-state-blocked')).not.toBeNull();
    expect(shell.classList.contains('unavailable')).toBe(true);

    const canvas = rowFor(fixture, 'Canvas');
    const box = canvas.querySelector('input[type="checkbox"]') as HTMLInputElement;
    expect(box).not.toBeNull();
    expect(box.checked).toBe(true);
  });

  it('an `off` row renders an unchecked, live checkbox', () => {
    const fixture = mount({resolved: response({categories: {knowledge: cat({state: 'off'})}})});
    const box = rowFor(fixture, 'Knowledge').querySelector(
      'input[type="checkbox"]',
    ) as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect(box.disabled).toBe(false);
  });

  it('a measurement says so, in words, above the rows', () => {
    const fixture = mount({resolved: response({categories: {canvas: cat()}})});
    const banner = (fixture.nativeElement as HTMLElement).querySelector('.toolset-provenance');
    expect(banner?.getAttribute('data-trust')).toBe('measured');
    expect(banner?.textContent).toContain('Live from the running agent');
  });

  it('a PREDICTION says so, and carries the server sentence — never rendered as fact', () => {
    const fixture = mount({
      resolved: response({
        origin: 'prediction',
        observed_at: null,
        prediction_reason: 'no agent exists for an unsaved session',
        categories: {canvas: cat({tools: ['get_canvas']})},
      }),
    });
    const banner = (fixture.nativeElement as HTMLElement).querySelector('.toolset-provenance');
    expect(banner?.getAttribute('data-trust')).toBe('predicted');
    expect(banner?.textContent).toContain('Predicted, not measured');
    expect(banner?.textContent).toContain('no agent exists for an unsaved session');
    // And the per-row count is labelled a forecast too, not a fact.
    expect(rowFor(fixture, 'Canvas').textContent).toContain('1 predicted');
  });

  it('agent_partial reads as a measurement and shows what it is missing', () => {
    // The common path against the deployed fleet image. Rendering it as a
    // forecast would tell the user their live session is a guess.
    const fixture = mount({
      resolved: response({
        origin: 'agent_partial',
        observed_at: null,
        degraded_reason: 'this agent image predates GET /session/toolset',
        categories: {canvas: cat({tools: ['get_canvas', 'set_canvas']})},
      }),
    });
    const banner = (fixture.nativeElement as HTMLElement).querySelector('.toolset-provenance');
    expect(banner?.getAttribute('data-trust')).toBe('measured_partial');
    expect(banner?.textContent).toContain('Live from the running agent');
    expect(banner?.textContent).toContain('partial answer');
    expect(banner?.textContent).toContain('predates GET /session/toolset');
    expect(rowFor(fixture, 'Canvas').textContent).toContain('2 bound');
  });

  it('no answer at all is labelled as such, and the static list still renders', () => {
    const fixture = mount({resolved: null});
    const banner = (fixture.nativeElement as HTMLElement).querySelector('.toolset-provenance');
    expect(banner?.getAttribute('data-trust')).toBe('unknown');
    expect(banner?.textContent).toContain('The resolved toolset could not be read');
    expect(rows(fixture).length).toBeGreaterThan(0);
  });

  it('no rendered string is an unresolved transloco key', () => {
    // A missing key renders as the key, and AOT does not catch it. Every
    // category the endpoint can return is exercised here, including the ones
    // the twelve-row form never showed.
    const categories: Record<string, SessionToolCategory> = {};
    for (const key of [
      'research', 'browser_direct', 'citation', 'shell', 'communication', 'delegation',
      'canvas', 'orchestrator', 'agent_catalog', 'workflows', 'knowledge', 'git',
      'workspace', 'core', 'session_task', 'product_help', 'evaluation', 'loop',
      'sql', 'mongodb', 'graph', 'webdav', 'email', 'repo', 'mcp', 'unclassified',
    ]) {
      categories[key] = cat({state: 'off'});
    }
    const fixture = mount({resolved: response({categories})});
    expect(rows(fixture)).toHaveLength(26);
    expect(text(fixture)).not.toContain('agentSettings.');
    expect(text(fixture)).not.toContain('grants.');
  });

  it('a category the cockpit has never heard of renders humanised, not as a raw key', () => {
    const fixture = mount({
      resolved: response({categories: {some_future_thing: cat({state: 'off'})}}),
    });
    const body = text(fixture);
    expect(body).toContain('Some Future Thing');
    expect(body).not.toContain('agentSettings.toolCategories.some_future_thing');
  });

  it('the grant gate renders UNAVAILABLE even when the server answered settable', () => {
    // The server's grant lookup fails OPEN on error, so the client-side gate
    // stays as a belt. It must reach the same third state, not a clickable box
    // that greys: a box the user can tick is a promise the PDP will refuse.
    const fixture = mount({
      resolved: response({categories: {shell: cat({state: 'off'})}}),
      gatedCapabilities: {shell_tools: false},
    });
    const row = rowFor(fixture, 'Shell');
    expect(row.classList.contains('disabled')).toBe(true);
    expect(row.classList.contains('unavailable')).toBe(true);
    expect(row.querySelector('input[type="checkbox"]')).toBeNull();
    expect(row.querySelector('.tool-toggle-reason')?.textContent).toBeTruthy();
    expect(row.querySelector('.tool-toggle-reason')?.textContent).not.toContain('grants.');
  });

  it('a grant-blocked category is never written, however the row is clicked', () => {
    const fixture = mount({
      resolved: response({categories: {shell: cat({state: 'off'}), canvas: cat({state: 'on'})}}),
      gatedCapabilities: {shell_tools: false},
    });
    fixture.componentInstance.toggleCategory('shell');
    fixture.detectChanges();
    expect(fixture.componentInstance.getOverrides()).toEqual({});
  });

  it('clicking a settable box repaints the row and emits the diff', () => {
    const fixture = mount({
      resolved: response({
        categories: {canvas: cat({state: 'on'}), shell: cat({state: 'off'})},
      }),
    });
    fixture.componentInstance.prefillFromResolved(fixture.componentInstance.resolved()!.categories!);
    fixture.detectChanges();

    const box = rowFor(fixture, 'Shell').querySelector(
      'input[type="checkbox"]',
    ) as HTMLInputElement;
    box.click();
    fixture.detectChanges();

    expect(
      (rowFor(fixture, 'Shell').querySelector('input[type="checkbox"]') as HTMLInputElement)
        .checked,
    ).toBe(true);
    // Shell refuses `true`, so the payload is the enumeration the server served.
    expect(fixture.componentInstance.getOverrides()).toEqual({
      tools: {shell: {only: ['cancel_command', 'run_command', 'shell_execute', 'shell_read']}},
    });
  });
});
