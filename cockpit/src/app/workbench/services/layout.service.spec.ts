import {beforeEach, describe, expect, it} from 'vitest';
import {TestBed} from '@angular/core/testing';
import {LayoutService} from './layout.service';
import {LayoutConfig} from '../layout.model';

const KEY = 'workbench-layout';
const LEGACY_KEY = 'cockpit-layout';

/** A valid layout that is deliberately not the default (default sizes are 25/50/25). */
function customLayout(): LayoutConfig {
  return {
    type: 'split',
    direction: 'vertical',
    sizes: [10, 20, 70],
    children: [
      {type: 'component', component: 'request-viewer'},
      {type: 'component', component: 'agent-activity'},
      {type: 'component', component: 'db-table'},
    ],
  };
}

describe('LayoutService storage', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    window.localStorage.clear();
  });

  it('loads a saved layout from the current key', () => {
    window.localStorage.setItem(KEY, JSON.stringify(customLayout()));
    const svc = TestBed.inject(LayoutService);
    expect(svc.layout().sizes).toEqual([10, 20, 70]);
  });

  // The workbench was formerly the "debug" page and keyed its layout on
  // `cockpit-layout`. Renaming the surface must not silently reset every
  // existing user's arrangement.
  it('migrates a layout saved under the pre-rename key', () => {
    window.localStorage.setItem(LEGACY_KEY, JSON.stringify(customLayout()));
    const svc = TestBed.inject(LayoutService);

    expect(svc.layout().sizes).toEqual([10, 20, 70]);
    expect(window.localStorage.getItem(KEY)).not.toBeNull();
    expect(window.localStorage.getItem(LEGACY_KEY)).toBeNull();
  });

  it('prefers the current key over a stale legacy one', () => {
    const stale = {...customLayout(), sizes: [80, 10, 10]};
    window.localStorage.setItem(KEY, JSON.stringify(customLayout()));
    window.localStorage.setItem(LEGACY_KEY, JSON.stringify(stale));

    const svc = TestBed.inject(LayoutService);

    expect(svc.layout().sizes).toEqual([10, 20, 70]);
    // The legacy key is only consumed when it is actually the source.
    expect(window.localStorage.getItem(LEGACY_KEY)).not.toBeNull();
  });

  it('falls back to the default layout when the legacy value is malformed', () => {
    window.localStorage.setItem(LEGACY_KEY, '{not json');
    const svc = TestBed.inject(LayoutService);
    expect(svc.layout().sizes).toEqual([25, 50, 25]);
  });
});
