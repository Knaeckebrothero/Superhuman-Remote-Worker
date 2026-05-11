import { Injectable, NgZone } from '@angular/core';
import { BehaviorSubject, Observable } from 'rxjs';
import { TimeBasedBarVisualizer } from '../audio/time-based-bar-visualizer';
import { VoiceAudioProcessor } from '../audio/voice-audio-processor';
import { RecordingConfig, RecordingResult, RecordingState } from '../models/recording.model';

/**
 * Microphone recording with live waveform visualization.
 *
 * Owns the MediaStream/MediaRecorder, an AudioContext for level
 * analysis, and a canvas-based bar visualizer that runs outside the
 * Angular zone for animation performance.
 */
@Injectable({ providedIn: 'root' })
export class VoiceRecordingService {
  private mediaRecorder: MediaRecorder | null = null;
  private audioStream: MediaStream | null = null;
  private audioChunks: Blob[] = [];
  private recordingState$ = new BehaviorSubject<RecordingState>({
    isRecording: false,
    duration: 0,
    audioLevel: 0,
  });
  private recordingStartTime = 0;
  private recordingTimer: ReturnType<typeof setInterval> | null = null;
  private audioContext: AudioContext | null = null;
  private analyser: AnalyserNode | null = null;
  private audioProcessor: VoiceAudioProcessor | null = null;
  private barVisualizer: TimeBasedBarVisualizer | null = null;

  constructor(private ngZone: NgZone) {}

  getRecordingState(): Observable<RecordingState> {
    return this.recordingState$.asObservable();
  }

  async startRecording(config: RecordingConfig, canvas?: HTMLCanvasElement): Promise<void> {
    try {
      const constraints = {
        audio:
          config.audioConstraints ?? {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
      };

      this.audioStream = await navigator.mediaDevices.getUserMedia(constraints);
      this.recordingStartTime = Date.now();
      this.audioChunks = [];

      this.setupAudioProcessing(this.audioStream);

      if (canvas && this.audioProcessor) {
        this.setupVisualization(canvas);
      }

      const mimeType = config.mimeType || this.getSupportedMimeType();
      this.mediaRecorder = new MediaRecorder(this.audioStream, { mimeType });
      this.mediaRecorder.ondataavailable = (event) => {
        if (event.data.size > 0) this.audioChunks.push(event.data);
      };
      this.mediaRecorder.start(100);

      this.startRecordingTimer();
      this.recordingState$.next({ ...this.recordingState$.value, isRecording: true });
    } catch (error) {
      console.error('Error starting recording:', error);
      this.cleanup();
      throw error;
    }
  }

  async stopRecording(): Promise<RecordingResult | null> {
    if (!this.mediaRecorder || !this.recordingState$.value.isRecording) {
      return null;
    }

    return new Promise<RecordingResult>((resolve) => {
      this.mediaRecorder!.onstop = () => {
        const duration = this.recordingState$.value.duration;
        const blob = new Blob(this.audioChunks, {
          type: this.mediaRecorder!.mimeType || 'audio/webm',
        });
        const result: RecordingResult = {
          blob,
          duration,
          mimeType: this.mediaRecorder!.mimeType || 'audio/webm',
        };
        this.cleanup();
        resolve(result);
      };
      this.mediaRecorder!.stop();
    });
  }

  cancelRecording(): void {
    if (this.mediaRecorder && this.recordingState$.value.isRecording) {
      this.mediaRecorder.stop();
      this.cleanup();
    }
  }

  private startRecordingTimer(): void {
    this.recordingTimer = setInterval(() => {
      const elapsed = Math.floor((Date.now() - this.recordingStartTime) / 1000);
      this.recordingState$.next({
        ...this.recordingState$.value,
        duration: elapsed,
        audioLevel: this.audioProcessor?.getVoiceLevel() ?? 0,
      });
    }, 100);
  }

  private setupAudioProcessing(stream: MediaStream): void {
    try {
      const Ctor = window.AudioContext || (window as any).webkitAudioContext;
      this.audioContext = new Ctor();
      this.analyser = this.audioContext.createAnalyser();
      const source = this.audioContext.createMediaStreamSource(stream);
      source.connect(this.analyser);
      this.audioProcessor = new VoiceAudioProcessor(this.analyser);
    } catch (error) {
      console.error('Error setting up audio processing:', error);
    }
  }

  private setupVisualization(canvas: HTMLCanvasElement): void {
    if (!this.audioProcessor) return;
    this.ngZone.runOutsideAngular(() => {
      this.barVisualizer = new TimeBasedBarVisualizer(
        canvas,
        () => this.audioProcessor?.getVoiceLevel() ?? 0,
      );
      this.barVisualizer.start();
    });
  }

  private getSupportedMimeType(): string {
    const types = [
      'audio/webm;codecs=opus',
      'audio/webm',
      'audio/ogg;codecs=opus',
      'audio/ogg',
      'audio/mp4',
      'audio/mpeg',
    ];
    for (const type of types) {
      if (MediaRecorder.isTypeSupported(type)) return type;
    }
    return '';
  }

  private cleanup(): void {
    if (this.recordingTimer) {
      clearInterval(this.recordingTimer);
      this.recordingTimer = null;
    }
    if (this.barVisualizer) {
      this.barVisualizer.stop();
      this.barVisualizer = null;
    }
    if (this.audioStream) {
      this.audioStream.getTracks().forEach((t) => t.stop());
      this.audioStream = null;
    }
    if (this.audioContext) {
      this.audioContext.close();
      this.audioContext = null;
      this.analyser = null;
      this.audioProcessor = null;
    }
    this.recordingState$.next({ isRecording: false, duration: 0, audioLevel: 0 });
    this.mediaRecorder = null;
    this.audioChunks = [];
  }
}
