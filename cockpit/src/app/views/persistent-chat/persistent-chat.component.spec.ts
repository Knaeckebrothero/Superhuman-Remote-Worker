import {describe, expect, it} from 'vitest';

import {
    canComposeDuringSession,
    extractClipboardFiles,
    isStartupBannerVisible,
    pickCodeServerUrlToOpen,
    pickCurrentStartupStep,
    pickRunningCommandCard,
    readingWidthToCss,
    shouldFoldToolRun,
    textSizeToCss,
} from './persistent-chat.component';
import {AssistantTurn} from '../../core/models/turn.model';

/**
 * Build a minimal DataTransferItem stand-in. The real DataTransferItemList
 * isn't constructible in jsdom, so we hand-roll the `.kind` / `.getAsFile()`
 * surface the helper reads. A `string` kind returns null from getAsFile, like
 * the browser does.
 */
function clipItem(kind: 'file' | 'string', file: File | null): DataTransferItem {
    return {
        kind,
        type: file?.type ?? 'text/plain',
        getAsFile: () => file,
    } as unknown as DataTransferItem;
}

function itemList(items: DataTransferItem[]): DataTransferItemList {
    // Array.from() in the helper only needs index access + length.
    return items as unknown as DataTransferItemList;
}

/**
 * These cover the three decisions behind the "always-on chat + startup
 * banner" behaviour. The template wiring (banner placement, composer
 * enablement) is exercised by Playwright in the dev-cluster verification —
 * see automations-page.component.spec.ts for the same split.
 */

describe('isStartupBannerVisible', () => {
    it('shows the banner while starting once at least one turn exists', () => {
        expect(isStartupBannerVisible(true, 1)).toBe(true);
        expect(isStartupBannerVisible(true, 5)).toBe(true);
    });

    it('hides the banner on the empty screen — the centered card covers that', () => {
        // Mutually exclusive with the @empty centered startup card, so the
        // card never floats over the message list (the old .resume overlay bug).
        expect(isStartupBannerVisible(true, 0)).toBe(false);
    });

    it('hides the banner once the session is no longer starting', () => {
        expect(isStartupBannerVisible(false, 3)).toBe(false);
        expect(isStartupBannerVisible(false, 0)).toBe(false);
    });
});

describe('canComposeDuringSession', () => {
    it('allows composing while the session is starting, even before the SSE opens', () => {
        // Pre-SSE "Creating thread" window: not connected yet, but the user
        // can type and the message is queued + auto-sent on session.state.
        expect(canComposeDuringSession(false, true)).toBe(true);
    });

    it('allows composing once connected', () => {
        expect(canComposeDuringSession(true, false)).toBe(true);
    });

    it('blocks composing when neither connected nor starting (mid-session reconnect)', () => {
        // markSessionReady() only flushes the pending queue once, so we must
        // NOT let a message be queued during a reconnect — it would never send.
        expect(canComposeDuringSession(false, false)).toBe(false);
    });
});

describe('pickCurrentStartupStep', () => {
    const steps = [
        {key: 'creating', state: 'done' as const, elapsedMs: 3200},
        {key: 'provisioning', state: 'done' as const, elapsedMs: 6100},
        {key: 'booting', state: 'active' as const, elapsedMs: 17000},
        {key: 'connecting', state: 'todo' as const, elapsedMs: null},
    ];

    it('returns the active step', () => {
        expect(pickCurrentStartupStep(steps)?.key).toBe('booting');
    });

    it('falls back to the last step when none is active', () => {
        const allDone = steps.map((s) => ({...s, state: 'done' as const}));
        expect(pickCurrentStartupStep(allDone)?.key).toBe('connecting');
    });

    it('returns null for an empty list', () => {
        expect(pickCurrentStartupStep([])).toBeNull();
    });
});

describe('pickRunningCommandCard', () => {
    const rt = {id: 'tc1', tool: 'run_command', args: {command: 'sleep 99'}};

    it('returns null when nothing is running', () => {
        expect(pickRunningCommandCard(null, [])).toBeNull();
    });

    it('surfaces the running tool when it is not yet in any turn (cold reattach mid-turn)', () => {
        // Cold reload mid-turn: REST history lacks the in-flight turn, so the
        // only signal is the welcome frame's running_tool — show the card.
        expect(pickRunningCommandCard(rt, [])).toEqual(rt);
    });

    it('suppresses the card when the tool call is already in a visible turn (warm reconnect)', () => {
        // Warm reconnect: the turn (with its own tool card) is still on screen,
        // so the standalone card would double-render — suppress it.
        const turn: AssistantTurn = {
            kind: 'assistant',
            id: 't1',
            status: 'streaming',
            startedAt: 0,
            events: [
                {
                    kind: 'tool_call',
                    id: 'tc1',
                    tool: 'run_command',
                    args: {},
                    status: 'running',
                    startedAt: 0,
                },
            ],
        };
        expect(pickRunningCommandCard(rt, [turn])).toBeNull();
    });
});

