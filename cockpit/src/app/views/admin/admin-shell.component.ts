import {Component} from '@angular/core';
import {RouterLink, RouterLinkActive, RouterOutlet} from '@angular/router';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppIconComponent} from '../../ui/icon';

/**
 * The gated /admin section shell.
 *
 * Six former top-level rail links (Models, Users, Config, Grants, Usage,
 * Capacity) used to sit as peers of Sessions; they now collapse into one
 * avatar-menu entry (RailAccountMenuComponent) that opens here. The six
 * routes are unchanged — same paths, same effective guards, just hoisted
 * onto this shell's parent `admin` route instead of repeated on each child
 * (see app.routes.ts) — so every existing deep link and bookmark still
 * resolves.
 */
@Component({
  selector: 'app-admin-shell',
  standalone: true,
  imports: [RouterOutlet, RouterLink, RouterLinkActive, AppIconComponent, TranslocoPipe],
  template: `
    <div class="admin-shell">
      <nav class="admin-sub">
        <div class="admin-sub-title">
          <app-icon size="md">shield</app-icon> {{ 'nav.admin' | transloco }}
        </div>
        @for (item of items; track item.path) {
          <a [routerLink]="item.path" routerLinkActive="active">{{ item.key | transloco }}</a>
        }
      </nav>
      <div class="admin-body"><router-outlet /></div>
    </div>
  `,
  styles: [`
    :host {
      display: block;
      height: 100%;
      overflow: hidden;
    }

    .admin-shell {
      display: flex;
      height: 100%;
    }

    .admin-sub {
      display: flex;
      flex-direction: column;
      gap: 2px;
      width: 200px;
      flex-shrink: 0;
      padding: 16px 0;
      background: var(--panel-bg);
      border-right: 1px solid var(--border-color);
      overflow-y: auto;
    }

    .admin-sub-title {
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 0 8px 8px;
      padding: 8px 12px;
      color: var(--text-primary);
      font-size: 13px;
      font-weight: 600;
    }

    .admin-sub a {
      display: block;
      margin: 0 8px;
      padding: 8px 12px;
      border-radius: var(--radius-control);
      color: var(--text-secondary);
      text-decoration: none;
      font-size: 13px;
      white-space: nowrap;
    }

    .admin-sub a:hover {
      background: var(--surface-0);
      color: var(--text-primary);
    }

    .admin-sub a.active {
      background: var(--surface-0);
      color: var(--accent-color);
    }

    .admin-body {
      flex: 1;
      min-width: 0;
      height: 100%;
      overflow: hidden;
    }
  `],
})
export class AdminShellComponent {
  protected readonly items = [
    {path: '/admin/models', key: 'admin.nav.models'},
    {path: '/admin/users', key: 'admin.nav.users'},
    {path: '/admin/config', key: 'admin.nav.config'},
    {path: '/admin/grants', key: 'admin.nav.grants'},
    {path: '/admin/usage', key: 'admin.nav.usage'},
    {path: '/admin/capacity', key: 'admin.nav.capacity'},
  ];
}
