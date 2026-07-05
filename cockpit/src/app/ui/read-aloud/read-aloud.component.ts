import {
    ChangeDetectionStrategy,
    Component,
    OnDestroy,
    computed,
    inject,
    input,
    signal,
} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';
import {firstValueFrom} from 'rxjs';
import {AppIconComponent} from '../icon';
import {ApiService} from '../../core/services/api.service';
import {I18nService} from '../../core/services/i18n.service';
import {ChatPreferencesService} from '../../core/services/chat-preferences.service';
import {VoiceCapabilitiesService} from '../../core/services/voice-capabilities.service';
import {ReadAloudPlaybackService, PlaybackPart} from './read-aloud-playback.service';

/** Lifecycle of one message's read-aloud. `rewriting` = the plan/clean call is
 *  in flight; `generating` = chunks known, synthesizing + playing; the rest are
 *  terminal. */
type ReadAloudPhase =
    | 'idle'
    | 'rewriting'
    | 'generating'
    | 'done'
    | 'cancelled'
    | 'error';

const MAX_SYNTH_ATTEMPTS = 3;
const ELAPSED_SHOW_AT = 10;
const REWRITE_SLOW_AT = 25;

/** Per-instance id so the playback engine can tell messages apart. */
let nextReadAloudId = 0;

/**
 * Read-aloud status box + custom player for one assistant message (Phases 1–2 of
 * the voice roadmap). Owns the generation lifecycle (plan → synthesize chunk by
 * chunk) and the transparent status box; playback is delegated to the
 * root-provided {@link ReadAloudPlaybackService}, which plays the chunks as one
 * virtual timeline through a single primed `<audio>` element (no native chrome,
 * pitch-corrected speed, Media Session, one-at-a-time across the chat).
 */
@Component({
    selector: 'app-read-aloud',
    standalone: true,
    changeDetection: ChangeDetectionStrategy.OnPush,
    imports: [TranslocoPipe, AppIconComponent],
    templateUrl: './read-aloud.component.html',
    styleUrl: './read-aloud.component.scss',
})
export class AppReadAloudComponent implements OnDestroy {
    readonly content = input.required<string>();
    readonly threadId = input<string | null>(null);

    private readonly api = inject(ApiService);
    private readonly i18n = inject(I18nService);
    private readonly prefs = inject(ChatPreferencesService);
    private readonly voiceCaps = inject(VoiceCapabilitiesService);
    readonly playback = inject(ReadAloudPlaybackService);

    private readonly id = `ra-${++nextReadAloudId}`;

    readonly phase = signal<ReadAloudPhase>('idle');
    readonly chunks = signal<string[]>([]);
    /** Synthesized parts (url + measured duration), in order. */
    readonly ready = signal<PlaybackPart[]>([]);
    /** Chunk currently being synthesized — drives "part N of M". */
    readonly synthIndex = signal(0);
    readonly rewritten = signal(true);
    readonly elapsed = signal(0);
    readonly partError = signal<number | null>(null);
    readonly retryInfo = signal<{index: number; attempt: number} | null>(null);
    readonly keptParts = signal(0);
    readonly hardError = signal<
        'rewrite' | 'synthesis' | 'empty' | 'no-thread' | 'unavailable' | 'provider' | null
    >(null);
    /** Provider failure code behind a 'provider' hardError (payment_required /
     *  auth / rate_limit) — drives a specific message instead of "synthesis
     *  failed". Set when the synth endpoint returns an actionable error. */
    readonly synthErrorCode = signal<string | null>(null);
    /** i18n key for the provider error message, chosen by code. */
    readonly providerErrorKey = computed(() => {
        switch (this.synthErrorCode()) {
            case 'payment_required': return 'chat.tts.err.paymentRequired';
            case 'auth': return 'chat.tts.err.auth';
            case 'rate_limit': return 'chat.tts.err.rateLimit';
            default: return 'chat.tts.err.generic';
        }
    });

    readonly unavailable = computed(() => this.voiceCaps.tts() === false);
    readonly total = computed(() => this.chunks().length);
    readonly hasAudio = computed(() => this.ready().length > 0);
    readonly isActive = computed(
        () => this.phase() === 'rewriting' || this.phase() === 'generating',
    );
    private readonly spoken = computed(() => this.chunks().join('\n\n'));
    readonly showSpoken = computed(
        () =>
            this.rewritten() &&
            this.chunks().length > 0 &&
            this.spoken().trim() !== this.content().trim(),
    );
    readonly spokenText = this.spoken;

