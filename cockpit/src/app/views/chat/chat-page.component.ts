import {
    Component,
    DestroyRef,
    ElementRef,
    computed,
    effect,
    inject,
    OnDestroy,
    OnInit,
    signal,
    viewChild,
} from '@angular/core';
import {takeUntilDestroyed} from '@angular/core/rxjs-interop';
import {ActivatedRoute, Router} from '@angular/router';
import {TranslocoPipe} from '@jsverse/transloco';
import {AngularSplitModule, SplitGutterInteractionEvent} from 'angular-split';
import {distinctUntilChanged, map} from 'rxjs';
import {PersistentChatComponent} from '../../views/persistent-chat/persistent-chat.component';
import {PersistentChatService} from '../../core/services/persistent-chat.service';
import {AppToastService} from '../../ui/toast';
import {ErrorMessageService} from '../../core/services/error-message.service';
import {CanvasService} from '../../core/services/canvas.service';
import {ViewportService} from '../../core/services/viewport.service';
import {AppIconComponent} from '../../ui/icon';
import {AppIconButtonComponent} from '../../ui/icon-button';
import {CanvasPaneComponent} from '../canvas/canvas-pane.component';
import {canvasSourceKey} from '../canvas/canvas-rendering';

@Component({
    selector: 'app-chat-page',
    standalone: true,
    imports: [
        AngularSplitModule,
        TranslocoPipe,
        PersistentChatComponent,
        CanvasPaneComponent,
        AppIconComponent,
        AppIconButtonComponent,
    ],
    template: `
      @if (canvasAvailable()) {
        <button type="button" class="canvas-skip-link" (click)="openCanvas(true)">
          {{ 'canvas.skipToCanvas' | transloco }}
        </button>
      }
      <as-split #split direction="horizontal" unit="percent"
                [gutterSize]="canvasAreaVisible() && !viewport.isMobile() ? 8 : 0"
                [gutterStep]="2" [disabled]="viewport.isMobile() || !canvasOpen()"
                [restrictMove]="true" [useTransition]="false"
                [gutterAriaLabel]="'canvas.resize' | transloco"
                (dragEnd)="onSplitDragEnd($event)"
                (gutterDblClick)="closeCanvas()">
        <as-split-area [size]="chatAreaSize()" [minSize]="viewport.isMobile() ? 100 : 25"
                       [maxSize]="viewport.isMobile() ? 100 : 75"
                       [visible]="chatAreaVisible()">
          <div id="chat-panel" class="chat-panel"
               [attr.inert]="chatAreaHidden() ? '' : null"
               [attr.aria-hidden]="chatAreaHidden() ? 'true' : null">
            <app-persistent-chat (canvasRequested)="openCanvas(true)">
              @if (canvasAvailable()) {
                <span chatHeaderAction #canvasToggle>
                  <app-icon-button size="sm"
                                   [ariaLabel]="(canvasAreaVisible() ? 'canvas.hide' : 'canvas.open') | transloco"
                                   [tooltip]="(canvasAreaVisible() ? 'canvas.hide' : 'canvas.open') | transloco"
                                   (clicked)="canvasAreaVisible() && !viewport.isMobile() ? closeCanvas() : openCanvas(true)">
                    <app-icon size="sm">dashboard_customize</app-icon>
                  </app-icon-button>
                </span>
              }
            </app-persistent-chat>
          </div>
        </as-split-area>

        <as-split-area [size]="canvasAreaSize()" [minSize]="viewport.isMobile() ? 100 : 25"
                       [maxSize]="viewport.isMobile() ? 100 : 75"
                       [visible]="canvasAreaVisible()">
          <div id="canvas-panel" class="canvas-panel"
               [attr.inert]="canvasAreaHidden() ? '' : null"
               [attr.aria-hidden]="canvasAreaHidden() ? 'true' : null">
            <app-canvas-pane #canvasPane [active]="canvasOpen()" [mobile]="viewport.isMobile()"
                             (closeRequested)="closeCanvas()"
                             (returnToChat)="returnToChat()"
                             (dirtyChange)="canvasDirty.set($event)" />
          </div>
        </as-split-area>
      </as-split>
    `,
    styles: `
      :host {
        position: relative;
        display: block;
        width: 100%;
        height: 100%;
        min-width: 0;
        overflow: hidden;
      }

      as-split {
        --as-gutter-background-color: var(--border-color);
        --as-gutter-disabled-cursor: default;

        width: 100%;
        height: 100%;
      }

      .chat-panel,
      .canvas-panel,
      app-persistent-chat,
      app-canvas-pane {
        display: block;
        width: 100%;
        height: 100%;
        min-width: 0;
      }

      .canvas-panel { border-left: 1px solid var(--border-color); }

      .canvas-skip-link {
        position: absolute;
        z-index: 100;
        top: 8px;
        left: 50%;
        min-height: 32px;
        padding: 5px 10px;
        color: var(--text-primary);
        background: var(--panel-bg);
        border: 1px solid var(--accent-color);
        border-radius: var(--radius-control);
        opacity: 0;
        pointer-events: none;
        transform: translate(-50%, -150%);
      }

      .canvas-skip-link:focus-visible {
        opacity: 1;
        pointer-events: auto;
        transform: translate(-50%, 0);
      }

      @media (width <= 768px) {
        .canvas-panel { border-left: 0; }
      }

      @media (prefers-reduced-motion: reduce) {
        .canvas-skip-link { transition: none; }
      }
    `,
})
export class ChatPageComponent implements OnInit, OnDestroy {
    private readonly route = inject(ActivatedRoute);
    private readonly router = inject(Router);
    private readonly chat = inject(PersistentChatService);
    private readonly toast = inject(AppToastService);
    private readonly errors = inject(ErrorMessageService);
    private readonly canvas = inject(CanvasService);
    readonly viewport = inject(ViewportService);
    private readonly destroyRef = inject(DestroyRef);
    private routeGeneration = 0;
    readonly canvasOpen = signal(false);
    readonly canvasFocus = signal(false);
    readonly canvasDirty = signal(false);
    readonly chatPercent = signal(56);
    private previousCanvasThread: string | null = null;
    private previousCanvasSource: string | null = null;
    private canvasOpener: HTMLElement | null = null;

