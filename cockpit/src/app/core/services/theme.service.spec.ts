import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {TestBed} from '@angular/core/testing';
import {ThemeService} from './theme.service';

interface MockMql {
  matches: boolean;
  listeners: Array<(event: {matches: boolean}) => void>;
  addEventListener: (type: 'change', cb: (event: {matches: boolean}) => void) => void;
  removeEventListener: (type: 'change', cb: (event: {matches: boolean}) => void) => void;
  fire: (matches: boolean) => void;
}

function makeMockMql(initialMatches: boolean): MockMql {
  const mql: MockMql = {
    matches: initialMatches,
    listeners: [],
    addEventListener: (_type, cb) => {
      mql.listeners.push(cb);
    },
    removeEventListener: (_type, cb) => {
      mql.listeners = mql.listeners.filter((l) => l !== cb);
    },
    fire: (matches: boolean) => {
      mql.matches = matches;
      mql.listeners.forEach((cb) => cb({matches}));
    },
  };
  return mql;
}

describe('ThemeService', () => {
  let mql: MockMql;
  const originalMatchMedia = window.matchMedia;

  beforeEach(() => {
    TestBed.resetTestingModule();
    document.body.className = '';
    window.localStorage.clear();
    mql = makeMockMql(true);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).matchMedia = vi.fn().mockReturnValue(mql);
  });

  afterEach(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).matchMedia = originalMatchMedia;
    document.body.className = '';
  });

  function makeService(): ThemeService {
    TestBed.configureTestingModule({providers: [ThemeService]});
    const service = TestBed.inject(ThemeService);
    TestBed.tick();
    return service;
  }

  describe('initial state', () => {
    it('defaults to senate when no stored preference', () => {
      const service = makeService();
      expect(service.preference()).toBe('senate');
      expect(service.resolved()).toBe('senate');
      expect(document.body.classList.contains('theme-senate')).toBe(true);
      expect(document.body.classList.contains('theme-travertine')).toBe(false);
    });

    it('reads stored preference from localStorage', () => {
      window.localStorage.setItem('cockpit:theme', 'travertine');
      const service = makeService();
      expect(service.preference()).toBe('travertine');
      expect(service.resolved()).toBe('travertine');
      expect(document.body.classList.contains('theme-travertine')).toBe(true);
    });

    it('ignores invalid stored values and falls back to senate', () => {
      window.localStorage.setItem('cockpit:theme', 'gibberish');
      const service = makeService();
      expect(service.preference()).toBe('senate');
    });

    it('reads stored Roman theme preference', () => {
      window.localStorage.setItem('cockpit:theme', 'praetorian');
      const service = makeService();
      expect(service.preference()).toBe('praetorian');
      expect(service.resolved()).toBe('praetorian');
      expect(document.body.classList.contains('theme-praetorian')).toBe(true);
    });

    it('migrates legacy "dark" preference to senate', () => {
      window.localStorage.setItem('cockpit:theme', 'dark');
      const service = makeService();
      expect(service.preference()).toBe('senate');
      expect(document.body.classList.contains('theme-senate')).toBe(true);
      // Migration should rewrite the stored value so subsequent reads are clean.
      expect(window.localStorage.getItem('cockpit:theme')).toBe('senate');
    });

    it('migrates legacy "light" preference to travertine', () => {
      window.localStorage.setItem('cockpit:theme', 'light');
      const service = makeService();
      expect(service.preference()).toBe('travertine');
      expect(document.body.classList.contains('theme-travertine')).toBe(true);
      expect(window.localStorage.getItem('cockpit:theme')).toBe('travertine');
    });
  });

  describe('setPreference', () => {
    it('applies senate class on body', () => {
      const service = makeService();
      service.setPreference('senate');
      TestBed.tick();
      expect(document.body.classList.contains('theme-senate')).toBe(true);
      expect(document.body.classList.contains('theme-travertine')).toBe(false);
    });

    it('applies travertine class on body', () => {
      const service = makeService();
      service.setPreference('travertine');
      TestBed.tick();
      expect(document.body.classList.contains('theme-travertine')).toBe(true);
      expect(document.body.classList.contains('theme-senate')).toBe(false);
    });

    it('persists choice to localStorage', () => {
      const service = makeService();
      service.setPreference('travertine');
      expect(window.localStorage.getItem('cockpit:theme')).toBe('travertine');

      service.setPreference('system');
      expect(window.localStorage.getItem('cockpit:theme')).toBe('system');
    });

    it('strips previous theme class when switching', () => {
      const service = makeService();
      service.setPreference('senate');
      TestBed.tick();
      expect(document.body.classList.contains('theme-senate')).toBe(true);

      service.setPreference('praetorian');
      TestBed.tick();
      expect(document.body.classList.contains('theme-praetorian')).toBe(true);
      expect(document.body.classList.contains('theme-senate')).toBe(false);
    });

    it('switches between travertine and praetorian', () => {
      const service = makeService();
      service.setPreference('travertine');
      TestBed.tick();
      expect(document.body.classList.contains('theme-travertine')).toBe(true);

      service.setPreference('praetorian');
      TestBed.tick();
      expect(document.body.classList.contains('theme-praetorian')).toBe(true);
      expect(document.body.classList.contains('theme-travertine')).toBe(false);
    });
  });

  describe('system preference', () => {
    it('resolves to senate when system prefers dark', () => {
      mql = makeMockMql(true);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).matchMedia = vi.fn().mockReturnValue(mql);
      const service = makeService();
      service.setPreference('system');
      TestBed.tick();
      expect(service.resolved()).toBe('senate');
      expect(document.body.classList.contains('theme-senate')).toBe(true);
    });

    it('resolves to travertine when system prefers light', () => {
      mql = makeMockMql(false);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).matchMedia = vi.fn().mockReturnValue(mql);
      const service = makeService();
      service.setPreference('system');
      TestBed.tick();
      expect(service.resolved()).toBe('travertine');
      expect(document.body.classList.contains('theme-travertine')).toBe(true);
    });

    it('flips body class when system preference changes (preference=system)', () => {
      mql = makeMockMql(true);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).matchMedia = vi.fn().mockReturnValue(mql);
      const service = makeService();
      service.setPreference('system');
      TestBed.tick();
      expect(service.resolved()).toBe('senate');

      mql.fire(false);
      TestBed.tick();
      expect(service.resolved()).toBe('travertine');
      expect(document.body.classList.contains('theme-travertine')).toBe(true);
      expect(document.body.classList.contains('theme-senate')).toBe(false);
    });

    it('does not flip when preference is explicit (system OS change ignored)', () => {
      mql = makeMockMql(true);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).matchMedia = vi.fn().mockReturnValue(mql);
      const service = makeService();
      service.setPreference('travertine');
      TestBed.tick();
      expect(service.resolved()).toBe('travertine');

      mql.fire(true);
      TestBed.tick();
      expect(service.resolved()).toBe('travertine');
    });
  });
});
