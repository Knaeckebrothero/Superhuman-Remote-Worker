import {effect, inject, Injectable, signal} from '@angular/core';
import {Observable} from 'rxjs';
import {TranslocoService} from '@jsverse/transloco';
import {SettingsService} from './settings.service';

export type SupportedLang = 'en' | 'de-DE';
export const SUPPORTED_LANGS: readonly SupportedLang[] = ['en', 'de-DE'] as const;
export const DEFAULT_LANG: SupportedLang = 'en';

/**
 * Owns language resolution and propagation.
 *
 * Resolution order at bootstrap: user preference → browser locale → default.
 * Runtime changes go through `setLanguage`, which updates transloco
 * synchronously (UI flips) and persists the choice via SettingsService.
 */
@Injectable({ providedIn: 'root' })
export class I18nService {
  private readonly transloco = inject(TranslocoService);
  private readonly settings = inject(SettingsService);

  /** Mirrors TranslocoService.getActiveLang() — safe to read in templates. */
  readonly activeLang = signal<SupportedLang>(DEFAULT_LANG);

  constructor() {
    this.transloco.langChanges$.subscribe((lang) => {
      this.activeLang.set(this.toSupported(lang) ?? DEFAULT_LANG);
    });

    // When preferences load (or are updated), switch to the stored language
    // if it differs from the active one. Runs in an injection context so
    // the effect cleans up with the service.
    effect(() => {
      const pref = this.settings.preferences()?.language;
      const target = this.toSupported(pref ?? null);
      if (target && target !== this.transloco.getActiveLang()) {
        this.transloco.setActiveLang(target);
      }
    });
  }

  /**
   * Synchronous initial resolution from the browser's preferred languages.
   * Called from APP_INITIALIZER so first paint is in the right locale.
   * Preferences may arrive later via the effect above.
   */
  applyInitialLanguage(): void {
    const resolved = this.fromBrowser() ?? DEFAULT_LANG;
    this.transloco.setActiveLang(resolved);
    this.activeLang.set(resolved);
  }

  /** User-triggered change: apply immediately, persist to backend. */
  setLanguage(lang: SupportedLang): Observable<{ status: string }> {
    this.transloco.setActiveLang(lang);
    this.activeLang.set(lang);
    return this.settings.updatePreferences({ language: lang });
  }

  private fromBrowser(): SupportedLang | null {
    if (typeof navigator === 'undefined') return null;
    const candidates = [navigator.language, ...(navigator.languages ?? [])];
    for (const cand of candidates) {
      const match = this.toSupported(cand);
      if (match) return match;
    }
    return null;
  }

  private toSupported(lang: string | null | undefined): SupportedLang | null {
    if (!lang) return null;
    if (lang === 'de-DE' || lang === 'de' || lang.startsWith('de-')) return 'de-DE';
    if (lang === 'en' || lang.startsWith('en-') || lang.startsWith('en_')) return 'en';
    return null;
  }
}
