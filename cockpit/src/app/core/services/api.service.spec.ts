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

describe('ApiService.generateTTS', () => {
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

  it('POSTs the content and decodes the base64 audio into an MP3 Blob', async () => {
    const pending = firstValueFrom(
      api.generateTTS('thread-1', 'hello there', {language: 'en'}),
    );

    const req = httpMock.expectOne((r) =>
      r.url.endsWith('/persistent/threads/thread-1/tts'),
    );
    expect(req.request.method).toBe('POST');
    expect(req.request.body.content).toBe('hello there');
    expect(req.request.body.reformulate).toBe(true);

    // base64 of the three bytes [1, 2, 3].
    req.flush({text: 'spoken', audio: btoa('\x01\x02\x03')});

    const result = await pending;
    if (result === null || result === 'unavailable') {
      throw new Error('expected a {text, audio} result');
    }
    expect(result.text).toBe('spoken');
    expect(result.audio).toBeInstanceOf(Blob);
    expect(result.audio.type).toBe('audio/mpeg');
    expect(result.audio.size).toBe(3);
  });

  it('maps a 204 response to "unavailable"', async () => {
    const pending = firstValueFrom(api.generateTTS('t', 'x'));
    const req = httpMock.expectOne((r) => r.url.endsWith('/tts'));
    req.flush(null, {status: 204, statusText: 'No Content'});
    expect(await pending).toBe('unavailable');
  });

  it('returns null on a 502 synthesis failure', async () => {
    const pending = firstValueFrom(api.generateTTS('t', 'x'));
    const req = httpMock.expectOne((r) => r.url.endsWith('/tts'));
    req.flush('synthesis failed', {status: 502, statusText: 'Bad Gateway'});
    expect(await pending).toBeNull();
  });
});
