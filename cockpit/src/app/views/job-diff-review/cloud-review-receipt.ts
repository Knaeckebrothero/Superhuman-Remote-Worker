/**
 * Client-side record of the last protected-cloud review decision.
 *
 * ## What this is, and what it deliberately is not
 *
 * PC-20: a four-path Apply took 34.4 s; the inactivity lifecycle ended the
 * session mid-flight; the backend returned 200 with `applied:3, deleted:1`;
 * the owner saw neither a success toast nor an error and could not determine
 * the outcome from Cockpit at all. The current success signal is a
 * four-second client-side toast.
 *
 * The orchestrator does not persist an operation record.
 * `apply_staged_diff` / `reject_staged_diff`
 * (orchestrator/services/cloud_staging/apply.py:288, 337) return their result
 * and log a line; no session event, no thread message, no audit row. So there
 * is nothing durable for the cockpit to read back.
 *
 * This module therefore stores the response **in the reviewer's browser** so
 * the outcome survives a reload and a re-open of the review surface, and the
 * UI labels it as such. It is NOT a durable receipt:
 *
 *   - it does not exist on another device or another browser;
 *   - it does not exist for a client that never received the response —
 *     which is exactly the PC-20 scenario;
 *   - it is not authoritative, and nothing reconciles it against the server.
 *
 * PC-20 stays open until the backend persists
 * `{epoch, decision, applied, deleted, overlay_reset, at, actor}` on the
 * thread and exposes it. See
 * knowledge/features/protected_cloud_review_surface_redesign.md §8.
 */

/** One resolved decision, as observed by this browser. */
export interface CloudReviewReceipt {
  decision: 'applied' | 'rejected';
  /** The staged epoch that was resolved (the value pinned in the request,
   *  not the post-resolution epoch the backend returns). Null in job
   *  context, which has no epoch — displaying a fabricated 0 there was one
   *  of the receipt's context errors. */
  epoch: number | null;
  applied: number;
  deleted: number;
  /** The backend's `overlay_reset`. False means the cloud write landed but
   *  the agent's capture overlay still holds the same content, so a resume
   *  can re-stage it — PC-07's documented duplicate-diff edge. */
  overlayReset: boolean;
  /** ISO timestamp, client clock. */
  at: string;
}

const PREFIX = 'srw:cloud-review-receipt:';
/** Bump when the shape changes so an old record is dropped, not misread. */
const VERSION = 1;

/**
 * How long a browser-local receipt stays useful.
 *
 * These accumulate one key per reviewed thread and nothing else ever deletes
 * them, so without an expiry a heavy user's localStorage grows without bound
 * and a months-old record can still be re-displayed as "the last result" for
 * a thread whose state has long since moved on. A week comfortably covers
 * "I came back to check what happened" and nothing beyond it.
 */
const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

function keyFor(threadId: string): string {
  return `${PREFIX}${threadId}`;
}

function isExpired(receipt: CloudReviewReceipt, now: number): boolean {
  const at = Date.parse(receipt.at);
  if (!Number.isFinite(at)) return true; // unparseable clock stamp: not trustworthy
  return now - at > MAX_AGE_MS;
}

function parse(raw: string | null): CloudReviewReceipt | null {
  if (!raw) return null;
  const parsed = JSON.parse(raw) as { v?: number; receipt?: CloudReviewReceipt };
  if (parsed?.v !== VERSION || !parsed.receipt) return null;
  const r = parsed.receipt;
  if (r.decision !== 'applied' && r.decision !== 'rejected') return null;
  return r;
}

/**
 * Every accessor is try/caught: localStorage throws outright in a Safari
 * private window and in any context with site data blocked, and a receipt is
 * a convenience — it must never be able to break the review surface.
 */
export function readReceipt(threadId: string | null): CloudReviewReceipt | null {
  if (!threadId) return null;
  try {
    const r = parse(localStorage.getItem(keyFor(threadId)));
    if (!r) return null;
    if (isExpired(r, Date.now())) {
      localStorage.removeItem(keyFor(threadId));
      return null;
    }
    return r;
  } catch {
    return null;
  }
}

/**
 * Persist a receipt for `threadId`, returning whether it actually reached
 * storage.
 *
 * The return value is load-bearing, not informational: the surface labels the
 * receipt "recorded in this browser only", and that sentence is false in job
 * context (no thread id, nothing written) and in a private window (the write
 * throws). The caller shows the provenance line only when this returns true.
 */
export function writeReceipt(threadId: string | null, receipt: CloudReviewReceipt): boolean {
  if (!threadId) return false;
  try {
    localStorage.setItem(keyFor(threadId), JSON.stringify({ v: VERSION, receipt }));
    pruneExpiredReceipts();
    return true;
  } catch {
    // Quota, private mode, or storage disabled. The in-memory signal still
    // shows the receipt for this page view; only reload-survival is lost.
    return false;
  }
}

export function clearReceipt(threadId: string | null): void {
  if (!threadId) return;
  try {
    localStorage.removeItem(keyFor(threadId));
  } catch {
    // ignore
  }
}

/**
 * Drop every expired or unreadable receipt. Called after each successful
 * write — the only moment we know storage is reachable and the only moment
 * the collection grows.
 */
export function pruneExpiredReceipts(now = Date.now()): number {
  let dropped = 0;
  try {
    const stale: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key || !key.startsWith(PREFIX)) continue;
      let receipt: CloudReviewReceipt | null = null;
      try {
        receipt = parse(localStorage.getItem(key));
      } catch {
        receipt = null; // corrupt record: same treatment as an expired one
      }
      if (!receipt || isExpired(receipt, now)) stale.push(key);
    }
    for (const key of stale) {
      localStorage.removeItem(key);
      dropped++;
    }
  } catch {
    // ignore
  }
  return dropped;
}

/**
 * Whether a stored receipt is still worth showing against a freshly loaded
 * summary.
 *
 * A receipt describes the resolution of one epoch. If the thread has since
 * staged a *newer* epoch, the old receipt is history, not the current state,
 * and showing it above a live pending diff would be actively misleading. An
 * equal epoch with nothing staged means "this is the decision you just made"
 * and is exactly what should be shown.
 */
export function receiptAppliesTo(
  receipt: CloudReviewReceipt | null,
  summaryEpoch: number | null,
  pendingFileCount: number,
): boolean {
  if (!receipt) return false;
  if (pendingFileCount > 0) return false;
  if (summaryEpoch == null) return true;
  // A receipt with no epoch cannot be compared against one — refuse to claim
  // it is current rather than guessing.
  if (receipt.epoch == null) return false;
  return summaryEpoch <= receipt.epoch + 1;
}
