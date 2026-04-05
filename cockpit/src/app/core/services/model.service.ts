import {inject, Injectable, signal} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {environment} from '../environment';

export interface ModelGroup {
  group: string;
  models: string[];
}

export interface ModelPreset {
  label: string;
  strategic: string;
  tactical: string;
}

export interface BuilderModel {
  label: string;
  id: string;
}

export interface HelperModel {
  id: string;
  label: string;
}

export interface EmbeddingModel extends HelperModel {
  dimensions?: number;
}

interface ModelsResponse {
  groups: ModelGroup[];
  presets: ModelPreset[];
  builder_models: BuilderModel[];
  auxiliary_models: HelperModel[];
  vision_models: HelperModel[];
  whisper_models: HelperModel[];
  embedding_models: EmbeddingModel[];
  providers: string[];
}

/**
 * Fetches the available models from the backend API.
 * Models are filtered server-side based on the user's API key access
 * (system env vars, user keys, project keys).
 */
@Injectable({providedIn: 'root'})
export class ModelService {
  private readonly http = inject(HttpClient);
  private fetchInFlight = false;

  readonly models = signal<ModelGroup[]>([]);
  readonly presets = signal<ModelPreset[]>([]);
  readonly builderModels = signal<BuilderModel[]>([]);
  readonly auxiliaryModels = signal<HelperModel[]>([]);
  readonly visionModels = signal<HelperModel[]>([]);
  readonly whisperModels = signal<HelperModel[]>([]);
  readonly embeddingModels = signal<EmbeddingModel[]>([]);
  readonly providers = signal<string[]>([]);
  readonly loading = signal(false);
  readonly loaded = signal(false);

  /**
   * Load available models from the API. Idempotent — skips if already loaded
   * unless `force` is true. Pass `projectId` to include project-level API keys.
   */
  load(projectId?: string, force = false): void {
    if ((this.loaded() || this.fetchInFlight) && !force) return;
    this.fetchInFlight = true;
    this.loading.set(true);

    let url = `${environment.apiUrl}/models`;
    if (projectId) url += `?project_id=${encodeURIComponent(projectId)}`;

    this.http.get<ModelsResponse>(url).subscribe({
      next: (resp) => {
        this.models.set(resp.groups);
        this.presets.set(resp.presets);
        this.builderModels.set(resp.builder_models);
        this.auxiliaryModels.set(resp.auxiliary_models ?? []);
        this.visionModels.set(resp.vision_models ?? []);
        this.whisperModels.set(resp.whisper_models ?? []);
        this.embeddingModels.set(resp.embedding_models ?? []);
        this.providers.set(resp.providers);
        this.loading.set(false);
        this.loaded.set(true);
        this.fetchInFlight = false;
      },
      error: () => {
        // Fallback: use env.js static config if API fails
        this.models.set(environment.models);
        this.presets.set(environment.modelPresets);
        this.builderModels.set(environment.builderModels);
        this.providers.set([]);
        this.loading.set(false);
        this.loaded.set(true);
        this.fetchInFlight = false;
      },
    });
  }
}
