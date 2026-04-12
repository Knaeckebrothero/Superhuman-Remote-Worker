import {Component, computed, inject, signal} from '@angular/core';
import {NavigationEnd, Router, RouterLink, RouterLinkActive} from '@angular/router';
import {takeUntilDestroyed, toSignal} from '@angular/core/rxjs-interop';
import {filter, map} from 'rxjs';
import {UserService} from '../../core/services/user.service';
import {SidebarService} from '../../core/services/sidebar.service';
import {ViewportService} from '../../core/services/viewport.service';
import {LayoutService} from '../../debug/services/layout.service';
import {LayoutPickerComponent} from '../../debug/components/layout-picker/layout-picker.component';
import {NotificationBellComponent} from '../../shared/components/notification-bell/notification-bell.component';
import {PersistentChatService} from '../../core/services/persistent-chat.service';
import {environment} from '../../core/environment';

@Component({
  selector: 'app-sidebar',
  standalone: true,
  imports: [RouterLink, RouterLinkActive, LayoutPickerComponent, NotificationBellComponent],
  template: `
    <nav class="sidebar">
      <div class="sidebar-header">
        <span class="sidebar-logo">SRW</span>
        <span class="sidebar-label">Cockpit</span>
        <button class="collapse-btn" (click)="sidebar.collapse()" title="Collapse sidebar">
          <span class="collapse-icon">chevron_left</span>
        </button>
      </div>

      <div class="sidebar-body">
        <div class="sidebar-nav">
          <a
            class="nav-link"
            routerLink="/"
            routerLinkActive="active"
            [routerLinkActiveOptions]="{ exact: true }"
          >
            <span class="nav-icon">construction</span>
            Builder
          </a>
          <a
            class="nav-link"
            [routerLink]="sessionsLink()"
            routerLinkActive="active"
          >
            <span class="nav-icon">chat</span>
            Sessions
          </a>
          <a
            class="nav-link"
            routerLink="/jobs"
            routerLinkActive="active"
          >
            <span class="nav-icon">work</span>
            Jobs
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
            routerLink="/datasources"
            routerLinkActive="active"
          >
            <span class="nav-icon">database</span>
            Data Sources
          </a>
          <a
            class="nav-link"
            routerLink="/create"
            routerLinkActive="active"
          >
            <span class="nav-icon">add_circle</span>
            Create
          </a>
          @if (!viewport.isMobile()) {
            <a
              class="nav-link"
              routerLink="/debug"
              routerLinkActive="active"
            >
              <span class="nav-icon">bug_report</span>
              Debug
            </a>
          }
        </div>

        @if (isDebugRoute()) {
          <div class="section">
            <div class="section-title">Databases</div>
            <a class="section-link" [href]="neo4jUrl" target="_blank" rel="noopener">
              <span class="link-icon">&#x1F535;</span>Neo4j Browser
            </a>
            <a class="section-link" [href]="pgadminUrl" target="_blank" rel="noopener">
              <span class="link-icon">&#x1F418;</span>PostgreSQL
            </a>
            <a class="section-link" [href]="mongoExpressUrl" target="_blank" rel="noopener">
              <span class="link-icon">&#x1F343;</span>MongoDB
            </a>
          </div>

          <div class="section">
            <div class="section-title">Tools</div>
            <a class="section-link" [href]="giteaUrl" target="_blank" rel="noopener">
              <span class="link-icon">&#x1F375;</span>Gitea
            </a>
            <a class="section-link" [href]="dozzleUrl" target="_blank" rel="noopener">
              <span class="link-icon">&#x1F4CB;</span>Dozzle
            </a>
            @if (minioConsoleUrl) {
              <a class="section-link" [href]="minioConsoleUrl" target="_blank" rel="noopener">
                <span class="link-icon">&#x1F4E6;</span>MinIO
              </a>
            }
            @if (nextcloudUrl) {
              <a class="section-link" [href]="nextcloudUrl" target="_blank" rel="noopener">
                <span class="link-icon">&#x2601;</span>Nextcloud
              </a>
            }
          </div>

          <div class="section">
            <div class="section-title">Layouts</div>
            <button class="section-link" #layoutBtn (click)="toggleLayoutPicker(layoutBtn)">
              <span class="link-icon">&#x1F4D0;</span>Choose Layout
            </button>
            <button class="section-link" (click)="resetLayout()">
              <span class="link-icon">&#x1F504;</span>Reset Layout
            </button>
            @if (isLayoutPickerOpen()) {
              <app-layout-picker
                [top]="pickerTop()"
                [left]="pickerLeft()"
                (closed)="closeLayoutPicker()"
              />
            }
          </div>
        }
      </div>

      <div class="sidebar-footer">
        @if (userService.currentUser(); as user) {
          <div class="user-profile">
            <span
              class="user-avatar"
              [style.background]="user.avatar_color"
            >{{ getInitials(user.display_name) }}</span>
            <span class="user-name">{{ user.display_name }}</span>
            @if (user.is_admin) {
              <span class="admin-badge">admin</span>
            }
          </div>
          <div class="footer-actions">
            <app-notification-bell />
            <a class="footer-link" routerLink="/settings" routerLinkActive="active" title="Settings">
              <span class="nav-icon">settings</span>
            </a>
            <button class="logout-button" (click)="logout()">Logout</button>
          </div>
        }
      </div>
    </nav>
  `,
  styles: [
    `
      :host {
        display: block;
        width: 200px;
        flex-shrink: 0;
        overflow: hidden;
        transition: width 0.2s ease;
      }

      :host(.collapsed) {
        width: 0;
      }

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
        flex-shrink: 0;
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

      .collapse-btn {
        margin-left: auto;
        display: flex;
        align-items: center;
        justify-content: center;
        width: 28px;
        height: 28px;
        background: transparent;
        border: none;
        border-radius: 6px;
        color: var(--text-muted, #6c7086);
        cursor: pointer;
        padding: 0;
        transition:
          color 0.15s ease,
          background 0.15s ease;
      }

      .collapse-btn:hover {
        color: var(--text-primary, #cdd6f4);
        background: var(--surface-0, #313244);
      }

      .collapse-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 18px;
      }

      .sidebar-body {
        flex: 1;
        overflow-y: auto;
        scrollbar-width: thin;
        scrollbar-color: var(--border-color, #313244) transparent;
      }

      .sidebar-nav {
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

      /* Debug sections */

      .section {
        padding: 8px;
        border-top: 1px solid var(--border-color, #313244);
      }

      .section-title {
        font-size: 10px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 1px;
        color: var(--text-muted, #6c7086);
        padding: 4px 8px 6px;
        margin: 0;
      }

      .section-link {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 6px 12px;
        border-radius: 6px;
        color: var(--text-secondary, #a6adc8);
        text-decoration: none;
        font-size: 12px;
        cursor: pointer;
        border: none;
        background: transparent;
        width: 100%;
        text-align: left;
        font-family: inherit;
        transition:
          background 0.15s ease,
          color 0.15s ease;
      }

      .section-link:hover {
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
      }

      .link-icon {
        font-size: 14px;
        width: 18px;
        text-align: center;
        flex-shrink: 0;
      }

      /* Footer */

      .sidebar-footer {
        padding: 12px;
        border-top: 1px solid var(--border-color, #313244);
        display: flex;
        flex-direction: column;
        gap: 8px;
        flex-shrink: 0;
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

      .admin-badge {
        font-size: 9px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: var(--accent-color, #cba6f7);
        opacity: 0.7;
        flex-shrink: 0;
      }

      .footer-actions {
        display: flex;
        align-items: center;
        gap: 8px;
      }

      .footer-link {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 30px;
        height: 30px;
        border-radius: 6px;
        color: var(--text-muted, #6c7086);
        text-decoration: none;
        transition: color 0.15s ease, background 0.15s ease;
      }

      .footer-link:hover,
      .footer-link.active {
        color: var(--accent-color, #cba6f7);
        background: var(--surface-0, #313244);
      }

      .footer-link .nav-icon {
        font-size: 18px;
      }

      .logout-button {
        flex: 1;
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
  readonly sidebar = inject(SidebarService);
  readonly layoutService = inject(LayoutService);
  private readonly router = inject(Router);
  private readonly chatService = inject(PersistentChatService);
  readonly viewport = inject(ViewportService);

  /** Sessions link — always goes to the session list. */
  readonly sessionsLink = computed(() => '/sessions');

  constructor() {
    // Auto-collapse sidebar on mobile after navigation
    this.router.events.pipe(
      filter(e => e instanceof NavigationEnd),
      takeUntilDestroyed(),
    ).subscribe(() => {
      if (this.viewport.isMobile()) {
        this.sidebar.collapse();
      }
    });
  }

  private readonly currentUrl = toSignal(
    this.router.events.pipe(
      filter((e): e is NavigationEnd => e instanceof NavigationEnd),
      map((e) => e.urlAfterRedirects),
    ),
    { initialValue: this.router.url },
  );

  readonly isDebugRoute = computed(
    () => this.currentUrl()?.startsWith('/debug') ?? false,
  );

  readonly giteaUrl = environment.giteaUrl;
  readonly dozzleUrl = environment.dozzleUrl;
  readonly neo4jUrl = environment.neo4jUrl;
  readonly pgadminUrl = environment.pgadminUrl;
  readonly mongoExpressUrl = environment.mongoExpressUrl;
  readonly minioConsoleUrl = environment.minioConsoleUrl;
  readonly nextcloudUrl = environment.nextcloudUrl;

  readonly isLayoutPickerOpen = signal(false);
  readonly pickerTop = signal(0);
  readonly pickerLeft = signal(0);

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
  }

  toggleLayoutPicker(buttonEl: HTMLButtonElement): void {
    if (!this.isLayoutPickerOpen()) {
      const rect = buttonEl.getBoundingClientRect();
      this.pickerTop.set(rect.top);
      this.pickerLeft.set(rect.right + 8);
    }
    this.isLayoutPickerOpen.update((v) => !v);
  }

  closeLayoutPicker(): void {
    this.isLayoutPickerOpen.set(false);
  }

  resetLayout(): void {
    this.layoutService.resetLayout();
  }
}
