import { existsSync, readFileSync } from 'node:fs';
import type { APIRequestContext } from '@playwright/test';
import { requireJson } from './api';
import { privateOutputPath, writePrivateJsonFile } from './environment';

const THREAD_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// Normal graceful retirement gets the full documented cleanup budget. Force
// is an exact-id fallback only after that window, never an early fast path.
const CLEANUP_WINDOW_MS = 180_000;
const MUTATION_HEADERS = { 'X-CSRF': '1' };

interface ThreadResource {
  kind: 'thread';
  id: string;
  created_at: string;
}

interface PersistedLedger {
  schema: 1;
  run_id: string;
  resources: ThreadResource[];
  finalized: boolean;
  cleanup_complete: boolean;
}

export interface ThreadEvidence {
  id: string;
  metadata_status: number;
  status?: string | null;
  total_turns?: number | null;
  execution_lane?: string | null;
  event_cursor?: number | null;
  messages_status: number;
  message_count?: number | null;
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolveDelay) => setTimeout(resolveDelay, milliseconds));
}

export class ResourceLedger {
  private readonly resources: ThreadResource[] = [];
  private finalized = false;
  private cleanupComplete = false;

  constructor(
    readonly runId: string,
    path: string,
  ) {
    this.path = privateOutputPath(path, 'APP_E2E_RESOURCE_LEDGER');
    if (existsSync(this.path)) {
      let previous: unknown;
      try {
        previous = JSON.parse(readFileSync(this.path, 'utf8')) as unknown;
      } catch {
        throw new Error(
          'Refusing to replace an unreadable prior E2E resource ledger; run diagnostics and cleanup first.',
        );
      }
      if (!persistedResourceLedgerCanBeReplaced(previous)) {
        throw new Error(
          'Refusing to replace a prior E2E resource ledger before exact resource and provider cleanup; run diagnostics and cleanup first.',
        );
      }
    }
    this.persist();
  }

  readonly path: string;

  registerThread(threadId: string): void {
    if (!THREAD_ID.test(threadId)) {
      throw new Error('Refusing to register a non-UUID thread id for destructive cleanup.');
    }
    if (this.resources.some((resource) => resource.id === threadId)) return;
    this.resources.push({ kind: 'thread', id: threadId, created_at: new Date().toISOString() });
    this.persist();
  }

  threadIds(): string[] {
    return this.resources.filter((resource) => resource.kind === 'thread').map(({ id }) => id);
  }

  finalize(): void {
    this.finalized = true;
    this.persist();
  }

  markCleanupComplete(): void {
    if (!this.finalized) {
      throw new Error('Refusing to mark an E2E resource ledger clean before registration closes.');
    }
    this.cleanupComplete = true;
    this.persist();
  }

  private persist(): void {
    const body: PersistedLedger = {
      schema: 1,
      run_id: this.runId,
      resources: this.resources,
      finalized: this.finalized,
      cleanup_complete: this.cleanupComplete,
    };
    writePrivateJsonFile(this.path, body);
  }

  async captureFailureEvidence(request: APIRequestContext): Promise<ThreadEvidence[]> {
    const evidence: ThreadEvidence[] = [];
    for (const id of this.threadIds()) {
      const metadataResponse = await request.get(
        `/api/persistent/threads/${encodeURIComponent(id)}`,
      );
      const messagesResponse = await request.get(
        `/api/persistent/threads/${encodeURIComponent(id)}/messages`,
      );
      const item: ThreadEvidence = {
        id,
        metadata_status: metadataResponse.status(),
        messages_status: messagesResponse.status(),
      };
      if (metadataResponse.ok()) {
        const metadata = (await metadataResponse.json()) as Record<string, unknown>;
        item.status = typeof metadata['status'] === 'string' ? metadata['status'] : null;
        item.total_turns =
          typeof metadata['total_turns'] === 'number' ? metadata['total_turns'] : null;
        item.execution_lane =
          typeof metadata['execution_lane'] === 'string' ? metadata['execution_lane'] : null;
        item.event_cursor =
          typeof metadata['event_cursor'] === 'number' ? metadata['event_cursor'] : null;
      }
      if (messagesResponse.ok()) {
        const messages = (await messagesResponse.json()) as Record<string, unknown>;
        item.message_count = typeof messages['total'] === 'number' ? messages['total'] : null;
      }
      evidence.push(item);
    }
    return evidence;
  }

