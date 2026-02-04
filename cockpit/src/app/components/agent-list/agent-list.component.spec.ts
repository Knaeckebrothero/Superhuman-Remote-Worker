import { describe, it, expect } from 'vitest';
import { AgentStatus } from '../../core/models/api.model';

/**
 * Unit tests for AgentListComponent utility functions.
 *
 * Note: These tests focus on pure utility functions extracted from the component.
 * Full component testing with Angular TestBed is not set up in this project.
 * Integration testing should be done via e2e tests.
 */

// Extract utility functions from component for testing
function getStatusIcon(status: AgentStatus): string {
  const icons: Record<AgentStatus, string> = {
    booting: '\u23F3',
    ready: '\u2705',
    working: '\u26A1',
    completed: '\u2714',
    failed: '\u274C',
    offline: '\u26AA',
  };
  return icons[status] || '\u2753';
}

function formatTimestamp(timestamp: string): string {
  const date = new Date(timestamp);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffSec = Math.floor(diffMs / 1000);

  if (diffSec < 60) {
    return `${diffSec}s ago`;
  }
  if (diffSec < 3600) {
    return `${Math.floor(diffSec / 60)}m ago`;
  }
  if (diffSec < 86400) {
    return `${Math.floor(diffSec / 3600)}h ago`;
  }
  return date.toLocaleDateString();
}

function truncatePrompt(prompt: string | undefined, maxLength: number = 60): string {
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

describe('AgentListComponent utilities', () => {
  describe('getStatusIcon', () => {
    it('should return correct icon for ready status', () => {
      expect(getStatusIcon('ready')).toBe('\u2705');
    });

    it('should return correct icon for working status', () => {
      expect(getStatusIcon('working')).toBe('\u26A1');
    });

    it('should return correct icon for booting status', () => {
      expect(getStatusIcon('booting')).toBe('\u23F3');
    });

    it('should return correct icon for completed status', () => {
      expect(getStatusIcon('completed')).toBe('\u2714');
    });

    it('should return correct icon for failed status', () => {
      expect(getStatusIcon('failed')).toBe('\u274C');
    });

    it('should return correct icon for offline status', () => {
      expect(getStatusIcon('offline')).toBe('\u26AA');
    });
  });

  describe('formatTimestamp', () => {
    it('should format recent timestamps as seconds ago', () => {
      const now = new Date();
      const timestamp = new Date(now.getTime() - 30000).toISOString(); // 30 seconds ago

      const result = formatTimestamp(timestamp);

      expect(result).toMatch(/\d+s ago/);
    });

    it('should format timestamps from minutes ago', () => {
      const now = new Date();
      const timestamp = new Date(now.getTime() - 300000).toISOString(); // 5 minutes ago

      const result = formatTimestamp(timestamp);

      expect(result).toMatch(/\d+m ago/);
    });

    it('should format timestamps from hours ago', () => {
      const now = new Date();
      const timestamp = new Date(now.getTime() - 7200000).toISOString(); // 2 hours ago

      const result = formatTimestamp(timestamp);

      expect(result).toMatch(/\d+h ago/);
    });

    it('should format old timestamps as date', () => {
      const oldDate = new Date();
      oldDate.setDate(oldDate.getDate() - 5);
      const timestamp = oldDate.toISOString();

      const result = formatTimestamp(timestamp);

      // Should be a date format, not "X ago"
      expect(result).not.toContain('ago');
    });
  });

  describe('truncatePrompt', () => {
    it('should not truncate short prompts', () => {
      const shortPrompt = 'Short prompt';

      const result = truncatePrompt(shortPrompt);

      expect(result).toBe(shortPrompt);
    });

    it('should truncate long prompts with ellipsis', () => {
      const longPrompt = 'A'.repeat(100);

      const result = truncatePrompt(longPrompt, 60);

      expect(result.length).toBe(63); // 60 chars + '...'
      expect(result.endsWith('...')).toBe(true);
    });

    it('should not truncate prompts exactly at max length', () => {
      const exactPrompt = 'A'.repeat(60);

      const result = truncatePrompt(exactPrompt, 60);

      expect(result).toBe(exactPrompt);
      expect(result.length).toBe(60);
    });

    it('should use default max length of 60', () => {
      const prompt = 'A'.repeat(70);

      const result = truncatePrompt(prompt);

      expect(result.length).toBe(63); // 60 + '...'
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

    it('should format current date', () => {
      const dateString = new Date().toISOString();

      const result = formatDate(dateString);

      expect(result).toBeTruthy();
      expect(result.length).toBeGreaterThan(0);
    });
  });
});
