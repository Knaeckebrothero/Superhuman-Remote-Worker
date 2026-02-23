import { Injectable, signal } from '@angular/core';

@Injectable({ providedIn: 'root' })
export class SidebarService {
  readonly collapsed = signal(false);

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
