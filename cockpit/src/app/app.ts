import { Component, inject, OnInit } from '@angular/core';
import { TimelineComponent } from './components/timeline/timeline.component';
import { SplitPanelComponent } from './layout/split-panel/split-panel.component';
import { LayoutService } from './core/services/layout.service';
import { ComponentRegistryService } from './core/services/component-registry.service';
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

@Component({
  selector: 'app-root',
  imports: [TimelineComponent, SplitPanelComponent],
  template: `
    <div class="app-frame">
      <header class="app-header">
        <app-timeline />
      </header>
      <main class="app-main">
        <app-split-panel [config]="layoutService.layout()" />
      </main>
    </div>
  `,
  styles: [
    `
      .app-frame {
        display: flex;
        flex-direction: column;
        height: 100vh;
        width: 100vw;
        overflow: hidden;
      }

      .app-header {
        flex-shrink: 0;
      }

      .app-main {
        flex: 1;
        overflow: hidden;
      }
    `,
  ],
})
export class App implements OnInit {
  readonly layoutService = inject(LayoutService);
  private readonly registry = inject(ComponentRegistryService);

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

    // Job Management Components
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
  }
}
