import { Injectable, PLATFORM_ID, computed, inject, signal } from '@angular/core';
import { isPlatformBrowser } from '@angular/common';

/**
 * Rail width bounds, in px.
 *
 * The default matches what the big assistant UIs converged on for a
 * conversation rail (ChatGPT ships a 260px sidebar); 200 — the width this rail
 * shipped at until now — becomes the *narrow* end of the range rather than the
 * only option, and 480 is roughly where a session title stops being the thing
 * the extra pixels buy you. The range is deliberately not viewport-relative:
 * below 768px the rail is an overlay drawer with its own fixed sizing (see the
 * media query in sidebar.component.ts), so a 480px rail can never crowd out a
 * narrow *desktop* window that isn't already a drawer.
 */
export const SIDEBAR_MIN_WIDTH = 200;
export const SIDEBAR_MAX_WIDTH = 480;
export const SIDEBAR_DEFAULT_WIDTH = 260;

/** Keyboard nudge per arrow press on the resize handle. */
export const SIDEBAR_WIDTH_STEP = 16;

const WIDTH_KEY = 'cockpit:sidebar:width';

/**
 * Clamp an arbitrary number to the rail's range. Non-finite input (a malformed
 * localStorage value, a NaN from arithmetic on a missing measurement) falls
 * back to the default rather than propagating NaN into a CSS length, which
 * would drop the declaration and collapse the rail to zero.
 */
export function clampSidebarWidth(px: number): number {
  if (!Number.isFinite(px)) return SIDEBAR_DEFAULT_WIDTH;
  return Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, Math.round(px)));
}

@Injectable({ providedIn: 'root' })
export class SidebarService {
  private readonly isBrowser = isPlatformBrowser(inject(PLATFORM_ID));

  readonly collapsed = signal(
    this.isBrowser ? window.matchMedia('(max-width: 768px)').matches : false,
  );

  /**
   * Desktop rail width in px. Device-local (localStorage), for the same reason
   * as the chat display preferences (see ChatPreferencesService): it's a
   * per-device *viewing* choice — a 27" monitor and a laptop want different
   * rails — and it has to apply at first paint, before any API response.
   */
  readonly width = signal(this.readWidth());

  /** `--sidebar-width`, bound on the rail host. */
  readonly widthPx = computed(() => `${this.width()}px`);

  /**
   * True for the duration of a drag. The rail animates its width (0.2s) so the
   * collapse/expand toggle reads as a wipe rather than a jump — during a drag
   * that same transition makes the edge lag the pointer, so the rail suppresses
   * it while this is set. Lives here, not in the resizer, because the two are
   * sibling components in the shell.
   */
  readonly resizing = signal(false);

  toggle(): void {
    this.collapsed.update((v) => !v);
  }

  collapse(): void {
    this.collapsed.set(true);
  }

  expand(): void {
    this.collapsed.set(false);
  }

  /**
   * Live width update. Deliberately does *not* persist: a drag calls this at
   * pointer-event rate, and localStorage is synchronous. {@link commitWidth}
   * writes once the gesture ends.
   */
  setWidth(px: number): void {
    this.width.set(clampSidebarWidth(px));
  }

  /** Persist the current width — called once at the end of a resize gesture. */
  commitWidth(): void {
    this.writeWidth(this.width());
  }

  /** Back to the shipped default (the double-click-the-handle affordance). */
  resetWidth(): void {
    this.width.set(SIDEBAR_DEFAULT_WIDTH);
    this.writeWidth(SIDEBAR_DEFAULT_WIDTH);
  }

  private readWidth(): number {
    if (!this.isBrowser) return SIDEBAR_DEFAULT_WIDTH;
    try {
      const raw = window.localStorage.getItem(WIDTH_KEY);
      if (raw === null) return SIDEBAR_DEFAULT_WIDTH;
      // Clamped on read, not just on write: the bounds can move between
      // releases, and a width stored under the old range must not survive as an
      // out-of-range rail.
      return clampSidebarWidth(Number(raw));
    } catch {
      // localStorage blocked (private mode / sandbox) — use the default.
      return SIDEBAR_DEFAULT_WIDTH;
    }
  }

  private writeWidth(px: number): void {
    if (!this.isBrowser) return;
    try {
      window.localStorage.setItem(WIDTH_KEY, String(px));
    } catch {
      // Blocked or over quota — the width still applies for this session, it
      // just won't survive a reload.
    }
  }
}
