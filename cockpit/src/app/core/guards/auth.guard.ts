import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { toObservable } from '@angular/core/rxjs-interop';
import { filter, map, take } from 'rxjs';
import { UserService } from '../services/user.service';

/**
 * Functional auth guard that waits for the initial session check
 * to complete, then allows or redirects based on authentication state.
 */
export const authGuard: CanActivateFn = () => {
  const userService = inject(UserService);
  const router = inject(Router);

  // If session check already completed, decide immediately
  if (userService.sessionReady()) {
    return userService.isAuthenticated() || router.createUrlTree(['/login']);
  }

  // Wait for sessionReady to become true, then check auth
  return toObservable(userService.sessionReady).pipe(
    filter((ready) => ready),
    take(1),
    map(() =>
      userService.isAuthenticated() || router.createUrlTree(['/login']),
    ),
  );
};
