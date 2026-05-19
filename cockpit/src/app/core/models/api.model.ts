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
// Expert Models
// =============================================================================

/**
 * Expert configuration for discovery and selection.
 */
export interface Expert {
  id: string;
  display_name: string;
  description: string;
  icon: string;
  color: string;
  tags: string[];
}

/**
 * Full expert detail including merged config and instructions content.
 * Returned by GET /api/experts/{expert_id}.
 */
export interface ExpertDetail extends Expert {
  config: Record<string, unknown>;
  instructions: string | null;
  /** Tool lists from defaults.yaml, used to re-enable expert-disabled categories. */
  defaults_tools?: Record<string, string[]>;
  /** Raw settings_matrix.yaml for client-side model-family resolution. */
  settings_matrix?: Record<string, Record<string, unknown>>;
}

// =============================================================================
// Datasource Models
// =============================================================================

/**
 * Supported datasource types.
 *
 * ``kubeconfig`` / ``ssh_key`` / ``generic_file`` are credential-file types whose
 * ``credentials.files[]`` payload is materialized as files on the agent's
 * filesystem at job start (see docs/features/credential_file_datasources.md).
 */
export type DatasourceType =
  | 'generic'
  | 'repository'
  | 'postgresql'
  | 'neo4j'
  | 'mongodb'
  | 'webdav'
  | 'kubeconfig'
  | 'ssh_key'
  | 'generic_file';

/**
 * A single file entry inside ``credentials.files[]`` for credential-file types.
 *
 * The server applies type-specific defaults (target_path, mode, env_var) when
 * the client omits them — the cockpit only needs to send ``contents`` for
 * ``kubeconfig`` and ``ssh_key``. ``generic_file`` requires ``target_path``.
 */
export interface CredentialFileEntry {
  name?: string;
  contents: string;
  target_path?: string;
  mode?: string;
  env_var?: string;
}

/**
 * Datasource configuration from the orchestrator.
 */
export interface Datasource {
  id: string;
  name: string;
  description: string | null;
  type: DatasourceType;
  connection_url: string | null;
  /**
   * F3: credentials are never returned over REST anymore — the field is
   * stripped server-side (see `redact_datasource` in
   * `orchestrator/security/access.py`). Edit forms must use the
   * "leave blank to keep existing" UX.
   */
  credentials?: Record<string, unknown>;
  cli_hint: string | null;
  default_branch: string | null;
  job_id: string | null;
  created_at: string;
  updated_at: string;
}

/**
 * Request body for creating a new datasource.
 */
export interface DatasourceCreateRequest {
  name: string;
  type: DatasourceType;
  connection_url?: string;
  description?: string;
  credentials?: Record<string, unknown>;
  job_id?: string;
  cli_hint?: string;
  default_branch?: string;
}

/**
 * Request body for updating a datasource.
 */
export interface DatasourceUpdateRequest {
  name?: string;
  description?: string;
  connection_url?: string;
  credentials?: Record<string, unknown>;
  cli_hint?: string;
  default_branch?: string;
}

/**
 * Datasource linked to a project, with project-level overrides.
 */
export interface ProjectDatasource extends Datasource {
  linked_at: string;
  project_read_only: boolean | null;
  project_description: string | null;
}

/**
 * Result from testing a datasource connection.
 */
export interface DatasourceTestResult {
  status: 'ok' | 'error';
  message: string;
}

/**
 * Response from POST /api/datasources/ssh-keys/generate.
 *
 * `private_key` is dropped into the SSH key textarea, `public_key` is shown
 * to the user so they can add it as a deploy key on their provider.
 */
export interface SSHKeyGenerateResponse {
  private_key: string;
  public_key: string;
}

// =============================================================================
// User Models
// =============================================================================

/**
 * User identity with optional email for session-based auth.
 */
export interface User {
  id: string;
  display_name: string;
  avatar_color: string;
  email?: string | null;
  default_project_id?: string | null;
  is_admin?: boolean;
  is_approved?: boolean;
  can_use_vm?: boolean;
  created_at: string;
}

