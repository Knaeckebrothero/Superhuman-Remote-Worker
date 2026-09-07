import {describe, expect, it} from 'vitest';
import {
  duplicateResultTranslationArgs,
  extraRoleTags,
  filterExperts,
  isBundled,
} from './experts-list.component';
import type {Expert} from '../../core/models/api.model';

const mk = (over: Partial<Expert>): Expert => ({
  id: 'x',
  display_name: 'X',
  description: '',
  icon: '',
  color: '',
  tags: [],
  ...over,
});

const rows: Expert[] = [
  mk({id: '1', source: 'user', expert_type: 'worker', tags: ['worker']}),
  mk({id: 'scholar', source: 'bundled', expert_type: 'worker', tags: ['worker', 'research']}),
  mk({id: '2', source: 'global', expert_type: 'session', tags: ['session']}),
  // U1: tags are additive role metadata — a session expert tagged for the
  // other roles lists under them too; `subagent` only ever exists as a tag.
  mk({id: '3', source: 'user', expert_type: 'session', tags: ['session', 'worker', 'subagent']}),
  mk({id: 'explorer', source: 'library', expert_type: 'worker', tags: ['subagent']}),
];

describe('filterExperts', () => {
  it('all returns everything', () => expect(filterExperts(rows, 'all').length).toBe(5));
  it('worker filter matches expert_type OR the worker tag', () =>
    expect(filterExperts(rows, 'worker').map((r) => r.id)).toEqual(['1', 'scholar', '3', 'explorer']));
  it('session filter', () =>
    expect(filterExperts(rows, 'session').map((r) => r.id)).toEqual(['2', '3']));
  it('subagent filter matches the tag only (never a type)', () =>
    expect(filterExperts(rows, 'subagent').map((r) => r.id)).toEqual(['3', 'explorer']));
  it('tolerates rows without tags', () =>
    expect(filterExperts([mk({id: 'n', expert_type: 'worker', tags: undefined as never})], 'subagent')).toEqual([]));
});

describe('extraRoleTags', () => {
  it('lists the role tags that are not the expert\'s own type, ignoring free-text tags', () => {
    expect(extraRoleTags(rows[3])).toEqual(['worker', 'subagent']);
    expect(extraRoleTags(rows[1])).toEqual([]);
    expect(extraRoleTags(rows[4])).toEqual(['subagent']);
  });
});

describe('isBundled', () => {
  it('bundled source is read-only', () => expect(isBundled(rows[1])).toBe(true));
  it('subagent-library source is read-only too', () => expect(isBundled(rows[4])).toBe(true));
  it('user source is editable', () => expect(isBundled(rows[0])).toBe(false));
  it('global source is editable', () => expect(isBundled(rows[2])).toBe(false));
  it('missing source defaults to bundled', () =>
    expect(isBundled(mk({source: undefined}))).toBe(true));
});

// Fix round 1 (task 3, 2026-08-04 plan): duplicate's response can carry
// `dropped` — capability grants the copier lacked, stripped rather than
// refusing the fork. This must reach the user (knowledge-history/done/
// global_expert_management.md decision 9: a silent capability downgrade
// burns debugging time), so the message shown differs from the plain
// "duplicated" toast whenever anything was actually removed.
describe('duplicateResultTranslationArgs', () => {
  it('reports the plain success key when nothing was dropped', () => {
    expect(duplicateResultTranslationArgs([])).toEqual(['experts.duplicated']);
  });

  it('reports the plain success key when dropped is absent', () => {
    expect(duplicateResultTranslationArgs(undefined)).toEqual(['experts.duplicated']);
  });

  it('names the missing grants, joined, when something was dropped', () => {
    expect(duplicateResultTranslationArgs(['shell_tools', 'delegation'])).toEqual([
      'experts.duplicatedMissingGrants',
      {grants: 'shell_tools, delegation'},
    ]);
  });

  it('handles a single dropped grant without a trailing separator', () => {
    expect(duplicateResultTranslationArgs(['shell_tools'])).toEqual([
      'experts.duplicatedMissingGrants',
      {grants: 'shell_tools'},
    ]);
  });
});
