import {Component, inject} from '@angular/core';
import {Router} from '@angular/router';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppMenuComponent, AppMenuItemComponent, AppMenuTriggerDirective} from '../../ui/menu';
import {AppIconComponent} from '../../ui/icon';
import {ViewportService} from '../../core/services/viewport.service';

/**
 * The rail's "More" flyout — the split rule's agent-facing half (see
 * RailAccountMenuComponent for the "about you and your instance" half).
 *
 * Holds the destinations Task 6 displaced from the flat nav when it replaced
 * those links with the Chat/Jobs/Projects mode switcher: Connectors,
 * Contacts, Experts, Skills, Automations, and — desktop only, gated exactly
 * as the old link was — the Workbench.
 */
@Component({
  selector: 'app-rail-more-menu',
  standalone: true,
  imports: [AppMenuComponent, AppMenuItemComponent, AppMenuTriggerDirective, AppIconComponent, TranslocoPipe],
  template: `
    <button class="rail-nav" [appMenuTrigger]="moreMenu" menuPlacement="top-start">
      <app-icon size="md">more_horiz</app-icon> {{ 'nav.more' | transloco }}
    </button>
    <app-menu #moreMenu>
      <app-menu-item (activated)="go('/datasources')">{{ 'nav.datasources' | transloco }}</app-menu-item>
      <app-menu-item (activated)="go('/contacts')">{{ 'nav.contacts' | transloco }}</app-menu-item>
      <app-menu-item (activated)="go('/experts')">{{ 'nav.experts' | transloco }}</app-menu-item>
      <app-menu-item (activated)="go('/skills')">{{ 'nav.skills' | transloco }}</app-menu-item>
      <app-menu-item (activated)="go('/automations')">{{ 'nav.automations' | transloco }}</app-menu-item>
      @if (!viewport.isMobile()) {
        <app-menu-item class="menu-divider" (activated)="go('/workbench')">{{ 'nav.workbench' | transloco }}</app-menu-item>
      }
    </app-menu>
  `,
  styles: [`
    .rail-nav {
      display: flex;
      align-items: center;
      gap: 10px;
      width: 100%;
      padding: 8px 12px;
      border: none;
      background: transparent;
      border-radius: var(--radius-control);
      color: var(--text-secondary);
      font-family: inherit;
      font-size: 13px;
      cursor: pointer;
      text-align: left;
      transition:
        background 0.15s ease,
        color 0.15s ease;
    }

    .rail-nav:hover,
    .rail-nav[aria-expanded='true'] {
      background: var(--surface-0);
      color: var(--text-primary);
    }

    /* Content-projection note: app-menu only forwards <app-menu-item>
       children (see AppMenuComponent's template) — a separate divider
       element placed here would silently not render. A border on the item
       that starts the new group draws the same line without needing one. */
    .menu-divider {
      margin-top: 6px;
      border-top: 1px solid var(--border-color);
    }

    @media (max-width: 768px) {
      .rail-nav {
        min-height: 44px;
        padding: 10px 14px;
        gap: 12px;
      }
    }
  `],
})
export class RailMoreMenuComponent {
  protected readonly viewport = inject(ViewportService);
  private readonly router = inject(Router);

  go(path: string): void {
    this.router.navigate([path]);
  }
}
