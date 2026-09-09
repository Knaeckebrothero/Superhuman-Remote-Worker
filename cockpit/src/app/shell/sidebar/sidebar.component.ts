import {Component, computed, inject, signal} from '@angular/core';
import {NavigationEnd, Router, RouterLink, RouterLinkActive} from '@angular/router';
import {takeUntilDestroyed, toSignal} from '@angular/core/rxjs-interop';
import {filter, map} from 'rxjs';
import {SidebarService} from '../../core/services/sidebar.service';
import {ViewportService} from '../../core/services/viewport.service';
import {SessionListService} from '../../core/services/session-list.service';
import {LayoutService} from '../../workbench/services/layout.service';
import {LayoutPickerComponent} from '../../workbench/components/layout-picker/layout-picker.component';
import {NotificationBellComponent} from '../notification-bell/notification-bell.component';
import {PersistentChatService} from '../../core/services/persistent-chat.service';
import {environment} from '../../core/environment';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppIconComponent} from '../../ui/icon';
import {LegionMarkComponent} from '../../ui/legion-mark';
import {AppTabNavComponent, AppTabNavItemComponent} from '../../ui/tab-nav';
import {RailMoreMenuComponent} from '../rail-more-menu/rail-more-menu.component';
import {RailAccountMenuComponent} from '../rail-account-menu/rail-account-menu.component';

export type RailMode = 'chat' | 'jobs' | 'projects';

const MODE_ROUTES: Record<RailMode, string> = {
  chat: '/',
  jobs: '/jobs',
  projects: '/projects',
};

