/**
 * Shared types for the agent settings component tree.
 */

import type {EffectiveModels} from '../../core/models/api.model';

export type SettingsMode = 'job' | 'session';

/** Tool category metadata for toggle display. */
export interface ToolCategoryMeta {
  key: string;
  label: string;
  icon: string;
  description: string;
}

/** Standard tool categories shown in job creation. */
export const JOB_TOOL_CATEGORIES: ToolCategoryMeta[] = [
  { key: 'research', label: 'Research', icon: 'travel_explore', description: 'Ability to search the web, find academic papers, and browse websites' },
  { key: 'browser_direct', label: 'Browser', icon: 'web', description: 'Direct browser control — navigate, click, type, screenshot, and visually verify pages' },
  { key: 'citation', label: 'Citation', icon: 'format_quote', description: 'Ability to track sources, manage citations, and generate bibliographies' },
  { key: 'shell', label: 'Shell', icon: 'terminal', description: 'Ability to run shell commands in a sandboxed terminal' },
  { key: 'communication', label: 'Communication', icon: 'mail', description: 'Ability to send email messages to you or your team' },
  { key: 'delegation', label: 'Delegation', icon: 'account_tree', description: 'Ability to spawn subagents that work in parallel' },
];

/** Session creation also shows SRW, knowledge, and git categories. */
export const SESSION_TOOL_CATEGORIES: ToolCategoryMeta[] = [
  ...JOB_TOOL_CATEGORIES,
  { key: 'canvas', label: 'Canvas', icon: 'dashboard_customize', description: 'Ability to present workspace files in the shared session Canvas' },
  { key: 'orchestrator', label: 'Fleet Management', icon: 'hub', description: 'Ability to inspect and steer SRW jobs, projects, repositories, and session workspace upgrades' },
  { key: 'agent_catalog', label: 'Experts & Skills', icon: 'extension', description: 'Ability to manage experts and skills' },
  { key: 'workflows', label: 'Automations & Loops', icon: 'auto_mode', description: 'Ability to inspect automations and project loops, and draft disabled automations' },
  { key: 'knowledge', label: 'Knowledge', icon: 'psychology', description: 'Ability to read and write to the project knowledge base' },
  { key: 'git', label: 'Git', icon: 'commit', description: 'Ability to inspect workspace version history' },
];

/** Autonomy level options. */
export const AUTONOMY_LEVELS = [
  { value: 'full', label: 'Full', description: 'Never freezes, runs to completion autonomously' },
  { value: 'review', label: 'Review', description: 'Freezes at job completion for human review' },
  { value: 'partial', label: 'Partial', description: 'Freezes at phase boundaries and job completion' },
  { value: 'guided', label: 'Guided', description: 'Freezes after every tactical phase' },
  { value: 'dependent', label: 'Dependent', description: 'Freezes after every phase (strategic and tactical)' },
] as const;

/** Priority level options. */
export const PRIORITY_LEVELS = [
  { value: 0, label: 'Low (backfill)' },
  { value: 5, label: 'Normal (default)' },
  { value: 10, label: 'High (preempts lower)' },
] as const;

/** Critic feedback round options. */
export const CRITIC_ROUND_OPTIONS = [
  { value: 1, label: '1 round' },
  { value: 3, label: '3 rounds' },
  { value: 5, label: '5 rounds (default)' },
  { value: 10, label: '10 rounds' },
  { value: 0, label: 'Unlimited' },
] as const;

/** Permission mode options for sessions. */
export const PERMISSION_MODES = [
  { value: 'supervised', label: 'Supervised', description: 'Agent asks for approval before executing commands' },
  { value: 'auto_accept', label: 'Auto-accept', description: 'Auto-approve most actions, flag risky ones' },
  { value: 'autonomous', label: 'Autonomous', description: 'Agent runs without asking for approval' },
] as const;

/** Image-quality tiers: resolution of images delivered to the model. Higher =
 *  more visual detail + more image tokens. Mirrors backend
 *  VALID_IMAGE_QUALITY_TIERS (src/core/loader.py) / image_downscale.py. */
