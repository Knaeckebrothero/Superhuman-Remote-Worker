import {Injector, runInInjectionContext, signal} from '@angular/core';
import {TranslocoService} from '@jsverse/transloco';
import {of} from 'rxjs';
import {describe, expect, it, vi} from 'vitest';

import {Datasource, DatasourceIndexStatus} from '../../core/models/api.model';
import {ApiService} from '../../core/services/api.service';
import {CapabilitiesService} from '../../core/services/capabilities.service';
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
      {
        provide: CapabilitiesService,
        useValue: {canPublishDatasources: () => true},
      },
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
      is_global: false,
      read_only: true,
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

describe('DatasourceListComponent publish confirmation tiers', () => {
  it('needs no confirmation for a private save', () => {
    const {component} = createComponent();
    component.openCreateForm();
    component.formData.name = 'Mine';
    expect(component.publishConfirmTier()).toBeNull();
  });

  it('warns on a read-only publish (create)', () => {
    const {component} = createComponent();
    component.openCreateForm();
    component.formData.name = 'Org Wiki';
    component.formData.is_global = true;
    expect(component.publishConfirmTier()).toBe('warn');
  });

  it('requires the typed name on a read-write publish (create)', () => {
    const {component} = createComponent();
    component.openCreateForm();
    component.formData.name = 'Org Wiki';
    component.formData.is_global = true;
    component.formData.read_only = false;
    expect(component.publishConfirmTier()).toBe('name');
  });

  it('requires the typed name on a public RO→RW flip (edit)', () => {
    const {component, ds} = createComponent();
    component.openEditForm({...ds, is_global: true, read_only: true});
    component.formData.read_only = false;
    // kb locks to read-only; use a repository-shaped edit for the flip.
    component.formData.type = 'repository';
    expect(component.publishConfirmTier()).toBe('name');
  });

  it('needs no confirmation for unpublish or RW→RO', () => {
    const {component, ds} = createComponent();
    component.openEditForm({
      ...ds,
      type: 'repository',
      is_global: true,
      read_only: false,
    });
    component.formData.is_global = false;
    expect(component.publishConfirmTier()).toBeNull();

    component.openEditForm({
      ...ds,
      type: 'repository',
      is_global: true,
      read_only: false,
    });
    component.formData.read_only = true;
    expect(component.publishConfirmTier()).toBeNull();
  });

  it('stays silent when an already-public RW datasource is edited unchanged', () => {
    const {component, ds} = createComponent();
    component.openEditForm({
      ...ds,
      type: 'repository',
      is_global: true,
      read_only: false,
    });
    expect(component.publishConfirmTier()).toBeNull();
  });

  it('sends is_global and read_only in the create payload', () => {
    const {api, component} = createComponent();
    component.openCreateForm();
    component.formData.name = 'Org Wiki';
    component.formData.type = 'repository';
    component.formData.is_global = true;
    component.gitAuthMethod = 'token';
    component.formCredentials.password = 'tok';
    component.doSave();
    const payload = api.createDatasource.mock.calls[0][0];
    expect(payload.is_global).toBe(true);
    expect(payload.read_only).toBe(true);
  });

  it('forces read_only=true for kb in the payload', () => {
    const {api, component} = createComponent();
    component.openCreateForm();
    component.formData.name = 'Org KB';
    component.formData.type = 'kb';
    component.formData.is_global = true;
    component.formData.read_only = false; // UI forbids this; belt-and-braces
    component.doSave();
    expect(api.createDatasource.mock.calls[0][0].read_only).toBe(true);
  });

  it('openEditForm seeds visibility from the datasource', () => {
    const {component, ds} = createComponent();
    component.openEditForm({...ds, is_global: true, read_only: false});
    expect(component.formData.is_global).toBe(true);
    expect(component.formData.read_only).toBe(false);
  });

  it('saveForm defers to the dialog when confirmation is needed', () => {
    const {api, component} = createComponent();
    component.openCreateForm();
    component.formData.name = 'Org Wiki';
    component.formData.type = 'repository';
    component.formData.is_global = true;
    component.gitAuthMethod = 'token';
    component.formCredentials.password = 'tok';

    component.saveForm();
    expect(api.createDatasource).not.toHaveBeenCalled();
    expect(component.showPublishConfirm()).toBe(true);
    expect(component.publishConfirmName()).toBeNull(); // warn tier

    component.onPublishConfirmed();
    expect(api.createDatasource).toHaveBeenCalledOnce();
  });
});