    private readonly pane = viewChild<CanvasPaneComponent>('canvasPane');
    private readonly split = viewChild<unknown, ElementRef<HTMLElement>>('split', {read: ElementRef});
    private readonly toggle = viewChild<unknown, ElementRef<HTMLElement>>('canvasToggle', {read: ElementRef});

    readonly canvasAvailable = computed(() => {
        const state = this.canvas.state();
        return this.canvasDirty() || (!!state?.source && state.status !== 'cleared');
    });
    readonly chatAreaVisible = computed(() => !this.viewport.isMobile() || !this.canvasFocus());
    readonly canvasAreaVisible = computed(
        () => this.canvasOpen() && (!this.viewport.isMobile() || this.canvasFocus()),
    );
    readonly chatAreaHidden = computed(() => this.viewport.isMobile() && this.canvasFocus());
    readonly canvasAreaHidden = computed(
        () => !this.canvasOpen() || (this.viewport.isMobile() && !this.canvasFocus()),
    );
    readonly chatAreaSize = computed(() => {
        if (this.viewport.isMobile() || !this.canvasAreaVisible()) return 100;
        return this.chatPercent();
    });
    readonly canvasAreaSize = computed(() => {
        if (this.viewport.isMobile()) return 100;
        return this.canvasAreaVisible() ? 100 - this.chatPercent() : 0;
    });

    /** Instant-landing draft chat at `/` (route data, not a URL param). */
    private readonly isDraftRoute = this.route.snapshot.data['draft'] === true;

    constructor() {
        // Draft flow: when the first send creates the thread
        // (_createFromDraftSession → createAndConnect sets threadId), move the
        // URL from / to the session. No replaceUrl — Back returns to a fresh
        // draft. The destination ChatPage instance skips reconnecting via the
        // ngOnInit same-thread guard below.
        effect(() => {
            const id = this.chat.threadId();
            if (this.isDraftRoute && id) {
                void this.router.navigate(['/sessions', id]);
            }
        });

        // New logical sources open automatically. A same-source republish only
        // refreshes the mounted renderer, so it never steals focus or reopens a
        // pane the user explicitly closed.
        effect(() => {
            const threadId = this.canvas.threadId();
            const state = this.canvas.state();
            const source = canvasSourceKey(state);
            const dirty = this.canvasDirty();
            if (threadId !== this.previousCanvasThread) {
                this.previousCanvasThread = threadId;
                this.previousCanvasSource = null;
                this.canvasOpen.set(false);
                this.canvasFocus.set(false);
                this.canvasDirty.set(false);
            }
            if (!source || state?.status === 'cleared') {
                if (dirty) {
                    this.configureSplitterAccessibility();
                    return;
                }
                const restoreFocus = this.canvasOpen() && this.shouldRestoreFocusFromCanvas();
                this.previousCanvasSource = null;
                this.canvasOpen.set(false);
                this.canvasFocus.set(false);
                if (restoreFocus) queueMicrotask(() => this.focusCanvasOpener());
            } else if (source !== this.previousCanvasSource) {
                this.previousCanvasSource = source;
                this.canvasOpen.set(true);
                // Desktop can reveal a sibling without disturbing the active
                // chat control. On mobile, making Canvas full-screen would put
                // that focused control inside a newly inert subtree. Keep the
                // new stage mounted/announced and let the user enter it through
                // the trusted toggle, tool card, or skip link.
                this.canvasFocus.set(false);
            }
            this.configureSplitterAccessibility();
        });
    }

