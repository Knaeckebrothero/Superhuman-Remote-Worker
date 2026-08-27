import {describe, it, expect} from 'vitest';
import {TestBed} from '@angular/core/testing';
import {of} from 'rxjs';
import {VoiceCapabilitiesService} from './voice-capabilities.service';
import {ApiService} from './api.service';
import type {VoiceCapabilities} from '../models/api.model';

function make(caps: VoiceCapabilities | null): VoiceCapabilitiesService {
  TestBed.configureTestingModule({
    providers: [
      VoiceCapabilitiesService,
      {provide: ApiService, useValue: {getVoiceCapabilities: () => of(caps)}},
    ],
  });
  return TestBed.inject(VoiceCapabilitiesService);
}

describe('VoiceCapabilitiesService', () => {
  it('reflects configured TTS with STT off', () => {
    const svc = make({tts: true, stt: false});
    expect(svc.tts()).toBe(true);
    expect(svc.stt()).toBe(false);
  });

  it('reflects both capabilities available', () => {
    const svc = make({tts: true, stt: true});
    expect(svc.tts()).toBe(true);
    expect(svc.stt()).toBe(true);
  });

  it('API unavailable (null) ⇒ fails open (signals stay null → callers stay optimistic)', () => {
    // Never disables a control we can't positively confirm is unavailable; the
    // button's own 204/502 handling is the authoritative backstop.
    const svc = make(null);
    expect(svc.tts()).toBeNull();
    expect(svc.stt()).toBeNull();
  });
});
