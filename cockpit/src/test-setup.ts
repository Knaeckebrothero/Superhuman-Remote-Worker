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

// Mock navigator.storage for getStorageEstimate
Object.defineProperty(globalThis.navigator, 'storage', {
  value: {
    estimate: async () => ({ usage: 1024, quota: 1024 * 1024 * 50 }),
  },
  writable: true,
  configurable: true,
});
