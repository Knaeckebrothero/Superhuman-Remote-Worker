import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { of } from 'rxjs';
import { DataService } from './data.service';
import { ApiService } from './api.service';
import { IndexedDbService } from './indexed-db.service';
import { AuditEntry } from '../models/audit.model';
import { ChatEntry } from '../models/chat.model';
import { GraphDelta } from '../models/graph.model';
import { JobCacheMetadata } from '../models/cache.model';

// Mock signal for testing (avoids Angular compiler issues in mocks)
function mockSignal<T>(initialValue: T) {
  let value = initialValue;
  const fn = () => value;
  fn.set = (newValue: T) => { value = newValue; };
  fn.update = (updateFn: (v: T) => T) => { value = updateFn(value); };
  return fn;
}

/**
 * Create mock audit entries for testing.
 */
function createMockAuditEntries(count: number, startIndex: number = 0): AuditEntry[] {
  return Array.from({ length: count }, (_, i) => ({
    _id: `audit_${startIndex + i}`,
    job_id: 'test-job-1',
    step_number: startIndex + i,
    step_type: i % 3 === 0 ? 'llm' : i % 3 === 1 ? 'tool' : 'check',
    node_name: 'execute',
    timestamp: new Date(Date.now() + (startIndex + i) * 1000).toISOString(),
    iteration: 1,
  })) as AuditEntry[];
}

/**
 * Create mock chat entries for testing.
 */
function createMockChatEntries(count: number): ChatEntry[] {
  return Array.from({ length: count }, (_, i) => ({
    _id: `chat_${i}`,
    job_id: 'test-job-1',
    agent_type: 'creator',
    sequence_number: i,
    timestamp: new Date(Date.now() + i * 1000).toISOString(),
    iteration: 1,
    model: 'gpt-4',
    inputs: [{ type: 'human' as const, content: `Message ${i}`, content_preview: `Message ${i}` }],
    response: { content: `Response ${i}`, content_preview: `Response ${i}`, has_tool_calls: false },
  }));
}

/**
 * Create mock graph deltas for testing.
 */
function createMockGraphDeltas(count: number): GraphDelta[] {
  return Array.from({ length: count }, (_, i) => ({
    timestamp: new Date(Date.now() + i * 1000).toISOString(),
    toolCallIndex: i,
    cypherQuery: `CREATE (n:Node${i})`,
    toolCallId: `tool_${i}`,
    changes: {
      nodesCreated: [],
      nodesDeleted: [],
      nodesModified: [],
      relationshipsCreated: [],
      relationshipsDeleted: [],
    },
  }));
}

