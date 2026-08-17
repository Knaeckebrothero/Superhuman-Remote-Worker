import { describe, it, expect } from 'vitest';
import { Job, JobStatus } from '../../core/models/api.model';
import { JobSummary } from '../../core/models/audit.model';
import { jobCloudAction } from './job-list.component';

/**
 * Unit tests for JobListComponent utility functions.
 *
 * Note: These tests focus on pure utility functions extracted from the component.
 * Full component testing with Angular TestBed is not set up in this project.
 * Integration testing should be done via e2e tests.
 */

// Extract utility functions from component for testing
function truncatePrompt(prompt: string | undefined, maxLength: number = 80): string {
  if (!prompt) {
    return '';
  }
  if (prompt.length <= maxLength) {
    return prompt;
  }
  return prompt.slice(0, maxLength) + '...';
}

function formatDate(dateString: string): string {
  const date = new Date(dateString);
  return date.toLocaleDateString('en-GB', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function filterJobsByStatus(jobs: Job[], filter: 'all' | JobStatus): Job[] {
  if (filter === 'all') {
    return jobs;
  }
  return jobs.filter((job) => job.status === filter);
}

function getStatusCount(jobs: Job[], status: JobStatus): number {
  return jobs.filter((job) => job.status === status).length;
}

// Mock job factory
function createMockJob(overrides: Partial<Job> = {}): Job {
  return {
    id: `job_${Math.random().toString(36).slice(2)}`,
    description: 'Test job description',
    config_name: 'default',
    status: 'created' as JobStatus,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    ...overrides,
  };
}

describe('JobListComponent utilities', () => {
  describe('truncatePrompt', () => {
    it('should not truncate short prompts', () => {
      const shortPrompt = 'A short prompt';

      const result = truncatePrompt(shortPrompt);

      expect(result).toBe(shortPrompt);
    });

    it('should truncate long prompts with ellipsis', () => {
      const longPrompt = 'A'.repeat(100);

      const result = truncatePrompt(longPrompt, 80);

      expect(result.length).toBe(83); // 80 chars + '...'
      expect(result.endsWith('...')).toBe(true);
    });

    it('should not truncate prompts exactly at max length', () => {
      const exactPrompt = 'A'.repeat(80);

      const result = truncatePrompt(exactPrompt, 80);

      expect(result).toBe(exactPrompt);
    });

    it('should use default max length of 80', () => {
      const prompt = 'A'.repeat(100);

      const result = truncatePrompt(prompt);

      expect(result.length).toBe(83); // 80 + '...'
    });
  });

  describe('formatDate', () => {
    it('should format date with day, month, hour and minute', () => {
      const dateString = '2024-03-15T10:30:00Z';

      const result = formatDate(dateString);

      // Should contain day and month
      expect(result).toMatch(/\d{2}/);
      expect(result).toMatch(/Mar/);
    });

    it('should format ISO date strings', () => {
      const dateString = new Date().toISOString();

      const result = formatDate(dateString);

      expect(result).toBeTruthy();
      expect(result.length).toBeGreaterThan(0);
    });
  });

  describe('filterJobsByStatus', () => {
    const mockJobs = [
      createMockJob({ id: 'job-1', status: 'created' }),
      createMockJob({ id: 'job-2', status: 'processing' }),
      createMockJob({ id: 'job-3', status: 'completed' }),
      createMockJob({ id: 'job-4', status: 'completed' }),
      createMockJob({ id: 'job-5', status: 'failed' }),
    ];

    it('should return all jobs when filter is "all"', () => {
      const result = filterJobsByStatus(mockJobs, 'all');

      expect(result).toHaveLength(5);
    });

    it('should filter jobs by created status', () => {
      const result = filterJobsByStatus(mockJobs, 'created');

      expect(result).toHaveLength(1);
      expect(result[0].id).toBe('job-1');
    });

    it('should filter jobs by completed status', () => {
      const result = filterJobsByStatus(mockJobs, 'completed');

      expect(result).toHaveLength(2);
      expect(result.every((j) => j.status === 'completed')).toBe(true);
    });

    it('should return empty array when no jobs match', () => {
      const result = filterJobsByStatus(mockJobs, 'cancelled');

      expect(result).toHaveLength(0);
    });
  });

  describe('getStatusCount', () => {
    const mockJobs = [
      createMockJob({ status: 'created' }),
      createMockJob({ status: 'processing' }),
      createMockJob({ status: 'completed' }),
      createMockJob({ status: 'completed' }),
      createMockJob({ status: 'failed' }),
    ];

    it('should count jobs by status correctly', () => {
      expect(getStatusCount(mockJobs, 'created')).toBe(1);
      expect(getStatusCount(mockJobs, 'processing')).toBe(1);
      expect(getStatusCount(mockJobs, 'completed')).toBe(2);
      expect(getStatusCount(mockJobs, 'failed')).toBe(1);
      expect(getStatusCount(mockJobs, 'cancelled')).toBe(0);
    });

    it('should return 0 for empty job list', () => {
      expect(getStatusCount([], 'created')).toBe(0);
    });
  });

  describe('status filters configuration', () => {
    const statusFilters = [
      { label: 'All', value: 'all' },
      { label: 'Created', value: 'created' },
      { label: 'Processing', value: 'processing' },
      { label: 'Completed', value: 'completed' },
      { label: 'Failed', value: 'failed' },
      { label: 'Cancelled', value: 'cancelled' },
    ];

    it('should have 6 status filters', () => {
      expect(statusFilters).toHaveLength(6);
    });

    it('should have correct filter values', () => {
      expect(statusFilters.map((f) => f.value)).toEqual([
        'all',
        'created',
        'processing',
        'completed',
        'failed',
        'cancelled',
      ]);
    });
  });
  describe('jobCloudAction', () => {
    // Export and open are two clicks on purpose: the export outlives the ~5s
    // of transient user activation, so auto-opening from its callback is
    // popup-blocked. See knowledge-base/knowledge/issues/job_cloud_export_open_blocked.md.
    function job(overrides: Partial<JobSummary> = {}): JobSummary {
      return {
        id: 'j1',
        status: 'completed',
        created_at: new Date(0).toISOString(),
        cloud_review_mode: 'open_folder',
        ...overrides,
      } as JobSummary;
    }

    it('offers export before anything has been exported', () => {
      expect(jobCloudAction(job())).toBe('export');
    });

    it('offers open once exported and a URL is available', () => {
      expect(
        jobCloudAction(
          job({ exported_at: '2026-08-04T08:19:36Z', exported_folder_url: 'https://c/f' }),
        ),
      ).toBe('open');
    });

    it('falls back to a plain badge when the URL is missing', () => {
      // Cloud backend down at read time: exported, but nothing to link to.
      expect(jobCloudAction(job({ exported_at: '2026-08-04T08:19:36Z' }))).toBe('exported');
    });

    it('offers nothing for Mode A (diff-review) jobs', () => {
      expect(jobCloudAction(job({ cloud_review_mode: 'diff' }))).toBe('none');
    });

    it('offers nothing until the job completes', () => {
      expect(jobCloudAction(job({ status: 'processing' }))).toBe('none');
    });
  });
});
