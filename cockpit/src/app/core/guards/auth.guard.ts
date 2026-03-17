import { inject } from '@angular/core';
import { CanActivateFn } from '@angular/router';
import { KeycloakService } from '../services/keycloak.service';

/**
 * Route guard that redirects unauthenticated users to Keycloak login.
 *
 * Because Keycloak is initialised via APP_INITIALIZER before the app boots,
 * `keycloak.authenticated` is already resolved — no async waiting needed.
 */
export const authGuard: CanActivateFn = () => {
  const keycloak = inject(KeycloakService);

  if (keycloak.authenticated) {
    return true;
  }

  // Redirect to Keycloak login page
  keycloak.login();
  return false;
};
