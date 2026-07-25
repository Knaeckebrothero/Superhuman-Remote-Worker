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
  CanvasOfficeTurnAdapter,
  CanvasService,
} from '../../core/services/canvas.service';
import {
  collaboraActionSave,
  collaboraHostPostmessageReady,
  collaboraHostVersionRestore,
  collaboraResetAccessToken,
  CollaboraMessage,
  parseCollaboraMessage,
} from './canvas-office-protocol';

let officeFrameSequence = 0;
const OFFICE_SAVE_TIMEOUT_MS = 30_000;
const MAX_TIMER_DELAY_MS = 2_147_483_647;

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
  private versionStatesSupported: boolean | null = null;
  private documentIsLoaded = false;
  private handshakeComplete = false;
  private mountedRevision = 0;
  private pendingRevision = 0;
  private restoreTargetRevision: number | null = null;
  private fallbackReloadPending = false;
  private modified = false;
  private modificationGeneration = 0;
  private needsTurnSync = false;
  private savePromise: Promise<boolean> | null = null;
  private saveResolve: ((value: boolean) => void) | null = null;
  private saveFinishing = false;
  private saveModificationGeneration = 0;
  private saveTimer: ReturnType<typeof setTimeout> | null = null;
  private tokenExpiryTimer: ReturnType<typeof setTimeout> | null = null;
  private tokenRenewal: Promise<void> | null = null;
  private detachTurnAdapter: (() => void) | null = null;
  private readonly changeDetector = inject(ChangeDetectorRef);
  private readonly canvas = inject(CanvasService);
  private readonly turnAdapter: CanvasOfficeTurnAdapter = {
    saveBeforeUserMessage: () => this.saveBeforeUserMessage(),
  };

  @ViewChild('officeFrame', {static: true})
  private frame!: ElementRef<HTMLIFrameElement>;
  @ViewChild('launchForm', {static: true})
  private form!: ElementRef<HTMLFormElement>;

  @Output() readonly documentLoaded = new EventEmitter<void>();
  @Output() readonly modifiedChange = new EventEmitter<boolean>();
  @Output() readonly conflict = new EventEmitter<string>();

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

  private editableValue = false;
  @Input()
  set editable(value: boolean) {
    this.editableValue = value === true;
    this.updateTurnAdapterRegistration();
  }

  @Input()
  set presentationRevision(value: number) {
    if (!Number.isSafeInteger(value) || value < 0) return;
    this.pendingRevision = value;
    queueMicrotask(() => this.handlePendingRevision());
  }

  @Input({required: true})
  refreshToken!: () => Promise<CanvasOfficeSession | null>;

  @Input({required: true})
  reloadSession!: () => void;

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

    const message = parseCollaboraMessage(event.data);
    if (!message) return;
    this.handleCollaboraMessage(message);
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
      this.updateTurnAdapterRegistration();
      this.scheduleMount();
    });
  }

  ngOnDestroy(): void {
    this.viewReady = false;
    this.mountGeneration += 1;
    if (typeof window !== 'undefined') {
      window.removeEventListener('message', this.onWindowMessage);
    }
    this.detachTurnAdapter?.();
    this.detachTurnAdapter = null;
    this.clearSave(false);
    this.clearTokenExpiryTimer();
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
      this.resetProtocolState();
      this.launch = launch;
      this.mountedRevision = this.pendingRevision;
      this.changeDetector.detectChanges();
      if (launch) {
        this.scheduleTokenExpiryFallback(launch.session);
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

  private resetProtocolState(): void {
    this.versionStatesSupported = null;
    this.documentIsLoaded = false;
    this.handshakeComplete = false;
    this.restoreTargetRevision = null;
    this.fallbackReloadPending = false;
    this.needsTurnSync = false;
    this.setModified(false);
    this.clearSave(false);
    this.clearTokenExpiryTimer();
  }

  private completeHandshake(): void {
    if (
      this.handshakeComplete ||
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
    this.handlePendingRevision();
  }

  private handleCollaboraMessage(message: CollaboraMessage): void {
    switch (message.kind) {
      case 'loading':
        if (message.value.status === 'Frame_Ready') {
          this.versionStatesSupported = message.value.versionStates;
          this.handlePendingRevision();
        } else {
          this.documentIsLoaded = true;
          this.completeHandshake();
        }
        return;
      case 'modified':
        if (message.modified) {
          this.modificationGeneration += 1;
          this.needsTurnSync = true;
        }
        this.setModified(message.modified);
        return;
      case 'save-response':
        if (this.saveResolve && !this.saveFinishing) {
          this.saveFinishing = true;
          void this.finishSave(message);
        }
        return;
      case 'version-restore':
        if (this.restoreTargetRevision !== null) {
          this.mountedRevision = this.restoreTargetRevision;
          this.restoreTargetRevision = null;
          this.fallbackReloadPending = true;
          this.reloadSession();
        }
        return;
      case 'token-expiring':
        if (!this.tokenRenewal) this.tokenRenewal = this.renewAccessToken();
        return;
    }
  }

  private updateTurnAdapterRegistration(): void {
    if (this.editableValue && this.viewReady && !this.detachTurnAdapter) {
      this.detachTurnAdapter = this.canvas.registerOfficeTurnAdapter(this.turnAdapter);
    } else if ((!this.editableValue || !this.viewReady) && this.detachTurnAdapter) {
      this.detachTurnAdapter();
      this.detachTurnAdapter = null;
    }
  }

  private saveBeforeUserMessage(): Promise<boolean> {
    if (!this.editableValue || !this.needsTurnSync) return Promise.resolve(true);
    if (
      !this.launch ||
      !this.boundWindow ||
      !this.documentIsLoaded ||
      !this.handshakeComplete
    ) {
      this.emitConflict('canvas_office_save_unavailable');
      return Promise.resolve(false);
    }
    if (this.savePromise) return this.savePromise;

    this.savePromise = new Promise<boolean>(resolve => {
      this.saveResolve = resolve;
      this.saveModificationGeneration = this.modificationGeneration;
      this.armSaveTimer();
    });
    this.postToOffice(collaboraActionSave());
    return this.savePromise;
  }

  private async finishSave(
    message: Extract<CollaboraMessage, {readonly kind: 'save-response'}>,
  ): Promise<void> {
    const accepted = message.success || message.result === 'unmodified';
    if (!accepted) {
      this.emitConflict('canvas_office_save_conflict');
      this.clearSave(false);
      return;
    }

    try {
      const revision = await this.canvas.reconcileOfficeSave();
      if (revision === null) {
        this.emitConflict('canvas_office_save_conflict');
        this.clearSave(false);
        return;
      }
      this.mountedRevision = Math.max(this.mountedRevision, revision);
      this.pendingRevision = Math.max(this.pendingRevision, revision);
      if (this.modificationGeneration !== this.saveModificationGeneration) {
        // Doc_ModifiedStatus may repeat the same true value. Conservatively
        // flush again if any such signal arrived while the prior save was in
        // flight; the outer chat turn remains paused on the same promise.
        this.saveModificationGeneration = this.modificationGeneration;
        this.saveFinishing = false;
        this.armSaveTimer();
        this.postToOffice(collaboraActionSave());
        return;
      }
      this.needsTurnSync = false;
      this.setModified(false);
      this.clearSave(true);
      this.handlePendingRevision();
    } catch {
      this.emitConflict('canvas_office_save_conflict');
      this.clearSave(false);
    }
  }

  private clearSave(result: boolean): void {
    if (this.saveTimer !== null) clearTimeout(this.saveTimer);
    this.saveTimer = null;
    const resolve = this.saveResolve;
    this.saveResolve = null;
    this.savePromise = null;
    this.saveFinishing = false;
    resolve?.(result);
  }

  private armSaveTimer(): void {
    if (this.saveTimer !== null) clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => {
      this.emitConflict('canvas_office_save_timeout');
      this.clearSave(false);
    }, OFFICE_SAVE_TIMEOUT_MS);
  }

  private handlePendingRevision(): void {
    if (
      !this.documentIsLoaded ||
      !this.handshakeComplete ||
      this.pendingRevision <= this.mountedRevision ||
      this.restoreTargetRevision !== null ||
      this.fallbackReloadPending ||
      this.saveResolve !== null
    ) {
      return;
    }
    if (this.versionStatesSupported === null) return;
    if (this.versionStatesSupported) {
      this.restoreTargetRevision = this.pendingRevision;
      this.postToOffice(collaboraHostVersionRestore());
      return;
    }
    this.mountedRevision = this.pendingRevision;
    this.fallbackReloadPending = true;
    this.reloadSession();
  }

  private async renewAccessToken(): Promise<void> {
    const generation = this.mountGeneration;
    this.clearTokenExpiryTimer();
    try {
      const session = await this.refreshToken();
      if (
        !session ||
        generation !== this.mountGeneration ||
        !this.launch ||
        !this.boundWindow
      ) {
        this.emitConflict('canvas_office_token_refresh_failed');
        return;
      }
      this.postToOffice(
        collaboraResetAccessToken(session.access_token, session.access_token_ttl),
      );
      this.scheduleTokenExpiryFallback(session);
    } catch {
      this.emitConflict('canvas_office_token_refresh_failed');
    } finally {
      this.tokenRenewal = null;
    }
  }

  private scheduleTokenExpiryFallback(session: CanvasOfficeSession): void {
    this.clearTokenExpiryTimer();
    const delay = session.access_token_ttl - Date.now();
    // Slice 1 tests and persisted historical fixtures may carry an already-old
    // timestamp. Only a newly minted, future expiry can establish that this
    // CODE build omitted App_TokenExpiring.
    if (delay <= 0 || delay > MAX_TIMER_DELAY_MS) return;
    this.tokenExpiryTimer = setTimeout(() => {
      this.tokenExpiryTimer = null;
      this.emitConflict('canvas_office_token_refresh_unavailable');
    }, delay);
  }

  private clearTokenExpiryTimer(): void {
    if (this.tokenExpiryTimer !== null) clearTimeout(this.tokenExpiryTimer);
    this.tokenExpiryTimer = null;
  }

  private setModified(modified: boolean): void {
    if (this.modified === modified) return;
    this.modified = modified;
    this.modifiedChange.emit(modified);
  }

  private emitConflict(code: string): void {
    this.conflict.emit(code);
  }

  private postToOffice(message: unknown): void {
    if (!this.boundWindow || !this.launch) return;
    this.boundWindow.postMessage(JSON.stringify(message), this.launch.origin);
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
