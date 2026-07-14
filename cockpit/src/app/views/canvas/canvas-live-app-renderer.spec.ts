import {Component} from '@angular/core';
import {ComponentFixture, TestBed} from '@angular/core/testing';
import {DomSanitizer, SafeResourceUrl} from '@angular/platform-browser';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {CanvasLiveAppRendererComponent} from './canvas-live-app-renderer.component';

@Component({
  standalone: true,
  imports: [CanvasLiveAppRendererComponent],
  template: `
    <app-canvas-live-app-renderer [src]="src" [title]="title" [warning]="warning"
      (frameBound)="onBound($event)" (frameUnbound)="unbound = $event"
      (frameMessage)="messages.push($event)" />
  `,
})
class LiveAppRendererHost {
  src!: SafeResourceUrl;
  title = 'Live Canvas application: Prototype';
  warning = 'Untrusted app — do not enter passwords or secrets.';
  bound: WindowProxy | null = null;
  unbound: WindowProxy | null = null;
  srcAtBind: string | null = 'not-bound';
  messages: MessageEvent<unknown>[] = [];

  onBound(frame: WindowProxy): void {
    this.bound = frame;
    this.srcAtBind = document.querySelector('app-canvas-live-app-renderer iframe')
      ?.getAttribute('src') ?? null;
  }
}

describe('Canvas live-app iframe boundary', () => {
  let fixture: ComponentFixture<LiveAppRendererHost>;

  beforeEach(() => {
    TestBed.configureTestingModule({imports: [LiveAppRendererHost]});
    fixture = TestBed.createComponent(LiveAppRendererHost);
    const sanitizer = TestBed.inject(DomSanitizer);
    fixture.componentInstance.src = sanitizer.bypassSecurityTrustResourceUrl(
      'https://origin.canvas.userland.test/_canvas/bootstrap' +
      '?attachment_id=54f4fd56-69d8-46c8-8ab7-a3349af0d784',
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

  it('binds the WindowProxy before mounting src and forwards only that frame', async () => {
    const iframe = fixture.nativeElement.querySelector('iframe') as HTMLIFrameElement;
    await fixture.whenStable();
    await Promise.resolve();
    fixture.detectChanges();

    expect(iframe.getAttribute('src')).toContain('/_canvas/bootstrap?attachment_id=');
    const frameWindow = fixture.componentInstance.bound!;
    expect(frameWindow).not.toBeNull();
    expect(fixture.componentInstance.srcAtBind).toBeNull();

    window.dispatchEvent(new MessageEvent('message', {
      source: window,
      origin: 'https://origin.canvas.userland.test',
      data: {ignored: true},
    }));
    expect(fixture.componentInstance.messages).toHaveLength(0);

    const accepted = new MessageEvent('message', {
      source: frameWindow,
      origin: 'https://origin.canvas.userland.test',
      data: {accepted: true},
    });
    window.dispatchEvent(accepted);
    expect(fixture.componentInstance.messages).toHaveLength(1);
    expect(fixture.componentInstance.messages[0] === accepted).toBe(true);

    const removeListener = vi.spyOn(window, 'removeEventListener');
    fixture.destroy();
    expect(fixture.componentInstance.unbound === frameWindow).toBe(true);
    expect(removeListener).toHaveBeenCalledWith('message', expect.any(Function));
    removeListener.mockRestore();
  });
});
