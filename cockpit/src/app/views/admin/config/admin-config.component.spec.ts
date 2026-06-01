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
});
