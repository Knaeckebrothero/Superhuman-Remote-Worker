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

    it('encodes model_id and passes capability in DELETE query string', () => {
      const http = {
        get: vi.fn().mockReturnValue(of([])),
        delete: vi.fn().mockReturnValue(of({status: 'deleted'})),
        put: vi.fn(), post: vi.fn(), patch: vi.fn(),
      };
      const {service} = createService(http);
      service.deleteSystemEndpointModel('ep-1', 'my-org/my-model', 'vision').subscribe();
      const url = http.delete.mock.calls[0][0];
      expect(url).toContain('/admin/providers/endpoints/ep-1/models/my-org%2Fmy-model');
      expect(url).toContain('capability=vision');
    });

    it('defaults capability to chat on DELETE when unspecified', () => {
      const http = {
        get: vi.fn().mockReturnValue(of([])),
        delete: vi.fn().mockReturnValue(of({status: 'deleted'})),
        put: vi.fn(), post: vi.fn(), patch: vi.fn(),
      };
      const {service} = createService(http);
      service.deleteSystemEndpointModel('ep-1', 'gpt-4o').subscribe();
      expect(http.delete.mock.calls[0][0]).toContain('capability=chat');
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

    it('POSTs to /discover and returns the parsed model list', () => {
      const http = {
        get: vi.fn(),
        post: vi.fn().mockReturnValue(of({
          ok: true, status: 200, error: null, probe_url: 'http://vllm/v1/models',
          models: [{id: 'gemma', owned_by: 'vllm', capability_hint: 'chat'}],
          already_registered: [],
        })),
        put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
      };
      const {service} = createService(http);
      let got: any = null;
      service.discoverSystemEndpointModels('ep-1').subscribe((r) => (got = r));
      expect(http.post).toHaveBeenCalledWith(
        expect.stringContaining('/admin/providers/endpoints/ep-1/discover'),
        {},
      );
      expect(got.models[0].id).toBe('gemma');
    });

    it('POSTs to /models:batch with the import payload', () => {
      const http = {
        get: vi.fn().mockReturnValue(of([])),
        post: vi.fn().mockReturnValue(of({created: 2, skipped: [], created_ids: ['a', 'b']})),
        put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
      };
      const {service} = createService(http);
      service.batchCreateSystemEndpointModels('ep-1', {
        models: [
          {model_id: 'gemma', display_name: 'Gemma', capability: 'chat'},
          {model_id: 'qwen-embed', display_name: 'Qwen Embed', capability: 'embedding'},
        ],
        skip_duplicates: true,
      }).subscribe();
      const [url, body] = http.post.mock.calls[0];
      expect(url).toContain('/admin/providers/endpoints/ep-1/models:batch');
      expect(body.models).toHaveLength(2);
      expect(body.skip_duplicates).toBe(true);
      // Batch success reloads the endpoint list.
      expect(http.get).toHaveBeenCalled();
    });
  });

  describe('defaults', () => {
    it('loads all default-kind values including new embedding/vision/auxiliary slots', () => {
      const http = {
        get: vi.fn().mockReturnValue(of({
          builder: 'gpt-4o', browser: null, citation: 'gpt-4',
          embedding: 'text-embedding-3-large', vision: null, auxiliary: 'gpt-4o-mini',
        })),
        put: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
      };
      const {service} = createService(http);
      service.loadDefaults();
      expect(http.get).toHaveBeenCalledWith(expect.stringContaining('/admin/providers/defaults'));
      const defaults = service.defaults();
      expect(defaults.builder).toBe('gpt-4o');
      expect(defaults.embedding).toBe('text-embedding-3-large');
      expect(defaults.auxiliary).toBe('gpt-4o-mini');
      expect(defaults.vision).toBe(null);
    });

    it('fills missing slots with null when the server omits them', () => {
      // Older backends return only builder/browser/citation — the client
      // must still render nulls for the new slots rather than crashing.
      const http = {
        get: vi.fn().mockReturnValue(of({builder: 'gpt-4o'})),
        put: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
      };
      const {service} = createService(http);
      service.loadDefaults();
      expect(service.defaults().embedding).toBeNull();
      expect(service.defaults().vision).toBeNull();
      expect(service.defaults().auxiliary).toBeNull();
    });

    it('PUTs a new embedding default', () => {
      const http = {
        get: vi.fn().mockReturnValue(of({})),
        put: vi.fn().mockReturnValue(of({kind: 'embedding', model: 'qwen3-embedding-8b'})),
        post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
      };
      const {service} = createService(http);
      service.setDefault('embedding', 'qwen3-embedding-8b').subscribe();
      expect(http.put).toHaveBeenCalledWith(
        expect.stringContaining('/admin/providers/defaults/embedding'),
        {model: 'qwen3-embedding-8b'},
      );
    });
  });
});