describe('pickCodeServerUrlToOpen', () => {
    // The pre-flight-then-open wiring (HTTP through the auth interceptor,
    // window.open, idle-session 401 → re-login) is exercised by Playwright on
    // the dev cluster; here we cover the pure open/skip decision.
    it('returns the code-server URL when the workspace is active', () => {
        expect(
            pickCodeServerUrlToOpen({status: 'active', code_server_url: 'https://api/x/proxy/'}),
        ).toBe('https://api/x/proxy/');
    });

    it('returns null when the status is not active (e.g. restoring)', () => {
        expect(
            pickCodeServerUrlToOpen({status: 'restoring', code_server_url: 'https://api/x/proxy/'}),
        ).toBeNull();
    });

    it('returns null when active but no URL is present', () => {
        expect(pickCodeServerUrlToOpen({status: 'active'})).toBeNull();
    });

    it('returns null when the pre-flight fetch yielded null (swallowed 401 → interceptor re-login)', () => {
        // getThreadIdeStatus swallows a 401 to null *after* the auth interceptor
        // has kicked off re-login; the button must not open a tab in that case.
        expect(pickCodeServerUrlToOpen(null)).toBeNull();
    });
});

describe('extractClipboardFiles', () => {
    // The (paste) wiring (preventDefault on file pastes, createFilePreviews →
    // addAttachments) is exercised by Playwright on the dev cluster; here we
    // cover the pure clipboard-items → File[] decision.
    const NOW = 1700000000000;

    it('returns [] for a plain-text paste so the default textarea paste runs', () => {
        const items = itemList([clipItem('string', null), clipItem('string', null)]);
        expect(extractClipboardFiles(items, NOW)).toEqual([]);
    });

    it('returns [] for null/undefined clipboard items', () => {
        expect(extractClipboardFiles(null, NOW)).toEqual([]);
        expect(extractClipboardFiles(undefined, NOW)).toEqual([]);
    });

    it('keeps a named file item verbatim', () => {
        const f = new File(['x'], 'report.pdf', {type: 'application/pdf'});
        const out = extractClipboardFiles(itemList([clipItem('file', f)]), NOW);
        expect(out).toHaveLength(1);
        expect(out[0]).toBe(f); // same File instance — no needless re-wrap
        expect(out[0].name).toBe('report.pdf');
    });

    it('synthesizes a stable name + extension for a nameless clipboard blob (screenshot paste)', () => {
        const blob = new File([new Uint8Array([1, 2, 3])], '', {type: 'image/png'});
        const out = extractClipboardFiles(itemList([clipItem('file', blob)]), NOW);
        expect(out).toHaveLength(1);
        expect(out[0].name).toBe(`pasted-${NOW}-0.png`);
        expect(out[0].type).toBe('image/png');
    });

    it('drops the string items and keeps only the file when a paste carries both', () => {
        // Copying an image from a webpage yields an image/png file item AND a
        // text/html string item — we want only the image.
        const img = new File(['x'], 'image.png', {type: 'image/png'});
        const out = extractClipboardFiles(
            itemList([clipItem('string', null), clipItem('file', img)]),
            NOW,
        );
        expect(out).toEqual([img]);
    });

    it('gives each nameless blob a collision-free index in a multi-image paste', () => {
        const a = new File(['a'], '', {type: 'image/png'});
        const b = new File(['b'], '', {type: 'image/jpeg'});
        const out = extractClipboardFiles(itemList([clipItem('file', a), clipItem('file', b)]), NOW);
        expect(out.map((f) => f.name)).toEqual([`pasted-${NOW}-0.png`, `pasted-${NOW}-1.jpeg`]);
    });

    it('falls back to a .bin extension when the blob has no MIME type', () => {
        const blob = new File(['x'], '', {type: ''});
        const out = extractClipboardFiles(itemList([clipItem('file', blob)]), NOW);
        expect(out[0].name).toBe(`pasted-${NOW}-0.bin`);
    });
});

describe('shouldFoldToolRun', () => {
    const T = 4; // TOOL_GROUP_THRESHOLD

    it('folds a long run when the Tool calls preference is Collapsed (default)', () => {
        expect(shouldFoldToolRun(4, false, T)).toBe(true);
        expect(shouldFoldToolRun(10, false, T)).toBe(true);
    });

    it('keeps a short run inline regardless of preference', () => {
        expect(shouldFoldToolRun(1, false, T)).toBe(false);
        expect(shouldFoldToolRun(3, false, T)).toBe(false);
        expect(shouldFoldToolRun(3, true, T)).toBe(false);
    });

    it('never folds when Tool calls is Expanded — every run renders inline', () => {
        // The whole point of the "inline" setting: no fold control, even for 50 calls.
        expect(shouldFoldToolRun(4, true, T)).toBe(false);
        expect(shouldFoldToolRun(50, true, T)).toBe(false);
    });

    it('treats the threshold as inclusive (>=); one below stays inline', () => {
        expect(shouldFoldToolRun(T, false, T)).toBe(true);
        expect(shouldFoldToolRun(T - 1, false, T)).toBe(false);
    });
});

describe('readingWidthToCss', () => {
    it('maps comfortable → the 700px reading column', () => {
        expect(readingWidthToCss('comfortable')).toBe('700px');
    });

    it('maps wide → 900px', () => {
        expect(readingWidthToCss('wide')).toBe('900px');
    });

    it('maps full → none (no cap = full-bleed)', () => {
        expect(readingWidthToCss('full')).toBe('none');
    });
});

describe('textSizeToCss', () => {
    it('maps small → 13px', () => {
        expect(textSizeToCss('small')).toBe('13px');
    });

    it('maps medium → the 15px default', () => {
        expect(textSizeToCss('medium')).toBe('15px');
    });

    it('maps large → 17px', () => {
        expect(textSizeToCss('large')).toBe('17px');
    });
});
