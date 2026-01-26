/**
 * Chat history models for the chat_history MongoDB collection.
 * Provides a clean sequential view of conversations without duplicates.
 */

/**
 * Input message that triggered an LLM response.
 */
export interface ChatInput {
  type: 'human' | 'tool' | 'system';
  content: string;
  contentPreview: string;
  toolCallId?: string;
  toolName?: string;
}

/**
 * Tool call made by the LLM in its response.
 */
export interface ChatToolCall {
  id: string;
  name: string;
  argsPreview: string;
}

/**
 * LLM response content and tool calls.
 */
export interface ChatResponse {
  content: string;
  contentPreview: string;
  toolCalls?: ChatToolCall[];
  hasToolCalls: boolean;
}

/**
 * Reasoning content for models that support it (e.g., DeepSeek).
 */
export interface ChatReasoning {
  content: string;
  contentPreview: string;
}

/**
 * Single chat history entry representing one conversation turn.
 */
export interface ChatEntry {
  _id: string;
  jobId: string;
  agentType: string;
  sequenceNumber: number;
  timestamp: string;
  iteration: number;
  phase?: string;
  phaseNumber?: number;
  model: string;
  latencyMs?: number;
  inputs: ChatInput[];
  response: ChatResponse;
  reasoning?: ChatReasoning;
  requestId: string;
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
