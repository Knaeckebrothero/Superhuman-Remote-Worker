import {Injectable, computed, inject, signal} from '@angular/core';
import {ChatPreferencesService, PLAYBACK_SPEEDS} from '../../core/services/chat-preferences.service';

/** One synthesized section: its blob URL and measured duration (seconds). */
export interface PlaybackPart {
    url: string;
    duration: number;
}

/**
 * A ~50 ms silent WAV played inside the click gesture to grant the shared
 * `<audio>` element autoplay activation, so a later programmatic `play()` (after
 * the async plan/synthesis) isn't rejected — the Safari per-element rule and the
 * ~5 s transient-activation window would otherwise block first audio.
 */
const SILENT_WAV =
    'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAACABAAZGF0YQAAAAA=';

/** Estimated synthesis speed for the not-yet-generated tail (~chars/second). */
const EST_CHARS_PER_SEC = 15;

/**
 * Read-aloud playback engine (Phase 2 of the voice roadmap). Owns ONE persistent
 * `HTMLAudioElement` and presents the ordered chunks as a single virtual
 * timeline (prefix-sum offsets), so the player has one seek bar, pitch-corrected
 * speed, Media Session, and native buffering "for free" — with imperceptible
 * src-swap seams because chunk boundaries fall on sentence ends.
 *
 * Root-provided and single-element ⇒ **one playback at a time** across the whole
 * chat inherently: whichever message calls {@link start} becomes active and any
 * previous session stops. Each `AppReadAloudComponent` owns its own blob URLs
 * (and revokes them); this service only references the active set.
 *
 * Rejected alternatives (see the roadmap): MSE (raw-MP3 SourceBuffer is
 * Chrome-only) and pure Web Audio scheduling (loses pitch-preserving speed).
 */
@Injectable({providedIn: 'root'})
export class ReadAloudPlaybackService {
    private readonly prefs = inject(ChatPreferencesService);
    private el: HTMLAudioElement | null = null;
    private rafId = 0;
    /** Part index the element is waiting on because it isn't synthesized yet. */
    private awaitingPart = -1;

    /** Owner id (message turn key) of the loaded session, or null. */
    readonly activeId = signal<string | null>(null);
    readonly parts = signal<PlaybackPart[]>([]);
    /** Part currently loaded in the element. */
    readonly index = signal(0);
    /** Playback position within the current part (seconds), driven by rAF. */
    readonly positionInPart = signal(0);
    readonly playing = signal(false);
    /** Set when `play()` is rejected by the browser (autoplay/activation). */
    readonly blocked = signal(false);
    /** Whether every chunk has been synthesized (⇒ no estimated tail). */
    readonly complete = signal(false);
    /** Characters still to synthesize, for the estimated-tail width. */
    readonly remainingChars = signal(0);
    /** A virtual seek target the user picked inside the not-yet-synthesized
     *  region; resolved when enough chunks arrive. */
    readonly pendingSeek = signal<number | null>(null);

    /** Effective playback rate as a number (persisted preset). */
    readonly speed = computed(() => parseFloat(this.prefs.playbackSpeed()) || 1);

    /** Virtual start time of each part (prefix sums of durations). */
    readonly offsets = computed(() => {
        const out: number[] = [];
        let acc = 0;
        for (const p of this.parts()) {
            out.push(acc);
            acc += p.duration;
        }
        return out;
    });
    /** Sum of the measured (synthesized) durations. */
    readonly knownDuration = computed(() =>
        this.parts().reduce((a, p) => a + p.duration, 0),
    );
    /** Estimated seconds for the tail that isn't synthesized yet. */
    readonly estimatedTail = computed(() =>
        this.complete() ? 0 : this.remainingChars() / EST_CHARS_PER_SEC,
    );
    /** Full timeline length: measured region + estimated tail (never shrinks
     *  visually because the tail only converts to measured as chunks land). */
    readonly estimatedTotal = computed(() => this.knownDuration() + this.estimatedTail());
    /** Current playback position on the virtual timeline (seconds). */
    readonly currentTime = computed(
        () => (this.offsets()[this.index()] ?? 0) + this.positionInPart(),
    );

    isActive(id: string): boolean {
        return this.activeId() === id;
    }

    // ===== Session control =====

    /** Prime the shared element inside the click gesture (Safari activation).
     *  Safe to call every click; a no-op once activation is granted. */
    prime(): void {
        const el = this.ensureEl();
        try {
            el.src = SILENT_WAV;
            el.muted = true;
            void el
                .play()
                .then(() => {
                    el.pause();
                    el.muted = false;
                })
                .catch(() => {
                    el.muted = false;
                });
        } catch {
            /* best effort */
        }
    }

