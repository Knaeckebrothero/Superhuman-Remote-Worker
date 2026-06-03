import {describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext, signal} from '@angular/core';
import {of} from 'rxjs';
import {AdminConfigComponent} from './admin-config.component';
import {AdminConfigService} from '../../../core/services/admin-config.service';
import {AppToastService} from '../../../ui/toast/toast.service';
import {TranslocoService} from '@jsverse/transloco';

const PERSONA_OVERRIDE = {
  id: 'o1', family: null, kind: 'prompts', name: 'persona', content: 'EXISTING',
  content_format: 'text', notes: null, created_by: null, updated_by: null,
  created_at: null, updated_at: null,
};

function make(opts?: {overrides?: any[]; catalog?: any[]}) {
  const overrides = signal<any[]>(opts?.overrides ?? []);
  const catalog = signal<any[]>(
    opts?.catalog ?? [{kind: 'prompts', name: 'persona', title: 'Persona', description: 'd'}],
  );
  const admin = {
    overrides,
    catalog,
    loadOverrides: vi.fn(),
    loadCatalog: vi.fn(),
    getBundled: vi.fn().mockReturnValue(
      of({family: null, kind: 'prompts', name: 'persona', content: 'BUNDLED', catalog: null}),
    ),
    createOverride: vi.fn().mockReturnValue(of({})),
    deleteOverride: vi.fn().mockReturnValue(of({deleted: true})),
  };
  const toast = {success: vi.fn(), danger: vi.fn(), info: vi.fn()};
  const injector = Injector.create({
    providers: [
      {provide: AdminConfigService, useValue: admin},
      {provide: AppToastService, useValue: toast},
      {provide: TranslocoService, useValue: {translate: (k: string) => k}},
    ],
  });
  const component = runInInjectionContext(injector, () => new AdminConfigComponent());
  return {component, admin, toast};
}

describe('AdminConfigComponent', () => {
  it('loads catalog + overrides on init', () => {
    const {component, admin} = make();
    component.ngOnInit();
    expect(admin.loadCatalog).toHaveBeenCalled();
    expect(admin.loadOverrides).toHaveBeenCalled();
  });

  it('fetches the bundled default when a prompt is selected', () => {
    const {component, admin} = make();
    component.onKeyChange('persona');
    expect(admin.getBundled).toHaveBeenCalledWith(null, 'prompts', 'persona');
    expect(component.bundledContent()).toBe('BUNDLED');
  });

  it('maps the "_" family option to a null family', () => {
    const {component, admin} = make();
    component.onKeyChange('persona');
    component.onFamilyChange('gemma');
    expect(admin.getBundled).toHaveBeenLastCalledWith('gemma', 'prompts', 'persona');
    component.onFamilyChange('_');
    expect(admin.getBundled).toHaveBeenLastCalledWith(null, 'prompts', 'persona');
    expect(component.selectedFamily()).toBeNull();
  });

  it('seeds the editor from an existing override and flags hasOverride', () => {
    const {component} = make({overrides: [PERSONA_OVERRIDE]});
    component.onKeyChange('persona');
    expect(component.hasOverride()).toBe(true);
    expect(component.overrideContent()).toBe('EXISTING');
  });

  it('save() POSTs the override with the resolved family + content', () => {
    const {component, admin, toast} = make();
    component.onKeyChange('persona');
    component.overrideContent.set('NEW CONTENT');
    component.save();
    expect(admin.createOverride).toHaveBeenCalledWith({
      family: null, kind: 'prompts', name: 'persona', content: 'NEW CONTENT',
    });
    expect(toast.success).toHaveBeenCalled();
  });

  it('save() refuses empty content', () => {
    const {component, admin, toast} = make();
    component.onKeyChange('persona');
    component.overrideContent.set('   ');
    component.save();
    expect(admin.createOverride).not.toHaveBeenCalled();
    expect(toast.danger).toHaveBeenCalled();
  });

  it('resetToBundled() deletes the existing override', () => {
    const {component, admin, toast} = make({overrides: [PERSONA_OVERRIDE]});
    component.onKeyChange('persona');
    component.resetToBundled();
    expect(admin.deleteOverride).toHaveBeenCalledWith('o1');
    expect(toast.success).toHaveBeenCalled();
  });

  it('resetToBundled() is a no-op when there is no override', () => {
    const {component, admin} = make();
    component.onKeyChange('persona');
    component.resetToBundled();
    expect(admin.deleteOverride).not.toHaveBeenCalled();
  });

  it('buckets the catalog into ordered <optgroup> groups, trailing "Other" for ungrouped keys', () => {
    const {component} = make({
      catalog: [
        {kind: 'prompts', name: 'systemprompt', title: 'System', description: 'd', group: 'Prompts · all agents'},
        {kind: 'prompts', name: 'strategic', title: 'Strategic', description: 'd', group: 'Prompts · worker (jobs)'},
        {kind: 'prompts', name: 'persona', title: 'Persona', description: 'd', group: 'Prompts · all agents'},
        {kind: 'prompts', name: 'systemprompt_interactive', title: 'Interactive', description: 'd', group: 'Prompts · persistent (sessions)'},
        {kind: 'settings', name: 'orphan', title: 'Orphan', description: 'd'}, // no group
      ],
    });
    const groups = component.groupedCatalog();
    // Group order = first-seen order in the catalog; "Other" trails for the ungrouped key.
    expect(groups.map((g) => g.label)).toEqual([
      'Prompts · all agents',
      'Prompts · worker (jobs)',
      'Prompts · persistent (sessions)',
      'Other',
    ]);
    // Entries land in their group, preserving catalog order within the group.
    expect(groups[0].entries.map((e) => e.name)).toEqual(['systemprompt', 'persona']);
    expect(groups[2].entries.map((e) => e.name)).toEqual(['systemprompt_interactive']);
    expect(groups[3].entries.map((e) => e.name)).toEqual(['orphan']);
  });

  // --- structured kinds (settings / guardrails) ---

  const SETTINGS_CATALOG = [
    {kind: 'settings', name: 'temperature', title: 'Temp', description: 'd', type: 'number'},
  ];

  function makeSettings() {
    const ctx = make({catalog: SETTINGS_CATALOG});
    ctx.admin.getBundled.mockReturnValue(
      of({family: null, kind: 'settings', name: 'temperature', content: 0.3, catalog: null}),
    );
    return ctx;
  }

  it('seeds a settings editor from the bundled value as JSON', () => {
    const {component} = makeSettings();
    component.onKeyChange('temperature');
    expect(component.isStructured()).toBe(true);
    expect(component.bundledContent()).toBe('0.3');
    expect(component.overrideContent()).toBe('0.3'); // no override -> seeded from bundled
  });

  it('save() POSTs parsed value_json for a settings key', () => {
    const {component, admin, toast} = makeSettings();
    component.onKeyChange('temperature');
    component.overrideContent.set('0.7');
    component.save();
    expect(admin.createOverride).toHaveBeenCalledWith({
      family: null, kind: 'settings', name: 'temperature', value_json: 0.7,
    });
    expect(toast.success).toHaveBeenCalled();
  });

  it('save() rejects invalid JSON for a structured key', () => {
    const {component, admin, toast} = makeSettings();
    component.onKeyChange('temperature');
    component.overrideContent.set('not json');
    component.save();
    expect(admin.createOverride).not.toHaveBeenCalled();
    expect(toast.danger).toHaveBeenCalled();
  });
});
