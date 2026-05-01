import {inject, Injectable, signal} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {environment} from '../environment';

export interface ModelGroup {
  group: string;
  provider: string;
  configured: boolean;
  models: string[];
}

export interface BuilderModel {
  label: string;
  id: string;
  configured: boolean;
}

export interface HelperModel {
  id: string;
  label: string;
  configured: boolean;
}

export interface EmbeddingModel extends HelperModel {
  dimensions?: number;
}

interface ModelsResponse {
  groups: ModelGroup[];
  builder_models: BuilderModel[];
  auxiliary_models: HelperModel[];
  vision_models: HelperModel[];
  whisper_models: HelperModel[];
  tts_models: HelperModel[];
  embedding_models: EmbeddingModel[];
  configured_providers: string[];
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
  readonly builderModels = signal<BuilderModel[]>([]);
  readonly auxiliaryModels = signal<HelperModel[]>([]);
  readonly visionModels = signal<HelperModel[]>([]);
  readonly whisperModels = signal<HelperModel[]>([]);
  readonly ttsModels = signal<HelperModel[]>([]);
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
        this.builderModels.set(resp.builder_models);
        this.auxiliaryModels.set(resp.auxiliary_models ?? []);
        this.visionModels.set(resp.vision_models ?? []);
        this.whisperModels.set(resp.whisper_models ?? []);
        this.ttsModels.set(resp.tts_models ?? []);
        this.embeddingModels.set(resp.embedding_models ?? []);
        this.providers.set(resp.configured_providers);
        this.loading.set(false);
        this.loaded.set(true);
        this.fetchInFlight = false;
      },
      error: () => {
        // The DB is the source of truth — on failure, surface an empty
        // catalog so the empty-state banner + disabled pickers render
        // instead of stale, hard-coded fallbacks.
        this.models.set([]);
        this.builderModels.set([]);
        this.auxiliaryModels.set([]);
        this.visionModels.set([]);
        this.whisperModels.set([]);
        this.ttsModels.set([]);
        this.embeddingModels.set([]);
        this.providers.set([]);
        this.loading.set(false);
        this.loaded.set(true);
        this.fetchInFlight = false;
      },
    });
  }
}
