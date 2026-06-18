import {deepMergeConfig, omitKeys, pickKeys} from '../agent-settings/config-merge';

/**
 * Top-level config keys owned by the structured editor controls
 * (execution-group, the model select, tools-group, advanced-accordion).
 * Everything else (e.g. `instruction_files`, leftover `agent_id`/`display_name`
 * from a duplicated bundled expert) is surfaced in the raw-JSON "Advanced
 * (other keys)" flap. Keep in sync with the controls' `getOverrides()` keys.
 */
export const MANAGED_CONFIG_KEYS: ReadonlySet<string> = new Set([
  'tools',
  'delegation', // tools-group
  'llm', // model select + advanced-accordion
  'autonomy',
  'scholar',
  'verification', // execution-group (worker)
  'interactive', // execution-group (session) + advanced-accordion
  'memory',
  'limits',
  'context_management', // advanced-accordion
  'workspace',
  'shell',
  'research',
  'browser',
  'auxiliary', // advanced-accordion
]);

export interface ExpertConfigSplit {
  /** The expert's own values for managed keys — kept as the save baseline so
   *  nothing pinned is dropped when a control emits no override. */
  managed: Record<string, unknown>;
  /** Pretty JSON of the unmanaged remainder for the raw flap ('' if empty). */
  rawRemainderText: string;
}

/** Split a stored expert config fragment into the managed part and the
 *  unmanaged remainder shown (and editable) in the raw-JSON flap. */
export function splitExpertConfig(
  fragment: Record<string, unknown>,
): ExpertConfigSplit {
  const managed = pickKeys(fragment, MANAGED_CONFIG_KEYS);
  const remainder = omitKeys(fragment, MANAGED_CONFIG_KEYS);
  const rawRemainderText = Object.keys(remainder).length
    ? JSON.stringify(remainder, null, 2)
    : '';
  return {managed, rawRemainderText};
}

/**
 * Assemble the final config to persist. Order is load-bearing:
 *  1. `managedBase` — the expert's own managed values, so anything a control
 *     leaves untouched (and re-emits as `{}`) survives the round-trip.
 *  2. `groupOverrides` — the structured controls' edits, which win on changed
 *     leaves over the stale base.
 *  3. `rawFlap` — the hand-edited unmanaged remainder, applied last so a power
 *     user can still override anything (it should only carry unmanaged keys).
 * Deep-merge recurses objects and replaces arrays/scalars, so an unowned subkey
 * (e.g. `workspace.structure`) survives via `managedBase` automatically.
 */
export function assembleExpertConfig(
  fragment: Record<string, unknown>,
  groupOverrides: Record<string, unknown>,
  rawFlap: Record<string, unknown>,
): Record<string, unknown> {
  const managedBase = pickKeys(fragment, MANAGED_CONFIG_KEYS);
  return deepMergeConfig(deepMergeConfig(managedBase, groupOverrides), rawFlap);
}
