import {afterEach, describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext, signal} from '@angular/core';
import {ActivatedRoute, Router, convertToParamMap} from '@angular/router';
import {of} from 'rxjs';
import {TranslocoService} from '@jsverse/transloco';

import {
  AutomationsPageComponent,
  derivePresetCron,
  inferPresetFromCron,
} from './automations-page.component';
import {ApiService} from '../../core/services/api.service';
import {AutomationsService} from '../../core/services/automations.service';

/**
 * Construct the component in a minimal injection context with mocked
 * services. We deliberately skip TestBed.createComponent so the spec
 * doesn't pay the cost of compiling the page template — the rendering
 * path is exercised by Playwright in the dev-cluster verification.
 */
function createComponent(queryParams: Record<string, string> = {}) {
  const automationsList = signal<unknown[]>([]);
  const isLoading = signal(false);
  const automationsServiceMock = {
    automations: automationsList,
    isLoading,
    loadMine: vi.fn(),
    loadByProject: vi.fn(),
    create: vi.fn().mockReturnValue(of({})),
    update: vi.fn().mockReturnValue(of({})),
    delete: vi.fn().mockReturnValue(of(undefined)),
    pause: vi.fn().mockReturnValue(of({})),
    resume: vi.fn().mockReturnValue(of({})),
    runNow: vi.fn().mockReturnValue(of({})),
    listRuns: vi.fn().mockReturnValue(of([])),
  };

  const apiServiceMock = {
    getExperts: vi.fn().mockReturnValue(
      of([{id: 'scholar', display_name: 'Scholar'}]),
    ),
    getProject: vi.fn().mockReturnValue(of(null)),
  };

  const routerMock = {navigate: vi.fn()};
  const routeMock = {
    snapshot: {queryParamMap: convertToParamMap(queryParams)},
  };

  const translocoMock = {
    translate: (key: string) => key,
    langChanges$: of('en'),
    getActiveLang: () => 'en',
  };

  const injector = Injector.create({
    providers: [
      {provide: AutomationsService, useValue: automationsServiceMock},
      {provide: ApiService, useValue: apiServiceMock},
      {provide: Router, useValue: routerMock},
      {provide: ActivatedRoute, useValue: routeMock},
      {provide: TranslocoService, useValue: translocoMock},
    ],
  });

  const component = runInInjectionContext(
    injector,
    () => new AutomationsPageComponent(),
  );

  return {component, automationsServiceMock, apiServiceMock, routerMock};
}

describe('derivePresetCron', () => {
  it('builds every_day with hour/minute', () => {
    expect(derivePresetCron('every_day', '09:00', '')).toBe('0 9 * * *');
    expect(derivePresetCron('every_day', '07:30', '')).toBe('30 7 * * *');
  });

  it('builds every_weekday with 1-5 DOW', () => {
    expect(derivePresetCron('every_weekday', '09:00', '')).toBe('0 9 * * 1-5');
  });

  it('builds every_monday', () => {
    expect(derivePresetCron('every_monday', '09:00', '')).toBe('0 9 * * 1');
  });

  it('builds every_month_first', () => {
    expect(derivePresetCron('every_month_first', '12:00', '')).toBe(
      '0 12 1 * *',
    );
  });

  it('builds every_hour using the minute slot only', () => {
    expect(derivePresetCron('every_hour', '00:15', '')).toBe('15 * * * *');
  });

  it('passes a custom expression through trimmed', () => {
    expect(derivePresetCron('custom', '09:00', '  */5 * * * *  ')).toBe(
      '*/5 * * * *',
    );
  });

  it('returns empty string when the time is unparseable', () => {
    expect(derivePresetCron('every_day', 'bogus', '')).toBe('');
  });
});

