import {Component} from '@angular/core';
import {DatasourceListComponent} from './datasource-list.component';

@Component({
  selector: 'app-datasources-page',
  standalone: true,
  imports: [DatasourceListComponent],
  template: `
    <div class="page">
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


      .page-content {
        flex: 1;
        overflow: hidden;
      }
    `,
  ],
})
export class DatasourcesPageComponent {}
