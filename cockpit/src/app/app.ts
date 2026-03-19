import { Component, inject, OnInit, computed } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { SidebarComponent } from './layout/sidebar/sidebar.component';
import { ToastComponent } from './core/components/toast/toast.component';
import { ComponentRegistryService } from './core/services/component-registry.service';
import { ViewportService } from './core/services/viewport.service';
import { UserService } from './core/services/user.service';
import { SidebarService } from './core/services/sidebar.service';
// Debug-only components
import { PlaceholderAComponent } from './debug/components/placeholders/placeholder-a.component';
import { PlaceholderBComponent } from './debug/components/placeholders/placeholder-b.component';
import { PlaceholderCComponent } from './debug/components/placeholders/placeholder-c.component';
import { DbTableComponent } from './debug/components/db-table/db-table.component';
import { AgentActivityComponent } from './debug/components/agent-activity/agent-activity.component';
import { RequestViewerComponent } from './debug/components/request-viewer/request-viewer.component';
import { GraphTimelineComponent } from './debug/components/graph-timeline/graph-timeline.component';
// Shared components
import { TodoListComponent } from './shared/components/todo-list/todo-list.component';
import { ChatHistoryComponent } from './shared/components/chat-history/chat-history.component';
import { AgentListComponent } from './shared/components/agent-list/agent-list.component';
import { JobListComponent } from './shared/components/job-list/job-list.component';
import { JobCreateComponent } from './shared/components/job-create/job-create.component';
import { StatisticsComponent } from './shared/components/statistics/statistics.component';
import { DatasourceListComponent } from './shared/components/datasource-list/datasource-list.component';
import { JobReviewComponent } from './shared/components/job-review/job-review.component';
import { WorkspaceBrowserComponent } from './shared/components/workspace-browser/workspace-browser.component';
import { InstructionBuilderComponent } from './shared/components/instruction-builder/instruction-builder.component';
import { ProjectListPageComponent } from './shared/pages/project-list.component';
import { MemoryPanelComponent } from './debug/components/memory-panel/memory-panel.component';
import { InboxPageComponent } from './simple/pages/inbox/inbox-page.component';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, SidebarComponent, ToastComponent],
  template: `
    <div class="app-container">
      @if (showSidebar()) {
        <app-sidebar [class.collapsed]="sidebar.collapsed()" />
      }
      <div class="content-area">
        <router-outlet />
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
        overflow: auto;
        position: relative;
      }

    `,
  ],
})
export class App implements OnInit {
  private readonly viewport = inject(ViewportService);
  private readonly userService = inject(UserService);
  private readonly registry = inject(ComponentRegistryService);
  readonly sidebar = inject(SidebarService);

  readonly showSidebar = computed(
    () => !this.viewport.isMobile() && this.userService.isAuthenticated(),
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
  }
}
