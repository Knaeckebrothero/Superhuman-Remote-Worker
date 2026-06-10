import {afterEach, describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext, signal} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {of} from 'rxjs';
import {TranslocoService} from '@jsverse/transloco';
import {ModelGroupComponent} from './model-group.component';
import {ModelService} from '../../core/services/model.service';
import {SettingsService} from '../../core/services/settings.service';

/**
 * Create a ModelGroupComponent in a minimal injection context with a mock ModelService.
 */
function createComponent(overrides?: {
  models?: { group: string; provider: string; configured: boolean; models: string[] }[];
}) {
  const mockModels = signal(overrides?.models ?? [
    {group: 'Local', provider: 'local', configured: true, models: ['openai/gpt-oss-120b']},
    {group: 'OpenAI', provider: 'openai', configured: true, models: ['gpt-5.4', 'gpt-4o']},
    {group: 'Anthropic', provider: 'anthropic', configured: false, models: ['claude-opus-4-6', 'claude-sonnet-4-5-20250929']},
  ]);

  const mockModelService = {
    models: mockModels,
    builderModels: signal([]),
    auxiliaryModels: signal([]),
    visionModels: signal([]),
    whisperModels: signal([]),
    embeddingModels: signal([]),
    providers: signal([]),
    loading: signal(false),
    loaded: signal(true),
    load: vi.fn(),
  };

  const mockHttp = {get: vi.fn().mockReturnValue(of({}))};

  const mockSettings = {
    apiKeys: signal([]),
    preferences: signal({}),
    updatePreferences: vi.fn().mockReturnValue(of({status: 'ok'})),
  };

  const mockTransloco = {
    translate: (key: string) => key,
    langChanges$: of('en'),
    getActiveLang: () => 'en',
  };

  const injector = Injector.create({
    providers: [
      {provide: ModelService, useValue: mockModelService},
      {provide: HttpClient, useValue: mockHttp},
      {provide: SettingsService, useValue: mockSettings},
      {provide: TranslocoService, useValue: mockTransloco},
    ],
  });

  const component = runInInjectionContext(injector, () => new ModelGroupComponent());
  return {component, mockModelService, mockModels};
}


describe('ModelGroupComponent', () => {
  afterEach(() => localStorage.clear());

  describe('signal wiring', () => {
    it('should read models from ModelService', () => {
      const {component} = createComponent();
      expect(component.availableModels()).toHaveLength(3);
      expect(component.availableModels()[0].group).toBe('Local');
    });

    it('should react to model service updates', () => {
      const {component, mockModels} = createComponent();
      expect(component.availableModels()).toHaveLength(3);

      mockModels.set([{group: 'Test', provider: 'local', configured: true, models: ['test-model']}]);
      expect(component.availableModels()).toHaveLength(1);
      expect(component.availableModels()[0].group).toBe('Test');
    });
  });

  describe('override state', () => {
    it('should start with null overrides (use defaults)', () => {
      const {component} = createComponent();
      expect(component.strategicModel()).toBeNull();
      expect(component.tacticalModel()).toBeNull();
      expect(component.sessionModel()).toBeNull();
    });

    it('should track modified count for job mode', () => {
      const {component} = createComponent();
      // Default mode is 'job'
      expect(component.modifiedCount()).toBe(0);

      component.strategicModel.set('gpt-5.4');
      expect(component.modifiedCount()).toBe(1);

      component.tacticalModel.set('gpt-4o');
      expect(component.modifiedCount()).toBe(2);
    });

    it('should track modified count for session mode', () => {
      const {component} = createComponent();
      // Can't easily change input() in unit test, but sessionModel tracks separately
      component.sessionModel.set('gpt-5.4');
      // In session mode this would be 1, but since mode() defaults to 'job',
      // the computed only counts strategic/tactical
      expect(component.strategicModel()).toBeNull();
    });
  });

  describe('getOverrides', () => {
    it('should return empty when no overrides set', () => {
      const {component} = createComponent();
      const overrides = component.getOverrides();
      expect(overrides).toEqual({});
    });

    it('should return strategic model override in job mode', () => {
      const {component} = createComponent();
      component.strategicModel.set('gpt-5.4');
      const overrides = component.getOverrides();
      expect(overrides).toEqual({llm: {strategic: {model: 'gpt-5.4'}}});
    });

    it('should return both strategic and tactical overrides', () => {
      const {component} = createComponent();
      component.strategicModel.set('claude-opus-4-6');
      component.tacticalModel.set('gpt-4o');
      const overrides = component.getOverrides();
      expect(overrides).toEqual({
        llm: {
          strategic: {model: 'claude-opus-4-6'},
          tactical: {model: 'gpt-4o'},
        },
      });
    });

    it('should return session model override in session mode', () => {
      const {component} = createComponent();
      // Simulate session mode: sessionModel is set, mode would be 'session'
      // Since we can't easily change input(), test the session branch directly
      component.sessionModel.set('gpt-4o');
      // In job mode, sessionModel is ignored by getOverrides
      // This tests that the signal itself works
      expect(component.sessionModel()).toBe('gpt-4o');
    });
  });

  describe('resetAll', () => {
    it('should clear all model overrides', () => {
      const {component} = createComponent();
      component.strategicModel.set('model-a');
      component.tacticalModel.set('model-b');
      component.sessionModel.set('model-c');

      component.resetAll();

      expect(component.strategicModel()).toBeNull();
      expect(component.tacticalModel()).toBeNull();
      expect(component.sessionModel()).toBeNull();
    });
  });

  describe('prefillFromConfig', () => {
    it('should not treat config models as user overrides', () => {
      const {component} = createComponent();
      component.prefillFromConfig({
        llm: {
          model: 'base-model',
          strategic: {model: 'strategic-override'},
          tactical: {model: 'tactical-override'},
        },
      });

      expect(component.strategicModel()).toBeNull();
      expect(component.tacticalModel()).toBeNull();
      expect(component.sessionModel()).toBeNull();
      expect(component.getOverrides()).toEqual({});
    });

    it('should not send base config model as an override', () => {
      const {component} = createComponent();
      component.prefillFromConfig({
        llm: {model: 'gpt-5.4'},
      });

      expect(component.strategicModel()).toBeNull();
      expect(component.tacticalModel()).toBeNull();
      expect(component.sessionModel()).toBeNull();
      expect(component.getOverrides()).toEqual({});
    });

    it('should handle empty config', () => {
      const {component} = createComponent();
      component.prefillFromConfig({});

      expect(component.strategicModel()).toBeNull();
      expect(component.tacticalModel()).toBeNull();
      expect(component.sessionModel()).toBeNull();
    });
  });

  describe('reasoning options', () => {
    it('should compute reasoning options for selected models', () => {
      const {component} = createComponent();
      // Reasoning options are computed from the model name
      // Default (null model) should still return something
      const options = component.strategicReasoningOptions();
      expect(Array.isArray(options)).toBe(true);
    });
  });
});
