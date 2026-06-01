import {inject, Injectable, PLATFORM_ID, signal} from '@angular/core';
import {isPlatformBrowser} from '@angular/common';

const REASONING_EXPANDED_KEY = 'cockpit:chat:reasoningExpanded';
const TOOL_CALLS_EXPANDED_KEY = 'cockpit:chat:toolCallsExpanded';

/**
 * Device-local display preferences for the persistent-chat view.
 *
 * Stored in localStorage rather than backend-persisted, for the same reason as
 * the appearance theme (see ThemeService): these are per-device *viewing*
 * choices — you might want agent reasoning expanded on a wide desktop but
 * folded on a phone — and they must apply at first paint, before any API
 * response. Nothing here is sent to the agent; it only governs how the client
 * renders what the agent already produced.
 */
@Injectable({providedIn: 'root'})
export class ChatPreferencesService {
  private readonly isBrowser = isPlatformBrowser(inject(PLATFORM_ID));

  /**
   * Whether agent reasoning ("thinking") blocks render expanded by default.
   * Defaults to `true` — the historical always-open behaviour. This only sets
   * the *initial* state of each reasoning card; the user can still fold/unfold
   * an individual card, and flipping this re-applies the default to all visible
   * cards live (the template binds the `<details open>` attribute to it).
   */
  readonly reasoningExpanded = signal<boolean>(this.readBool(REASONING_EXPANDED_KEY, true));

  /** Set the reasoning-expanded default and persist it for this device. */
  setReasoningExpanded(expanded: boolean): void {
    this.reasoningExpanded.set(expanded);
    this.writeBool(REASONING_EXPANDED_KEY, expanded);
  }

  /**
   * Whether a run of consecutive tool calls renders fully inline ("expanded")
   * vs. folded into a "N× tool calls" disclosure ("collapsed").
   * Defaults to `false` (folded — the historical Slice 3 grouping that keeps
   * long tool runs from flooding the transcript). When `true`, every run
   * renders as plain inline cards with no fold control, like a 2-call run.
   */
  readonly toolCallsExpanded = signal<boolean>(this.readBool(TOOL_CALLS_EXPANDED_KEY, false));

  /** Set the tool-calls-expanded default and persist it for this device. */
  setToolCallsExpanded(expanded: boolean): void {
    this.toolCallsExpanded.set(expanded);
    this.writeBool(TOOL_CALLS_EXPANDED_KEY, expanded);
  }

  private readBool(key: string, fallback: boolean): boolean {
    if (!this.isBrowser) return fallback;
    try {
      const raw = window.localStorage.getItem(key);
      if (raw === 'true') return true;
      if (raw === 'false') return false;
      return fallback; // unset or malformed → default
    } catch {
      // localStorage blocked (private mode / sandbox) — use the default.
      return fallback;
    }
  }

  private writeBool(key: string, value: boolean): void {
    if (!this.isBrowser) return;
    try {
      window.localStorage.setItem(key, String(value));
    } catch {
      // localStorage may be blocked (private mode, quota); the preference still
      // applies for this session, it just won't survive a reload.
    }
  }
}
