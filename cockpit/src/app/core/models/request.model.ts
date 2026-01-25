/**
 * LLM request/response models from MongoDB llm_requests collection.
 */

/**
 * Tool call within an LLM message.
 */
export interface LLMToolCall {
  id: string;
  name: string;
  args: Record<string, unknown>;
}

/**
 * Single message in the LLM conversation.
 */
export interface LLMMessage {
  type: string;           // "SystemMessage", "HumanMessage", "AIMessage", "ToolMessage"
  role: string;           // "system", "human", "assistant", "tool"
  content: string;
  tool_calls?: LLMToolCall[];
  tool_call_id?: string;  // For ToolMessage
  name?: string;          // For ToolMessage (tool name)
  additional_kwargs?: {
    reasoning_content?: string;  // Model reasoning (for supported models)
    [key: string]: unknown;
  };
}

/**
 * Token usage metrics from LLM response.
 */
export interface TokenUsage {
  prompt_tokens?: number;
  completion_tokens?: number;
  reasoning_tokens?: number;
}

/**
 * Request metrics.
 */
export interface RequestMetrics {
  input_chars: number;
  output_chars: number;
  tool_calls: number;
  token_usage?: TokenUsage;
}

/**
 * Full LLM request document from MongoDB.
 */
export interface LLMRequest {
  _id: string;
  job_id: string;
  agent_type: string;
  timestamp: string;
  model: string;
  iteration?: number;
  latency_ms?: number;
  request: {
    messages: LLMMessage[];
    message_count: number;
  };
  response: LLMMessage;
  metrics?: RequestMetrics;
  metadata?: Record<string, unknown>;
}
