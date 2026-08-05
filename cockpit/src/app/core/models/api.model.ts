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
  /** Slug used to reference the expert by name (e.g. a loop's role_sequence).
   *  For bundled experts this equals `id`; for DB experts it's the name column. */
  name?: string;
  display_name: string;
  description: string;
  icon: string;
  color: string;
  tags: string[];
  /** 'bundled' (disk config) | 'user' | 'global' | 'managed' (DB-backed). DB experts are
   *  selected via expert_id; bundled experts via config_name. */
  source?: string;
  storage_kind?: 'bundled' | 'db';
  owner_id?: string | null;
  managed_key?: string | null;
  /** 'worker' | 'session'. */
  expert_type?: string;
}

export interface ExpertDefaultSlot {
  application: Expert | null;
  personal: Expert | null;
  effective: Expert | null;
  source: 'project' | 'user' | 'application' | 'explicit';
}

export interface ExpertDefaultsResponse {
  personal_defaults_allowed: boolean;
  defaults: {
    worker: ExpertDefaultSlot;
    session: ExpertDefaultSlot;
  };
}

/**
 * Full expert detail including merged config and instructions content.
 * Returned by GET /api/experts/{expert_id}.
 */
/** Provenance of a slot's effective model (what runs if the picker is untouched). */
export type EffectiveModelSource =
  | 'expert'
  | 'account_default'
  | 'system_default'
  | 'project';

export interface EffectiveModelSlot {
  model: string | null;
  source: EffectiveModelSource;
}

/**
 * Per-slot effective model + provenance, computed server-side with the same
 * precedence dispatch uses. Lets the picker show the model that will actually
 * run if the user makes no change. See Layer 3 in the issue doc
 * loop_ran_codex_spark_not_selected_model_then_hung_on_cooldown.md.
 */
export interface EffectiveModels {
  strategic: EffectiveModelSlot;
  tactical: EffectiveModelSlot;
  /** Reader model for spawn_subagent delegation. Resolves subagent → tactical →
   *  base server-side, mirroring the agent's reader-model fallback. */
  subagent: EffectiveModelSlot;
  session: EffectiveModelSlot;
}

export interface ExpertDetail extends Expert {
  config: Record<string, unknown>;
  instructions: string | null;
  /**
   * Categories that refuse `tools.<c>: true` at the write boundary, mapped to
   * the enumeration to send instead. Registry-derived, served rather than
   * mirrored — it is what lets a form with no resolved read offer "shell on"
   * instead of emitting a request the boundary rejects.
   */
  enumerate_only?: Record<string, string[]>;
  /** Raw settings_matrix.yaml for client-side model-family resolution. */
  settings_matrix?: Record<string, Record<string, unknown>>;
  /** Effective model + provenance per slot (server-resolved). */
  effective_models?: EffectiveModels | null;
  /** DB-backed experts only — present on create/update responses + detail. */
  name?: string;
  owner_id?: string;
  version?: number;
  /** Prompt segments for DB-backed experts: persona, instructions, and (Part 2)
   *  strategic, tactical, summarization. Empty/absent segments inherit the base. */
  prompts?: Record<string, unknown>;
}

/**
 * Response of POST /api/experts/{id}/duplicate. The forked row (same shape
 * as ExpertDetail) plus `dropped` — not folded into ExpertDetail itself
 * because no other expert endpoint ever sets this field.
 *
 * 2026-08-04 decision: a source config may need a capability grant (e.g.
 * `shell_tools`) the copier does not hold. Duplicate strips what's ungranted
 * rather than refusing the fork (refusing blocked "start from scholar" for
 * every default-grants user), and reports what it removed here so the
 * strip is never silent — see docs/done/global_expert_management.md decision 9.
 */
export interface ExpertDuplicateResult extends ExpertDetail {
  /**
   * Capability grant keys stripped from the copy because the copier doesn't
   * hold them (e.g. `"shell_tools"`, `"delegation"`) — the same names shown
   * in the Admin UI's grants panel. Empty or absent when nothing was
   * stripped (the copier already held everything the source needed).
   */
  dropped?: string[];
}

/**
 * Response of POST /api/expert-defaults/{type}/fork — `duplicate` plus
 * "select the copy as my default" (task 4 of the same 2026-08-04 plan as
 * `ExpertDuplicateResult` above). `default` is the new personal-default row,
 * in the same summary shape `GET /api/expert-defaults` returns for a slot
 * (not a full `ExpertDetail`: no `config`/`instructions`).
 */
