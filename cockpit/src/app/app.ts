import { Component, inject, OnInit } from '@angular/core';
import { TimelineComponent } from './components/timeline/timeline.component';
import { SplitPanelComponent } from './layout/split-panel/split-panel.component';
import { LayoutService } from './core/services/layout.service';
import { ComponentRegistryService } from './core/services/component-registry.service';
import { PlaceholderAComponent } from './components/placeholders/placeholder-a.component';
import { PlaceholderBComponent } from './components/placeholders/placeholder-b.component';
import { PlaceholderCComponent } from './components/placeholders/placeholder-c.component';
import { DbTableComponent } from './components/db-table/db-table.component';

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
  }
}
