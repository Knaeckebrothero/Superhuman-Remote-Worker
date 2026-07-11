import {Injector, runInInjectionContext, signal} from '@angular/core';
import {TranslocoService} from '@jsverse/transloco';
import {of} from 'rxjs';
import {describe, expect, it, vi} from 'vitest';

import {Datasource, DatasourceIndexStatus} from '../../core/models/api.model';
import {ApiService} from '../../core/services/api.service';
import {UserService} from '../../core/services/user.service';
import {ViewportService} from '../../core/services/viewport.service';
import {DatasourceListComponent} from './datasource-list.component';

function kbDatasource(overrides: Partial<Datasource> = {}): Datasource {
  return {
    id: 'kb-1',
    name: 'Engineering Handbook',
    description: 'Team knowledge',
    type: 'kb',
    connection_url: 'https://github.com/acme/handbook.git',
    cli_hint: null,
    default_branch: 'main',
    config: {root_path: 'vault'},
    job_id: null,
    created_by: 'user-1',
    created_at: '',
    updated_at: '',
    ...overrides,
  };
}

function createComponent() {
  const ds = kbDatasource();
  const api = {
    getDatasources: vi.fn().mockReturnValue(of([])),
    getDatasourceIndexStatus: vi.fn().mockReturnValue(of(null)),
    createDatasource: vi.fn().mockReturnValue(of(ds)),
    updateDatasource: vi.fn().mockReturnValue(of({status: 'updated'})),
    testDatasource: vi.fn().mockReturnValue(of({status: 'ok', message: 'ok'})),
    reindexDatasource: vi.fn().mockReturnValue(
      of({status: 'ready', indexed_commit: 'abcdef123456', full: false}),
    ),
    deleteDatasource: vi.fn().mockReturnValue(of({status: 'deleted'})),
    generateSshKey: vi.fn().mockReturnValue(of(null)),
  };
  const currentUser = signal({id: 'user-1', is_admin: false});
  const injector = Injector.create({
    providers: [
      {provide: ApiService, useValue: api},
      {
        provide: TranslocoService,
        useValue: {
          translate: (key: string, params?: Record<string, string>) =>
            params?.['sha'] ? `${key}:${params['sha']}` : key,
        },
      },
      {provide: ViewportService, useValue: {isMobile: signal(false)}},
      {provide: UserService, useValue: {currentUser}},
    ],
  });
  const component = runInInjectionContext(
    injector,
    () => new DatasourceListComponent(),
  );
  return {api, component, currentUser, ds};
}

describe('DatasourceListComponent OKF Knowledge Base support', () => {
  it('authors a KB with repository auth, branch, and OKF root config', () => {
    const {api, component} = createComponent();
    component.openCreateForm();
    component.formData = {
      name: 'Engineering Handbook',
      type: 'kb',
      connection_url: 'https://github.com/acme/handbook.git',
      description: 'Team knowledge',
      cli_hint: '',
      default_branch: 'main',
      root_path: 'vault',
    };
    component.gitAuthMethod = 'token';
    component.formCredentials.password = 'secret-token';

    component.saveForm();

    expect(api.createDatasource).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'kb',
        default_branch: 'main',
        config: {root_path: 'vault'},
        credentials: {auth_method: 'token', token: 'secret-token'},
      }),
    );
  });

  it('loads root config for editing and leaves blank credentials untouched', () => {
    const {api, component, ds} = createComponent();
    component.openEditForm(ds);
    expect(component.formData.root_path).toBe('vault');
    component.gitAuthMethod = 'ssh';
    component.gitSshKey = '';

    component.saveForm();

    expect(api.updateDatasource).toHaveBeenCalledWith(
      ds.id,
      expect.objectContaining({
        config: {root_path: 'vault'},
        credentials: undefined,
      }),
    );
  });

  it('runs normal and confirmed full rebuilds', () => {
    const {api, component, ds} = createComponent();
    const confirmSpy = vi.spyOn(globalThis, 'confirm').mockReturnValue(true);

    component.reindexDatasource(ds, false);
    component.reindexDatasource(ds, true);

    expect(api.reindexDatasource).toHaveBeenNthCalledWith(1, ds.id, false);
    expect(api.reindexDatasource).toHaveBeenNthCalledWith(2, ds.id, true);
    expect(confirmSpy).toHaveBeenCalledOnce();
    confirmSpy.mockRestore();
  });

  it('loads and formats credential-redacted index status', () => {
    const {api, component, ds} = createComponent();
    const status: DatasourceIndexStatus = {
      datasource_id: ds.id,
      status: 'ready',
      source_head: 'abcdef1234567890',
      indexed_commit: 'abcdef1234567890',
      pipeline_version: null,
      repo_name: null,
      branch: 'main',
      last_attempt_at: '2026-07-11T12:00:00Z',
      last_success_at: '2026-07-11T12:00:00Z',
      last_error: null,
    };
    api.getDatasources.mockReturnValue(of([ds]));
    api.getDatasourceIndexStatus.mockReturnValue(of(status));

    component.refresh();

    expect(component.indexStatuses()[ds.id]).toEqual(status);
    expect(component.indexStatusLabel(status)).toBe(
      'datasources.table.indexReady:abcdef12',
    );
    expect(
      component.redactIndexError(
        'fetch https://token-value@example.test failed token=token-value',
      ),
    ).toBe('fetch https://***@example.test failed token=***');
  });

  it('limits management actions to the creator or an admin', () => {
    const {component, currentUser, ds} = createComponent();
    expect(component.canManage(ds)).toBe(true);
    currentUser.set({id: 'other-user', is_admin: false});
    expect(component.canManage(ds)).toBe(false);
    currentUser.set({id: 'admin', is_admin: true});
    expect(component.canManage(ds)).toBe(true);
  });
});
