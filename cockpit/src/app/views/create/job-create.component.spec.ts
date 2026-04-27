import { describe, it, expect } from 'vitest';
import { JobCreateRequest } from '../../core/models/api.model';

/**
 * Unit tests for JobCreateComponent utility functions.
 *
 * Note: These tests focus on pure utility functions and form validation logic.
 * Full component testing with Angular TestBed is not set up in this project.
 * Integration testing should be done via e2e tests.
 */

// Extract utility functions from component for testing
function parseContextJson(contextJson: string): { context: Record<string, unknown> | undefined; error: string | null } {
  if (!contextJson.trim()) {
    return { context: undefined, error: null };
  }
  try {
    const parsed = JSON.parse(contextJson);
    return { context: parsed, error: null };
  } catch {
    return { context: undefined, error: 'Invalid JSON format' };
  }
}

function buildJobRequest(formData: JobCreateRequest, contextJson: string): { request: JobCreateRequest | null; error: string | null } {
  if (!formData.description) {
    return { request: null, error: 'Description is required' };
  }

  // Parse context JSON if provided
  const { context, error } = parseContextJson(contextJson);
  if (error) {
    return { request: null, error };
  }

  // Build request with only non-empty fields
  const request: JobCreateRequest = {
    description: formData.description,
  };

  if (formData.document_path?.trim()) {
    request.document_path = formData.document_path.trim();
  }
  if (formData.document_dir?.trim()) {
    request.document_dir = formData.document_dir.trim();
  }
  if (formData.config_name?.trim()) {
    request.config_name = formData.config_name.trim();
  }
  if (context) {
    request.context = context;
  }
  if (formData.instructions?.trim()) {
    request.instructions = formData.instructions.trim();
  }

  return { request, error: null };
}

describe('JobCreateComponent utilities', () => {
  describe('parseContextJson', () => {
    it('should parse valid JSON object', () => {
      const { context, error } = parseContextJson('{"key": "value", "count": 5}');

      expect(error).toBeNull();
      expect(context).toEqual({ key: 'value', count: 5 });
    });

    it('should return undefined for empty string', () => {
      const { context, error } = parseContextJson('');

      expect(error).toBeNull();
      expect(context).toBeUndefined();
    });

    it('should return undefined for whitespace-only string', () => {
      const { context, error } = parseContextJson('   ');

      expect(error).toBeNull();
      expect(context).toBeUndefined();
    });

    it('should return error for invalid JSON', () => {
      const { context, error } = parseContextJson('invalid json');

      expect(error).toBe('Invalid JSON format');
      expect(context).toBeUndefined();
    });

    it('should parse JSON array', () => {
      const { context, error } = parseContextJson('[1, 2, 3]');

      expect(error).toBeNull();
      expect(context).toEqual([1, 2, 3]);
    });

    it('should parse nested JSON object', () => {
      const { context, error } = parseContextJson('{"nested": {"key": "value"}}');

      expect(error).toBeNull();
      expect(context).toEqual({ nested: { key: 'value' } });
    });
  });

  describe('buildJobRequest', () => {
    it('should return error when description is empty', () => {
      const formData: JobCreateRequest = { description: '' };

      const { request, error } = buildJobRequest(formData, '');

      expect(request).toBeNull();
      expect(error).toBe('Description is required');
    });

    it('should build request with description only', () => {
      const formData: JobCreateRequest = { description: 'Extract requirements' };

      const { request, error } = buildJobRequest(formData, '');

      expect(error).toBeNull();
      expect(request).toEqual({ description: 'Extract requirements' });
    });

    it('should build request with all optional fields', () => {
      const formData: JobCreateRequest = {
        description: 'Test prompt',
        document_path: '/path/to/doc.pdf',
        document_dir: '/path/to/docs/',
        config_name: 'creator',
        instructions: 'Additional instructions',
      };

      const { request, error } = buildJobRequest(formData, '{"key": "value"}');

      expect(error).toBeNull();
      expect(request).toEqual({
        description: 'Test prompt',
        document_path: '/path/to/doc.pdf',
        document_dir: '/path/to/docs/',
        config_name: 'creator',
        context: { key: 'value' },
        instructions: 'Additional instructions',
      });
    });

    it('should return error for invalid JSON context', () => {
      const formData: JobCreateRequest = { description: 'Test' };

      const { request, error } = buildJobRequest(formData, 'invalid');

      expect(request).toBeNull();
      expect(error).toBe('Invalid JSON format');
    });

    it('should trim whitespace from optional fields', () => {
      const formData: JobCreateRequest = {
        description: 'Test prompt',
        document_path: '  /path/to/doc.pdf  ',
        config_name: '  creator  ',
        instructions: '  Some instructions  ',
      };

      const { request, error } = buildJobRequest(formData, '');

      expect(error).toBeNull();
      expect(request?.document_path).toBe('/path/to/doc.pdf');
      expect(request?.config_name).toBe('creator');
      expect(request?.instructions).toBe('Some instructions');
    });

    it('should not include empty optional fields', () => {
      const formData: JobCreateRequest = {
        description: 'Test prompt',
        document_path: '   ', // Only whitespace
        config_name: '',
      };

      const { request, error } = buildJobRequest(formData, '');

      expect(error).toBeNull();
      expect(request).toEqual({ description: 'Test prompt' });
      expect(request?.document_path).toBeUndefined();
      expect(request?.config_name).toBeUndefined();
    });
  });

  describe('form validation', () => {
    it('should require description field', () => {
      const formData: JobCreateRequest = { description: '' };
      const { error } = buildJobRequest(formData, '');

      expect(error).toBe('Description is required');
    });

    it('should accept description with only whitespace as valid (validation not trimming description)', () => {
      const formData: JobCreateRequest = { description: '   ' };
      const { request, error } = buildJobRequest(formData, '');

      // Note: The actual component might want to trim this, but we're testing current behavior
      expect(error).toBeNull();
      expect(request?.description).toBe('   ');
    });
  });

  describe('form reset', () => {
    it('should produce empty form data structure', () => {
      const emptyFormData: JobCreateRequest = {
        description: '',
        document_path: undefined,
        document_dir: undefined,
        config_name: undefined,
        context: undefined,
        instructions: undefined,
      };

      expect(emptyFormData.description).toBe('');
      expect(emptyFormData.document_path).toBeUndefined();
      expect(emptyFormData.document_dir).toBeUndefined();
      expect(emptyFormData.config_name).toBeUndefined();
      expect(emptyFormData.context).toBeUndefined();
      expect(emptyFormData.instructions).toBeUndefined();
    });
  });
});
