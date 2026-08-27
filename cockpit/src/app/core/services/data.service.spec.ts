import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { of } from 'rxjs';
import { DataService } from './data.service';
import { ApiService } from './api.service';
import { IndexedDbService } from './indexed-db.service';

/**
 * DataService is now a thin job-selection holder: the workbench panels load the
 * audit + chat streams lazily via their own paged trace services
 * (AuditTraceService / ChatTraceService) and graph via GraphService, all driven
 * by `currentJobId`. Nothing is eagerly downloaded here anymore — that bulk path
 * OOM'd the orchestrator on large jobs. See
 * knowledge-base/knowledge/features/debug_audit_view_refactor.md (Phase 2c / P3).
 */
describe('DataService', () => {
  let service: DataService;
  let mockApiService: Partial<ApiService>;
  let mockDbService: Partial<IndexedDbService>;

  beforeEach(() => {
    mockApiService = {
      getJobs: vi.fn().mockReturnValue(of([])),
      getJobVersion: vi.fn().mockReturnValue(of(null)),
    };

    mockDbService = {
      clearJob: vi.fn().mockResolvedValue(undefined),
    };

    // Test-mode constructor: provides deps + skips effect setup.
    service = new DataService(
      mockApiService as ApiService,
      mockDbService as IndexedDbService,
    );
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe('initialization', () => {
    it('should create the service with an empty selection', () => {
      expect(service).toBeTruthy();
      expect(service.currentJobId()).toBeNull();
      expect(service.error()).toBeNull();
    });
  });

  describe('job selection', () => {
    it('loadJob sets the current job without eagerly fetching any stream', async () => {
      await service.loadJob('test-job-1');

      expect(service.currentJobId()).toBe('test-job-1');
      // No eager fetching anymore — the trace services load audit/chat lazily.
      expect(mockApiService.getJobVersion).not.toHaveBeenCalled();
    });

    it('loadJob is a no-op when the same job is already selected', async () => {
      await service.loadJob('test-job-1');
      await service.loadJob('test-job-1');
      expect(service.currentJobId()).toBe('test-job-1');
    });

    it('setCurrentJob selects and clears the current job', () => {
      service.setCurrentJob('test-job-1');
      expect(service.currentJobId()).toBe('test-job-1');

      service.setCurrentJob(null);
      expect(service.currentJobId()).toBeNull();
    });

    it('clears the error on a new selection', async () => {
      service.error.set('boom');
      await service.loadJob('test-job-2');
      expect(service.error()).toBeNull();
    });
  });
});
