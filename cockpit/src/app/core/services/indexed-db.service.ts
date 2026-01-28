import { Injectable, signal, PLATFORM_ID, inject } from '@angular/core';
import { isPlatformBrowser } from '@angular/common';
import Dexie, { Table } from 'dexie';
import { AuditEntry } from '../models/audit.model';
import { ChatEntry } from '../models/chat.model';
import { GraphDelta } from '../models/graph.model';
import {
  CachedAuditEntry,
  CachedChatEntry,
  CachedGraphDelta,
  JobCacheMetadata,
} from '../models/cache.model';

/** Current cache schema version */
const CACHE_VERSION = 1;

/**
 * Dexie database class for cockpit cache.
 * Defines tables and indexes for efficient querying.
 */
class CockpitDatabase extends Dexie {
  auditEntries!: Table<CachedAuditEntry>;
  chatEntries!: Table<CachedChatEntry>;
  graphDeltas!: Table<CachedGraphDelta>;
  jobMetadata!: Table<JobCacheMetadata>;

  constructor() {
    super('cockpit-cache');
    this.version(1).stores({
      // Primary key: id, indexes: jobId, compound [jobId+index], compound [jobId+stepType+index]
      auditEntries: 'id, jobId, [jobId+index], [jobId+stepType+index]',
      // Primary key: id, indexes: jobId, compound [jobId+sequenceNumber]
      chatEntries: 'id, jobId, [jobId+sequenceNumber]',
      // Primary key: id, indexes: jobId, compound [jobId+index]
      graphDeltas: 'id, jobId, [jobId+index]',
      // Primary key: jobId
      jobMetadata: 'jobId',
    });
  }
}

/**
 * IndexedDB caching service using Dexie.js.
 * Provides client-side storage for audit entries, chat history, and graph deltas.
 *
 * Usage:
 * - Call cacheAuditEntries() to store entries from API response
 * - Call getAuditEntries() to retrieve cached entries by index range
 * - Use getJobMetadata() to check cache state before fetching from API
 */
@Injectable({ providedIn: 'root' })
export class IndexedDbService {
  private db: CockpitDatabase | null = null;
  private readonly platformId = inject(PLATFORM_ID);
  private readonly isBrowser: boolean;

  /** Whether the database is ready for operations */
  readonly isReady = signal(false);

  /** Error message if initialization failed */
  readonly error = signal<string | null>(null);

  constructor() {
    this.isBrowser = isPlatformBrowser(this.platformId);
    if (this.isBrowser) {
      this.db = new CockpitDatabase();
      this.init();
    }
    // On server, db stays null and isReady stays false - that's fine for SSR
  }

  private async init(): Promise<void> {
    if (!this.db) return;
    try {
      await this.db.open();
      this.isReady.set(true);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this.error.set(`IndexedDB initialization failed: ${message}`);
      console.error('IndexedDB initialization failed:', err);
    }
  }

  // ===== Job Metadata =====

  /**
   * Get cache metadata for a job.
   */
  async getJobMetadata(jobId: string): Promise<JobCacheMetadata | undefined> {
    if (!this.db) return undefined;
    return this.db.jobMetadata.get(jobId);
  }

  /**
   * Set or update cache metadata for a job.
   */
  async setJobMetadata(metadata: JobCacheMetadata): Promise<void> {
    if (!this.db) return;
    await this.db.jobMetadata.put(metadata);
  }

  /**
   * Check if a job has cached data.
   */
  async hasJob(jobId: string): Promise<boolean> {
    if (!this.db) return false;
    const metadata = await this.db.jobMetadata.get(jobId);
    return metadata !== undefined;
  }

  // ===== Audit Entries =====

  /**
   * Cache audit entries for a job.
   * Entries are indexed by step_number for efficient range queries.
   *
   * @param jobId The job ID
   * @param entries Audit entries to cache (must have step_number)
   * @param startIndex Starting index for these entries (for incremental caching)
   */
  async cacheAuditEntries(
    jobId: string,
    entries: AuditEntry[],
    startIndex: number = 0,
  ): Promise<void> {
    if (!this.db) return;
    const cached: CachedAuditEntry[] = entries.map((entry, i) => ({
      id: `${jobId}_${startIndex + i}`,
      jobId,
      index: startIndex + i,
      timestamp: entry.timestamp,
      stepType: entry.step_type,
      data: entry,
    }));

    await this.db.auditEntries.bulkPut(cached);

    // Update metadata
    await this.updateAuditMetadata(jobId, cached);
  }