export interface ExpertDefaultForkResult {
  default: Expert | null;
  source: 'user';
  /** Same meaning as `ExpertDuplicateResult.dropped` above — the grant keys
   *  the fork needed that this caller doesn't hold, stripped rather than
   *  refusing the fork. Empty or absent when nothing was stripped. */
  dropped?: string[];
}

/**
 * Create a DB-backed expert (POST /api/experts). The save-time hard-deny scan
 * runs server-side on ``config``; per-user grants are a later slice.
 */
export interface ExpertCreateRequest {
  name: string;
  display_name: string;
  expert_type: 'worker' | 'session';
  description?: string | null;
  icon?: string;
  color?: string;
  tags?: string[];
  config?: Record<string, unknown>;
  prompts?: Record<string, unknown>;
}

/**
 * Patch a DB-backed expert (PUT /api/experts/{id}). ``name`` and
 * ``expert_type`` are immutable, so they are absent.
 */
export type ExpertUpdateRequest = Partial<
  Omit<ExpertCreateRequest, 'name' | 'expert_type'>
>;

// =============================================================================
// Skill Models (Agent Skills — the open SKILL.md standard)
// =============================================================================

/** Skill catalog entry (GET /api/skills). */
export interface Skill {
  id: string;
  name: string;
  display_name: string;
  description: string;
  icon: string;
  color: string;
  tags: string[];
  /** 'bundled' (disk) | 'user' | 'global' (DB-backed). */
  source?: string;
}

/** Full skill detail incl. the file tree (GET /api/skills/{id}). */
export interface SkillDetail extends Skill {
  /** path -> content; always includes 'SKILL.md' (the canonical artifact). */
  files: Record<string, string>;
  version?: number;
  owner_id?: string;
}

/**
 * Create a DB-backed skill (POST /api/skills). name + description are parsed
 * from the SKILL.md frontmatter server-side, not sent separately.
 */
export interface SkillCreateRequest {
  files: Record<string, string>;
  display_name?: string | null;
  icon?: string;
  color?: string;
  tags?: string[];
}

