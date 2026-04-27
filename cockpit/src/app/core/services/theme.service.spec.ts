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
    it('defaults to dark when no stored preference', () => {
      const service = makeService();
      expect(service.preference()).toBe('dark');
      expect(service.resolved()).toBe('dark');
      expect(document.body.classList.contains('theme-dark')).toBe(true);
      expect(document.body.classList.contains('theme-light')).toBe(false);
    });

    it('reads stored preference from localStorage', () => {
      window.localStorage.setItem('cockpit:theme', 'light');
      const service = makeService();
      expect(service.preference()).toBe('light');
      expect(service.resolved()).toBe('light');
      expect(document.body.classList.contains('theme-light')).toBe(true);
    });

    it('ignores invalid stored values and falls back to dark', () => {
      window.localStorage.setItem('cockpit:theme', 'gibberish');
      const service = makeService();
      expect(service.preference()).toBe('dark');
    });
  });

  describe('setPreference', () => {
    it('applies dark class on body', () => {
      const service = makeService();
      service.setPreference('dark');
      TestBed.tick();
      expect(document.body.classList.contains('theme-dark')).toBe(true);
      expect(document.body.classList.contains('theme-light')).toBe(false);
    });

    it('applies light class on body', () => {
      const service = makeService();
      service.setPreference('light');
      TestBed.tick();
      expect(document.body.classList.contains('theme-light')).toBe(true);
      expect(document.body.classList.contains('theme-dark')).toBe(false);
    });

    it('persists choice to localStorage', () => {
      const service = makeService();
      service.setPreference('light');
      expect(window.localStorage.getItem('cockpit:theme')).toBe('light');

      service.setPreference('system');
      expect(window.localStorage.getItem('cockpit:theme')).toBe('system');
    });
  });

  describe('system preference', () => {
    it('resolves to dark when system prefers dark', () => {
      mql = makeMockMql(true);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).matchMedia = vi.fn().mockReturnValue(mql);
      const service = makeService();
      service.setPreference('system');
      TestBed.tick();
      expect(service.resolved()).toBe('dark');
      expect(document.body.classList.contains('theme-dark')).toBe(true);
    });

    it('resolves to light when system prefers light', () => {
      mql = makeMockMql(false);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).matchMedia = vi.fn().mockReturnValue(mql);
      const service = makeService();
      service.setPreference('system');
      TestBed.tick();
      expect(service.resolved()).toBe('light');
      expect(document.body.classList.contains('theme-light')).toBe(true);
    });

    it('flips body class when system preference changes (preference=system)', () => {
      mql = makeMockMql(true);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).matchMedia = vi.fn().mockReturnValue(mql);
      const service = makeService();
      service.setPreference('system');
      TestBed.tick();
      expect(service.resolved()).toBe('dark');

      mql.fire(false);
      TestBed.tick();
      expect(service.resolved()).toBe('light');
      expect(document.body.classList.contains('theme-light')).toBe(true);
      expect(document.body.classList.contains('theme-dark')).toBe(false);
    });

    it('does not flip when preference is explicit (system OS change ignored)', () => {
      mql = makeMockMql(true);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).matchMedia = vi.fn().mockReturnValue(mql);
      const service = makeService();
      service.setPreference('light');
      TestBed.tick();
      expect(service.resolved()).toBe('light');

      mql.fire(true);
      TestBed.tick();
      expect(service.resolved()).toBe('light');
    });
  });
});
