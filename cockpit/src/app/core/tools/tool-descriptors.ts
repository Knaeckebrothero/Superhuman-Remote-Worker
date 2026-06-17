import {lineDiff} from '../util/line-diff';
import {
    NormalizedToolCall,
    ParamKind,
    ToolCardView,
    ToolDetail,
    ToolParam,
    ToolResult,
    ToolResultKind,
} from '../models/tool-card.model';

/**
 * Per-tool rendering knowledge — the single source of truth that replaces the
 * scattered `TOOL_LABELS` / `toolIcon` / `formatToolArgs` / `toolLabelContext`
 * / `fileEditView` (persistent chat) and `toolCategories` / `getStepBadge`
 * (debug audit) maps. A generic fallback covers unregistered tools so a new
 * tool renders sanely with zero registry work.
 *
 * Design: `docs/features/unified_tool_cards.md`.
 */

export type ToolCategory =
    | 'workspace'
    | 'shell'
    | 'git'
    | 'research'
    | 'browser'
    | 'core'
    | 'knowledge'
    | 'citation'
    | 'sql'
    | 'graph'
    | 'cloud'
    | 'communication'
    | 'delegation';

export interface ToolDescriptor {
    /** i18n key `toolCard.titles.<tool>` is tried first; this literal is the fallback. */
    title: string;
    icon: string;
    category?: ToolCategory;
    /** Inputs to surface, in order. Empty values are dropped at build time. */
    params: Array<{key: string | string[]; label: string; kind: ParamKind}>;
    /** How to render the tool's result. Omit for tools whose result is uninteresting. */
    result?: {kind: ToolResultKind; languageFrom?: 'path'};
    /** Raw value for the collapsed-row hint (clamped by the builder). */
    subtitle?: (args: Record<string, unknown>) => string;
    /** Fallback only: build params from every arg rather than a fixed list. */
    dynamicParams?: boolean;
}

/** Lines kept in a rendered diff before the "+N more" footer. */
const DIFF_LINE_CAP = 400;
/** Max characters in the collapsed-row hint. */
const SUBTITLE_MAX = 64;

// ── helpers ────────────────────────────────────────────────────────────────

function stringifyArg(v: unknown): string {
    if (v == null) return '';
    if (typeof v === 'string') return v;
    if (typeof v === 'number' || typeof v === 'boolean') return String(v);
    try {
        return JSON.stringify(v);
    } catch {
        return '';
    }
}

/** First present, non-empty arg among `key` (or the single key), stringified. */
export function pickArg(args: Record<string, unknown>, key: string | string[]): string {
    const keys = Array.isArray(key) ? key : [key];
    for (const k of keys) {
        const s = stringifyArg(args[k]);
        if (s.length > 0) return s;
    }
    return '';
}

function basename(path: string): string {
    if (!path) return '';
    const trimmed = path.replace(/\/+$/, '');
    const idx = trimmed.lastIndexOf('/');
    return idx === -1 ? trimmed : trimmed.slice(idx + 1);
}

/** Collapse to a single line and clamp to `max` chars with an ellipsis. */
export function clampLine(s: string, max = SUBTITLE_MAX): string {
    const line = s.replace(/\s+/g, ' ').trim();
    return line.length > max ? line.slice(0, max - 1).trimEnd() + '…' : line;
}

/** Prettify a snake/camel tool id into a Title-ish label (fallback titles). */
export function prettifyToolName(name: string): string {
    return name
        .replace(/_/g, ' ')
        .replace(/([a-z])([A-Z])/g, '$1 $2')
        .replace(/^./, (c) => c.toUpperCase());
}

const EXT_LANGUAGE: Record<string, string> = {
    ts: 'typescript', tsx: 'tsx', js: 'javascript', jsx: 'jsx', mjs: 'javascript',
    py: 'python', rb: 'ruby', go: 'go', rs: 'rust', java: 'java', kt: 'kotlin',
    c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', hpp: 'cpp', cs: 'csharp',
    sh: 'bash', bash: 'bash', zsh: 'bash', fish: 'bash',
    json: 'json', yaml: 'yaml', yml: 'yaml', toml: 'toml', ini: 'ini',
    md: 'markdown', html: 'html', htm: 'html', css: 'css', scss: 'scss',
    sql: 'sql', xml: 'xml', php: 'php', swift: 'swift', dockerfile: 'docker',
};

