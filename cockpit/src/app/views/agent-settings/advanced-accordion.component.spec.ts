import {describe, expect, it} from 'vitest';
import {signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {TranslocoService} from '@jsverse/transloco';
import {of} from 'rxjs';
import {AdvancedAccordionComponent} from './advanced-accordion.component';

/**
 * Create the component under TestBed with its `backendOverride` input stubbed
 * by a writable signal.
 *
 * The backend selector itself is a level-1 control owned by
 * ExecutionGroupComponent (see execution-group.component.spec.ts) — this
 * section only consumes the choice, so the tests drive it through the input.
 * Signal inputs cannot be set through `setInput()` in this pipeline, so the
 * input is replaced by a closure over a real signal to stay reactive; same
 * workaround, and same reason, as live-mode.spec.ts.
 */
function createComponent() {
  const mockTransloco = {
    translate: (key: string) => key,
    langChanges$: of('en'),
    getActiveLang: () => 'en',
  };

  TestBed.configureTestingModule({
    providers: [
      AdvancedAccordionComponent,
      {provide: TranslocoService, useValue: mockTransloco},
    ],
  });

  const component = TestBed.inject(AdvancedAccordionComponent);
  const backend = signal<string | null>(null);
  Object.defineProperty(component, 'backendOverride', {value: () => backend()});
  return {component, backend};
}

describe('AdvancedAccordionComponent — lite workspace gating', () => {
  it('isLiteBackend / isNoneBackend track the backend chosen in Execution', () => {
    const {component, backend} = createComponent();
    // No override resolves to the 'sandbox' config default.
    expect(component.isLiteBackend()).toBe(false);
    expect(component.isNoneBackend()).toBe(false);

    backend.set('vm');
    expect(component.isLiteBackend()).toBe(false);

    backend.set('virtual');
    expect(component.isLiteBackend()).toBe(true);
    expect(component.isNoneBackend()).toBe(false);

    backend.set('none');
    expect(component.isLiteBackend()).toBe(true);
    expect(component.isNoneBackend()).toBe(true);
  });

  it('omits shell/git overrides for a virtual backend but keeps file limits', () => {
    const {component, backend} = createComponent();
    backend.set('virtual');
    component.gitVersioning.set(true);
    component.shellMode.set('persistent');
    component.maxReadWords.set(5000);

    const o = component.getOverrides() as Record<string, any>;
    // `workspace.backend` is the Execution group's to emit, not this one's.
    expect(o['workspace'].backend).toBeUndefined();
    expect(o['workspace'].git_versioning).toBeUndefined();
    expect(o['workspace'].max_read_words).toBe(5000); // virtual keeps file tools
    expect(o['shell']).toBeUndefined();
    // No `browser` fragment on any tier: this group's two browser toggles were
    // `browse_website` knobs and nothing read them.
    expect(o['browser']).toBeUndefined();
  });

  it('omits file-size limits for a none backend', () => {
    const {component, backend} = createComponent();
    backend.set('none');
    component.maxReadWords.set(5000);
    component.maxWriteWords.set(2000);

    const o = component.getOverrides() as Record<string, any>;
    expect(o['workspace']).toBeUndefined();
  });

  it('keeps shell and git overrides for a sandbox backend', () => {
    const {component, backend} = createComponent();
    backend.set('sandbox');
    component.shellMode.set('persistent');
    component.gitVersioning.set(true);

    const o = component.getOverrides() as Record<string, any>;
    expect(o['shell'].mode).toBe('persistent');
    expect(o['workspace'].git_versioning).toBe(true);
  });
});

describe('AdvancedAccordionComponent — VM sizing', () => {
  it('emits VM sizing whenever the effective backend is vm', () => {
    const {component, backend} = createComponent();
    backend.set('vm');
    component.vmCpuCores.set(4);
    component.vmMemory.set('8Gi');

    const o = component.getOverrides() as Record<string, any>;
    expect(o['workspace'].vm).toEqual({cpu_cores: 4, memory: '8Gi'});
  });

  it('emits the VM disk size next to cores and memory', () => {
    const {component, backend} = createComponent();
    backend.set('vm');
    component.vmDiskSize.set('120Gi');

    const o = component.getOverrides() as Record<string, any>;
    expect(o['workspace'].vm).toEqual({disk_size: '120Gi'});
  });

  it('leaves disk_size out until the user sets it', () => {
    const {component, backend} = createComponent();
    backend.set('vm');
    component.vmMemory.set('8Gi');

    const o = component.getOverrides() as Record<string, any>;
    expect(o['workspace'].vm).toEqual({memory: '8Gi'});
    expect(component.resolvedVmDiskSize()).toBe('');
  });

  it('drops VM sizing once the backend moves off vm', () => {
    const {component, backend} = createComponent();
    backend.set('vm');
    component.vmCpuCores.set(4);

    backend.set('sandbox');
    const o = component.getOverrides() as Record<string, any>;
    expect(o['workspace']?.vm).toBeUndefined();
  });
});