  /**
   * Get audit entries by index range (inclusive).
   */
  async getAuditEntries(
    jobId: string,
    startIndex: number,
    endIndex: number,
  ): Promise<AuditEntry[]> {
    if (!this.db) return [];
    const entries = await this.db.auditEntries
      .where('[jobId+index]')
      .between([jobId, startIndex], [jobId, endIndex], true, true)
      .toArray();

    return entries.map((e) => e.data);
  }

  /**
   * Get audit entries filtered by step type within an index range.
   */
  async getAuditEntriesByType(
    jobId: string,
    stepType: string,
    startIndex: number,
    endIndex: number,
  ): Promise<AuditEntry[]> {
    if (!this.db) return [];
    // Use compound index for efficient filtering
    const entries = await this.db.auditEntries
      .where('[jobId+stepType+index]')
      .between([jobId, stepType, startIndex], [jobId, stepType, endIndex], true, true)
      .toArray();

    return entries.map((e) => e.data);
  }

  /**
   * Get the count of cached audit entries for a job.
   */
  async getAuditEntryCount(jobId: string): Promise<number> {
    if (!this.db) return 0;
    return this.db.auditEntries.where('jobId').equals(jobId).count();
  }

  private async updateAuditMetadata(
    jobId: string,
    newEntries: CachedAuditEntry[],
  ): Promise<void> {
    if (!this.db || newEntries.length === 0) return;

    const existing = await this.db.jobMetadata.get(jobId);
    const count = await this.getAuditEntryCount(jobId);

    const timestamps = newEntries.map((e) => e.timestamp).sort();
    const firstNew = timestamps[0];
    const lastNew = timestamps[timestamps.length - 1];

    const metadata: JobCacheMetadata = {
      jobId,
      auditEntryCount: count,
      chatEntryCount: existing?.chatEntryCount ?? 0,
      graphDeltaCount: existing?.graphDeltaCount ?? 0,
      firstTimestamp: this.minTimestamp(existing?.firstTimestamp, firstNew),
      lastTimestamp: this.maxTimestamp(existing?.lastTimestamp, lastNew),
      cachedAt: new Date().toISOString(),
      version: CACHE_VERSION,
    };

    await this.db.jobMetadata.put(metadata);
  }

  // ===== Chat Entries =====

  /**
   * Cache chat entries for a job.
   */
  async cacheChatEntries(jobId: string, entries: ChatEntry[]): Promise<void> {
    if (!this.db) return;
    const cached: CachedChatEntry[] = entries.map((entry) => ({
      id: `${jobId}_${entry.sequence_number}`,
      jobId,
      sequenceNumber: entry.sequence_number,
      timestamp: entry.timestamp,
      data: entry,
    }));

    await this.db.chatEntries.bulkPut(cached);

    // Update metadata
    await this.updateChatMetadata(jobId, cached);
  }

  /**
   * Get chat entries by sequence number range (inclusive).
   */
  async getChatEntries(
    jobId: string,
    startSeq: number,
    endSeq: number,
  ): Promise<ChatEntry[]> {
    if (!this.db) return [];
    const entries = await this.db.chatEntries
      .where('[jobId+sequenceNumber]')
      .between([jobId, startSeq], [jobId, endSeq], true, true)
      .toArray();

    return entries.map((e) => e.data);
  }

  /**
   * Get the count of cached chat entries for a job.
   */
  async getChatEntryCount(jobId: string): Promise<number> {
    if (!this.db) return 0;
    return this.db.chatEntries.where('jobId').equals(jobId).count();
  }

  private async updateChatMetadata(
    jobId: string,
    newEntries: CachedChatEntry[],
  ): Promise<void> {
    if (!this.db || newEntries.length === 0) return;

    const existing = await this.db.jobMetadata.get(jobId);
    const count = await this.getChatEntryCount(jobId);

    const timestamps = newEntries.map((e) => e.timestamp).sort();
    const firstNew = timestamps[0];
    const lastNew = timestamps[timestamps.length - 1];

    const metadata: JobCacheMetadata = {
      jobId,
      auditEntryCount: existing?.auditEntryCount ?? 0,
      chatEntryCount: count,
      graphDeltaCount: existing?.graphDeltaCount ?? 0,
      firstTimestamp: this.minTimestamp(existing?.firstTimestamp, firstNew),
      lastTimestamp: this.maxTimestamp(existing?.lastTimestamp, lastNew),
      cachedAt: new Date().toISOString(),
      version: CACHE_VERSION,
    };

    await this.db.jobMetadata.put(metadata);
  }

