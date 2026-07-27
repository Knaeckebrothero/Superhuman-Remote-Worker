import {describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {of} from 'rxjs';

import {ProjectBacklogComponent, groupByPriority} from './project-backlog.component';
import {ApiService} from '../../core/services/api.service';
import type {BacklogItem, ProjectBacklog} from '../../core/models/api.model';

/**
 * Pure-function + signal-wiring tests (the project convention — see
 * project-loop.component.spec.ts). TestBed.createComponent doesn't work in
 * this repo: it JIT-compiles templates and throws on any styleUrl, including
 * ones reached through child components — and this component's own
 * `app-button` / `app-spinner` children both declare `styleUrl`.
 *
 * `groupByPriority` is tested directly. For the component, `backlog` and
 * `loading` are plain `signal()`s (not `input()`s), so a bare
 * `new ProjectBacklogComponent()` built via `Injector.create` can still be
 * driven with `.set()` and its computed signals — `total`/`counts`/
 * `inProgress`/`groups` — asserted; those are exactly what the template
 * reads. `projectId` IS an `input()`, which Angular only allows the real
 * binding/rendering pipeline to write to, so the one test that exercises
 * `ngOnInit()` relies on its documented default ('') instead of overriding it.
 */

function backlogPayload(overrides: Partial<ProjectBacklog> = {}): ProjectBacklog {
  return {
    total: 3,
    counts: {high: 1, normal: 1, low: 1},
    in_progress: null,
    items: [
      {note_id: 'a', note_type: 'feature', title: 'Add dark mode', priority: 'high'},
      {note_id: 'b', note_type: 'issue', title: 'Login race', priority: 'normal'},
      {note_id: 'c', note_type: 'idea', title: 'Try RAG cache', priority: 'low'},
    ],
    ...overrides,
  };
}

function createComponent(apiOverrides: Record<string, unknown> = {}) {
  const api = {
    getProjectBacklog: vi.fn().mockReturnValue(of(backlogPayload())),
    ...apiOverrides,
  };
  const injector = Injector.create({
    providers: [{provide: ApiService, useValue: api}],
  });
  const component = runInInjectionContext(injector, () => new ProjectBacklogComponent());
  return {component, api};
}

describe('groupByPriority', () => {
  const item = (id: string, priority: BacklogItem['priority']): BacklogItem => ({
    note_id: id,
    note_type: 'feature',
    title: id,
    priority,
  });

  it('orders high, normal, low regardless of input order', () => {
    const grouped = groupByPriority([
      item('c', 'low'),
      item('a', 'high'),
      item('b', 'normal'),
    ]);
    expect(grouped.map((g) => g.priority)).toEqual(['high', 'normal', 'low']);
    expect(grouped[0].items[0].note_id).toBe('a');
  });

  it('omits empty priority groups', () => {
    const grouped = groupByPriority([item('a', 'high')]);
    expect(grouped).toHaveLength(1);
    expect(grouped[0].priority).toBe('high');
  });

  it('is empty for an empty pool', () => {
    expect(groupByPriority([])).toEqual([]);
  });

  it('keeps items of the same priority together and in input order', () => {
    const grouped = groupByPriority([item('a', 'high'), item('b', 'low'), item('c', 'high')]);
    expect(grouped[0].priority).toBe('high');
    expect(grouped[0].items.map((i) => i.note_id)).toEqual(['a', 'c']);
  });
});

describe('ProjectBacklogComponent', () => {
  it('maps a well-formed response into the signals the template renders', () => {
    const {component} = createComponent();
    // Numbers match the documented GET /api/projects/{id}/backlog example.
    component.backlog.set(
      backlogPayload({
        total: 34,
        counts: {high: 12, normal: 15, low: 7},
        in_progress: {note_id: 'issue-deploy-docs', title: 'Deployment docs missing'},
      }),
    );

    expect(component.total()).toBe(34);
    expect(component.counts()).toEqual({high: 12, normal: 15, low: 7});
    expect(component.inProgress()).toEqual({
      note_id: 'issue-deploy-docs',
      title: 'Deployment docs missing',
    });
    expect(component.groups().map((g) => g.priority)).toEqual(['high', 'normal', 'low']);
    expect(component.groups()[0].items[0].note_id).toBe('a');
  });

  it('keeps counts/total independent of the capped items list (large-pool tail)', () => {
    // The server caps `items` at 200 but never caps `counts`/`total` — a
    // large pool's counts can exceed items.length. The panel must show both,
    // not derive one from the other.
    const {component} = createComponent();
    component.backlog.set(backlogPayload({total: 34, counts: {high: 12, normal: 15, low: 7}}));

    expect(component.total()).toBe(34);
    expect(component.backlog()?.items.length).toBe(3);
    expect(component.total()).toBeGreaterThan(component.backlog()!.items.length);
  });

  it('renders sensibly with a null in_progress and an empty items array', () => {
    const {component} = createComponent();
    component.backlog.set(
      backlogPayload({total: 0, counts: {high: 0, normal: 0, low: 0}, in_progress: null, items: []}),
    );

    expect(component.inProgress()).toBeNull();
    expect(component.groups()).toEqual([]);
    expect(component.total()).toBe(0);
  });

  it('renders sensible defaults before any response has loaded', () => {
    const {component} = createComponent();

    expect(component.backlog()).toBeNull();
    expect(component.total()).toBe(0);
    expect(component.counts()).toEqual({high: 0, normal: 0, low: 0});
    expect(component.inProgress()).toBeNull();
    expect(component.groups()).toEqual([]);
  });

  it('never calls the API and clears loading when mounted with no project id', () => {
    const {component, api} = createComponent();

    component.ngOnInit();

    expect(api.getProjectBacklog).not.toHaveBeenCalled();
    expect(component.loading()).toBe(false);
    expect(component.backlog()).toBeNull();
  });
});
