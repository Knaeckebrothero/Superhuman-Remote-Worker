import {describe, expect, it, vi} from 'vitest';
import {signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
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
    getBundledSettings: vi.fn().mockReturnValue(of({})),
  };
  const toast = {success: vi.fn(), danger: vi.fn(), info: vi.fn()};
  // TestBed (not a bare Injector) so the constructor effect() resolves
  // ChangeDetectionScheduler. The effect stays dormant without TestBed.tick(),
  // which is fine — these tests drive the seed/save methods directly.
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      AdminConfigComponent,
      {provide: AdminConfigService, useValue: admin},
      {provide: AppToastService, useValue: toast},
      {provide: TranslocoService, useValue: {translate: (k: string) => k}},
    ],
  });
  const component = TestBed.inject(AdminConfigComponent);
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

  it('groups the dropdown by label and excludes settings (they have their own form)', () => {
    const {component} = make({
      catalog: [
        {kind: 'prompts', name: 'systemprompt', title: 'System', description: 'd', group: 'Prompts · all agents'},
        {kind: 'prompts', name: 'strategic', title: 'Strategic', description: 'd', group: 'Prompts · worker (jobs)'},
        {kind: 'prompts', name: 'persona', title: 'Persona', description: 'd', group: 'Prompts · all agents'},
        {kind: 'prompts', name: 'systemprompt_interactive', title: 'Interactive', description: 'd', group: 'Prompts · persistent (sessions)'},
        {kind: 'settings', name: 'temperature', title: 'Temp', description: 'd', type: 'number', group: 'Inference settings'},
        {kind: 'guardrails', name: 'orphan', title: 'Orphan', description: 'd'}, // no group
      ],
    });
    const groups = component.groupedCatalog();
    // Group order = first-seen order in the catalog; "Other" trails the ungrouped key.
    expect(groups.map((g) => g.label)).toEqual([
      'Prompts · all agents',
      'Prompts · worker (jobs)',
      'Prompts · persistent (sessions)',
      'Other',
    ]);
    // Settings never appear in the dropdown groups.
    expect(groups.some((g) => g.entries.some((e) => e.kind === 'settings'))).toBe(false);
    // Entries land in their group, preserving catalog order within the group.
    expect(groups[0].entries.map((e) => e.name)).toEqual(['systemprompt', 'persona']);
    expect(groups[2].entries.map((e) => e.name)).toEqual(['systemprompt_interactive']);
    expect(groups[3].entries.map((e) => e.name)).toEqual(['orphan']);
  });

  // --- structured kind in the dropdown: guardrails (settings moved to a form) ---

  const GUARDRAILS_CATALOG = [
    {kind: 'guardrails', name: 'guardrails', title: 'Guardrails', description: 'd', type: 'json'},
  ];

  function makeGuardrails() {
    const ctx = make({catalog: GUARDRAILS_CATALOG});
    ctx.admin.getBundled.mockReturnValue(
      of({family: null, kind: 'guardrails', name: 'guardrails', content: {x: 1}, catalog: null}),
    );
    return ctx;
  }

  it('seeds the guardrails editor from the bundled value as JSON', () => {
    const {component} = makeGuardrails();
    component.onKeyChange('guardrails');
    expect(component.isStructured()).toBe(true);
    expect(component.bundledContent()).toBe('{\n  "x": 1\n}');
    expect(component.overrideContent()).toBe('{\n  "x": 1\n}'); // no override -> seeded from bundled
  });

  it('save() POSTs parsed value_json for a structured key', () => {
    const {component, admin, toast} = makeGuardrails();
    component.onKeyChange('guardrails');
    component.overrideContent.set('{"a": 2}');
    component.save();
    expect(admin.createOverride).toHaveBeenCalledWith({
      family: null, kind: 'guardrails', name: 'guardrails', value_json: {a: 2},
    });
    expect(toast.success).toHaveBeenCalled();
  });

  it('save() rejects invalid JSON for a structured key', () => {
    const {component, admin, toast} = makeGuardrails();
    component.onKeyChange('guardrails');
    component.overrideContent.set('not json');
    component.save();
    expect(admin.createOverride).not.toHaveBeenCalled();
    expect(toast.danger).toHaveBeenCalled();
  });

  // --- settings form ---

  const SETTINGS_FORM_CATALOG = [
    {kind: 'settings', name: 'temperature', title: 'Temperature', description: 'd', type: 'number', min: 0, max: 2},
    {kind: 'settings', name: 'parallel_tool_calls', title: 'Parallel', description: 'd', type: 'boolean'},
  ];

  function makeForm(overrides: any[] = []) {
    const ctx = make({catalog: SETTINGS_FORM_CATALOG, overrides});
    ctx.admin.getBundledSettings = vi
      .fn()
      .mockReturnValue(of({temperature: 0.3, parallel_tool_calls: false}));
    return ctx;
  }

  function settingsOverride(name: string, value_json: unknown, id = 's1') {
    return {
      id, family: null, kind: 'settings', name, content: null, content_format: null,
      value_json, notes: null, created_by: null, updated_by: null, created_at: null, updated_at: null,
    };
  }

  it('settingsEntries selects only settings; they are absent from the dropdown', () => {
    const {component} = makeForm();
    expect(component.settingsEntries().map((e) => e.name)).toEqual([
      'temperature', 'parallel_tool_calls',
    ]);
    expect(component.groupedCatalog()).toEqual([]);
  });

  it('seeds the form from bundled defaults when no override exists', () => {
    const {component} = makeForm();
    component.seedSettingsForm(null, component.settingsEntries());
    expect(component.strValue('temperature')).toBe('0.3');
    expect(component.boolValue('parallel_tool_calls')).toBe(false);
    expect(component.settingOverridden('temperature')).toBe(false);
  });

  it('seeds an overridden field from the override value and flags it', () => {
    const {component} = makeForm([settingsOverride('temperature', 1)]);
    component.seedSettingsForm(null, component.settingsEntries());
    expect(component.strValue('temperature')).toBe('1');
    expect(component.settingOverridden('temperature')).toBe(true);
  });

  it('saveSettings upserts changed fields and deletes those reset to bundled', () => {
    const {component, admin, toast} = makeForm([settingsOverride('temperature', 1)]);
    component.seedSettingsForm(null, component.settingsEntries());
    component.setStr('temperature', '0.3'); // back to bundled -> delete
    component.setBool('parallel_tool_calls', true); // differs from bundled false -> upsert
    component.saveSettings();
    expect(admin.deleteOverride).toHaveBeenCalledWith('s1');
    expect(admin.createOverride).toHaveBeenCalledWith({
      family: null, kind: 'settings', name: 'parallel_tool_calls', value_json: true,
    });
    expect(toast.success).toHaveBeenCalled();
  });

  it('saveSettings is a no-op (no writes) when nothing differs from bundled', () => {
    const {component, admin, toast} = makeForm();
    component.seedSettingsForm(null, component.settingsEntries());
    component.saveSettings();
    expect(admin.createOverride).not.toHaveBeenCalled();
    expect(admin.deleteOverride).not.toHaveBeenCalled();
    expect(toast.info).toHaveBeenCalled();
  });

  it('saveSettings does not re-write an override whose value is unchanged', () => {
    const {component, admin, toast} = makeForm([settingsOverride('temperature', 1)]);
    component.seedSettingsForm(null, component.settingsEntries());
    component.saveSettings(); // temperature still 1 (its override), parallel still bundled
    expect(admin.createOverride).not.toHaveBeenCalled();
    expect(admin.deleteOverride).not.toHaveBeenCalled();
    expect(toast.info).toHaveBeenCalled();
  });

  it('saveSettings rejects out-of-range numbers before writing', () => {
    const {component, admin, toast} = makeForm();
    component.seedSettingsForm(null, component.settingsEntries());
    component.setStr('temperature', '5'); // max is 2
    component.saveSettings();
    expect(admin.createOverride).not.toHaveBeenCalled();
    expect(toast.danger).toHaveBeenCalled();
  });

  it('resetSetting drops a field back to its bundled default', () => {
    const {component} = makeForm([settingsOverride('temperature', 1)]);
    component.seedSettingsForm(null, component.settingsEntries());
    expect(component.strValue('temperature')).toBe('1');
    component.resetSetting('temperature');
    expect(component.strValue('temperature')).toBe('0.3');
  });

  // --- single-field editor (bundled folded into one editable field) ---

  it('seeds the single editable field from the bundled default when no override exists', () => {
    const {component} = make();
    component.onKeyChange('persona');
    expect(component.bundledContent()).toBe('BUNDLED');
    expect(component.overrideContent()).toBe('BUNDLED'); // field shows the effective value
    expect(component.hasOverride()).toBe(false);
    expect(component.editorLabel()).toContain('default');
  });

  it('save() is a no-op when the field is unchanged from the active override', () => {
    const {component, admin, toast} = make({overrides: [PERSONA_OVERRIDE]});
    component.onKeyChange('persona'); // field seeded to 'EXISTING'
    component.save();
    expect(admin.createOverride).not.toHaveBeenCalled();
    expect(admin.deleteOverride).not.toHaveBeenCalled();
    expect(toast.info).toHaveBeenCalled();
  });

  it('save() deletes the override when the field is reset back to the bundled default', () => {
    const {component, admin, toast} = make({overrides: [PERSONA_OVERRIDE]});
    component.onKeyChange('persona');
    component.overrideContent.set('BUNDLED'); // matches the shipped default
    component.save();
    expect(admin.deleteOverride).toHaveBeenCalledWith('o1');
    expect(admin.createOverride).not.toHaveBeenCalled();
    expect(toast.success).toHaveBeenCalled();
  });

  it('save() with no override and a field matching bundled just informs', () => {
    const {component, admin, toast} = make();
    component.onKeyChange('persona'); // field seeded to 'BUNDLED', no override
    component.save();
    expect(admin.createOverride).not.toHaveBeenCalled();
    expect(admin.deleteOverride).not.toHaveBeenCalled();
    expect(toast.info).toHaveBeenCalled();
  });

  it('loadFileIntoEditor rejects a non-text file and leaves the field untouched', () => {
    const {component, toast} = make();
    component.onKeyChange('persona');
    component.overrideContent.set('KEEP ME');
    component.loadFileIntoEditor(
      new File(['x'], 'evil.exe', {type: 'application/octet-stream'}),
    );
    expect(component.overrideContent()).toBe('KEEP ME');
    expect(toast.danger).toHaveBeenCalled();
  });
});
