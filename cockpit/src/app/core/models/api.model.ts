/**
 * API data models for the Debug Cockpit.
 */

/**
 * Table metadata from the API.
 */
export interface TableInfo {
  name: string;
  rowCount: number;
}

/**
 * Column definition for a database table.
 */
export interface ColumnDef {
  name: string;
  type: 'string' | 'number' | 'boolean' | 'date' | 'json' | 'binary';
  nullable: boolean;
}

/**
 * Paginated table data response.
 */
export interface TableDataResponse {
  columns: ColumnDef[];
  rows: Record<string, unknown>[];
  total: number;
  page: number;
  pageSize: number;
}

/**
 * Pagination state.
 */
export interface PaginationState {
  page: number;
  pageSize: number;
  total: number;
}

// =============================================================================
// Agent Models
// =============================================================================

/**
 * Agent status types.
 */
export type AgentStatus = 'booting' | 'ready' | 'working' | 'completed' | 'failed' | 'offline';

/**
 * Registered agent from the orchestrator.
 */
export interface Agent {
  id: string;
  config_name: string;
  hostname?: string;
  pod_ip?: string;
  pod_port: number;
  pid?: number;
  status: AgentStatus;
  current_job_id?: string;
  registered_at: string;
  last_heartbeat: string;
  metadata?: Record<string, unknown>;
}

// =============================================================================
// Job Models
// =============================================================================

/**
 * Job status types.
 */
export type JobStatus = 'created' | 'pending' | 'processing' | 'completed' | 'failed' | 'cancelled';

/**
 * Job from the orchestrator.
 */
export interface Job {
  id: string;
  prompt: string;
  document_path?: string;
  status: JobStatus;
  creator_status: string;
  validator_status: string;
  created_at: string;
  updated_at?: string;
  completed_at?: string;
  error_message?: string;
  audit_count?: number;
}

/**
 * Request body for creating a new job.
 */
export interface JobCreateRequest {
  prompt: string;
  upload_id?: string;
  config_upload_id?: string;
  instructions_upload_id?: string;
  document_path?: string;
  document_dir?: string;
  config_name?: string;
  context?: Record<string, unknown>;
  instructions?: string;
}

/**
 * Requirement counts by status.
 */
export interface RequirementSummary {
  pending: number;
  validating: number;
  integrated: number;
  rejected: number;
  failed: number;
  total: number;
}

/**
 * Job progress with ETA calculation.
 */
export interface JobProgress {
  job_id: string;
  status: JobStatus;
  creator_status: string;
  validator_status: string;
  requirements: RequirementSummary;
  progress_percent: number;
  elapsed_seconds: number;
  eta_seconds?: number;
  created_at?: string;
  updated_at?: string;
  completed_at?: string;
}

/**
 * Requirement from a job.
 */
export interface Requirement {
  id: string;
  requirement_id?: string;
  name?: string;
  text: string;
  type?: string;
  priority?: string;
  status: string;
  source_document?: string;
  gobd_relevant?: boolean;
  gdpr_relevant?: boolean;
  quality_score?: number;
  fulfillment_status?: string;
  neo4j_id?: string;
  created_at: string;
  updated_at?: string;
}

/**
 * Response for requirements list endpoint.
 */
export interface RequirementsResponse {
  requirements: Requirement[];
  total: number;
  limit: number;
  offset: number;
}

// =============================================================================
// Statistics Models
// =============================================================================

/**
 * Overall job statistics.
 */
export interface JobStatistics {
  total_jobs: number;
  created: number;
  processing: number;
  completed: number;
  failed: number;
  cancelled: number;
}

/**
 * Daily job statistics.
 */
export interface DailyStatistics {
  date: string;
  jobs_created: number;
  jobs_completed: number;
  jobs_failed: number;
  jobs_cancelled: number;
}

/**
 * Agent workforce summary.
 */
export interface AgentStatistics {
  total: number;
  booting: number;
  ready: number;
  working: number;
  completed: number;
  failed: number;
  offline: number;
}

/**
 * Stuck job with reason.
 */
export interface StuckJob {
  id: string;
  prompt: string;
  status: string;
  creator_status: string;
  validator_status: string;
  created_at: string;
  updated_at: string;
  pending_requirements: number;
  integrated_requirements: number;
  stuck_reason: string;
  stuck_component: string;
}

// =============================================================================
// Workspace Models
// =============================================================================

/**
 * Workspace file metadata.
 */
export interface WorkspaceFile {
  name: string;
  size: number;
  modified: number;
}

/**
 * Workspace overview for a job.
 */
export interface WorkspaceOverview {
  job_id: string;
  has_workspace: boolean;
  files: WorkspaceFile[];
  workspace_md?: string;
  plan_md?: string;
  todos?: {
    todos: unknown[];
    source: string;
    is_current: boolean;
  };
  archive_count: number;
}