@Component({
  selector: 'app-sidebar',
  standalone: true,
  imports: [RouterLink, RouterLinkActive, LayoutPickerComponent, NotificationBellComponent, TranslocoPipe, AppIconComponent, LegionMarkComponent, AppTabNavComponent, AppTabNavItemComponent, RailMoreMenuComponent, RailAccountMenuComponent],
  template: `
    <nav class="sidebar" (click)="onSidebarClick($event)">
      <div class="sidebar-header">
        <div class="sidebar-brand">
          <srw-legion-mark [size]="22" />
          <div class="sidebar-brand-stack">
            <span class="sidebar-logo">SRW</span>
            <span class="sidebar-label">{{ 'nav.cockpit' | transloco }}</span>
          </div>
        </div>
        <button class="collapse-btn" (click)="sidebar.collapse()" [title]="'nav.collapseSidebar' | transloco">
          <app-icon size="md" class="collapse-icon">chevron_left</app-icon>
        </button>
      </div>

      <div class="sidebar-body">
        <app-tab-nav class="mode-switcher" [value]="mode()" (valueChange)="selectMode($event)">
          <app-tab-nav-item value="chat">{{ 'nav.modeChat' | transloco }}</app-tab-nav-item>
          <app-tab-nav-item value="jobs">{{ 'nav.modeJobs' | transloco }}</app-tab-nav-item>
          <app-tab-nav-item value="projects">{{ 'nav.modeProjects' | transloco }}</app-tab-nav-item>
        </app-tab-nav>

        <a class="rail-new" routerLink="/">
          <app-icon size="md">edit_square</app-icon> {{ 'nav.newChat' | transloco }}
        </a>

        @for (group of sessionGroups(); track group.label) {
          <div class="rail-group">{{ ('nav.recency.' + group.label) | transloco }}</div>
          @for (t of group.threads; track t.id) {
            <a class="rail-item" [routerLink]="['/sessions', t.id]" routerLinkActive="active">
              {{ t.title }}
            </a>
          }
        }

        @if (isWorkbenchRoute()) {
          <div class="section">
            <div class="section-title">Databases</div>
            <a class="section-link" [href]="neo4jUrl" target="_blank" rel="noopener">
              <span class="link-icon">&#x1F535;</span>Neo4j Browser
            </a>
            <a class="section-link" [href]="pgadminUrl" target="_blank" rel="noopener">
              <span class="link-icon">&#x1F418;</span>PostgreSQL
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
            @if (cloudUrl) {
              <a class="section-link" [href]="cloudUrl" target="_blank" rel="noopener">
                <span class="link-icon">&#x2601;</span>Cloud
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
        <app-notification-bell />
        <app-rail-more-menu />
        <div class="rail-divider"></div>
        <app-rail-account-menu />
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
        background: var(--panel-bg);
        border-right: 1px solid var(--border-color);
      }

      .sidebar-header {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 16px;
        border-bottom: 1px solid var(--border-color);
        flex-shrink: 0;
      }

      .sidebar-brand {
        display: flex;
        align-items: center;
        gap: 10px;
        color: var(--accent-color);
      }

      .sidebar-brand-stack {
        display: flex;
        flex-direction: column;
        line-height: 1;
        gap: 3px;
      }

      .sidebar-logo {
        font-family: var(--font-display, inherit);
        font-size: 18px;
        font-weight: 700;
        color: var(--accent-color);
        letter-spacing: 1px;
      }

      .sidebar-label {
        font-family: var(--font-display, inherit);
        font-size: 11px;
        letter-spacing: 0.18em;
        text-transform: uppercase;
        color: var(--text-muted);
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
        border-radius: var(--radius-control);
        color: var(--text-muted);
        cursor: pointer;
        padding: 0;
        transition:
          color 0.15s ease,
          background 0.15s ease;
      }

      .collapse-btn:hover {
        color: var(--text-primary);
        background: var(--surface-0);
      }


      .sidebar-body {
        flex: 1;
        overflow-y: auto;
        scrollbar-width: thin;
        scrollbar-color: var(--border-color) transparent;
      }

      .mode-switcher {
        margin: 8px;
      }

      /* Rail session list: the Chat mode's "New chat" action and the
         recency-grouped thread list that fills the space below the
         switcher. */

      .rail-new {
        display: flex;
        align-items: center;
        gap: 10px;
        margin: 0 8px;
        padding: 8px 12px;
        border-radius: var(--radius-control);
        color: var(--text-secondary);
        text-decoration: none;
        font-size: 13px;
        transition:
          background 0.15s ease,
          color 0.15s ease;
      }

      .rail-new:hover {
        background: var(--surface-0);
        color: var(--text-primary);
      }

      .rail-group {
        margin: 0 8px;
        padding: 12px 4px 4px;
        font-size: 10px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 1px;
        color: var(--text-muted);
      }

      .rail-item {
        display: block;
        margin: 0 8px;
        padding: 8px 12px;
        border-radius: var(--radius-control);
        color: var(--text-secondary);
        text-decoration: none;
        font-size: 13px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        transition:
          background 0.15s ease,
          color 0.15s ease;
      }

      .rail-item:hover {
        background: var(--surface-0);
        color: var(--text-primary);
      }

      .rail-item.active {
        background: var(--surface-0);
        color: var(--accent-color);
      }

      /* Mobile drawer sizing: the 200px/13px desktop rail reads cramped as an
         overlay drawer. Widen it (capped below the viewport so the backdrop
         stays tappable) and scale the type/targets for thumbs. The width:0
         collapse still wins via :host(.collapsed) specificity, unchanged. */
      @media (max-width: 768px) {
        :host,
        .sidebar {
          width: min(300px, 84vw);
        }

        .sidebar-logo {
          font-size: 20px;
        }

        .sidebar-label {
          font-size: 12px;
        }

        /* Tap-target restoration (Task 8 step 5): Task 6 deleted the old flat
           nav's .nav-link rule — min-height: 44px; padding: 10px 14px;
           gap: 12px — along with the links it sized. Every control the rail
           has grown since (Tasks 6, 7 and this one) needs that minimum back.
           .rail-new and .rail-item are rendered directly in this template,
           so one rule reaches both. The More and avatar triggers (.rail-nav,
           .rail-account) are owned by their own components now and restore
           this same rule in their own stylesheets — Emulated encapsulation
           means a rule here can't reach into their templates. The mode
           switcher's tabs are app-tab-nav-item, a shared ui/ component with
           the same encapsulation boundary; ::ng-deep reaches its host
           element, scoped under .mode-switcher so the other app-tab-nav
           consumers (admin-models, agent-settings) are unaffected. */
        .rail-new,
        .rail-item {
          min-height: 44px;
          padding: 10px 14px;
          gap: 12px;
        }

        .mode-switcher ::ng-deep app-tab-nav-item {
          min-height: 44px;
          padding: 10px 14px;
          gap: 12px;
        }
      }

      /* Workbench sections */

      .section {
        padding: 8px;
        border-top: 1px solid var(--border-color);
      }

      .section-title {
        font-size: 10px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 1px;
        color: var(--text-muted);
        padding: 4px 8px 6px;
        margin: 0;
      }

      .section-link {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 6px 12px;
        border-radius: var(--radius-control);
        color: var(--text-secondary);
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
        background: var(--surface-0);
        color: var(--text-primary);
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
        border-top: 1px solid var(--border-color);
        display: flex;
        flex-direction: column;
        gap: 8px;
        flex-shrink: 0;
      }

      /* Everything else in this column stretches full-width by default (flex
         column + the default align-items: stretch). The bell keeps its own
         icon-sized footprint instead, so its unread badge — positioned via
         the bell's own :host box — stays pinned to the icon rather than
         drifting toward the far edge of the rail. */
      .sidebar-footer app-notification-bell {
        align-self: flex-start;
      }

      .rail-divider {
        border-top: 1px solid var(--border-color);
      }
    `,
  ],
})
export class SidebarComponent {
  readonly sidebar = inject(SidebarService);
  readonly layoutService = inject(LayoutService);
  private readonly router = inject(Router);
  private readonly chatService = inject(PersistentChatService);
  readonly viewport = inject(ViewportService);
  private readonly sessions = inject(SessionListService);