    openCanvas(focus = false): void {
        if (!this.canvasAvailable()) return;
        if (focus && typeof document !== 'undefined') {
            this.canvasOpener = document.activeElement instanceof HTMLElement
                ? document.activeElement
                : null;
        }
        this.canvasOpen.set(true);
        if (this.viewport.isMobile()) this.canvasFocus.set(true);
        this.configureSplitterAccessibility();
        if (focus) queueMicrotask(() => this.pane()?.focusContent());
    }

    returnToChat(): void {
        this.canvasFocus.set(false);
        queueMicrotask(() => this.focusCanvasOpener());
    }

    closeCanvas(): void {
        const restoreFocus = this.shouldRestoreFocusFromCanvas();
        this.canvasOpen.set(false);
        this.canvasFocus.set(false);
        if (restoreFocus) queueMicrotask(() => this.focusCanvasOpener());
    }

    onSplitDragEnd(event: SplitGutterInteractionEvent): void {
        const chatSize = event.sizes[0];
        if (typeof chatSize === 'number') {
            this.chatPercent.set(Math.max(25, Math.min(75, chatSize)));
        }
        this.configureSplitterAccessibility();
    }

    private configureSplitterAccessibility(): void {
        queueMicrotask(() => {
            const gutter = this.split()?.nativeElement.querySelector<HTMLElement>('[role="separator"]');
            if (gutter) {
                gutter.setAttribute('aria-controls', 'chat-panel canvas-panel');
                gutter.setAttribute('aria-orientation', 'vertical');
            }
        });
    }

    private focusCanvasOpener(): void {
        if (this.canvasOpener?.isConnected) {
            this.canvasOpener.focus({preventScroll: true});
            this.canvasOpener = null;
            return;
        }
        const fallback =
            this.toggle()?.nativeElement.querySelector<HTMLElement>('button') ??
            this.split()?.nativeElement.querySelector<HTMLElement>(
                '#chat-panel textarea:not([disabled]), #chat-panel button:not([disabled])',
            );
        fallback?.focus({preventScroll: true});
        this.canvasOpener = null;
    }

    private shouldRestoreFocusFromCanvas(): boolean {
        const activeElement = typeof document === 'undefined' ? null : document.activeElement;
        const focusWasInCanvas =
            activeElement instanceof Element && activeElement.closest('#canvas-panel') !== null;
        const focusWasOnSplitter =
            activeElement instanceof Element && activeElement.getAttribute('role') === 'separator';
        return this.viewport.isMobile() ||
            this.canvasFocus() ||
            focusWasInCanvas ||
            focusWasOnSplitter ||
            this.canvasOpener !== null;
    }

    ngOnInit(): void {
        if (this.isDraftRoute) {
            this.canvas.selectThread(null);
            this.chat.enterDraftSession();
            return;
        }

        // Angular reuses this component for /sessions/:threadId → another
        // /sessions/:threadId navigation. Observe params for the component's
        // whole lifetime so both chat and Canvas switch together.
        this.route.paramMap.pipe(
            map(params => params.get('threadId')),
            distinctUntilChanged(),
            takeUntilDestroyed(this.destroyRef),
        ).subscribe(threadId => this.handleThreadRoute(threadId));
    }

    private handleThreadRoute(threadId: string | null): void {
        const routeGeneration = ++this.routeGeneration;

        if (threadId === '_creating') {
            this.canvas.selectThread(null);
            // Navigate arrived before thread exists — create it now
            const state = history.state as { createBody?: Record<string, any> };
            if (state?.createBody) {
                this.chat.createAndConnect(state.createBody).then(
                    id => {
                        if (routeGeneration !== this.routeGeneration) return false;
                        this.canvas.selectThread(id);
                        return this.router.navigate(['/sessions', id], {replaceUrl: true});
                    },
                    err => {
                        if (routeGeneration !== this.routeGeneration) return;
                        this.toast.danger(this.errors.translate(err, 'errors.sessions.createFailed'));
                        void this.router.navigate(['/sessions']);
                    }
                );
            } else {
                void this.router.navigate(['/sessions']);
            }
        } else if (threadId) {
            // Canvas state reconciles independently from chat history and may
            // remain available even when the live agent transport is offline.
            this.canvas.selectThread(threadId);
            // Already connected or mid-start on this thread? Don't reconnect.
            // The mid-start case is the draft flow landing here right after
            // createAndConnect — a second connect() would race the first.
            if (
                this.chat.threadId() === threadId &&
                (this.chat.isConnected() || this.chat.isStartingSession())
            ) return;

            void this.chat.connect(threadId);
        } else {
            this.canvas.selectThread(null);
            void this.router.navigate(['/sessions']);
        }
    }

    ngOnDestroy(): void {
        this.routeGeneration++;
        // Don't disconnect — keep session alive across navigation
    }
}
