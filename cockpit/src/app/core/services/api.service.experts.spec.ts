import {describe, expect, it, vi} from 'vitest';
import {of} from 'rxjs';
import {ApiService} from './api.service';
import type {ExpertCreateRequest} from '../models/api.model';

// Build an ApiService without running its constructor (which would call
// inject(HttpClient) outside an injection context), then wire a mock http +
// baseUrl. Methods only touch this.http / this.baseUrl.
function makeService() {
  const http = {
    get: vi.fn(() => of(null)),
    post: vi.fn(() => of({})),
    put: vi.fn(() => of({})),
    delete: vi.fn(() => of({})),
  };
  const svc = Object.create(ApiService.prototype) as ApiService;
  (svc as unknown as {http: unknown}).http = http;
  (svc as unknown as {baseUrl: string}).baseUrl = '/api';
  return {svc, http};
}

const body: ExpertCreateRequest = {
  name: 'x',
  display_name: 'X',
  expert_type: 'session',
};

describe('ApiService expert write methods', () => {
  it('createExpert POSTs to /experts', () => {
    const {svc, http} = makeService();
    svc.createExpert(body).subscribe();
    expect(http.post).toHaveBeenCalledWith('/api/experts', body);
  });

  it('updateExpert PUTs to /experts/{id}', () => {
    const {svc, http} = makeService();
    svc.updateExpert('abc', {display_name: 'Y'}).subscribe();
    expect(http.put).toHaveBeenCalledWith('/api/experts/abc', {display_name: 'Y'});
  });

  it('deleteExpert DELETEs /experts/{id}', () => {
    const {svc, http} = makeService();
    svc.deleteExpert('abc').subscribe();
    expect(http.delete).toHaveBeenCalledWith('/api/experts/abc');
  });

  it('duplicateExpert POSTs to /experts/{id}/duplicate', () => {
    const {svc, http} = makeService();
    svc.duplicateExpert('abc').subscribe();
    expect(http.post).toHaveBeenCalledWith('/api/experts/abc/duplicate', {});
  });

  it('exportExpert GETs /experts/{id}/export', () => {
    const {svc, http} = makeService();
    svc.exportExpert('abc').subscribe();
    expect(http.get).toHaveBeenCalledWith('/api/experts/abc/export');
  });

  it('importExpert POSTs to /experts/import', () => {
    const {svc, http} = makeService();
    svc.importExpert(body).subscribe();
    expect(http.post).toHaveBeenCalledWith('/api/experts/import', body);
  });
});
