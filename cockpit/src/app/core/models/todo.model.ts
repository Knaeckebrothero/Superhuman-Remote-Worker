/**
 * Todo item from workspace.
 */
export interface TodoItem {
  id?: string;
  content: string;
  status: 'pending' | 'in_progress' | 'completed';
  priority?: 'high' | 'medium' | 'low';
  notes?: string[];
  created_at?: string;
}

/**
 * Current todos from todos.yaml.
 */
export interface CurrentTodos {
  todos: TodoItem[];
  source: string;
  is_current: boolean;
}

/**
 * Archive metadata from list endpoint.
 */
export interface TodoArchiveInfo {
  filename: string;
  phase_name: string;
  timestamp: string | null;
  path: string;
}

/**
 * Parsed archived todos.
 */
export interface ArchivedTodos {
  source: string;
  is_current: boolean;
  todos: TodoItem[];
  summary: {
    total?: number;
    completed?: number;
    not_completed?: number;
  };
  phase_name: string | null;
  archived_at: string | null;
  failure_note: string | null;
}

/**
 * Combined todos response from /api/jobs/{job_id}/todos.
 */
export interface JobTodos {
  job_id: string;
  current: CurrentTodos | null;
  archives: TodoArchiveInfo[];
  has_workspace: boolean;
}
