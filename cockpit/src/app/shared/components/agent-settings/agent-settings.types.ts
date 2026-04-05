/**
 * Shared types for the agent settings component tree.
 */

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
  { key: 'research', label: 'Research', icon: 'travel_explore', description: 'Web search, paper search, browsing' },
  { key: 'citation', label: 'Citation', icon: 'format_quote', description: 'Citation and literature management' },
  { key: 'document', label: 'Document', icon: 'article', description: 'Document processing and chunking' },
  { key: 'coding', label: 'Coding', icon: 'code', description: 'Shell command execution' },
];

/** Session creation also shows knowledge and git categories. */
export const SESSION_TOOL_CATEGORIES: ToolCategoryMeta[] = [
  ...JOB_TOOL_CATEGORIES,
  { key: 'knowledge', label: 'Knowledge', icon: 'psychology', description: 'Knowledge graph and memory tools' },
  { key: 'git', label: 'Git', icon: 'commit', description: 'Git repository operations' },
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