export const IMAGE_QUALITY_TIERS = [
  { value: 'economy', label: 'Economy', description: 'Lowest resolution (~768px) — cheapest, coarse detail' },
  { value: 'standard', label: 'Standard', description: 'Balanced (~1568px) — good detail for most tasks' },
  { value: 'high', label: 'High', description: 'Model-family max — best for OCR, charts, UI screenshots' },
] as const;

/** Deep-read a nested path from a config object. */
export function readConfigPath(config: Record<string, unknown>, path: string): unknown {
  return path.split('.').reduce((obj: any, key) => obj?.[key], config) ?? null;
}

/** Deep-set a nested path on a config object, creating intermediate objects. */
export function setConfigPath(obj: Record<string, unknown>, path: string, value: unknown): void {
  const keys = path.split('.');
  let current: any = obj;
  for (let i = 0; i < keys.length - 1; i++) {
    if (current[keys[i]] === undefined || typeof current[keys[i]] !== 'object') {
      current[keys[i]] = {};
    }
    current = current[keys[i]];
  }
  current[keys[keys.length - 1]] = value;
}

/**
 * Detect model family from model name for settings_matrix resolution.
 * TypeScript port of src/core/loader.py:detect_model_family().
 */
export function detectModelFamily(model: string): string {
  let name = model.toLowerCase();

  // Strip provider prefixes
  for (const prefix of ['openrouter/', 'groq/', 'codex/']) {
    if (name.startsWith(prefix)) {
      name = name.slice(prefix.length);
      const slash = name.indexOf('/');
      if (slash !== -1) name = name.slice(slash + 1);
      break;
    }
  }
  if (name.startsWith('openai/')) name = name.slice('openai/'.length);

  if (name.startsWith('claude-opus')) return 'claude-opus';
  if (name.startsWith('claude-sonnet')) return 'claude-sonnet';
  if (name.startsWith('claude-haiku')) return 'claude-haiku';
  if (name.includes('codex-spark')) return 'codex-spark';
  if (name.includes('codex') && name.startsWith('gpt-5')) return 'codex';
  if (name.startsWith('gpt-5.6')) return 'gpt-5.6';
  if (name.startsWith('gpt-5')) return 'gpt-5';
  if (name.startsWith('gpt-4o')) return 'gpt-4o';
  if (name.startsWith('o1') || name.startsWith('o3') || name.startsWith('o4')) return 'o-series';
  if (name.includes('deepseek')) return 'deepseek';
  if (name.includes('glm')) return 'glm';
  if (
    name.startsWith('mistral') || name.startsWith('codestral') || name.startsWith('magistral') ||
    name.startsWith('ministral') || name.startsWith('devstral') || name.startsWith('pixtral') ||
    name.startsWith('voxtral')
  ) return 'mistral';
  if (name.includes('qwen') || name.includes('qwq')) return 'qwen';
  if (name.includes('llama')) return 'llama';
  if (name.startsWith('gemini')) return 'gemini';
  if (name.startsWith('gpt-oss')) return 'gpt-oss';
  if (name.includes('gemma')) return 'gemma';
  if (name.includes('minimax') && name.includes('m3')) return 'minimax-m3';
  if (name.includes('minimax')) return 'minimax';

  return 'default';
}

/** Deep-merge two plain objects (override replaces arrays and scalars). */
function deepMerge(base: Record<string, unknown>, override: Record<string, unknown>): Record<string, unknown> {
  const result = {...base};
  for (const key of Object.keys(override)) {
    const sv = override[key];
    const tv = result[key];
    if (sv && typeof sv === 'object' && !Array.isArray(sv) && tv && typeof tv === 'object' && !Array.isArray(tv)) {
      result[key] = deepMerge(tv as Record<string, unknown>, sv as Record<string, unknown>);
    } else {
      result[key] = sv;
    }
  }
  return result;
}

/**
 * Resolve settings_matrix for a specific model.
 * Returns the merged default + family-specific settings (flat dict with
 * keys like temperature, top_p, model_max_context_tokens, limits).
 */
