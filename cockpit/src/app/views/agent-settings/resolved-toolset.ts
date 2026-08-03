/**
 * The tool-groups read, turned into rows a settings surface can render.
 *
 * Pure functions over `SessionToolGroupsResponse`. They exist as their own
 * module because the New Session form and the live settings pane must reach
 * the SAME conclusion from the same payload — the moment each surface derives
 * its own answer we are back to two implementations of one fact, which is the
 * defect this whole series is closing.
 *
 * Two rules are load-bearing and neither is obvious from the type:
 *
 * 1. **`origin` is the only discriminator.** `agent_partial` is a measurement
 *    with `observed_at: null`, and it is the COMMON path against a fleet image
 *    that predates `GET /session/toolset` — so `observed_at ? measured :
 *    predicted` reads a live measurement as a forecast.
 * 2. **Never re-derive a reason.** `reason` is the server's sentence and is
 *    rendered verbatim. A degraded measurement carries no `backend`, so the
 *    client cannot know whether a workspace tier is involved; inventing a
 *    workspace-tier explanation there would be a confident lie at exactly the
 *    seam this task exists to make honest.
 */

import type {
    SessionToolCategory,
    SessionToolGroupsResponse,
} from '../../core/services/api.service';
import type {ToolCategoryMeta} from './agent-settings.types';

/** How much the answer can be trusted, and in which direction it is weak. */
export type ToolsetTrust =
    /** A running agent enumerated its bound tools, in full. */
    | 'measured'
    /** A running agent answered, names only — no timestamp, no workspace tier. */
    | 'measured_partial'
    /** No agent to ask. A forecast from the merged config. */
    | 'predicted'
    /** No answer at all (older orchestrator, or the request failed). */
    | 'unknown';

export interface ToolsetProvenance {
    trust: ToolsetTrust;
    /** The server's own sentence: why a forecast, or what the measurement lacks. */
    detail: string | null;
    /** Probe time. Set on `measured` only — see rule 1 above. */
    observedAt: string | null;
}

/** One rendered row: what the server said, overlaid with what the user chose. */
export interface ResolvedToolRow {
    key: string;
    /** Presentation metadata when the cockpit knows this category; else null. */
    meta: ToolCategoryMeta | null;
    /** Displayed state. `unavailable` is the server's, and the user cannot leave it. */
    state: 'on' | 'off' | 'unavailable';
    /** False ⇒ the control is inert and `reason` says why. */
    settable: boolean;
    /**
     * The server's explanation. Present on `on` too, where it explains "not
     * settable" and NOT "why off" — render it as a note, never as a denial.
     */
    reason: string | null;
    /** Which layer produced the answer: grant / backend / runtime / registry / a config layer. */
    decidedBy: string;
    /** How many tools this category holds — bound if measured, forecast if not. */
    toolCount: number;
    /** True while the row still reflects the server's answer rather than an edit. */
    pristine: boolean;
}

const TRUST_BY_ORIGIN: Record<string, ToolsetTrust> = {
    agent: 'measured',
    agent_partial: 'measured_partial',
    prediction: 'predicted',
};

/** Trusts that came off a running agent rather than out of a config merge. */
export function isMeasured(trust: ToolsetTrust): boolean {
    return trust === 'measured' || trust === 'measured_partial';
}

/**
 * Classify an answer's provenance.
 *
 * Branches on `origin` and nothing else. A response with categories but no
 * `origin` predates the three-value contract; it is treated as `predicted`,
 * because claiming a measurement we cannot substantiate is the one error mode
 * that matters here.
 */
export function toolsetProvenance(
    response: SessionToolGroupsResponse | null | undefined,
): ToolsetProvenance {
    if (!response || !response.categories) {
        return {trust: 'unknown', detail: null, observedAt: null};
    }
    const trust = TRUST_BY_ORIGIN[response.origin ?? ''] ?? 'predicted';
    const detail =
        trust === 'measured_partial'
            ? (response.degraded_reason ?? null)
            : trust === 'predicted'
              ? (response.prediction_reason ?? null)
              : null;
    return {
        trust,
        detail,
        observedAt: trust === 'measured' ? (response.observed_at ?? null) : null,
    };
}

/**
 * Order the answer's categories for display.
 *
 * The familiar ones keep their familiar order (the metadata list), then
 * everything else alphabetically. The category set is NOT fixed — `mcp`,
 * `unclassified` and any category a config can name are unioned in server-side
 * — so an unknown key is ordered and rendered rather than dropped.
 */
