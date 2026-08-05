import {Injector, runInInjectionContext, signal} from '@angular/core';
import {ActivatedRoute, Router} from '@angular/router';
import {TranslocoService} from '@jsverse/transloco';
import {of, Subject, throwError} from 'rxjs';
import {afterEach, describe, expect, it, vi} from 'vitest';

import {Datasource, ProjectDatasource} from '../../core/models/api.model';
import {ApiService} from '../../core/services/api.service';
import {CapabilitiesService} from '../../core/services/capabilities.service';
import {UserService} from '../../core/services/user.service';
import {ViewportService} from '../../core/services/viewport.service';
import {ProjectDetailPageComponent} from './project-detail.component';

function datasource(id: string, overrides: Partial<Datasource> = {}): Datasource {
  return {
    id,
    name: id,
    description: null,
    type: 'postgresql',
    connection_url: 'postgresql://db/app',
    cli_hint: null,
    default_branch: null,
    job_id: null,
    created_by: 'user-1',
    is_global: false,
    scope_mode: 'all',
    created_at: '',
    updated_at: '',
    ...overrides,
  };
}

function linkedDatasource(id: string): ProjectDatasource {
  return {
    ...datasource(id),
    linked_at: '',
    project_read_only: null,
    project_description: null,
  };
}

function createComponent(policyAvailable = true) {
  const api = {
    getProjectDatasources: vi.fn().mockReturnValue(of([])),
    getLinkableProjectDatasources: vi.fn().mockReturnValue(
      of({items: [], next_cursor: null}),
    ),
    getEligibleDatasources: vi.fn().mockReturnValue(of([])),
    getDatasources: vi.fn().mockReturnValue(of([])),
    linkProjectDatasource: vi.fn().mockReturnValue(of({status: 'linked'})),
  };
  const currentUser = signal({id: 'user-1', is_admin: false});
  const injector = Injector.create({
    providers: [
      {provide: ApiService, useValue: api},
      {provide: ActivatedRoute, useValue: {paramMap: of({get: () => 'project-a'})}},
      {provide: Router, useValue: {navigate: vi.fn()}},
      {provide: UserService, useValue: {currentUser}},
      {
        provide: CapabilitiesService,
        useValue: {
          datasourceScopeAutoAttachAvailable: () => policyAvailable,
          datasourceScopeAutoAttachAvailability$: of(policyAvailable),
        },
      },
      {
        provide: TranslocoService,
        useValue: {translate: (key: string) => key, getActiveLang: () => 'en'},
      },
      {provide: ViewportService, useValue: {isMobile: signal(false)}},
    ],
  });
  const component = runInInjectionContext(
    injector,
    () => new ProjectDetailPageComponent(),
  );
  (component as unknown as {projectId: string}).projectId = 'project-a';
  return {api, component, currentUser};
}