export function resolveMatrixForModel(
  matrix: Record<string, Record<string, unknown>>,
  model: string,
): Record<string, unknown> {
  if (!matrix || !model) return {};
  const family = detectModelFamily(model);
  const base = {...(matrix['default'] ?? {})};
  if (family !== 'default' && matrix[family]) {
    return deepMerge(base, matrix[family] as Record<string, unknown>);
  }
  return base;
}

/** Result of comparing the two phase models' family settings (window + multimodal). */
export interface ModelMismatch {
  /** Window gap exceeds 2× (mirrors the backend's prominent-warning threshold). */
  prominent: boolean;
  /** Set when the two windows differ; `min` is the budget the shared history is capped to. */
  window: {min: number; strategicWindow: number; tacticalWindow: number} | null;
  /** True when the two models differ in multimodal capability (⇒ image input disabled for both). */
  multimodal: boolean;
}

/**
 * Compare the strategic + tactical phase models' family settings and report a
 * mismatch worth warning about. Pure client-side mirror of the backend's
 * `resolve_phase_model_budget` (src/core/loader.py): a worker job runs both
 * phase models over ONE shared history, so the context budget collapses to the
 * `min` of their windows and multimodal collapses to the AND.
 *
 * Only flags cases with a real consequence — different context windows (history
 * capped to the smaller) or different multimodal capability (image input
 * disabled for both). Stays silent when the two models merely differ in family
 * but agree on window AND multimodal, because there the backend's min/AND is a
 * no-op. Window/multimodal come from the family matrix, so an admin per-model
 * `context_window` override is not reflected (this is an advisory hint).
 */
export function computeModelMismatch(
  matrix: Record<string, Record<string, unknown>>,
  strategicModel: string | null,
  tacticalModel: string | null,
): ModelMismatch | null {
  if (!matrix || Object.keys(matrix).length === 0) return null;
  if (!strategicModel || !tacticalModel || strategicModel === tacticalModel) return null;

  const sm = resolveMatrixForModel(matrix, strategicModel);
  const tm = resolveMatrixForModel(matrix, tacticalModel);

  const sWin = typeof sm['model_max_context_tokens'] === 'number'
    ? (sm['model_max_context_tokens'] as number) : null;
  const tWin = typeof tm['model_max_context_tokens'] === 'number'
    ? (tm['model_max_context_tokens'] as number) : null;
  const sMulti = sm['multimodal'] === true;
  const tMulti = tm['multimodal'] === true;

  const windowDiffers = sWin !== null && tWin !== null && sWin !== tWin;
  const multimodalDiffers = sMulti !== tMulti;
  if (!windowDiffers && !multimodalDiffers) return null;

  const window = windowDiffers
    ? {min: Math.min(sWin as number, tWin as number), strategicWindow: sWin as number, tacticalWindow: tWin as number}
    : null;
  const prominent = window !== null && Math.max(sWin as number, tWin as number) > 2 * window.min;

  return {prominent, window, multimodal: multimodalDiffers};
}

/**
 * Pick the server-resolved effective-models payload the model picker should show
 * for its "Default" option: the selected expert's resolution when present, else
 * the framework defaults' resolution (the no-expert create path). Without the
 * framework fallback the picker drops to the config-literal `llm.model` — the
 * hardcoded YAML placeholder (`RedHatAI/gemma-4-31B-it-FP8-Dynamic`) — instead of
 * the resolved system chat pin. `undefined` (older API, no `effective_models`)
 * falls back the same as `null` (no expert selected).
 */
export function resolveEffectiveModels(
  expertModels: EffectiveModels | null | undefined,
  frameworkModels: EffectiveModels | null,
): EffectiveModels | null {
  return expertModels ?? frameworkModels ?? null;
}

/**
 * Label for a model picker's "inherit the default" option, revealing the model
 * that default currently resolves to. `prefix` is the inherit-marker ("Base
 * default", "Project default"); when a resolved `model` is known it is appended
 * ("Base default · gemma-4-31b") so the option isn't an opaque "default". Falls
 * back to the bare prefix when nothing is resolved yet (picker still loading, or
 * no catalog row), avoiding a dangling separator.
 */
export function defaultModelOptionLabel(prefix: string, model: string | null | undefined): string {
  return model ? `${prefix} · ${model}` : prefix;
}
