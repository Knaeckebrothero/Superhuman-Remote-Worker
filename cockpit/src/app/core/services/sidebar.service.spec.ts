import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {TestBed} from '@angular/core/testing';
import {
  SIDEBAR_DEFAULT_WIDTH,
  SIDEBAR_MAX_WIDTH,
  SIDEBAR_MIN_WIDTH,
  SidebarService,
  clampSidebarWidth,
} from './sidebar.service';

const WIDTH_KEY = 'cockpit:sidebar:width';

describe('SidebarService width', () => {
  const originalMatchMedia = window.matchMedia;

  beforeEach(() => {
    TestBed.resetTestingModule();
    window.localStorage.clear();
    // The constructor reads the mobile breakpoint; pin it to desktop so these
    // tests don't depend on jsdom's matchMedia stub.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).matchMedia = vi.fn().mockReturnValue({matches: false});
  });

  afterEach(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).matchMedia = originalMatchMedia;
    window.localStorage.clear();
  });

  function makeService(): SidebarService {
    TestBed.configureTestingModule({providers: [SidebarService]});
    return TestBed.inject(SidebarService);
  }

  describe('clampSidebarWidth', () => {
    it('keeps a width inside the range untouched', () => {
      expect(clampSidebarWidth(320)).toBe(320);
    });

    it('clamps to the bounds rather than rejecting', () => {
      expect(clampSidebarWidth(10)).toBe(SIDEBAR_MIN_WIDTH);
      expect(clampSidebarWidth(9000)).toBe(SIDEBAR_MAX_WIDTH);
    });

    it('rounds — a fractional CSS length is pointless and NaN-adjacent', () => {
      expect(clampSidebarWidth(287.6)).toBe(288);
    });

    // NaN would otherwise reach `${width}px`, which is an invalid CSS length:
    // the declaration drops and the rail falls back to whatever the previous
    // rule said — in the collapsed case, zero.
    // Infinity gets the default rather than the max: a non-finite width is a
    // bug signal (bad arithmetic, a corrupt stored value), not a request for
    // the widest rail available.
    it('falls back to the default for non-finite input', () => {
      expect(clampSidebarWidth(NaN)).toBe(SIDEBAR_DEFAULT_WIDTH);
      expect(clampSidebarWidth(Infinity)).toBe(SIDEBAR_DEFAULT_WIDTH);
      expect(clampSidebarWidth(-Infinity)).toBe(SIDEBAR_DEFAULT_WIDTH);
    });
  });

  describe('initial width', () => {
    it('defaults when nothing is stored', () => {
      expect(makeService().width()).toBe(SIDEBAR_DEFAULT_WIDTH);
    });

    it('restores a stored width', () => {
      window.localStorage.setItem(WIDTH_KEY, '340');
      expect(makeService().width()).toBe(340);
    });

    // The bounds can move between releases; a width stored under the old range
    // must not survive as an out-of-range rail.
    it('clamps a stored width that is out of range', () => {
      window.localStorage.setItem(WIDTH_KEY, '2000');
      expect(makeService().width()).toBe(SIDEBAR_MAX_WIDTH);
    });

    it('falls back to the default for a malformed stored value', () => {
      window.localStorage.setItem(WIDTH_KEY, 'wide-ish');
      expect(makeService().width()).toBe(SIDEBAR_DEFAULT_WIDTH);
    });

    it('exposes the width as a CSS length', () => {
      expect(makeService().widthPx()).toBe(`${SIDEBAR_DEFAULT_WIDTH}px`);
    });
  });

  describe('setWidth / commitWidth', () => {
    // A drag calls setWidth at pointer-event rate. localStorage is synchronous,
    // so the write is deliberately deferred to the end of the gesture.
    it('setWidth updates the signal without writing to storage', () => {
      const service = makeService();
      service.setWidth(300);
      expect(service.width()).toBe(300);
      expect(window.localStorage.getItem(WIDTH_KEY)).toBeNull();
    });

    it('commitWidth persists what setWidth last applied', () => {
      const service = makeService();
      service.setWidth(300);
      service.commitWidth();
      expect(window.localStorage.getItem(WIDTH_KEY)).toBe('300');
    });

    it('setWidth clamps, so a commit can never store an out-of-range width', () => {
      const service = makeService();
      service.setWidth(-40);
      service.commitWidth();
      expect(service.width()).toBe(SIDEBAR_MIN_WIDTH);
      expect(window.localStorage.getItem(WIDTH_KEY)).toBe(String(SIDEBAR_MIN_WIDTH));
    });
  });

  describe('resetWidth', () => {
    it('restores the default and persists it immediately', () => {
      const service = makeService();
      service.setWidth(460);
      service.resetWidth();
      expect(service.width()).toBe(SIDEBAR_DEFAULT_WIDTH);
      expect(window.localStorage.getItem(WIDTH_KEY)).toBe(String(SIDEBAR_DEFAULT_WIDTH));
    });
  });

  describe('collapse state', () => {
    it('is unaffected by a resize — they are independent controls', () => {
      const service = makeService();
      service.setWidth(400);
      service.collapse();
      expect(service.collapsed()).toBe(true);
      // The width survives the collapse, so expanding restores the rail the
      // user sized rather than the default.
      expect(service.width()).toBe(400);
      service.expand();
      expect(service.collapsed()).toBe(false);
      expect(service.width()).toBe(400);
    });
  });
});
