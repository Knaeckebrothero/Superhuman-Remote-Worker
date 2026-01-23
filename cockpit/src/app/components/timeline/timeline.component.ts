import { Component, signal, computed } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MenuComponent } from '../menu/menu.component';

/**
 * Timeline scrubber component for playback control.
 * Fixed 60px height bar at the top of the app.
 */
@Component({
  selector: 'app-timeline',
  imports: [FormsModule, MenuComponent],
  template: `
    <div class="timeline">
      <app-menu />

      <div class="divider"></div>

      <button class="play-button" (click)="togglePlay()" [attr.aria-label]="isPlaying() ? 'Pause' : 'Play'">
        @if (isPlaying()) {
          <svg viewBox="0 0 24 24" fill="currentColor">
            <rect x="6" y="4" width="4" height="16" />
            <rect x="14" y="4" width="4" height="16" />
          </svg>
        } @else {
          <svg viewBox="0 0 24 24" fill="currentColor">
            <polygon points="5,3 19,12 5,21" />
          </svg>
        }
      </button>

      <span class="time-display">{{ formattedCurrentTime() }}</span>

      <div class="scrubber-container">
        <input
          type="range"
          class="scrubber"
          [min]="0"
          [max]="duration()"
          [ngModel]="currentTime()"
          (ngModelChange)="seek($event)"
          [attr.aria-label]="'Timeline position'"
        />
        <div class="scrubber-track">
          <div class="scrubber-progress" [style.width.%]="progressPercent()"></div>
        </div>
      </div>

      <span class="time-display">{{ formattedDuration() }}</span>
    </div>
  `,
  styles: [
    `
      .timeline {
        display: flex;
        align-items: center;
        gap: 16px;
        height: 60px;
        padding: 0 20px;
        background: var(--timeline-bg, #11111b);
        border-bottom: 1px solid var(--border-color, #313244);
      }

      .play-button {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 36px;
        height: 36px;
        border: none;
        border-radius: 50%;
        background: var(--accent-color, #cba6f7);
        color: var(--timeline-bg, #11111b);
        cursor: pointer;
        transition: transform 0.1s, background 0.2s;
      }

      .play-button:hover {
        background: var(--accent-hover, #b4befe);
        transform: scale(1.05);
      }

      .play-button:active {
        transform: scale(0.95);
      }

      .play-button svg {
        width: 16px;
        height: 16px;
      }

      .time-display {
        font-family: 'JetBrains Mono', monospace;
        font-size: 13px;
        color: var(--text-secondary, #a6adc8);
        min-width: 70px;
      }

      .scrubber-container {
        flex: 1;
        position: relative;
        height: 20px;
        display: flex;
        align-items: center;
      }

      .scrubber {
        position: absolute;
        width: 100%;
        height: 100%;
        opacity: 0;
        cursor: pointer;
        z-index: 2;
        margin: 0;
      }

      .scrubber-track {
        position: absolute;
        width: 100%;
        height: 4px;
        background: var(--track-bg, #313244);
        border-radius: 2px;
        overflow: hidden;
      }

      .scrubber-progress {
        height: 100%;
        background: var(--accent-color, #cba6f7);
        border-radius: 2px;
        transition: width 0.05s linear;
      }

      .scrubber:hover + .scrubber-track {
        height: 6px;
      }

      .scrubber:hover + .scrubber-track .scrubber-progress {
        background: var(--accent-hover, #b4befe);
      }

      .divider {
        width: 1px;
        height: 24px;
        background: var(--border-color, #313244);
      }
    `,
  ],
})
export class TimelineComponent {
  readonly isPlaying = signal(false);
  readonly currentTime = signal(323); // 5:23 in seconds
  readonly duration = signal(767); // 12:47 in seconds

  readonly progressPercent = computed(() => {
    const dur = this.duration();
    return dur > 0 ? (this.currentTime() / dur) * 100 : 0;
  });

  readonly formattedCurrentTime = computed(() => this.formatTime(this.currentTime()));
  readonly formattedDuration = computed(() => this.formatTime(this.duration()));

  togglePlay(): void {
    this.isPlaying.update((v) => !v);
  }

  seek(time: number): void {
    this.currentTime.set(time);
  }

  private formatTime(seconds: number): string {
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  }
}
