import {describe, expect, it} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {AppInlineEditableTextComponent} from './inline-editable-text.component';

/**
 * Instantiate in a bare injection context (no template render), matching the
 * repo's component-unit convention. `startEdit(current)` takes the value as an
 * argument so we never need to set the `value` input() signal.
 */
function createComponent() {
  const injector = Injector.create({providers: []});
  const comp = runInInjectionContext(
    injector,
    () => new AppInlineEditableTextComponent(),
  );
  const emitted: string[] = [];
  comp.save.subscribe((v) => emitted.push(v));
  return {comp, emitted};
}

describe('AppInlineEditableTextComponent', () => {
  it('starts in display (non-editing) mode', () => {
    const {comp} = createComponent();
    expect(comp.editing()).toBe(false);
  });

  it('startEdit enters edit mode and seeds the draft from the given value', () => {
    const {comp} = createComponent();
    comp.startEdit('Original session');
    expect(comp.editing()).toBe(true);
    expect(comp.draft()).toBe('Original session');
  });

  it('commit emits the trimmed new value and exits edit mode', () => {
    const {comp, emitted} = createComponent();
    comp.startEdit('Original session');
    comp.draft.set('  Renamed session  ');
    comp.commit();
    expect(emitted).toEqual(['Renamed session']);
    expect(comp.editing()).toBe(false);
  });

  it('commit does not emit when the value is unchanged', () => {
    const {comp, emitted} = createComponent();
    comp.startEdit('Original session');
    comp.commit(); // draft still equals the original
    expect(emitted).toEqual([]);
    expect(comp.editing()).toBe(false);
  });

  it('commit does not emit when the new value is empty/whitespace', () => {
    const {comp, emitted} = createComponent();
    comp.startEdit('Original session');
    comp.draft.set('   ');
    comp.commit();
    expect(emitted).toEqual([]);
    expect(comp.editing()).toBe(false);
  });

  it('commit is idempotent — a trailing blur after Enter does not emit twice', () => {
    const {comp, emitted} = createComponent();
    comp.startEdit('Original session');
    comp.draft.set('Renamed');
    comp.commit(); // Enter
    comp.commit(); // blur fired after the input was removed
    expect(emitted).toEqual(['Renamed']);
  });

  it('cancel exits edit mode without emitting', () => {
    const {comp, emitted} = createComponent();
    comp.startEdit('Original session');
    comp.draft.set('Renamed');
    comp.cancel();
    expect(emitted).toEqual([]);
    expect(comp.editing()).toBe(false);
  });

  it('Enter commits, Escape cancels', () => {
    const {comp, emitted} = createComponent();

    comp.startEdit('Original session');
    comp.draft.set('Via Enter');
    comp.onKeydown(new KeyboardEvent('keydown', {key: 'Enter'}));
    expect(emitted).toEqual(['Via Enter']);
    expect(comp.editing()).toBe(false);

    comp.startEdit('Original session');
    comp.draft.set('Via Esc');
    comp.onKeydown(new KeyboardEvent('keydown', {key: 'Escape'}));
    expect(emitted).toEqual(['Via Enter']); // Esc did not emit
    expect(comp.editing()).toBe(false);
  });
});
