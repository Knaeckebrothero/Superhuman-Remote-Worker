import {Component, computed, effect, inject, OnInit} from '@angular/core';
import {RouterOutlet} from '@angular/router';
import {SidebarComponent} from './shell/sidebar/sidebar.component';
import {AppToastContainerComponent} from './ui/toast';
import {ComponentRegistryService} from './core/services/component-registry.service';
import {ViewportService} from './core/services/viewport.service';
import {UserService} from './core/services/user.service';
import {SidebarService} from './core/services/sidebar.service';
import {ActionCenterService} from './core/services/action-center.service';
// Debug-only components
import {PlaceholderAComponent} from './debug/components/placeholders/placeholder-a.component';
import {PlaceholderBComponent} from './debug/components/placeholders/placeholder-b.component';
import {PlaceholderCComponent} from './debug/components/placeholders/placeholder-c.component';
import {DbTableComponent} from './debug/components/db-table/db-table.component';
import {AgentActivityComponent} from './debug/components/agent-activity/agent-activity.component';
import {RequestViewerComponent} from './debug/components/request-viewer/request-viewer.component';
import {GraphTimelineComponent} from './debug/components/graph-timeline/graph-timeline.component';
// Shared components
import {TodoListComponent} from './views/todos/todo-list.component';
import {ChatHistoryComponent} from './views/chat-history/chat-history.component';
import {AgentListComponent} from './views/agents/agent-list.component';
import {JobListComponent} from './views/jobs/job-list.component';
import {JobCreateComponent} from './views/create/job-create.component';
import {StatisticsComponent} from './views/statistics/statistics.component';
import {DatasourceListComponent} from './views/datasources/datasource-list.component';
import {ExpertsListComponent} from './views/experts/experts-list.component';
import {JobReviewComponent} from './views/job-review/job-review.component';
import {WorkspaceBrowserComponent} from './views/workspace-browser/workspace-browser.component';
import {ProjectListPageComponent} from './views/projects/project-list.component';
import {MemoryPanelComponent} from './debug/components/memory-panel/memory-panel.component';
import {InboxPageComponent} from './views/inbox/inbox-page.component';
import {ConfigEditorComponent} from './views/config-editor/config-editor.component';
import {EmptyCatalogBannerComponent} from './shell/empty-catalog-banner/empty-catalog-banner.component';
import {ReadinessGateBannerComponent} from './shell/readiness-gate-banner/readiness-gate-banner.component';
import {ViewModeBannerComponent} from './shell/view-mode-banner/view-mode-banner.component';
import {AppPwaBannerComponent} from './shell/pwa-banner/pwa-banner.component';
import {AppIconComponent} from './ui/icon';

@Component({
  selector: 'app-root',
  imports: [
    RouterOutlet,
    SidebarComponent,
    AppToastContainerComponent,
    EmptyCatalogBannerComponent,
    ReadinessGateBannerComponent,
    ViewModeBannerComponent,
    AppPwaBannerComponent,
    AppIconComponent,
  ],
  template: `
    <app-pwa-banner />
    <div class="app-container">
      @if (showSidebar()) {
        <app-sidebar [class.collapsed]="sidebar.collapsed()" />
      }
      @if (showMobileBackdrop()) {
        <div class="sidebar-backdrop" (click)="sidebar.collapse()"></div>
      }
      <div class="content-area">
        @if (pendingApproval()) {
          <div class="pending-approval">
            <div class="pending-approval-card">
              <app-icon size="inherit" class="pending-icon">hourglass_empty</app-icon>
              <h2>Account Pending Approval</h2>
              <p>Your account has been created but an administrator needs to approve it before you can access the system.</p>
              <p class="pending-detail">You'll get full access as soon as an administrator approves your account.</p>
              <button class="pending-logout" (click)="userService.logout()">Logout</button>
            </div>
          </div>
        } @else {
          <app-readiness-gate-banner />
          <app-empty-catalog-banner />
          <app-view-mode-banner />
          <router-outlet />
        }
      </div>
    </div>
    <app-toast-container />
  `,
  styles: [
    `
      .app-container {
        display: flex;
        height: 100vh;
        height: 100dvh;
        width: 100vw;
        overflow: hidden;
      }

      .content-area {
        flex: 1;
        display: flex;
        flex-direction: column;
        min-width: 0;
        min-height: 0;
        overflow: hidden;
        position: relative;
      }

      .content-area > router-outlet ~ * {
        flex: 1;
        min-height: 0;
      }

      .pending-approval {
        display: flex;
        align-items: center;
        justify-content: center;
        height: 100%;
        padding: 2rem;
        background: var(--bg-primary, #1e1e2e);
      }

      .pending-approval-card {
        text-align: center;
        max-width: 480px;
        padding: 3rem 2.5rem;
        border-radius: var(--radius-surface);
        background: var(--bg-secondary, #313244);
        border: 1px solid var(--border-color, #45475a);
      }

      .pending-icon {
        font-size: 3rem;
        color: var(--text-tertiary, #a6adc8);
        display: block;
        margin-bottom: 1rem;
      }

      .pending-approval-card h2 {
        margin: 0 0 1rem;
        color: var(--text-primary, #cdd6f4);
        font-size: 1.5rem;
        font-weight: 600;
      }

      .pending-approval-card p {
        margin: 0 0 0.75rem;
        color: var(--text-secondary, #bac2de);
        line-height: 1.6;
      }

      .pending-detail {
        font-size: 0.875rem;
        color: var(--text-tertiary, #a6adc8);
      }

      .pending-logout {
        margin-top: 1.5rem;
        padding: 0.625rem 2rem;
        border: 1px solid var(--border-color, #45475a);
        border-radius: var(--radius-control);
        background: transparent;
        color: var(--text-secondary, #bac2de);
        font-size: 0.875rem;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .pending-logout:hover {
        background: var(--bg-tertiary, #45475a);
        color: var(--text-primary, #cdd6f4);
      }

      .sidebar-backdrop {
        display: none;
      }

      @media (max-width: 768px) {
        /* Sidebar overlay on mobile */
        app-sidebar {
          position: fixed;
          top: 0;
          left: 0;
          z-index: 1000;
          height: 100dvh;
          transform: translateX(-100%);
          transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
          will-change: transform;
        }

        app-sidebar:not(.collapsed) {
          transform: translateX(0);
        }

        .sidebar-backdrop {
          display: block;
          position: fixed;
          inset: 0;
          background: rgba(17, 17, 27, 0.6);
          backdrop-filter: blur(4px);
          z-index: 999;
          transition: opacity 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }

      }
    `,
  ],
})
export class App implements OnInit {
  private readonly viewport = inject(ViewportService);
  readonly userService = inject(UserService);
  private readonly registry = inject(ComponentRegistryService);
  readonly sidebar = inject(SidebarService);
  private readonly actionCenter = inject(ActionCenterService);

