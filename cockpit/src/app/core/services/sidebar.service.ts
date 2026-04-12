import { Injectable, PLATFORM_ID, inject, signal } from '@angular/core';
import { isPlatformBrowser } from '@angular/common';

@Injectable({ providedIn: 'root' })
export class SidebarService {
  readonly collapsed = signal(
    isPlatformBrowser(inject(PLATFORM_ID))
      ? window.matchMedia('(max-width: 768px)').matches
      : false,
  );

  toggle(): void {
    this.collapsed.update((v) => !v);
  }

  collapse(): void {
    this.collapsed.set(true);
  }

  expand(): void {
    this.collapsed.set(false);
  }
}
