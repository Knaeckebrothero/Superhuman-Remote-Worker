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

describe('ApiService.planTTS', () => {
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

  it('POSTs the content and returns the chunks + rewritten flag', async () => {
    const pending = firstValueFrom(api.planTTS('thread-1', 'a long message'));
    const req = httpMock.expectOne((r) =>
      r.url.endsWith('/persistent/threads/thread-1/tts/plan'),
    );
    expect(req.request.method).toBe('POST');
    expect(req.request.body.content).toBe('a long message');
    req.flush({chunks: ['part one', 'part two'], rewritten: true});
    expect(await pending).toEqual({chunks: ['part one', 'part two'], rewritten: true});
  });

  it('defaults rewritten to false when the field is absent', async () => {
    const pending = firstValueFrom(api.planTTS('t', 'x'));
    const req = httpMock.expectOne((r) => r.url.endsWith('/tts/plan'));
    req.flush({chunks: ['only part']});
    expect(await pending).toEqual({chunks: ['only part'], rewritten: false});
  });

  it('maps a 204 response to "unavailable"', async () => {
    const pending = firstValueFrom(api.planTTS('t', 'x'));
    const req = httpMock.expectOne((r) => r.url.endsWith('/tts/plan'));
    req.flush(null, {status: 204, statusText: 'No Content'});
    expect(await pending).toBe('unavailable');
  });

  it('returns null on error', async () => {
    const pending = firstValueFrom(api.planTTS('t', 'x'));
    const req = httpMock.expectOne((r) => r.url.endsWith('/tts/plan'));
    req.flush('boom', {status: 502, statusText: 'Bad Gateway'});
    expect(await pending).toBeNull();
  });
});

describe('ApiService audit id normalization (Mongo _id / Postgres id)', () => {
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

  it('coerces a Postgres integer id + nested request_id to strings (audit)', async () => {
    const pending = firstValueFrom(api.getJobAudit('job-1'));
    const req = httpMock.expectOne((r) => r.url.endsWith('/jobs/job-1/audit'));
    req.flush({
      entries: [{id: 4271, step_type: 'llm', llm: {request_id: 99}}],
      total: 1,
      page: 1,
      pageSize: 50,
      hasMore: false,
    });
    const res = await pending;
    expect(res.entries[0]._id).toBe('4271');
    expect(typeof res.entries[0]._id).toBe('string');
    expect(res.entries[0].llm!.request_id).toBe('99');
  });

  it('leaves a Mongo ObjectId _id untouched (audit)', async () => {
    const pending = firstValueFrom(api.getJobAudit('job-1'));
    const req = httpMock.expectOne((r) => r.url.endsWith('/jobs/job-1/audit'));
    req.flush({
      entries: [{_id: '507f1f77bcf86cd799439011', step_type: 'tool'}],
      total: 1,
      page: 1,
      pageSize: 50,
      hasMore: false,
    });
    const res = await pending;
    expect(res.entries[0]._id).toBe('507f1f77bcf86cd799439011');
  });

  it('coerces chat id + request_id to strings', async () => {
    const pending = firstValueFrom(api.getChatHistory('job-1'));
    const req = httpMock.expectOne((r) => r.url.endsWith('/jobs/job-1/chat'));
    req.flush({
      entries: [{id: 5, request_id: 88}],
      total: 1,
      page: 1,
      pageSize: 50,
      hasMore: false,
    });
    const res = await pending;
    expect(res.entries[0]._id).toBe('5');
    expect(res.entries[0].request_id).toBe('88');
  });

  it('coerces a single LLM request id to string', async () => {
    const pending = firstValueFrom(api.getRequest('7'));
    const req = httpMock.expectOne((r) => r.url.endsWith('/requests/7'));
    req.flush({id: 7, job_id: 'job-1'});
    const res = await pending;
    expect(res!._id).toBe('7');
  });
});

describe('ApiService OKF Knowledge Base datasource index endpoints', () => {
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

  it('GETs credential-free datasource index status', async () => {
    const pending = firstValueFrom(api.getDatasourceIndexStatus('kb-1'));
    const req = httpMock.expectOne((r) =>
      r.url.endsWith('/datasources/kb-1/index-status'),
    );
    expect(req.request.method).toBe('GET');
    req.flush({
      datasource_id: 'kb-1',
      status: 'ready',
      indexed_commit: 'abc123',
      last_success_at: '2026-07-11T12:00:00Z',
    });
    expect((await pending)?.indexed_commit).toBe('abc123');
  });

  it('POSTs incremental and full datasource reindex requests', async () => {
    const incremental = firstValueFrom(api.reindexDatasource('kb-1'));
    const req1 = httpMock.expectOne((r) =>
      r.url.endsWith('/datasources/kb-1/reindex') && r.params.get('full') === 'false',
    );
    expect(req1.request.method).toBe('POST');
    req1.flush({status: 'ready', full: false, indexed_commit: 'abc123'});
    expect((await incremental)?.full).toBe(false);

    const full = firstValueFrom(api.reindexDatasource('kb-1', true));
    const req2 = httpMock.expectOne((r) => r.params.get('full') === 'true');
    req2.flush({status: 'ready', full: true, indexed_commit: 'def456'});
    expect((await full)?.full).toBe(true);
  });

  it('persists read-only access when linking an external KB to a project', async () => {
    const pending = firstValueFrom(
      api.linkProjectDatasource('project-1', 'kb-1', {read_only: true}),
    );
    const req = httpMock.expectOne((r) =>
      r.url.endsWith('/projects/project-1/datasources/kb-1'),
    );
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({read_only: true});
    req.flush({status: 'linked'});
    expect(await pending).toEqual({status: 'linked'});
  });
});
