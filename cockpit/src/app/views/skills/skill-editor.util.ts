export interface SkillFile {
  path: string;
  content: string;
}

export const NEW_SKILL_TEMPLATE = `---
name: my-skill
description: Use when ... (third person; state when to use it).
---

# My Skill

## Overview
What this is, in one or two sentences.

## Steps
1. ...
`;

/** Array form (editor state) -> record form (API payload). */
export function filesToRecord(files: SkillFile[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const f of files) out[f.path] = f.content;
  return out;
}

/** Record form -> array form, SKILL.md first then the rest alphabetically. */
export function recordToFiles(rec: Record<string, string>): SkillFile[] {
  return Object.keys(rec)
    .sort((a, b) =>
      a === 'SKILL.md' ? -1 : b === 'SKILL.md' ? 1 : a.localeCompare(b),
    )
    .map((path) => ({path, content: rec[path]}));
}

export function hasSkillMd(files: SkillFile[]): boolean {
  return files.some((f) => f.path === 'SKILL.md');
}