/**
 * Response body from /api/admin/system-settings/vm_workspaces (GET/PUT).
 */
export interface VmWorkspacesSetting {
  enabled: boolean;
  updated_at: string | null;
  updated_by: string | null;
}

// =============================================================================
// API Key Models
// =============================================================================

/**
 * Supported LLM and tool provider slugs for API key management.
 */
export type ApiKeyProvider = 'openai' | 'anthropic' | 'google' | 'groq' | 'openrouter' | 'codex' | 'vision';

/**
 * An API key entry (as returned by GET endpoints — no full key, prefix only).
 */
export interface ApiKeyEntry {
  id: string;
  provider: ApiKeyProvider;
  key_prefix: string;
  label: string | null;
  created_at: string;
  updated_at: string;
}

/**
 * Request body for setting an API key.
 */
export interface ApiKeySetRequest {
  api_key: string;
  label?: string | null;
}

// =============================================================================
// LLM Endpoint Models
// =============================================================================

/**
 * Which slot a model fills. Chat is the default; non-chat rows are routed
 * to the matching Admin → Defaults selector (embedding/vision/whisper/tts)
 * or used as auxiliary LLMs for memory extraction / curation / title gen.
 */
export type LlmModelCapability =
  | 'chat'
  | 'vision'
  | 'embedding'
  | 'auxiliary'
  | 'whisper'
  | 'tts';

/**
 * A user-registered OpenAI-compatible LLM endpoint. Models attached to this
 * endpoint live in the catalog (`models` table) and are managed via Admin →
 * Models, not nested here. The `models` field is kept for shape compat with
 * the legacy serializer and is always an empty array.
 */
export interface LlmEndpoint {
  id: string;
  label: string;
  base_url: string;
  key_prefix: string | null;
  created_at: string | null;
  updated_at: string | null;
  models: never[];
}

export interface LlmEndpointCreateRequest {
  label: string;
  base_url: string;
  api_key?: string | null;
  allow_insecure?: boolean;
}

export interface LlmEndpointUpdateRequest {
  label?: string | null;
  base_url?: string | null;
  api_key?: string | null;
  clear_api_key?: boolean;
  allow_insecure?: boolean;
}

/**
 * Response from the test-connection probe (POST /api/settings/llm-endpoints/{id}/test).
 */
export interface LlmEndpointTestResult {
  ok: boolean;
  status: number | null;
  error: string | null;
  probe_url: string;
}

/**
 * A single model surfaced by `GET {base_url}/models` via the admin discovery
 * endpoint. Used by Admin → Models as a quick-fill helper when the admin has
 * picked a system endpoint as the catalog row's transport.
 *
 * `capability_hints` is the array of suggested capabilities — chat-capable
 * rows always include `auxiliary`.
 */
export interface LlmEndpointDiscoveredModel {
  id: string;
  owned_by: string | null;
  capability_hints: LlmModelCapability[];
  family: string | null;
  context_window: number | null;
}

export interface LlmEndpointDiscoveryResult {
  ok: boolean;
  status: number | null;
  error: string | null;
  probe_url: string;
  models: LlmEndpointDiscoveredModel[];
}

/**
 * One row in the Admin → Providers post-save discovery dialog. Comes from
 * the orchestrator's `discover_models()` per-provider clients (OpenAI,
 * Google, Groq, OpenRouter); Anthropic + the env-bridge `vision` provider
 * never produce these — admins add those models manually.
 */
export interface DiscoveryCandidate {
  model_id: string;
  detected_family: string;
  /** True iff `detected_family` has a non-default entry in
   * `model_config_matrix.yaml` — i.e. we ship custom prompts/settings. The
   * dialog defaults supported rows to checked. */
  supported: boolean;
  /** Suggested capabilities array. Chat-capable models include `auxiliary`
   * automatically; multimodal chat families also include `vision`. */
  suggested_capabilities: CatalogCapability[];
  suggested_display_label: string;
}