describe('inferPresetFromCron', () => {
  it('round-trips every_day', () => {
    expect(inferPresetFromCron('0 9 * * *')).toEqual({
      preset: 'every_day',
      time: '09:00',
    });
  });

  it('round-trips every_weekday', () => {
    expect(inferPresetFromCron('0 9 * * 1-5')).toEqual({
      preset: 'every_weekday',
      time: '09:00',
    });
  });

  it('round-trips every_monday', () => {
    expect(inferPresetFromCron('0 9 * * 1')).toEqual({
      preset: 'every_monday',
      time: '09:00',
    });
  });

  it('round-trips every_month_first', () => {
    expect(inferPresetFromCron('0 12 1 * *')).toEqual({
      preset: 'every_month_first',
      time: '12:00',
    });
  });

  it('recognizes every_hour and zeroes the time slot', () => {
    expect(inferPresetFromCron('15 * * * *')).toEqual({
      preset: 'every_hour',
      time: '00:00',
    });
  });

  it('falls back to custom for unrecognized shapes', () => {
    // dow=0 (Sunday) doesn't match any of the preset DOW patterns.
    expect(inferPresetFromCron('0 9 * * 0').preset).toBe('custom');
  });

  it('falls back to custom when the field count is wrong', () => {
    expect(inferPresetFromCron('weird').preset).toBe('custom');
  });
});

describe('AutomationsPageComponent', () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  describe('ngOnInit project-filter wiring', () => {
    it('loads my automations when there is no ?project= param', () => {
      const {component, automationsServiceMock} = createComponent();
      component.ngOnInit();
      expect(automationsServiceMock.loadMine).toHaveBeenCalledTimes(1);
      expect(automationsServiceMock.loadByProject).not.toHaveBeenCalled();
      expect(component.projectFilter()).toBeNull();
    });

    it('loads by project + sets the filter signal when ?project=<id> is present', () => {
      const {component, automationsServiceMock, apiServiceMock} =
        createComponent({project: 'p1'});
      component.ngOnInit();
      expect(automationsServiceMock.loadByProject).toHaveBeenCalledWith('p1');
      expect(automationsServiceMock.loadMine).not.toHaveBeenCalled();
      expect(component.projectFilter()).toBe('p1');
      expect(apiServiceMock.getProject).toHaveBeenCalledWith('p1');
    });

    it('populates projectFilterName when getProject resolves a value', () => {
      const {component, apiServiceMock} = createComponent({project: 'p1'});
      apiServiceMock.getProject.mockReturnValueOnce(
        of({id: 'p1', name: 'My Project'}),
      );
      component.ngOnInit();
      expect(component.projectFilterName()).toBe('My Project');
    });
  });

  describe('clearProjectFilter', () => {
    it('drops the filter, calls loadMine, and replaces the URL', () => {
      const {component, automationsServiceMock, routerMock} = createComponent({
        project: 'p1',
      });
      component.ngOnInit();
      automationsServiceMock.loadMine.mockClear();

      component.clearProjectFilter();

      expect(component.projectFilter()).toBeNull();
      expect(component.projectFilterName()).toBeNull();
      expect(automationsServiceMock.loadMine).toHaveBeenCalledTimes(1);
      expect(routerMock.navigate).toHaveBeenCalledWith(['/automations'], {
        queryParams: {},
        replaceUrl: true,
      });
    });
  });

  describe('openNewEditor', () => {
    it('prefills projectId from the active filter', () => {
      const {component} = createComponent({project: 'p1'});
      component.ngOnInit();
      // ngOnInit opens the editor once for the cross-link entry path;
      // close it so the New-automation-button path is exercised in isolation.
      component.closeEditor();
      component.openNewEditor();
      const editor = component.editor();
      expect(editor).not.toBeNull();
      expect(editor!.projectId).toBe('p1');
      expect(editor!.mode).toBe('create');
    });

    it('leaves projectId null when no filter is active', () => {
      const {component} = createComponent();
      component.ngOnInit();
      component.openNewEditor();
      const editor = component.editor();
      expect(editor).not.toBeNull();
      expect(editor!.projectId).toBeNull();
    });
  });

  describe('projectFilterDisplayName', () => {
    it('returns the loaded project name when available', () => {
      const {component, apiServiceMock} = createComponent({project: 'p1'});
      apiServiceMock.getProject.mockReturnValueOnce(
        of({id: 'p1', name: 'My Project'}),
      );
      component.ngOnInit();
      expect(component.projectFilterDisplayName()).toBe('My Project');
    });

    it('falls back to the localized key when getProject returns null', () => {
      const {component} = createComponent({project: 'p1'});
      component.ngOnInit();
      // Mock transloco.translate returns the key — that's what we assert.
      expect(component.projectFilterDisplayName()).toBe(
        'automations.list.projectFilter.fallbackName',
      );
    });
  });
});
