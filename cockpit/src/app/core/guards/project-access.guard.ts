import {inject} from '@angular/core';
import {CanActivateFn, Router, UrlTree} from '@angular/router';
import {map, of} from 'rxjs';
import {TranslocoService} from '@jsverse/transloco';
import {ApiService} from '../services/api.service';
import {AppToastService} from '../../ui/toast';

/**
 * Pre-fetches the project before activating /projects/:id. The orchestrator
 * already gates the underlying GET behind project membership (G2), so this
 * guard is defense-in-depth UX: it skips the empty-detail flash and surfaces
 * a toast when the caller has no access.
 *
 * `ApiService.getProject` swallows HTTP errors into `null`, so we treat null
 * uniformly as "not visible / not available" — distinguishing 403 from 404
 * isn't useful here.
 */
export const projectAccessGuard: CanActivateFn = (route) => {
  const api = inject(ApiService);
  const router = inject(Router);
  const toast = inject(AppToastService);
  const transloco = inject(TranslocoService);

  const projectId = route.paramMap.get('id');
  if (!projectId) return router.parseUrl('/projects');

  return api.getProject(projectId).pipe(
    map((project): true | UrlTree => {
      if (project) return true;
      toast.danger(transloco.translate('projects.error.noAccess'));
      return router.parseUrl('/projects');
    }),
  );
};
