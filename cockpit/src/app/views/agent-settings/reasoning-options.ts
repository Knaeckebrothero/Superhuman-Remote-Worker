/**
 * Reasoning level options for the Cockpit, driven by the backend-provided
 * per-model reasoning capability (config/model_config_matrix.yaml's `reasoning`
 * block, surfaced via /api/models → ModelService.reasoningByModel). This file
 * no longer hardcodes family/provider logic — the backend is the single source
 * of truth. See knowledge-base/knowledge/features/family_centered_reasoning.md.
 */
import type {ReasoningCapability} from '../../core/services/model.service';

export interface ReasoningOption {
  value: string | null;
  label: string;
}

const LABELS: Record<string, string> = {
  none: 'None',
  minimal: 'Minimal',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  xhigh: 'X-High',
  max: 'Max',
  on: 'On',
  off: 'Off',
};

function labelFor(value: string): string {
  return LABELS[value] ?? value.charAt(0).toUpperCase() + value.slice(1);
}

/**
 * Build the dropdown options for a model's reasoning capability.
 *
 * - `none` / missing capability → just "Default" (no control).
 * - `always_on` → a single non-selectable "Always on" entry (no off switch).
 * - `effort_enum` / `binary_toggle` → "Default" + the family's exact options
 *   (e.g. Low/Medium/High, or On/Off for gemma).
 *
 * "Default" (value `null`) means "don't override" — the family default applies.
 */
export function getReasoningOptions(
  cap?: ReasoningCapability | null,
): ReasoningOption[] {
  const base: ReasoningOption[] = [{value: null, label: 'Default'}];
  if (!cap || cap.method === 'none') return base;
  if (cap.method === 'always_on') return [{value: null, label: 'Always on'}];
  if (!cap.options?.length) return base;
  return [...base, ...cap.options.map((o) => ({value: o, label: labelFor(o)}))];
}

/**
 * Convenience: resolve options for a model id against the capability map from
 * ModelService.reasoningByModel(). Unknown / not-yet-loaded models fall back to
 * "Default" only (the backend supplies a capability for every catalog model).
 */
export function reasoningOptionsForModel(
  modelId: string | null,
  byModel: Record<string, ReasoningCapability>,
): ReasoningOption[] {
  return getReasoningOptions(modelId ? byModel[modelId] : null);
}

/**
 * Concrete options WITHOUT the "Default" sentinel, for controls that preselect
 * the resolved default value instead of offering a Default entry (the
 * Settings-tab Reasoning select). Empty when the capability offers nothing the
 * user can choose (`none`, `always_on`, missing/empty options) — callers hide
 * the control entirely in that case.
 */
export function getSelectableReasoningOptions(
  cap?: ReasoningCapability | null,
): ReasoningOption[] {
  if (!cap || cap.method === 'none' || cap.method === 'always_on') return [];
  if (!cap.options?.length) return [];
  return cap.options.map((o) => ({value: o, label: labelFor(o)}));
}

/**
 * The value a capability resolves to when the user picks nothing: the family
 * default when it is one of the selectable options, else the first option (an
 * out-of-set default must not leave a native select unmatched — that renders
 * as a blank control). `null` when nothing is selectable.
 */
export function defaultSelectableReasoning(
  cap?: ReasoningCapability | null,
): string | null {
  const options = getSelectableReasoningOptions(cap);
  if (!options.length) return null;
  const fam = cap?.default ?? null;
  return options.some((o) => o.value === fam) ? fam : options[0].value;
}
