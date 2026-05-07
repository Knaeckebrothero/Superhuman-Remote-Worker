import {ChangeDetectionStrategy, Component, computed, inject, OnInit} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';
import {
  AdminProvidersService,
  DEFAULT_MODEL_KINDS,
  DefaultModelKind,
} from '../../../core/services/admin-providers.service';
import {ModelService} from '../../../core/services/model.service';
import {AppSelectComponent} from '../../../ui/select';
import {AppFormFieldComponent} from '../../../ui/form-field';

@Component({
  selector: 'app-admin-defaults',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    TranslocoPipe,
    AppSelectComponent,
    AppFormFieldComponent,
  ],
  template: `
    <div class="admin-defaults">
      <section class="admin-section">
        <h2 class="section-title">{{ 'admin.providers.defaults.title' | transloco }}</h2>
        <p class="section-desc">{{ 'admin.providers.defaults.desc' | transloco }}</p>

        @if (catalogEmpty()) {
          <p class="empty-state">{{ 'admin.providers.defaults.emptyCatalog' | transloco }}</p>
        } @else {
          <div class="defaults-form">
            @for (kind of defaultKinds; track kind) {
              <app-form-field
                [label]="('admin.providers.defaults.kind.' + kind) | transloco"
              >
                <app-select
                  [value]="admin.defaults()[kind] ?? ''"
                  (changed)="setDefault(kind, $event ?? '')"
                >
                  <option value="">{{ 'admin.providers.defaults.unset' | transloco }}</option>
                  @if (kind === 'embedding') {
                    @for (m of modelService.embeddingModels(); track m.id) {
                      <option [value]="m.id">{{ m.label }}</option>
                    }
                  } @else if (kind === 'vision') {
                    @for (m of modelService.visionModels(); track m.id) {
                      <option [value]="m.id">{{ m.label }}</option>
                    }
                  } @else if (kind === 'whisper') {
                    @for (m of modelService.whisperModels(); track m.id) {
                      <option [value]="m.id">{{ m.label }}</option>
                    }
                  } @else if (kind === 'tts') {
                    @for (m of modelService.ttsModels(); track m.id) {
                      <option [value]="m.id">{{ m.label }}</option>
                    }
                  } @else if (kind === 'auxiliary') {
                    <!-- Strict: only rows whose capabilities[] includes
                         'auxiliary'. Under the array fan-out a chat row
                         registered as ['chat','auxiliary'] surfaces
                         here too — same row serves both slots. -->
                    @for (m of modelService.auxiliaryModels(); track m.id) {
                      <option [value]="m.id">{{ m.label }}</option>
                    }
                  } @else {
                    <!-- Chat-slot kinds (chat/builder/browser/citation):
                         strict filter by 'chat' capability via the
                         pre-bucketed groups list. -->
                    @for (group of modelService.models(); track group.group) {
                      <optgroup [label]="group.group">
                        @for (model of group.models; track model) {
                          <option [value]="model">{{ model }}</option>
                        }
                      </optgroup>
                    }
                  }
                </app-select>
              </app-form-field>
            }
          </div>
        }
      </section>
    </div>
  `,
  styles: [`
    :host {
      display: block;
    }
    .admin-defaults {
      display: block;
    }
    .admin-section {
      background: var(--panel-bg);
      border: 1px solid var(--border-color);
      border-radius: 12px;
      padding: 24px;
    }
    .section-title {
      font-size: 18px;
      font-weight: 600;
      margin-bottom: 4px;
      color: var(--text-primary);
    }
    .section-desc {
      font-size: 13px;
      color: var(--text-muted);
      margin-bottom: 20px;
    }
    .empty-state {
      font-size: 13px;
      color: var(--text-muted);
      text-align: center;
      padding: 18px 12px;
    }
    .defaults-form {
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
  `],
})
export class AdminDefaultsComponent implements OnInit {
  readonly admin = inject(AdminProvidersService);
  readonly modelService = inject(ModelService);

  readonly defaultKinds: DefaultModelKind[] = DEFAULT_MODEL_KINDS;
  readonly catalogEmpty = computed(() => this.modelService.models().length === 0);

  ngOnInit(): void {
    this.admin.loadDefaults();
    this.modelService.load();
  }

  setDefault(kind: DefaultModelKind, model: string): void {
    this.admin.setDefault(kind, model).subscribe();
  }
}
