import { Component, inject, computed, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MenuComponent } from '../menu/menu.component';
import { TimeService } from '../../core/services/time.service';
import { AuditService } from '../../core/services/audit.service';

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

      <select
        class="job-selector"
        [value]="audit.selectedJobId() || ''"
        (change)="onJobSelect($event)"
      >
        <option value="">Select a job...</option>
        @for (job of audit.jobs(); track job.id) {
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
        (click)="audit.refresh()"
        [disabled]="audit.isLoading()"
        title="Refresh jobs"
      >
        &#x21bb;
      </button>

      <div class="divider"></div>

      <button
        class="play-button"
        (click)="togglePlay()"
        [attr.aria-label]="isPlaying() ? 'Pause' : 'Play'"
        [disabled]="!hasTimeRange()"
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

      <div class="scrubber-container" [class.disabled]="!hasTimeRange()">
        <input
          type="range"
          class="scrubber"
          [min]="0"
          [max]="duration()"
          [ngModel]="currentTime()"
          (ngModelChange)="seek($event)"
          [attr.aria-label]="'Timeline position'"
          [disabled]="!hasTimeRange()"
        />
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
    `,
  ],
})
export class TimelineComponent implements OnInit {
  readonly time = inject(TimeService);
  readonly audit = inject(AuditService);

  // Convert ms to seconds for display
  readonly currentTime = computed(() => Math.floor(this.time.currentOffsetMs() / 1000));
  readonly duration = computed(() => Math.floor(this.time.durationMs() / 1000));
  readonly isPlaying = computed(() => this.time.isPlaying());
  readonly progressPercent = computed(() => this.time.progressPercent());
  readonly hasTimeRange = computed(() => this.time.hasTimeRange());

  readonly formattedCurrentTime = computed(() => this.formatTime(this.currentTime()));
  readonly formattedDuration = computed(() => this.formatTime(this.duration()));

  ngOnInit(): void {
    this.audit.loadJobs();
  }

  onJobSelect(event: Event): void {
    const value = (event.target as HTMLSelectElement).value;
    this.audit.selectJob(value || null);
  }

  togglePlay(): void {
    this.time.togglePlay();
  }

  seek(seconds: number): void {
    this.time.seek(seconds * 1000);
  }

  private formatTime(seconds: number): string {
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  }
}
