import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  EffectRef,
  Injector,
  OnDestroy,
  ViewChild,
  computed,
  effect,
  inject,
} from '@angular/core';
import { TranslocoPipe } from '@jsverse/transloco';
import { AppSpinnerComponent } from '../../ui/spinner';
import {
  CanvasBrowserConnectionStatus,
  CanvasBrowserController,
} from './canvas-browser.controller';

const BROWSER_STATUS_KEYS: Record<CanvasBrowserConnectionStatus, string> = {
  idle: 'canvas.browser.status.connecting',
  connecting: 'canvas.browser.status.connecting',
  ready: 'canvas.browser.status.ready',
  reconnecting: 'canvas.browser.status.reconnecting',
  ended: 'canvas.browser.status.ended',
  viewer_limit: 'canvas.browser.status.viewerLimit',
  unauthorized: 'canvas.browser.status.unauthorized',
  unavailable: 'canvas.browser.status.unavailable',
  error: 'canvas.browser.status.error',
};

export function canvasBrowserStatusKey(status: CanvasBrowserConnectionStatus): string {
  return BROWSER_STATUS_KEYS[status];
}

export function paintBrowserBitmap(canvas: HTMLCanvasElement, bitmap: ImageBitmap | null): void {
  const context = canvas.getContext('2d');
  if (!context) return;
  if (!bitmap) {
    context.clearRect(0, 0, canvas.width, canvas.height);
    return;
  }
  // The decoded image is authoritative for the backing store. Protocol
  // metadata is intentionally absent from this paint path.
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  context.clearRect(0, 0, bitmap.width, bitmap.height);
  context.drawImage(bitmap, 0, 0);
}

/** Trusted view-only chrome and bitmap surface for the shared browser. */
@Component({
  selector: 'app-canvas-browser-renderer',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AppSpinnerComponent, TranslocoPipe],
  template: `
    <section class="browser-renderer" [attr.data-connection-status]="browser.connectionStatus()">
      <header class="browser-toolbar">
        <div class="browser-page">
          <strong class="browser-title">
            {{ browser.pageState()?.title || ('canvas.browser.untitled' | transloco) }}
          </strong>
          <span class="browser-url">
            {{ browser.pageState()?.url || ('canvas.browser.noUrl' | transloco) }}
          </span>
        </div>
        <div class="browser-indicators">
          @if (browser.pageState()?.loading) {
            <span class="browser-loading">
              <app-spinner size="sm" tone="accent" />
              {{ 'canvas.browser.loading' | transloco }}
            </span>
          }
          <span class="browser-connection">
            {{ connectionStatusKey() | transloco }}
          </span>
          @if (batonKey(); as key) {
            <span class="browser-baton" [attr.data-baton]="browser.pageState()?.baton">
              {{ key | transloco }}
            </span>
          }
        </div>
      </header>

      <div class="browser-stage">
        <canvas
          #surface
          class="browser-surface"
          tabindex="0"
          [style.aspect-ratio]="aspectRatio()"
          [attr.aria-label]="'canvas.browser.surfaceLabel' | transloco"
        >
        </canvas>
        @if (!browser.frame()) {
          <div class="browser-empty-state" role="status">
            @if (isConnecting()) {
              <app-spinner size="md" tone="accent" />
            }
            <span>{{ connectionStatusKey() | transloco }}</span>
          </div>
        }
      </div>
    </section>
  `,
  styleUrl: './canvas-browser-renderer.component.scss',
})
export class CanvasBrowserRendererComponent implements AfterViewInit, OnDestroy {
  readonly browser = inject(CanvasBrowserController);
  private readonly injector = inject(Injector);
  private paintEffect: EffectRef | null = null;
  @ViewChild('surface', { static: true })
  private surface!: ElementRef<HTMLCanvasElement>;

  readonly aspectRatio = computed(() => {
    const viewport = this.browser.pageState()?.viewport;
    return viewport ? `${viewport.width} / ${viewport.height}` : '16 / 9';
  });
  readonly connectionStatusKey = computed(() =>
    canvasBrowserStatusKey(this.browser.connectionStatus()),
  );
  readonly batonKey = computed(() => {
    const baton = this.browser.pageState()?.baton;
    return baton === 'agent'
      ? 'canvas.browser.baton.agent'
      : baton === 'user'
        ? 'canvas.browser.baton.user'
        : null;
  });
  readonly isConnecting = computed(() => {
    const status = this.browser.connectionStatus();
    return status === 'connecting' || status === 'reconnecting';
  });

  ngAfterViewInit(): void {
    this.paintEffect = effect(
      () => paintBrowserBitmap(this.surface.nativeElement, this.browser.frame()),
      { injector: this.injector },
    );
  }

  ngOnDestroy(): void {
    this.paintEffect?.destroy();
    this.paintEffect = null;
  }
}