    // ===== Player bindings (only meaningful while this message is active) =====
    readonly isPlaybackActive = computed(() => this.playback.isActive(this.id));
    readonly playing = computed(() => this.isPlaybackActive() && this.playback.playing());
    readonly playBlocked = computed(() => this.isPlaybackActive() && this.playback.blocked());
    /** Total duration of the synthesized parts (stable even when not active). */
    private readonly knownTotal = computed(() =>
        this.ready().reduce((a, p) => a + p.duration, 0),
    );
    /** Timeline length for the bar: the engine's estimate while active, else the
     *  measured total. */
    readonly playTotal = computed(() =>
        this.isPlaybackActive() ? this.playback.estimatedTotal() : this.knownTotal(),
    );
    readonly playCurrent = computed(() =>
        this.isPlaybackActive() ? this.playback.currentTime() : 0,
    );
    /** Played fraction (0–1) of the whole timeline. */
    readonly progressFrac = computed(() => {
        const total = this.playTotal();
        return total > 0 ? Math.min(1, this.playCurrent() / total) : 0;
    });
    /** Fraction of the bar that is *synthesized* (the rest is the estimated
     *  tail, rendered distinctly). */
    readonly knownFrac = computed(() => {
        const total = this.playTotal();
        if (total <= 0) return 0;
        const known = this.isPlaybackActive() ? this.playback.knownDuration() : this.knownTotal();
        return Math.min(1, known / total);
    });
    readonly currentLabel = computed(() => this.fmt(this.playCurrent()));
    readonly totalLabel = computed(() => {
        const prefix = this.isPlaybackActive() && !this.playback.complete() ? '~' : '';
        return prefix + this.fmt(this.playTotal());
    });
    readonly speedLabel = computed(() => `${this.prefs.playbackSpeed()}×`);
    readonly durationLabel = computed(() => (this.knownTotal() > 0 ? this.fmt(this.knownTotal()) : ''));

    private cancelRequested = false;
    private elapsedTimer: ReturnType<typeof setInterval> | null = null;
    private readonly blobUrls = new Set<string>();
    /** Aborts the in-flight rewrite SSE stream (cancel / bailout / destroy). */
    private streamAbort: AbortController | null = null;
    /** True once the chunk list is final (stream `done`, error tail, or fallback)
     *  — tells the synthesis worker to stop waiting for more chunks. */
    private streamComplete = false;
    /** Set while the "read it as-is" bailout takes over, so `start()`'s aborted
     *  stream loop exits silently instead of surfacing an error. */
    private bailedOut = false;

    // ===== Generation lifecycle =====

    async start(): Promise<void> {
        // Already running → keep the current box rather than restarting.
        if (this.phase() === 'rewriting' || this.phase() === 'generating') return;

        // Acknowledge the click IMMEDIATELY, in place — flip to the box BEFORE
        // anything that could throw (validation, reset, priming), so hitting Read
        // ALWAYS shows something. This is the core transparency guarantee; every
        // failure below becomes a visible, honest state, never a silent return.
        this.phase.set('rewriting');
        this.reset();
        this.startElapsed();
        // Prime the shared audio element inside the gesture so the first play()
        // after the async plan isn't blocked on Safari (guarded internally).
        this.playback.prime();

        // Read inputs defensively (a message with no final text ⇒ undefined).
        const threadId = this.threadId();
        const text = (this.content() ?? '').trim();

        if (this.unavailable()) {
            this.failStart('unavailable');
            return;
        }
        if (!text) {
            this.failStart('empty');
            return;
        }
        if (!threadId) {
            this.failStart('no-thread');
            return;
        }

        // Stream the rewrite: consume chunks as the aux LLM emits them, and start
        // synthesizing chunk 0 the instant it lands (rather than awaiting the whole
        // plan). The synthesis worker runs concurrently, draining `chunks` as it
        // grows and settling into 'done' once the stream is complete.
        const controller = new AbortController();
        this.streamAbort = controller;
        let firstChunkSeen = false;
        let synthStarted = false;
        let rewrittenFlag = true;
        try {
            for await (const ev of this.api.streamTTSPlan(threadId, text, controller.signal)) {
                if (this.cancelRequested || this.bailedOut) return;
                if (ev.type === 'unavailable') {
                    this.stopElapsed();
                    this.phase.set('idle');
                    return;
                }
                if (ev.type === 'chunk') {
                    this.chunks.update((c) => [...c, ev.text]);
                    if (ev.rewritten === false) rewrittenFlag = false;
                    this.rewritten.set(rewrittenFlag);
                    if (!firstChunkSeen) {
                        firstChunkSeen = true;
                        this.stopElapsed();
                        this.phase.set('generating');
                    }
                    if (!synthStarted) {
                        synthStarted = true;
                        void this.runSynthesis(0);
                    }
                } else if (ev.type === 'done') {
                    rewrittenFlag = ev.rewritten;
                    this.rewritten.set(rewrittenFlag);
                    this.streamComplete = true;
                } else if (ev.type === 'error') {
                    // Nothing produced yet → try the buffered plan as a safety net
                    // (transient SSE hiccup); mid-stream → keep what we have.
                    if (!firstChunkSeen) {
                        await this.planFallback(threadId, text);
                        return;
                    }
                    this.streamComplete = true;
                }
            }
        } catch {
            if (this.cancelRequested || this.bailedOut) return;
            if (!firstChunkSeen) {
                await this.planFallback(threadId, text);
                return;
            }
        }
        // Loop ended (with or without an explicit `done`) — the chunk list is now
        // final so the synthesis worker can finish and settle into 'done'.
        this.streamComplete = true;
        if (!firstChunkSeen && !this.cancelRequested && !this.bailedOut) {
            // Stream closed without ever yielding a chunk → nothing to read.
            this.stopElapsed();
            this.phase.set('idle');
        }
    }

