import {describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {of} from 'rxjs';
import {AdminPromptsService} from './admin-prompts.service';

function createService(mockHttp?: any) {
  const http = mockHttp ?? {
    get: vi.fn().mockReturnValue(of([])),
    post: vi.fn().mockReturnValue(of({})),
    delete: vi.fn().mockReturnValue(of({deleted: true})),
  };
  const injector = Injector.create({
    providers: [{provide: HttpClient, useValue: http}],
  });
  const service = runInInjectionContext(injector, () => new AdminPromptsService());
  return {service, http};
}

describe('AdminPromptsService', () => {
  it('loads overrides from /admin/prompts/overrides', () => {
    const http = {
      get: vi.fn().mockReturnValue(
        of([
          {
            id: '1', family: 'gemma', kind: 'prompts', name: 'persona', content: 'X',
            content_format: 'text', notes: null, created_by: null, updated_by: null,
            created_at: null, updated_at: null,
          },
        ]),
      ),
      post: vi.fn(), delete: vi.fn(),
    };
    const {service} = createService(http);
    service.loadOverrides();
    expect(http.get).toHaveBeenCalledWith(
      expect.stringContaining('/admin/prompts/overrides'),
    );
    expect(service.overrides()).toHaveLength(1);
  });

  it('loads the catalog from /admin/prompts/catalog', () => {
    const http = {
      get: vi.fn().mockReturnValue(
        of([{kind: 'prompts', name: 'persona', title: 'Persona', description: 'd'}]),
      ),
      post: vi.fn(), delete: vi.fn(),
    };
    const {service} = createService(http);
    service.loadCatalog();
    expect(http.get).toHaveBeenCalledWith(
      expect.stringContaining('/admin/prompts/catalog'),
    );
    expect(service.catalog()).toHaveLength(1);
  });

  it('GETs the bundled default, mapping a null (global) family to "_"', () => {
    const http = {get: vi.fn().mockReturnValue(of({})), post: vi.fn(), delete: vi.fn()};
    const {service} = createService(http);
    service.getBundled(null, 'prompts', 'persona').subscribe();
    expect(http.get).toHaveBeenCalledWith(
      expect.stringContaining('/admin/prompts/bundled/_/prompts/persona'),
    );
    service.getBundled('gemma', 'prompts', 'persona').subscribe();
    expect(http.get).toHaveBeenCalledWith(
      expect.stringContaining('/admin/prompts/bundled/gemma/prompts/persona'),
    );
  });

  it('POSTs an override and reloads the list', () => {
    const http = {
      get: vi.fn().mockReturnValue(of([])),
      post: vi.fn().mockReturnValue(of({id: '1'})),
      delete: vi.fn(),
    };
    const {service} = createService(http);
    service
      .createOverride({family: 'gemma', kind: 'prompts', name: 'persona', content: 'NEW'})
      .subscribe();
    expect(http.post).toHaveBeenCalledWith(
      expect.stringContaining('/admin/prompts/overrides'),
      expect.objectContaining({family: 'gemma', kind: 'prompts', name: 'persona', content: 'NEW'}),
    );
    expect(http.get).toHaveBeenCalled(); // tap() reload
  });

  it('DELETEs an override and reloads the list', () => {
    const http = {
      get: vi.fn().mockReturnValue(of([])),
      post: vi.fn(),
      delete: vi.fn().mockReturnValue(of({deleted: true})),
    };
    const {service} = createService(http);
    service.deleteOverride('abc').subscribe();
    expect(http.delete).toHaveBeenCalledWith(
      expect.stringContaining('/admin/prompts/overrides/abc'),
    );
    expect(http.get).toHaveBeenCalled(); // tap() reload
  });
});
