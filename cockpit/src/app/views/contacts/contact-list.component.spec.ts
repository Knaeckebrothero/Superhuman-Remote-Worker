import {Injector, runInInjectionContext} from '@angular/core';
import {of} from 'rxjs';
import {describe, expect, it, vi} from 'vitest';

import {Contact} from '../../core/models/api.model';
import {ContactListComponent} from './contact-list.component';

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
    const injector = Injector.create({providers: []});
    return runInInjectionContext(injector, () => new ContactListComponent());
  }

  it('annotates a chip when its primary address is not opted in', () => {
    const c = make();
    expect(c.chipLabel(anna(), 'whatsapp')).toBe('whatsapp·pending');
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
});
