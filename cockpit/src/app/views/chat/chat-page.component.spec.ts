import {signal} from '@angular/core';
import {ComponentFixture, TestBed} from '@angular/core/testing';
import {ActivatedRoute, convertToParamMap, Router} from '@angular/router';
import {BehaviorSubject} from 'rxjs';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {CanvasService} from '../../core/services/canvas.service';
import {ErrorMessageService} from '../../core/services/error-message.service';
import {PersistentChatService} from '../../core/services/persistent-chat.service';
import {AppToastService} from '../../ui/toast';
import {ChatPageComponent} from './chat-page.component';

function createFixture(options: {draft?: boolean; threadId?: string} = {}): {
  fixture: ComponentFixture<ChatPageComponent>;
  params: BehaviorSubject<ReturnType<typeof convertToParamMap>>;
  chat: {
    threadId: ReturnType<typeof signal<string | null>>;
    isConnected: ReturnType<typeof signal<boolean>>;
    isStartingSession: ReturnType<typeof signal<boolean>>;
    connect: ReturnType<typeof vi.fn>;
    enterDraftSession: ReturnType<typeof vi.fn>;
    createAndConnect: ReturnType<typeof vi.fn>;
  };
  canvas: {selectThread: ReturnType<typeof vi.fn>};
  router: {navigate: ReturnType<typeof vi.fn>};
} {
  const params = new BehaviorSubject(
    convertToParamMap(options.threadId ? {threadId: options.threadId} : {}),
  );
  const chat = {
    threadId: signal<string | null>(null),
    isConnected: signal(false),
    isStartingSession: signal(false),
    connect: vi.fn().mockResolvedValue(undefined),
    enterDraftSession: vi.fn(),
    createAndConnect: vi.fn().mockResolvedValue('created-thread'),
  };
  const canvas = {selectThread: vi.fn()};
  const router = {navigate: vi.fn().mockResolvedValue(true)};

  // Remove the large child before TestBed queues standalone imports; this spec
  // exercises only route coordination in the thin wrapper.
  TestBed.overrideComponent(ChatPageComponent, {
    set: {imports: [], template: ''},
  });
  TestBed.configureTestingModule({
    providers: [
      {
        provide: ActivatedRoute,
        useValue: {
          snapshot: {data: {draft: options.draft === true}},
          paramMap: params.asObservable(),
        },
      },
      {provide: Router, useValue: router},
      {provide: PersistentChatService, useValue: chat},
      {provide: CanvasService, useValue: canvas},
      {provide: AppToastService, useValue: {danger: vi.fn()}},
      {provide: ErrorMessageService, useValue: {translate: vi.fn()}},
    ],
  });
  return {
    fixture: TestBed.createComponent(ChatPageComponent),
    params,
    chat,
    canvas,
    router,
  };
}

describe('ChatPageComponent Canvas route selection', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('switches chat and Canvas when Angular reuses the component for a new thread', () => {
    const {fixture, params, chat, canvas} = createFixture({threadId: 'thread-1'});
    fixture.detectChanges();

    expect(canvas.selectThread).toHaveBeenLastCalledWith('thread-1');
    expect(chat.connect).toHaveBeenLastCalledWith('thread-1');

    params.next(convertToParamMap({threadId: 'thread-2'}));
    expect(canvas.selectThread).toHaveBeenLastCalledWith('thread-2');
    expect(chat.connect).toHaveBeenLastCalledWith('thread-2');

    // takeUntilDestroyed prevents a reused route stream from acting after the
    // host view has gone away.
    fixture.destroy();
    params.next(convertToParamMap({threadId: 'thread-3'}));
    expect(canvas.selectThread).toHaveBeenCalledTimes(2);
    expect(chat.connect).toHaveBeenCalledTimes(2);
  });

  it('keeps the root route as a Canvas-free instant draft', () => {
    const {fixture, params, chat, canvas} = createFixture({draft: true});
    fixture.detectChanges();

    expect(canvas.selectThread).toHaveBeenCalledOnce();
    expect(canvas.selectThread).toHaveBeenCalledWith(null);
    expect(chat.enterDraftSession).toHaveBeenCalledOnce();
    expect(chat.connect).not.toHaveBeenCalled();

    params.next(convertToParamMap({threadId: 'ignored-on-draft-route'}));
    expect(canvas.selectThread).toHaveBeenCalledOnce();
  });
});
