// Setup fake-indexeddb for testing
import 'fake-indexeddb/auto';

// Mock navigator.storage for getStorageEstimate
Object.defineProperty(globalThis.navigator, 'storage', {
  value: {
    estimate: async () => ({ usage: 1024, quota: 1024 * 1024 * 50 }),
  },
  writable: true,
  configurable: true,
});
