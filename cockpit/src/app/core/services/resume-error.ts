/** Outcome classification for POST /resume.
 *
 *  This exists as a pure function because PersistentChatComponent cannot be
 *  mounted in specs (NG0951), so the decision has to be testable on its own.
 */

export interface ConfigDriftItem {
    id: string;
    kind: 'connector' | 'project' | 'grant';
    reason: 'deleted' | 'revoked' | 'out_of_scope';
    label: string;
}

export type ResumeOutcome =
    | {kind: 'ok'}
    | {kind: 'drift'; items: ConfigDriftItem[]}
    | {kind: 'benign'}
    | {kind: 'error'; status: number};

/** Classify a failed resume.
 *
 *  409 stays benign: it means the thread was not actually 'ended' (a
 *  double-click), and connect()'s cold path is self-healing. Everything else
 *  used to be swallowed by the same catch, which is why a 403 rendered as a
 *  dead button with no message at all.
 *
 *  428 carries the drift list under `error.detail.drift` — FastAPI wraps the
 *  raised `HTTPException(428, detail={...})` payload under a `detail` key, so
 *  the Angular HttpErrorResponse body is `{detail: {code, detail, drift,
 *  summary}}`. Reading `error.drift` directly (skipping that wrapper) would
 *  silently yield undefined and an empty dialog.
 */
export function classifyResumeError(err: unknown): ResumeOutcome {
    const status = (err as {status?: number})?.status ?? 0;
    if (status === 409) return {kind: 'benign'};
    if (status === 428) {
        const detail = (err as {error?: {detail?: {drift?: ConfigDriftItem[]}}})
            ?.error?.detail;
        const items = detail?.drift;
        if (Array.isArray(items) && items.length > 0) {
            return {kind: 'drift', items};
        }
        return {kind: 'error', status: 428};
    }
    return {kind: 'error', status};
}
