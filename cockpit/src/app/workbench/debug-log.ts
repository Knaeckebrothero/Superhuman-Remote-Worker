import { isDevMode } from '@angular/core';

/**
 * Workbench-only trace logging. No-ops in production builds so the debug
 * surface keeps its instrumentation without shipping console noise.
 */
export function debugLog(...args: unknown[]): void {
  if (isDevMode()) {
    console.log(...args);
  }
}
