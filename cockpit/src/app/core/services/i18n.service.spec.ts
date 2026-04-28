import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {Subject, of} from 'rxjs';
import {TranslocoService} from '@jsverse/transloco';
import {SettingsService} from './settings.service';
import {I18nService} from './i18n.service';

function buildMocks(opts: { preferences?: Record<string, unknown> } = {}) {
  const langChanges$ = new Subject<string>();
  let currentLang = 'en';
  const transloco = {
    setActiveLang: vi.fn((lang: string) => {
      currentLang = lang;
      langChanges$.next(lang);
    }),
    getActiveLang: vi.fn(() => currentLang),
    langChanges$,
  };

  // Real signal so that the effect() inside I18nService tracks changes.
  const preferences = signal<Record<string, unknown>>(opts.preferences ?? {});
  const settings = {
    preferences,
    updatePreferences: vi.fn().mockReturnValue(of({ status: 'ok' })),
  };

  return { transloco, settings };
}

function configure(mocks: ReturnType<typeof buildMocks>) {
  TestBed.configureTestingModule({
    providers: [
      I18nService,
      { provide: TranslocoService, useValue: mocks.transloco },
      { provide: SettingsService, useValue: mocks.settings },
    ],
  });
}

function setNavigatorLanguages(languages: string[]) {
  Object.defineProperty(globalThis.navigator, 'language', {
    value: languages[0],
    configurable: true,
  });
  Object.defineProperty(globalThis.navigator, 'languages', {
    value: languages,
    configurable: true,
  });
}

describe('I18nService', () => {
  const origLang = (globalThis.navigator as any).language;
  const origLangs = (globalThis.navigator as any).languages;

  beforeEach(() => {
    TestBed.resetTestingModule();
  });

  afterEach(() => {
    Object.defineProperty(globalThis.navigator, 'language', { value: origLang, configurable: true });
    Object.defineProperty(globalThis.navigator, 'languages', { value: origLangs, configurable: true });
    vi.clearAllMocks();
  });

  describe('applyInitialLanguage', () => {
    it('resolves de-DE from browser language', () => {
      setNavigatorLanguages(['de-DE', 'de', 'en']);
      const mocks = buildMocks();
      configure(mocks);
      const service = TestBed.inject(I18nService);

      service.applyInitialLanguage();

      expect(mocks.transloco.setActiveLang).toHaveBeenCalledWith('de-DE');
      expect(service.activeLang()).toBe('de-DE');
    });

    it('normalizes de-AT to de-DE', () => {
      setNavigatorLanguages(['de-AT']);
      const mocks = buildMocks();
      configure(mocks);
      const service = TestBed.inject(I18nService);

      service.applyInitialLanguage();
      expect(mocks.transloco.setActiveLang).toHaveBeenCalledWith('de-DE');
    });

    it('normalizes en-GB to en', () => {
      setNavigatorLanguages(['en-GB']);
      const mocks = buildMocks();
      configure(mocks);
      const service = TestBed.inject(I18nService);

      service.applyInitialLanguage();
      expect(mocks.transloco.setActiveLang).toHaveBeenCalledWith('en');
    });

    it('falls back to en when browser lang is unsupported', () => {
      setNavigatorLanguages(['fr-FR', 'ja']);
      const mocks = buildMocks();
      configure(mocks);
      const service = TestBed.inject(I18nService);

      service.applyInitialLanguage();
      expect(mocks.transloco.setActiveLang).toHaveBeenCalledWith('en');
    });

    it('picks first supported language when primary is unsupported', () => {
      setNavigatorLanguages(['fr-FR', 'de-DE', 'en']);
      const mocks = buildMocks();
      configure(mocks);
      const service = TestBed.inject(I18nService);

      service.applyInitialLanguage();
      expect(mocks.transloco.setActiveLang).toHaveBeenCalledWith('de-DE');
    });
  });

  describe('setLanguage', () => {
    it('updates transloco and persists via settings', () => {
      const mocks = buildMocks();
      configure(mocks);
      const service = TestBed.inject(I18nService);

      service.setLanguage('de-DE').subscribe();

      expect(mocks.transloco.setActiveLang).toHaveBeenCalledWith('de-DE');
      expect(mocks.settings.updatePreferences).toHaveBeenCalledWith({ language: 'de-DE' });
      expect(service.activeLang()).toBe('de-DE');
    });
  });

  describe('langChanges$ subscription', () => {
    it('mirrors transloco active lang into the activeLang signal', () => {
      const mocks = buildMocks();
      configure(mocks);
      const service = TestBed.inject(I18nService);

      mocks.transloco.setActiveLang('de-DE');
      expect(service.activeLang()).toBe('de-DE');

      mocks.transloco.setActiveLang('en');
      expect(service.activeLang()).toBe('en');
    });
  });

  describe('preference → transloco propagation', () => {
    it('applies a pre-existing de-DE preference via the effect', () => {
      const mocks = buildMocks({ preferences: { language: 'de-DE' } });
      configure(mocks);
      const service = TestBed.inject(I18nService);

      // Trigger effect flush.
      TestBed.tick();

      expect(mocks.transloco.setActiveLang).toHaveBeenCalledWith('de-DE');
      expect(service.activeLang()).toBe('de-DE');
    });

    it('switches lang when preference changes from en to de-DE', () => {
      const mocks = buildMocks({ preferences: { language: 'en' } });
      configure(mocks);
      const service = TestBed.inject(I18nService);
      TestBed.tick();

      mocks.transloco.setActiveLang.mockClear();
      mocks.settings.preferences.set({ language: 'de-DE' });
      TestBed.tick();

      expect(mocks.transloco.setActiveLang).toHaveBeenCalledWith('de-DE');
    });
  });
});
