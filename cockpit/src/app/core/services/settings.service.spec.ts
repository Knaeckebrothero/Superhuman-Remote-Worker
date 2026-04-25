import {describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {of, throwError} from 'rxjs';
import {SettingsService} from './settings.service';
import {LlmEndpoint} from '../models/api.model';

function makeEndpoint(overrides: Partial<LlmEndpoint> = {}): LlmEndpoint {
  return {
    id: 'ep-1',
    label: 'My vLLM',
    base_url: 'https://vllm.example/v1',
    key_prefix: 'sk-abcd',
    created_at: null,
    updated_at: null,
    models: [],
    ...overrides,
  };
}

function createService(mockHttp?: any) {
  const http = mockHttp ?? {
    get: vi.fn().mockReturnValue(of([])),
    post: vi.fn().mockReturnValue(of({})),
    patch: vi.fn().mockReturnValue(of({})),
    delete: vi.fn().mockReturnValue(of({status: 'deleted'})),
    put: vi.fn().mockReturnValue(of({})),
  };
  const injector = Injector.create({
    providers: [{provide: HttpClient, useValue: http}],
  });
  const service = runInInjectionContext(injector, () => new SettingsService());
  return {service, http};
}

describe('SettingsService — LLM endpoints', () => {
  it('loadLlmEndpoints populates the signal from GET response', () => {
    const endpoints = [makeEndpoint()];
    const http = {
      get: vi.fn().mockReturnValue(of(endpoints)),
      post: vi.fn(),
      patch: vi.fn(),
      delete: vi.fn(),
      put: vi.fn(),
    };
    const {service} = createService(http);
    service.loadLlmEndpoints();
    expect(service.llmEndpoints()).toEqual(endpoints);
    expect(http.get).toHaveBeenCalledWith(expect.stringContaining('/settings/llm-endpoints'));
  });

  it('loadLlmEndpoints swallows errors and keeps empty list', () => {
    const http = {
      get: vi.fn().mockReturnValue(throwError(() => new Error('network'))),
      post: vi.fn(),
      patch: vi.fn(),
      delete: vi.fn(),
      put: vi.fn(),
    };
    const {service} = createService(http);
    service.loadLlmEndpoints();
    expect(service.llmEndpoints()).toEqual([]);
  });

  it('createLlmEndpoint POSTs and reloads the list', () => {
    const http = {
      get: vi.fn().mockReturnValue(of([])),
      post: vi.fn().mockReturnValue(of(makeEndpoint())),
      patch: vi.fn(),
      delete: vi.fn(),
      put: vi.fn(),
    };
    const {service} = createService(http);
    service
      .createLlmEndpoint({label: 'L', base_url: 'https://x/v1', api_key: null})
      .subscribe();
    expect(http.post).toHaveBeenCalledWith(
      expect.stringContaining('/settings/llm-endpoints'),
      expect.objectContaining({label: 'L', base_url: 'https://x/v1'}),
    );
    // tap(() => loadLlmEndpoints()) re-fetches
    expect(http.get).toHaveBeenCalled();
  });

  it('updateLlmEndpoint PATCHes the correct URL', () => {
    const http = {
      get: vi.fn().mockReturnValue(of([])),
      post: vi.fn(),
      patch: vi.fn().mockReturnValue(of(makeEndpoint())),
      delete: vi.fn(),
      put: vi.fn(),
    };
    const {service} = createService(http);
    service.updateLlmEndpoint('ep-1', {label: 'renamed'}).subscribe();
    expect(http.patch).toHaveBeenCalledWith(
      expect.stringContaining('/settings/llm-endpoints/ep-1'),
      {label: 'renamed'},
    );
  });

  it('deleteLlmEndpoint DELETEs and reloads', () => {
    const http = {
      get: vi.fn().mockReturnValue(of([])),
      post: vi.fn(),
      patch: vi.fn(),
      delete: vi.fn().mockReturnValue(of({status: 'deleted'})),
      put: vi.fn(),
    };
    const {service} = createService(http);
    service.deleteLlmEndpoint('ep-1').subscribe();
    expect(http.delete).toHaveBeenCalledWith(
      expect.stringContaining('/settings/llm-endpoints/ep-1'),
    );
    expect(http.get).toHaveBeenCalled();
  });

  // The user-side createLlmEndpointModel / updateLlmEndpointModel /
  // deleteLlmEndpointModel methods were removed when the admin-curated
  // models catalog became the single source of truth. Coverage for the
  // catalog CRUD lives in admin-models.service.spec.ts (Phase C).

  it('testLlmEndpoint POSTs to the /test subpath', () => {
    const http = {
      get: vi.fn().mockReturnValue(of([])),
      post: vi.fn().mockReturnValue(
        of({ok: true, status: 200, error: null, probe_url: 'https://x/v1/models'}),
      ),
      patch: vi.fn(),
      delete: vi.fn(),
      put: vi.fn(),
    };
    const {service} = createService(http);
    let result: any;
    service.testLlmEndpoint('ep-1').subscribe((r) => (result = r));
    expect(http.post).toHaveBeenCalledWith(
      expect.stringContaining('/settings/llm-endpoints/ep-1/test'),
      {},
    );
    expect(result.ok).toBe(true);
  });
});
