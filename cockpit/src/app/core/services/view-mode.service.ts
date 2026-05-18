import {Injectable, computed, effect, inject, signal} from '@angular/core';
import {UserService} from './user.service';

/**
 * Persists the admin "View as" toggle.
 *
 * Admins default to fleet-wide visibility (`'all'`) but can flip to
 * regular-user visibility (`'me'`) to dogfood the non-admin UX. The
 * value rides on every orchestrator request as the `X-Admin-View-As`
 * header (see `view-as.interceptor.ts`); the orchestrator's
 * `require_approved_user` flips `is_admin=False` on the resolved user
 * dict when the header is `user`, while preserving `real_is_admin` for
 * admin-only gates. Design: `docs/features/admin_view_as_user.md`.
 *
 * Persistence is per-user (`srw.viewMode.<userId>`) so shared browser
 * profiles don't bleed one admin's preference into another. The
 * default is `'all'` to keep silent rollout — existing admins see no
 * behavior change until they click the toggle (PR 3).
 */
export type ViewMode = 'me' | 'all';

const STORAGE_KEY_PREFIX = 'srw.viewMode';
const DEFAULT_MODE: ViewMode = 'all';

@Injectable({providedIn: 'root'})
export class ViewModeService {
  private readonly userService = inject(UserService);

  /** Persisted toggle state. Reads/writes localStorage keyed by user. */
  readonly viewMode = signal<ViewMode>(DEFAULT_MODE);

  /**
   * What the toggle effectively means for the current user.
   *
   * Non-admins are never shadowed (the header is a no-op for them on
   * the backend), so their effective mode is always `'me'`. Admins see
   * the actual toggle value. UI badges/pills should read this — it
   * stays correct after a logout/login race without manual gating.
   */
  readonly effectiveMode = computed<ViewMode>(() => {
    return this.userService.currentUser()?.is_admin ? this.viewMode() : 'me';
  });

  constructor() {
    // Rehydrate the toggle from localStorage whenever the active user
    // changes (initial bootstrap; admin demotes self; account switch
    // in the same browser profile). Without this the signal would stay
    // at the constructor-time default through the entire session.
    effect(() => {
      const userId = this.userService.currentUserId();
      this.viewMode.set(this.loadForUser(userId));
    });
  }

  setMode(mode: ViewMode): void {
    this.viewMode.set(mode);
    const userId = this.userService.currentUserId();
    if (!userId) return;
    try {
      localStorage.setItem(this.keyFor(userId), mode);
    } catch {
      // Private-browsing / storage-disabled — keep the in-memory signal.
    }
  }

  private loadForUser(userId: string | null): ViewMode {
    if (!userId) return DEFAULT_MODE;
    try {
      const raw = localStorage.getItem(this.keyFor(userId));
      return raw === 'me' || raw === 'all' ? raw : DEFAULT_MODE;
    } catch {
      return DEFAULT_MODE;
    }
  }

  private keyFor(userId: string): string {
    return `${STORAGE_KEY_PREFIX}.${userId}`;
  }
}
