/**
 * The unified notification feed (knowledge-base/knowledge/features/
 * unified_notification_system.md). One durable row per recipient; every
 * channel (email, ntfy, …) is a delivery OF a row, never an independent send.
 *
 * The center never learns what a category *means*: it renders the row, the
 * server-declared `actions`, and POSTs `{action_type, params}` back (D7).
 * Category-specific meaning lives in server-side handlers.
 */

export type NotificationSeverity = 'low' | 'normal' | 'high' | 'critical';

/** Known categories (slice 1). Open-ended on purpose: a new server category
 *  renders generically until the cockpit learns an icon/label for it. */
export type NotificationCategory =
  | 'review_queue'
  | 'vm_upgrade'
  | 'budget_exceeded'
  | 'incident'
  | 'officer_question'
  | 'officer_runtime'
  | (string & {});

export type NotificationActionStyle = 'default' | 'primary' | 'danger';
export type NotificationActionInput = 'text' | 'textarea';

/** One server-declared action. `label_key` is a `notifications.actions.*`
 *  transloco key; `input` actions collect `params[input_name]` before posting. */
export interface NotificationAction {
  type: string;
  label_key: string;
  style: NotificationActionStyle;
  input?: NotificationActionInput | null;
  input_name?: string | null;
  params: Record<string, unknown>;
}

export interface SourceRef {
  kind: string;
  id: string;
}

export interface Notification {
  id: string;
  category: NotificationCategory;
  severity: NotificationSeverity;
  subject: string;
  body: string;
  source_ref: SourceRef | null;
  actions: NotificationAction[];
  payload: Record<string, unknown>;
  created_at: string | null;
  /** Rendered in the feed (Knock "seen"). */
  seen_at: string | null;
  /** Explicitly opened / marked. */
  read_at: string | null;
  /** An action was taken on it. */
  interacted_at: string | null;
  /** The underlying source was settled — by anyone (an officer approving
   *  the job resolves the human's row too). */
  resolved_at: string | null;
  resolved_by: string | null;
  archived_at: string | null;
}

export interface NotificationCounts {
  unseen: number;
  unread: number;
  pending: number;
  by_category: Record<string, {pending: number; unseen: number}>;
}

/** `notification.updated` SSE frame / partial patch: only changed fields. */
export type NotificationUpdate = {id: string} & Partial<
  Pick<
    Notification,
    'seen_at' | 'read_at' | 'interacted_at' | 'resolved_at' | 'resolved_by' | 'archived_at'
  >
>;

export interface NotificationFeedPage {
  items: Notification[];
  next_before: string | null;
  counts: NotificationCounts;
}

/** `GET /api/notifications/{id}` — the row plus its source's presentation
 *  payload (job + freeze data, sudo request row, thread summary). */
export interface NotificationDetail {
  notification: Notification;
  source: Record<string, unknown> | null;
}

export interface NotificationActResponse {
  status: string;
  result: Record<string, unknown>;
  notification: Notification;
}

export const EMPTY_NOTIFICATION_COUNTS: NotificationCounts = {
  unseen: 0,
  unread: 0,
  pending: 0,
  by_category: {},
};

/** Sort weight for the action-center comparator: pending rows by severity. */
export const SEVERITY_URGENCY: Record<NotificationSeverity, number> = {
  critical: 95,
  high: 75,
  normal: 45,
  low: 25,
};
