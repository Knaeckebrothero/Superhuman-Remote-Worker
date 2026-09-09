import {describe, expect, it} from 'vitest';
import {Injector, runInInjectionContext, signal} from '@angular/core';
import {DraftEmptyStateComponent} from './draft-empty-state.component';

function create() {
  const injector = Injector.create({providers: []});
  return runInInjectionContext(injector, () => new DraftEmptyStateComponent());
}

describe('DraftEmptyStateComponent', () => {
  it('re-emits a picked suggestion to the parent', () => {
    const component = create();
    const seen: unknown[] = [];
    component.suggestionPicked.subscribe((s) => seen.push(s));

    component.pick({icon: 'route', text: 'Review the manifest'});

    expect(seen).toEqual([{icon: 'route', text: 'Review the manifest'}]);
  });

  it('re-emits the connector toggle as a boolean', () => {
    const component = create();
    const seen: boolean[] = [];
    component.connectorsToggled.subscribe((v) => seen.push(v));

    component.onConnectorsToggle({target: {checked: true}} as unknown as Event);

    expect(seen).toEqual([true]);
  });
});
