import { Component } from '@angular/core';
import { JobListComponent } from './job-list.component';

@Component({
  selector: 'app-jobs-page',
  standalone: true,
  imports: [JobListComponent],
  template: `
    <div class="page">
      <main class="page-content">
        <app-job-list />
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
export class JobsPageComponent {}
