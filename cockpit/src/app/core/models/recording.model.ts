/**
 * Recording data models for voice messages in the persistent chat composer.
 */

export interface RecordingConfig {
  isHoldToRecord: boolean;
  maxDuration: number;
  mimeType?: string;
  audioConstraints?: MediaTrackConstraints;
}

export interface RecordingResult {
  blob: Blob;
  duration: number;
  mimeType: string;
}

export interface RecordingState {
  isRecording: boolean;
  duration: number;
  audioLevel: number;
}

export interface VisualizationBar {
  height: number;
  timestamp: number;
  x: number;
}

export interface DeviceCapabilities {
  hasCamera: boolean;
  hasMultipleCameras: boolean;
  hasGeolocation: boolean;
  hasAudioInput: boolean;
  isMobile: boolean;
}
