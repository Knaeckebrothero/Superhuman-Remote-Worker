import { Component, inject } from '@angular/core';
import { TimelineComponent } from '../components/timeline/timeline.component';
import { SplitPanelComponent } from '../layout/split-panel/split-panel.component';
import { SidebarToggleComponent } from '../../shell/sidebar-toggle/sidebar-toggle.component';
import { LayoutService } from '../services/layout.service';

@Component({
  selector: 'app-workbench-page',
  standalone: true,
  imports: [TimelineComponent, SplitPanelComponent, SidebarToggleComponent],
  template: `
    <div class="workbench-frame">
      <header class="workbench-header">
        <app-sidebar-toggle />
        <app-timeline />
      </header>
      <main class="workbench-main">
        <app-split-panel [config]="layoutService.layout()" />
      </main>
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
      }

      .workbench-frame {
        display: flex;
        flex-direction: column;
        height: 100%;
        overflow: hidden;
      }

      .workbench-header {
        display: flex;
        align-items: center;
        flex-shrink: 0;
      }

      .workbench-header app-sidebar-toggle {
        padding-left: 12px;
      }

      .workbench-header app-timeline {
        flex: 1;
        min-width: 0;
      }

      .workbench-main {
        flex: 1;
        overflow: hidden;
      }
    `,
  ],
})
export class WorkbenchPageComponent {
  readonly layoutService = inject(LayoutService);
}
