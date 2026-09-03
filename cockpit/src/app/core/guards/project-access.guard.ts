import {inject} from '@angular/core';
import {CanActivateFn, Router, UrlTree} from '@angular/router';
import {map} from 'rxjs';
import {TranslocoService} from '@jsverse/transloco';
import {ApiService} from '../services/api.service';
import {AppToastService} from '../../ui/toast';

/**
 * Maps a failed project fetch to the message the user actually needs.
 *
 * Conflating 403 and 404 is deliberate — telling a stranger whether a project
 * exists is itself a disclosure, and "no access" covers both honestly. A 5xx
 * is a different animal: the server broke, and saying "no access" there sends
 * the reader hunting for a permission they already have.
 */
function messageKeyFor(status: number | null): string {
  if (status === 403 || status === 404) return 'projects.error.noAccess';
  return 'projects.error.loadFailed';
}

/**
 * Pre-fetches the project before activating /projects/:id. The orchestrator
 * already gates the underlying GET behind project membership (G2), so this
 * guard is defense-in-depth UX: it skips the empty-detail flash and surfaces
 * a toast when the project can't be loaded.
 */
export const projectAccessGuard: CanActivateFn = (route) => {
  const api = inject(ApiService);
  const router = inject(Router);
  const toast = inject(AppToastService);
  const transloco = inject(TranslocoService);

  const projectId = route.paramMap.get('id');
  if (!projectId) return router.parseUrl('/projects');

  return api.getProjectOrError(projectId).pipe(
    map(({project, status}): true | UrlTree => {
      if (project) return true;
      toast.danger(transloco.translate(messageKeyFor(status)));
      return router.parseUrl('/projects');
    }),
  );
};
