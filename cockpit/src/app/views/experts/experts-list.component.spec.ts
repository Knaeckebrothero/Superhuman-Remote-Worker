import {describe, expect, it} from 'vitest';
import {filterExperts, isBundled} from './experts-list.component';
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
  mk({id: '1', source: 'user', expert_type: 'worker'}),
  mk({id: 'scholar', source: 'bundled', expert_type: 'worker'}),
  mk({id: '2', source: 'global', expert_type: 'session'}),
];

describe('filterExperts', () => {
  it('all returns everything', () => expect(filterExperts(rows, 'all').length).toBe(3));
  it('worker filter', () =>
    expect(filterExperts(rows, 'worker').map((r) => r.id)).toEqual(['1', 'scholar']));
  it('session filter', () =>
    expect(filterExperts(rows, 'session').map((r) => r.id)).toEqual(['2']));
});

describe('isBundled', () => {
  it('bundled source is read-only', () => expect(isBundled(rows[1])).toBe(true));
  it('user source is editable', () => expect(isBundled(rows[0])).toBe(false));
  it('global source is editable', () => expect(isBundled(rows[2])).toBe(false));
  it('missing source defaults to bundled', () =>
    expect(isBundled(mk({source: undefined}))).toBe(true));
});