    /** Buffered-plan safety net when the stream fails before producing anything
     *  (used by `start()` on an early stream error). */
    private async planFallback(threadId: string, text: string): Promise<void> {
        let plan: {chunks: string[]; rewritten: boolean} | 'unavailable' | null;
        try {
            plan = await firstValueFrom(this.api.planTTS(threadId, text));
        } catch {
            plan = null;
        }
        this.stopElapsed();
        if (this.cancelRequested || this.bailedOut) return;
        if (plan === 'unavailable') {
            this.phase.set('idle');
            return;
        }
        if (plan === null) {
            this.hardError.set('rewrite');
            this.phase.set('error');
            return;
        }
        if (!plan.chunks.length) {
            this.phase.set('idle');
            return;
        }
        this.chunks.set(plan.chunks);
        this.rewritten.set(plan.rewritten);
        this.streamComplete = true;
        this.phase.set('generating');
        await this.runSynthesis(0);
    }

    /** Synthesis worker: drains the (growing) chunk queue in order, synthesizing +
     *  handing each part to playback. Waits for the next chunk while the rewrite
     *  stream is still producing; stops once `streamComplete` and the queue is
     *  drained. Runs concurrently with the streaming loop in `start()`. */
    private async runSynthesis(startAt: number): Promise<void> {
        for (let i = startAt; ; i++) {
            // Wait for chunk `i` to arrive (stream still trickling) or for the
            // stream to finish with no more chunks.
            while (i >= this.chunks().length && !this.streamComplete) {
                if (this.cancelRequested) return;
                await this.delay(100);
            }
            if (this.cancelRequested) return;
            if (i >= this.chunks().length) break; // stream done, queue drained
            this.synthIndex.set(i);
            const part = await this.synthChunk(i);
            if (this.cancelRequested) return;
            if (part && 'errorCode' in part) {
                // Actionable provider error (e.g. this voice needs a paid plan)
                // — retrying every part is pointless, so fail the whole read with
                // a specific message rather than a generic per-part retry.
                this.synthErrorCode.set(part.errorCode);
                this.hardError.set('provider');
                this.phase.set('error');
                return;
            }
            if (!part) {
                if (i === 0) {
                    this.hardError.set('synthesis');
                    this.phase.set('error');
                } else {
                    this.partError.set(i);
                }
                return;
            }
            this.ready.update((r) => [...r, part]);
            this.pushToPlayback(i === 0);
        }
        this.partError.set(null);
        // Every chunk synthesized and the stream is done → tell playback the
        // timeline is final (collapses the estimated tail; rescues a park-at-end).
        this.playback.markComplete(this.id);
        this.phase.set('done');
    }