export function orderedCategoryKeys(
    categories: Record<string, SessionToolCategory>,
    meta: ToolCategoryMeta[],
): string[] {
    const known = meta.map((m) => m.key).filter((key) => key in categories);
    const rest = Object.keys(categories)
        .filter((key) => !known.includes(key))
        .sort();
    return [...known, ...rest];
}

/**
 * Build the rows.
 *
 * `disabled` is the user's own set of switched-off categories, which overrides
 * the server's `state` for a SETTABLE row — the control has to follow the
 * click before the next read. It can never override `unavailable`: that is the
 * server refusing, and letting a click paint over it would rebuild the
 * fail-open this task removes.
 */
export function resolvedToolRows(
    categories: Record<string, SessionToolCategory>,
    meta: ToolCategoryMeta[],
    disabled: ReadonlySet<string>,
): ResolvedToolRow[] {
    const byKey = new Map(meta.map((m) => [m.key, m]));
    return orderedCategoryKeys(categories, meta).map((key) => {
        const entry = categories[key];
        const settable = entry.settable !== false;
        const serverOn = entry.state === 'on';
        const userOn = !disabled.has(key);
        return {
            key,
            meta: byKey.get(key) ?? null,
            state: !settable ? 'unavailable' : userOn ? 'on' : 'off',
            settable,
            reason: entry.reason ?? null,
            decidedBy: entry.decided_by ?? 'unset',
            toolCount: (entry.tools ?? []).length,
            pristine: !settable || userOn === serverOn,
        };
    });
}

/**
 * The categories the server's answer says are currently ON.
 *
 * This is the baseline a settings surface diffs against, and it replaces
 * reading `tools.<c>.length` out of a merged config: the config cannot see the
 * runtime injection layer, the backend capability gate, or a tool that failed
 * to instantiate, and on a real session those three moved 28 names.
 */
export function enabledCategoryKeys(
    categories: Record<string, SessionToolCategory>,
): Set<string> {
    return new Set(
        Object.keys(categories).filter((key) => categories[key].state === 'on'),
    );
}

/**
 * The payload that turns a category ON.
 *
 * `true` for almost everything: the write boundary expands it against the
 * registry, which is why the cockpit no longer carries tool-name lists.
 *
 * `shell` is the exception and the response says so. Categories in
 * `enumerate_only` refuse `true` (a tool added to a code-execution category
 * must not reach live configs with no diff to review), so the server ships the
 * enumeration to send instead. Echoing it back freezes the membership at write
 * time, which is the property that made the rule necessary in the first place.
 *
 * Returns null when the category cannot be enabled at all — an empty
 * enumeration means every tool in it is granted by runtime code, and asking
 * for it would be a 400 saying exactly that.
 */
export function enablePolicy(
    key: string,
    enumerateOnly: Record<string, string[]> | null | undefined,
): true | {only: string[]} | null {
    const names = enumerateOnly?.[key];
    if (names === undefined) return true;
    return names.length ? {only: [...names]} : null;
}

/**
 * Turn a baseline + the user's switches into a `tools` fragment.
 *
 * Off is `[]` rather than `false`. Both are accepted everywhere and mean the
 * same thing, but `[]` is also what the agent-facing expert catalogue and the
 * eight surviving `== []` consumers already read, so it is the spelling with
 * no reader that can misread it.
 *
 * A category the server marked unsettable is never emitted, in either
 * direction: the user cannot have changed it, and writing config against a
 * grant or a workspace tier only produces a fragment the PDP will refuse.
 */
export function toolsFragment(
    keys: Iterable<string>,
    opts: {
        disabled: ReadonlySet<string>;
        baselineOn: ReadonlySet<string>;
        unsettable: ReadonlySet<string>;
        enumerateOnly: Record<string, string[]> | null | undefined;
    },
): Record<string, unknown> {
    const tools: Record<string, unknown> = {};
    for (const key of keys) {
        if (opts.unsettable.has(key)) continue;
        const wantOn = !opts.disabled.has(key);
        if (wantOn === opts.baselineOn.has(key)) continue;
        if (!wantOn) {
            tools[key] = [];
            continue;
        }
        const policy = enablePolicy(key, opts.enumerateOnly);
        if (policy !== null) tools[key] = policy;
    }
    return tools;
}

/**
 * Fallback label for a category the cockpit has no metadata for.
 *
 * `unclassified` and unknown config keys are unioned into the answer
 * server-side, and a transloco miss renders the raw key — which AOT will not
 * catch. Humanising the key is worse than a real label and much better than
 * `agentSettings.toolCategories.unclassified.label` on screen.
 */
export function humanizeCategoryKey(key: string): string {
    return key
        .split('_')
        .filter(Boolean)
        .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
        .join(' ');
}
