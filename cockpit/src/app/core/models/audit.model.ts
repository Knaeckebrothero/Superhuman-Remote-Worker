/**
 * Audit step types from MongoDB agent_audit collection.
 */
export type AuditStepType =
  | 'initialize'
  | 'llm'           // Combined: replaces llm_call + llm_response
  | 'tool'          // Combined: replaces tool_call + tool_result
  | 'check'
  | 'routing'
  | 'phase_complete'
  | 'error';

/**
 * Filter categories for audit entries.
 */
export type AuditFilterCategory = 'all' | 'messages' | 'tools' | 'errors';

/**
 * Tool execution details within an audit entry.
 * Contains both call info (arguments) and result info (result_preview, success).
 * Result fields are null while the tool is executing.
 */
export interface AuditToolInfo {
  name: string;
  call_id?: string;
  arguments?: Record<string, unknown>;
  // Result fields - null while pending
  result_preview?: string | null;
  result_size_bytes?: number | null;
  success?: boolean | null;
  error?: string | null;
}

/**
 * LLM interaction details within an audit entry.
 * Contains both call info (model, input_message_count) and response info.
 * Response fields are null while waiting for LLM response.
 */
export interface AuditLLMInfo {
  model?: string;
  input_message_count?: number;
  // Response fields - null while pending
  request_id?: string | null;
  response_content_preview?: string | null;
  tool_calls?: Array<{ name: string; call_id?: string }> | null;
  metrics?: {
    output_chars?: number;
    tool_call_count?: number;
  } | null;
}

/**
 * Error details within an audit entry.
 */
export interface AuditErrorInfo {
  type: string;
  message: string;
  traceback?: string;
}

/**
 * Single audit entry from the agent_audit MongoDB collection.
 */
export interface AuditEntry {
  _id: string;
  job_id: string;
  step_number: number;
  step_type: AuditStepType;
  node_name: string;
  timestamp: string;
  latency_ms?: number;
  iteration: number;
  phase?: string;           // "strategic" | "tactical"
  phase_number?: number;    // 0, 1, 2, ...
  tool?: AuditToolInfo;
  llm?: AuditLLMInfo;
  error?: AuditErrorInfo;
  state?: Record<string, unknown>;
  // Timing fields for combined events
  started_at?: string;
  completed_at?: string | null;  // null = in progress
}

/**
 * Paginated response from the audit API endpoint.
 */
export interface AuditResponse {
  entries: AuditEntry[];
  total: number;
  page: number;
  pageSize: number;
  hasMore: boolean;
  error?: string;
}

/**
 * Job summary from PostgreSQL jobs table.
 */
export interface JobSummary {
  id: string;
  prompt: string;
  status: string;
  creator_status?: string;
  validator_status?: string;
  created_at: string;
  audit_count?: number | null;
}
