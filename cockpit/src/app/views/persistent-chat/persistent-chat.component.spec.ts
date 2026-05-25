import {describe, expect, it} from 'vitest';

import {
    canComposeDuringSession,
    isStartupBannerVisible,
    pickCurrentStartupStep,
    pickRunningCommandCard,
} from './persistent-chat.component';
import {AssistantTurn} from '../../core/models/turn.model';

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
