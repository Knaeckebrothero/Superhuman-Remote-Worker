import {describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {of} from 'rxjs';
import {AdminConfigService, coerceOverrideValue} from './admin-config.service';

function overrideRow(extra: Record<string, unknown>) {
  return {
    id: 'x', family: null, kind: 'settings', name: 'n', content: null,
    content_format: null, value_json: null, notes: null, created_by: null,
    updated_by: null, created_at: null, updated_at: null, ...extra,
  };
}

function createService(mockHttp?: any) {
  const http = mockHttp ?? {
    get: vi.fn().mockReturnValue(of([])),
    post: vi.fn().mockReturnValue(of({})),
    delete: vi.fn().mockReturnValue(of({deleted: true})),
  };
  const injector = Injector.create({
    providers: [{provide: HttpClient, useValue: http}],
  });
  const service = runInInjectionContext(injector, () => new AdminConfigService());
  return {service, http};
}

describe('AdminConfigService', () => {
  it('loads overrides from /admin/config/overrides', () => {
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
      expect.stringContaining('/admin/config/overrides'),
    );
    expect(service.overrides()).toHaveLength(1);
  });

  it('loads the catalog from /admin/config/catalog', () => {
    const http = {
      get: vi.fn().mockReturnValue(
        of([{kind: 'prompts', name: 'persona', title: 'Persona', description: 'd'}]),
      ),
      post: vi.fn(), delete: vi.fn(),
    };
    const {service} = createService(http);
    service.loadCatalog();
    expect(http.get).toHaveBeenCalledWith(
      expect.stringContaining('/admin/config/catalog'),
    );
    expect(service.catalog()).toHaveLength(1);
  });

  it('GETs the bundled default, mapping a null (global) family to "_"', () => {
    const http = {get: vi.fn().mockReturnValue(of({})), post: vi.fn(), delete: vi.fn()};
    const {service} = createService(http);
    service.getBundled(null, 'prompts', 'persona').subscribe();
    expect(http.get).toHaveBeenCalledWith(
      expect.stringContaining('/admin/config/bundled/_/prompts/persona'),
    );
    service.getBundled('gemma', 'prompts', 'persona').subscribe();
    expect(http.get).toHaveBeenCalledWith(
      expect.stringContaining('/admin/config/bundled/gemma/prompts/persona'),
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
      expect.stringContaining('/admin/config/overrides'),
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
      expect.stringContaining('/admin/config/overrides/abc'),
    );
    expect(http.get).toHaveBeenCalled(); // tap() reload
  });

  it('decodes JSON-encoded value_json on load (asyncpg returns JSONB as text)', () => {
    const http = {
      get: vi.fn().mockReturnValue(
        of([
          overrideRow({name: 'temperature', value_json: '1'}),
          overrideRow({name: 'parallel_tool_calls', value_json: 'false'}),
          overrideRow({name: 'guardrails', kind: 'guardrails', value_json: '{"nudges":[]}'}),
        ]),
      ),
      post: vi.fn(), delete: vi.fn(),
    };
    const {service} = createService(http);
    service.loadOverrides();
    const rows = service.overrides();
    expect(rows.find((o) => o.name === 'temperature')!.value_json).toBe(1);
    expect(rows.find((o) => o.name === 'parallel_tool_calls')!.value_json).toBe(false);
    expect(rows.find((o) => o.name === 'guardrails')!.value_json).toEqual({nudges: []});
  });

  it('coerceOverrideValue leaves real values and unparseable strings untouched', () => {
    expect(coerceOverrideValue(overrideRow({value_json: true})).value_json).toBe(true);
    expect(coerceOverrideValue(overrideRow({value_json: 0.3})).value_json).toBe(0.3);
    expect(coerceOverrideValue(overrideRow({value_json: 'not json{'})).value_json).toBe('not json{');
  });

  it('getBundledSettings fans out one bundled GET per leaf, keyed by name', () => {
    const http = {
      get: vi.fn().mockImplementation((url: string) =>
        of({family: null, content: url.includes('temperature') ? 0.3 : false}),
      ),
      post: vi.fn(), delete: vi.fn(),
    };
    const {service} = createService(http);
    let result: Record<string, unknown> | undefined;
    service.getBundledSettings(null, ['temperature', 'parallel_tool_calls']).subscribe((r) => {
      result = r;
    });
    expect(result).toEqual({temperature: 0.3, parallel_tool_calls: false});
  });

  it('getBundledSettings resolves to {} without a request when there are no names', () => {
    const http = {get: vi.fn(), post: vi.fn(), delete: vi.fn()};
    const {service} = createService(http);
    let result: Record<string, unknown> | undefined;
    service.getBundledSettings(null, []).subscribe((r) => {
      result = r;
    });
    expect(result).toEqual({});
    expect(http.get).not.toHaveBeenCalled();
  });
});
