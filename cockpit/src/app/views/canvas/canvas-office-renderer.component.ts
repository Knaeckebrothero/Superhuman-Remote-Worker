import {
  AfterViewInit,
  ChangeDetectorRef,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  EventEmitter,
  Input,
  OnDestroy,
  Output,
  ViewChild,
  inject,
} from '@angular/core';
import {CanvasOfficeSession} from '../../core/models/canvas.model';
import {
  collaboraHostPostmessageReady,
  parseCollaboraLoadingStatus,
} from './canvas-office-protocol';

let officeFrameSequence = 0;

interface OfficeLaunch {
  readonly action: string;
  readonly origin: string;
  readonly session: CanvasOfficeSession;
}

/**
 * Cross-origin Collabora mount. The exact WindowProxy is captured before the
 * hidden form submits, then every message is filtered by source and origin.
 */
@Component({
  selector: 'app-canvas-office-renderer',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <form #launchForm class="office-launch-form" method="post"
          [attr.action]="launch?.action ?? null" [attr.target]="frameName">
      @if (launch; as current) {
        <input type="hidden" name="access_token" [value]="current.session.access_token" />
        <input type="hidden" name="access_token_ttl"
               [value]="current.session.access_token_ttl" />
      }
    </form>
    <iframe
      #officeFrame
      [attr.name]="frameName"
      sandbox="allow-scripts allow-same-origin allow-forms"
      referrerpolicy="no-referrer"
      allow="camera 'none'; microphone 'none'; geolocation 'none'; clipboard-read 'none'; clipboard-write 'none'"
      [title]="title">
    </iframe>
  `,
  styles: `
    :host {
      display: block;
      width: 100%;
      height: 100%;
      min-height: 320px;
    }
    .office-launch-form {
      display: none;
    }
    iframe {
      display: block;
      width: 100%;
      height: 100%;
      min-height: 320px;
      border: 0;
      background: white;
    }
  `,
})
export class CanvasOfficeRendererComponent implements AfterViewInit, OnDestroy {
  readonly frameName = `canvas-office-${++officeFrameSequence}`;
  launch: OfficeLaunch | null = null;

  private pendingSession: CanvasOfficeSession | null = null;
  private pendingOrigin = '';
  private boundWindow: WindowProxy | null = null;
  private viewReady = false;
  private mountGeneration = 0;
  private versionStatesReady = false;
  private documentIsLoaded = false;
  private handshakeComplete = false;
  private readonly changeDetector = inject(ChangeDetectorRef);

  @ViewChild('officeFrame', {static: true})
  private frame!: ElementRef<HTMLIFrameElement>;
  @ViewChild('launchForm', {static: true})
  private form!: ElementRef<HTMLFormElement>;

  @Output() readonly documentLoaded = new EventEmitter<void>();

  @Input({required: true})
  set session(value: CanvasOfficeSession) {
    this.pendingSession = value;
    this.scheduleMount();
  }

  @Input({required: true})
  set officeOrigin(value: string) {
    this.pendingOrigin = value;
    this.scheduleMount();
  }

  @Input() title = '';

  private readonly onWindowMessage = (event: MessageEvent<unknown>): void => {
    const launch = this.launch;
    if (
      !launch ||
      !this.boundWindow ||
      event.source !== this.boundWindow ||
      event.origin !== launch.origin
    ) {
      return;
    }

    const message = parseCollaboraLoadingStatus(event.data);
    if (!message || this.handshakeComplete) return;
    if (message.status === 'Frame_Ready') this.versionStatesReady = true;
    if (message.status === 'Document_Loaded') this.documentIsLoaded = true;
    this.completeHandshake();
  };

  ngAfterViewInit(): void {
    this.viewReady = true;
    const generation = ++this.mountGeneration;
    queueMicrotask(() => {
      if (generation !== this.mountGeneration || !this.viewReady) return;
      const frameWindow = this.frame.nativeElement.contentWindow;
      if (!frameWindow) return;
      this.boundWindow = frameWindow;
      if (typeof window !== 'undefined') {
        window.addEventListener('message', this.onWindowMessage);
      }
      this.scheduleMount();
    });
  }

  ngOnDestroy(): void {
    this.viewReady = false;
    this.mountGeneration += 1;
    if (typeof window !== 'undefined') {
      window.removeEventListener('message', this.onWindowMessage);
    }
    this.boundWindow = null;
    this.launch = null;
  }

  private scheduleMount(): void {
    if (!this.viewReady || !this.boundWindow) return;
    const launch = buildOfficeLaunch(this.pendingSession, this.pendingOrigin);
    const generation = ++this.mountGeneration;
    queueMicrotask(() => {
      if (
        generation !== this.mountGeneration ||
        !this.viewReady ||
        !this.boundWindow
      ) {
        return;
      }
      this.resetHandshake();
      this.launch = launch;
      this.changeDetector.detectChanges();
      if (launch) {
        // Render the exact hidden fields before invoking the browser's native
        // form submission into the already-bound target frame.
        setTimeout(() => {
          if (
            generation === this.mountGeneration &&
            this.boundWindow &&
            this.launch === launch
          ) {
            this.form.nativeElement.submit();
          }
        }, 0);
      }
    });
  }

  private resetHandshake(): void {
    this.versionStatesReady = false;
    this.documentIsLoaded = false;
    this.handshakeComplete = false;
  }

  private completeHandshake(): void {
    if (
      this.handshakeComplete ||
      !this.versionStatesReady ||
      !this.documentIsLoaded ||
      !this.boundWindow ||
      !this.launch
    ) {
      return;
    }
    this.handshakeComplete = true;
    this.boundWindow.postMessage(
      JSON.stringify(collaboraHostPostmessageReady()),
      this.launch.origin,
    );
    this.documentLoaded.emit();
  }
}

function buildOfficeLaunch(
  session: CanvasOfficeSession | null,
  officeOrigin: string,
): OfficeLaunch | null {
  if (
    !session ||
    !session.access_token ||
    session.access_token.length > 16_384 ||
    !Number.isSafeInteger(session.access_token_ttl) ||
    session.access_token_ttl <= 0
  ) {
    return null;
  }

  try {
    const origin = new URL(officeOrigin);
    const action = new URL(session.urlsrc);
    const wopiSource = new URL(session.WOPISrc);
    if (
      origin.origin !== officeOrigin ||
      origin.pathname !== '/' ||
      origin.search ||
      origin.hash ||
      origin.username ||
      origin.password ||
      (origin.protocol !== 'https:' && origin.protocol !== 'http:') ||
      action.origin !== origin.origin ||
      action.username ||
      action.password ||
      action.hash ||
      !action.pathname.endsWith('/cool.html') ||
      action.searchParams.has('WOPISrc') ||
      (wopiSource.protocol !== 'https:' && wopiSource.protocol !== 'http:') ||
      wopiSource.username ||
      wopiSource.password ||
      wopiSource.search ||
      wopiSource.hash ||
      !/^\/wopi\/files\/[^/]+$/.test(wopiSource.pathname)
    ) {
      return null;
    }

    const separator = session.urlsrc.includes('?')
      ? session.urlsrc.endsWith('?') || session.urlsrc.endsWith('&') ? '' : '&'
      : '?';
    return {
      action: `${session.urlsrc}${separator}WOPISrc=${encodeURIComponent(session.WOPISrc)}`,
      origin: origin.origin,
      session,
    };
  } catch {
    return null;
  }
}
