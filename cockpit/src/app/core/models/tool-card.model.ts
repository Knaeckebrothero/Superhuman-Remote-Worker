import {DiffLine} from '../util/line-diff';

/**
 * Unified, source-agnostic view-model for an agent tool call.
 *
 * Both the live persistent-session stream (`ToolCallEvent`) and the debug
 * MongoDB audit trail (`AuditToolInfo`) map into this one shape, which a single
 * presentational component (`<app-tool-card>`) renders identically. All
 * per-tool knowledge (title, icon, which arg is the "primary" parameter, how to
 * render the result) lives in the descriptor registry — see
 * `core/tools/tool-descriptors.ts`. Design: `docs/features/unified_tool_cards.md`.
 */

export type ToolCardStatus = 'pending' | 'running' | 'ok' | 'error' | 'denied';

/** How a result/param body should be presented. */
export type ToolResultKind =
    | 'text'
    | 'code'
    | 'terminal'
    | 'diff'
    | 'json'
    | 'markdown'
    | 'none';

export type ParamKind = 'code' | 'path' | 'text' | 'json';

/** A single input parameter, shown full (never truncated) in the expanded body. */
export interface ToolParam {
    label: string;
    value: string;
    kind: ParamKind;
}

/** A small meta fact (duration, exit code, size). Omitted as a group when empty. */
export interface ToolDetail {
    label: string;
    value: string;
    tone?: 'default' | 'ok' | 'error' | 'warn';
}

/** The result the model saw, rendered per `kind`. */
export interface ToolResult {
    kind: ToolResultKind;
    /** Plain content for text/code/terminal/json/markdown kinds. */
    content?: string;
    /** Highlight hint for `code`, derived from a file extension when available. */
    language?: string;
    /** Diff lines for `kind === 'diff'`. */
    diffLines?: DiffLine[];
    diffMode?: 'replace' | 'append' | 'prepend' | 'write';
    /** Lines dropped by the adapter's render cap (diff), surfaced as a footer. */
    truncatedLines?: number;
    /** Set when `content` is only a preview of a larger result (debug side). */
    bytesTotal?: number;
}

/**
 * The complete, render-ready description of one tool call. Produced by
 * `buildToolCardView()` from a `NormalizedToolCall`; consumed by
 * `<app-tool-card [view]>`.
 */
export interface ToolCardView {
    /** Raw tool id, e.g. `run_command`. Shown small/mono in the body. */
    tool: string;
    /** Human verb, e.g. "Execute command". May be an i18n key (literal fallback). */
    title: string;
    /** Material-symbol icon name. */
    icon: string;
    /** Short, single-line, ellipsized collapsed-row hint. May be empty. */
    subtitle?: string;
    status: ToolCardStatus;
    params: ToolParam[];
    result?: ToolResult;
    details: ToolDetail[];
    /** Explicit error message (audit) or the errored output (live). */
    error?: string;
}

/**
 * Minimal common shape each data source normalizes into before
 * `buildToolCardView()` applies descriptor-driven rendering. Keeps all tool
 * semantics in one place; the per-source adapters only translate field names
 * and map status.
 */
export interface NormalizedToolCall {
    tool: string;
    args: Record<string, unknown>;
    status: ToolCardStatus;
    /** Content the model saw. May be a preview (see `resultBytesTotal`). */
    result?: string | null;
    resultBytesTotal?: number | null;
    error?: string | null;
    durationMs?: number | null;
    exitCode?: number | null;
}
