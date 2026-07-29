/**
 * Public Dynamic Canvas models.
 *
 * Canvas state is a logical presentation pointer. The public Cockpit payload
 * deliberately excludes workspace addresses, generations, proxy credentials,
 * and other gateway-only data.
 */

export const MAIN_CANVAS_ID = 'main' as const;

export type CanvasId = typeof MAIN_CANVAS_ID;

export interface WorkspaceFileCanvasSource {
  readonly type: 'workspace_file';
  readonly path: string;
  readonly [key: string]: unknown;
}

export interface WorkspaceAppCanvasSource {
  readonly type: 'workspace_app';
  readonly manifest_path?: string | null;
  readonly entry_path?: string;
  readonly [key: string]: unknown;
}

export interface BrowserCanvasSource {
  readonly type: 'browser';
  readonly [key: string]: unknown;
}

/**
 * A newer server may add source kinds before this Cockpit has a renderer for
 * them. Keep the pointer inspectable so the trusted host can show an
 * unsupported-source fallback in a later slice.
 */
export interface UnknownCanvasSource {
  readonly type: string;
  readonly [key: string]: unknown;
}

export type CanvasSource =
  | WorkspaceFileCanvasSource
  | WorkspaceAppCanvasSource
  | BrowserCanvasSource
  | UnknownCanvasSource;

export type CanvasRenderer =
  | 'auto'
  | 'markdown'
  | 'text'
  | 'html'
  | 'html-interactive'
  | 'image'
  | 'office';

export type CanvasContentOrigin = 'workspace' | 'snapshot';

export type CanvasStatus =
  | 'ready'
  | 'starting'
  | 'source_changed'
  | 'unavailable'
  | 'ended'
  | 'error'
  | 'cleared';

export interface CanvasCapabilities {
  readonly can_edit: boolean;
  readonly can_pop_out: boolean;
  readonly can_take_control: boolean;
  /** Positive server capability for launching this Office presentation. */
  readonly can_view_office?: boolean;
  /**
   * Positive server capability for creating an isolated live-app attachment.
   * Optional while older/default-off orchestrators still return the Slice 3A
   * capability shape; absence always means unsupported.
   */
  readonly can_create_viewer_session?: boolean;
  /** Positive authority for attaching to an already-staged shared browser. */
  readonly can_stream_browser?: boolean;
}

/** Standard Collabora form-post parameters minted by the cookie-authenticated BFF. */
export interface CanvasOfficeSession {
  readonly urlsrc: string;
  readonly WOPISrc: string;
  readonly access_token: string;
  /** Absolute epoch milliseconds, as required by the Collabora launch form. */
  readonly access_token_ttl: number;
}

export type BrowserCapabilityReason =
  | 'feature_disabled'
  | 'workspace_required'
  | 'workspace_unattested'
  | 'workspace_unroutable'
  | 'transport_unavailable';

/** Owner-scoped capability for creating or recovering a shared browser. */
export interface BrowserCapability {
  readonly feature_enabled: boolean;
  readonly can_open_browser: boolean;
  readonly workspace_ready: boolean;
  readonly reason: BrowserCapabilityReason | null;
}

export type BrowserCapabilityStatus = 'idle' | 'loading' | 'ready' | 'error';
export type BrowserOpenStatus = 'idle' | 'workspace' | 'browser' | 'error';

export interface CanvasViewAttachment {
  readonly attachment_id: string;
  readonly origin: string;
  readonly bootstrap_url: string;
  /**
   * Per-attachment parent proof. This value is returned only to the trusted
   * Cockpit and must never be placed in the iframe URL or application state.
   */
  readonly bridge_nonce: string;
  readonly bootstrap_expires_at: string;
  readonly expires_at: string;
  readonly renew_after: string;
}

export interface CanvasViewAuthorization {
  readonly challenge: string;
  readonly ready_receipt: string;
  readonly exchange_code: string;
  readonly expires_at: string;
}

export interface CanvasViewAttachmentRenewal {
  readonly expires_at: string;
  readonly renew_after: string;
}

export interface CanvasState {
  readonly canvas_id: CanvasId;
  readonly source: CanvasSource | null;
  readonly title: string | null;
  readonly renderer: CanvasRenderer;
  readonly editable: boolean;
  readonly alt_text: string | null;
  readonly presentation_revision: number;
  readonly source_version: string | null;
  /**
   * Server-minted, authorized file-content pointer. The Cockpit may resolve
   * this relative URL against the configured API origin, but must never derive
   * a content path or its presentation/source identity query itself.
   */
  readonly content_url?: string | null;
  readonly status: CanvasStatus;
  readonly capabilities: CanvasCapabilities;
  readonly updated_at: string;
  /**
   * Where the bytes behind `content_url` come from.
   *
   * `'snapshot'` means the workspace could not be reached and the server is
   * serving the copy it kept when these bytes were published — the Canvas is
   * read-only and the content is as old as `content_captured_at`.
   *
   * ABSENT MEANS `'workspace'`. Older orchestrators do not send the field at
   * all, so absence must never be treated as unknown-and-blocked.
   */
  readonly content_origin?: CanvasContentOrigin | null;
  /** When snapshot-backed bytes were captured. ISO 8601. */
  readonly content_captured_at?: string | null;
}

export type CanvasLoadStatus = 'idle' | 'loading' | 'ready' | 'error';

export interface CanvasRequestError {
  readonly status: number | null;
  readonly code: string | null;
}

export interface CanvasContentSaveRequest {
  readonly contentUrl: string;
  readonly contentEtag: string;
  readonly presentationRevision: number;
  readonly content: string;
}

export interface CanvasMutationResponse {
  readonly state: CanvasState;
  readonly stateEtag: string;
  readonly contentEtag: string;
}

/** A Canvas state mutation which does not read or replace workspace bytes. */
export interface CanvasStateMutationResponse {
  readonly state: CanvasState;
  readonly stateEtag: string;
}
