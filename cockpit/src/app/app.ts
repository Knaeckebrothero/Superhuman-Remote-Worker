import { Component, inject, OnInit, computed } from '@angular/core';
import { Router, RouterOutlet, NavigationEnd } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';
import { filter, map } from 'rxjs';
import { SidebarComponent } from './layout/sidebar/sidebar.component';
import { ComponentRegistryService } from './core/services/component-registry.service';
import { ViewportService } from './core/services/viewport.service';
import { UserService } from './core/services/user.service';
import { SidebarService } from './core/services/sidebar.service';
import { PlaceholderAComponent } from './components/placeholders/placeholder-a.component';
import { PlaceholderBComponent } from './components/placeholders/placeholder-b.component';
import { PlaceholderCComponent } from './components/placeholders/placeholder-c.component';
import { DbTableComponent } from './components/db-table/db-table.component';
import { AgentActivityComponent } from './components/agent-activity/agent-activity.component';
import { RequestViewerComponent } from './components/request-viewer/request-viewer.component';
import { GraphTimelineComponent } from './components/graph-timeline/graph-timeline.component';
import { TodoListComponent } from './components/todo-list/todo-list.component';
import { ChatHistoryComponent } from './components/chat-history/chat-history.component';
import { AgentListComponent } from './components/agent-list/agent-list.component';
import { JobListComponent } from './components/job-list/job-list.component';
import { JobCreateComponent } from './components/job-create/job-create.component';
import { StatisticsComponent } from './components/statistics/statistics.component';
import { DatasourceListComponent } from './components/datasource-list/datasource-list.component';
import { JobReviewComponent } from './components/job-review/job-review.component';
import { WorkspaceBrowserComponent } from './components/workspace-browser/workspace-browser.component';
import { InstructionBuilderComponent } from './components/instruction-builder/instruction-builder.component';
import { ProjectListPageComponent } from './pages/project-list/project-list.component';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, SidebarComponent],
  template: `
    <div class="app-container">
      @if (showSidebar()) {
        <app-sidebar [class.collapsed]="sidebar.collapsed()" />
      }
      <div class="content-area">
        <router-outlet />
      </div>
    </div>
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
        overflow: hidden;
        position: relative;
      }

    `,
  ],
})
export class App implements OnInit {
  private readonly router = inject(Router);
  private readonly viewport = inject(ViewportService);
  private readonly userService = inject(UserService);
  private readonly registry = inject(ComponentRegistryService);
  readonly sidebar = inject(SidebarService);

  private readonly currentUrl = toSignal(
    this.router.events.pipe(
      filter((e): e is NavigationEnd => e instanceof NavigationEnd),
      map((e) => e.urlAfterRedirects),
    ),
    { initialValue: this.router.url },
  );

  readonly showSidebar = computed(
    () =>
      !this.viewport.isMobile() &&
      this.userService.isAuthenticated() &&
      this.currentUrl() !== '/login',
  );

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
      type: 'instruction-builder',
      displayName: 'Instruction Builder',
      component: InstructionBuilderComponent,
    });

    this.registry.register({
      type: 'project-list',
      displayName: 'Projects',
      component: ProjectListPageComponent,
    });
  }
}