/** Wrapper stored on `system_api_keys.discovery_cache_json`. */
export interface DiscoveryPayload {
  provider: string;
  fetched_at: string;
  count: number;
  candidates: DiscoveryCandidate[];
}

/** Response shape for `GET /api/admin/providers/keys/{provider}/discovery`
 * and `POST .../rediscover`. ``ready=false`` means no probe has completed
 * yet (e.g. the async post-save probe is still in flight). */
export interface DiscoveryResponse {
  ready: boolean;
  fresh: boolean;
  payload: DiscoveryPayload | null;
  cached_at: string | null;
}

// =============================================================================
// Models Catalog (Admin → Models)
// =============================================================================

/** Locked enum for catalog rows. Adding a capability requires schema + resolver work. */
export type CatalogCapability = 'chat' | 'auxiliary' | 'embedding' | 'vision' | 'whisper' | 'tts';

/** Provider anchor for a catalog row. */
export type CatalogProviderKind = 'system' | 'endpoint';

export const CATALOG_CAPABILITIES: CatalogCapability[] = [
  'chat', 'auxiliary', 'embedding', 'vision', 'whisper', 'tts',
];

/**
 * A row in the admin-curated `models` table. Mirrors orchestrator
 * `_serialize_catalog_model`.
 *
 * `capabilities` is the source of truth: a single row can claim multiple
 * roles — e.g. `['chat','auxiliary']` for a chat-capable LLM,
 * `['chat','auxiliary','vision']` for a multimodal one.
 */
