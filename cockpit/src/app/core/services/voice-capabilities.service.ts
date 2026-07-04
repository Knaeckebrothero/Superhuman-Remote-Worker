import {inject, Injectable, signal} from '@angular/core';
import {ApiService} from './api.service';

/**
 * Whether the current user has a usable TTS / STT model configured, fetched
 * once and shared. Drives the disabled-with-reason state on the read-aloud +
 * mic buttons so a misconfigured deployment shows an honest "not set up"
 * affordance instead of a dead click that silently answers `204` — the root of
 * the "read doesn't work" reports (Phase 0 of the voice roadmap).
 *
 * Fails **open**: while loading, and on any error, the signals stay `null` and
 * callers treat that as "assume available". The button's own `204`/`502`
 * handling remains the authoritative backstop; this layer only *disables* a
 * control we positively know the backend can't serve.
 */
@Injectable({providedIn: 'root'})
export class VoiceCapabilitiesService {
  private readonly api = inject(ApiService);

  // null = not yet loaded / errored (fail open); true|false = known availability.
  readonly tts = signal<boolean | null>(null);
  readonly stt = signal<boolean | null>(null);

  constructor() {
    this.load();
  }

  load(): void {
    this.api.getVoiceCapabilities().subscribe((caps) => {
      if (!caps) return; // fail open — leave null so callers stay optimistic
      this.tts.set(!!caps.tts);
      this.stt.set(!!caps.stt);
    });
  }
}
