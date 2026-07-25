import {Component} from '@angular/core';
import {ComponentFixture, TestBed} from '@angular/core/testing';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {CanvasOfficeSession} from '../../core/models/canvas.model';
import {CanvasService, CanvasOfficeTurnAdapter} from '../../core/services/canvas.service';
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
      [editable]="editable"
      [presentationRevision]="presentationRevision"
      [refreshToken]="refreshToken"
      [reloadSession]="reloadSession"
      (documentLoaded)="loaded += 1"
      (modifiedChange)="modified = $event"
      (conflict)="conflicts.push($event)" />
  `,
})
class OfficeRendererHost {
  session = SESSION;
  officeOrigin = OFFICE_ORIGIN;
  title = 'Office document: Quarterly report';
  editable = true;
  presentationRevision = 1;
  refreshToken = vi.fn<() => Promise<CanvasOfficeSession | null>>();
  reloadSession = vi.fn();
  loaded = 0;
  modified = false;
  conflicts: string[] = [];
}

describe('Canvas Office iframe boundary', () => {
  let fixture: ComponentFixture<OfficeRendererHost>;
  let submit: ReturnType<typeof vi.spyOn>;
  let turnAdapter: CanvasOfficeTurnAdapter | null;
  let reconcileOfficeSave: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    turnAdapter = null;
    reconcileOfficeSave = vi.fn().mockResolvedValue(1);
    submit = vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(() => undefined);
    TestBed.configureTestingModule({
      imports: [OfficeRendererHost],
      providers: [{
        provide: CanvasService,
        useValue: {
          registerOfficeTurnAdapter: vi.fn((adapter: CanvasOfficeTurnAdapter) => {
            turnAdapter = adapter;
            return () => {
              if (turnAdapter === adapter) turnAdapter = null;
            };
          }),
          reconcileOfficeSave,
        },
      }],
    });
    fixture = TestBed.createComponent(OfficeRendererHost);
    fixture.componentInstance.refreshToken.mockResolvedValue({
      ...SESSION,
      access_token: 'renewed-token',
      access_token_ttl: 1_721_858_999_000,
    });
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

  it('handshakes on Document_Loaded only for the exact source and origin', async () => {
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
    expect(postMessage).toHaveBeenCalledOnce();

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

  it('tracks the Modified value and flushes Action_Save before a user turn', async () => {
    await fixture.whenStable();
    await Promise.resolve();
    const iframe = fixture.nativeElement.querySelector('iframe') as HTMLIFrameElement;
    const frameWindow = iframe.contentWindow!;
    const postMessage = vi.spyOn(frameWindow, 'postMessage');
    dispatchOffice(frameWindow, {
      MessageId: 'App_LoadingStatus',
      SendTime: 1,
      Values: {Status: 'Document_Loaded', DocumentLoadedTime: 1},
    });
    dispatchOffice(frameWindow, {
      MessageId: 'Doc_ModifiedStatus',
      SendTime: 2,
      Values: {Modified: true},
    });
    expect(fixture.componentInstance.modified).toBe(true);
    expect(turnAdapter).not.toBeNull();

    let settled = false;
    const saving = turnAdapter!.saveBeforeUserMessage().then(result => {
      settled = true;
      return result;
    });
    expect(settled).toBe(false);
    expect(lastPostedMessage(postMessage)).toEqual({
      MessageId: 'Action_Save',
      SendTime: expect.any(Number),
      Values: {DontSaveIfUnmodified: true, Notify: true},
    });

    dispatchOffice(frameWindow, {
      MessageId: 'Action_Save_Resp',
      SendTime: 3,
      Values: {success: false, result: 'unmodified', errorMsg: ''},
    });
    await expect(saving).resolves.toBe(true);
    expect(reconcileOfficeSave).toHaveBeenCalledOnce();
    expect(fixture.componentInstance.modified).toBe(false);
  });

  it('reads repeated modified=true values and keeps the turn paused for a second flush', async () => {
    await fixture.whenStable();
    await Promise.resolve();
    const iframe = fixture.nativeElement.querySelector('iframe') as HTMLIFrameElement;
    const frameWindow = iframe.contentWindow!;
    const postMessage = vi.spyOn(frameWindow, 'postMessage');
    dispatchOffice(frameWindow, {
      MessageId: 'App_LoadingStatus',
      SendTime: 1,
      Values: {Status: 'Document_Loaded', DocumentLoadedTime: 1},
    });
    dispatchOffice(frameWindow, {
      MessageId: 'Doc_ModifiedStatus',
      SendTime: 2,
      Values: {Modified: true},
    });

    const saving = turnAdapter!.saveBeforeUserMessage();
    dispatchOffice(frameWindow, {
      MessageId: 'Doc_ModifiedStatus',
      SendTime: 3,
      Values: {Modified: true},
    });
    dispatchOffice(frameWindow, {
      MessageId: 'Action_Save_Resp',
      SendTime: 4,
      Values: {success: true},
    });
    await Promise.resolve();
    await Promise.resolve();

    const saveMessages = postMessage.mock.calls
      .map(call => JSON.parse(call[0] as string) as {MessageId?: string})
      .filter(message => message.MessageId === 'Action_Save');
    expect(saveMessages).toHaveLength(2);

    dispatchOffice(frameWindow, {
      MessageId: 'Action_Save_Resp',
      SendTime: 5,
      Values: {success: false, result: 'unmodified'},
    });
    await expect(saving).resolves.toBe(true);
    expect(reconcileOfficeSave).toHaveBeenCalledTimes(2);
  });

  it('waits for Pre_Restore_Ack before reloading an updated agent version', async () => {
    await fixture.whenStable();
    await Promise.resolve();
    const iframe = fixture.nativeElement.querySelector('iframe') as HTMLIFrameElement;
    const frameWindow = iframe.contentWindow!;
    const postMessage = vi.spyOn(frameWindow, 'postMessage');
    dispatchOffice(frameWindow, {
      MessageId: 'App_LoadingStatus',
      SendTime: 1,
      Values: {Status: 'Frame_Ready', Features: {VersionStates: true}},
    });
    dispatchOffice(frameWindow, {
      MessageId: 'App_LoadingStatus',
      SendTime: 2,
      Values: {Status: 'Document_Loaded', DocumentLoadedTime: 2},
    });
    postMessage.mockClear();

    fixture.componentInstance.presentationRevision = 2;
    fixture.detectChanges();
    await fixture.whenStable();
    expect(lastPostedMessage(postMessage)).toEqual({
      MessageId: 'Host_VersionRestore',
      SendTime: expect.any(Number),
      Values: {Status: 'Pre_Restore'},
    });
    expect(fixture.componentInstance.reloadSession).not.toHaveBeenCalled();

    dispatchOffice(frameWindow, {
      MessageId: 'App_VersionRestore',
      SendTime: 3,
      Values: {Status: 'Pre_Restore_Ack'},
    });
    expect(fixture.componentInstance.reloadSession).toHaveBeenCalledOnce();
  });

  it('falls back to a fresh-token remount when VersionStates is absent', async () => {
    await fixture.whenStable();
    await Promise.resolve();
    const iframe = fixture.nativeElement.querySelector('iframe') as HTMLIFrameElement;
    const frameWindow = iframe.contentWindow!;
    const postMessage = vi.spyOn(frameWindow, 'postMessage');
    dispatchOffice(frameWindow, {
      MessageId: 'App_LoadingStatus',
      SendTime: 1,
      Values: {Status: 'Frame_Ready', Features: {}},
    });
    dispatchOffice(frameWindow, {
      MessageId: 'App_LoadingStatus',
      SendTime: 2,
      Values: {Status: 'Document_Loaded', DocumentLoadedTime: 2},
    });
    postMessage.mockClear();

    fixture.componentInstance.presentationRevision = 2;
    fixture.detectChanges();
    await fixture.whenStable();
    expect(fixture.componentInstance.reloadSession).toHaveBeenCalledOnce();
    expect(postMessage).not.toHaveBeenCalledWith(
      expect.stringContaining('Host_VersionRestore'),
      OFFICE_ORIGIN,
    );
  });

  it('renews an expiring token without remounting the editor', async () => {
    await fixture.whenStable();
    await Promise.resolve();
    const iframe = fixture.nativeElement.querySelector('iframe') as HTMLIFrameElement;
    const frameWindow = iframe.contentWindow!;
    const postMessage = vi.spyOn(frameWindow, 'postMessage');
    dispatchOffice(frameWindow, {
      MessageId: 'App_LoadingStatus',
      SendTime: 1,
      Values: {Status: 'Document_Loaded', DocumentLoadedTime: 1},
    });
    postMessage.mockClear();

    dispatchOffice(frameWindow, {
      MessageId: 'App_TokenExpiring',
      SendTime: 2,
      Values: {Timeout: 899_000},
    });
    await Promise.resolve();
    await Promise.resolve();

    expect(fixture.componentInstance.refreshToken).toHaveBeenCalledOnce();
    expect(lastPostedMessage(postMessage)).toEqual({
      MessageId: 'Reset_Access_Token',
      SendTime: expect.any(Number),
      Values: {
        token: 'renewed-token',
        ttl: 1_721_858_999_000,
      },
    });
    expect(fixture.componentInstance.reloadSession).not.toHaveBeenCalled();
  });
});

function dispatchOffice(frameWindow: WindowProxy, data: unknown): void {
  window.dispatchEvent(new MessageEvent('message', {
    source: frameWindow,
    origin: OFFICE_ORIGIN,
    data,
  }));
}

function lastPostedMessage(postMessage: ReturnType<typeof vi.spyOn>): unknown {
  const call = postMessage.mock.calls.at(-1);
  expect(call?.[1]).toBe(OFFICE_ORIGIN);
  expect(typeof call?.[0]).toBe('string');
  return JSON.parse(call![0] as string);
}