  async cleanup(request: APIRequestContext): Promise<void> {
    const failures: string[] = [];
    for (const resource of [...this.resources].reverse()) {
      try {
        await this.deleteThread(request, resource.id);
      } catch (error) {
        failures.push(error instanceof Error ? error.message : String(error));
      }
    }
    if (failures.length > 0) {
      throw new Error(`Exact resource cleanup failed:\n${failures.join('\n')}`);
    }
  }

  private async deleteThread(request: APIRequestContext, threadId: string): Promise<void> {
    // The id comes only from this instance's persisted ledger. Force is never
    // reached through a prefix, title, list position, or broad owner sweep.
    if (!this.resources.some((resource) => resource.id === threadId)) {
      throw new Error('Refusing to clean a thread absent from this test resource ledger.');
    }

    const pathname = `/api/persistent/threads/${encodeURIComponent(threadId)}`;
    const deadline = Date.now() + CLEANUP_WINDOW_MS;
    let backoff = 250;
    let deleted = false;

    while (Date.now() < deadline) {
      const response = await request.delete(`${pathname}?permanent=true`, {
        headers: MUTATION_HEADERS,
        timeout: 20_000,
      });
      if (response.ok() || response.status() === 404) {
        deleted = true;
        break;
      }
      if (response.status() !== 409 && response.status() !== 503) {
        throw new Error(
          `Permanent delete for exact thread ${threadId} returned HTTP ${response.status()}.`,
        );
      }
      await delay(Math.min(backoff, Math.max(0, deadline - Date.now())));
      backoff = Math.min(backoff * 2, 2_000);
    }

    if (!deleted) {
      const forced = await request.delete(`${pathname}?permanent=true&force=true`, {
        headers: MUTATION_HEADERS,
        timeout: 30_000,
      });
      if (!forced.ok() && forced.status() !== 404) {
        throw new Error(
          `Bounded force delete for exact thread ${threadId} returned HTTP ${forced.status()}.`,
        );
      }
    }

    const exact = await request.get(pathname, { timeout: 15_000 });
    if (exact.status() === 404) return;
    if (exact.status() === 401 || exact.status() === 403 || exact.status() >= 500) {
      throw new Error(
        `Deletion verification for exact thread ${threadId} returned HTTP ${exact.status()}.`,
      );
    }

    const listed = await requireJson<{ threads: Array<{ id: string }> }>(
      await request.get('/api/persistent/threads', { timeout: 15_000 }),
      'thread-list deletion verification',
    );
    if (listed.threads.some(({ id }) => id === threadId)) {
      throw new Error(`Exact thread ${threadId} still exists after permanent cleanup.`);
    }
  }
}

export function persistedResourceLedgerCanBeReplaced(value: unknown): boolean {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const document = value as Record<string, unknown>;
  if (
    document['schema'] !== 1 ||
    typeof document['run_id'] !== 'string' ||
    document['run_id'].length === 0 ||
    document['finalized'] !== true ||
    document['cleanup_complete'] !== true ||
    !Array.isArray(document['resources'])
  ) {
    return false;
  }
  return document['resources'].every((resource) => {
    if (!resource || typeof resource !== 'object' || Array.isArray(resource)) return false;
    const entry = resource as Record<string, unknown>;
    return (
      entry['kind'] === 'thread' && typeof entry['id'] === 'string' && THREAD_ID.test(entry['id'])
    );
  });
}
