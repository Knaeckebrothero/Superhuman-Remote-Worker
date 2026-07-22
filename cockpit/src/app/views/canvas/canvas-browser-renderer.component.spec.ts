import { ComponentFixture, TestBed } from '@angular/core/testing';
import { TranslocoTestingModule } from '@jsverse/transloco';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { signal, ɵresolveComponentResources } from '@angular/core';
import { BrowserPageState } from './canvas-browser-protocol';
import {
  CanvasBrowserConnectionStatus,
  CanvasBrowserController,
} from './canvas-browser.controller';
import { CanvasBrowserRendererComponent } from './canvas-browser-renderer.component';

const translations = {
  canvas: {
    browser: {
      untitled: 'Untitled page',
      noUrl: 'No page URL',
      loading: 'Page loading…',
      surfaceLabel: 'Shared browser page',
      baton: { agent: 'Agent is driving', user: "You're driving" },
      status: {
        connecting: 'Connecting',
        ready: 'Connected',
        reconnecting: 'Reconnecting',
        ended: 'Ended',
        viewerLimit: 'Viewer limit',
        unauthorized: 'Unauthorized',
        unavailable: 'Unavailable',
        error: 'Protocol error',
      },
    },
  },
};

function page(overrides: Partial<BrowserPageState> = {}): BrowserPageState {
  return {
    baton: 'agent',
    viewport: { width: 1280, height: 720 },
    url: 'https://example.test/form',
    title: 'Example form',
    loading: false,
    ...overrides,
  };
}

function bitmap(width: number, height: number): ImageBitmap {
  return { width, height, close: vi.fn() } as unknown as ImageBitmap;
}

describe('Canvas shared-browser renderer', () => {
  let fixture: ComponentFixture<CanvasBrowserRendererComponent>;
  let controller: {
    connectionStatus: ReturnType<typeof signal<CanvasBrowserConnectionStatus>>;
    pageState: ReturnType<typeof signal<BrowserPageState | null>>;
    frame: ReturnType<typeof signal<ImageBitmap | null>>;
  };
  let context: {
    clearRect: ReturnType<typeof vi.fn>;
    drawImage: ReturnType<typeof vi.fn>;
  };
  let getContext: ReturnType<typeof vi.spyOn>;

  beforeAll(async () => {
    await ɵresolveComponentResources(() => Promise.resolve(''));
  });

  beforeEach(async () => {
    controller = {
      connectionStatus: signal<CanvasBrowserConnectionStatus>('connecting'),
      pageState: signal<BrowserPageState | null>(page()),
      frame: signal<ImageBitmap | null>(null),
    };
    context = { clearRect: vi.fn(), drawImage: vi.fn() };
    getContext = vi
      .spyOn(HTMLCanvasElement.prototype, 'getContext')
      .mockReturnValue(context as unknown as CanvasRenderingContext2D);
    TestBed.configureTestingModule({
      imports: [
        CanvasBrowserRendererComponent,
        TranslocoTestingModule.forRoot({
          langs: { en: translations },
          translocoConfig: { availableLangs: ['en'], defaultLang: 'en' },
        }),
      ],
      providers: [{ provide: CanvasBrowserController, useValue: controller }],
    });
    await TestBed.compileComponents();
    fixture = TestBed.createComponent(CanvasBrowserRendererComponent);
    fixture.detectChanges();
    TestBed.flushEffects();
    fixture.detectChanges();
  });

  afterEach(() => {
    fixture?.destroy();
    getContext?.mockRestore();
    TestBed.resetTestingModule();
  });

  it('renders only trusted read-only chrome and one focusable bitmap canvas', () => {
    const root = fixture.nativeElement as HTMLElement;
    const surface = root.querySelector('canvas') as HTMLCanvasElement;

    expect(root.textContent).toContain('Example form');
    expect(root.textContent).toContain('https://example.test/form');
    expect(root.textContent).toContain('Agent is driving');
    expect(root.textContent).toContain('Connecting');
    expect(surface.tabIndex).toBe(0);
    expect(surface.getAttribute('aria-label')).toBe('Shared browser page');
    expect(root.querySelectorAll('canvas')).toHaveLength(1);
    expect(root.querySelector('iframe, img, object, embed')).toBeNull();
  });

  it('uses decoded bitmap dimensions for backing pixels despite metadata mismatch', async () => {
    const decoded = bitmap(640, 480);
    controller.frame.set(decoded);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    const surface = fixture.nativeElement.querySelector('canvas') as HTMLCanvasElement;

    expect(getContext).toHaveBeenCalled();
    expect(surface.width).toBe(640);
    expect(surface.height).toBe(480);
    expect(surface.style.aspectRatio).toBe('1280 / 720');
    expect(context.drawImage).toHaveBeenLastCalledWith(decoded, 0, 0);

    controller.frame.set(null);
    fixture.detectChanges();
    await fixture.whenStable();
    expect(context.clearRect).toHaveBeenLastCalledWith(0, 0, 640, 480);
  });

  it('shows page loading and the authoritative user baton', () => {
    controller.connectionStatus.set('ready');
    controller.pageState.set(page({ baton: 'user', loading: true }));
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain("You're driving");
    expect(fixture.nativeElement.textContent).toContain('Page loading…');
    expect(
      (fixture.nativeElement.querySelector('.browser-baton') as HTMLElement).dataset['baton'],
    ).toBe('user');
  });

  it.each([
    ['connecting', 'Connecting'],
    ['reconnecting', 'Reconnecting'],
    ['unavailable', 'Unavailable'],
    ['viewer_limit', 'Viewer limit'],
    ['error', 'Protocol error'],
    ['ended', 'Ended'],
  ] as const)('renders the %s non-frame state as text', (status, copy) => {
    controller.connectionStatus.set(status);
    controller.frame.set(null);
    fixture.detectChanges();

    const state = fixture.nativeElement.querySelector('.browser-empty-state') as HTMLElement;
    expect(state.getAttribute('role')).toBe('status');
    expect(state.textContent).toContain(copy);
    expect(fixture.nativeElement.querySelector('.browser-renderer').dataset.connectionStatus).toBe(
      status,
    );
  });
});