describe('DataService', () => {
  let service: DataService;
  let mockApiService: Partial<ApiService>;
  let mockDbService: Partial<IndexedDbService>;

  beforeEach(() => {
    // Create mock services
    mockApiService = {
      getJobs: vi.fn().mockReturnValue(of([])),
      getJobVersion: vi.fn().mockReturnValue(of(null)),
      getJobAuditBulk: vi.fn().mockReturnValue(
        of({
          entries: [],
          total: 0,
          offset: 0,
          limit: 5000,
          hasMore: false,
        }),
      ),
      getChatHistoryBulk: vi.fn().mockReturnValue(
        of({
          entries: [],
          total: 0,
          offset: 0,
          limit: 5000,
          hasMore: false,
        }),
      ),
      getGraphDeltasBulk: vi.fn().mockReturnValue(
        of({
          deltas: [],
          total: 0,
          offset: 0,
          limit: 5000,
          hasMore: false,
        }),
      ),
    };

    mockDbService = {
      isReady: mockSignal(true),
      getJobMetadata: vi.fn().mockResolvedValue(undefined),
      setJobMetadata: vi.fn().mockResolvedValue(undefined),
      cacheAuditEntries: vi.fn().mockResolvedValue(undefined),
      cacheChatEntries: vi.fn().mockResolvedValue(undefined),
      cacheGraphDeltas: vi.fn().mockResolvedValue(undefined),
      getAuditEntries: vi.fn().mockResolvedValue([]),
      getChatEntries: vi.fn().mockResolvedValue([]),
      getGraphDeltas: vi.fn().mockResolvedValue([]),
      clearJob: vi.fn().mockResolvedValue(undefined),
    };

    // Create service with mocked dependencies
    service = new DataService(
      mockApiService as ApiService,
      mockDbService as IndexedDbService,
    );
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe('initialization', () => {
    it('should create the service with initial state', () => {
      expect(service).toBeTruthy();
      expect(service.currentJobId()).toBeNull();
      expect(service.sliderIndex()).toBe(0);
      expect(service.maxIndex()).toBe(0);
      expect(service.isLoading()).toBe(false);
      expect(service.error()).toBeNull();
      expect(service.isCached()).toBe(false);
    });
  });

  describe('loadJobs', () => {
    it('should load jobs from API', async () => {
      const mockJobs = [
        { id: 'job-1', status: 'completed', created_at: '2024-01-01' },
        { id: 'job-2', status: 'running', created_at: '2024-01-02' },
      ];
      (mockApiService.getJobs as ReturnType<typeof vi.fn>).mockReturnValue(of(mockJobs));

      await service.loadJobs();

      expect(service.jobs()).toEqual(mockJobs);
      expect(service.isLoading()).toBe(false);
      expect(mockApiService.getJobs).toHaveBeenCalled();
    });
  });

  describe('loadJob - with cache', () => {
    it('should use cached data when available and valid', async () => {
      const jobId = 'test-job-1';
      const metadata: JobCacheMetadata = {
        jobId,
        auditEntryCount: 100,
        chatEntryCount: 20,
        graphDeltaCount: 10,
        firstTimestamp: '2024-01-01T00:00:00Z',
        lastTimestamp: '2024-01-01T01:00:00Z',
        cachedAt: new Date().toISOString(),
        version: 1,
      };
      const versionInfo = {
        version: 1,
        auditEntryCount: 100,
        chatEntryCount: 20,
        graphDeltaCount: 10,
        lastUpdate: '2024-01-01T01:00:00Z',
      };

      (mockDbService.getJobMetadata as ReturnType<typeof vi.fn>).mockResolvedValue(metadata);
      (mockApiService.getJobVersion as ReturnType<typeof vi.fn>).mockReturnValue(of(versionInfo));

      const mockEntries = createMockAuditEntries(100);
      (mockDbService.getAuditEntries as ReturnType<typeof vi.fn>).mockResolvedValue(
        mockEntries.slice(-1000),
      );

      await service.loadJob(jobId);

      expect(service.isCached()).toBe(true);
      expect(service.maxIndex()).toBe(99);
      expect(mockApiService.getJobAuditBulk).not.toHaveBeenCalled();
      expect(mockDbService.getAuditEntries).toHaveBeenCalled();
    });

    it('should refetch when cache is stale', async () => {
      const jobId = 'test-job-1';
      const metadata: JobCacheMetadata = {
        jobId,
        auditEntryCount: 50, // Old count
        chatEntryCount: 20,
        graphDeltaCount: 10,
        firstTimestamp: '2024-01-01T00:00:00Z',
        lastTimestamp: '2024-01-01T01:00:00Z',
        cachedAt: new Date().toISOString(),
        version: 1,
      };
      const versionInfo = {
        version: 2,
        auditEntryCount: 100, // New count - more entries
        chatEntryCount: 25,
        graphDeltaCount: 15,
        lastUpdate: '2024-01-01T02:00:00Z',
      };

      (mockDbService.getJobMetadata as ReturnType<typeof vi.fn>)
        .mockResolvedValueOnce(metadata)
        .mockResolvedValueOnce({ ...metadata, auditEntryCount: 100 });
      (mockApiService.getJobVersion as ReturnType<typeof vi.fn>).mockReturnValue(of(versionInfo));

      const mockEntries = createMockAuditEntries(100);
      (mockApiService.getJobAuditBulk as ReturnType<typeof vi.fn>).mockReturnValue(
        of({
          entries: mockEntries,
          total: 100,
          offset: 0,
          limit: 5000,
          hasMore: false,
        }),
      );
      (mockApiService.getChatHistoryBulk as ReturnType<typeof vi.fn>).mockReturnValue(
        of({
          entries: createMockChatEntries(25),
          total: 25,
          offset: 0,
          limit: 5000,
          hasMore: false,
        }),
      );
      (mockApiService.getGraphDeltasBulk as ReturnType<typeof vi.fn>).mockReturnValue(
        of({
          deltas: createMockGraphDeltas(15),
          total: 15,
          offset: 0,
          limit: 5000,
          hasMore: false,
        }),
      );

      await service.loadJob(jobId);

      expect(mockApiService.getJobAuditBulk).toHaveBeenCalled();
      expect(mockDbService.cacheAuditEntries).toHaveBeenCalled();
    });
  });

  describe('loadJob - fresh load', () => {
    it('should fetch and cache data when no cache exists', async () => {
      const jobId = 'test-job-1';
      (mockDbService.getJobMetadata as ReturnType<typeof vi.fn>)
        .mockResolvedValueOnce(undefined)
        .mockResolvedValueOnce({
          jobId,
          auditEntryCount: 100,
          chatEntryCount: 20,
          graphDeltaCount: 10,
          firstTimestamp: '2024-01-01T00:00:00Z',
          lastTimestamp: '2024-01-01T01:00:00Z',
          cachedAt: new Date().toISOString(),
          version: 1,
        });

      const mockEntries = createMockAuditEntries(100);
      (mockApiService.getJobAuditBulk as ReturnType<typeof vi.fn>).mockReturnValue(
        of({
          entries: mockEntries,
          total: 100,
          offset: 0,
          limit: 5000,
          hasMore: false,
        }),
      );
      (mockApiService.getChatHistoryBulk as ReturnType<typeof vi.fn>).mockReturnValue(
        of({
          entries: createMockChatEntries(20),
          total: 20,
          offset: 0,
          limit: 5000,
          hasMore: false,
        }),
      );
      (mockApiService.getGraphDeltasBulk as ReturnType<typeof vi.fn>).mockReturnValue(
        of({
          deltas: createMockGraphDeltas(10),
          total: 10,
          offset: 0,
          limit: 5000,
          hasMore: false,
        }),
      );

      await service.loadJob(jobId);

      expect(service.currentJobId()).toBe(jobId);
      expect(mockApiService.getJobAuditBulk).toHaveBeenCalledWith(jobId, 0, 5000);
      expect(mockDbService.cacheAuditEntries).toHaveBeenCalledWith(jobId, mockEntries, 0);
    });

    it('should handle pagination for large datasets', async () => {
      const jobId = 'test-job-1';
      (mockDbService.getJobMetadata as ReturnType<typeof vi.fn>)
        .mockResolvedValueOnce(undefined)
        .mockResolvedValueOnce({
          jobId,
          auditEntryCount: 7000,
          chatEntryCount: 0,
          graphDeltaCount: 0,
          firstTimestamp: '2024-01-01T00:00:00Z',
          lastTimestamp: '2024-01-01T01:00:00Z',
          cachedAt: new Date().toISOString(),
          version: 1,
        });

      // First batch
      (mockApiService.getJobAuditBulk as ReturnType<typeof vi.fn>).mockReturnValueOnce(
        of({
          entries: createMockAuditEntries(5000, 0),
          total: 7000,
          offset: 0,
          limit: 5000,
          hasMore: true,
        }),
      );
      // Second batch
      (mockApiService.getJobAuditBulk as ReturnType<typeof vi.fn>).mockReturnValueOnce(
        of({
          entries: createMockAuditEntries(2000, 5000),
          total: 7000,
          offset: 5000,
          limit: 5000,
          hasMore: false,
        }),
      );
      (mockApiService.getChatHistoryBulk as ReturnType<typeof vi.fn>).mockReturnValue(
        of({ entries: [], total: 0, offset: 0, limit: 5000, hasMore: false }),
      );
      (mockApiService.getGraphDeltasBulk as ReturnType<typeof vi.fn>).mockReturnValue(
        of({ deltas: [], total: 0, offset: 0, limit: 5000, hasMore: false }),
      );

      await service.loadJob(jobId);

      expect(mockApiService.getJobAuditBulk).toHaveBeenCalledTimes(2);
      expect(mockDbService.cacheAuditEntries).toHaveBeenCalledTimes(2);
    });
  });

  describe('slider navigation', () => {
    async function setupLoadedJob() {
      const metadata: JobCacheMetadata = {
        jobId: 'test-job-1',
        auditEntryCount: 500,
        chatEntryCount: 100,
        graphDeltaCount: 50,
        firstTimestamp: '2024-01-01T00:00:00Z',
        lastTimestamp: '2024-01-01T01:00:00Z',
        cachedAt: new Date().toISOString(),
        version: 1,
      };
      const versionInfo = {
        version: 1,
        auditEntryCount: 500,
        chatEntryCount: 100,
        graphDeltaCount: 50,
        lastUpdate: '2024-01-01T01:00:00Z',
      };

      (mockDbService.getJobMetadata as ReturnType<typeof vi.fn>).mockResolvedValue(metadata);
      (mockApiService.getJobVersion as ReturnType<typeof vi.fn>).mockReturnValue(of(versionInfo));
      (mockDbService.getAuditEntries as ReturnType<typeof vi.fn>).mockResolvedValue(
        createMockAuditEntries(500),
      );
      (mockDbService.getChatEntries as ReturnType<typeof vi.fn>).mockResolvedValue(
        createMockChatEntries(100),
      );
      (mockDbService.getGraphDeltas as ReturnType<typeof vi.fn>).mockResolvedValue(
        createMockGraphDeltas(50),
      );

      await service.loadJob('test-job-1');
    }

    it('should set slider index within bounds', async () => {
      await setupLoadedJob();

      service.setSliderIndex(250);
      expect(service.sliderIndex()).toBe(250);
    });

    it('should clamp slider index to max', async () => {
      await setupLoadedJob();

      service.setSliderIndex(1000); // Beyond max
      expect(service.sliderIndex()).toBe(499); // Clamped to maxIndex
    });

    it('should clamp slider index to 0', async () => {
      await setupLoadedJob();

      service.setSliderIndex(-10); // Below 0
      expect(service.sliderIndex()).toBe(0);
    });

    it('should seek to end', async () => {
      await setupLoadedJob();

      service.setSliderIndex(100);
      service.seekToEnd();
      expect(service.sliderIndex()).toBe(499);
    });

    it('should seek to start', async () => {
      await setupLoadedJob();

      service.setSliderIndex(250);
      service.seekToStart();
      expect(service.sliderIndex()).toBe(0);
    });
  });

  describe('filtering', () => {
    it('should update active filter', () => {
      expect(service.activeFilter()).toBe('all');

      service.setFilter('messages');
      expect(service.activeFilter()).toBe('messages');

      service.setFilter('tools');
      expect(service.activeFilter()).toBe('tools');

      service.setFilter('errors');
      expect(service.activeFilter()).toBe('errors');
    });
  });

  describe('computed values', () => {
    it('should compute total audit entries', async () => {
      const metadata: JobCacheMetadata = {
        jobId: 'test-job-1',
        auditEntryCount: 250,
        chatEntryCount: 50,
        graphDeltaCount: 25,
        firstTimestamp: '2024-01-01T00:00:00Z',
        lastTimestamp: '2024-01-01T01:00:00Z',
        cachedAt: new Date().toISOString(),
        version: 1,
      };
      const versionInfo = {
        version: 1,
        auditEntryCount: 250,
        chatEntryCount: 50,
        graphDeltaCount: 25,
        lastUpdate: '2024-01-01T01:00:00Z',
      };

      (mockDbService.getJobMetadata as ReturnType<typeof vi.fn>).mockResolvedValue(metadata);
      (mockApiService.getJobVersion as ReturnType<typeof vi.fn>).mockReturnValue(of(versionInfo));
      (mockDbService.getAuditEntries as ReturnType<typeof vi.fn>).mockResolvedValue(
        createMockAuditEntries(250),
      );

      await service.loadJob('test-job-1');

      expect(service.totalAuditEntries()).toBe(250);
    });

    it('should compute time range from metadata', async () => {
      const metadata: JobCacheMetadata = {
        jobId: 'test-job-1',
        auditEntryCount: 100,
        chatEntryCount: 20,
        graphDeltaCount: 10,
        firstTimestamp: '2024-01-01T00:00:00Z',
        lastTimestamp: '2024-01-01T01:00:00Z',
        cachedAt: new Date().toISOString(),
        version: 1,
      };
      const versionInfo = {
        version: 1,
        auditEntryCount: 100,
        chatEntryCount: 20,
        graphDeltaCount: 10,
        lastUpdate: '2024-01-01T01:00:00Z',
      };

      (mockDbService.getJobMetadata as ReturnType<typeof vi.fn>).mockResolvedValue(metadata);
      (mockApiService.getJobVersion as ReturnType<typeof vi.fn>).mockReturnValue(of(versionInfo));
      (mockDbService.getAuditEntries as ReturnType<typeof vi.fn>).mockResolvedValue(
        createMockAuditEntries(100),
      );

      await service.loadJob('test-job-1');

      const timeRange = service.timeRange();
      expect(timeRange).toEqual({
        start: '2024-01-01T00:00:00Z',
        end: '2024-01-01T01:00:00Z',
      });
    });
  });

  describe('refresh', () => {
    it('should clear cache and reload job', async () => {
      // First load
      const metadata: JobCacheMetadata = {
        jobId: 'test-job-1',
        auditEntryCount: 100,
        chatEntryCount: 20,
        graphDeltaCount: 10,
        firstTimestamp: '2024-01-01T00:00:00Z',
        lastTimestamp: '2024-01-01T01:00:00Z',
        cachedAt: new Date().toISOString(),
        version: 1,
      };
      (mockDbService.getJobMetadata as ReturnType<typeof vi.fn>)
        .mockResolvedValueOnce(metadata)
        .mockResolvedValueOnce(undefined)
        .mockResolvedValueOnce(metadata);
      (mockApiService.getJobVersion as ReturnType<typeof vi.fn>).mockReturnValue(
        of({
          version: 1,
          auditEntryCount: 100,
          chatEntryCount: 20,
          graphDeltaCount: 10,
          lastUpdate: '2024-01-01T01:00:00Z',
        }),
      );
      (mockDbService.getAuditEntries as ReturnType<typeof vi.fn>).mockResolvedValue(
        createMockAuditEntries(100),
      );

      await service.loadJob('test-job-1');
      expect(service.isCached()).toBe(true);

      // Setup for refresh (will need to refetch since cache is cleared)
      (mockApiService.getJobAuditBulk as ReturnType<typeof vi.fn>).mockReturnValue(
        of({
          entries: createMockAuditEntries(100),
          total: 100,
          offset: 0,
          limit: 5000,
          hasMore: false,
        }),
      );
      (mockApiService.getChatHistoryBulk as ReturnType<typeof vi.fn>).mockReturnValue(
        of({ entries: [], total: 0, offset: 0, limit: 5000, hasMore: false }),
      );
      (mockApiService.getGraphDeltasBulk as ReturnType<typeof vi.fn>).mockReturnValue(
        of({ deltas: [], total: 0, offset: 0, limit: 5000, hasMore: false }),
      );

      await service.refresh();

      expect(mockDbService.clearJob).toHaveBeenCalledWith('test-job-1');
    });
  });

  describe('clear', () => {
    it('should reset all state', async () => {
      const metadata: JobCacheMetadata = {
        jobId: 'test-job-1',
        auditEntryCount: 100,
        chatEntryCount: 20,
        graphDeltaCount: 10,
        firstTimestamp: '2024-01-01T00:00:00Z',
        lastTimestamp: '2024-01-01T01:00:00Z',
        cachedAt: new Date().toISOString(),
        version: 1,
      };
      (mockDbService.getJobMetadata as ReturnType<typeof vi.fn>).mockResolvedValue(metadata);
      (mockApiService.getJobVersion as ReturnType<typeof vi.fn>).mockReturnValue(
        of({
          version: 1,
          auditEntryCount: 100,
          chatEntryCount: 20,
          graphDeltaCount: 10,
          lastUpdate: '2024-01-01T01:00:00Z',
        }),
      );
      (mockDbService.getAuditEntries as ReturnType<typeof vi.fn>).mockResolvedValue(
        createMockAuditEntries(100),
      );

      await service.loadJob('test-job-1');
      expect(service.currentJobId()).toBe('test-job-1');

      service.clear();

      expect(service.currentJobId()).toBeNull();
      expect(service.sliderIndex()).toBe(0);
      expect(service.maxIndex()).toBe(0);
      expect(service.isCached()).toBe(false);
      expect(service.cacheMetadata()).toBeNull();
    });
  });

  describe('no job loaded', () => {
    it('should not load if same job is already loaded', async () => {
      const metadata: JobCacheMetadata = {
        jobId: 'test-job-1',
        auditEntryCount: 100,
        chatEntryCount: 20,
        graphDeltaCount: 10,
        firstTimestamp: '2024-01-01T00:00:00Z',
        lastTimestamp: '2024-01-01T01:00:00Z',
        cachedAt: new Date().toISOString(),
        version: 1,
      };
      (mockDbService.getJobMetadata as ReturnType<typeof vi.fn>).mockResolvedValue(metadata);
      (mockApiService.getJobVersion as ReturnType<typeof vi.fn>).mockReturnValue(
        of({
          version: 1,
          auditEntryCount: 100,
          chatEntryCount: 20,
          graphDeltaCount: 10,
          lastUpdate: '2024-01-01T01:00:00Z',
        }),
      );
      (mockDbService.getAuditEntries as ReturnType<typeof vi.fn>).mockResolvedValue(
        createMockAuditEntries(100),
      );

      await service.loadJob('test-job-1');
      const callCount = (mockDbService.getJobMetadata as ReturnType<typeof vi.fn>).mock.calls
        .length;

      // Try to load same job again
      await service.loadJob('test-job-1');

      // Should not call getJobMetadata again
      expect((mockDbService.getJobMetadata as ReturnType<typeof vi.fn>).mock.calls.length).toBe(
        callCount,
      );
    });
  });
});
