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
  it('filters the catalog by expert type', () => {
    const {svc, http} = makeService();
    svc.getExperts('session').subscribe();
    expect(http.get).toHaveBeenCalledWith('/api/experts', {params: expect.anything()});
    const params = http.get.mock.calls[0][1].params;
    expect(params.get('type')).toBe('session');
  });

  it('loads project-aware effective defaults', () => {
    const {svc, http} = makeService();
    svc.getExpertDefaults('project-1').subscribe();
    expect(http.get).toHaveBeenCalledWith('/api/expert-defaults', {params: expect.anything()});
    const params = http.get.mock.calls[0][1].params;
    expect(params.get('project_id')).toBe('project-1');
  });

  it('sets and clears a personal default', () => {
    const {svc, http} = makeService();
    svc.setPersonalExpertDefault('worker', 'expert-1').subscribe();
    svc.clearPersonalExpertDefault('worker').subscribe();
    expect(http.put).toHaveBeenCalledWith('/api/expert-defaults/worker', {
      expert_id: 'expert-1',
    });
    expect(http.delete).toHaveBeenCalledWith('/api/expert-defaults/worker');
  });

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

  // The expert editor diffs against this response, so the account layer has to
  // stay out unless a caller asks for it — otherwise a personal preference
  // reads as the framework baseline and can be saved into a shared expert.
  it('getExpertDetail omits the account layer by default', () => {
    const {svc, http} = makeService();
    svc.getExpertDetail('worker_base').subscribe();
    expect(http.get).toHaveBeenCalledWith('/api/experts/worker_base');
  });

  it('getExpertDetail requests the account layer for create forms', () => {
    const {svc, http} = makeService();
    svc.getExpertDetail('worker_base', {accountDefaults: true}).subscribe();
    expect(http.get).toHaveBeenCalledWith(
      '/api/experts/worker_base?account_defaults=true',
    );
  });
});
