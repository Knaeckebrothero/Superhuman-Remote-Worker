import {Injectable, signal, computed, inject} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {catchError, of} from 'rxjs';
import {User} from '../models/api.model';
import {environment} from '../environment';
import {SessionService} from './session.service';

/**
 * Manages user identity.
 *
 * The cookie BFF flow guarantees that if a session cookie is valid, the
 * orchestrator can JIT-provision a local user row from the access token's
 * claims. The cockpit fetches the profile from `GET /api/auth/me` on
 * bootstrap (see `app.config.ts` APP_INITIALIZER) — by the time components
 * render, `currentUser()` is either populated or the user has already been
 * redirected to the BFF `/auth/login`.
 */
@Injectable({providedIn: 'root'})
export class UserService {
  private readonly http = inject(HttpClient);
  private readonly session = inject(SessionService);
  private readonly baseUrl = environment.apiUrl;

  /** All known users (loaded from API, used for color dots in job list). */
  readonly users = signal<User[]>([]);

  /** Currently authenticated user (from orchestrator). */
  readonly currentUser = signal<User | null>(null);

  /** Whether the user is authenticated. */
  readonly isAuthenticated = computed(() => this.currentUser() !== null);

  /** Always true — bootstrap completes before app render via APP_INITIALIZER. */
  readonly sessionReady = signal(true);

  /** Whether the user's account has been approved by an admin (app-side admission — the `users.is_approved` flag). */
  readonly isApproved = computed(() => this.currentUser()?.is_approved ?? false);

  /** Convenience: current user's ID (used by job-create, job-list, builder). */
  readonly currentUserId = computed(() => this.currentUser()?.id ?? null);

  // ===========================================================================
  // User Profile
  // ===========================================================================

  /** Fetch current user profile from the orchestrator (JIT-provisioned from OIDC). */
  loadCurrentUser(): void {
    this.http
      .get<{ user: User }>(`${this.baseUrl}/auth/me`)
      .pipe(catchError(() => of(null)))
      .subscribe((res) => {
        if (res?.user) {
          this.currentUser.set(res.user);
          this.session.authenticated.set(true);
        }
      });
  }

  /** Log out via the BFF (revoke server-side session + KC RP-initiated logout). */
  async logout(): Promise<void> {
    this.currentUser.set(null);
    await this.session.logout();
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
