import { Component, inject, computed, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MenuComponent } from '../menu/menu.component';
import { DataService } from '../../core/services/data.service';

/**
 * Timeline scrubber component for playback control.
 * Fixed 60px height bar at the top of the app.
 *
 * Now uses index-based navigation via DataService instead of
 * timestamp-based navigation via TimeService.
 */
@Component({
  selector: 'app-timeline',
  imports: [FormsModule, MenuComponent],
  template: `
    <div class="timeline">
      <app-menu />

      <div class="divider"></div>

      <select
        class="job-selector"
        [value]="data.currentJobId() || ''"
        (change)="onJobSelect($event)"
      >
        <option value="">Select a job...</option>
        @for (job of data.jobs(); track job.id) {
          <option [value]="job.id">
            {{ job.id.slice(0, 8) }}... | {{ job.status }}
            @if (job.audit_count !== null) {
              ({{ job.audit_count }} steps)
            }
          </option>
        }
      </select>
      <button
        class="refresh-btn"
        (click)="onRefresh()"
        [disabled]="data.isLoading()"
        title="Refresh jobs"
      >
        &#x21bb;
      </button>

      <div class="divider"></div>

      <button
        class="play-button"
        (click)="togglePlay()"
        [attr.aria-label]="isPlaying() ? 'Pause' : 'Play'"
        [disabled]="!hasEntries()"
      >
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

      <div class="scrubber-container" [class.disabled]="!hasEntries()">
        <input
          type="range"
          class="scrubber"
          [min]="0"
          [max]="data.maxIndex()"
          [ngModel]="data.sliderIndex()"
          (ngModelChange)="onSliderChange($event)"
          [attr.aria-label]="'Timeline position'"
          [disabled]="!hasEntries()"
        />
      </div>

      <span class="time-display">{{ formattedDuration() }}</span>

      <!-- Loading indicator -->
      @if (data.isLoading()) {
        <div class="loading-indicator">
          <span class="spinner-small"></span>
          @if (data.loadingProgress() > 0) {
            <span class="progress-text">{{ data.loadingProgress() }}%</span>
          }
        </div>
      }

      <!-- Cache indicator -->
      @if (data.isCached() && !data.isLoading()) {
        <span class="cache-indicator" title="Loaded from cache">&#x26A1;</span>
      }
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

      .play-button:disabled {
        opacity: 0.4;
        cursor: not-allowed;
        transform: none;
      }

      .play-button:disabled:hover {
        background: var(--accent-color, #cba6f7);
        transform: none;
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
        width: 100%;
        height: 6px;
        -webkit-appearance: none;
        appearance: none;
        background: var(--track-bg, #313244);
        border-radius: 3px;
        cursor: pointer;
        margin: 0;
      }

      .scrubber::-webkit-slider-thumb {
        -webkit-appearance: none;
        appearance: none;
        width: 14px;
        height: 14px;
        background: var(--accent-color, #cba6f7);
        border-radius: 50%;
        cursor: pointer;
        border: 2px solid var(--timeline-bg, #11111b);
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
      }

      .scrubber::-moz-range-thumb {
        width: 14px;
        height: 14px;
        background: var(--accent-color, #cba6f7);
        border-radius: 50%;
        cursor: pointer;
        border: 2px solid var(--timeline-bg, #11111b);
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
      }

      .scrubber:hover::-webkit-slider-thumb {
        background: var(--accent-hover, #b4befe);
        transform: scale(1.1);
      }

      .scrubber:hover::-moz-range-thumb {
        background: var(--accent-hover, #b4befe);
        transform: scale(1.1);
      }

      .scrubber:disabled {
        opacity: 0.4;
        cursor: not-allowed;
      }

      .scrubber:disabled::-webkit-slider-thumb {
        cursor: not-allowed;
      }

      .scrubber:disabled::-moz-range-thumb {
        cursor: not-allowed;
      }

      .scrubber-container.disabled {
        opacity: 0.4;
        pointer-events: none;
      }

      .divider {
        width: 1px;
        height: 24px;
        background: var(--border-color, #313244);
      }

      .job-selector {
        padding: 6px 12px;
        border: 1px solid var(--border-color, #313244);
        border-radius: 4px;
        background: var(--panel-bg, #181825);
        color: var(--text-primary, #cdd6f4);
        font-size: 12px;
        font-family: 'JetBrains Mono', monospace;
        cursor: pointer;
        min-width: 200px;
      }

      .job-selector:hover {
        border-color: var(--text-muted, #6c7086);
      }

      .job-selector:focus {
        outline: none;
        border-color: var(--accent-color, #cba6f7);
      }

      .refresh-btn {
        padding: 6px 10px;
        border: none;
        border-radius: 4px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        font-size: 14px;
        cursor: pointer;
        transition: all 0.15s ease;
      }

      .refresh-btn:hover:not(:disabled) {
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
      }

      .refresh-btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }

      .loading-indicator {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 11px;
        color: var(--text-muted, #6c7086);
      }

      .spinner-small {
        width: 14px;
        height: 14px;
        border: 2px solid var(--surface-0, #313244);
        border-top-color: var(--accent-color, #cba6f7);
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }

      @keyframes spin {
        to {
          transform: rotate(360deg);
        }
      }

      .progress-text {
        font-family: 'JetBrains Mono', monospace;
      }

      .cache-indicator {
        font-size: 14px;
        color: #a6e3a1;
        cursor: help;
      }
    `,
  ],
})
export class TimelineComponent implements OnInit {
  readonly data = inject(DataService);

  // Playback state (placeholder - auto-advance not implemented yet)
  readonly isPlaying = computed(() => false);

  // Whether we have entries loaded
  readonly hasEntries = computed(() => this.data.maxIndex() > 0);

  // Format current position as time from entry timestamp
  readonly formattedCurrentTime = computed(() => {
    const timestamp = this.data.currentTimestamp();
    if (!timestamp) {
      const index = this.data.sliderIndex();
      return `#${index}`;
    }
    return this.formatTimestamp(timestamp);
  });

  // Format total duration / max index
  readonly formattedDuration = computed(() => {
    const total = this.data.totalAuditEntries();
    return `${total} entries`;
  });

  ngOnInit(): void {
    this.data.loadJobs();
  }

  onJobSelect(event: Event): void {
    const value = (event.target as HTMLSelectElement).value;
    if (value) {
      this.data.loadJob(value);
    } else {
      this.data.clear();
    }
  }

  onSliderChange(index: number): void {
    this.data.setSliderIndex(index);
  }

  onRefresh(): void {
    this.data.loadJobs();
    if (this.data.currentJobId()) {
      this.data.refresh();
    }
  }

  togglePlay(): void {
    // Playback not implemented yet
    // Could auto-advance slider index at a rate
  }

  private formatTimestamp(isoString: string): string {
    const date = new Date(isoString);
    return date.toLocaleTimeString('en-GB', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  }
}