    /** Load a fresh session (stopping any other) and start from part 0. */
    start(
        id: string,
        parts: PlaybackPart[],
        opts: {remainingChars: number; complete: boolean},
    ): void {
        this.activeId.set(id);
        this.parts.set(parts.slice());
        this.remainingChars.set(opts.remainingChars);
        this.complete.set(opts.complete);
        this.index.set(0);
        this.positionInPart.set(0);
        this.pendingSeek.set(null);
        this.awaitingPart = -1;
        this.blocked.set(false);
        this.ensureEl();
        if (parts.length) {
            this.loadPart(0);
            this.playEl();
        }
        this.updateMediaSession();
    }

    /** Append a newly-synthesized part to the active session's timeline. */
    append(
        id: string,
        part: PlaybackPart,
        opts: {remainingChars: number; complete: boolean},
    ): void {
        if (this.activeId() !== id) return;
        this.parts.update((ps) => [...ps, part]);
        this.remainingChars.set(opts.remainingChars);
        this.complete.set(opts.complete);

        // If the element ran dry waiting for this exact part, resume into it.
        if (this.awaitingPart === this.parts().length - 1) {
            this.awaitingPart = -1;
            this.loadPart(this.parts().length - 1);
            this.playEl();
        }
        // Resolve a pending seek now that the target may be within reach.
        const pend = this.pendingSeek();
        if (pend !== null && pend < this.knownDuration()) {
            this.pendingSeek.set(null);
            this.seek(pend);
        }
    }

    /** Mark the active session's chunk list final (the rewrite stream finished and
     *  every chunk is synthesized) so the estimated tail collapses to the real
     *  total. Also rescues the race where the last chunk was appended while the
     *  stream was still open (`complete:false`): playback would then park at the
     *  end waiting for a chunk that never comes — settle it into the ended state. */
    markComplete(id: string): void {
        if (this.activeId() !== id) return;
        this.complete.set(true);
        this.remainingChars.set(0);
        if (this.awaitingPart >= 0 && this.awaitingPart >= this.parts().length) {
            this.awaitingPart = -1;
            this.playing.set(false);
            this.stopRaf();
            const last = this.parts().length - 1;
            if (last >= 0) {
                this.index.set(last);
                this.positionInPart.set(this.parts()[last]?.duration ?? 0);
            }
        }
    }

    /** Stop + clear the session iff `id` is the active one (component destroy). */
    stopIfActive(id: string): void {
        if (this.activeId() !== id) return;
        this.pauseEl();
        this.activeId.set(null);
        this.parts.set([]);
        this.index.set(0);
        this.positionInPart.set(0);
        this.awaitingPart = -1;
        if (this.el) this.el.removeAttribute('src');
    }

    // ===== Transport =====

    toggle(): void {
        if (this.playing()) this.pauseEl();
        else this.playEl();
    }

    /** Seek to a virtual time (seconds). Targets in the unsynthesized tail are
     *  parked as a pending seek and resolved on chunk arrival. */
    seek(virtualTime: number): void {
        const t = Math.max(0, virtualTime);
        if (t >= this.knownDuration() && !this.complete()) {
            this.pendingSeek.set(t);
            return;
        }
        const offsets = this.offsets();
        const parts = this.parts();
        let i = parts.length - 1;
        for (let k = 0; k < parts.length; k++) {
            if (t < offsets[k] + parts[k].duration) {
                i = k;
                break;
            }
        }
        if (i !== this.index()) this.loadPart(i);
        const local = Math.max(0, t - (offsets[i] ?? 0));
        if (this.el) this.el.currentTime = Math.min(local, parts[i]?.duration || local);
        this.positionInPart.set(local);
        this.updatePositionState();
    }

    seekToPart(i: number): void {
        const offsets = this.offsets();
        if (i < 0 || i >= offsets.length) return;
        this.seek(offsets[i]);
        if (!this.playing()) this.playEl();
    }

    nextPart(): void {
        this.seekToPart(this.index() + 1);
    }

    prevPart(): void {
        // If we're >2s into a part, restart it; else jump to the previous part.
        this.seekToPart(this.positionInPart() > 2 ? this.index() : this.index() - 1);
    }

    /** Cycle to the next speed preset, persist it, and apply immediately. */
    cycleSpeed(): void {
        const cur = this.prefs.playbackSpeed();
        const next = PLAYBACK_SPEEDS[(PLAYBACK_SPEEDS.indexOf(cur) + 1) % PLAYBACK_SPEEDS.length];
        this.prefs.setPlaybackSpeed(next);
        this.applyRate();
    }

