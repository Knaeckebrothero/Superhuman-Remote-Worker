import {Component} from '@angular/core';
import {ExpertsListComponent} from './experts-list.component';

@Component({
  selector: 'app-experts-page',
  standalone: true,
  imports: [ExpertsListComponent],
  template: `
    <div class="page">
      <main class="page-content">
        <app-experts-list />
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
export class ExpertsPageComponent {}
