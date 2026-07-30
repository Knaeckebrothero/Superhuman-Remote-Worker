import {beforeEach, describe, expect, it, vi} from 'vitest';
import {TestBed} from '@angular/core/testing';
import {Subject, of} from 'rxjs';
import {ChatTraceService} from './chat-trace.service';
import {ApiService} from './api.service';
import {ChatEntry, ChatHistoryResponse} from '../models/chat.model';

function entry(id: string, over: Partial<ChatEntry> = {}): ChatEntry {
  return {
    _id: id,
    job_id: 'job-1',
    agent_type: 'universal',
    timestamp: '2026-07-30T10:00:00Z',
    iteration: 1,
    model: 'm',
    inputs: [],
    response: {content_preview: 'ok', has_tool_calls: false},
    ...over,
  };
}

function page(
  entries: ChatEntry[],
  total: number,
  pageNo = 1,
): ChatHistoryResponse {
  return {entries, total, page: pageNo, pageSize: 100, hasMore: false};
}

describe('ChatTraceService', () => {
  let api: {
    getChatHistory: ReturnType<typeof vi.fn>;
    getChatEntry: ReturnType<typeof vi.fn>;
  };
  let svc: ChatTraceService;

  beforeEach(() => {
    api = {getChatHistory: vi.fn(), getChatEntry: vi.fn()};
    TestBed.configureTestingModule({
      providers: [ChatTraceService, {provide: ApiService, useValue: api}],
    });
    svc = TestBed.inject(ChatTraceService);
  });

  it('fetches lean pages driven by the selected job', async () => {
    api.getChatHistory.mockReturnValue(of(page([entry('1')], 2)));
    await svc.setJob('job-1');
    expect(api.getChatHistory).toHaveBeenCalledWith('job-1', 1, 100, true);
    expect(svc.rows().map((r) => r._id)).toEqual(['1']);
    expect(svc.total()).toBe(2);
    expect(svc.hasMore()).toBe(true);
  });

  it('appends the next lean page on loadMore', async () => {
    api.getChatHistory
      .mockReturnValueOnce(of(page([entry('1')], 2)))
      .mockReturnValueOnce(of(page([entry('2')], 2, 2)));
    await svc.setJob('job-1');
    await svc.loadMore();
    expect(api.getChatHistory).toHaveBeenLastCalledWith('job-1', 2, 100, true);
    expect(svc.rows().map((r) => r._id)).toEqual(['1', '2']);
    expect(svc.hasMore()).toBe(false);
  });

  it('hydrateEntry swaps the lean row for the full row in place', async () => {
    const lean = entry('7', {
      response: {content_preview: 'p', truncated: true, has_tool_calls: false},
    });
    api.getChatHistory.mockReturnValue(of(page([lean], 1)));
    await svc.setJob('job-1');

    const full = entry('7', {
      response: {content: 'FULL BODY', content_preview: 'p', has_tool_calls: false},
    });
    api.getChatEntry.mockReturnValue(of(full));

    expect(await svc.hydrateEntry('7')).toBe(true);
    expect(api.getChatEntry).toHaveBeenCalledWith('job-1', '7');
    expect(svc.rows()[0].response.content).toBe('FULL BODY');
  });

  it('dedupes concurrent hydrations of the same entry', async () => {
    api.getChatHistory.mockReturnValue(of(page([entry('7')], 1)));
    await svc.setJob('job-1');

    const pending = new Subject<ChatEntry | null>();
    api.getChatEntry.mockReturnValue(pending.asObservable());
    const a = svc.hydrateEntry('7');
    const b = svc.hydrateEntry('7');
    expect(api.getChatEntry).toHaveBeenCalledTimes(1);

    pending.next(entry('7'));
    pending.complete();
    expect(await a).toBe(true);
    expect(await b).toBe(true);
  });

  it('a hydration that loses to a job switch does not clobber rows', async () => {
    api.getChatHistory.mockReturnValue(of(page([entry('7')], 1)));
    await svc.setJob('job-1');

    const pending = new Subject<ChatEntry | null>();
    api.getChatEntry.mockReturnValue(pending.asObservable());
    const stale = svc.hydrateEntry('7');

    api.getChatHistory.mockReturnValue(of(page([entry('9')], 1)));
    await svc.setJob('job-2');

    pending.next(
      entry('7', {
        response: {content: 'STALE', content_preview: 'p', has_tool_calls: false},
      }),
    );
    pending.complete();
    expect(await stale).toBe(false);
    expect(svc.rows().map((r) => r._id)).toEqual(['9']);
  });

  it('hydrateEntry resolves false when the fetch fails', async () => {
    api.getChatHistory.mockReturnValue(of(page([entry('7')], 1)));
    await svc.setJob('job-1');
    api.getChatEntry.mockReturnValue(of(null));
    expect(await svc.hydrateEntry('7')).toBe(false);
  });
});
