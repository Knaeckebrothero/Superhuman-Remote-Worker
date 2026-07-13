import {Component} from '@angular/core';
import {ComponentFixture, TestBed} from '@angular/core/testing';
import {DomSanitizer, SafeResourceUrl} from '@angular/platform-browser';
import {afterEach, beforeEach, describe, expect, it} from 'vitest';
import {CanvasLiveAppRendererComponent} from './canvas-live-app-renderer.component';

@Component({
  standalone: true,
  imports: [CanvasLiveAppRendererComponent],
  template: `<app-canvas-live-app-renderer [src]="src" [title]="title" [warning]="warning" />`,
})
class LiveAppRendererHost {
  src!: SafeResourceUrl;
  title = 'Live Canvas application: Prototype';
  warning = 'Untrusted app — do not enter passwords or secrets.';
}

describe('Canvas live-app iframe boundary', () => {
  let fixture: ComponentFixture<LiveAppRendererHost>;

  beforeEach(() => {
    TestBed.configureTestingModule({imports: [LiveAppRendererHost]});
    fixture = TestBed.createComponent(LiveAppRendererHost);
    const sanitizer = TestBed.inject(DomSanitizer);
    fixture.componentInstance.src = sanitizer.bypassSecurityTrustResourceUrl(
      'https://origin.canvas.userland.test/_canvas/bootstrap?token=one-time',
    );
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('uses a fixed sandbox and deny-by-default feature policy without a top-level escape', () => {
    const iframe = fixture.nativeElement.querySelector('iframe') as HTMLIFrameElement;
    expect(iframe.getAttribute('sandbox')).toBe('allow-scripts allow-same-origin allow-forms');
    expect(iframe.getAttribute('referrerpolicy')).toBe('no-referrer');
    expect(iframe.getAttribute('allow')).toBe(
      "camera 'none'; microphone 'none'; geolocation 'none'; " +
      "clipboard-read 'none'; clipboard-write 'none'",
    );
    expect(iframe.getAttribute('sandbox')).not.toContain('allow-top-navigation');
    expect(iframe.getAttribute('sandbox')).not.toContain('allow-popups');
    expect(iframe.getAttribute('sandbox')).not.toContain('allow-downloads');
    expect(iframe.getAttribute('title')).toBe('Live Canvas application: Prototype');
    const warning = fixture.nativeElement.querySelector('.live-app-warning') as HTMLElement;
    expect(warning.textContent?.trim()).toBe(
      'Untrusted app — do not enter passwords or secrets.',
    );
    expect(warning.getAttribute('role')).toBe('note');
  });
});
