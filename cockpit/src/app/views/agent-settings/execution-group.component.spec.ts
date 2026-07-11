import {describe, expect, it} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {of} from 'rxjs';
import {TranslocoService} from '@jsverse/transloco';
import {ExecutionGroupComponent} from './execution-group.component';

function createComponent(): ExecutionGroupComponent {
  const mockTransloco = {
    translate: (key: string) => key,
    langChanges$: of('en'),
    getActiveLang: () => 'en',
  };
  const injector = Injector.create({
    providers: [{provide: TranslocoService, useValue: mockTransloco}],
  });
  return runInInjectionContext(injector, () => new ExecutionGroupComponent());
}

describe('ExecutionGroupComponent image quality', () => {
  it('defaults to standard with no override emitted', () => {
    const c = createComponent();
    expect(c.imageQuality()).toBeNull();
    expect(c.resolvedImageQuality()).toBe('standard');
    expect(c.getOverrides()['image_quality']).toBeUndefined();
  });

  it('captures a non-default tier into the override fragment', () => {
    const c = createComponent();
    c.onImageQualityChange('high');
    expect(c.imageQuality()).toBe('high');
    // image_quality is added regardless of mode (top-level knob for job+session)
    expect(c.getOverrides()['image_quality']).toBe('high');
  });

  it('clears the override when set back to the resolved default', () => {
    const c = createComponent();
    c.onImageQualityChange('economy');
    expect(c.imageQuality()).toBe('economy');
    c.onImageQualityChange('standard'); // equals the resolved default
    expect(c.imageQuality()).toBeNull();
    expect(c.getOverrides()['image_quality']).toBeUndefined();
  });

  it('counts toward modifiedCount and clears on resetAll', () => {
    const c = createComponent();
    c.onImageQualityChange('high');
    expect(c.modifiedCount()).toBe(1);
    c.resetAll();
    expect(c.imageQuality()).toBeNull();
    expect(c.modifiedCount()).toBe(0);
  });
});
