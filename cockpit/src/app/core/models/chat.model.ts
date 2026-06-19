/**
 * Chat history models for the chat_history MongoDB collection.
 * Provides a clean sequential view of conversations without duplicates.
 *
 * Note: Field names match MongoDB snake_case convention.
 */

/**
 * Input message that triggered an LLM response.
 */
export interface ChatInput {
  type: 'human' | 'tool' | 'system';
  content: string;
  content_preview: string;
  tool_call_id?: string;
  tool_name?: string;
}

/**
 * Tool call made by the LLM in its response.
 */
export interface ChatToolCall {
  id: string;
  name: string;
  args_preview: string;
}

/**
 * LLM response content and tool calls.
 */
export interface ChatResponse {
  content: string;
  content_preview: string;
  tool_calls?: ChatToolCall[];
  has_tool_calls: boolean;
}

/**
 * Reasoning content for models that support it (e.g., DeepSeek).
 */
export interface ChatReasoning {
  content: string;
  content_preview: string;
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
  shell_state?: string;
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
