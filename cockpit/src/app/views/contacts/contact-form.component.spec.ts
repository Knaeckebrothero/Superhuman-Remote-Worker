import {Injector, linkedSignal, runInInjectionContext, signal} from '@angular/core';
import {describe, expect, it} from 'vitest';

import {Contact} from '../../core/models/api.model';
import {
  ContactFormComponent,
  seedName,
  seedNotes,
  seedProjectIds,
  seedRows,
} from './contact-form.component';

function makeContact(overrides: Partial<Contact> = {}): Contact {
  return {
    id: 'c1', owner_user_id: 'u1', display_name: 'Anna Weber', notes: 'CET.',
    addresses: [
      {id: 'a1', channel: 'email', address: 'anna@acme.de', is_primary: true,
       opt_in_status: 'opted_in', last_inbound_at: null, created_at: ''},
    ],
    projects: [{id: 'p1', name: 'Acme Website'}],
    created_at: '', updated_at: '', ...overrides,
  };
}

const bjorn = makeContact({
  id: 'c2', display_name: 'Bjorn Alvestrand', notes: 'PST.',
  addresses: [
    {id: 'b1', channel: 'whatsapp', address: '+15551234567', is_primary: true,
     opt_in_status: 'opted_in', last_inbound_at: null, created_at: ''},
  ],
  projects: [{id: 'p2', name: 'Q3'}],
});

// Harness note: this exercises the fix at two levels, and documents a real
// gap rather than papering over it.
//
// 1. seedName/seedNotes/seedRows/seedProjectIds are pure — tested directly.
// 2. The re-seed-on-source-change *reactive contract* (the actual bug) is
//    tested by wiring those same exported functions into a linkedSignal
//    driven by a plain signal() standing in for `contact()`.
// 3. ContactFormComponent itself is constructed bare via runInInjectionContext
//    (the working pattern from contact-list.component.spec.ts) to confirm it
//    actually exposes name/notes/rows/selectedProjects as callable signals
//    seeded from a null contact — this alone fails against the pre-fix
//    ngOnInit/plain-field version (`form.name is not a function`), so it does
//    catch a regression to the old shape.
//
// What's NOT covered here: driving `contact()` itself (the real input()) to a
// second value and re-reading the real component. Verified empirically (see
// commit) that this repo's vitest pipeline doesn't run components through
// ngtsc, so signal `input()` fields compile with no metadata under TestBed —
// `ɵcmp.inputs` is `{}`, and both template property binding and
// `fixture.componentRef.setInput('contact', ...)` throw NG0303 ("Can't
// bind/set... isn't a known property/input"). This affects every signal-input
// component in the repo, not just this one (confirmed against
// ContactListComponent too, and contrasted with a decorator-`@Input()`
// component, CanvasLiveAppRendererComponent, whose inputs DO compile
// correctly under the same harness) — `@angular/build`'s vitest unit-test
// runner (present in node_modules) would close this gap but isn't wired into
// angular.json; activating it is a separate, larger change, not part of this
// fix.
describe('ContactFormComponent seed derivation (pure)', () => {
  it('seeds name/notes/rows/projectIds from a contact', () => {
    const c = makeContact();
    expect(seedName(c)).toBe('Anna Weber');
    expect(seedNotes(c)).toBe('CET.');
    expect(seedRows(c)).toEqual([
      {id: 'a1', channel: 'email', address: 'anna@acme.de', is_primary: true},
    ]);
    expect(seedProjectIds(c)).toEqual(new Set(['p1']));
  });

  it('seeds blank/empty state from null (new-contact mode)', () => {
    expect(seedName(null)).toBe('');
    expect(seedNotes(null)).toBe('');
    expect(seedRows(null)).toEqual([]);
    expect(seedProjectIds(null)).toEqual(new Set());
  });
});

describe('linkedSignal re-seed contract (the actual bug)', () => {
  function make() {
    const injector = Injector.create({providers: []});
    return runInInjectionContext(injector, () => {
      const target = signal<Contact | null>(null);
      return {
        target,
        name: linkedSignal(() => seedName(target())),
        rows: linkedSignal(() => seedRows(target())),
        selectedProjects: linkedSignal(() => seedProjectIds(target())),
      };
    });
  }

  it('re-seeds fully on target switch, discarding an in-progress local edit', () => {
    const {target, name, rows, selectedProjects} = make();

    target.set(makeContact());
    expect(name()).toBe('Anna Weber');
    expect(rows().map(r => r.id)).toEqual(['a1']);
    expect([...selectedProjects()]).toEqual(['p1']);

    // In-progress edit on Anna, not yet saved — mirrors the user typing in
    // the name field before switching targets.
    name.set('Anna (typo fix)');
    expect(name()).toBe('Anna (typo fix)');

    // Switch target without ever destroying the signals (mirrors: expand row
    // A -> Edit -> without closing, expand row B -> Edit; contacts-page's
    // `showForm()` stays true so the real component instance persists).
    target.set(bjorn);

    expect(name()).toBe('Bjorn Alvestrand');
    expect(rows().map(r => r.id)).toEqual(['b1']);
    expect([...selectedProjects()]).toEqual(['p2']);
  });

  it('re-seeds to blank when the target clears to null (edit -> new)', () => {
    const {target, name, rows, selectedProjects} = make();
    target.set(makeContact());
    expect(name()).toBe('Anna Weber');

    target.set(null);

    expect(name()).toBe('');
    expect(rows()).toEqual([]);
    expect(selectedProjects().size).toBe(0);
  });
});

describe('ContactFormComponent (bare construction)', () => {
  function make(): ContactFormComponent {
    const injector = Injector.create({providers: []});
    return runInInjectionContext(injector, () => new ContactFormComponent());
  }

  it('exposes name/notes/rows/selectedProjects as signals seeded blank with no edit target', () => {
    const form = make();
    expect(form.contact()).toBe(null);
    expect(form.name()).toBe('');
    expect(form.notes()).toBe('');
    expect(form.rows()).toEqual([]);
    expect(form.selectedProjects().size).toBe(0);
    expect(form.valid()).toBe(false);
  });
});
