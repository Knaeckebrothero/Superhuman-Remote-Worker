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
  signal,
} from '@angular/core';
import { TranslocoPipe } from '@jsverse/transloco';
import { AppButtonComponent } from '../../ui/button';
import { AppIconComponent } from '../../ui/icon';
import { AppIconButtonComponent } from '../../ui/icon-button';
import { AppSpinnerComponent } from '../../ui/spinner';
import {
  BrowserMouseButton,
  BrowserPoint,
  browserModifiers,
  browserMouseButton,
  browserPrintableText,
  browserWheelDeltas,
  mapBrowserPoint,
} from './canvas-browser-input';
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

interface CapturedBrowserPointer {
  readonly id: number;
  readonly button: BrowserMouseButton;
  point: BrowserPoint;
}

interface PendingBrowserMove extends BrowserPoint {
  readonly buttons: number;
  readonly modifiers: number;
}

interface PressedBrowserKey {
  readonly key: string;
  readonly code: string;
  readonly location: number;
}

/** Trusted view-only chrome and bitmap surface for the shared browser. */
@Component({
  selector: 'app-canvas-browser-renderer',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AppButtonComponent,
    AppIconComponent,
    AppIconButtonComponent,
    AppSpinnerComponent,
    TranslocoPipe,
  ],
  template: `
    <section class="browser-renderer" [attr.data-connection-status]="browser.connectionStatus()">
      <header class="browser-toolbar">
        <div class="browser-page">
          <strong class="browser-title">
            {{ browser.pageState()?.title || ('canvas.browser.untitled' | transloco) }}
          </strong>
          <form class="browser-navigation" (submit)="navigate($event)">
            <app-icon-button size="sm"
                             [ariaLabel]="'canvas.browser.toolbar.back' | transloco"
                             [tooltip]="'canvas.browser.toolbar.back' | transloco"
                             [disabled]="!canDrive()"
                             (clicked)="goBack()">
              <app-icon size="sm">arrow_back</app-icon>
            </app-icon-button>
            <app-icon-button size="sm"
                             [ariaLabel]="'canvas.browser.toolbar.reload' | transloco"
                             [tooltip]="'canvas.browser.toolbar.reload' | transloco"
                             [disabled]="!canDrive()"
                             (clicked)="reload()">
              <app-icon size="sm">refresh</app-icon>
            </app-icon-button>
            <label class="browser-url-label">
              <span>{{ 'canvas.browser.toolbar.address' | transloco }}</span>
              <input type="text" inputmode="url" autocomplete="off" spellcheck="false"
                     maxlength="8192" [value]="urlValue()"
                     [placeholder]="'canvas.browser.noUrl' | transloco"
                     [disabled]="!canDrive()"
                     (focus)="urlEditing.set(true)"
                     (blur)="urlEditing.set(false)"
                     (input)="onUrlInput($event)" />
            </label>
          </form>
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
              <span>{{ key | transloco }}</span>
              <app-button size="sm" variant="ghost" [disabled]="batonDisabled()"
                          (clicked)="toggleBaton()">
                {{ batonActionKey() | transloco }}
              </app-button>
            </span>
          }
        </div>
      </header>

      @if (navigationError(); as message) {
        <div class="browser-navigation-error" role="alert">
          <app-icon size="sm">warning</app-icon>
          <span>{{ 'canvas.browser.toolbar.navigationRejected' | transloco }} {{ message }}</span>
        </div>
      }

      <div class="browser-stage">
        <canvas
          #surface
          class="browser-surface"
          tabindex="0"
          [style.aspect-ratio]="aspectRatio()"
          [attr.aria-label]="'canvas.browser.surfaceLabel' | transloco"
          (pointermove)="onPointerMove($event)"
          (pointerdown)="onPointerDown($event)"
          (pointerup)="onPointerUp($event)"
          (pointercancel)="onPointerCancel($event)"
          (wheel)="onWheel($event)"
          (keydown)="onKeyDown($event)"
          (keyup)="onKeyUp($event)"
          (blur)="releaseHeldInput()"
          (contextmenu)="onContextMenu($event)"
          (compositionstart)="composing = true"
          (compositionend)="composing = false"
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
  private pointerFrame: number | null = null;
  private pendingPointerMove: PendingBrowserMove | null = null;
  private capturedPointer: CapturedBrowserPointer | null = null;
  private readonly pressedKeys = new Map<string, PressedBrowserKey>();
  @ViewChild('surface', { static: true })
  private surface!: ElementRef<HTMLCanvasElement>;

  readonly urlValue = signal('');
  readonly urlEditing = signal(false);
  composing = false;

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
  readonly canDrive = computed(() =>
    this.browser.connectionStatus() === 'ready' &&
    this.browser.pageState()?.baton === 'user' &&
    this.browser.pendingBaton() === null,
  );
  readonly batonDisabled = computed(() =>
    this.browser.connectionStatus() !== 'ready' ||
    this.browser.pageState() === null ||
    this.browser.pendingBaton() !== null,
  );
  readonly batonActionKey = computed(() =>
    this.browser.pageState()?.baton === 'user'
      ? 'canvas.browser.baton.release'
      : 'canvas.browser.baton.take',
  );
  readonly navigationError = computed(() =>
    this.browser.errorCode() === 'navigation_rejected'
      ? this.browser.errorMessage()
      : null,
  );

  private readonly visibilityListener = (): void => {
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') {
      this.releaseHeldInput();
    }
  };

  constructor() {
    effect(() => {
      const pageUrl = this.browser.pageState()?.url ?? '';
      if (!this.urlEditing()) this.urlValue.set(pageUrl);
    });
    effect(() => {
      const eligible = this.canDrive();
      if (!eligible) this.releaseHeldInput();
    });
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', this.visibilityListener);
    }
  }

  ngAfterViewInit(): void {
    this.paintEffect = effect(
      () => paintBrowserBitmap(this.surface.nativeElement, this.browser.frame()),
      { injector: this.injector },
    );
  }

  ngOnDestroy(): void {
    this.releaseHeldInput();
    this.cancelPointerFrame();
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', this.visibilityListener);
    }
    this.paintEffect?.destroy();
    this.paintEffect = null;
  }

  onUrlInput(event: Event): void {
    const target = event.target;
    if (target instanceof HTMLInputElement) this.urlValue.set(target.value);
  }

  navigate(event: Event): void {
    event.preventDefault();
    const url = this.urlValue().trim();
    if (!this.canDrive() || !url) return;
    this.browser.sendControl({op: 'navigate', url});
  }

  goBack(): void {
    if (this.canDrive()) this.browser.sendControl({op: 'back'});
  }

  reload(): void {
    if (this.canDrive()) this.browser.sendControl({op: 'reload'});
  }

  toggleBaton(): void {
    if (this.batonDisabled()) return;
    if (this.browser.pageState()?.baton === 'user') {
      this.releaseHeldInput();
      this.browser.sendControl({op: 'release_baton'});
    } else {
      this.browser.sendControl({op: 'take_baton'});
    }
  }

  onPointerMove(event: PointerEvent): void {
    if (!this.canDrive() || event.isPrimary === false) return;
    const point = this.mapPoint(event.clientX, event.clientY);
    if (!point) return;
    if (this.capturedPointer?.id === event.pointerId) this.capturedPointer.point = point;
    this.pendingPointerMove = {
      ...point,
      buttons: event.buttons,
      modifiers: browserModifiers(event),
    };
    if (this.pointerFrame !== null) return;
    this.pointerFrame = this.requestPointerFrame(() => {
      this.pointerFrame = null;
      this.flushPointerMove();
    });
  }

  onPointerDown(event: PointerEvent): void {
    if (!this.canDrive() || event.isPrimary === false) return;
    const button = browserMouseButton(event.button);
    const point = this.mapPoint(event.clientX, event.clientY);
    if (!button || !point || (this.capturedPointer && this.capturedPointer.id !== event.pointerId)) {
      return;
    }
    this.surface.nativeElement.focus({preventScroll: true});
    const sent = this.browser.sendInput({
      kind: 'mouse',
      params: {
        type: 'mousePressed',
        ...point,
        button,
        buttons: event.buttons,
        modifiers: browserModifiers(event),
        clickCount: Math.max(1, Math.min(3, Math.trunc(event.detail) || 1)),
      },
    });
    if (!sent) return;
    this.capturedPointer = {id: event.pointerId, button, point};
    try {
      this.surface.nativeElement.setPointerCapture?.(event.pointerId);
    } catch {
      // Capture may fail if the pointer ended between dispatch and this call.
    }
    event.preventDefault();
  }

  onPointerUp(event: PointerEvent): void {
    const captured = this.capturedPointer;
    if (!captured || captured.id !== event.pointerId) return;
    this.flushPointerMove();
    const point = this.mapPoint(event.clientX, event.clientY) ?? captured.point;
    if (this.canDrive()) {
      this.browser.sendInput({
        kind: 'mouse',
        params: {
          type: 'mouseReleased',
          ...point,
          button: captured.button,
          buttons: event.buttons,
          modifiers: browserModifiers(event),
          clickCount: Math.max(1, Math.min(3, Math.trunc(event.detail) || 1)),
        },
      });
      event.preventDefault();
    }
    this.clearCapturedPointer();
  }

  onPointerCancel(event: PointerEvent): void {
    const captured = this.capturedPointer;
    if (!captured || captured.id !== event.pointerId) return;
    this.flushPointerMove();
    if (this.canDrive()) {
      this.browser.sendInput({
        kind: 'mouse',
        params: {
          type: 'mouseReleased',
          ...captured.point,
          button: captured.button,
          buttons: 0,
          modifiers: browserModifiers(event),
          clickCount: 1,
        },
      });
    }
    this.clearCapturedPointer();
  }

  onWheel(event: WheelEvent): void {
    if (!this.canDrive() || !this.surfaceFocused()) return;
    const viewport = this.browser.pageState()?.viewport;
    const bounds = this.surface.nativeElement.getBoundingClientRect();
    const point = viewport
      ? mapBrowserPoint(event.clientX, event.clientY, bounds, viewport)
      : null;
    const deltas = viewport
      ? browserWheelDeltas(event.deltaX, event.deltaY, event.deltaMode, bounds, viewport)
      : null;
    if (!point || !deltas) return;
    if (this.browser.sendInput({
      kind: 'wheel',
      params: {...point, ...deltas, modifiers: browserModifiers(event)},
    })) {
      event.preventDefault();
    }
  }

  onKeyDown(event: KeyboardEvent): void {
    if (
      !this.canDrive() ||
      !this.surfaceFocused() ||
      this.composing ||
      event.isComposing
    ) {
      return;
    }
    const text = browserPrintableText(event);
    const params = {
      type: 'keyDown',
      key: event.key,
      code: event.code,
      location: event.location,
      autoRepeat: event.repeat,
      modifiers: browserModifiers(event),
      ...(text ? {text} : {}),
    };
    if (!this.browser.sendInput({kind: 'key', params})) return;
    this.pressedKeys.set(this.keyIdentity(event), {
      key: event.key,
      code: event.code,
      location: event.location,
    });
    event.preventDefault();
  }

  onKeyUp(event: KeyboardEvent): void {
    const identity = this.keyIdentity(event);
    const pressed = this.pressedKeys.get(identity);
    if (!pressed) return;
    this.pressedKeys.delete(identity);
    if (
      this.canDrive() &&
      this.surfaceFocused() &&
      !this.composing &&
      !event.isComposing &&
      this.browser.sendInput({
        kind: 'key',
        params: {
          type: 'keyUp',
          key: pressed.key,
          code: pressed.code,
          location: pressed.location,
          modifiers: browserModifiers(event),
        },
      })
    ) {
      event.preventDefault();
    }
  }

  onContextMenu(event: MouseEvent): void {
    if (this.canDrive() && this.surfaceFocused()) event.preventDefault();
  }

  /** Best-effort release for blur, baton/lifecycle changes, and destruction. */
  releaseHeldInput(): void {
    this.cancelPointerFrame();
    this.pendingPointerMove = null;
    const captured = this.capturedPointer;
    if (captured) {
      this.browser.sendInput({
        kind: 'mouse',
        params: {
          type: 'mouseReleased',
          ...captured.point,
          button: captured.button,
          buttons: 0,
          modifiers: 0,
          clickCount: 1,
        },
      });
      this.clearCapturedPointer();
    }
    const pressed = [...this.pressedKeys.values()];
    this.pressedKeys.clear();
    for (const key of pressed) {
      this.browser.sendInput({
        kind: 'key',
        params: {
          type: 'keyUp',
          key: key.key,
          code: key.code,
          location: key.location,
          modifiers: 0,
        },
      });
    }
  }

  private mapPoint(clientX: number, clientY: number): BrowserPoint | null {
    const viewport = this.browser.pageState()?.viewport;
    return viewport
      ? mapBrowserPoint(
          clientX,
          clientY,
          this.surface.nativeElement.getBoundingClientRect(),
          viewport,
        )
      : null;
  }

  private flushPointerMove(): void {
    this.cancelPointerFrame();
    const move = this.pendingPointerMove;
    this.pendingPointerMove = null;
    if (!move || !this.canDrive()) return;
    this.browser.sendInput({
      kind: 'mouse',
      params: {
        type: 'mouseMoved',
        x: move.x,
        y: move.y,
        buttons: move.buttons,
        modifiers: move.modifiers,
      },
    });
  }

  private clearCapturedPointer(): void {
    const captured = this.capturedPointer;
    this.capturedPointer = null;
    if (!captured) return;
    try {
      this.surface.nativeElement.releasePointerCapture?.(captured.id);
    } catch {
      // Capture may already have been released by the browser.
    }
  }

  private requestPointerFrame(callback: () => void): number {
    if (typeof requestAnimationFrame === 'function') return requestAnimationFrame(callback);
    return setTimeout(callback, 16) as unknown as number;
  }

  private cancelPointerFrame(): void {
    if (this.pointerFrame === null) return;
    if (typeof cancelAnimationFrame === 'function') cancelAnimationFrame(this.pointerFrame);
    else clearTimeout(this.pointerFrame);
    this.pointerFrame = null;
  }

  private surfaceFocused(): boolean {
    return typeof document === 'undefined' || document.activeElement === this.surface.nativeElement;
  }

  private keyIdentity(event: Pick<KeyboardEvent, 'code' | 'key' | 'location'>): string {
    return `${event.code || event.key}:${event.location}`;
  }
}