  // ===== Graph Deltas =====

  /**
   * Cache graph deltas for a job.
   */
  async cacheGraphDeltas(jobId: string, deltas: GraphDelta[]): Promise<void> {
    if (!this.db) return;
    const cached: CachedGraphDelta[] = deltas.map((delta) => ({
      id: `${jobId}_${delta.toolCallIndex}`,
      jobId,
      index: delta.toolCallIndex,
      timestamp: delta.timestamp,
      data: delta,
    }));

    await this.db.graphDeltas.bulkPut(cached);

    // Update metadata
    await this.updateGraphMetadata(jobId, cached);
  }

  /**
   * Get graph deltas by index range (inclusive).
   */
  async getGraphDeltas(
    jobId: string,
    startIndex: number,
    endIndex: number,
  ): Promise<GraphDelta[]> {
    if (!this.db) return [];
    const entries = await this.db.graphDeltas
      .where('[jobId+index]')
      .between([jobId, startIndex], [jobId, endIndex], true, true)
      .toArray();

    return entries.map((e) => e.data);
  }

  /**
   * Get the count of cached graph deltas for a job.
   */
  async getGraphDeltaCount(jobId: string): Promise<number> {
    if (!this.db) return 0;
    return this.db.graphDeltas.where('jobId').equals(jobId).count();
  }

  private async updateGraphMetadata(
    jobId: string,
    newEntries: CachedGraphDelta[],
  ): Promise<void> {
    if (!this.db || newEntries.length === 0) return;

    const existing = await this.db.jobMetadata.get(jobId);
    const count = await this.getGraphDeltaCount(jobId);

    const timestamps = newEntries.map((e) => e.timestamp).sort();
    const firstNew = timestamps[0];
    const lastNew = timestamps[timestamps.length - 1];

    const metadata: JobCacheMetadata = {
      jobId,
      auditEntryCount: existing?.auditEntryCount ?? 0,
      chatEntryCount: existing?.chatEntryCount ?? 0,
      graphDeltaCount: count,
      firstTimestamp: this.minTimestamp(existing?.firstTimestamp, firstNew),
      lastTimestamp: this.maxTimestamp(existing?.lastTimestamp, lastNew),
      cachedAt: new Date().toISOString(),
      version: CACHE_VERSION,
    };

    await this.db.jobMetadata.put(metadata);
  }

  // ===== Cache Management =====

  /**
   * Clear all cached data for a specific job.
   */
  async clearJob(jobId: string): Promise<void> {
    if (!this.db) return;
    await Promise.all([
      this.db.auditEntries.where('jobId').equals(jobId).delete(),
      this.db.chatEntries.where('jobId').equals(jobId).delete(),
      this.db.graphDeltas.where('jobId').equals(jobId).delete(),
      this.db.jobMetadata.delete(jobId),
    ]);
  }

  /**
   * Clear all cached data.
   */
  async clearAll(): Promise<void> {
    if (!this.db) return;
    await Promise.all([
      this.db.auditEntries.clear(),
      this.db.chatEntries.clear(),
      this.db.graphDeltas.clear(),
      this.db.jobMetadata.clear(),
    ]);
  }

  /**
   * Get storage usage estimate.
   * Returns usage and quota in bytes.
   */
  async getStorageEstimate(): Promise<{ usage: number; quota: number }> {
    if (!this.isBrowser) return { usage: 0, quota: 0 };
    if (navigator.storage && navigator.storage.estimate) {
      const estimate = await navigator.storage.estimate();
      return {
        usage: estimate.usage ?? 0,
        quota: estimate.quota ?? 0,
      };
    }
    return { usage: 0, quota: 0 };
  }

  // ===== Helpers =====

  private minTimestamp(a: string | null | undefined, b: string): string {
    if (!a) return b;
    return a < b ? a : b;
  }

  private maxTimestamp(a: string | null | undefined, b: string): string {
    if (!a) return b;
    return a > b ? a : b;
  }
}
