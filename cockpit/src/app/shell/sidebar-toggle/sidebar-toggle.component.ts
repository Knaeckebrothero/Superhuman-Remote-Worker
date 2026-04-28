import { Component, inject } from '@angular/core';
import { SidebarService } from '../../core/services/sidebar.service';
import { TranslocoPipe } from '@jsverse/transloco';
import { AppIconComponent } from '../../ui/icon';

@Component({
  selector: 'app-sidebar-toggle',
  standalone: true,
  imports: [TranslocoPipe, AppIconComponent],
  template: `
    @if (sidebar.collapsed()) {
      <button class="sidebar-toggle" (click)="sidebar.expand()" [title]="'nav.openSidebar' | transloco">
        <app-icon size="lg" class="toggle-icon">menu</app-icon>
      </button>
    }
  `,
  styles: [
    `
      :host {
        display: contents;
      }

      .sidebar-toggle {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 32px;
        height: 32px;
        background: transparent;
        border: 1px solid var(--border-color, #313244);
        border-radius: 6px;
        color: var(--text-muted, #6c7086);
        cursor: pointer;
        padding: 0;
        flex-shrink: 0;
        transition:
          color 0.15s ease,
          border-color 0.15s ease;
      }

      .sidebar-toggle:hover {
        color: var(--accent-color, #cba6f7);
        border-color: var(--accent-color, #cba6f7);
      }

    `,
  ],
})
export class SidebarToggleComponent {
  readonly sidebar = inject(SidebarService);
}