/** Patch a DB-backed skill (PUT /api/skills/{id}). name is immutable, so absent. */
export interface SkillUpdateRequest {
  files?: Record<string, string>;
  display_name?: string;
  icon?: string;
  color?: string;
  tags?: string[];
  is_global?: boolean;
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
  | 'kb'
  | 'postgresql'
  | 'neo4j'
  | 'mongodb'
  | 'webdav'
  | 'email'
  | 'mcp'
  | 'kubeconfig'
  | 'ssh_key'
  | 'generic_file';

/**
 * Access tier for an ``email`` datasource (see docs/features/email_datasource.md).
 * ``draft`` is the default: the agent composes drafts, the human sends them.
 */
export type EmailAccessTier = 'read' | 'read_write' | 'draft' | 'send';

/**
 * Forge (git host software) for a ``repository`` datasource. Required by the
 * server (``orchestrator/main.py`` ``_normalize_repository_config``): it
 * infers ``github``/``gitlab`` from a github.com/gitlab.com connection URL,
 * but a self-hosted host must declare this explicitly — a self-hosted Gitea
 * and a self-hosted GitLab are indistinguishable by URL alone.
 */
export type RepositoryForge = 'github' | 'gitea' | 'gitlab';

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

/** Non-secret, type-specific datasource configuration. */
export interface DatasourceConfig {
  /** Repository-relative POSIX root containing OKF Markdown notes. */
  root_path?: string;
  /** Repository: which forge this connector targets — see ``RepositoryForge``. */
  forge?: RepositoryForge;
  /** Email: access tier (tool-layer enforced; ``draft`` is the default). */
  access?: EmailAccessTier;
  /** Email: folder allowlist; empty = whole mailbox (rejected for ``send``). */
  folders?: string[];
  /** Email: fallback Drafts folder (SPECIAL-USE ``\\Drafts`` resolved first). */
  drafts_folder?: string;
  /** Email: From address used for compositions. */
  from_address?: string;
  /** Email: addresses/@domains allowed for new (non-reply) compositions. */
  recipient_allowlist?: string[];
  /** Email: skip the human send-approval freeze (needs the
   *  ``email_autonomous_send`` grant; the server rejects it otherwise). */
  unattended_send?: boolean;
}

export type DatasourceIndexState =
  | 'pending'
  | 'indexing'
  | 'ready'
  | 'partial'
  | 'failed';

/** Operational state of a centrally indexed OKF Knowledge Base datasource. */
export interface DatasourceIndexStatus {
  datasource_id: string;
  status: DatasourceIndexState;
  source_head: string | null;
  indexed_commit: string | null;
  pipeline_version: string | null;
  repo_name?: string | null;
  branch?: string | null;
  last_attempt_at: string | null;
  last_success_at: string | null;
  /** Bounded, credential-redacted diagnostic supplied by the orchestrator. */
  last_error: string | null;
  /** Per-run progress counters while a KB indexes (advisory; null when idle). */
  notes_done?: number | null;
  notes_total?: number | null;
}

/** Tolerant summary returned by a manual incremental/full KB reindex. */
export interface DatasourceReindexResult {
  status: string;
  indexed_commit?: string | null;
  full?: boolean;
  upserted?: number;
  deleted?: number;
  reconciled?: number;
  skipped?: number;
  errors?: number;
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
  config?: DatasourceConfig;
  job_id: string | null;
  /** Creator id is used only to gate owner/admin management actions in Cockpit. */
  created_by?: string | null;
  /** Whether the datasource is visible to all users (vs owner/project only). */
  is_global?: boolean;
  /** Declared read-only flag for public datasources (null = not applicable).
   *  Declarative — credentials are the enforcement boundary. */
  read_only?: boolean | null;
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
  config?: DatasourceConfig;
  /** Publish org-wide; requires the public_datasources capability. */
  is_global?: boolean;
  /** Declared read-only flag; defaults to true server-side on publish. */
  read_only?: boolean;
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
  config?: DatasourceConfig;
  /** Publish (true) / unpublish (false); publishing requires the capability. */
  is_global?: boolean;
  /** Declared read-only flag (kb: always true). */
  read_only?: boolean;
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
  approved_at?: string | null;
  approved_by?: string | null;
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
export type ApiKeyProvider = 'openai' | 'anthropic' | 'google' | 'groq' | 'openrouter' | 'mistral' | 'codex' | 'vision';

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
 * Response from an endpoint test-connection probe (server-side
 * `GET {base_url}/models`). Used by Admin → Providers.
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
  /** Effective window: explicit `context_window` when set, else the family default. Read-only, computed server-side. */
  resolved_context_window: number;
  /** Whether `resolved_context_window` came from the explicit cap or the inherited family default. */
  context_window_source: 'explicit' | 'family_default';
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
  default_tts_model?: string;
  default_embedding_model?: string;
  embedding_provider?: string;
  persistent_agent?: {
    model?: string;
    permission_mode?: string;
    idle_timeout_minutes?: number;
    config_name?: string;
    workspace_backend?: string;
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
  default_tts_model?: string | null;
  /** User's chosen read-aloud voice (overrides the admin/per-language default). */
  default_tts_voice?: string | null;
  default_session_model?: string | null;
  default_embedding_model?: string | null;
  embedding_provider?: string | null;
  language?: 'en' | 'de-DE' | null;
  /** Admin "View as" scope: 'all' = fleet-wide (default), 'me' = own data only. */
  admin_view_mode?: 'me' | 'all' | null;
  communication?: CommunicationSettings | null;
  persistent_agent?: PersistentAgentSettings | null;
  /** How the aux LLM rewrites messages for read-aloud (reasoning + custom prompt). */
  read_aloud?: ReadAloudSettings | null;
  _resolved?: ResolvedDefaults;
}

/** A read-aloud reasoning level. 'off' (default) keeps the fast rewrite path;
 * the rest turn the aux model's thinking on (or set its effort). */
export type ReadAloudReasoningLevel = 'off' | 'low' | 'medium' | 'high';

/**
 * Read-aloud rewrite preferences (stored under users.settings.read_aloud, read by
 * orchestrator/services/tts.py). `reasoning_level` trades latency for a smarter
 * rewrite (off by default); `custom_prompt` is the user's own standing
 * instructions ("skip tables", "give me a TLDR", "omit code file names") — it
 * outranks the default rules, so it can summarize/omit, capped at 1000 chars.
 */
export interface ReadAloudSettings {
  reasoning_level?: ReadAloudReasoningLevel | null;
  custom_prompt?: string | null;
}

/**
 * User settings for persistent agent sessions.
 */
export interface PersistentAgentSettings {
    model?: string | null;
    permission_mode?: string | null;
    /** Default session workspace tier; null tracks the system default (virtual). */
    workspace_backend?: 'virtual' | 'sandbox' | 'none' | null;
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
  /**
   * Whether the codex-proxy deployment is reachable at all. False when the
   * proxy is disabled (e.g. `codexProxy.enabled: false`) or down — the UI
   * shows an "enable it" disclaimer instead of a Connect button that 502s.
   */
  reachable: boolean;
  accounts: { name: string; status: string; status_message?: string }[];
  model_count: number;
}

/** One Codex rate-limit window (5-hour or weekly), from ChatGPT `wham/usage`. */
export interface CodexUsageWindow {
  used_percent: number | null;
  window_seconds: number | null;
  reset_after_seconds: number | null;
  reset_at: number | null;
}

/**
 * Codex subscription usage / rate-limit windows (admin-only), fetched from the
 * ChatGPT backend via the codex proxy. Powers the capacity bars in
 * Settings → Codex. `available: false` when the proxy is down, no account is
 * connected, or the backend is unreachable (the OAuth token stays server-side —
 * only these aggregates cross to the UI).
 */
export interface CodexUsage {
  available: boolean;
  account?: string | null;
  plan_type?: string | null;
  limit_reached?: boolean;
  /** 5-hour rolling "session" window. */
  primary?: CodexUsageWindow | null;
  /** 7-day "weekly" window. */
  secondary?: CodexUsageWindow | null;
  per_model?: {
    name: string;
    primary: CodexUsageWindow | null;
    secondary: CodexUsageWindow | null;
  }[];
  credits?: { has_credits: boolean; unlimited: boolean; balance?: string | null } | null;
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
  main_cloud_backend?: string | null;
  network_tier?: ProjectNetworkTier;
  created_at: string;
  updated_at: string;
  job_count?: number;
  repo_count?: number;
  member_count?: number;
}

/** Status of a project self-improvement loop. */
export type ProjectLoopStatus =
  | 'running'
  | 'paused'
  | 'stopped'
  | 'completed'
  | 'failed';

/**
 * A multi-job campaign a campaign-scheduled loop's checkpoint critic filed
 * via `loop_plan`. Lives in `project_loops.campaign` (JSONB); shape is what
 * `_advance_planner_campaign` writes (orchestrator/main.py).
 * See docs/features/loop_campaign_scheduling.md.
 */
export interface LoopCampaign {
  /** Deterministic: the plan-filing critic job's id. */
  id: string;
  plan_job_id: string;
  /** KB note carrying the initiative the campaign invests in. */
  initiative_note_id: string;
  title: string;
  /** Ordered execution stages; each spawns one job of that role. */
  stages: {role: string}[];
  /** Pre-registered acceptance checks the closing critic judges against. */
  acceptance: string[];
  /** Next stage index to spawn (the running stage is cursor - 1). */
  cursor: number;
  /** Stages that finished (success or failure). */
  stages_done: number;
  /** Consecutive failed members; hitting the cap aborts the campaign. */
  member_failures: number;
  extensions_used: number;
  /** active → running stages; review/aborted → awaiting the critic's verdict. */
  status: 'active' | 'review' | 'aborted';
}

/** A disposed campaign, archived on `project_loops.campaign_history` (newest last). */
export interface LoopCampaignHistoryEntry {
  id: string | null;
  initiative_note_id: string | null;
  title: string | null;
  stages_total: number;
  stages_done: number | null;
  extensions_used: number | null;
  status_at_close: string | null;
  /** The critic's verdict: ship (done), extend (new campaign, same initiative), kill. */
  outcome: 'ship' | 'extend' | 'kill';
  notes: string | null;
  disposed_by: string;
}

/**
 * A project self-improvement loop — the control row that runs jobs one at a
 * time, rotating role_sequence (e.g. scholar→critic→developer) until a
 * budget (max_iterations / run_until / consecutive-failure cap) stops it.
 * Mirrors the project_loops table. See docs/features/project_self_improvement_loop.md.
 */
export interface ProjectLoop {
  id: string;
  project_id: string;
  owner_id: string | null;
  status: ProjectLoopStatus;
  goal: string | null;
  acceptance_criteria: string | null;
  user_prompt: string | null;
  model: string | null;
  /** Per-loop workspace tier for every spawned job (null = default sandbox). */
  workspace_backend: string | null;
  /**
   * Rotation of stages. A plain string is a one-job stage; a nested array is a
   * parallel *fan-out* stage whose analysis roles run concurrently and barrier
   * before the loop rotates (e.g. `[["scholar","product-qa"], "critic"]`).
   */
  role_sequence: (string | string[])[];
  seq_index: number;
  max_iterations: number | null;
  remaining_iterations: number | null;
  run_until: string | null;
  max_consecutive_failures: number;
  current_job_id: string | null;
  /**
   * Job ids of the in-flight fan-out stage. Populated only while a parallel
   * stage is running (single-role stages use `current_job_id` and leave this
   * empty). Drives the stage-job chips in the live panel.
   */
  current_stage_jobs?: string[];
  total_jobs_run: number;
  consecutive_failures: number;
  last_error: string | null;
  stop_reason: string | null;
  /**
   * Execution-slot scheduling: 'standard' (default — one job per turn) or
   * 'campaign' (the checkpoint critic may expand the execution slot into a
   * multi-stage campaign). Start-time only. loop_campaign_scheduling.md.
   */
  scheduling?: 'standard' | 'campaign';
  /** The live campaign on a campaign-scheduled loop (null between campaigns). */
  campaign?: LoopCampaign | null;
  /** Disposed campaigns, newest last (bounded server-side). */
  campaign_history?: LoopCampaignHistoryEntry[];
  /** Per-loop guardrail overrides ({max_stages, max_extensions, abort_failures}). */
  campaign_caps?: Record<string, number> | null;
  created_at: string;
  updated_at: string;
}

/** Request body for starting a project loop (POST /projects/{id}/loop). */
export interface ProjectLoopStartRequest {
  model?: string | null;
  /** Workspace tier for every spawned job: 'sandbox' | 'vm' | 'virtual' | 'none'. */
  workspace_backend?: string | null;
  /** Stage rotation; a nested array entry is a concurrent analysis fan-out. */
  role_sequence?: (string | string[])[] | null;
  max_iterations?: number | null;
  run_until?: string | null;
  acceptance_criteria?: string | null;
  user_prompt?: string | null;
  goal_override?: string | null;
  max_consecutive_failures?: number;
  /**
   * 'campaign' lets the checkpoint critic file multi-job campaigns; requires
   * exactly one single-role critic step followed by a single-role step
   * (validated server-side; mirrored in plannerIneligibility client-side).
   */
  scheduling?: 'standard' | 'campaign';
}

// =============================================================================
// Project Backlog (loop ticket pool) —
// docs/superpowers/specs/2026-07-26-project-backlog-pipeline-design.md
// =============================================================================

/**
 * Priority label for a backlog ticket. Always a word on the wire — the 0/1/2
 * storage rank (`orchestrator/services/project_backlog.py::PRIORITY_WORDS`) is
 * a server-side implementation detail the cockpit never sees. A label only:
 * it sorts what's shown, nothing gates or reorders work because of it.
 */
export type BacklogPriority = 'high' | 'normal' | 'low';

/** One ticket in the project's backlog pool (GET /projects/{id}/backlog). */
export interface BacklogItem {
  note_id: string;
  note_type: 'feature' | 'issue' | 'idea';
  title: string;
  priority: BacklogPriority;
}

/**
 * The project's ticket pool, as shown to the user. `items` is capped
 * server-side (200, priority-then-age order) but `counts`/`total` are NOT —
 * for a large pool they can exceed `items.length`, which is why the cockpit
 * must render the counts and not just the list (a capped list otherwise
 * hides its own tail). `in_progress` is the active loop's current campaign
 * initiative — excluded from `items` so it's never shown twice; null when no
 * campaign is running.
 */
export interface ProjectBacklog {
  total: number;
  counts: Record<BacklogPriority, number>;
  in_progress: { note_id: string; title: string } | null;
  items: BacklogItem[];
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

/**
 * 'awaiting_user' (headless natural pause) and 'suspended' (workspace
 * snapshotted, pods torn down — attention-sleep or drift-drain) are both
 * live-resumable: sending a message wakes the session on a fresh agent.
 * Only 'ended' renders the resume card.
 */
export type ThreadStatus = 'created' | 'active' | 'awaiting_user' | 'suspended' | 'ended';

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
  /**
   * Auxiliary-model health from the latest heartbeat (aux Phase 2). True while
   * the agent's auxiliary LLM (memory extraction/curation, session titles) is
   * sustained-failing. Compact failure detail lives in `metadata.aux`.
   */
  aux_degraded?: boolean;
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
  /**
   * JSONB — **may arrive as a raw JSON STRING, not an object.** asyncpg hands
   * JSONB back as text and the orchestrator passes it through, so indexing
   * straight into this type-checks and then silently yields `undefined` at
   * runtime. Run it through `asRecord()` (core/util/job-status) before reading
   * a key. Verified against dev 2026-07-29.
   */
  context?: Record<string, any> | string | null;
  /**
   * Freeze blob for a job awaiting review — `{summary, confidence,
   * deliverables, freeze_type, …}`. Cleared on approval. Same
   * JSONB-may-be-a-string caveat as `context`.
   */
  freeze_data?: Record<string, any> | string | null;
  /** Mode A diff-review state (job_cloud_export.md). NULL = no diff captured. */
  diff_status?: 'pending' | 'accepted' | 'rejected' | null;
  /** Mode A baseline commit hash for project-folder diff. Internal; only set if attached to project. */
  cloud_diff_baseline_commit?: string | null;
  /** Mode B export marker (set when "Export to shared folder" succeeds). */
  exported_folder_handle?: string | null;
  exported_at?: string | null;
  /**
   * Backend-computed cloud-review routing (job_cloud_export.md). `'diff'` when
   * the job's project has a main-cloud folder (Mode A diff-review);
   * `'open_folder'` otherwise — loose jobs and default-project / no-cloud-folder
   * jobs get the Mode B "Open cloud folder" button.
   */
  cloud_review_mode?: 'diff' | 'open_folder' | null;
  /**
   * Session thread that created this job (session_wake_on_job_completion.md).
   * NULL for cockpit-, automation- and worker-child-created jobs. Set
   * server-side from the authenticated internal create path — never submitted.
   * The job card uses it to render "launched from this session".
   */
  created_by_thread_id?: string | null;
  /** Whether this job's terminal state owes its creating session a wake. */
  wake_on_complete?: boolean;
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
  /** DB-backed expert UUID. Preferred over config_name for expert selection;
   *  the orchestrator resolves it into the job config. config_name stays base. */
  expert_id?: string;
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
 * Mode A diff summary for a project-attached job in `pending_review`.
 * See docs/done/job_cloud_export.md §3.4–§3.5.
 */
export interface JobDiffFileEntry {
  path: string;
  status: 'added' | 'modified' | 'deleted';
}

export interface JobDiffSummary {
  job_id: string;
  diff_status: 'pending' | 'accepted' | 'rejected' | null;
  baseline_commit: string;
  head_commit: string;
  files: JobDiffFileEntry[];
}

export interface JobDiffFile {
  job_id: string;
  path: string;
  status: 'added' | 'modified' | 'deleted';
  old_content: string | null;
  new_content: string | null;
}

export interface JobAcceptResult {
  job_id: string;
  diff_status: 'accepted';
  status: 'completed';
  applied: number;
  deleted: number;
}

export interface JobRejectResult {
  job_id: string;
  diff_status: 'rejected';
  status: 'completed';
}

/** Returned in the 409 body when the cloud folder diverged since seed. */
export interface JobAcceptConflict {
  code: 'external_modifications_detected';
  message: string;
  diverged: Array<{
    path: string;
    kind: 'etag_mismatch' | 'missing_at_cloud' | 'unexpected_at_cloud';
  }>;
}

/** Returned in the 502 body when partial WebDAV writes failed during apply. */
export interface JobAcceptPartialFailure {
  code: 'partial_write_failure';
  applied: number;
  deleted: number;
  errors: string[];
}

/** Outcome shape the cockpit uses to drive the diff-review UI state. Shared
 *  between the job-mode (`acceptJobDiff`) and thread-mode
 *  (`applyThreadCloudDiff`) apply calls — see api.model.ts's
 *  `ThreadCloudApplyResult` doc comment for why `ok.data` is a union. */
export type JobAcceptOutcome =
  | { kind: 'ok'; data: JobAcceptResult | ThreadCloudApplyResult }
  | { kind: 'conflict'; data: JobAcceptConflict }
  | { kind: 'partial'; data: JobAcceptPartialFailure }
  /** 409 epoch_stale (protected cloud mode only, Task 10): someone else
   *  applied/rejected/restaged since the caller last read the summary. The
   *  component reloads the diff and shows a "changed — reloaded" notice. */
  | { kind: 'stale'; staged_epoch: number }
  | { kind: 'error'; status: number; detail: string };

// =============================================================================
// Protected cloud mode (Slice C, Task 8/10) — thread cloud-diff review
// =============================================================================
//
// Thread-mode counterpart to the Mode A job diff review above: the same
// JobDiffReviewComponent, generalized to also drive against a persistent
// thread's staged protected-cloud diff instead of a job's Gitea-baseline
// diff. See .superpowers/sdd/task-8-brief.md / task-10-brief.md for the
// endpoint contracts and docs/design/cloud_access_unification.md §5/§11.

/**
 * Staged protected-cloud diff summary for a thread (GET
 * .../agents/threads/{id}/cloud-diff). `epoch` must be threaded back into
 * apply/reject as an optimistic-concurrency pin. All-zero/epoch-0/empty
 * `files` (never null from the endpoint) means nothing has been staged yet.
 */
export interface ThreadCloudDiffSummary {
  thread_id: string;
  epoch: number;
  staged_at: string | null;
  counts: { added: number; modified: number; deleted: number };
  protected_mount: string | null;
  files: Array<{ path: string; status: 'added' | 'modified' | 'deleted'; binary: boolean }>;
}

/** One staged file's old/new content for the thread cloud-diff viewer. */
export interface ThreadCloudDiffFile {
  thread_id: string;
  path: string;
  status: 'added' | 'modified' | 'deleted';
  old_content: string | null;
  new_content: string | null;
  old_binary: boolean;
  new_binary: boolean;
}

/**
 * Success body of POST .../cloud-diff/apply. Deliberately NOT shaped like
 * `JobAcceptResult` (no job_id/diff_status/status — a thread isn't a job):
 * `JobAcceptOutcome`'s `ok.data` is a union of the two so the single tagged
 * outcome type can be reused verbatim for both `acceptJobDiff` and
 * `applyThreadCloudDiff`, per the Task 14 brief.
 */
export interface ThreadCloudApplyResult {
  thread_id: string;
  applied: number;
  deleted: number;
  errors: string[];
  epoch: number;
  overlay_reset: boolean;
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

// --- Capability grants (User-Defined Experts, Slice 2) ---

/** One entry of the capability catalog (delivered by both grant endpoints). */
export interface GrantCatalogEntry {
  type: 'bool' | 'enum' | 'list';
  default: unknown;
  restrict_only: boolean;
  order?: string[];
}

export type GrantCatalog = Record<string, GrantCatalogEntry>;

/** Deployment-level feature flags surfaced alongside the caller's grants
 * (e.g. protected cloud mode — Slice C's session-create toggle gate). */
export interface UserCapabilityFeatures {
  protected_cloud?: boolean;
}

/** GET /api/users/me/capabilities */
export interface UserCapabilities {
  is_admin: boolean;
  grants: Record<string, unknown> | null; // null ⇒ admin (unrestricted)
  catalog: GrantCatalog;
  features?: UserCapabilityFeatures;
}

/** GET /api/voice/capabilities — whether a usable TTS/STT model is configured
 * for the caller, so the read-aloud + mic buttons can render disabled-with-reason
 * instead of a dead click that silently answers 204. */
export interface VoiceCapabilities {
  tts: boolean;
  stt: boolean;
}

/** One explicitly-set grant row (GET /api/admin/grants). */
export interface Grant {
  key: string;
  value_json: unknown;
  granted_by: string | null;
  updated_at: string;
}

/** GET /api/admin/grants?scope_kind=&scope_id= */
export interface GrantListResponse {
  grants: Grant[];
  catalog: GrantCatalog;
}

// --- Contacts registry (docs/done/contacts_registry.md) ---
export type ContactChannel = 'email' | 'whatsapp';
export type ContactOptIn = 'pending' | 'opted_in' | 'opted_out';

export interface ContactAddress {
  id: string;
  channel: ContactChannel;
  address: string;
  is_primary: boolean;
  opt_in_status: ContactOptIn;
  last_inbound_at: string | null;
  created_at: string;
}

export interface ContactProjectRef {
  id: string;
  name: string;
}

export interface Contact {
  id: string;
  owner_user_id: string;
  display_name: string;
  notes: string | null;
  addresses: ContactAddress[];
  projects: ContactProjectRef[];
  created_at: string;
  updated_at: string;
}
