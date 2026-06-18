import {Injectable, computed, effect, inject, signal, untracked} from '@angular/core';
import {UserService} from './user.service';
import {SettingsService} from './settings.service';

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
 * **Hybrid persistence.** The choice is stored server-side as the
 * `admin_view_mode` user preference (the source of truth — it follows the
 * account across browsers, devices, and cache clears) AND mirrored to a
 * per-user `localStorage` key (`srw.viewMode.<userId>`). The mirror exists
 * because `viewAsInterceptor` needs the value *synchronously* on the very
 * first `/api` request, which fires during bootstrap before
 * `settings.loadPreferences()` resolves. Once the server value loads we
 * reconcile to it and refresh the mirror.
 */
export type ViewMode = 'me' | 'all';

const STORAGE_KEY_PREFIX = 'srw.viewMode';
const DEFAULT_MODE: ViewMode = 'all';

@Injectable({providedIn: 'root'})
export class ViewModeService {
  private readonly userService = inject(UserService);
  private readonly settings = inject(SettingsService);

  /** Current toggle state. Hydrated from localStorage, reconciled to server. */
  readonly viewMode = signal<ViewMode>(DEFAULT_MODE);

  /**
   * What the toggle effectively means for the current user.
   *
   * Non-admins are never shadowed (the header is a no-op for them on
   * the backend), so their effective mode is always `'me'`. Admins see
   * the actual toggle value. Note: admin-only UI must still gate on
   * `is_admin` separately — this returns `'me'` for non-admins too.
   */
  readonly effectiveMode = computed<ViewMode>(() => {
    return this.userService.currentUser()?.is_admin ? this.viewMode() : 'me';
  });

  constructor() {
    // (1) Synchronous bridge. Rehydrate from localStorage whenever the active
    // user changes (initial bootstrap; admin demotes self; account switch in
    // the same profile). This is what gives `viewAsInterceptor` a value on the
    // first /api request, which fires before server preferences finish loading.
    effect(() => {
      const userId = this.userService.currentUserId();
      this.viewMode.set(this.loadForUser(userId));
    });

    // (2) Server is the source of truth. Once `/api/settings/preferences` loads
    // (or changes after a save / a login on another device), adopt the
    // persisted value and refresh the localStorage mirror so the next cold
    // start matches. `untracked` keeps this effect depending only on
    // `preferences()`, not on `viewMode()` — otherwise `setMode`'s signal write
    // would re-trigger it. A missing/invalid server value leaves the
    // localStorage-derived choice untouched (preserves pre-existing local prefs).
    effect(() => {
      const serverMode = this.settings.preferences()?.admin_view_mode;
      if (serverMode !== 'me' && serverMode !== 'all') return;
      untracked(() => {
        if (this.viewMode() === serverMode) return;
        this.viewMode.set(serverMode);
        this.writeMirror(serverMode);
      });
    });
  }

  setMode(mode: ViewMode): void {
    this.viewMode.set(mode);
    this.writeMirror(mode);
    // Persist server-side so the choice follows the account. localStorage
    // already holds it, so a failed PATCH is non-fatal for this session.
    this.settings.updatePreferences({admin_view_mode: mode}).subscribe({
      error: () => {
        /* non-fatal — the localStorage mirror preserves the choice */
      },
    });
  }

  private writeMirror(mode: ViewMode): void {
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
