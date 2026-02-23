import { Injectable, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { ApiService } from './api.service';
import { JobSummary } from '../models/audit.model';

/**
 * Single source of truth for "which job is selected" and "what jobs are available."
 * Both DataService and JobArtifactService delegate to this service.
 */
@Injectable({ providedIn: 'root' })
export class JobContextService {
  private readonly api = inject(ApiService);

  /** Currently selected job ID across all cockpit views */
  readonly activeJobId = signal<string | null>(null);

  /** Available jobs list, shared by all dropdowns */
  readonly jobs = signal<JobSummary[]>([]);

  /** Whether a job list load is in progress */
  readonly isLoadingJobs = signal(false);

  /** Select a job (or null to deselect) */
  selectJob(jobId: string | null): void {
    this.activeJobId.set(jobId);
  }

  /** Load/refresh the jobs list from the API */
  async loadJobs(): Promise<void> {
    this.isLoadingJobs.set(true);
    try {
      const jobs = await firstValueFrom(this.api.getJobs());
      this.jobs.set(jobs);
    } finally {
      this.isLoadingJobs.set(false);
    }
  }
}
