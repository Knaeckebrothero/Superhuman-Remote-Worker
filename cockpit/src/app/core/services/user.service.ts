import { Injectable, signal, computed, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { catchError, of } from 'rxjs';
import { User } from '../models/api.model';
import { KeycloakService } from './keycloak.service';
import { environment } from '../environment';

/**
 * Manages user identity via Keycloak OIDC.
 *
 * On app start (if authenticated), fetches the local user profile from the
 * orchestrator via GET /api/auth/me (which JIT-provisions the user row from
 * the Keycloak token on first login).
 */
@Injectable({ providedIn: 'root' })
export class UserService {
  private readonly http = inject(HttpClient);
  private readonly keycloak = inject(KeycloakService);
  private readonly baseUrl = environment.apiUrl;

  /** All known users (loaded from API, used for color dots in job list). */
  readonly users = signal<User[]>([]);

  /** Currently authenticated user (from orchestrator). */
  readonly currentUser = signal<User | null>(null);

  /** Whether the user is authenticated (delegates to Keycloak). */
  readonly isAuthenticated = computed(() => this.keycloak.authenticated && this.currentUser() !== null);

  /** Always true — Keycloak init completes before app bootstrap via APP_INITIALIZER. */
  readonly sessionReady = signal(true);

  /** Whether the user's account has been approved by an admin (has 'user' role in Keycloak). */
  readonly isApproved = computed(() => this.currentUser()?.is_approved ?? false);

  /** Convenience: current user's ID (used by job-create, job-list, builder). */
  readonly currentUserId = computed(() => this.currentUser()?.id ?? null);

  constructor() {
    if (this.keycloak.authenticated) {
      this.loadCurrentUser();
      this.loadUsers();
    }
  }

  // ===========================================================================
  // User Profile
  // ===========================================================================

  /** Fetch current user profile from orchestrator (JIT-provisioned from OIDC). */
  loadCurrentUser(): void {
    this.http
      .get<{ user: User }>(`${this.baseUrl}/auth/me`)
      .pipe(catchError(() => of(null)))
      .subscribe((res) => {
        if (res?.user) {
          this.currentUser.set(res.user);
        }
      });
  }

  /** Logout via Keycloak (redirects to Keycloak end-session endpoint). */
  logout(): void {
    this.currentUser.set(null);
    this.keycloak.logout();
  }

  // ===========================================================================
  // User Management
  // ===========================================================================

  /** Fetch users from the API (for user color dots in job list etc.). */
  loadUsers(): void {
    this.http
      .get<User[]>(`${this.baseUrl}/users`)
      .pipe(catchError(() => of([])))
      .subscribe((users) => {
        this.users.set(users);
      });
  }

  /** Create a new user via the API and refresh the list. */
  createUser(displayName: string, avatarColor: string = '#89b4fa', email?: string): void {
    const body: Record<string, string> = {
      display_name: displayName,
      avatar_color: avatarColor,
    };
    if (email) body['email'] = email;

    this.http
      .post<User>(`${this.baseUrl}/users`, body)
      .pipe(catchError(() => of(null)))
      .subscribe((user) => {
        if (user) {
          this.loadUsers();
        }
      });
  }
}