describe('ProjectDetailPageComponent connector candidates', () => {
  afterEach(() => vi.useRealTimers());

  it('loads v1 candidates from the target-aware linkable catalog', () => {
    const {api, component} = createComponent();
    const candidate = datasource('owned');
    api.getLinkableProjectDatasources.mockReturnValue(
      of({items: [candidate], next_cursor: 'next-page'}),
    );

    component.loadProjectDatasources();

    expect(api.getLinkableProjectDatasources).toHaveBeenCalledWith('project-a', {
      q: undefined,
      cursor: undefined,
      limit: 50,
    });
    expect(api.getEligibleDatasources).not.toHaveBeenCalled();
    expect(api.getDatasources).not.toHaveBeenCalled();
    expect(component.availableDatasources()).toEqual([candidate]);
    expect(component.datasourceCandidatesNextCursor()).toBe('next-page');
  });

  it('retains the legacy visible-connector list while v1 is unavailable', () => {
    const {api, component} = createComponent(false);
    const candidate = datasource('legacy');
    api.getDatasources.mockReturnValue(of([candidate]));

    component.loadProjectDatasources();

    expect(api.getDatasources).toHaveBeenCalledOnce();
    expect(api.getLinkableProjectDatasources).not.toHaveBeenCalled();
    expect(component.availableDatasources()).toEqual([candidate]);
  });

  it('never presents restricted, native, or already-linked rows as candidates', () => {
    const {api, component} = createComponent();
    api.getProjectDatasources.mockReturnValue(of([linkedDatasource('already-linked')]));
    api.getLinkableProjectDatasources.mockReturnValue(of({
      items: [
        datasource('owned-restricted', {
          scope_mode: 'projects',
          project_ids: ['project-b'],
        }),
        datasource('shared-public', {
          created_by: 'user-2',
          is_global: true,
          scope_mode: 'all',
        }),
        datasource('other-private', {created_by: 'user-2'}),
        datasource('other-project-scoped', {
          created_by: 'user-2',
          is_global: true,
          scope_mode: 'projects',
        }),
        datasource('native', {config: {native_project_id: 'project-b'}}),
        datasource('already-linked'),
      ],
      next_cursor: null,
    }));

    component.loadProjectDatasources();

    expect(component.availableDatasources().map(ds => ds.id)).toEqual([
      'owned-restricted',
      'shared-public',
    ]);
  });

  it('debounces search into the server contract and clears a hidden selection', () => {
    vi.useFakeTimers();
    const {api, component} = createComponent();
    const database = datasource('database', {name: 'Application Database'});
    const repository = datasource('repository', {
      name: 'Source Repository',
      type: 'repository',
      connection_url: 'https://example.test/repo.git',
    });
    api.getLinkableProjectDatasources
      .mockReturnValueOnce(of({items: [database, repository], next_cursor: null}))
      .mockReturnValueOnce(of({items: [repository], next_cursor: null}));
    component.loadDatasourceCandidates();
    component.dsLinkId.set('database');

    component.onDatasourceCandidateSearch('source');
    vi.advanceTimersByTime(250);

    expect(api.getLinkableProjectDatasources).toHaveBeenLastCalledWith('project-a', {
      q: 'source',
      cursor: undefined,
      limit: 50,
    });
    expect(component.availableDatasources().map(ds => ds.id)).toEqual(['repository']);
    expect(component.dsLinkId()).toBe('');
  });

  it('paginates and de-duplicates server-authorized candidates', () => {
    const {api, component} = createComponent();
    const first = datasource('first');
    const second = datasource('second');
    api.getLinkableProjectDatasources
      .mockReturnValueOnce(of({items: [first], next_cursor: 'cursor-1'}))
      .mockReturnValueOnce(of({items: [first, second], next_cursor: null}));

    component.loadDatasourceCandidates();
    component.loadMoreDatasourceCandidates();

    expect(api.getLinkableProjectDatasources).toHaveBeenLastCalledWith('project-a', {
      q: undefined,
      cursor: 'cursor-1',
      limit: 50,
    });
    expect(component.allDatasources().map(ds => ds.id)).toEqual(['first', 'second']);
    expect(component.datasourceCandidatesNextCursor()).toBeNull();
  });

  it('ignores stale v1 responses after a newer request starts', () => {
    const {api, component} = createComponent();
    const stale = new Subject<{items: Datasource[]; next_cursor: string | null}>();
    const fresh = new Subject<{items: Datasource[]; next_cursor: string | null}>();
    api.getLinkableProjectDatasources
      .mockReturnValueOnce(stale)
      .mockReturnValueOnce(fresh);

    component.loadDatasourceCandidates();
    component.loadDatasourceCandidates();
    stale.next({items: [datasource('stale')], next_cursor: null});
    fresh.next({items: [datasource('fresh')], next_cursor: null});

    expect(component.allDatasources().map(ds => ds.id)).toEqual(['fresh']);
  });

  it('fails closed and removes stale candidates when v1 loading fails', () => {
    const {api, component} = createComponent();
    component.allDatasources.set([datasource('stale')]);
    component.dsLinkId.set('stale');
    api.getLinkableProjectDatasources.mockReturnValue(
      throwError(() => new Error('unavailable')),
    );

    component.loadDatasourceCandidates();

    expect(component.datasourceCandidatesError()).toBe(true);
    expect(component.allDatasources()).toEqual([]);
    expect(component.dsLinkId()).toBe('');
  });

  it('links only a currently authorized candidate and keeps KB links read-only', () => {
    const {api, component} = createComponent();
    const kb = datasource('kb', {
      type: 'kb',
      connection_url: 'https://example.test/knowledge.git',
    });
    component.allDatasources.set([kb]);
    component.dsLinkId.set(kb.id);

    component.linkDatasource();

    expect(api.linkProjectDatasource).toHaveBeenCalledWith(
      'project-a',
      kb.id,
      {read_only: true},
    );
  });
});
