import {inject, Injectable, PLATFORM_ID, signal} from '@angular/core';
import {isPlatformBrowser} from '@angular/common';

const REASONING_EXPANDED_KEY = 'cockpit:chat:reasoningExpanded';
const TOOL_CALLS_EXPANDED_KEY = 'cockpit:chat:toolCallsExpanded';
const READING_WIDTH_KEY = 'cockpit:chat:readingWidth';
const TEXT_SIZE_KEY = 'cockpit:chat:textSize';

/** Reading-column width preset for the chat transcript. */
export type ReadingWidth = 'comfortable' | 'wide' | 'full';
export const READING_WIDTHS: readonly ReadingWidth[] = ['comfortable', 'wide', 'full'];

/** Prose text-size preset for the chat transcript. */
export type ChatTextSize = 'small' | 'medium' | 'large';
export const CHAT_TEXT_SIZES: readonly ChatTextSize[] = ['small', 'medium', 'large'];

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

  /**
   * Reading-column width for the transcript. Defaults to `comfortable` (the
   * ~700px cap, ~94 CPL). `wide` loosens it; `full` removes the cap entirely
   * (the pre-readability-cap full-bleed look). Drives `--chat-content-width`,
   * which both the message column and the composer read.
   */
  readonly readingWidth = signal<ReadingWidth>(
    this.readEnum(READING_WIDTH_KEY, READING_WIDTHS, 'comfortable'),
  );

  /** Set the reading-width preset and persist it for this device. */
  setReadingWidth(width: ReadingWidth): void {
    this.readingWidth.set(width);
    this.writeString(READING_WIDTH_KEY, width);
  }

  /**
   * Prose text size for the transcript. Defaults to `medium` (15px). Only the
   * message body scales; code/tables keep their own fixed sizes. Drives
   * `--chat-body-font-size`.
   */
  readonly textSize = signal<ChatTextSize>(
    this.readEnum(TEXT_SIZE_KEY, CHAT_TEXT_SIZES, 'medium'),
  );

  /** Set the text-size preset and persist it for this device. */
  setTextSize(size: ChatTextSize): void {
    this.textSize.set(size);
    this.writeString(TEXT_SIZE_KEY, size);
  }

  /** Read a stored value, returning the fallback unless it's a known member. */
  private readEnum<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
    if (!this.isBrowser) return fallback;
    try {
      const raw = window.localStorage.getItem(key);
      return (allowed as readonly string[]).includes(raw ?? '') ? (raw as T) : fallback;
    } catch {
      return fallback;
    }
  }

  private writeString(key: string, value: string): void {
    if (!this.isBrowser) return;
    try {
      window.localStorage.setItem(key, value);
    } catch {
      // localStorage blocked (private mode / quota) — applies for this session only.
    }
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
