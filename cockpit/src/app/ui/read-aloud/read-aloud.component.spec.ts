import {describe, it, expect} from 'vitest';
import {Injector, runInInjectionContext, signal} from '@angular/core';
import {AppReadAloudComponent} from './read-aloud.component';
import {ApiService} from '../../core/services/api.service';
import {I18nService} from '../../core/services/i18n.service';
import {VoiceCapabilitiesService} from '../../core/services/voice-capabilities.service';

/**
 * Instantiate in a bare injection context (no template render), matching the
 * repo's component-unit convention. The async plan/synthesis flow depends on
 * required signal inputs (exercised by the build's template typecheck + the
 * live smoke); these unit tests cover the input-independent state machine:
 * availability gating, the elapsed-driven rewrite copy, duration formatting,
 * and cancel semantics.
 */
function create(ttsAvailable: boolean | null = true): AppReadAloudComponent {
  const injector = Injector.create({
    providers: [
      {provide: ApiService, useValue: {}},
      {provide: I18nService, useValue: {activeLang: signal('en')}},
      {
        provide: VoiceCapabilitiesService,
        useValue: {tts: signal(ttsAvailable), stt: signal(true)},
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

  it('formats the total duration once every part has loaded', () => {
    const c = create();
    c.chunks.set(['a', 'b']);
    (c as unknown as {durations: {set: (v: number[]) => void}}).durations.set([61, 12]);
    expect(c.durationLabel()).toBe('1:13');
  });

  it('hides the duration until every part is measured', () => {
    const c = create();
    c.chunks.set(['a', 'b']);
    (c as unknown as {durations: {set: (v: number[]) => void}}).durations.set([61]); // only one
    expect(c.durationLabel()).toBe('');
  });

  it('cancel keeps already-synthesized parts', () => {
    const c = create();
    c.chunks.set(['a', 'b', 'c']);
    c.chunkUrls.set(['blob:1', 'blob:2', undefined]);
    c.phase.set('generating');
    c.cancel();
    expect(c.phase()).toBe('cancelled');
    expect(c.keptParts()).toBe(2);
  });

  it('cancel before any part exists returns to the button', () => {
    const c = create();
    c.chunkUrls.set([undefined, undefined]);
    c.phase.set('rewriting');
    c.cancel();
    expect(c.phase()).toBe('idle');
  });
});
