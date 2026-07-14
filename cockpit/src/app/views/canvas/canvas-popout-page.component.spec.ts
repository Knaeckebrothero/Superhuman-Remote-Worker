import {TestBed} from '@angular/core/testing';
import {ActivatedRoute, convertToParamMap, Router} from '@angular/router';
import {BehaviorSubject} from 'rxjs';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {routes} from '../../app.routes';
import {authGuard} from '../../core/guards/auth.guard';
import {CanvasService} from '../../core/services/canvas.service';
import {
  CANVAS_POPOUT_RECONCILE_MS,
  CanvasPopoutPageComponent,
} from './canvas-popout-page.component';

describe('Canvas authenticated pop-out wrapper', () => {
  afterEach(() => {
    TestBed.resetTestingModule();
    vi.useRealTimers();
  });

  it('is reachable only through the authenticated session route', () => {
    const route = routes.find(candidate => candidate.path === 'sessions/:threadId/canvas');

    expect(route?.component).toBe(CanvasPopoutPageComponent);
    expect(route?.canActivate).toContain(authGuard);
    expect(route?.data?.['canvasPopout']).toBe(true);
  });

  it('selects the route thread and follows Angular param reuse', () => {
    const params = new BehaviorSubject(convertToParamMap({threadId: 'thread-1'}));
    const canvas = {selectThread: vi.fn(), threadId: vi.fn(() => 'thread-1')};
    const router = {navigate: vi.fn().mockResolvedValue(true)};
    TestBed.configureTestingModule({
      providers: [
        CanvasPopoutPageComponent,
        {provide: ActivatedRoute, useValue: {paramMap: params.asObservable()}},
        {provide: Router, useValue: router},
        {provide: CanvasService, useValue: canvas},
      ],
    });
    const component = TestBed.inject(CanvasPopoutPageComponent);

    component.ngOnInit();
    expect(canvas.selectThread).toHaveBeenLastCalledWith('thread-1');

    params.next(convertToParamMap({threadId: 'thread-2'}));
    expect(canvas.selectThread).toHaveBeenLastCalledWith('thread-2');
    expect(router.navigate).not.toHaveBeenCalled();
  });

  it('polls authoritative state only while visible and stops on destroy', () => {
    vi.useFakeTimers();
    const originalVisibility = Object.getOwnPropertyDescriptor(document, 'visibilityState');
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      value: 'visible',
    });
    const params = new BehaviorSubject(convertToParamMap({threadId: 'thread-1'}));
    const canvas = {
      selectThread: vi.fn(),
      threadId: vi.fn(() => 'thread-1'),
      reconcile: vi.fn(),
    };
    TestBed.configureTestingModule({
      providers: [
        CanvasPopoutPageComponent,
        {provide: ActivatedRoute, useValue: {paramMap: params.asObservable()}},
        {provide: Router, useValue: {navigate: vi.fn().mockResolvedValue(true)}},
        {provide: CanvasService, useValue: canvas},
      ],
    });
    TestBed.inject(CanvasPopoutPageComponent).ngOnInit();

    vi.advanceTimersByTime(CANVAS_POPOUT_RECONCILE_MS);
    expect(canvas.reconcile).toHaveBeenCalledOnce();

    TestBed.resetTestingModule();
    vi.advanceTimersByTime(CANVAS_POPOUT_RECONCILE_MS * 2);
    expect(canvas.reconcile).toHaveBeenCalledOnce();
    if (originalVisibility) {
      Object.defineProperty(document, 'visibilityState', originalVisibility);
    } else {
      Reflect.deleteProperty(document, 'visibilityState');
    }
  });
});
