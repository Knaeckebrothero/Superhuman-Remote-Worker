import {Injector, runInInjectionContext} from '@angular/core';
import {TranslocoService} from '@jsverse/transloco';
import {describe, expect, it} from 'vitest';

import {Contact} from '../../core/models/api.model';
import {ContactListComponent} from './contact-list.component';

// contact-list now injects TranslocoService (chipLabel routes opt-in state
// through contacts.optIn.* instead of building literal English — finding 7).
// The real service's constructor pulls in TRANSLOCO_TRANSPILER/CONFIG/etc,
// none of which exist in this bare-construction harness (no TestBed, see
// contact-form.component.spec.ts for the documented signal-input gap this
// repo's vitest pipeline has) — so provide a minimal stub instead of the
// real service. Mirrors the en.json strings so chipLabel's output stays
// meaningful in assertions.
const TRANSLATIONS: Record<string, string> = {
  'contacts.optIn.pending': 'opt-in pending',
  'contacts.optIn.opted_out': 'opted out',
};
const translocoStub = {translate: (key: string) => TRANSLATIONS[key] ?? key};

function anna(overrides: Partial<Contact> = {}): Contact {
  return {
    id: 'c1', owner_user_id: 'u1', display_name: 'Anna Weber', notes: 'CET.',
    addresses: [
      {id: 'a1', channel: 'email', address: 'anna@acme.de', is_primary: true,
       opt_in_status: 'opted_in', last_inbound_at: null, created_at: ''},
      {id: 'a2', channel: 'whatsapp', address: '+4917055501', is_primary: true,
       opt_in_status: 'pending', last_inbound_at: null, created_at: ''},
    ],
    projects: [{id: 'p1', name: 'Acme Website'}, {id: 'p2', name: 'Q3'}],
    created_at: '', updated_at: '', ...overrides,
  };
}

describe('ContactListComponent', () => {
  function make(): ContactListComponent {
    const injector = Injector.create({
      providers: [{provide: TranslocoService, useValue: translocoStub}],
    });
    return runInInjectionContext(injector, () => new ContactListComponent());
  }

  it('annotates a chip with the translated opt-in state when its primary address is not opted in', () => {
    const c = make();
    expect(c.chipLabel(anna(), 'whatsapp')).toBe('whatsapp·opt-in pending');
    expect(c.chipLabel(anna(), 'email')).toBe('email');
  });

  it('expansion is per-row and read-only state only', () => {
    const c = make();
    expect(c.isExpanded('c1')).toBe(false);
    c.toggle('c1');
    c.toggle('c2');
    expect(c.isExpanded('c1')).toBe(true);
    expect(c.isExpanded('c2')).toBe(true);  // several rows open at once (C1)
    c.toggle('c1');
    expect(c.isExpanded('c1')).toBe(false);
  });

  it('channelsOf lists channels present on the contact', () => {
    const c = make();
    expect(c.channelsOf(anna())).toEqual(['email', 'whatsapp']);
  });

  it('canModify gates Edit/Delete to the contact owner only (finding 4)', () => {
    const c = make();
    const owned = anna({owner_user_id: 'u1'});
    expect(c.canModify(owned, 'u1')).toBe(true);
    expect(c.canModify(owned, 'u2')).toBe(false);
    expect(c.canModify(owned, null)).toBe(false);
  });
});
