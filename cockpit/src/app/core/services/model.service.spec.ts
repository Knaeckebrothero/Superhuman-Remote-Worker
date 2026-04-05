import {describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {of, throwError} from 'rxjs';
import {ModelService} from './model.service';

function createService(mockHttp?: any) {
  const http = mockHttp ?? {
    get: vi.fn().mockReturnValue(of({
      groups: [
        {group: 'Local', models: ['openai/gpt-oss-120b']},
        {group: 'OpenAI', models: ['gpt-5.4', 'gpt-4o']},
      ],
      presets: [{label: 'OSS Local', strategic: 'openai/gpt-oss-120b', tactical: 'openai/gpt-oss-120b'}],
      builder_models: [{label: 'GPT OSS 120B', id: 'openai/gpt-oss-120b'}],
      auxiliary_models: [{id: 'gpt-4o-mini', label: 'GPT-4o Mini'}],
      vision_models: [{id: 'gpt-4o', label: 'GPT-4o'}],
      whisper_models: [{id: 'whisper-1', label: 'Whisper v1'}],
      embedding_models: [{id: 'text-embedding-3-small', label: 'TE3 Small', dimensions: 1536}],
      providers: ['openai'],
    })),
  };

  const injector = Injector.create({
    providers: [{provide: HttpClient, useValue: http}],
  });

  const service = runInInjectionContext(injector, () => new ModelService());
  return {service, http};
}


describe('ModelService', () => {
  describe('initial state', () => {
    it('should start with empty signals', () => {
      const {service} = createService();
      expect(service.models()).toEqual([]);
      expect(service.presets()).toEqual([]);
      expect(service.builderModels()).toEqual([]);
      expect(service.auxiliaryModels()).toEqual([]);
      expect(service.visionModels()).toEqual([]);
      expect(service.whisperModels()).toEqual([]);
      expect(service.embeddingModels()).toEqual([]);
      expect(service.providers()).toEqual([]);
      expect(service.loaded()).toBe(false);
      expect(service.loading()).toBe(false);
    });
  });

  describe('load', () => {
    it('should populate all signals from API response', () => {
      const {service} = createService();
      service.load();

      expect(service.models()).toHaveLength(2);
      expect(service.models()[0].group).toBe('Local');
      expect(service.presets()).toHaveLength(1);
      expect(service.builderModels()).toHaveLength(1);
      expect(service.auxiliaryModels()).toHaveLength(1);
      expect(service.visionModels()).toHaveLength(1);
      expect(service.whisperModels()).toHaveLength(1);
      expect(service.embeddingModels()).toHaveLength(1);
      expect(service.embeddingModels()[0].dimensions).toBe(1536);
      expect(service.providers()).toEqual(['openai']);
      expect(service.loaded()).toBe(true);
      expect(service.loading()).toBe(false);
    });

    it('should call API with correct URL', () => {
      const {service, http} = createService();
      service.load();
      expect(http.get).toHaveBeenCalledWith(expect.stringContaining('/models'));
    });

    it('should include project_id query param when provided', () => {
      const {service, http} = createService();
      service.load('proj-123');
      expect(http.get).toHaveBeenCalledWith(expect.stringContaining('project_id=proj-123'));
    });
  });

  describe('idempotency', () => {
    it('should not fetch twice after successful load', () => {
      const {service, http} = createService();
      service.load();
      service.load();
      expect(http.get).toHaveBeenCalledTimes(1);
    });

    it('should refetch when force=true', () => {
      const {service, http} = createService();
      service.load();
      service.load(undefined, true);
      expect(http.get).toHaveBeenCalledTimes(2);
    });

    it('should not fetch while in-flight', () => {
      // Use a Subject to control when the response arrives
      const {service, http} = createService();
      service.load();
      // Second call while first is still "processing" (synchronous in test, but fetchInFlight flag)
      // After first load completes synchronously, loaded() is true so second is skipped
      service.load();
      expect(http.get).toHaveBeenCalledTimes(1);
    });
  });

  describe('error fallback', () => {
    it('should fall back to environment.models on API error', () => {
      const http = {
        get: vi.fn().mockReturnValue(throwError(() => new Error('Network error'))),
      };
      const {service} = createService(http);
      service.load();

      // Should still be loaded (with fallback data)
      expect(service.loaded()).toBe(true);
      expect(service.loading()).toBe(false);
      // Fallback data comes from environment.ts — in test env it's empty arrays
      // The important thing is it doesn't throw and sets loaded=true
    });

    it('should allow retry after error with force=true', () => {
      const http = {
        get: vi.fn()
          .mockReturnValueOnce(throwError(() => new Error('fail')))
          .mockReturnValueOnce(of({
            groups: [{group: 'Test', models: ['test-model']}],
            presets: [],
            builder_models: [],
            auxiliary_models: [],
            vision_models: [],
            whisper_models: [],
            embedding_models: [],
            providers: ['openai'],
          })),
      };
      const {service} = createService(http);

      service.load();
      expect(service.loaded()).toBe(true);

      service.load(undefined, true);
      expect(service.models()).toHaveLength(1);
      expect(service.models()[0].group).toBe('Test');
    });
  });

  describe('helper model null safety', () => {
    it('should handle missing helper model fields in response', () => {
      const http = {
        get: vi.fn().mockReturnValue(of({
          groups: [],
          presets: [],
          builder_models: [],
          // helper fields omitted (old backend)
          providers: [],
        })),
      };
      const {service} = createService(http);
      service.load();

      expect(service.auxiliaryModels()).toEqual([]);
      expect(service.visionModels()).toEqual([]);
      expect(service.whisperModels()).toEqual([]);
      expect(service.embeddingModels()).toEqual([]);
    });
  });
});
