import {TestBed} from '@angular/core/testing';
import {provideHttpClient} from '@angular/common/http';
import {HttpTestingController, provideHttpClientTesting} from '@angular/common/http/testing';
import {describe, expect, it, beforeEach, afterEach} from 'vitest';

import {ContactsService} from './contacts.service';
import {environment} from '../environment';

describe('ContactsService', () => {
  let service: ContactsService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(ContactsService);
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => http.verify());

  it('lists with filters', () => {
    service.list({q: 'anna', channel: 'whatsapp'}).subscribe();
    const req = http.expectOne(
      r => r.url === `${environment.apiUrl}/contacts`
        && r.params.get('q') === 'anna' && r.params.get('channel') === 'whatsapp');
    expect(req.request.method).toBe('GET');
    req.flush({contacts: []});
  });

  it('links a contact to a project', () => {
    service.link('c1', 'p1').subscribe();
    const req = http.expectOne(`${environment.apiUrl}/contacts/c1/projects/p1`);
    expect(req.request.method).toBe('POST');
    req.flush({status: 'linked'});
  });
});
