import { Injectable, signal, computed, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, catchError, map, of, tap } from 'rxjs';
import { User } from '../models/api.model';
import { ApiService } from './api.service';
import { environment } from '../environment';

/**
 * Manages user identity via session-based authentication.
 *
 * Login: POST /api/auth/login with email → backend sets httpOnly session cookie.
 * Session check: GET /api/auth/me → returns current user from session.
 * Logout: POST /api/auth/logout → clears session cookie.
 */
@Injectable({ providedIn: 'root' })
export class UserService {
  private readonly http = inject(HttpClient);
  private readonly api = inject(ApiService);
  private readonly baseUrl = environment.apiUrl;

  /** All known users (loaded from API, used for color dots in job list). */
  readonly users = signal<User[]>([]);

  /** Currently authenticated user (from session). */
  readonly currentUser = signal<User | null>(null);

  /** Whether the user is authenticated. */
  readonly isAuthenticated = computed(() => this.currentUser() !== null);

  /** Whether the initial session check has completed (used by auth guard). */
  readonly sessionReady = signal(false);

  /** Convenience: current user's ID (used by job-create, job-list, builder). */
  readonly currentUserId = computed(() => this.currentUser()?.id ?? null);

  constructor() {
    this.checkSession();
    this.loadUsers();
  }

  /** Check if we have a valid session (called on init). */
  checkSession(): void {
    this.http
      .get<{ user: User; csrf_token?: string }>(`${this.baseUrl}/auth/me`, { withCredentials: true })
      .pipe(catchError(() => of(null)))
      .subscribe((res) => {
        if (res?.user) {
          this.currentUser.set(res.user);
          if (res.csrf_token) {
            this.api.csrfToken = res.csrf_token;
          }
        } else {
          this.currentUser.set(null);
        }
        this.sessionReady.set(true);
      });
  }

  /** Login with email address. Returns observable so callers can react to completion. */
  login(email: string): Observable<User | null> {
    return this.http
      .post<{ user: User; csrf_token?: string; message: string }>(
        `${this.baseUrl}/auth/login`,
        { email },
        { withCredentials: true },
      )
      .pipe(
        tap((res) => {
          if (res?.user) {
            this.currentUser.set(res.user);
            if (res.csrf_token) {
              this.api.csrfToken = res.csrf_token;
            }
            this.loadUsers();
          }
        }),
        map((res) => res?.user ?? null),
        catchError(() => of(null)),
      );
  }

  /** Logout and clear session. */
  logout(): void {
    // Clear state immediately so auth guard redirects to login
    this.currentUser.set(null);
    this.api.csrfToken = null;
    this.http
      .post(`${this.baseUrl}/auth/logout`, {}, { withCredentials: true })
      .pipe(catchError(() => of(null)))
      .subscribe();
  }

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
