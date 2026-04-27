import {computed, effect, inject, Injectable, PLATFORM_ID, signal} from '@angular/core';
import {isPlatformBrowser} from '@angular/common';

export type ThemePreference = 'dark' | 'light' | 'system';
export type ResolvedTheme = 'dark' | 'light';

const STORAGE_KEY = 'cockpit:theme';
const BODY_CLASS_DARK = 'theme-dark';
const BODY_CLASS_LIGHT = 'theme-light';

/**
 * Owns appearance-theme resolution and propagation.
 *
 * Stored as a per-device preference (localStorage) rather than backend-persisted —
 * users often want different themes on different devices (laptop vs phone) and
 * the body class is needed before the API responds for first paint.
 *
 * Resolution: stored preference → 'system' (which follows prefers-color-scheme).
 * `setPreference` is the only mutation entry point; the body class swap happens
 * via an effect, so it stays in lockstep with the resolved signal.
 */
@Injectable({providedIn: 'root'})
export class ThemeService {
  private readonly isBrowser = isPlatformBrowser(inject(PLATFORM_ID));

  /** What the user picked. `system` means follow OS preference live. */
  readonly preference = signal<ThemePreference>(this.readStoredPreference());

  /** Live OS preference. Updates when the user flips dark/light at the OS level. */
  private readonly systemPrefersDark = signal<boolean>(this.readSystemPrefersDark());

  /** Effective theme that's actually applied to <body>. */
  readonly resolved = computed<ResolvedTheme>(() => {
    const pref = this.preference();
    if (pref === 'dark') return 'dark';
    if (pref === 'light') return 'light';
    return this.systemPrefersDark() ? 'dark' : 'light';
  });

  constructor() {
    if (this.isBrowser) {
      // Watch the OS-level preference; only matters when preference === 'system',
      // but keeping the listener live is cheaper than attaching/detaching.
      const mql = window.matchMedia('(prefers-color-scheme: dark)');
      const onChange = (event: MediaQueryListEvent) => this.systemPrefersDark.set(event.matches);
      // Older Safari (<14) lacks addEventListener on MediaQueryList — fall back gracefully.
      if (typeof mql.addEventListener === 'function') {
        mql.addEventListener('change', onChange);
      } else {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (mql as any).addListener?.(onChange);
      }
    }

    // Apply the body class whenever resolved theme changes. Runs at construction
    // (so first paint matches the stored preference) and on every flip.
    effect(() => {
      const theme = this.resolved();
      this.applyBodyClass(theme);
    });
  }

  /** User picks a theme (or 'system'). Persists locally; body class follows via effect. */
  setPreference(pref: ThemePreference): void {
    this.preference.set(pref);
    if (this.isBrowser) {
      try {
        window.localStorage.setItem(STORAGE_KEY, pref);
      } catch {
        // localStorage might be blocked (private mode, quota); preference still
        // takes effect for the session, just doesn't survive a reload.
      }
    }
  }

  private readStoredPreference(): ThemePreference {
    if (!this.isBrowser) return 'dark';
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (raw === 'dark' || raw === 'light' || raw === 'system') return raw;
    } catch {
      // ignore
    }
    return 'dark';
  }

  private readSystemPrefersDark(): boolean {
    if (!this.isBrowser) return true;
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  }

  private applyBodyClass(theme: ResolvedTheme): void {
    if (!this.isBrowser) return;
    const body = document.body;
    if (theme === 'dark') {
      body.classList.add(BODY_CLASS_DARK);
      body.classList.remove(BODY_CLASS_LIGHT);
    } else {
      body.classList.add(BODY_CLASS_LIGHT);
      body.classList.remove(BODY_CLASS_DARK);
    }
  }
}
