import { Injectable } from '@angular/core';
import { BehaviorSubject, Observable } from 'rxjs';
import { DeviceCapabilities } from '../models/recording.model';

/**
 * Detects whether the running device exposes a camera, microphone,
 * geolocation, and whether it's mobile. Used by the persistent-chat
 * composer to show or hide camera/voice/location buttons.
 */
@Injectable({ providedIn: 'root' })
export class DeviceCapabilitiesService {
  private capabilities$ = new BehaviorSubject<DeviceCapabilities>({
    hasCamera: false,
    hasMultipleCameras: false,
    hasGeolocation: false,
    hasAudioInput: false,
    isMobile: false,
  });

  constructor() {
    this.detectCapabilities();
  }

  getCapabilities(): Observable<DeviceCapabilities> {
    return this.capabilities$.asObservable();
  }

  private async detectCapabilities(): Promise<void> {
    const capabilities: DeviceCapabilities = {
      hasCamera: false,
      hasMultipleCameras: false,
      hasGeolocation: false,
      hasAudioInput: false,
      isMobile: this.isMobileDevice(),
    };

    if (
      navigator.mediaDevices &&
      typeof navigator.mediaDevices.enumerateDevices === 'function' &&
      window.isSecureContext
    ) {
      try {
        const devices = await navigator.mediaDevices.enumerateDevices();
        const videoDevices = devices.filter((d) => d.kind === 'videoinput');
        const audioDevices = devices.filter((d) => d.kind === 'audioinput');
        capabilities.hasCamera = videoDevices.length > 0;
        capabilities.hasMultipleCameras = videoDevices.length > 1;
        capabilities.hasAudioInput = audioDevices.length > 0;
      } catch (error) {
        console.warn('Error detecting media devices:', error);
      }
    }

    capabilities.hasGeolocation = 'geolocation' in navigator && window.isSecureContext;

    this.capabilities$.next(capabilities);
  }

  private isMobileDevice(): boolean {
    return /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(
      navigator.userAgent,
    );
  }
}
