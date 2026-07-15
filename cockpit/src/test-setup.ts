// Setup fake-indexeddb for testing
import 'fake-indexeddb/auto';

// Import Angular compiler for JIT compilation in tests
import '@angular/compiler';

import {TestBed} from '@angular/core/testing';
import {BrowserTestingModule, platformBrowserTesting} from '@angular/platform-browser/testing';

// Initialize Angular's TestBed once per test session so specs that opt into
// it (e.g. services using `effect()`) can call TestBed.configureTestingModule.
// Specs that prefer `Injector.create` continue to work unchanged.
try {
  TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
} catch {
  // Already initialized (vitest may re-import the setup file).
}

// jsdom does not implement ResizeObserver (jsdom#3368) and never will for
// anything layout-dependent — it has no layout engine, so clientHeight /
// scrollHeight / getBoundingClientRect all return 0.
//
// Nothing exercises this stub today: PersistentChatComponent (the only RO user,
// for its scroll pin) is never instantiated in a spec — its logic is tested via
// exported pure functions instead. This is insurance, so that the first spec to
// mount it gets a working harness rather than a bare
// `ReferenceError: ResizeObserver is not defined`.
//
// It is a WIRING stub, not a polyfill. Do NOT swap in resize-observer-polyfill
// or @juggle/resize-observer: both bail on zero-size elements, so under jsdom
// they silently never fire and buy zero signal. `trigger()` lets a spec fire the
// callback and assert observe/disconnect wiring; real geometry needs a real
// browser. Note the real API has no takeRecords() — don't cargo-cult one in from
// IntersectionObserver.
class ResizeObserverStub implements ResizeObserver {
  static instances: ResizeObserverStub[] = [];
  readonly observed: Element[] = [];
  disconnected = false;
  constructor(readonly callback: ResizeObserverCallback) {
    ResizeObserverStub.instances.push(this);
  }
  observe(target: Element): void {
    this.observed.push(target);
  }
  unobserve(target: Element): void {
    const i = this.observed.indexOf(target);
    if (i >= 0) this.observed.splice(i, 1);
  }
  disconnect(): void {
    this.disconnected = true;
    this.observed.length = 0;
  }
  /** Test hook: fire the callback as the browser would. */
  trigger(): void {
    this.callback([], this);
  }
}
globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;
(globalThis as Record<string, unknown>)['__ResizeObserverStub'] = ResizeObserverStub;

// Mock navigator.storage for getStorageEstimate
Object.defineProperty(globalThis.navigator, 'storage', {
  value: {
    estimate: async () => ({ usage: 1024, quota: 1024 * 1024 * 50 }),
  },
  writable: true,
  configurable: true,
});
