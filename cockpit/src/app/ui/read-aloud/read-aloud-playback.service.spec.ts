import {describe, it, expect, vi, beforeEach} from 'vitest';
import {TestBed} from '@angular/core/testing';
import {ReadAloudPlaybackService, PlaybackPart} from './read-aloud-playback.service';
import {ChatPreferencesService} from '../../core/services/chat-preferences.service';

/** Minimal HTMLAudioElement stand-in for the seek/start paths (jsdom's media
 *  element throws on play()). rAF is stubbed to never fire so signals stay put. */
function fakeAudio() {
  return {
    play: vi.fn().mockResolvedValue(undefined),
    pause: vi.fn(),
    addEventListener: vi.fn(),
    removeAttribute: vi.fn(),
    currentTime: 0,
    playbackRate: 1,
    defaultPlaybackRate: 1,
    preservesPitch: true,
    muted: false,
    preload: '',
    src: '',
  } as unknown as HTMLAudioElement;
}

function make(speed = '1'): ReadAloudPlaybackService {
  TestBed.configureTestingModule({
    providers: [
      ReadAloudPlaybackService,
      {
        provide: ChatPreferencesService,
        useValue: {
          playbackSpeed: () => speed,
          setPlaybackSpeed: vi.fn(),
        },
      },
    ],
  });
  const svc = TestBed.inject(ReadAloudPlaybackService);
  (svc as unknown as {el: HTMLAudioElement}).el = fakeAudio();
  return svc;
}

const parts = (durations: number[]): PlaybackPart[] =>
  durations.map((d, i) => ({url: `blob:${i}`, duration: d}));

describe('ReadAloudPlaybackService — virtual timeline', () => {
  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', () => 0);
    vi.stubGlobal('cancelAnimationFrame', () => {});
  });

  it('offsets are prefix sums of part durations', () => {
    const svc = make();
    svc.parts.set(parts([10, 20, 30]));
    expect(svc.offsets()).toEqual([0, 10, 30]);
    expect(svc.knownDuration()).toBe(60);
  });

  it('currentTime = offset of current part + position within it', () => {
    const svc = make();
    svc.parts.set(parts([10, 20, 30]));
    svc.index.set(1);
    svc.positionInPart.set(7);
    expect(svc.currentTime()).toBe(17);
  });

  it('estimates a tail from remaining chars until complete', () => {
    const svc = make();
    svc.parts.set(parts([10]));
    svc.remainingChars.set(150); // /15 chars-per-sec ⇒ 10s
    svc.complete.set(false);
    expect(svc.estimatedTail()).toBe(10);
    expect(svc.estimatedTotal()).toBe(20); // 10 known + 10 estimated
    svc.complete.set(true);
    expect(svc.estimatedTail()).toBe(0);
    expect(svc.estimatedTotal()).toBe(10);
  });

  it('speed reflects the persisted preset', () => {
    expect(make('1.5').speed()).toBe(1.5);
  });
});

describe('ReadAloudPlaybackService — seek routing', () => {
  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', () => 0);
    vi.stubGlobal('cancelAnimationFrame', () => {});
  });

  it('routes a virtual seek to the right part + local offset', () => {
    const svc = make();
    svc.complete.set(true);
    svc.parts.set(parts([10, 20, 30]));
    svc.seek(25); // 25s ⇒ part 1 (starts at 10), 15s into it
    expect(svc.index()).toBe(1);
    expect(svc.positionInPart()).toBe(15);
  });

  it('parks a seek into the unsynthesized tail and resolves it on arrival', () => {
    const svc = make();
    svc.parts.set(parts([10]));
    svc.complete.set(false);
    svc.remainingChars.set(600);
    svc.seek(50); // beyond the 10s known region ⇒ pending
    expect(svc.pendingSeek()).toBe(50);
    expect(svc.index()).toBe(0);

    // A big chunk arrives covering 50s → the pending seek resolves.
    svc.append(svc.activeId() ?? 'x', {url: 'blob:1', duration: 60}, {
      remainingChars: 0,
      complete: true,
    });
  });
});

describe('ReadAloudPlaybackService — one playback at a time', () => {
  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', () => 0);
    vi.stubGlobal('cancelAnimationFrame', () => {});
  });

  it('start() makes a session active; a second start() takes over', () => {
    const svc = make();
    svc.start('msg-A', parts([5]), {remainingChars: 0, complete: true});
    expect(svc.isActive('msg-A')).toBe(true);

    svc.start('msg-B', parts([8]), {remainingChars: 0, complete: true});
    expect(svc.isActive('msg-B')).toBe(true);
    expect(svc.isActive('msg-A')).toBe(false);
  });

  it('append ignores parts for a non-active session', () => {
    const svc = make();
    svc.start('msg-A', parts([5]), {remainingChars: 100, complete: false});
    svc.append('msg-OTHER', {url: 'blob:x', duration: 9}, {remainingChars: 0, complete: true});
    expect(svc.parts().length).toBe(1); // unchanged
  });

  it('markComplete collapses the estimated tail to the real total', () => {
    const svc = make();
    svc.start('msg-A', parts([10]), {remainingChars: 300, complete: false});
    expect(svc.estimatedTotal()).toBe(30); // 10 known + 300/15 estimated
    svc.markComplete('msg-A');
    expect(svc.complete()).toBe(true);
    expect(svc.estimatedTotal()).toBe(10); // tail gone
  });

  it('markComplete rescues a session parked at the end (last chunk arrived while streaming)', () => {
    const svc = make();
    svc.start('msg-A', parts([5, 8]), {remainingChars: 0, complete: false});
    // Simulate the element having run out at the end of the last synthesized part.
    (svc as unknown as {awaitingPart: number}).awaitingPart = 2; // == parts.length
    svc.playing.set(true);
    svc.markComplete('msg-A');
    expect(svc.playing()).toBe(false);
    expect(svc.index()).toBe(1); // settled on the last part
    expect(svc.positionInPart()).toBe(8); // at its end
  });

  it('markComplete ignores a non-active session', () => {
    const svc = make();
    svc.start('msg-A', parts([5]), {remainingChars: 100, complete: false});
    svc.markComplete('msg-OTHER');
    expect(svc.complete()).toBe(false);
  });

  it('stopIfActive clears only the active session', () => {
    const svc = make();
    svc.start('msg-A', parts([5]), {remainingChars: 0, complete: true});
    svc.stopIfActive('msg-OTHER');
    expect(svc.isActive('msg-A')).toBe(true);
    svc.stopIfActive('msg-A');
    expect(svc.activeId()).toBeNull();
  });
});