    /** Hand a newly-ready part to the playback engine: the first one starts the
     *  session (and autoplays), the rest append to its timeline. */
    private pushToPlayback(isFirst: boolean): void {
        const parts = this.ready();
        const remainingChars = this.chunks()
            .slice(parts.length)
            .reduce((a, c) => a + c.length, 0);
        // "complete" only once the rewrite stream is done AND every received chunk
        // is synthesized — otherwise the engine would collapse the estimated tail
        // and stop at the first chunk while more are still streaming in.
        const complete = this.streamComplete && parts.length === this.chunks().length;
        if (isFirst) {
            this.playback.start(this.id, parts, {remainingChars, complete});
        } else {
            this.playback.append(this.id, parts[parts.length - 1], {remainingChars, complete});
        }
    }

    /** Synthesize chunk `i` with bounded auto-retry; measure its duration.
     *  Returns the ready part, `null` for a transient/exhausted failure, or
     *  `{errorCode}` for an *actionable* provider error (paid-plan / auth /
     *  rate-limit) — which is NOT retried, since retrying can't fix it. */
    private async synthChunk(
        i: number,
    ): Promise<PlaybackPart | {errorCode: string} | null> {
        const chunks = this.chunks();
        if (i < 0 || i >= chunks.length) return null;
        const threadId = this.threadId();
        if (!threadId) return null;
        const lang = this.i18n.activeLang().startsWith('de') ? 'de' : 'en';

        for (let attempt = 1; attempt <= MAX_SYNTH_ATTEMPTS; attempt++) {
            if (this.cancelRequested) return null;
            if (attempt > 1) {
                this.retryInfo.set({index: i, attempt});
                await this.delay(600 * (attempt - 1));
                if (this.cancelRequested) return null;
            }
            let res: {text: string; audio: Blob} | 'unavailable' | {errorCode: string} | null;
            try {
                res = await firstValueFrom(
                    this.api.generateTTS(threadId, chunks[i], {language: lang, reformulate: false}),
                );
            } catch {
                res = null;
            }
            if (res && res !== 'unavailable' && 'audio' in res) {
                this.retryInfo.set(null);
                const url = URL.createObjectURL(res.audio);
                this.blobUrls.add(url);
                const duration = await this.probeDuration(url);
                return {url, duration};
            }
            if (res === 'unavailable') break;
            if (res && 'errorCode' in res) {
                this.retryInfo.set(null);
                return res; // actionable — stop retrying, surface it
            }
        }
        this.retryInfo.set(null);
        return null;
    }

    /** Read a blob's duration off-screen so the timeline is known before it
     *  plays (a throwaway metadata-only element). */
    private probeDuration(url: string): Promise<number> {
        return new Promise((resolve) => {
            const a = new Audio();
            a.preload = 'metadata';
            const done = (d: number) => resolve(isFinite(d) && d > 0 ? d : 0);
            a.addEventListener('loadedmetadata', () => done(a.duration), {once: true});
            a.addEventListener('error', () => done(0), {once: true});
            a.src = url;
        });
    }

    async retryPart(i: number): Promise<void> {
        if (this.phase() !== 'generating') this.phase.set('generating');
        this.partError.set(null);
        this.cancelRequested = false;
        const part = await this.synthChunk(i);
        if (part && 'errorCode' in part) {
            this.synthErrorCode.set(part.errorCode);
            this.hardError.set('provider');
            this.phase.set('error');
            return;
        }
        if (!part) {
            this.partError.set(i);
            return;
        }
        this.ready.update((r) => [...r, part]);
        this.pushToPlayback(this.ready().length === 1);
        await this.runSynthesis(i + 1);
    }

    cancel(): void {
        this.cancelRequested = true;
        this.streamAbort?.abort();
        this.stopElapsed();
        this.playback.stopIfActive(this.id);
        const kept = this.ready().length;
        if (kept === 0) {
            this.phase.set('idle');
        } else {
            this.keptParts.set(kept);
            this.phase.set('cancelled');
        }
    }

    restart(): void {
        this.start();
    }

    /** Turn the just-opened box into an honest error state (never a silent
     *  no-op). Called from start() when there's nothing to read, no session
     *  scope, or TTS is off. */
    private failStart(kind: 'unavailable' | 'empty' | 'no-thread'): void {
        this.stopElapsed();
        this.hardError.set(kind);
        this.phase.set('error');
    }

    /** Dismiss a non-retryable error box back to the Read button. */
    dismiss(): void {
        this.stopElapsed();
        this.phase.set('idle');
    }

