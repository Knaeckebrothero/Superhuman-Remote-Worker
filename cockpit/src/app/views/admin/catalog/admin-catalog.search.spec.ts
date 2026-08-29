import {CUSTOM_ELEMENTS_SCHEMA, signal, ɵresolveComponentResources} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {beforeAll, beforeEach, describe, expect, it, vi} from 'vitest';
import {of, Subject} from 'rxjs';
import {AdminModelsService} from '../../../core/services/admin-models.service';
import {AdminProvidersService} from '../../../core/services/admin-providers.service';
import {AdminModelsCoordinatorService} from '../models/admin-models-coordinator.service';
import {AdminCatalogComponent} from './admin-catalog.component';

const createModel = vi.fn(() => of({} as never));

const models = {
  models: signal([]),
  families: signal(['default']),
  familyDefaults: signal<Record<string, number>>({}),
  loading: signal(false),
  loadModels: vi.fn(),
  loadFamilies: vi.fn(),
  detectFamily: vi.fn(() => of({family: 'default', source: 'fallback'})),
  createModel,
  updateModel: vi.fn(() => of({})),
  deleteModel: vi.fn(() => of({})),
  testModel: vi.fn(() => of({})),
};

const providers = {
  systemApiKeys: signal([]),
  systemEndpoints: signal([]),
  codexAvailability: signal({
    available: false,
    account_count: 0,
    models: [],
    proxy_url: null,
    endpoint_id: null,
  }),
  loadSystemApiKeys: vi.fn(),
  loadSystemEndpoints: vi.fn(),
  loadCodexAvailability: vi.fn(),
  discoverSystemEndpointModels: vi.fn(() => of({models: []})),
};

describe('AdminCatalogComponent search/fetch form', () => {
  beforeAll(async () => {
    await ɵresolveComponentResources(() => Promise.resolve(''));
  });

  beforeEach(() => {
    createModel.mockClear();
    TestBed.configureTestingModule({
      imports: [AdminCatalogComponent],
      providers: [
        {provide: AdminModelsService, useValue: models},
        {provide: AdminProvidersService, useValue: providers},
        {
          provide: AdminModelsCoordinatorService,
          useValue: {discoverEndpoint$: new Subject<string>()},
        },
      ],
    });
    TestBed.overrideComponent(AdminCatalogComponent, {
      set: {imports: [], schemas: [CUSTOM_ELEMENTS_SCHEMA]},
    });
  });

  it('hides chat-only fields and offers only registered adapter names', () => {
    const fixture = TestBed.createComponent(AdminCatalogComponent);
    const component = fixture.componentInstance;
    component.formCapabilities.set(['search']);
    fixture.detectChanges();

    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('.chat-model-config')).toBeNull();
    expect(host.querySelector('.research-config')).not.toBeNull();
    const adapters = Array.from(
      host.querySelectorAll<HTMLOptionElement>('.research-config app-select option'),
    )
      .map((option) => option.value)
      .filter(Boolean);
    expect(adapters).toEqual(['brave', 'searxng', 'tavily']);
  });

  it('requires adapter and matching operations, then writes params_json', () => {
    const fixture = TestBed.createComponent(AdminCatalogComponent);
    const component = fixture.componentInstance;
    component.formProviderKey.set('system:tavily');
    component.formModelId.set('tavily');
    component.formDisplayLabel.set('Tavily');
    component.formCapabilities.set(['search', 'fetch']);

    expect(component.canSubmit()).toBe(false);
    component.onSearchProviderChange('tavily');
    expect(component.canSubmit()).toBe(false);
    component.toggleFormSearchOp('search', true);
    expect(component.canSubmit()).toBe(false);
    component.toggleFormSearchOp('extract', true);
    expect(component.canSubmit()).toBe(true);

    component.submit();
    expect(createModel).toHaveBeenCalledWith(expect.objectContaining({
      capabilities: ['search', 'fetch'],
      family: 'default',
      context_window: null,
      params_json: {provider: 'tavily', ops: ['search', 'extract']},
    }));
  });
});
