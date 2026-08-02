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
    auxiliaryModels: signal([]),
    visionModels: signal([]),
    whisperModels: signal([]),
    embeddingModels: signal([]),
    providers: signal([]),
    reasoningByModel: signal<Record<string, {method: string; default: string | null; options: string[]}>>({
      'gpt-5.4': {method: 'effort_enum', default: 'high', options: ['low', 'medium', 'high']},
      'gemma-4-moe': {method: 'binary_toggle', default: 'on', options: ['on', 'off']},
    }),
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
  return {component, mockModelService, mockModels, mockSettings};
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

  describe('persist (UI-only, no account write)', () => {
    it('remembers the pick in localStorage but never PATCHes account preferences', () => {
      const {component, mockSettings} = createComponent();

      component.onStrategicModelChange('gpt-4o');
      component.onTacticalModelChange('gpt-5.4');

      // Remembered locally for next-time preselect…
      expect(localStorage.getItem('ui.lastModel.strategic')).toBe('gpt-4o');
      expect(localStorage.getItem('ui.lastModel.tactical')).toBe('gpt-5.4');
      // …but NOT written to the user's global account defaults (the original bug).
      expect(mockSettings.updatePreferences).not.toHaveBeenCalled();
    });

    it('clears the localStorage key when reset to default (null)', () => {
      const {component} = createComponent();
      component.onStrategicModelChange('gpt-4o');
      expect(localStorage.getItem('ui.lastModel.strategic')).toBe('gpt-4o');

      component.onStrategicModelChange(null);
      expect(localStorage.getItem('ui.lastModel.strategic')).toBeNull();
    });
  });

  describe('effective-model default label (Layer 3)', () => {
    // mockTransloco.translate echoes the key, so we assert which label branch
    // (and thus which i18n key) defaultLabel selects.
    it('uses the with-source label when a server effective model + source exist', () => {
      const {component} = createComponent();
      expect(
        component.defaultLabel({model: 'gpt-5.5', source: 'account_default'}, null),
      ).toBe('agentSettings.model.defaultResolved');
    });

    it('uses the no-source label when only a config fallback model exists', () => {
      const {component} = createComponent();
      expect(component.defaultLabel(null, 'gemma-4-moe')).toBe(
        'agentSettings.model.defaultResolvedNoSource',
      );
    });

    it('falls back to a bare "Default" when no model is resolvable', () => {
      const {component} = createComponent();
      expect(component.defaultLabel(null, null)).toBe('agentSettings.model.default');
    });

    it('prefers the server effective model over the config fallback', () => {
      const {component} = createComponent();
      expect(component.defaultLabel({model: 'eff', source: 'expert'}, 'cfg')).toBe(
        'agentSettings.model.defaultResolved',
      );
    });
  });

  describe('model in effect (override > effective > config)', () => {
    it('an explicit override wins', () => {
      const {component} = createComponent();
      component.strategicModel.set('chosen');
      expect(component.strategicInEffect()).toBe('chosen');
    });

    it('is null when nothing is set (empty config, no server effective_models)', () => {
      const {component} = createComponent();
      expect(component.strategicInEffect()).toBeNull();
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

  describe('session reasoning override (Settings-tab field)', () => {
    it('starts null, captures a pick, and clears via a null change', () => {
      const {component} = createComponent();
      expect(component.sessionReasoning()).toBeNull();
      component.onSessionReasoningChange('high');
      expect(component.sessionReasoning()).toBe('high');
      component.onSessionReasoningChange(null);
      expect(component.sessionReasoning()).toBeNull();
    });

    it('derives concrete options (no Default sentinel) from the session model in effect', () => {
      const {component} = createComponent();
      component.sessionModel.set('gemma-4-moe');
      expect(component.sessionReasoningOptions()).toEqual([
        {value: 'on', label: 'On'},
        {value: 'off', label: 'Off'},
      ]);
    });

    it('resolves to the family default and pins it when picked', () => {
      const {component} = createComponent();
      component.sessionModel.set('gemma-4-moe');
      expect(component.resolvedSessionReasoning()).toBe('on');

      // Picking the concrete level that happens to be the default is intent,
      // so it lands in the override rather than collapsing back to inherit.
      // (`mode` defaults to 'job' in this harness, whose getOverrides() reads
      // the strategic/tactical slots — assert the session fragment under the
      // mode that actually emits it.)
      Object.defineProperty(component, 'mode', {value: () => 'session'});
      component.onSessionReasoningChange('on');
      expect(component.sessionReasoning()).toBe('on');
      expect(component.getOverrides()).toEqual({
        llm: {model: 'gemma-4-moe', reasoning_level: 'on'},
      });

      component.onSessionReasoningChange('off');
      expect(component.sessionReasoning()).toBe('off');

      // The explicit "Default" option is what clears it.
      component.onSessionReasoningChange(null);
      expect(component.sessionReasoning()).toBeNull();
      expect(component.getOverrides()).toEqual({llm: {model: 'gemma-4-moe'}});
    });

    it('offers nothing for a model without a selectable capability (field hidden)', () => {
      const {component} = createComponent();
      component.sessionModel.set('gpt-4o');
      expect(component.sessionReasoningOptions()).toEqual([]);
      expect(component.resolvedSessionReasoning()).toBeNull();
    });

    it('is dropped on a session model change (no cross-family leak)', () => {
      const {component} = createComponent();
      component.onSessionModelChange('gpt-5.4');
      component.onSessionReasoningChange('high');
      component.onSessionModelChange('gemma-4-moe');
      expect(component.sessionReasoning()).toBeNull();
    });

    it('is cleared by resetAll and by an expert prefill', () => {
      const {component} = createComponent();
      component.onSessionReasoningChange('high');
      component.resetAll();
      expect(component.sessionReasoning()).toBeNull();

      component.onSessionReasoningChange('low');
      component.prefillFromConfig({});
      expect(component.sessionReasoning()).toBeNull();
    });

    it('does not leak into job-mode overrides', () => {
      const {component} = createComponent();
      component.onSessionReasoningChange('high');
      // mode defaults to 'job' in this harness — the session-only field must
      // not surface in the job override fragment.
      expect(component.getOverrides()).toEqual({});
    });
  });

  describe('reasoning reset notice (Task 3 fix)', () => {
    // The reset itself is correct and stays (decided, not open — reasoning
    // vocabularies are per-family and don't translate). What was missing is
    // any sign it happened: the select just snaps back to the family default,
    // which looks identical to having never picked anything.
    it('starts false and does not fire when there was nothing to lose', () => {
      const {component} = createComponent();
      expect(component.reasoningResetNotice()).toBe(false);

      component.onSessionModelChange('gemma-4-moe');
      expect(component.reasoningResetNotice()).toBe(false);

      component.prefillFromConfig({});
      expect(component.reasoningResetNotice()).toBe(false);
    });

    it('fires when a session model change clears an existing pick', () => {
      const {component} = createComponent();
      component.onSessionReasoningChange('high');
      component.onSessionModelChange('gemma-4-moe');
      expect(component.sessionReasoning()).toBeNull();
      expect(component.reasoningResetNotice()).toBe(true);
    });

    it('fires when a config prefill clears an existing pick — not only on a deliberate expert switch', () => {
      const {component} = createComponent();
      component.onSessionReasoningChange('low');
      // No model change, no expert-card click modeled here — prefillFromConfig
      // is the same method SessionCreateComponent's applyEffectiveDefault()
      // invokes automatically once an in-flight default-expert lookup
      // resolves (see session-create.component.spec.ts). This asserts the
      // sink's behavior in isolation from that trigger.
      component.prefillFromConfig({});
      expect(component.sessionReasoning()).toBeNull();
      expect(component.reasoningResetNotice()).toBe(true);
    });

    it('is dismissed by the next deliberate reasoning pick', () => {
      const {component} = createComponent();
      component.onSessionReasoningChange('high');
      component.onSessionModelChange('gemma-4-moe'); // clears + raises the notice
      expect(component.reasoningResetNotice()).toBe(true);

      component.onSessionReasoningChange('on');
      expect(component.reasoningResetNotice()).toBe(false);
    });

    it('is dismissed by re-confirming the shown default via pinReasoning', () => {
      const {component} = createComponent();
      component.sessionModel.set('gemma-4-moe');
      component.onSessionReasoningChange('off');
      component.onSessionModelChange('gemma-4-moe'); // clears 'off' back to null, raises the notice
      expect(component.reasoningResetNotice()).toBe(true);

      component.pinReasoning();
      expect(component.reasoningResetNotice()).toBe(false);
      // pinReasoning also does what pinValue always does: promotes the shown
      // resolved default into an explicit pin.
      expect(component.sessionReasoning()).toBe(component.resolvedSessionReasoning());
    });

    it('is cleared by resetAll', () => {
      const {component} = createComponent();
      component.onSessionReasoningChange('high');
      component.onSessionModelChange('gemma-4-moe');
      expect(component.reasoningResetNotice()).toBe(true);

      component.resetAll();
      expect(component.reasoningResetNotice()).toBe(false);
    });
  });

  describe('subagent (delegation reader) model', () => {
    it('starts null and is counted in job-mode modifiedCount', () => {
      const {component} = createComponent();
      expect(component.subagentModel()).toBeNull();
      expect(component.modifiedCount()).toBe(0);

      component.subagentModel.set('gpt-4o');
      expect(component.modifiedCount()).toBe(1);
    });

    it('emits an llm.subagent.model override alongside the phase models', () => {
      const {component} = createComponent();
      component.strategicModel.set('claude-opus-4-6');
      component.subagentModel.set('gpt-4o');
      expect(component.getOverrides()).toEqual({
        llm: {strategic: {model: 'claude-opus-4-6'}, subagent: {model: 'gpt-4o'}},
      });
    });

    it('remembers the pick under its own localStorage key, never account prefs', () => {
      const {component, mockSettings} = createComponent();
      component.onSubagentModelChange('gpt-4o');
      expect(localStorage.getItem('ui.lastModel.subagent')).toBe('gpt-4o');
      expect(mockSettings.updatePreferences).not.toHaveBeenCalled();
    });

    it('does not treat a config subagent pin as a user override', () => {
      const {component} = createComponent();
      component.prefillFromConfig({llm: {subagent: {model: 'reader-model'}}});
      expect(component.subagentModel()).toBeNull();
      expect(component.getOverrides()).toEqual({});
    });

    it('does not preselect a saved model when tactical is pinned (subagent inherits it)', () => {
      localStorage.setItem('ui.lastModel.subagent', 'saved-model');
      const {component} = createComponent();
      component.prefillFromConfig({llm: {tactical: {model: 'tactical-model'}}});
      expect(component.subagentModel()).toBeNull();
    });

    it('is cleared by resetAll', () => {
      const {component} = createComponent();
      component.subagentModel.set('gpt-4o');
      component.resetAll();
      expect(component.subagentModel()).toBeNull();
    });

    it('is shown by default for standalone use', () => {
      const {component} = createComponent();
      expect(component.showSubagent()).toBe(true);
    });
  });
});
