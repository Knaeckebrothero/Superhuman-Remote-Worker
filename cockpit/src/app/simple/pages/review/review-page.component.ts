import { Component } from '@angular/core';
import { SidebarToggleComponent } from '../../layout/sidebar-toggle/sidebar-toggle.component';
import { JobReviewComponent } from '../../../shared/components/job-review/job-review.component';

@Component({
  selector: 'app-review-page',
  standalone: true,
  imports: [SidebarToggleComponent, JobReviewComponent],
  template: `
    <div class="page">
      <div class="page-toggle">
        <app-sidebar-toggle />
      </div>
      <main class="page-content">
        <app-job-review />
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
export class ReviewPageComponent {}
