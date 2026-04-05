/**
 * Pure function to determine available reasoning levels for a given model.
 *
 * Mirrors backend logic in src/core/loader.py (detect_model_family + detect_reasoning_method).
 * Extracted here so it can be shared between job and session creation components and tested independently.
 */

export interface ReasoningOption {
  value: string | null;
  label: string;
}

/**
 * Returns the available reasoning level options for a given model identifier.
 *
 * The logic matches the backend's provider/family detection:
 * - OpenRouter: supports all 6 levels natively
 * - Groq: no reasoning control
 * - gpt-oss (vLLM): supports all 6 levels via prompt injection
 * - Claude, Gemini: no reasoning control
 * - OpenAI, DeepSeek, Qwen, Llama: standard 4 levels
 */
export function getReasoningOptions(model: string | null): ReasoningOption[] {
  const base: ReasoningOption[] = [{ value: null, label: 'Default' }];

  if (!model) {
    return [...base,
      { value: 'none', label: 'None' },
      { value: 'low', label: 'Low' },
      { value: 'medium', label: 'Medium' },
      { value: 'high', label: 'High' },
    ];
  }

  const lower = model.toLowerCase();

  // Provider-level: OpenRouter supports all 6 levels natively
  if (lower.startsWith('openrouter/')) {
    return [...base,
      { value: 'none', label: 'None' },
      { value: 'minimal', label: 'Minimal' },
      { value: 'low', label: 'Low' },
      { value: 'medium', label: 'Medium' },
      { value: 'high', label: 'High' },
      { value: 'xhigh', label: 'X-High' },
    ];
  }

  // Provider-level: Groq doesn't pass reasoning through
  if (lower.startsWith('groq/')) return base;

  // Strip provider prefix for model family detection
  let name = lower;
  for (const prefix of ['openai/']) {
    if (name.startsWith(prefix)) {
      name = name.slice(prefix.length);
      break;
    }
  }

  // Model families that don't support reasoning control
  if (name.startsWith('claude') || name.startsWith('gemini')) return base;

  // gpt-oss (vLLM prompt injection) supports all levels
  if (name.startsWith('gpt-oss')) {
    return [...base,
      { value: 'none', label: 'None' },
      { value: 'minimal', label: 'Minimal' },
      { value: 'low', label: 'Low' },
      { value: 'medium', label: 'Medium' },
      { value: 'high', label: 'High' },
      { value: 'xhigh', label: 'X-High' },
    ];
  }

  // OpenAI, DeepSeek, Qwen, Llama, default -> low/medium/high
  return [...base,
    { value: 'none', label: 'None' },
    { value: 'low', label: 'Low' },
    { value: 'medium', label: 'Medium' },
    { value: 'high', label: 'High' },
  ];
}
