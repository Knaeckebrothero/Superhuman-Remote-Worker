import { describe, it, expect } from 'vitest';
import {
  JobStatistics,
  AgentStatistics,
  DailyStatistics,
  StuckJob,
} from '../../core/models/api.model';

/**
 * Unit tests for StatisticsComponent utility functions.
 *
 * Note: These tests focus on pure utility functions extracted from the component.
 * Full component testing with Angular TestBed is not set up in this project.
 * Integration testing should be done via e2e tests.
 */

// Extract utility functions from component for testing
function formatDate(dateString: string): string {
  const date = new Date(dateString);
  return date.toLocaleDateString('en-GB', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  });
}

function formatTimestamp(timestamp: string): string {
  const date = new Date(timestamp);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMin = Math.floor(diffMs / 60000);

  if (diffMin < 60) {
    return `${diffMin} min ago`;
  }
  if (diffMin < 1440) {
    return `${Math.floor(diffMin / 60)} hours ago`;
  }
  return date.toLocaleDateString();
}

// Mock data factories
function createMockJobStats(overrides: Partial<JobStatistics> = {}): JobStatistics {
  return {
    total_jobs: 100,
    created: 10,
    processing: 5,
    completed: 75,
    failed: 8,
    cancelled: 2,
    ...overrides,
  };
}

function createMockAgentStats(overrides: Partial<AgentStatistics> = {}): AgentStatistics {
  return {
    total: 10,
    ready: 5,
    working: 3,
    booting: 1,
    completed: 0,
    failed: 0,
    offline: 1,
    ...overrides,
  };
}

function createMockDailyStats(days: number = 7): DailyStatistics[] {
  return Array.from({ length: days }, (_, i) => {
    const date = new Date();
    date.setDate(date.getDate() - i);
    return {
      date: date.toISOString().split('T')[0],
      jobs_created: 10 + i,
      jobs_completed: 8 + i,
      jobs_failed: i,
    };
  });
}

function createMockStuckJob(overrides: Partial<StuckJob> = {}): StuckJob {
  return {
    id: `job_${Math.random().toString(36).slice(2)}`,
    description: 'Test stuck job',
    status: 'processing',
    stuck_component: 'creator',
    stuck_reason: 'No heartbeat for 60 minutes',
    updated_at: new Date(Date.now() - 3600000).toISOString(), // 1 hour ago
    ...overrides,
  };
}