  readonly mode = computed<RailMode | null>(() => {
    // Allowlist, deliberately not a fallback: a route that is none of the three
    // modes must light no tab, rather than defaulting to Chat and telling the
    // user they are somewhere they are not. Routes outside these three
    // (/experts, /settings, /admin/*, ...) are reached from the More and avatar
    // menus and have no mode of their own.
    // router.url carries the query string and fragment (e.g. '/?foo=bar') —
    // strip both before matching, or the landing page itself falls through.
    const path = this.currentUrl().split(/[?#]/)[0];
    if (path === '/' || path.startsWith('/sessions')) return 'chat';
    if (path.startsWith('/jobs')) return 'jobs';
    if (path.startsWith('/projects')) return 'projects';
    return null;
  });

  // Null, same as 'jobs'/'projects': outside chat mode the rail shows no
  // session groups at all (see the mode() allowlist above).
  readonly sessionGroups = computed(() =>
    this.mode() === 'chat' ? this.sessions.grouped() : [],
  );

  selectMode(mode: RailMode | null): void {
    // Always the mode's own route. Never a thread id — see the April 2026
    // hijack regression recorded in coding_agent_ui_assessment.md §3.
    if (mode === null) return;
    this.router.navigate([MODE_ROUTES[mode]]);
  }

  constructor() {
    // enabledNonBlocking (Angular's default) constructs this component before the
    // first navigation, so the subscription below covers the initial load. If the
    // sidebar is instead constructed after navigation already finished
    // (enabledBlocking, SSR, or a remount), no event is coming and we must fetch here.
    if (this.router.navigated) {
      this.sessions.refresh();
    }

    // Auto-collapse sidebar on mobile after navigation, and keep the rail's
    // session list current — reusing this subscription rather than adding a
    // second one. Gated on chat mode so navigating within Jobs/Admin doesn't
    // refetch threads for a list that isn't even shown.
    this.router.events.pipe(
      filter(e => e instanceof NavigationEnd),
      takeUntilDestroyed(),
    ).subscribe(() => {
      if (this.viewport.isMobile()) {
        this.sidebar.collapse();
      }
      if (this.mode() === 'chat') {
        this.sessions.refresh();
      }
    });
  }

  /**
   * Close the mobile drawer on any link tap, delegated from the nav root.
   * The NavigationEnd subscription above misses the most intuitive dismiss
   * gesture: tapping the page you're already on — a same-URL navigation is
   * skipped by the router and emits nothing, so the drawer just sat there.
   * Buttons (bell, logout, collapse) are exempt on purpose.
   */
  onSidebarClick(event: Event): void {
    if (!this.viewport.isMobile()) return;
    if ((event.target as HTMLElement).closest('a[href]')) {
      this.sidebar.collapse();
    }
  }

  private readonly currentUrl = toSignal(
    this.router.events.pipe(
      filter((e): e is NavigationEnd => e instanceof NavigationEnd),
      map((e) => e.urlAfterRedirects),
    ),
    { initialValue: this.router.url },
  );

  readonly isWorkbenchRoute = computed(
    () => this.currentUrl()?.startsWith('/workbench') ?? false,
  );

  readonly giteaUrl = environment.giteaUrl;
  readonly dozzleUrl = environment.dozzleUrl;
  readonly neo4jUrl = environment.neo4jUrl;
  readonly pgadminUrl = environment.pgadminUrl;
  readonly minioConsoleUrl = environment.minioConsoleUrl;
  readonly cloudUrl = environment.cloudUrl;

  readonly isLayoutPickerOpen = signal(false);
  readonly pickerTop = signal(0);
  readonly pickerLeft = signal(0);

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
