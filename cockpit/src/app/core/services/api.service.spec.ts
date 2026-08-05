import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {TestBed} from '@angular/core/testing';
import {provideHttpClient} from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import {TranslocoService} from '@jsverse/transloco';
import {firstValueFrom} from 'rxjs';
import {ApiService, SESSION_TOOL_GROUPS_TIMEOUT_MS} from './api.service';
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

describe('ApiService datasource policy endpoints', () => {
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

  it('requests the filtered cursor catalog', async () => {
    const pending = firstValueFrom(api.getDatasourceCatalog({
      q: 'prod', availability: 'projects', auto_attach: true, ownership: 'mine', limit: 25,
    }));
    const req = httpMock.expectOne(r => r.url.endsWith('/datasources/catalog'));
    expect(req.request.params.get('q')).toBe('prod');
    expect(req.request.params.get('availability')).toBe('projects');
    expect(req.request.params.get('auto_attach')).toBe('true');
    expect(req.request.params.get('ownership')).toBe('mine');
    req.flush({items: [], next_cursor: 'next'});
    await expect(pending).resolves.toEqual({items: [], next_cursor: 'next'});
  });

  it('preserves repeated project ids and lets eligibility failures propagate', async () => {
    const pending = firstValueFrom(api.getEligibleDatasources(['p1', 'p2']));
    const req = httpMock.expectOne(r => r.url.endsWith('/datasources/eligible'));
    expect(req.request.params.getAll('project_id')).toEqual(['p1', 'p2']);
    req.flush({detail: 'unavailable'}, {status: 503, statusText: 'Unavailable'});
    await expect(pending).rejects.toBeTruthy();
  });

  it('loads retained project targets and propagates policy-write conflicts', async () => {
    const targets = firstValueFrom(api.getLinkableDatasourceProjects({
      datasourceId: 'ds1', q: 'app', cursor: 'c1', limit: 20,
    }));
    const read = httpMock.expectOne(r => r.url.endsWith('/projects/linkable-datasource-targets'));
    expect(read.request.params.get('datasource_id')).toBe('ds1');
    expect(read.request.params.get('q')).toBe('app');
    expect(read.request.params.get('cursor')).toBe('c1');
    read.flush({items: [], next_cursor: null});
    await targets;

    const update = firstValueFrom(api.updateDatasource('ds1', {
      scope_mode: 'projects', project_ids: ['p1'], policy_revision: 3,
    }));
    const write = httpMock.expectOne(r => r.url.endsWith('/datasources/ds1'));
    write.flush({detail: 'stale policy'}, {status: 409, statusText: 'Conflict'});
    await expect(update).rejects.toBeTruthy();
  });

  it('requests a cursor page of linkable connectors for one project', async () => {
    const pending = firstValueFrom(api.getLinkableProjectDatasources('project-1', {
      q: 'database', cursor: 'cursor-1', limit: 25,
    }));
    const req = httpMock.expectOne(
      r => r.url.endsWith('/projects/project-1/linkable-datasources'),
    );
    expect(req.request.params.get('q')).toBe('database');
    expect(req.request.params.get('cursor')).toBe('cursor-1');
    expect(req.request.params.get('limit')).toBe('25');
    req.flush({items: [], next_cursor: 'cursor-2'});
    await expect(pending).resolves.toEqual({items: [], next_cursor: 'cursor-2'});
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

describe('ApiService.getSessionToolGroups', () => {
  let api: ApiService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    vi.useFakeTimers();
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

  afterEach(() => {
    vi.useRealTimers();
  });

  it('returns the WHOLE answer, not just the four booleans', async () => {
    // `tool_groups` was all the old transport carried, which is why the live
    // pane could show four of twenty-five categories and say nothing about the
    // rest. The provenance, the per-category reasons and the write vocabulary
    // all live in the fields it dropped.
    const body = {
      thread_id: 'thread-1',
      source: 'resolved',
      origin: 'agent_partial',
      observed_at: null,
      degraded_reason: 'this agent image predates GET /session/toolset',
      enumerate_only: {shell: ['run_command']},
      tool_groups: {canvas: true},
      categories: {
        canvas: {state: 'on', settable: true, reason: null, decided_by: 'base', tools: ['get_canvas']},
      },
    };
    const pending = firstValueFrom(api.getSessionToolGroups('thread-1'));
    httpMock
      .expectOne((r) => r.url.endsWith('/persistent/threads/thread-1/tool-groups'))
      .flush(body);
    await expect(pending).resolves.toEqual(body);
    httpMock.verify();
  });

  it('gives up after a deadline instead of hanging the settings pane', async () => {
    // The server now probes the session pod for its real bound toolset, so
    // this request can hang on a wedged agent. `loadThread` forkJoins it and
    // anchors `lastApplied` only once both arms settle — without a deadline
    // the baseline stays unanchored and every later edit is swallowed.
    const pending = firstValueFrom(api.getSessionToolGroups('thread-1'));
    const req = httpMock.expectOne((r) =>
      r.url.endsWith('/persistent/threads/thread-1/tool-groups'),
    );

    expect(req.cancelled).toBe(false);

    await vi.advanceTimersByTimeAsync(SESSION_TOOL_GROUPS_TIMEOUT_MS + 1);

    // Resolves (to the null fallback) rather than never settling, and the
    // in-flight request is actually aborted rather than left dangling.
    await expect(pending).resolves.toBeNull();
    expect(req.cancelled).toBe(true);
    httpMock.verify();
  });

  it('sits above the server-side probe budget so a slow-but-live agent wins', () => {
    expect(SESSION_TOOL_GROUPS_TIMEOUT_MS).toBeGreaterThan(3000);
  });

  it('previewToolGroups POSTs the selection and returns the forecast', async () => {
    const body = {
      source: 'resolved',
      origin: 'prediction',
      observed_at: null,
      prediction_reason: 'no agent exists for an unsaved session',
      enumerate_only: {shell: ['run_command']},
      tool_groups: {canvas: true},
      categories: {
        canvas: {state: 'on', settable: true, reason: null, decided_by: 'base', tools: ['get_canvas']},
      },
    };
    const pending = firstValueFrom(
      api.previewToolGroups({config_name: 'session_base', project_id: 'p1'}),
    );
    const req = httpMock.expectOne((r) => r.url.endsWith('/persistent/tool-groups/preview'));
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({config_name: 'session_base', project_id: 'p1'});
    req.flush(body);
    await expect(pending).resolves.toEqual(body);
    httpMock.verify();
  });

  it('previewToolGroups is deadline-bounded and fails silently to null', async () => {
    // The creation form blocks nothing on this, but a hung request that never
    // settles would leave the tool switches anchored to the config forever.
    const pending = firstValueFrom(api.previewToolGroups({config_name: 'session_base'}));
    const req = httpMock.expectOne((r) => r.url.endsWith('/persistent/tool-groups/preview'));
    await vi.advanceTimersByTimeAsync(SESSION_TOOL_GROUPS_TIMEOUT_MS + 1);
    await expect(pending).resolves.toBeNull();
    expect(req.cancelled).toBe(true);
    httpMock.verify();
  });
});
