import { AUDIO_VISUALIZATION_CONFIG } from './audio-constants';

export class AudioLevelSmoother {
  private currentLevel = 0;
  private readonly ATTACK = AUDIO_VISUALIZATION_CONFIG.ATTACK_RATE;
  private readonly RELEASE = AUDIO_VISUALIZATION_CONFIG.RELEASE_RATE;

  smooth(inputLevel: number): number {
    if (inputLevel > this.currentLevel) {
      this.currentLevel = this.ATTACK * inputLevel + (1 - this.ATTACK) * this.currentLevel;
    } else {
      this.currentLevel = this.RELEASE * inputLevel + (1 - this.RELEASE) * this.currentLevel;
    }
    return Math.round(this.currentLevel);
  }
}
