import { Injectable, signal, OnDestroy, PLATFORM_ID, inject } from '@angular/core';
import { isPlatformBrowser } from '@angular/common';

/**
 * Reactive viewport detection service.
 *
 * Uses `window.matchMedia` to detect mobile breakpoint (768px)
 * and exposes a signal for conditional rendering.
 * SSR-safe: defaults to desktop (false) when running on the server.
 */
@Injectable({ providedIn: 'root' })
export class ViewportService implements OnDestroy {
  readonly isMobile = signal(false);

  private mql: MediaQueryList | null = null;
  private listener: ((e: MediaQueryListEvent) => void) | null = null;

  constructor() {
    const platformId = inject(PLATFORM_ID);
    if (isPlatformBrowser(platformId)) {
      this.mql = window.matchMedia('(max-width: 768px)');
      this.isMobile.set(this.mql.matches);
      this.listener = (e) => this.isMobile.set(e.matches);
      this.mql.addEventListener('change', this.listener);
    }
  }

  ngOnDestroy(): void {
    if (this.mql && this.listener) {
      this.mql.removeEventListener('change', this.listener);
    }
  }
}
