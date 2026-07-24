import {Component} from '@angular/core';
import {ComponentFixture, TestBed} from '@angular/core/testing';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {CanvasOfficeSession} from '../../core/models/canvas.model';
import {CanvasOfficeRendererComponent} from './canvas-office-renderer.component';

const OFFICE_ORIGIN = 'https://office.example.test';
const SESSION: CanvasOfficeSession = {
  urlsrc: `${OFFICE_ORIGIN}/browser/version-hash/cool.html?`,
  WOPISrc: 'http://srw-orchestrator:8085/wopi/files/abc123',
  access_token: 'signed-wopi-token',
  access_token_ttl: 1_721_858_400_000,
};

@Component({
  standalone: true,
  imports: [CanvasOfficeRendererComponent],
  template: `
    <app-canvas-office-renderer
      [session]="session"
      [officeOrigin]="officeOrigin"
      [title]="title"
      (documentLoaded)="loaded += 1" />
  `,
})
class OfficeRendererHost {
  session = SESSION;
  officeOrigin = OFFICE_ORIGIN;
  title = 'Office document: Quarterly report';
  loaded = 0;
}

describe('Canvas Office iframe boundary', () => {
  let fixture: ComponentFixture<OfficeRendererHost>;
  let submit: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    submit = vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(() => undefined);
    TestBed.configureTestingModule({imports: [OfficeRendererHost]});
    fixture = TestBed.createComponent(OfficeRendererHost);
    fixture.detectChanges();
  });

  afterEach(() => {
    fixture.destroy();
    submit.mockRestore();
    TestBed.resetTestingModule();
  });

  it('binds the frame before form-posting only the standard token fields', async () => {
    const iframe = fixture.nativeElement.querySelector('iframe') as HTMLIFrameElement;
    const form = fixture.nativeElement.querySelector('form') as HTMLFormElement;
    expect(submit).not.toHaveBeenCalled();

    await fixture.whenStable();
    await new Promise(resolve => setTimeout(resolve, 0));
    fixture.detectChanges();

    expect(submit).toHaveBeenCalledOnce();
    expect(form.method.toLowerCase()).toBe('post');
    expect(form.target).toBe(iframe.name);
    expect(form.action).toBe(
      `${OFFICE_ORIGIN}/browser/version-hash/cool.html?` +
      'WOPISrc=http%3A%2F%2Fsrw-orchestrator%3A8085%2Fwopi%2Ffiles%2Fabc123',
    );
    const fields = Array.from(form.querySelectorAll('input')).map(input => [
      input.name,
      input.value,
    ]);
    expect(fields).toEqual([
      ['access_token', 'signed-wopi-token'],
      ['access_token_ttl', '1721858400000'],
    ]);
    expect(iframe.getAttribute('sandbox')).toBe(
      'allow-scripts allow-same-origin allow-forms',
    );
    expect(iframe.getAttribute('referrerpolicy')).toBe('no-referrer');
    expect(iframe.getAttribute('title')).toBe('Office document: Quarterly report');
  });

  it('handshakes only after exact source, origin, and VersionStates messages', async () => {
    await fixture.whenStable();
    await Promise.resolve();
    const iframe = fixture.nativeElement.querySelector('iframe') as HTMLIFrameElement;
    const frameWindow = iframe.contentWindow!;
    const postMessage = vi.spyOn(frameWindow, 'postMessage');
    const frameReady = {
      MessageId: 'App_LoadingStatus',
      SendTime: 1,
      Values: {Status: 'Frame_Ready', Features: {VersionStates: true}},
    };
    const loaded = {
      MessageId: 'App_LoadingStatus',
      SendTime: 2,
      Values: {Status: 'Document_Loaded', DocumentLoadedTime: 2},
    };

    window.dispatchEvent(new MessageEvent('message', {
      source: window,
      origin: OFFICE_ORIGIN,
      data: frameReady,
    }));
    window.dispatchEvent(new MessageEvent('message', {
      source: frameWindow,
      origin: 'https://evil.example.test',
      data: frameReady,
    }));
    window.dispatchEvent(new MessageEvent('message', {
      source: frameWindow,
      origin: OFFICE_ORIGIN,
      data: {...frameReady, extra: true},
    }));
    window.dispatchEvent(new MessageEvent('message', {
      source: frameWindow,
      origin: OFFICE_ORIGIN,
      data: loaded,
    }));
    expect(postMessage).not.toHaveBeenCalled();

    window.dispatchEvent(new MessageEvent('message', {
      source: frameWindow,
      origin: OFFICE_ORIGIN,
      data: JSON.stringify(frameReady),
    }));
    window.dispatchEvent(new MessageEvent('message', {
      source: frameWindow,
      origin: OFFICE_ORIGIN,
      data: JSON.stringify(loaded),
    }));

    expect(postMessage).toHaveBeenCalledOnce();
    const [payload, targetOrigin] = postMessage.mock.calls[0];
    expect(targetOrigin).toBe(OFFICE_ORIGIN);
    expect(typeof payload).toBe('string');
    expect(JSON.parse(payload as string)).toEqual({
      MessageId: 'Host_PostmessageReady',
      SendTime: expect.any(Number),
      Values: {},
    });
    expect(fixture.componentInstance.loaded).toBe(1);
  });
});
