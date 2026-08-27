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
    | {kind: 'not_ended'}
    | {kind: 'error'; status: number};

/** Classify a failed resume.
 *
 *  The typed ``session_not_ended`` 409 is distinct, but not proof of a
 *  successor life: the agent can journal ``session.ended`` before the
 *  orchestrator has entered/settled retirement. The caller keeps its terminal
 *  review plane latched and asks the owner to retry. Other 409s include
 *  protected-cloud class/tier refusals and remain ordinary visible errors.
 *
 *  428 carries the drift list under `error.detail.drift` — FastAPI wraps the
 *  raised `HTTPException(428, detail={...})` payload under a `detail` key, so
 *  the Angular HttpErrorResponse body is `{detail: {code, detail, drift,
 *  summary}}`. Reading `error.drift` directly (skipping that wrapper) would
 *  silently yield undefined and an empty dialog.
 */
export function classifyResumeError(err: unknown): ResumeOutcome {
    const status = (err as {status?: number})?.status ?? 0;
    const code = (err as {error?: {detail?: {code?: unknown}}})?.error?.detail?.code;
    if (status === 409 && code === 'session_not_ended') return {kind: 'not_ended'};
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