    /** "Skip the rewrite — read it as-is." The escape hatch for when the rewrite
     *  is slow to even start: abort the stream and synthesize the markdown-
     *  stripped raw text instead. Only meaningful while still waiting for the
     *  first chunk (`rewriting`); once audio is flowing there's nothing to skip. */
    async bailout(): Promise<void> {
        if (this.phase() !== 'rewriting') return;
        this.bailedOut = true;
        this.streamAbort?.abort();
        this.stopElapsed();

        const threadId = this.threadId();
        const text = (this.content() ?? '').trim();
        if (!threadId || !text) {
            this.failStart('empty');
            return;
        }
        let plan: {chunks: string[]; rewritten: boolean} | 'unavailable' | null;
        try {
            plan = await firstValueFrom(this.api.planTTS(threadId, text, {reformulate: false}));
        } catch {
            plan = null;
        }
        if (this.cancelRequested) return;
        if (plan === 'unavailable') {
            this.phase.set('idle');
            return;
        }
        if (!plan || !plan.chunks.length) {
            this.hardError.set('rewrite');
            this.phase.set('error');
            return;
        }
        this.chunks.set(plan.chunks);
        this.rewritten.set(false);
        this.streamComplete = true;
        this.bailedOut = false; // handoff complete; let synthesis run
        this.phase.set('generating');
        await this.runSynthesis(0);
    }

    private reset(): void {
        this.cancelRequested = false;
        this.streamAbort?.abort();
        this.streamAbort = null;
        this.streamComplete = false;
        this.bailedOut = false;
        this.playback.stopIfActive(this.id);
        this.revokeBlobs();
        this.chunks.set([]);
        this.ready.set([]);
        this.synthIndex.set(0);
        this.rewritten.set(true);
        this.elapsed.set(0);
        this.partError.set(null);
        this.retryInfo.set(null);
        this.keptParts.set(0);
        this.hardError.set(null);
        this.synthErrorCode.set(null);
    }

    ngOnDestroy(): void {
        this.cancelRequested = true;
        this.streamAbort?.abort();
        this.stopElapsed();
        this.playback.stopIfActive(this.id);
        this.revokeBlobs();
    }

    // ===== Player controls =====

    togglePlayback(): void {
        if (this.isPlaybackActive()) {
            this.playback.toggle();
        } else if (this.ready().length) {
            // Re-activate this message's playback (another message had taken over).
            const remainingChars = this.chunks()
                .slice(this.ready().length)
                .reduce((a, c) => a + c.length, 0);
            this.playback.start(this.id, this.ready(), {
                remainingChars,
                complete: this.streamComplete && this.ready().length === this.chunks().length,
            });
        }
    }

    prevPart(): void {
        if (this.isPlaybackActive()) this.playback.prevPart();
    }

    nextPart(): void {
        if (this.isPlaybackActive()) this.playback.nextPart();
    }

    cycleSpeed(): void {
        this.playback.cycleSpeed();
    }

    /** Seek from a click/drag on the progress bar (only when active). */
    onSeek(event: MouseEvent): void {
        if (!this.isPlaybackActive()) {
            this.togglePlayback();
            return;
        }
        const bar = event.currentTarget as HTMLElement;
        const rect = bar.getBoundingClientRect();
        const frac = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
        this.playback.seek(frac * this.playback.estimatedTotal());
    }

    // ===== Rewrite-stage copy helpers =====

    readonly rewriteKey = computed(() => {
        const e = this.elapsed();
        if (e < 1) return 'chat.tts.preparingRead';
        if (e >= REWRITE_SLOW_AT) return 'chat.tts.rewriteSlow';
        return 'chat.tts.rewriting';
    });
    readonly showElapsed = computed(
        () => this.elapsed() >= ELAPSED_SHOW_AT && this.elapsed() < REWRITE_SLOW_AT,
    );

    private startElapsed(): void {
        this.stopElapsed();
        this.elapsed.set(0);
        this.elapsedTimer = setInterval(() => this.elapsed.update((v) => v + 1), 1000);
    }

    private stopElapsed(): void {
        if (this.elapsedTimer !== null) {
            clearInterval(this.elapsedTimer);
            this.elapsedTimer = null;
        }
    }

    private revokeBlobs(): void {
        this.blobUrls.forEach((url) => URL.revokeObjectURL(url));
        this.blobUrls.clear();
    }

    private delay(ms: number): Promise<void> {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }

    private fmt(seconds: number): string {
        const s = Math.max(0, Math.round(seconds));
        const m = Math.floor(s / 60);
        return `${m}:${(s % 60).toString().padStart(2, '0')}`;
    }
}
