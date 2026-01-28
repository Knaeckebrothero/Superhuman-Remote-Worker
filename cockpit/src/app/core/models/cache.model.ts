/**
 * Cache-specific models for IndexedDB storage.
 * These wrap the existing domain models with job context and indexing fields.
 */

import { AuditEntry } from './audit.model';
import { ChatEntry } from './chat.model';
import { GraphDelta } from './graph.model';

/**
 * Cached audit entry with job context and index for efficient retrieval.
 */
export interface CachedAuditEntry {
  /** Composite key: `${jobId}_${index}` */
  id: string;
  jobId: string;
  /** Sequential index within the job (based on step_number) */
  index: number;
  timestamp: string;
  stepType: string;
  /** The original audit entry data */
  data: AuditEntry;
}

/**
 * Cached chat entry with job context and sequence number.
 */
export interface CachedChatEntry {
  /** Composite key: `${jobId}_${sequenceNumber}` */
  id: string;
  jobId: string;
  sequenceNumber: number;
  timestamp: string;
  /** The original chat entry data */
  data: ChatEntry;
}

/**
 * Cached graph delta with job context and index.
 */
export interface CachedGraphDelta {
  /** Composite key: `${jobId}_${index}` */
  id: string;
  jobId: string;
  /** Index based on toolCallIndex */
  index: number;
  timestamp: string;
  /** The original graph delta data */
  data: GraphDelta;
}

/**
 * Metadata about cached data for a job.
 * Used for cache validation and incremental updates.
 */
export interface JobCacheMetadata {
  jobId: string;
  auditEntryCount: number;
  chatEntryCount: number;
  graphDeltaCount: number;
  /** Earliest timestamp in cached data */
  firstTimestamp: string | null;
  /** Latest timestamp in cached data */
  lastTimestamp: string | null;
  /** When this cache was last updated */
  cachedAt: string;
  /** Cache version for migration support */
  version: number;
}