    // ===== Element plumbing =====

    private ensureEl(): HTMLAudioElement {
        if (this.el) return this.el;
        const el = new Audio();
        el.preload = 'auto';
        el.addEventListener('ended', () => this.onEnded());
        el.addEventListener('play', () => {
            this.playing.set(true);
            this.blocked.set(false);
            this.startRaf();
        });
        // Deliberately NO 'pause' event listener. Swapping `src` between chunks
        // makes the browser fire a spurious 'pause'; treating it as a real pause
        // here used to cancel the scrubber's rAF — and on a play-before-pause
        // event race the restart no-op'd (rafId still set) and the pause then
        // killed the loop for good, freezing the progress bar for the rest of
        // playback. Pausing is owned explicitly by pauseEl()/onEnded() instead,
        // which manage both the rAF and the `playing`/position state.
        el.addEventListener('loadedmetadata', () => this.applyRate());
        this.el = el;
        return el;
    }

    private loadPart(i: number): void {
        const el = this.ensureEl();
        const part = this.parts()[i];
        if (!part) return;
        this.index.set(i);
        this.positionInPart.set(0);
        if (el.src !== part.url) el.src = part.url;
        this.applyRate();
    }

    private playEl(): void {
        const el = this.ensureEl();
        try {
            const p = el.play();
            if (p && typeof p.then === 'function') {
                p.then(() => this.blocked.set(false)).catch(() => this.blocked.set(true));
            }
        } catch {
            // Synchronous throw (e.g. jsdom, or a detached element) — treat as blocked.
            this.blocked.set(true);
        }
    }

    private pauseEl(): void {
        try {
            this.el?.pause();
        } catch {
            /* jsdom / detached element */
        }
        // Own the pause explicitly (there is no 'pause' event listener) so a real
        // pause stops the scrubber, while a chunk-transition src-swap doesn't.
        this.playing.set(false);
        this.stopRaf();
    }

    private onEnded(): void {
        const next = this.index() + 1;
        if (next < this.parts().length) {
            this.loadPart(next);
            this.playEl();
        } else if (!this.complete()) {
            // Ran out of synthesized audio mid-message — wait for the next chunk.
            this.awaitingPart = next;
            this.playing.set(false);
            this.stopRaf();
        } else {
            this.playing.set(false);
            this.positionInPart.set(this.parts()[this.index()]?.duration ?? 0);
            this.stopRaf();
        }
    }

    private applyRate(): void {
        if (!this.el) return;
        const rate = this.speed();
        // Set both so the rate survives a src swap; preservesPitch keeps voices
        // natural at speed (webkit prefix for older Safari).
        this.el.playbackRate = rate;
        this.el.defaultPlaybackRate = rate;
        const anyEl = this.el as HTMLAudioElement & {webkitPreservesPitch?: boolean};
        this.el.preservesPitch = true;
        anyEl.webkitPreservesPitch = true;
    }

    // Drive the scrubber from rAF (smooth) rather than 'timeupdate' (~4 Hz).
    private startRaf(): void {
        if (this.rafId) return;
        const tick = () => {
            if (this.el) this.positionInPart.set(this.el.currentTime);
            this.updatePositionState();
            this.rafId = requestAnimationFrame(tick);
        };
        this.rafId = requestAnimationFrame(tick);
    }

    private stopRaf(): void {
        if (this.rafId) {
            cancelAnimationFrame(this.rafId);
            this.rafId = 0;
        }
    }

    // ===== Media Session (lock-screen / hardware keys) =====

    private updateMediaSession(): void {
        const ms = navigator.mediaSession;
        if (!ms) return;
        try {
            ms.setActionHandler('play', () => this.playEl());
            ms.setActionHandler('pause', () => this.pauseEl());
            ms.setActionHandler('seekbackward', () => this.seek(this.currentTime() - 10));
            ms.setActionHandler('seekforward', () => this.seek(this.currentTime() + 10));
            ms.setActionHandler('previoustrack', () => this.prevPart());
            ms.setActionHandler('nexttrack', () => this.nextPart());
        } catch {
            /* some handlers unsupported — ignore */
        }
    }

    private updatePositionState(): void {
        const ms = navigator.mediaSession;
        if (!ms || typeof ms.setPositionState !== 'function') return; // Firefox lacks it
        const duration = this.estimatedTotal();
        if (!isFinite(duration) || duration <= 0) return;
        try {
            ms.setPositionState({
                duration,
                playbackRate: this.speed(),
                position: Math.min(this.currentTime(), duration), // clamp ≤ duration
            });
        } catch {
            /* invalid state (position>duration race) — ignore */
        }
    }
}