  readonly showSidebar = computed(
    () => this.userService.isAuthenticated() && this.userService.isApproved(),
  );

  readonly showMobileBackdrop = computed(
    () => this.viewport.isMobile() && !this.sidebar.collapsed() && this.showSidebar(),
  );

  /** Show the pending-approval screen when authenticated but not yet approved. */
  readonly pendingApproval = computed(
    () => this.userService.isAuthenticated() && !this.userService.isApproved(),
  );

  constructor() {
    // Lock body scroll when mobile sidebar is open
    effect(() => {
      if (this.viewport.isMobile() && !this.sidebar.collapsed()) {
        document.body.style.overflow = 'hidden';
      } else {
        document.body.style.overflow = '';
      }
    });

    // Open the always-on notification SSE as soon as the user is signed
    // in and approved. Previously this was lazy-initialised inside the
    // Inbox page's ngOnInit, which meant `/notifications/events` wasn't
    // open while the user was on /sessions creating a new chat — so
    // `session.lifecycle` events never reached the cockpit before they
    // were emitted. ``initSSE()`` is idempotent (guarded by its own
    // `sseInitialized` flag), safe to call any time the predicate flips.
    effect(() => {
      if (this.userService.isAuthenticated() && this.userService.isApproved()) {
        this.actionCenter.initSSE();
      }
    });
  }

  ngOnInit(): void {
    this.registerComponents();
  }

  private registerComponents(): void {
    this.registry.register({
      type: 'placeholder-a',
      displayName: 'Workspace',
      component: PlaceholderAComponent,
    });

    this.registry.register({
      type: 'placeholder-b',
      displayName: 'Agent Chat',
      component: PlaceholderBComponent,
    });

    this.registry.register({
      type: 'placeholder-c',
      displayName: 'Database',
      component: PlaceholderCComponent,
    });

    this.registry.register({
      type: 'db-table',
      displayName: 'PostgreSQL Tables',
      component: DbTableComponent,
    });

    this.registry.register({
      type: 'agent-activity',
      displayName: 'Agent Activity',
      component: AgentActivityComponent,
    });

    this.registry.register({
      type: 'request-viewer',
      displayName: 'Request Viewer',
      component: RequestViewerComponent,
    });

    this.registry.register({
      type: 'graph-timeline',
      displayName: 'Graph Timeline',
      component: GraphTimelineComponent,
    });

    this.registry.register({
      type: 'todo-list',
      displayName: 'Todo List',
      component: TodoListComponent,
    });

    this.registry.register({
      type: 'agent-chat',
      displayName: 'Chat History',
      component: ChatHistoryComponent,
    });

    this.registry.register({
      type: 'agent-list',
      displayName: 'Agents',
      component: AgentListComponent,
    });

    this.registry.register({
      type: 'job-list',
      displayName: 'Jobs',
      component: JobListComponent,
    });

    this.registry.register({
      type: 'job-create',
      displayName: 'New Job',
      component: JobCreateComponent,
    });

    this.registry.register({
      type: 'statistics',
      displayName: 'Statistics',
      component: StatisticsComponent,
    });

    this.registry.register({
      type: 'datasource-list',
      displayName: 'Datasources',
      component: DatasourceListComponent,
    });

    this.registry.register({
      type: 'experts-list',
      displayName: 'Experts',
      component: ExpertsListComponent,
    });

    this.registry.register({
      type: 'job-review',
      displayName: 'Job Review',
      component: JobReviewComponent,
    });

    this.registry.register({
      type: 'workspace-browser',
      displayName: 'Workspace Browser',
      component: WorkspaceBrowserComponent,
    });

    this.registry.register({
      type: 'project-list',
      displayName: 'Projects',
      component: ProjectListPageComponent,
    });

    this.registry.register({
      type: 'memory-panel',
      displayName: 'Memory Panel',
      component: MemoryPanelComponent,
    });

    this.registry.register({
      type: 'action-center',
      displayName: 'Action Center',
      component: InboxPageComponent,
    });

    this.registry.register({
      type: 'config-editor',
      displayName: 'Config Editor',
      component: ConfigEditorComponent,
    });
  }
}