export function languageFromPath(path: string): string | undefined {
    const name = basename(path).toLowerCase();
    if (!name) return undefined;
    if (name === 'dockerfile') return 'docker';
    const dot = name.lastIndexOf('.');
    if (dot === -1) return undefined;
    return EXT_LANGUAGE[name.slice(dot + 1)];
}

function formatDuration(ms: number): string {
    return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)}s`;
}

function inferParamKind(value: unknown): ParamKind {
    if (value != null && typeof value === 'object') return 'json';
    if (typeof value === 'string' && (value.includes('\n') || value.length > 80)) return 'code';
    return 'text';
}

// ── registry ─────────────────────────────────────────────────────────────────

const pathParam = {key: ['path', 'file_path', 'file'], label: 'Path', kind: 'path' as const};
const fileSubtitle = (a: Record<string, unknown>) => basename(pickArg(a, ['path', 'file_path', 'file']));

export const TOOL_DESCRIPTORS: Record<string, ToolDescriptor> = {
    // Workspace — files
    read_file: {title: 'Read file', icon: 'description', category: 'workspace', params: [pathParam], result: {kind: 'code', languageFrom: 'path'}, subtitle: fileSubtitle},
    write_file: {title: 'Write file', icon: 'note_add', category: 'workspace', params: [pathParam], result: {kind: 'diff'}, subtitle: fileSubtitle},
    edit_file: {title: 'Edit file', icon: 'edit', category: 'workspace', params: [pathParam], result: {kind: 'diff'}, subtitle: fileSubtitle},
    list_files: {title: 'List files', icon: 'folder_open', category: 'workspace', params: [{key: ['path', 'pattern'], label: 'Path', kind: 'path'}], result: {kind: 'text'}, subtitle: (a) => pickArg(a, ['path', 'pattern'])},
    search_files: {title: 'Search files', icon: 'search', category: 'workspace', params: [{key: ['pattern', 'query'], label: 'Pattern', kind: 'text'}, pathParam], result: {kind: 'text'}, subtitle: (a) => pickArg(a, ['pattern', 'query'])},
    file_exists: {title: 'Check file', icon: 'help_outline', category: 'workspace', params: [pathParam], subtitle: fileSubtitle},
    delete_file: {title: 'Delete file', icon: 'delete', category: 'workspace', params: [pathParam], subtitle: fileSubtitle},
    move_file: {title: 'Move file', icon: 'drive_file_move', category: 'workspace', params: [{key: 'source', label: 'From', kind: 'path'}, {key: 'destination', label: 'To', kind: 'path'}]},
    get_document_info: {title: 'Inspect document', icon: 'description', category: 'workspace', params: [pathParam], result: {kind: 'json'}, subtitle: fileSubtitle},

    // Shell
    run_command: {title: 'Execute command', icon: 'terminal', category: 'shell', params: [{key: 'command', label: 'Command', kind: 'code'}], result: {kind: 'terminal'}, subtitle: (a) => pickArg(a, ['command', 'description'])},
    shell_execute: {title: 'Execute command', icon: 'terminal', category: 'shell', params: [{key: 'command', label: 'Command', kind: 'code'}], result: {kind: 'terminal'}, subtitle: (a) => pickArg(a, ['command', 'description'])},
    shell_read: {title: 'Read output', icon: 'terminal', category: 'shell', params: [], result: {kind: 'terminal'}},

    // Git
    git_log: {title: 'View git history', icon: 'history', category: 'git', params: [], result: {kind: 'terminal'}},
    git_show: {title: 'View commit', icon: 'history', category: 'git', params: [{key: 'ref', label: 'Ref', kind: 'text'}], result: {kind: 'terminal'}},
    // git_diff output is already-formatted unified-diff text (not arg-derived
    // like edit/write), so render it as terminal text rather than 'diff'.
    git_diff: {title: 'Compare changes', icon: 'difference', category: 'git', params: [], result: {kind: 'terminal'}},
    git_status: {title: 'Check git status', icon: 'history', category: 'git', params: [], result: {kind: 'terminal'}},

    // Research & web
    web_search: {title: 'Search the web', icon: 'public', category: 'research', params: [{key: ['query', 'q'], label: 'Query', kind: 'text'}], result: {kind: 'markdown'}, subtitle: (a) => pickArg(a, ['query', 'q'])},
    extract_webpage: {title: 'Extract page', icon: 'public', category: 'research', params: [{key: 'url', label: 'URL', kind: 'text'}], result: {kind: 'markdown'}, subtitle: (a) => pickArg(a, 'url')},
    browse_website: {title: 'Browse website', icon: 'public', category: 'research', params: [{key: 'url', label: 'URL', kind: 'text'}], result: {kind: 'markdown'}, subtitle: (a) => pickArg(a, 'url')},
    search_papers: {title: 'Search papers', icon: 'menu_book', category: 'research', params: [{key: 'query', label: 'Query', kind: 'text'}], result: {kind: 'text'}, subtitle: (a) => pickArg(a, 'query')},

    // Knowledge base
    kb_write: {title: 'Write to knowledge base', icon: 'lightbulb', category: 'knowledge', params: [{key: ['title', 'key'], label: 'Title', kind: 'text'}, {key: ['content', 'value'], label: 'Content', kind: 'text'}], subtitle: (a) => pickArg(a, ['title', 'key'])},
    kb_read: {title: 'Read knowledge', icon: 'lightbulb', category: 'knowledge', params: [{key: ['key', 'query'], label: 'Key', kind: 'text'}], result: {kind: 'markdown'}, subtitle: (a) => pickArg(a, ['key', 'query'])},
    kb_search: {title: 'Search knowledge', icon: 'lightbulb', category: 'knowledge', params: [{key: 'query', label: 'Query', kind: 'text'}], result: {kind: 'text'}, subtitle: (a) => pickArg(a, 'query')},

    // Tasks & todos
    next_phase_todos: {title: 'Plan next phase', icon: 'checklist', category: 'core', params: [], result: {kind: 'json'}},
    todo_complete: {title: 'Complete todo', icon: 'task_alt', category: 'core', params: [{key: ['id', 'todo_id'], label: 'Todo', kind: 'text'}]},

    // Citation
    cite_document: {title: 'Cite document', icon: 'format_quote', category: 'citation', params: [pathParam], subtitle: fileSubtitle},

    // Communication & delegation
    send_message: {title: 'Send message', icon: 'send', category: 'communication', params: [{key: ['message', 'content'], label: 'Message', kind: 'text'}], subtitle: (a) => pickArg(a, ['message', 'content'])},
    delegate_work: {title: 'Delegate work', icon: 'group_work', category: 'delegation', params: [{key: ['task', 'description'], label: 'Task', kind: 'text'}], subtitle: (a) => pickArg(a, ['task', 'description'])},

    // Job lifecycle
    job_complete: {title: 'Complete job', icon: 'check_circle', category: 'core', params: []},
    mark_complete: {title: 'Mark complete', icon: 'check_circle', category: 'core', params: []},
};

/** Heuristic icon for an unregistered tool (mirrors the old `toolIcon`). */
function fallbackIcon(name: string): string {
    const t = name.toLowerCase();
    if (/(read|write|edit|list_files)/.test(t)) return 'description';
    if (t.includes('git')) return 'history';
    if (/(bash|shell|run_command|exec)/.test(t)) return 'terminal';
    if (/(web|search|fetch|crawl)/.test(t)) return 'public';
    if (t.includes('grep')) return 'search';
    return 'build';
}

/** Resolve a descriptor for `tool`, synthesizing a generic one when unregistered. */
export function resolveToolDescriptor(tool: string): ToolDescriptor {
    const known = TOOL_DESCRIPTORS[tool];
    if (known) return known;
    return {
        title: prettifyToolName(tool),
        icon: fallbackIcon(tool),
        params: [],
        result: {kind: 'text'},
        dynamicParams: true,
    };
}

// ── builder ──────────────────────────────────────────────────────────────────

function buildParams(d: ToolDescriptor, args: Record<string, unknown>): ToolParam[] {
    if (d.dynamicParams) {
        return Object.entries(args)
            .map(([k, v]) => ({label: prettifyToolName(k), value: stringifyArg(v), kind: inferParamKind(v)}))
            .filter((p) => p.value.length > 0);
    }
    return d.params
        .map((p) => ({label: p.label, value: pickArg(args, p.key), kind: p.kind}))
        .filter((p) => p.value.length > 0);
}

/** Port of the old `fileEditView`: a diff for write/edit, built from args alone. */
function buildDiff(tool: string, args: Record<string, unknown>): ToolResult | null {
    const str = (k: string): string => (typeof args[k] === 'string' ? (args[k] as string) : '');
    let mode: ToolResult['diffMode'];
    let lines;
    if (tool === 'write_file') {
        mode = 'write';
        lines = lineDiff('', str('content'));
    } else if (tool === 'edit_file') {
        const position = str('position');
        if (position === 'end') {
            mode = 'append';
            lines = lineDiff('', str('new_string'));
        } else if (position === 'start') {
            mode = 'prepend';
            lines = lineDiff('', str('new_string'));
        } else {
            mode = 'replace';
            lines = lineDiff(str('old_string'), str('new_string'));
        }
    } else {
        return null;
    }
    if (lines.length === 0) return null;
    const truncatedLines = Math.max(0, lines.length - DIFF_LINE_CAP);
    return {kind: 'diff', diffMode: mode, diffLines: lines.slice(0, DIFF_LINE_CAP), truncatedLines};
}

function buildResult(
    d: ToolDescriptor,
    n: NormalizedToolCall,
    args: Record<string, unknown>,
): ToolResult | undefined {
    // Diff tools render from args even before any result row arrives — but a
    // failed call shows its error output instead of a diff that never applied.
    if (d.result?.kind === 'diff' && n.status !== 'error') {
        const diff = buildDiff(n.tool, args);
        if (diff) return diff;
    }
    const content = n.result;
    if (content == null || content === '') return undefined;
    const kind = d.result && d.result.kind !== 'diff' ? d.result.kind : 'text';
    const language =
        d.result?.languageFrom === 'path'
            ? languageFromPath(pickArg(args, ['path', 'file_path', 'file']))
            : undefined;
    return {
        kind,
        content: String(content),
        language,
        bytesTotal: n.resultBytesTotal ?? undefined,
    };
}

function buildDetails(n: NormalizedToolCall): ToolDetail[] {
    const out: ToolDetail[] = [];
    if (n.durationMs != null && n.durationMs > 0) {
        out.push({label: 'Duration', value: formatDuration(n.durationMs)});
    }
    if (n.exitCode != null) {
        out.push({label: 'Exit code', value: String(n.exitCode), tone: n.exitCode === 0 ? 'ok' : 'error'});
    }
    // The total size is surfaced by the card's "preview · N total" note (from
    // `result.bytesTotal`), so it isn't duplicated as a detail row here.
    return out;
}

/**
 * Build the render-ready {@link ToolCardView} from a normalized call. Pure and
 * transloco-free (the component re-resolves the title via i18n with this
 * literal as fallback), so it is unit-tested without a component render.
 */
export function buildToolCardView(n: NormalizedToolCall): ToolCardView {
    const d = resolveToolDescriptor(n.tool);
    const args = n.args || {};
    const result = buildResult(d, n, args);
    const subtitle = d.subtitle ? clampLine(d.subtitle(args)) : '';
    return {
        tool: n.tool,
        title: d.title,
        icon: d.icon,
        subtitle: subtitle || undefined,
        status: n.status,
        params: buildParams(d, args),
        result,
        details: buildDetails(n),
        error: n.error ? String(n.error) : undefined,
    };
}
