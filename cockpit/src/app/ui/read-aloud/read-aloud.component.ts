import {
    ChangeDetectionStrategy,
    Component,
    ElementRef,
    OnDestroy,
    QueryList,
    ViewChildren,
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
import {VoiceCapabilitiesService} from '../../core/services/voice-capabilities.service';

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

/** Auto-retry cap for a transient chunk-synthesis failure (attempts 2..MAX are
 *  shown as "retrying (attempt N of MAX)"). */
const MAX_SYNTH_ATTEMPTS = 3;
/** Seconds in the rewrite stage before we surface the elapsed counter / the
 *  "responding slowly" line (NN/g: waits >10 s need determinate feedback). */
const ELAPSED_SHOW_AT = 10;
const REWRITE_SLOW_AT = 25;

/**
 * Read-aloud status box for one assistant message (Phase 1 of the voice
 * roadmap, docs/features/voice_experience_roadmap.md). Renders the read button,
 * then — the instant it's clicked — a box that ticks through named stages
 * (rewriting → generating part N of M → player) with an elapsed counter,
 * cancel-at-every-stage, per-part retry, and an honest "rewriting skipped" note
 * when no auxiliary model cleaned the text. Playback still uses native
 * `<audio>` elements; the custom themed player replaces them in Phase 2.
 *
 * Design-system widget: standalone, OnPush, signal state, own stylesheet. The
 * box appears in the same slot the player occupies (in-place acknowledgment),
 * never a detached spinner.
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
    /** The assistant message text to read aloud. */
    readonly content = input.required<string>();
    /** Persistent-thread id — the auth scope for the TTS endpoints. */
    readonly threadId = input<string | null>(null);

    private readonly api = inject(ApiService);
    private readonly i18n = inject(I18nService);
    private readonly voiceCaps = inject(VoiceCapabilitiesService);

    readonly phase = signal<ReadAloudPhase>('idle');
    readonly chunks = signal<string[]>([]);
    /** Blob URL per chunk, filled as each is synthesized (undefined until then). */
    readonly chunkUrls = signal<(string | undefined)[]>([]);
    /** Chunk currently being synthesized — drives "part N of M". */
    readonly synthIndex = signal(0);
    /** Whether the aux LLM actually rewrote the text (false → raw markdown). */
    readonly rewritten = signal(true);
    /** Any player is currently playing → the status demotes to "Playing · …". */
    readonly playing = signal(false);
    /** Seconds elapsed in the rewrite stage (shown after ELAPSED_SHOW_AT). */
    readonly elapsed = signal(0);
    /** Index of a chunk that failed after auto-retries — earlier parts stay
     *  playable and a "Retry part N" is offered. */
    readonly partError = signal<number | null>(null);
    /** Live auto-retry indicator for a transient chunk failure. */
    readonly retryInfo = signal<{index: number; attempt: number} | null>(null);
    /** Index whose autoplay the browser blocked → a "tap to play" prompt. */
    readonly playBlocked = signal<number | undefined>(undefined);
    /** Parts already synthesized when the user cancelled (kept playable). */
    readonly keptParts = signal(0);
    /** 'rewrite' = the plan call failed; 'synthesis' = the very first chunk
     *  failed (nothing playable). Both are hard errors offering a restart. */
    readonly hardError = signal<'rewrite' | 'synthesis' | null>(null);
    /** Per-chunk audio durations (seconds), read on loadedmetadata. */
    private readonly durations = signal<number[]>([]);

    /** TTS positively known to be unconfigured → button disabled with a reason
     *  (fails open: null/true stays enabled, the 204 path is the backstop). */
    readonly unavailable = computed(() => this.voiceCaps.tts() === false);
    readonly total = computed(() => this.chunks().length);
    readonly hasAudio = computed(() => this.chunkUrls().some(Boolean));
    /** Active (non-terminal) stage → show the ✕ cancel control. */
    readonly isActive = computed(
        () => this.phase() === 'rewriting' || this.phase() === 'generating',
    );
    private readonly spoken = computed(() => this.chunks().join('\n\n'));
    /** The "Spoken version" disclosure only when the rewrite differs from the
     *  original (otherwise it just mirrors the message bubble). */
    readonly showSpoken = computed(
        () =>
            this.rewritten() &&
            this.chunks().length > 0 &&
            this.spoken().trim() !== this.content().trim(),
    );
    readonly spokenText = this.spoken;
    /** "m:ss" total once every part's metadata has loaded, else empty. */
    readonly durationLabel = computed(() => {
        const ds = this.durations();
        if (ds.length !== this.total() || ds.some((d) => !d)) return '';
        const secs = Math.round(ds.reduce((a, b) => a + b, 0));
        const m = Math.floor(secs / 60);
        const s = secs % 60;
        return `${m}:${s.toString().padStart(2, '0')}`;
    });

    // Non-signal control state.
    private cancelRequested = false;
    private playPending: number | null = null;
    private elapsedTimer: ReturnType<typeof setInterval> | null = null;
    private readonly blobUrls = new Set<string>();

    @ViewChildren('raAudio')
    private players?: QueryList<ElementRef<HTMLAudioElement>>;

    // ===== Lifecycle =====

    /** Begin (or restart) reading the message aloud. */
    async start(): Promise<void> {
        const threadId = this.threadId();
        const text = this.content();
        if (!threadId || !text.trim() || this.unavailable()) return;
        if (this.phase() === 'rewriting' || this.phase() === 'generating') return;

        this.reset();
        this.phase.set('rewriting');
        this.startElapsed();

        let plan: {chunks: string[]; rewritten: boolean} | 'unavailable' | null;
        try {
            plan = await firstValueFrom(this.api.planTTS(threadId, text));
        } catch {
            plan = null;
        }
        this.stopElapsed();
        if (this.cancelRequested) return;

        if (plan === 'unavailable') {
            // Feature turned off mid-flight — revert to the button silently.
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
        this.chunkUrls.set(new Array(plan.chunks.length));
        this.rewritten.set(plan.rewritten);
        this.playPending = 0; // the first section autoplays once it loads
        this.phase.set('generating');
        await this.runSynthesis(0);
    }

    /** Synthesize chunks in order from `startAt`; the first autoplays and the
     *  rest render as they arrive. A failed chunk stops the chain but keeps the
     *  earlier ones playable (or hard-fails when it's the very first). */
    private async runSynthesis(startAt: number): Promise<void> {
        const total = this.chunks().length;
        for (let i = startAt; i < total; i++) {
            if (this.cancelRequested) return;
            this.synthIndex.set(i);
            const url = await this.synthChunk(i);
            if (this.cancelRequested) return;
            if (!url) {
                if (i === 0) {
                    this.hardError.set('synthesis');
                    this.phase.set('error');
                } else {
                    // Earlier sections stay playable; offer a retry for this one.
                    this.partError.set(i);
                }
                return;
            }
        }
        this.partError.set(null);
        this.phase.set('done');
    }

    /** Synthesize chunk `i` with bounded, visible auto-retry. Returns its blob
     *  URL or null after exhausting the retries. */
    private async synthChunk(i: number): Promise<string | null> {
        const chunks = this.chunks();
        if (i < 0 || i >= chunks.length) return null;
        const cached = this.chunkUrls()[i];
        if (cached) return cached;
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
            let res: {text: string; audio: Blob} | 'unavailable' | null;
            try {
                res = await firstValueFrom(
                    this.api.generateTTS(threadId, chunks[i], {
                        language: lang,
                        reformulate: false,
                    }),
                );
            } catch {
                res = null;
            }
            if (res && res !== 'unavailable') {
                this.retryInfo.set(null);
                const url = URL.createObjectURL(res.audio);
                this.blobUrls.add(url);
                const urls = this.chunkUrls().slice();
                urls[i] = url;
                this.chunkUrls.set(urls);
                return url;
            }
            // 'unavailable' mid-run is a real failure (not "off"); don't retry.
            if (res === 'unavailable') break;
        }
        this.retryInfo.set(null);
        return null;
    }

    /** Regenerate one failed chunk from a user gesture, then resume the chain. */
    async retryPart(i: number): Promise<void> {
        if (this.phase() !== 'generating') this.phase.set('generating');
        this.partError.set(null);
        this.cancelRequested = false;
        const url = await this.synthChunk(i);
        if (!url) {
            this.partError.set(i);
            return;
        }
        await this.runSynthesis(i + 1);
    }

    /** Cancel at any stage. Parts already synthesized stay playable; cancelling
     *  before any part exists just returns to the button. */
    cancel(): void {
        this.cancelRequested = true;
        this.stopElapsed();
        this.players?.forEach((r) => {
            if (!r.nativeElement.paused) r.nativeElement.pause();
        });
        const kept = this.chunkUrls().filter(Boolean).length;
        if (kept === 0) {
            this.phase.set('idle');
        } else {
            this.keptParts.set(kept);
            this.phase.set('cancelled');
        }
    }

    /** Restart from scratch (from a terminal state). */
    restart(): void {
        this.start();
    }

    private reset(): void {
        this.cancelRequested = false;
        this.playPending = null;
        this.revokeBlobs();
        this.chunks.set([]);
        this.chunkUrls.set([]);
        this.synthIndex.set(0);
        this.rewritten.set(true);
        this.playing.set(false);
        this.elapsed.set(0);
        this.partError.set(null);
        this.retryInfo.set(null);
        this.playBlocked.set(undefined);
        this.keptParts.set(0);
        this.hardError.set(null);
        this.durations.set([]);
    }

    ngOnDestroy(): void {
        this.cancelRequested = true;
        this.stopElapsed();
        this.revokeBlobs();
    }

    // ===== Playback (native <audio>, keyless — one instance per message) =====

    /** A section's player loaded: record its duration and autoplay it iff it's
     *  the section we're waiting for. */
    onPlayerReady(event: Event, index: number): void {
        const el = event.target as HTMLAudioElement;
        const d = el.duration;
        if (isFinite(d) && d > 0) {
            const ds = this.durations().slice();
            ds[index] = d;
            this.durations.set(ds);
        }
        if (this.playPending !== index) return;
        this.playPending = null;
        el.play().catch(() => this.playBlocked.set(index));
    }

    /** Auto-advance: play the next section, or queue it if it isn't ready yet. */
    onChunkEnded(index: number): void {
        const next = index + 1;
        if (next >= this.chunks().length) {
            this.playing.set(false);
            return;
        }
        const el = this.findPlayer(next);
        if (el) el.play().catch(() => this.playBlocked.set(next));
        else this.playPending = next;
    }

    /** One voice at a time; a successful play clears any "tap to play" prompt. */
    onPlayerPlay(event: Event): void {
        this.playing.set(true);
        if (this.playBlocked() !== undefined) this.playBlocked.set(undefined);
        const active = event.target as HTMLAudioElement;
        this.players?.forEach((r) => {
            const el = r.nativeElement;
            if (el !== active && !el.paused) el.pause();
        });
    }

    onPlayerPause(): void {
        const anyPlaying = this.players?.some((r) => !r.nativeElement.paused);
        if (!anyPlaying) this.playing.set(false);
    }

    /** Resume the section whose autoplay was blocked, from this user gesture. */
    resumeBlockedPlayback(): void {
        const index = this.playBlocked();
        if (index === undefined) return;
        this.playBlocked.set(undefined);
        const el = this.findPlayer(index);
        el?.play().catch(() => this.playBlocked.set(index));
    }

    private findPlayer(index: number): HTMLAudioElement | null {
        const ref = this.players?.find(
            (r) => r.nativeElement.dataset['raIndex'] === String(index),
        );
        return ref?.nativeElement ?? null;
    }

    // ===== Helpers =====

    /** Copy for the rewrite stage: instant ack → rewriting → elapsed → slow. */
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
}
