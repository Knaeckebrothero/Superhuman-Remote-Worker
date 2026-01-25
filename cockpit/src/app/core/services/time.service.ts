import { Injectable, inject, signal, computed } from '@angular/core';
import { ApiService } from './api.service';

/**
 * Central time synchronization service for the cockpit.
 * Manages the global playhead position and broadcasts time changes to all components.
 */
@Injectable({ providedIn: 'root' })
export class TimeService {
  private readonly api = inject(ApiService);

  // Time range from audit data (first/last entry timestamps)
  readonly startTime = signal<Date | null>(null);
  readonly endTime = signal<Date | null>(null);

  // Current playhead position (ms offset from startTime)
  readonly currentOffsetMs = signal<number>(0);

  // Computed: current timestamp as ISO string
  readonly currentTimestamp = computed(() => {
    const start = this.startTime();
    const offset = this.currentOffsetMs();
    if (!start) return null;
    return new Date(start.getTime() + offset).toISOString();
  });

  // Computed: total duration in ms
  readonly durationMs = computed(() => {
    const start = this.startTime();
    const end = this.endTime();
    if (!start || !end) return 0;
    return end.getTime() - start.getTime();
  });

  // Computed: progress percentage (0-100)
  readonly progressPercent = computed(() => {
    const dur = this.durationMs();
    return dur > 0 ? (this.currentOffsetMs() / dur) * 100 : 0;
  });

  // Computed: whether we have a valid time range
  readonly hasTimeRange = computed(() => {
    return this.startTime() !== null && this.endTime() !== null;
  });

  // Playback state (placeholder - no auto-advance yet)
  readonly isPlaying = signal(false);

  // Counter incremented when global seek is triggered (allows components to detect global slider use)
  readonly globalSeekVersion = signal(0);

  // Auto-refresh interval handle
  private refreshInterval: ReturnType<typeof setInterval> | null = null;
  private currentJobId: string | null = null;

  /**
   * Seek to a specific offset in milliseconds.
   * Increments globalSeekVersion to signal that global slider was used.
   */
  seek(offsetMs: number): void {
    const dur = this.durationMs();
    this.currentOffsetMs.set(Math.max(0, Math.min(offsetMs, dur)));
    // Signal that global slider was used
    this.globalSeekVersion.update((v) => v + 1);
  }

  /**
   * Seek to the end (most recent state).
   */
  seekToEnd(): void {
    this.currentOffsetMs.set(this.durationMs());
  }

  /**
   * Seek to the start.
   */
  seekToStart(): void {
    this.currentOffsetMs.set(0);
  }

  /**
   * Toggle playback state (placeholder - auto-advance not implemented).
   */
  togglePlay(): void {
    this.isPlaying.update((v) => !v);
    // Playback implementation deferred
  }

  /**
   * Initialize time range from ISO timestamps.
   * Positions the playhead at the end (most recent state).
   */
  setTimeRange(startIso: string, endIso: string): void {
    this.startTime.set(new Date(startIso));
    this.endTime.set(new Date(endIso));
    // Start at end (most recent state)
    this.seekToEnd();
  }

  /**
   * Update only the end time (for live updates).
   * If currently at end, stay at end.
   */
  updateEndTime(endIso: string): void {
    const wasAtEnd = this.currentOffsetMs() >= this.durationMs() - 100; // Within 100ms of end
    this.endTime.set(new Date(endIso));
    if (wasAtEnd) {
      this.seekToEnd();
    }
  }

  /**
   * Clear all state when job is deselected.
   */
  clear(): void {
    this.stopRefresh();
    this.startTime.set(null);
    this.endTime.set(null);
    this.currentOffsetMs.set(0);
    this.isPlaying.set(false);
    this.currentJobId = null;
  }

  /**
   * Load time range for a job and start auto-refresh.
   */
  loadTimeRange(jobId: string): void {
    this.currentJobId = jobId;
    this.fetchTimeRange(jobId);
    this.startRefresh(jobId);
  }

  /**
   * Fetch time range from the API.
   */
  private fetchTimeRange(jobId: string): void {
    this.api.getAuditTimeRange(jobId).subscribe({
      next: (range) => {
        if (range && range.start && range.end) {
          const currentStart = this.startTime();
          if (!currentStart) {
            // First load - set full range
            this.setTimeRange(range.start, range.end);
          } else {
            // Update - only update end time
            this.updateEndTime(range.end);
          }
        }
      },
      error: (err) => {
        console.error('[TimeService] Failed to fetch time range:', err);
      },
    });
  }

  /**
   * Start auto-refresh with 15-second interval.
   */
  startRefresh(jobId: string): void {
    this.stopRefresh();
    this.refreshInterval = setInterval(() => {
      if (this.currentJobId === jobId) {
        this.fetchTimeRange(jobId);
      }
    }, 15000);
  }

  /**
   * Stop auto-refresh.
   */
  stopRefresh(): void {
    if (this.refreshInterval !== null) {
      clearInterval(this.refreshInterval);
      this.refreshInterval = null;
    }
  }
}
