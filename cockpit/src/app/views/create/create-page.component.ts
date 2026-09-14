import { Component } from '@angular/core';
import { JobCreateComponent } from './job-create.component';

@Component({
  selector: 'app-create-page',
  standalone: true,
  imports: [JobCreateComponent],
  template: `
    <div class="page">
      <main class="page-content">
        <app-job-create />
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
export class CreatePageComponent {}
