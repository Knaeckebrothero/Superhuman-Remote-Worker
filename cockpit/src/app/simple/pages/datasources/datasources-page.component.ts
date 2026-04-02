import {Component} from '@angular/core';
import {SidebarToggleComponent} from '../../layout/sidebar-toggle/sidebar-toggle.component';
import {DatasourceListComponent} from '../../../shared/components/datasource-list/datasource-list.component';

@Component({
  selector: 'app-datasources-page',
  standalone: true,
  imports: [SidebarToggleComponent, DatasourceListComponent],
  template: `
    <div class="page">
      <div class="page-toggle">
        <app-sidebar-toggle />
      </div>
      <main class="page-content">
        <app-datasource-list />
      </main>
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
      }

      .page {
        display: flex;
        flex-direction: column;
        height: 100%;
      }

      .page-toggle {
        padding: 8px 12px;
        flex-shrink: 0;
      }

      .page-toggle:empty {
        display: none;
      }

      .page-content {
        flex: 1;
        overflow: hidden;
      }
    `,
  ],
})
export class DatasourcesPageComponent {}
