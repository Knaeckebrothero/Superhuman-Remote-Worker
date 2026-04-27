import { Component, inject, OnInit } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';

/**
 * Tiny redirect component: reads :jobId/:threadId from route params,
 * navigates to /inbox?job={jobId}&thread={threadId}.
 *
 * Needed because Angular's redirectTo can't transform path params to query params.
 * Catches old email "Reply in Cockpit" links: /jobs/{jobId}/messages/{threadId}
 */
@Component({
  selector: 'app-message-redirect',
  standalone: true,
  template: '<p style="padding: 20px; color: #6c7086;">Redirecting...</p>',
})
export class MessageRedirectComponent implements OnInit {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  ngOnInit(): void {
    const jobId = this.route.snapshot.paramMap.get('jobId');
    const threadId = this.route.snapshot.paramMap.get('threadId');

    if (jobId && threadId) {
      this.router.navigate(['/inbox'], {
        queryParams: { job: jobId, thread: threadId },
        replaceUrl: true,
      });
    } else {
      this.router.navigate(['/inbox'], { replaceUrl: true });
    }
  }
}
