import { Component, inject } from '@angular/core';
import { TimelineComponent } from '../components/timeline/timeline.component';
import { SplitPanelComponent } from '../layout/split-panel/split-panel.component';
import { SidebarToggleComponent } from '../../simple/layout/sidebar-toggle/sidebar-toggle.component';
import { LayoutService } from '../services/layout.service';

@Component({
  selector: 'app-debug-page',
  standalone: true,
  imports: [TimelineComponent, SplitPanelComponent, SidebarToggleComponent],
  template: `
    <div class="debug-frame">
      <header class="debug-header">
        <app-sidebar-toggle />
        <app-timeline />
      </header>
      <main class="debug-main">
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

      .debug-frame {
        display: flex;
        flex-direction: column;
        height: 100%;
        overflow: hidden;
      }

      .debug-header {
        display: flex;
        align-items: center;
        flex-shrink: 0;
      }

      .debug-header app-sidebar-toggle {
        padding-left: 12px;
      }

      .debug-header app-timeline {
        flex: 1;
        min-width: 0;
      }

      .debug-main {
        flex: 1;
        overflow: hidden;
      }
    `,
  ],
})
export class DebugPageComponent {
  readonly layoutService = inject(LayoutService);
}
