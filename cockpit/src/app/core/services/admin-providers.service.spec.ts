import {describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {of} from 'rxjs';
import {AdminProvidersService} from './admin-providers.service';

function createService(mockHttp?: any) {
  const http = mockHttp ?? {
    get: vi.fn().mockReturnValue(of([])),
    put: vi.fn().mockReturnValue(of({})),
    post: vi.fn().mockReturnValue(of({})),
    patch: vi.fn().mockReturnValue(of({})),
    delete: vi.fn().mockReturnValue(of({status: 'deleted'})),
  };

  const injector = Injector.create({
    providers: [{provide: HttpClient, useValue: http}],
  });

  const service = runInInjectionContext(injector, () => new AdminProvidersService());
  return {service, http};
}

describe('AdminProvidersService', () => {
  describe('system api keys', () => {
    it('loads keys from /admin/providers/keys', () => {
      const http = {
        get: vi.fn().mockReturnValue(of([
          {
            id: '1', provider: 'openai', key_prefix: 'sk-proj-',
            label: 'seed', seeded_from: 'helm:llm.seed',
            created_at: null, updated_at: null,
          },
        ])),
        put: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
      };
      const {service} = createService(http);
      service.loadSystemApiKeys();
      expect(http.get).toHaveBeenCalledWith(expect.stringContaining('/admin/providers/keys'));
      expect(service.systemApiKeys()).toHaveLength(1);
      expect(service.systemApiKeys()[0].seeded_from).toBe('helm:llm.seed');
    });

    it('PUTs a key to /admin/providers/keys/{provider}', () => {
      const http = {
        get: vi.fn().mockReturnValue(of([])),
        put: vi.fn().mockReturnValue(of({id: '1', provider: 'openai', key_prefix: 'sk-new-',
          label: null, seeded_from: null, created_at: null, updated_at: null})),
        post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
      };
      const {service} = createService(http);
      service.setSystemApiKey('openai', {api_key: 'sk-new-abc'}).subscribe();
      expect(http.put).toHaveBeenCalledWith(
        expect.stringContaining('/admin/providers/keys/openai'),
        {api_key: 'sk-new-abc'},
      );
      expect(http.get).toHaveBeenCalled(); // tap reloads
    });

    it('DELETEs a key and refreshes the list', () => {
      const http = {
        get: vi.fn().mockReturnValue(of([])),
        delete: vi.fn().mockReturnValue(of({status: 'deleted'})),
        put: vi.fn(), post: vi.fn(), patch: vi.fn(),
      };
      const {service} = createService(http);
      service.deleteSystemApiKey('anthropic').subscribe();
      expect(http.delete).toHaveBeenCalledWith(
        expect.stringContaining('/admin/providers/keys/anthropic'),
      );
      expect(http.get).toHaveBeenCalled();
    });
  });

  describe('system endpoints', () => {
    it('loads endpoints with nested models', () => {
      const http = {
        get: vi.fn().mockReturnValue(of([
          {id: 'ep-1', label: 'Local vLLM', base_url: 'http://vllm/v1', key_prefix: null,
           created_at: null, updated_at: null,
           models: [{id: 'm-1', endpoint_id: 'ep-1', model_id: 'gemma', display_name: 'Gemma',
                     family: null, context_window: null, reasoning_level: null,
                     enabled: true, created_at: null}]},
        ])),
        put: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
      };
      const {service} = createService(http);
      service.loadSystemEndpoints();
      expect(service.systemEndpoints()).toHaveLength(1);
      expect(service.systemEndpoints()[0].models[0].model_id).toBe('gemma');
    });

    it('encodes model_id in DELETE path', () => {
      const http = {
        get: vi.fn().mockReturnValue(of([])),
        delete: vi.fn().mockReturnValue(of({status: 'deleted'})),
        put: vi.fn(), post: vi.fn(), patch: vi.fn(),
      };
      const {service} = createService(http);
      service.deleteSystemEndpointModel('ep-1', 'my-org/my-model').subscribe();
      const url = http.delete.mock.calls[0][0];
      expect(url).toContain('/admin/providers/endpoints/ep-1/models/my-org%2Fmy-model');
    });

    it('POSTs to the test endpoint', () => {
      const http = {
        get: vi.fn(),
        post: vi.fn().mockReturnValue(of({ok: true, status: 200, error: null, probe_url: ''})),
        put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
      };
      const {service} = createService(http);
      service.testSystemEndpoint('ep-1').subscribe();
      expect(http.post).toHaveBeenCalledWith(
        expect.stringContaining('/admin/providers/endpoints/ep-1/test'),
        {},
      );
    });
  });

  describe('defaults', () => {
    it('loads the three default-kind values', () => {
      const http = {
        get: vi.fn().mockReturnValue(of({builder: 'gpt-4o', browser: null, citation: 'gpt-4'})),
        put: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
      };
      const {service} = createService(http);
      service.loadDefaults();
      expect(http.get).toHaveBeenCalledWith(expect.stringContaining('/admin/providers/defaults'));
      expect(service.defaults()).toEqual({builder: 'gpt-4o', browser: null, citation: 'gpt-4'});
    });

    it('PUTs a new default', () => {
      const http = {
        get: vi.fn().mockReturnValue(of({builder: null, browser: null, citation: null})),
        put: vi.fn().mockReturnValue(of({kind: 'builder', model: 'gpt-4o'})),
        post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
      };
      const {service} = createService(http);
      service.setDefault('builder', 'gpt-4o').subscribe();
      expect(http.put).toHaveBeenCalledWith(
        expect.stringContaining('/admin/providers/defaults/builder'),
        {model: 'gpt-4o'},
      );
    });
  });
});
