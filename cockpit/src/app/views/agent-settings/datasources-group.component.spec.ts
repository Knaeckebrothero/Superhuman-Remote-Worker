import {describe, expect, it} from 'vitest';
import {
  activeDatasourceIds,
  allDatasourcesSelected,
  datasourceSetKey,
  isRepositoryDatasource,
  selectedDatasourceIds,
} from './datasources-group.component';
import {Datasource, DatasourceType} from '../../core/models/api.model';

/**
 * Selection logic for the explicit-only datasource picker: by default all
 * eligible datasources are selected (the picker is the source of truth), the
 * user can opt out, repository sources are excluded under a lite (virtual/none)
 * backend, and a stale selection (after switching project) re-applies defaults.
 */
function makeDs(id: string, type: string): Datasource {
  return {
    id,
    name: id,
    description: null,
    type: type as DatasourceType,
    connection_url: null,
    cli_hint: null,
    default_branch: null,
    job_id: null,
    created_at: '',
    updated_at: '',
  };
}

describe('datasources-group selection logic', () => {
  it('defaults to all eligible selected when untouched', () => {
    const ds = [makeDs('a', 'postgresql'), makeDs('b', 'webdav')];
    expect(selectedDatasourceIds(ds, null, false)).toEqual(['a', 'b']);
  });

  it('respects an explicit opt-out for the current set', () => {
    const ds = [makeDs('a', 'postgresql'), makeDs('b', 'webdav')];
    const selection = {key: datasourceSetKey(ds), ids: new Set(['b'])};
    expect(selectedDatasourceIds(ds, selection, false)).toEqual(['b']);
  });

  it('excludes repository datasources under a lite backend', () => {
    const ds = [makeDs('repo', 'repository'), makeDs('pg', 'postgresql')];
    expect(selectedDatasourceIds(ds, null, true)).toEqual(['pg']);
  });

  it('includes repository datasources when the backend is not lite', () => {
    const ds = [makeDs('repo', 'repository'), makeDs('pg', 'postgresql')];
    expect(new Set(selectedDatasourceIds(ds, null, false))).toEqual(
      new Set(['repo', 'pg']),
    );
  });

  it('re-applies the default when the selection is stale (set changed)', () => {
    const oldDs = [makeDs('a', 'postgresql')];
    // Opted out of everything while project A was selected.
    const stale = {key: datasourceSetKey(oldDs), ids: new Set<string>()};
    const newDs = [makeDs('x', 'webdav'), makeDs('y', 'neo4j')];
    expect(new Set(selectedDatasourceIds(newDs, stale, false))).toEqual(
      new Set(['x', 'y']),
    );
  });

  it('drops ids no longer present in the datasource set', () => {
    const ds = [makeDs('a', 'postgresql')];
    const selection = {key: datasourceSetKey(ds), ids: new Set(['a', 'ghost'])};
    expect(selectedDatasourceIds(ds, selection, false)).toEqual(['a']);
  });

  it('activeDatasourceIds returns all when untouched, the choice when tagged', () => {
    const ds = [makeDs('a', 'postgresql'), makeDs('b', 'webdav')];
    expect(activeDatasourceIds(ds, null)).toEqual(new Set(['a', 'b']));
    const sel = {key: datasourceSetKey(ds), ids: new Set(['a'])};
    expect(activeDatasourceIds(ds, sel)).toEqual(new Set(['a']));
  });

  it('isRepositoryDatasource is case-insensitive', () => {
    expect(isRepositoryDatasource('repository')).toBe(true);
    expect(isRepositoryDatasource('Repository')).toBe(true);
    expect(isRepositoryDatasource('postgresql')).toBe(false);
  });
});

describe('datasources-group select-all state', () => {
  it('is true by default (untouched ⇒ all eligible selected)', () => {
    const ds = [makeDs('a', 'postgresql'), makeDs('b', 'webdav')];
    expect(allDatasourcesSelected(ds, null, false)).toBe(true);
  });

  it('is false when one is opted out', () => {
    const ds = [makeDs('a', 'postgresql'), makeDs('b', 'webdav')];
    const selection = {key: datasourceSetKey(ds), ids: new Set(['a'])};
    expect(allDatasourcesSelected(ds, selection, false)).toBe(false);
  });

  it('is false when nothing is selected', () => {
    const ds = [makeDs('a', 'postgresql'), makeDs('b', 'webdav')];
    const selection = {key: datasourceSetKey(ds), ids: new Set<string>()};
    expect(allDatasourcesSelected(ds, selection, false)).toBe(false);
  });

  it('ignores lite-excluded repository sources (all toggleable selected ⇒ true)', () => {
    const ds = [makeDs('repo', 'repository'), makeDs('pg', 'postgresql')];
    // repo is excluded under a lite backend, so "all selected" only considers pg.
    const selection = {key: datasourceSetKey(ds), ids: new Set(['pg'])};
    expect(allDatasourcesSelected(ds, selection, true)).toBe(true);
  });

  it('is false when there are no selectable datasources', () => {
    const ds = [makeDs('repo', 'repository')];
    expect(allDatasourcesSelected(ds, null, true)).toBe(false);
  });
});
