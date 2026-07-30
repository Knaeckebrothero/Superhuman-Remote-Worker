/**
 * Chat history models for the chat_history audit-store table.
 * Provides a clean sequential view of conversations without duplicates.
 *
 * Note: Field names match the store's snake_case convention.
 *
 * Lean listings (`/chat?lean=true`) strip full `content`/`args` bodies and
 * mark the element `truncated`; the full entry is hydrated on demand via
 * `/chat/entry/{id}` (ApiService.getChatEntry).
 */

/**
 * Input message that triggered an LLM response.
 *
 * `type === 'context'` entries are compact descriptors of the transient
 * tail-injection block (todos, memory, knowledge, citation feedback,
 * instruction files) that is re-injected fresh on every request — kind +
 * content hash + size + preview, with full content stored only on the turn
 * the hash changes. Legacy rows instead carry the raw injected block as
 * human/tool inputs (detectable via `<active_tasks>` content prefix and
 * `*_inject_` tool_call_ids).
 */
export interface ChatInput {
  type: 'human' | 'tool' | 'system' | 'context';
  /** Full body; absent in lean listings when it exceeds the preview. */
  content?: string;
  content_preview: string;
  /** Lean listing: full content available via entry hydration. */
  truncated?: boolean;
  /** Full content length, when truncated. */
  chars?: number;
  tool_call_id?: string;
  tool_name?: string;
  /** Context entries: injection kind (todos, knowledge, memory, …). */
  kind?: string;
  /** Context entries: 8-hex content hash (change tracking across turns). */
  hash?: string;
  /** Context entries: source label (e.g. instruction file path). */
  label?: string;
}

/**
 * Tool call made by the LLM in its response.
 */
export interface ChatToolCall {
  id: string;
  name: string;
  args_preview: string;
  /** Arguments up to 4 kB (newer rows); absent in lean listings. */
  args?: string;
  /** Lean listing: full args available via entry hydration. */
  args_truncated?: boolean;
}

/**
 * LLM response content and tool calls.
 */
export interface ChatResponse {
  /** Full body; absent in lean listings when it exceeds the preview. */
  content?: string;
  content_preview: string;
  truncated?: boolean;
  chars?: number;
  tool_calls?: ChatToolCall[];
  has_tool_calls: boolean;
}

/**
 * Reasoning content for models that support it (e.g., DeepSeek).
 */
export interface ChatReasoning {
  /** Full body; absent in lean listings when it exceeds the preview. */
  content?: string;
  content_preview: string;
  truncated?: boolean;
  chars?: number;
}

/**
 * Single chat history entry representing one conversation turn.
 */
export interface ChatEntry {
  _id: string;
  /** Transitional: Postgres sends integer `id`, Mongo string `_id`; ApiService
   * normalizes both into `_id` (string). */
  id?: number | string;
  job_id: string;
  agent_type: string;
  timestamp: string;
  iteration: number;
  phase?: string;
  phase_number?: number;
  model: string;
  latency_ms?: number;
  inputs: ChatInput[];
  response: ChatResponse;
  reasoning?: ChatReasoning;
  request_id?: string;
}

/**
 * Paginated response from the chat history API endpoint.
 */
export interface ChatHistoryResponse {
  entries: ChatEntry[];
  total: number;
  page: number;
  pageSize: number;
  hasMore: boolean;
  error?: string;
}
