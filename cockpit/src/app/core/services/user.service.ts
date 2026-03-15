import { Injectable, signal, computed, inject } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Observable, catchError, map, of, tap, throwError } from 'rxjs';
import { User } from '../models/api.model';
import { ApiService } from './api.service';
import { environment } from '../environment';

/**
 * Manages user identity via session-based authentication.
 *
 * Login: POST /api/auth/login with email + password.
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

  // ===========================================================================
  // Session Management
  // ===========================================================================

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

  /** Login with email + password.
   *  Errors are propagated so callers can show specific messages (401, 403). */
  login(email: string, password: string): Observable<User | null> {
    const body = { email, password };

    return this.http
      .post<{ user: User; csrf_token?: string; message: string }>(
        `${this.baseUrl}/auth/login`,
        body,
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
        catchError((err: HttpErrorResponse) => throwError(() => err)),
      );
  }

  /** Logout and clear session. */
  logout(): void {
    this.currentUser.set(null);
    this.api.csrfToken = null;
    this.http
      .post(`${this.baseUrl}/auth/logout`, {}, { withCredentials: true })
      .pipe(catchError(() => of(null)))
      .subscribe();
  }

  // ===========================================================================
  // Registration & Verification
  // ===========================================================================

  /** Register a new account. Returns the API response or throws on error. */
  register(email: string, password: string, displayName: string): Observable<{ message: string }> {
    return this.http
      .post<{ message: string }>(`${this.baseUrl}/auth/register`, {
        email,
        password,
        display_name: displayName,
      })
      .pipe(catchError((err: HttpErrorResponse) => throwError(() => err)));
  }

  /** Verify email with a 6-digit code. */
  verifyEmail(email: string, code: string): Observable<{ message: string }> {
    return this.http
      .post<{ message: string }>(`${this.baseUrl}/auth/verify`, { email, code })
      .pipe(catchError((err: HttpErrorResponse) => throwError(() => err)));
  }

  /** Resend verification code. */
  resendVerification(email: string): Observable<{ message: string }> {
    return this.http
      .post<{ message: string }>(`${this.baseUrl}/auth/resend-verification`, { email })
      .pipe(catchError((err: HttpErrorResponse) => throwError(() => err)));
  }

  // ===========================================================================
  // Password Reset
  // ===========================================================================

  /** Request a password reset code. */
  forgotPassword(email: string): Observable<{ message: string }> {
    return this.http
      .post<{ message: string }>(`${this.baseUrl}/auth/forgot-password`, { email })
      .pipe(catchError((err: HttpErrorResponse) => throwError(() => err)));
  }

  /** Reset password with a verification code. */
  resetPassword(email: string, code: string, newPassword: string): Observable<{ message: string }> {
    return this.http
      .post<{ message: string }>(`${this.baseUrl}/auth/reset-password`, {
        email,
        code,
        new_password: newPassword,
      })
      .pipe(catchError((err: HttpErrorResponse) => throwError(() => err)));
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
