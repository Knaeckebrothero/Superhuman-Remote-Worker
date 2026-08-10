/**
 * Pure helpers for the outbox's upload stage.
 *
 * Lives outside persistent-chat.service.ts so the send/upload logic that
 * matters most is testable without instantiating a 4300-line service.
 * See docs/features/session_attachment_send_flow.md §5.
 */
import type {ChatAttachment} from './persistent-chat.service';

/** One file queued on an outbox item, before or during its upload. */
export interface PendingUpload {
    /** FilePreview.id — a stable key that exists before the server path does. */
    id: string;
    file: File;
    name: string;
    size: number;
    mimeType: string;
    /** Bytes sent so far; 0 until progress reporting lands (Slice 2). */
    loaded: number;
    /** Total bytes, or null when the browser cannot compute it. */
    total: number | null;
    status: 'queued' | 'uploading' | 'done' | 'failed';
    error?: string;
    /** Set once the server confirms; retries must never re-upload these. */
    resolved?: ChatAttachment;
}

/**
 * What the agent sees: the user's text plus a plain-language hint naming the
 * uploaded files. Kept identical to the pre-refactor string so existing
 * sessions and prompt expectations don't shift.
 */
export function composeAgentContent(text: string, names: readonly string[]): string {
    if (names.length === 0) return text;
    const hint = `[Attached files in uploads/: ${names.join(', ')}]`;
    return text ? `${text}\n\n${hint}` : hint;
}

/**
 * Terminal failures will never succeed on retry, so the bubble must offer a way
 * out rather than a Retry button that can only fail again:
 *   400 — too many files, or no files provided
 *   413 — a file exceeds the backend's 100MB cap
 * Everything else (409 workspace-not-ready, 502/503 transport, 0 offline) is
 * retryable and keeps the item queued.
 */
export function classifyUploadFailure(status: number): 'terminal' | 'retryable' {
    return status === 400 || status === 413 ? 'terminal' : 'retryable';
}

/** Aggregate state of one outbox item's files, for the bubble's stage line. */
export function uploadSummary(files: readonly PendingUpload[]): {
    done: number;
    total: number;
    allDone: boolean;
    firstFailed?: PendingUpload;
} {
    const done = files.filter((f) => f.status === 'done').length;
    return {
        done,
        total: files.length,
        allDone: done === files.length,
        firstFailed: files.find((f) => f.status === 'failed'),
    };
}
