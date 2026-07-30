import {Injectable, inject} from '@angular/core';
import {HttpClient, HttpParams} from '@angular/common/http';
import {Observable, map} from 'rxjs';

import {Contact, ContactAddress} from '../models/api.model';
import {environment} from '../environment';

export interface ContactAddressIn {
  channel: string;
  address: string;
  is_primary?: boolean;
}

export interface ContactCreateBody {
  display_name: string;
  notes?: string | null;
  addresses?: ContactAddressIn[];
}

@Injectable({providedIn: 'root'})
export class ContactsService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = `${environment.apiUrl}/contacts`;

  list(filters: {project_id?: string; channel?: string; q?: string} = {}): Observable<Contact[]> {
    let params = new HttpParams();
    for (const [k, v] of Object.entries(filters)) {
      if (v) params = params.set(k, v);
    }
    return this.http
      .get<{contacts: Contact[]}>(this.baseUrl, {params})
      .pipe(map(r => r.contacts));
  }

  create(body: ContactCreateBody): Observable<Contact> {
    return this.http.post<{contact: Contact}>(this.baseUrl, body).pipe(map(r => r.contact));
  }

  update(id: string, patch: {display_name?: string; notes?: string | null}): Observable<Contact> {
    return this.http.patch<{contact: Contact}>(`${this.baseUrl}/${id}`, patch).pipe(map(r => r.contact));
  }

  remove(id: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl}/${id}`);
  }

  addAddress(contactId: string, body: ContactAddressIn): Observable<ContactAddress> {
    return this.http
      .post<{address: ContactAddress}>(`${this.baseUrl}/${contactId}/addresses`, body)
      .pipe(map(r => r.address));
  }

  patchAddress(addressId: string, patch: {address?: string; is_primary?: boolean}): Observable<ContactAddress> {
    return this.http
      .patch<{address: ContactAddress}>(`${this.baseUrl}/addresses/${addressId}`, patch)
      .pipe(map(r => r.address));
  }

  removeAddress(addressId: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl}/addresses/${addressId}`);
  }

  link(contactId: string, projectId: string): Observable<void> {
    return this.http.post<void>(`${this.baseUrl}/${contactId}/projects/${projectId}`, {});
  }

  unlink(contactId: string, projectId: string): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl}/${contactId}/projects/${projectId}`);
  }
}
