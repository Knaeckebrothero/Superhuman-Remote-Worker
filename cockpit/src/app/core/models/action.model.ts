import {Notification, NotificationCategory} from './notification.model';

export type ActionItemStatus = 'pending' | 'resolved';

/**
 * One row of the action center. Since slice 3 of the unified notification
 * system every item IS a feed notification (server id, engagement state,
 * declared actions) — the client-side join over sudo requests, message
 * threads, review jobs and session events is gone.
 */
export interface ActionItem {
  /** Stable ID: `ntf:<notification id>`. */
  id: string;
  status: ActionItemStatus;
  /** 0-100, higher = more urgent */
  urgency: number;
  /** ISO 8601, used for secondary sort */
  timestamp: string;
  title: string;
  subtitle: string;
  jobId: string | null;
  notification: Notification;
  category: NotificationCategory;
}

export interface ThreadMessage {
  id: string;
  direction: 'outbound' | 'inbound';
  subject?: string;
  message: string;
  created_at: string;
  read_at: string | null;
}

export interface ThreadDetail {
  thread_id: string;
  subject: string;
  mode: string;
  status: string;
  messages: ThreadMessage[];
}
