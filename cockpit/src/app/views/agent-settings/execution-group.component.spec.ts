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

  // Choosing a value is intent, even when that value happens to be the one
  // already resolved. Collapsing it back to "inherit" made the displayed
  // default the one option the form could not express, and left the user
  // unable to pin against a later change to the expert/account layer. Only the
  // reset control clears an override now.
  it('pins the resolved default when it is explicitly selected', () => {
    const c = createComponent();
    c.onImageQualityChange('economy');
    expect(c.imageQuality()).toBe('economy');
    c.onImageQualityChange('standard'); // equals the resolved default
    expect(c.imageQuality()).toBe('standard');
    expect(c.getOverrides()['image_quality']).toBe('standard');
  });

  it('pins the resolved default when the user interacts without changing it', () => {
    const c = createComponent();
    expect(c.imageQuality()).toBeNull();
    // What PinOnInteractDirective's (pin) output triggers: a native <select>
    // fires no change event when the shown option is re-picked.
    c.pinValue(c.imageQuality, c.resolvedImageQuality());
    expect(c.imageQuality()).toBe('standard');
    expect(c.getOverrides()['image_quality']).toBe('standard');
  });

  it('pinning never overwrites a value the user already chose', () => {
    const c = createComponent();
    c.onImageQualityChange('high');
    c.pinValue(c.imageQuality, c.resolvedImageQuality());
    expect(c.imageQuality()).toBe('high');
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
