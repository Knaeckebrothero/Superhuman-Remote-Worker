import {BadgeTone} from '../../ui/badge/badge.component';

/**
 * Shared job-status vocabulary.
 *
 * `jobStatusTone` was copy-pasted verbatim into `job-list.component.ts` and
 * `job-review.component.ts`; the job tool card is the third consumer and the
 * point at which duplicating it again stops being defensible. Both call sites
 * now delegate here.
 *
 * `isTerminalJobStatus` is new — no terminal-status predicate existed anywhere
 * in the frontend, and a card that polls for live status needs one to know when
 * to stop. Getting it wrong is not cosmetic: too narrow and a finished job is
 * polled forever, too broad and the card freezes mid-run.
 *
 * Design: docs/features/unified_tool_cards.md (slice 4).
 */

/**
 * Statuses from which a job never moves again on its own.
 *
 * `pending_review` is deliberately NOT terminal: the job stopped, but an
 * approval flips it to `completed`, and that transition is exactly what a
 * watching card must catch. (The same asymmetry drives the wake feature's
 * per-status dedup key — see docs/features/session_wake_on_job_completion.md.)
 *
 * `paused` is also not terminal — the dispatcher re-picks a paused job.
 */
const TERMINAL_JOB_STATUSES: ReadonlySet<string> = new Set([
    'completed',
    'failed',
    'cancelled',
]);

export function isTerminalJobStatus(status: string | null | undefined): boolean {
    return !!status && TERMINAL_JOB_STATUSES.has(status);
}

/** True while a job is still doing work (as opposed to waiting on a human). */
export function isRunningJobStatus(status: string | null | undefined): boolean {
    return status === 'created' || status === 'processing' || status === 'waiting'
        || status === 'reviewing' || status === 'paused';
}

/**
 * Coerce a job's JSONB-backed field into an object.
 *
 * `GET /api/jobs/{id}` returns `context` and `freeze_data` as raw JSON
 * **strings**, not objects — asyncpg hands JSONB back as text and the
 * orchestrator passes it through. The cockpit `Job` model nonetheless types
 * them as `Record<string, any>`, so indexing straight into one type-checks,
 * compiles, and silently yields `undefined` forever. Verified against a real
 * dev job on 2026-07-29.
 *
 * Returns null for anything that isn't a usable object.
 */
export function asRecord(value: unknown): Record<string, unknown> | null {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
        return value as Record<string, unknown>;
    }
    if (typeof value === 'string' && value.trim()) {
        try {
            const parsed: unknown = JSON.parse(value);
            if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
                return parsed as Record<string, unknown>;
            }
        } catch {
            return null;
        }
    }
    return null;
}

/** Badge tone for a job status. Single source of truth for all three surfaces. */
export function jobStatusTone(status: string): BadgeTone {
    switch (status) {
        case 'completed':
            return 'success';
        case 'processing':
        case 'pending_review':
            return 'warning';
        case 'failed':
            return 'danger';
        case 'created':
        case 'waiting':
            return 'info';
        case 'reviewing':
            return 'accent';
        case 'cancelled':
        case 'paused':
            return 'neutral';
        default:
            return 'neutral';
    }
}
