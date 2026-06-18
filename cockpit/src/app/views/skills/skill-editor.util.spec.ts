import {describe, expect, it} from 'vitest';
import {
  filesToRecord,
  hasSkillMd,
  NEW_SKILL_TEMPLATE,
  recordToFiles,
} from './skill-editor.util';

describe('skill-editor.util', () => {
  it('round-trips files array <-> record', () => {
    const arr = [
      {path: 'SKILL.md', content: 'a'},
      {path: 'references/x.md', content: 'b'},
    ];
    expect(recordToFiles(filesToRecord(arr))).toEqual(arr);
  });

  it('recordToFiles always sorts SKILL.md first', () => {
    const rec = {'references/x.md': 'b', 'SKILL.md': 'a'};
    expect(recordToFiles(rec)[0].path).toBe('SKILL.md');
  });

  it('hasSkillMd detects the canonical file', () => {
    expect(hasSkillMd([{path: 'SKILL.md', content: 'x'}])).toBe(true);
    expect(hasSkillMd([{path: 'references/x.md', content: 'x'}])).toBe(false);
  });

  it('the new-skill template is a valid SKILL.md skeleton', () => {
    expect(NEW_SKILL_TEMPLATE).toContain('---');
    expect(NEW_SKILL_TEMPLATE).toContain('name:');
    expect(NEW_SKILL_TEMPLATE).toContain('description:');
  });
});
