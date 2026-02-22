import { Component, inject } from '@angular/core';
import { TimelineComponent } from '../../components/timeline/timeline.component';
import { SplitPanelComponent } from '../../layout/split-panel/split-panel.component';
import { LayoutService } from '../../core/services/layout.service';

@Component({
  selector: 'app-debug-page',
  standalone: true,
  imports: [TimelineComponent, SplitPanelComponent],
  template: `
    <div class="debug-frame">
      <header class="debug-header">
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
        flex-shrink: 0;
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
