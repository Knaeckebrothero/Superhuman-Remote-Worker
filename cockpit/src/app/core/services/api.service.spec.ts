import {afterEach, beforeEach, describe, expect, it} from 'vitest';
import {TestBed} from '@angular/core/testing';
import {provideHttpClient} from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import {TranslocoService} from '@jsverse/transloco';
import {firstValueFrom} from 'rxjs';
import {ApiService} from './api.service';
import {AppToastService} from '../../ui/toast';
import {ErrorMessageService} from './error-message.service';

describe('ApiService.transcribeVoice', () => {
  let api: ApiService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        ApiService,
        provideHttpClient(),
        provideHttpClientTesting(),
        {provide: AppToastService, useValue: {}},
        {provide: TranslocoService, useValue: {translate: (k: string) => k}},
        {provide: ErrorMessageService, useValue: {}},
      ],
    });
    api = TestBed.inject(ApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('POSTs the audio as multipart FormData to the thread transcribe endpoint', async () => {
    const file = new File([new Uint8Array([1, 2, 3])], 'voice.webm', {
      type: 'audio/webm',
    });
    const pending = firstValueFrom(api.transcribeVoice('thread-1', file));

    const req = httpMock.expectOne((r) =>
      r.url.endsWith('/persistent/threads/thread-1/transcribe'),
    );
    expect(req.request.method).toBe('POST');
    expect(req.request.body instanceof FormData).toBe(true);

    req.flush({text: 'hello world'});
    expect(await pending).toEqual({text: 'hello world'});
  });

  it('maps a 204 response to "unavailable"', async () => {
    const blob = new Blob([new Uint8Array([1])], {type: 'audio/webm'});
    const pending = firstValueFrom(api.transcribeVoice('t', blob));

    const req = httpMock.expectOne((r) => r.url.endsWith('/transcribe'));
    req.flush(null, {status: 204, statusText: 'No Content'});
    expect(await pending).toBe('unavailable');
  });

  it('returns null on transport error', async () => {
    const blob = new Blob([new Uint8Array([1])], {type: 'audio/webm'});
    const pending = firstValueFrom(api.transcribeVoice('t', blob));

    const req = httpMock.expectOne((r) => r.url.endsWith('/transcribe'));
    req.flush('error', {status: 500, statusText: 'Server Error'});
    expect(await pending).toBeNull();
  });
});
