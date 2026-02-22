import { Component, inject } from '@angular/core';
import { Router, RouterLink, RouterLinkActive } from '@angular/router';
import { UserService } from '../../core/services/user.service';

@Component({
  selector: 'app-sidebar',
  standalone: true,
  imports: [RouterLink, RouterLinkActive],
  template: `
    <nav class="sidebar">
      <div class="sidebar-header">
        <span class="sidebar-logo">SRW</span>
        <span class="sidebar-label">Cockpit</span>
      </div>

      <div class="sidebar-nav">
        <a
          class="nav-link"
          routerLink="/"
          routerLinkActive="active"
          [routerLinkActiveOptions]="{ exact: true }"
        >
          <span class="nav-icon">dashboard</span>
          Simple
        </a>
        <a
          class="nav-link"
          routerLink="/projects"
          routerLinkActive="active"
        >
          <span class="nav-icon">folder_shared</span>
          Projects
        </a>
        <a
          class="nav-link"
          routerLink="/debug"
          routerLinkActive="active"
        >
          <span class="nav-icon">bug_report</span>
          Debug
        </a>
      </div>

      <div class="sidebar-footer">
        @if (userService.currentUser(); as user) {
          <div class="user-profile">
            <span
              class="user-avatar"
              [style.background]="user.avatar_color"
            >{{ getInitials(user.display_name) }}</span>
            <span class="user-name">{{ user.display_name }}</span>
          </div>
          <button class="logout-button" (click)="logout()">Logout</button>
        }
      </div>
    </nav>
  `,
  styles: [
    `
      .sidebar {
        display: flex;
        flex-direction: column;
        width: 200px;
        height: 100%;
        background: var(--panel-bg, #181825);
        border-right: 1px solid var(--border-color, #313244);
      }

      .sidebar-header {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 16px;
        border-bottom: 1px solid var(--border-color, #313244);
      }

      .sidebar-logo {
        font-size: 18px;
        font-weight: 700;
        color: var(--accent-color, #cba6f7);
        letter-spacing: 1px;
      }

      .sidebar-label {
        font-size: 13px;
        color: var(--text-muted, #6c7086);
      }

      .sidebar-nav {
        flex: 1;
        display: flex;
        flex-direction: column;
        gap: 2px;
        padding: 12px 8px;
      }

      .nav-link {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 8px 12px;
        border-radius: 6px;
        color: var(--text-secondary, #a6adc8);
        text-decoration: none;
        font-size: 13px;
        transition:
          background 0.15s ease,
          color 0.15s ease;
      }

      .nav-link:hover {
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
      }

      .nav-link.active {
        background: var(--surface-0, #313244);
        color: var(--accent-color, #cba6f7);
      }

      .nav-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 18px;
      }

      .sidebar-footer {
        padding: 12px;
        border-top: 1px solid var(--border-color, #313244);
        display: flex;
        flex-direction: column;
        gap: 8px;
      }

      .user-profile {
        display: flex;
        align-items: center;
        gap: 8px;
      }

      .user-avatar {
        width: 28px;
        height: 28px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 11px;
        font-weight: 600;
        color: var(--timeline-bg, #11111b);
        flex-shrink: 0;
      }

      .user-name {
        font-size: 12px;
        color: var(--text-primary, #cdd6f4);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .logout-button {
        width: 100%;
        padding: 6px 12px;
        background: transparent;
        border: 1px solid var(--border-color, #313244);
        border-radius: 6px;
        color: var(--text-muted, #6c7086);
        font-size: 12px;
        font-family: inherit;
        cursor: pointer;
        transition:
          color 0.15s ease,
          border-color 0.15s ease;
      }

      .logout-button:hover {
        color: var(--accent-color, #cba6f7);
        border-color: var(--accent-color, #cba6f7);
      }
    `,
  ],
})
export class SidebarComponent {
  readonly userService = inject(UserService);
  private readonly router = inject(Router);

  getInitials(name: string): string {
    return name
      .split(' ')
      .map((w) => w[0])
      .join('')
      .toUpperCase()
      .slice(0, 2);
  }

  logout(): void {
    this.userService.logout();
    this.router.navigate(['/login']);
  }
}
