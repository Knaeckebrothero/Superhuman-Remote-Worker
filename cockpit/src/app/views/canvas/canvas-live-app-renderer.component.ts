import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  EventEmitter,
  Input,
  OnDestroy,
  Output,
  SecurityContext,
  ViewChild,
  inject,
} from '@angular/core';
import {DomSanitizer, SafeResourceUrl} from '@angular/platform-browser';

/** Fixed host boundary for an isolated, server-minted Canvas application URL. */
@Component({
  selector: 'app-canvas-live-app-renderer',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="live-app-warning" role="note">{{ warning }}</div>
    <iframe
      #canvasFrame
      sandbox="allow-scripts allow-same-origin allow-forms"
      referrerpolicy="no-referrer"
      allow="camera 'none'; microphone 'none'; geolocation 'none'; clipboard-read 'none'; clipboard-write 'none'"
      [title]="title">
    </iframe>
  `,
  styles: `
    :host {
      display: flex;
      flex-direction: column;
      width: 100%;
      height: 100%;
      min-height: 320px;
    }
    .live-app-warning {
      flex: 0 0 auto;
      padding: 6px 10px;
      color: var(--text-primary);
      background: color-mix(in srgb, var(--warning-color, #f59e0b) 12%, transparent);
      border-bottom: 1px solid color-mix(in srgb, var(--warning-color, #f59e0b) 35%, transparent);
      font-size: 12px;
      font-weight: 600;
      line-height: 1.35;
    }
    iframe {
      display: block;
      flex: 1 1 auto;
      width: 100%;
      height: 100%;
      min-height: 320px;
      border: 0;
      background: white;
    }
  `,
})
export class CanvasLiveAppRendererComponent implements AfterViewInit, OnDestroy {
  private pendingSrc: SafeResourceUrl | null = null;
  private boundWindow: WindowProxy | null = null;
  private mountGeneration = 0;
  private readonly sanitizer = inject(DomSanitizer);
  @ViewChild('canvasFrame', {static: true})
  private frame!: ElementRef<HTMLIFrameElement>;

  @Output() readonly frameBound = new EventEmitter<WindowProxy>();
  @Output() readonly frameUnbound = new EventEmitter<WindowProxy>();
  @Output() readonly frameMessage = new EventEmitter<MessageEvent<unknown>>();

  @Input({required: true})
  set src(value: SafeResourceUrl) {
    this.pendingSrc = value;
    if (this.boundWindow) this.scheduleMount(this.boundWindow, value);
  }
  @Input() title = '';
  @Input() warning = '';

  private readonly onWindowMessage = (event: MessageEvent<unknown>): void => {
    if (this.boundWindow && event.source === this.boundWindow) {
      this.frameMessage.emit(event);
    }
  };

  ngAfterViewInit(): void {
    const generation = ++this.mountGeneration;
    queueMicrotask(() => {
      if (generation !== this.mountGeneration) return;
      const frameWindow = this.frame.nativeElement.contentWindow;
      if (!frameWindow) return;
      this.boundWindow = frameWindow;
      if (typeof window !== 'undefined') {
        window.addEventListener('message', this.onWindowMessage);
      }
      if (this.pendingSrc) this.scheduleMount(frameWindow, this.pendingSrc);
    });
  }

  ngOnDestroy(): void {
    this.mountGeneration += 1;
    if (typeof window !== 'undefined') {
      window.removeEventListener('message', this.onWindowMessage);
    }
    const frameWindow = this.boundWindow;
    this.boundWindow = null;
    if (frameWindow) this.frameUnbound.emit(frameWindow);
  }

  private scheduleMount(frameWindow: WindowProxy, src: SafeResourceUrl): void {
    const generation = ++this.mountGeneration;
    // Angular supplies inputs before the view hook. Deferring the assignment
    // guarantees that the exact WindowProxy is bound in the parent before a
    // remote bootstrap script can execute and post its first challenge.
    queueMicrotask(() => {
      if (
        generation !== this.mountGeneration ||
        this.boundWindow !== frameWindow ||
        this.pendingSrc !== src
      ) {
        return;
      }
      this.frameBound.emit(frameWindow);
      const trustedUrl = this.sanitizer.sanitize(SecurityContext.RESOURCE_URL, src);
      if (trustedUrl && this.boundWindow === frameWindow) {
        this.frame.nativeElement.src = trustedUrl;
      }
    });
  }
}