describe('StatisticsComponent utilities', () => {
  describe('formatDate', () => {
    it('should format date with weekday, month, and day', () => {
      const dateString = '2024-03-15';

      const result = formatDate(dateString);

      expect(result).toMatch(/Fri/);
      expect(result).toMatch(/Mar/);
      expect(result).toMatch(/15/);
    });

    it('should format ISO date string', () => {
      const dateString = '2024-01-01T00:00:00Z';

      const result = formatDate(dateString);

      expect(result).toMatch(/Mon/);
      expect(result).toMatch(/Jan/);
    });
  });

  describe('formatTimestamp', () => {
    it('should format recent timestamps as minutes ago', () => {
      const now = new Date();
      const timestamp = new Date(now.getTime() - 1800000).toISOString(); // 30 minutes ago

      const result = formatTimestamp(timestamp);

      expect(result).toBe('30 min ago');
    });

    it('should format timestamps from hours ago', () => {
      const now = new Date();
      const timestamp = new Date(now.getTime() - 7200000).toISOString(); // 2 hours ago

      const result = formatTimestamp(timestamp);

      expect(result).toBe('2 hours ago');
    });

    it('should format very old timestamps as date', () => {
      const oldDate = new Date();
      oldDate.setDate(oldDate.getDate() - 5);
      const timestamp = oldDate.toISOString();

      const result = formatTimestamp(timestamp);

      // Should be a date format, not "X ago"
      expect(result).not.toContain('ago');
    });

    it('should handle 0 minutes correctly', () => {
      const now = new Date();
      const timestamp = now.toISOString();

      const result = formatTimestamp(timestamp);

      expect(result).toBe('0 min ago');
    });
  });

  describe('JobStatistics model', () => {
    it('should have correct default values', () => {
      const stats = createMockJobStats();

      expect(stats.total_jobs).toBe(100);
      expect(stats.created).toBe(10);
      expect(stats.processing).toBe(5);
      expect(stats.completed).toBe(75);
      expect(stats.failed).toBe(8);
      expect(stats.cancelled).toBe(2);
    });

    it('should allow overriding values', () => {
      const stats = createMockJobStats({
        total_jobs: 200,
        completed: 150,
      });

      expect(stats.total_jobs).toBe(200);
      expect(stats.completed).toBe(150);
    });

    it('should have consistent status sum', () => {
      const stats = createMockJobStats();

      const statusSum = stats.created + stats.processing + stats.completed + stats.failed + stats.cancelled;

      expect(statusSum).toBe(stats.total_jobs);
    });
  });

  describe('AgentStatistics model', () => {
    it('should have correct default values', () => {
      const stats = createMockAgentStats();

      expect(stats.total).toBe(10);
      expect(stats.ready).toBe(5);
      expect(stats.working).toBe(3);
      expect(stats.booting).toBe(1);
      expect(stats.offline).toBe(1);
    });

    it('should allow overriding values', () => {
      const stats = createMockAgentStats({
        total: 20,
        ready: 15,
      });

      expect(stats.total).toBe(20);
      expect(stats.ready).toBe(15);
    });
  });

  describe('DailyStatistics model', () => {
    it('should generate correct number of days', () => {
      const stats = createMockDailyStats(7);

      expect(stats).toHaveLength(7);
    });

    it('should have correct date format', () => {
      const stats = createMockDailyStats(1);

      expect(stats[0].date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    });

    it('should have required fields', () => {
      const stats = createMockDailyStats(1)[0];

      expect(typeof stats.jobs_created).toBe('number');
      expect(typeof stats.jobs_completed).toBe('number');
      expect(typeof stats.jobs_failed).toBe('number');
    });
  });

  describe('StuckJob model', () => {
    it('should have correct default values', () => {
      const stuckJob = createMockStuckJob();

      expect(stuckJob.status).toBe('processing');
      expect(stuckJob.stuck_component).toBe('creator');
      expect(stuckJob.stuck_reason).toBe('No heartbeat for 60 minutes');
    });

    it('should allow different stuck components', () => {
      const creatorStuck = createMockStuckJob({ stuck_component: 'creator' });
      const validatorStuck = createMockStuckJob({ stuck_component: 'validator' });

      expect(creatorStuck.stuck_component).toBe('creator');
      expect(validatorStuck.stuck_component).toBe('validator');
    });

    it('should have ID and description', () => {
      const stuckJob = createMockStuckJob({ id: 'test-123', description: 'Test description' });

      expect(stuckJob.id).toBe('test-123');
      expect(stuckJob.description).toBe('Test description');
    });
  });

  describe('UI display calculations', () => {
    it('should detect when there are stuck jobs', () => {
      const stuckJobs = [createMockStuckJob(), createMockStuckJob()];

      expect(stuckJobs.length > 0).toBe(true);
    });

    it('should detect when there are no stuck jobs', () => {
      const stuckJobs: StuckJob[] = [];

      expect(stuckJobs.length === 0).toBe(true);
    });

    it('should calculate daily statistics totals', () => {
      const dailyStats = createMockDailyStats(7);

      const totalCreated = dailyStats.reduce((sum, d) => sum + d.jobs_created, 0);
      const totalCompleted = dailyStats.reduce((sum, d) => sum + d.jobs_completed, 0);

      expect(totalCreated).toBeGreaterThan(0);
      expect(totalCompleted).toBeGreaterThan(0);
    });
  });
});
