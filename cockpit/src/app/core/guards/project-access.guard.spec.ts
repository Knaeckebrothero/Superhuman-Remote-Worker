import {describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {ActivatedRouteSnapshot, Router, RouterStateSnapshot} from '@angular/router';
import {of, firstValueFrom, isObservable} from 'rxjs';
import {TranslocoService} from '@jsverse/transloco';
import {ApiService} from '../services/api.service';
import {AppToastService} from '../../ui/toast';
import {projectAccessGuard} from './project-access.guard';

/**
 * Spec for `projectAccessGuard`. Uses `Injector.create` + manual mocks,
 * matching `view-as.interceptor.spec.ts`.
 *
 * The regression this pins: a 500 from `GET /api/projects/{id}` reached the
 * user as "You don't have access to that project", which is not merely vague
 * but the opposite of true — `require_project_member` had already passed and
 * the handler crashed afterwards, building a cloud deep-link. The guard must
 * distinguish a refusal from a breakage.
 */

const PROJECT_ID = 'a572e4a0-d97a-4103-91fd-92a980d6717d';

function setup(result: {project: unknown; status: number | null}) {
  const api = {
    getProjectOrError: vi.fn().mockReturnValue(of(result)),
  } as unknown as ApiService;

  const toast = {danger: vi.fn()} as unknown as AppToastService;
  const projectsUrl = {toString: () => '/projects'};
  const router = {
    parseUrl: vi.fn().mockReturnValue(projectsUrl),
  } as unknown as Router;
  const transloco = {
    translate: vi.fn((key: string) => key),
  } as unknown as TranslocoService;

  const injector = Injector.create({
    providers: [
      {provide: ApiService, useValue: api},
      {provide: AppToastService, useValue: toast},
      {provide: Router, useValue: router},
      {provide: TranslocoService, useValue: transloco},
    ],
  });

  const route = {
    paramMap: {get: (k: string) => (k === 'id' ? PROJECT_ID : null)},
  } as unknown as ActivatedRouteSnapshot;

  const run = async () => {
    const out = runInInjectionContext(injector, () =>
      projectAccessGuard(route, {} as RouterStateSnapshot),
    );
    return isObservable(out) ? await firstValueFrom(out) : await out;
  };

  return {run, api, toast, router, transloco, projectsUrl};
}

describe('projectAccessGuard', () => {
  it('activates the route when the project loads', async () => {
    const {run, toast} = setup({project: {id: PROJECT_ID}, status: 200});
    await expect(run()).resolves.toBe(true);
    expect(toast.danger).not.toHaveBeenCalled();
  });

  it('reports a 403 as no-access', async () => {
    const {run, toast, projectsUrl} = setup({project: null, status: 403});
    await expect(run()).resolves.toBe(projectsUrl);
    expect(toast.danger).toHaveBeenCalledWith('projects.error.noAccess');
  });

  it('reports a 404 as no-access — existence is itself a disclosure', async () => {
    const {run, toast} = setup({project: null, status: 404});
    await run();
    expect(toast.danger).toHaveBeenCalledWith('projects.error.noAccess');
  });

  it('does NOT report a 500 as no-access', async () => {
    const {run, toast} = setup({project: null, status: 500});
    await run();
    expect(toast.danger).toHaveBeenCalledWith('projects.error.loadFailed');
    expect(toast.danger).not.toHaveBeenCalledWith('projects.error.noAccess');
  });

  it('treats a network failure with no status as a load failure', async () => {
    const {run, toast} = setup({project: null, status: null});
    await run();
    expect(toast.danger).toHaveBeenCalledWith('projects.error.loadFailed');
  });

  it('redirects to /projects without fetching when the route has no id', async () => {
    const {api, router} = setup({project: null, status: 500});
    const injector = Injector.create({
      providers: [
        {provide: ApiService, useValue: api},
        {provide: AppToastService, useValue: {danger: vi.fn()}},
        {provide: Router, useValue: router},
        {provide: TranslocoService, useValue: {translate: (k: string) => k}},
      ],
    });
    const route = {
      paramMap: {get: () => null},
    } as unknown as ActivatedRouteSnapshot;

    runInInjectionContext(injector, () =>
      projectAccessGuard(route, {} as RouterStateSnapshot),
    );
    expect(api.getProjectOrError).not.toHaveBeenCalled();
    expect(router.parseUrl).toHaveBeenCalledWith('/projects');
  });
});
