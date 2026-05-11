import { AUDIO_VISUALIZATION_CONFIG } from './audio-constants';
import { AudioLevelSmoother } from './audio-level-smoother';

export class VoiceAudioProcessor {
  private analyser: AnalyserNode;
  // Use a non-shared ArrayBuffer-backed view so the strict TS lib types
  // (Uint8Array<ArrayBuffer>, not <ArrayBufferLike>) line up with
  // AnalyserNode.getByteTimeDomainData's parameter type.
  private readonly timeDomainData: Uint8Array<ArrayBuffer>;
  private smoother: AudioLevelSmoother;

  constructor(analyserNode: AnalyserNode) {
    this.analyser = analyserNode;
    this.analyser.fftSize = AUDIO_VISUALIZATION_CONFIG.FFT_SIZE;
    this.analyser.smoothingTimeConstant = AUDIO_VISUALIZATION_CONFIG.SMOOTHING_TIME_CONSTANT;

    this.timeDomainData = new Uint8Array(new ArrayBuffer(this.analyser.fftSize));
    this.smoother = new AudioLevelSmoother();
  }

  getVoiceLevel(): number {
    this.analyser.getByteTimeDomainData(this.timeDomainData);

    let sum = 0;
    for (let i = 0; i < this.timeDomainData.length; i++) {
      const sample = (this.timeDomainData[i] - 128) / 128;
      sum += sample * sample;
    }
    const rms = Math.sqrt(sum / this.timeDomainData.length);
    const db = 20 * Math.log10(Math.max(rms, 0.0001));

    return this.smoother.smooth(this.normalizeVoiceLevel(db));
  }

  private normalizeVoiceLevel(db: number): number {
    const c = AUDIO_VISUALIZATION_CONFIG;
    if (db <= c.SILENCE_DB) return 1;
    if (db <= c.QUIET_DB) return this.lerp(1, 20, (db - c.SILENCE_DB) / (c.QUIET_DB - c.SILENCE_DB));
    if (db <= c.NORMAL_DB) return this.lerp(20, 40, (db - c.QUIET_DB) / (c.NORMAL_DB - c.QUIET_DB));
    if (db <= c.LOUD_DB) return this.lerp(40, 60, (db - c.NORMAL_DB) / (c.LOUD_DB - c.NORMAL_DB));
    if (db <= c.VERY_LOUD_DB)
      return this.lerp(60, 80, (db - c.LOUD_DB) / (c.VERY_LOUD_DB - c.LOUD_DB));
    return this.lerp(80, 100, Math.min(1, (db - c.VERY_LOUD_DB) / (c.MAX_DB - c.VERY_LOUD_DB)));
  }

  private lerp(a: number, b: number, t: number): number {
    return a + (b - a) * Math.max(0, Math.min(1, t));
  }
}
