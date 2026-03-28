import {SudoRequest} from '../services/sudo.service';

export type ActionItemType = 'sudo' | 'message' | 'review';
export type ActionItemStatus = 'pending' | 'resolved';

export interface ActionItem {
  /** Stable ID: prefixed to avoid collisions (e.g., "sudo:uuid", "msg:thread_id", "rev:job_id") */
  id: string;
  type: ActionItemType;
  status: ActionItemStatus;
  /** 0-100, higher = more urgent */
  urgency: number;
  /** ISO 8601, used for secondary sort */
  timestamp: string;
  title: string;
  subtitle: string;
  jobId: string | null;

  // Discriminated payload - exactly one is populated
  sudo?: SudoRequest;
  message?: MessageActionData;
  review?: ReviewActionData;
}

export interface MessageActionData {
  threadId: string;
  subject: string;
  mode: 'blocking' | 'async';
  lastMessage: string;
  configName: string | null;
  jobDescription: string | null;
  unread: boolean;
}

export interface ReviewActionData {
  jobId: string;
  jobDescription: string;
  configName: string | null;
  freezeType: 'job_complete' | 'phase_boundary' | 'vm_upgrade_required';
  phaseType: string | null;
  phaseNumber: number | null;
  summary: string | null;
  confidence: number | null;
  deliverables: string[];
  frozenAt: string | null;
  command: string | null;
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

export interface PendingActionCounts {
  counts: {
    sudo: number;
    messages: number;
    reviews: number;
    total: number;
  };
  most_urgent: {
    type: string;
    id: string;
    title: string;
    expires_in_seconds: number;
  } | null;
}
