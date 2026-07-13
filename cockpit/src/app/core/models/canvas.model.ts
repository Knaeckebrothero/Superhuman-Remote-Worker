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

export type CanvasRenderer = 'auto' | 'markdown' | 'text' | 'html' | 'image';

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
  /**
   * Positive server capability for creating an isolated live-app attachment.
   * Optional while older/default-off orchestrators still return the Slice 3A
   * capability shape; absence always means unsupported.
   */
  readonly can_create_viewer_session?: boolean;
}

export interface CanvasViewAttachment {
  readonly attachment_id: string;
  readonly origin: string;
  readonly bootstrap_url: string;
  readonly expires_at: string;
  readonly renew_after: string;
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
