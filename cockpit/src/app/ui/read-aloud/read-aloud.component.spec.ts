import {describe, it, expect, vi} from 'vitest';
import {Injector, runInInjectionContext, signal} from '@angular/core';
import {AppReadAloudComponent} from './read-aloud.component';
import {ApiService} from '../../core/services/api.service';
import {I18nService} from '../../core/services/i18n.service';
import {ChatPreferencesService} from '../../core/services/chat-preferences.service';
import {VoiceCapabilitiesService} from '../../core/services/voice-capabilities.service';
import {ReadAloudPlaybackService} from './read-aloud-playback.service';

/**
 * Bare injection-context instantiation (no template render), matching the repo's
 * component-unit convention. The async plan/synthesis + playback flow depends on
 * required signal inputs and the DOM (covered by the build's template typecheck,
 * the playback-service spec, and the live smoke); these tests cover the
 * input-independent state machine: availability gating, the elapsed-driven
 * rewrite copy, duration formatting, and cancel semantics.
 */
function create(ttsAvailable: boolean | null = true): AppReadAloudComponent {
  const injector = Injector.create({
    providers: [
      {provide: ApiService, useValue: {}},
      {provide: I18nService, useValue: {activeLang: signal('en')}},
      {
        provide: ChatPreferencesService,
        useValue: {playbackSpeed: () => '1', setPlaybackSpeed: vi.fn()},
      },
      {
        provide: VoiceCapabilitiesService,
        useValue: {tts: signal(ttsAvailable), stt: signal(true)},
      },
      {
        provide: ReadAloudPlaybackService,
        useValue: {
          isActive: () => false,
          stopIfActive: vi.fn(),
          prime: vi.fn(),
          markComplete: vi.fn(),
        },
      },
    ],
  });
  return runInInjectionContext(injector, () => new AppReadAloudComponent());
}

describe('AppReadAloudComponent', () => {
  it('disables only on positively-known unavailability (fails open)', () => {
    expect(create(false).unavailable()).toBe(true);
    expect(create(true).unavailable()).toBe(false);
    expect(create(null).unavailable()).toBe(false);
  });

  it('walks the rewrite copy from instant-ack → rewriting → slow by elapsed time', () => {
    const c = create();
    expect(c.rewriteKey()).toBe('chat.tts.preparingRead'); // elapsed 0
    c.elapsed.set(12);
    expect(c.rewriteKey()).toBe('chat.tts.rewriting');
    expect(c.showElapsed()).toBe(true);
    c.elapsed.set(30);
    expect(c.rewriteKey()).toBe('chat.tts.rewriteSlow');
    expect(c.showElapsed()).toBe(false); // the slow line replaces the counter
  });

  it('formats the total duration from the synthesized parts', () => {
    const c = create();
    c.ready.set([
      {url: 'blob:1', duration: 61},
      {url: 'blob:2', duration: 12},
    ]);
    expect(c.durationLabel()).toBe('1:13');
    expect(c.hasAudio()).toBe(true);
  });

  it('cancel keeps already-synthesized parts', () => {
    const c = create();
    c.ready.set([
      {url: 'blob:1', duration: 5},
      {url: 'blob:2', duration: 5},
    ]);
    c.phase.set('generating');
    c.cancel();
    expect(c.phase()).toBe('cancelled');
    expect(c.keptParts()).toBe(2);
  });

  it('cancel before any part exists returns to the button', () => {
    const c = create();
    c.phase.set('rewriting');
    c.cancel();
    expect(c.phase()).toBe('idle');
  });

  it('a start-time failure surfaces an honest error box, never a silent no-op', () => {
    const c = create();
    (c as unknown as {failStart: (k: string) => void}).failStart('empty');
    expect(c.phase()).toBe('error');
    expect(c.hardError()).toBe('empty');
  });

  it('dismiss returns a non-retryable error box to the Read button', () => {
    const c = create();
    c.phase.set('error');
    c.hardError.set('no-thread');
    c.dismiss();
    expect(c.phase()).toBe('idle');
  });
});
