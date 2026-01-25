/**
 * Audit step types from MongoDB agent_audit collection.
 */
export type AuditStepType =
  | 'initialize'
  | 'llm_call'
  | 'llm_response'
  | 'tool_call'
  | 'tool_result'
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
 */
export interface AuditToolInfo {
  name: string;
  arguments?: Record<string, unknown>;
  result_preview?: string;
  success?: boolean;
  error?: string;
}

/**
 * LLM interaction details within an audit entry.
 */
export interface AuditLLMInfo {
  model?: string;
  response_content_preview?: string;
  tool_calls?: Array<{ name: string; arguments?: Record<string, unknown> }>;
  input_tokens?: number;
  output_tokens?: number;
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
  phase?: string;
  tool?: AuditToolInfo;
  llm?: AuditLLMInfo;
  error?: AuditErrorInfo;
  state_snapshot?: Record<string, unknown>;
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
  status: string;
  creator_status?: string;
  validator_status?: string;
  created_at: string;
  audit_count?: number | null;
}