export interface CatalogModel {
  id: string;
  provider_kind: CatalogProviderKind;
  provider_ref: string;
  model_id: string;
  display_label: string;
  capabilities: CatalogCapability[];
  family: string;
  context_window: number | null;
  reasoning_level: string | null;
  params_json: Record<string, unknown> | null;
  enabled: boolean;
  seeded_from: string | null;
  notes: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface CatalogModelCreateRequest {
  provider_kind: CatalogProviderKind;
  provider_ref: string;
  model_id: string;
  display_label: string;
  /** Capabilities the row claims. Required, non-empty. */
  capabilities: CatalogCapability[];
  family: string;
  context_window?: number | null;
  reasoning_level?: string | null;
  params_json?: Record<string, unknown> | null;
  enabled?: boolean;
  notes?: string | null;
}

export interface CatalogModelUpdateRequest {
  provider_kind?: CatalogProviderKind;
  provider_ref?: string;
  model_id?: string;
  display_label?: string;
  capabilities?: CatalogCapability[];
  family?: string;
  context_window?: number | null;
  reasoning_level?: string | null;
  params_json?: Record<string, unknown> | null;
  enabled?: boolean;
  notes?: string | null;
}

export interface CatalogModelTestResult {
  ok: boolean;
  status: number | null;
  error: string | null;
  probe_url: string | null;
}

export interface CatalogModelDeleteResult {
  status: string;
  id: string;
  warning: string | null;
}

/**
 * Resolved default values for every user preference field.
 * Returned by the backend in the `_resolved` key of GET /api/settings/preferences.
 */
export interface ResolvedDefaults {
  default_model?: string;
  default_autonomy?: string;
  default_reasoning_level?: string;
  default_auxiliary_model?: string;
  default_vision_model?: string;
  default_whisper_model?: string;
  default_embedding_model?: string;
  embedding_provider?: string;
  persistent_agent?: {
    model?: string;
    permission_mode?: string;
    idle_timeout_minutes?: number;
    config_name?: string;
  };
}

/**
 * User preference settings.
 */
export interface UserSettings {
  default_model?: string | null;
  default_autonomy?: string | null;
  default_reasoning_level?: string | null;
  default_auxiliary_model?: string | null;
  default_vision_model?: string | null;
  default_whisper_model?: string | null;
  default_builder_model?: string | null;
  default_session_model?: string | null;
  default_strategic_model?: string | null;
  default_tactical_model?: string | null;
  default_embedding_model?: string | null;
  embedding_provider?: string | null;
  language?: 'en' | 'de-DE' | null;
  communication?: CommunicationSettings | null;
  persistent_agent?: PersistentAgentSettings | null;
  _resolved?: ResolvedDefaults;
}

/**
 * User settings for persistent agent sessions.
 */
export interface PersistentAgentSettings {
    model?: string | null;
    permission_mode?: string | null;
    config_name?: string | null;
    greeting?: string | null;
    idle_timeout_minutes?: number | null;
    command_allowlist?: string[] | null;
    // Phase 6 headless controls. Backend reads these as direct children of
    // users.settings.persistent_agent (see orchestrator/main.py create_thread
    // merge + attention_sleep_sweeper COALESCE).
    headless_mode?: 'eager' | 'polite' | null;
    headless_attention_sleep_minutes?: number | null;
    notification_channels?: string[] | null;
}

/**
 * Codex proxy status (admin-only, from CLIProxyAPI management API).
 */
export interface CodexStatus {
  connected: boolean;
  accounts: { name: string; status: string; status_message?: string }[];
  model_count: number;
}

/**
 * Communication delivery preferences (Phase 3 Live Communication).
 */
export interface CommunicationSettings {
  delivery?: {
    async_reply?: 'immediate_interrupt' | 'next_strategic_phase' | 'llm_triage';
    urgent_override?: boolean;
  };
  channels?: {
    email?: boolean;
    cockpit?: boolean;
    ntfy?: boolean;
    slack_webhook?: boolean;
    discord_webhook?: boolean;
  };
  quiet_hours?: {
    enabled?: boolean;
    start?: string;
    end?: string;
    timezone?: string;
  };
}

/**
 * A notification entry from the orchestrator.
 */
export interface AppNotification {
  id: string;
  job_id: string | null;
  thread_id: string | null;
  subject: string;
  message: string;
  job_description: string | null;
  config_name: string | null;
  status: string;
  read_at: string | null;
  created_at: string;
}

/**
 * An external contact linked to a project.
 */
export interface ExternalContact {
  id: string;
  display_name: string;
  email: string;
  created_at: string | null;
}

// =============================================================================
// MCP Token Models
// =============================================================================

/**
 * An MCP API token (as returned by GET /api/mcp-tokens — no plaintext).
 */
export interface McpToken {
  id: string;
  name: string;
  token_prefix: string;
  scope: string;
  origin: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
  created_at: string;
}

/**
 * Request body for creating an MCP token.
 */
export interface McpTokenCreateRequest {
  name: string;
  scope?: string;
  expires_in_days?: number | null;
}

/**
 * Response from POST /api/mcp-tokens — includes plaintext token (shown once).
 */
export interface McpTokenCreateResponse extends McpToken {
  token: string;
}

// =============================================================================
// Project Models
// =============================================================================

/**
 * Project status types.
 */
export type ProjectStatus = 'active' | 'archived' | 'deleted';

/**
 * Project member role types.
 */
export type ProjectMemberRole = 'owner' | 'editor' | 'viewer';

/**
 * Project repository role types.
 */
export type ProjectRepoRole = 'jobs' | 'source' | 'reference';

/**
 * Project from the orchestrator.
 */
export interface Project {
  id: string;
  name: string;
  description?: string | null;
  goal?: string | null;
  status: ProjectStatus;
  is_default: boolean;
  default_config_name?: string | null;
  default_config_override?: Record<string, unknown> | null;
  nextcloud_folder_id?: number | null;
  cloud_storage_read_only?: boolean;
  cloud_storage_url?: string | null;
  network_tier?: ProjectNetworkTier;
  created_at: string;
  updated_at: string;
  job_count?: number;
  repo_count?: number;
  member_count?: number;
}

/**
 * Workspace egress tier for a project. The set must stay in sync with
 * the CHECK constraint in 0016_project_network_tier.sql and the
 * `workspace.networkPolicy.tiers` list in helm values.
 */
export type ProjectNetworkTier = 'internet-only' | 'home-allowed';

/**
 * Request body for creating a new project.
 */
export interface ProjectCreateRequest {
  name: string;
  description?: string;
  goal?: string;
  default_config_name?: string;
  default_config_override?: Record<string, unknown>;
  user_id: string;
}

/**
 * Request body for updating a project.
 */
export interface ProjectUpdateRequest {
  name?: string;
  description?: string;
  goal?: string;
  status?: ProjectStatus;
  default_config_name?: string;
  default_config_override?: Record<string, unknown>;
  cloud_storage_read_only?: boolean;
  network_tier?: ProjectNetworkTier;
}

/**
 * Project member with user info.
 */
export interface ProjectMember {
  project_id: string;
  user_id: string;
  role: ProjectMemberRole;
  joined_at: string;
  display_name?: string;
  avatar_color?: string;
}

/**
 * Request body for adding a project member.
 */
export interface ProjectMemberAddRequest {
  user_id: string;
  role?: ProjectMemberRole;
}

/**
 * Request body for updating a project member's role.
 */
export interface ProjectMemberUpdateRequest {
  role: ProjectMemberRole;
}

/**
 * Project repository configuration.
 */
export interface ProjectRepository {
  id: string;
  project_id: string;
  name: string;
  description?: string | null;
  repo_url?: string | null;
  role: ProjectRepoRole;
  read_only: boolean;
  is_managed: boolean;
  branch?: string | null;
  clone_path?: string | null;
  credentials?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

/**
 * Request body for creating/attaching a project repository.
 */
export interface ProjectRepositoryCreateRequest {
  name: string;
  description?: string;
  repo_url?: string;
  role?: ProjectRepoRole;
  read_only?: boolean;
  branch?: string;
  clone_path?: string;
  create_managed?: boolean;
}

/**
 * Request body for updating a project repository.
 */
export interface ProjectRepositoryUpdateRequest {
  name?: string;
  description?: string;
  read_only?: boolean;
  branch?: string;
  clone_path?: string;
}

/**
 * Request body for promoting a default-project job into a named project.
 */
export interface PromoteRequest {
  name: string;
  description?: string;
  goal?: string;
  user_id: string;
}

// =============================================================================
// Persistent Thread (Session) Models
// =============================================================================

export type ThreadStatus = 'created' | 'active' | 'ended';

/**
 * Persistent agent session thread.
 */
export interface Thread {
  id: string;
  title: string;
  status: ThreadStatus;
  config_name: string;
  permission_mode: string;
  user_id?: string | null;
  project_id?: string | null;
  agent_id?: string | null;
  created_at: string;
  last_activity: string;
  ended_at?: string | null;
  total_turns: number;
  total_tokens: number;
  nc_session_folder?: string | null;
  nc_share_id?: number | null;
  cloud_session_url?: string | null;
  metadata?: Record<string, unknown>;
}

// =============================================================================
// Knowledge Base Models
// =============================================================================

/**
 * Knowledge note types.
 */
export type KnowledgeNoteType =
  | 'goal'
  | 'plan'
  | 'decision'
  | 'learning'
  | 'code'
  | 'source'
  | 'question'
  | 'state'
  | 'retrospective';

/**
 * Knowledge note status types.
 */
export type KnowledgeNoteStatus = 'active' | 'resolved' | 'superseded' | 'archived';

/**
 * Knowledge note summary (list view).
 */
export interface KnowledgeNote {
  id: string;
  note_id: string;
  title: string;
  note_type: KnowledgeNoteType;
  status: KnowledgeNoteStatus;
  confidence?: string | null;
  tags: string[];
  keywords: string[];
  job_id?: string | null;
  phase?: number | null;
  content_preview?: string;
  content?: string;
  created_at: string;
  modified_at: string;
}

/**
 * Knowledge note relationship (from Neo4j).
 */
export interface KnowledgeRelationship {
  type: string;
  direction: string;
  target_id: string;
  target_title?: string;
}

/**
 * Full knowledge note detail (single note view).
 */
export interface KnowledgeNoteDetail extends KnowledgeNote {
  content: string;
  retrieval_messages?: string[];
  content_hash?: string;
  relationships: KnowledgeRelationship[];
}

/**
 * Knowledge base summary statistics.
 */
export interface KnowledgeSummary {
  total: number;
  by_type: Record<string, number>;
  by_status: Record<string, number>;
  recent: { note_id: string; title: string; note_type: string; status: string; modified_at: string }[];
}

/**
 * Paginated knowledge note list response.
 */
export interface KnowledgeListResponse {
  notes: KnowledgeNote[];
  total: number;
  limit: number;
  offset: number;
}

/**
 * Knowledge search response.
 */
export interface KnowledgeSearchResponse {
  notes: KnowledgeNote[];
  query: string;
  total: number;
}

// =============================================================================
// Memory Models
// =============================================================================

export type MemoryType = 'factual' | 'procedural' | 'error_solution' | 'vocabulary' | 'relational';
export type MemorySource = 'observer' | 'todo' | 'compaction' | 'phase_archive' | 'tool_error';

export interface Memory {
  id: string;
  job_id: string;
  project_id?: string | null;
  agent_id?: string | null;
  content_preview: string;
  summary?: string | null;
  memory_type: MemoryType;
  source: MemorySource;
  keywords: string[];
  importance: number;
  source_turn_start?: number | null;
  source_turn_end?: number | null;
  source_phase?: number | null;
  token_count: number;
  access_count: number;
  created_at: string;
  last_accessed: string;
}

export interface MemoryListResponse {
  memories: Memory[];
  total: number;
  limit: number;
  offset: number;
}

export interface MemoryStats {
  total: number;
  total_tokens: number;
  total_accesses: number;
  factual: number;
  procedural: number;
  error_solution: number;
  vocabulary: number;
  relational: number;
  from_observer: number;
  from_todo: number;
  from_compaction: number;
  from_phase_archive: number;
  from_tool_error: number;
  avg_importance: number | null;
}

export type MemorySortField = 'created_at' | 'importance' | 'access_count' | 'token_count' | 'last_accessed';

// =============================================================================
// Agent Models
// =============================================================================

/**
 * Agent status types.
 */
export type AgentStatus = 'booting' | 'ready' | 'working' | 'session' | 'draining' | 'completed' | 'failed' | 'offline';

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
export type JobStatus = 'created' | 'pending' | 'processing' | 'completed' | 'failed' | 'cancelled' | 'pending_review' | 'paused' | 'reviewing' | 'waiting';

/**
 * Job from the orchestrator.
 */
export interface Job {
  id: string;
  description: string;
  document_path?: string;
  config_name: string;
  config_override?: Record<string, unknown>;
  assigned_agent_id?: string;
  user_id?: string | null;
  project_id?: string | null;
  parent_job_id?: string | null;
  creation_order?: number | null;
  delegation_context?: string | null;
  worktree_path?: string | null;
  repo_name?: string | null;
  branch_name?: string | null;
  merge_status?: string | null;
  priority?: number;
  status: JobStatus;
  created_at: string;
  updated_at?: string;
  completed_at?: string;
  error_message?: string;
  audit_count?: number;
  context?: Record<string, any> | null;
}

/**
 * Request body for creating a new job.
 */
export interface JobCreateRequest {
  description: string;
  upload_id?: string;
  config_upload_id?: string;
  instructions_upload_id?: string;
  document_path?: string;
  document_dir?: string;
  config_name?: string;
  config_override?: Record<string, unknown>;
  context?: Record<string, unknown>;
  instructions?: string;
  kickoff_message?: string;
  datasource_ids?: string[];
  builder_session_id?: string;
  user_id?: string;
  project_id?: string;
  priority?: number;
}

/**
 * Job progress with ETA calculation.
 */
export interface JobProgress {
  job_id: string;
  status: JobStatus;
  progress_percent: number;
  elapsed_seconds: number;
  eta_seconds?: number;
  created_at?: string;
  updated_at?: string;
  completed_at?: string;
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
  description: string;
  status: string;
  created_at: string;
  updated_at: string;
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
