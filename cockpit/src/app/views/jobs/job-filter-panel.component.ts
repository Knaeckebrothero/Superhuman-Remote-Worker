import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  ViewChild,
  computed,
  input,
  output,
} from '@angular/core';
import {TranslocoDirective} from '@jsverse/transloco';
import {AppCheckboxComponent} from '../../ui/checkbox';
import {AppButtonComponent} from '../../ui/button';
import {AppMultiSelectComponent, MultiSelectOption} from '../../ui/multi-select';
import {JobListFilters, KNOWN_JOB_ORIGINS} from './job-filters';

/**
 * Overflow filters, in a flyout panel rather than a modal.
 *
 * Deliberate: a modal covers the result set and kills the select→observe
 * loop that filtering is. Carbon, Cloudscape, Helios and MOJ all use a
 * panel or drawer here; nothing surveyed recommends a modal, which is why
 * `ui/dialog` is the wrong primitive for this.
 */
@Component({
  selector: 'app-job-filter-panel',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    TranslocoDirective,
    AppCheckboxComponent,
    AppButtonComponent,
    AppMultiSelectComponent,
  ],
  template: `
    @if (open()) {
      <aside
        #panel
        class="job-panel"
        role="region"
        *transloco="let t"
        [attr.aria-label]="t('jobs.filter.panelLabel')"
        (keydown.escape)="closed.emit()"
      >
        <header class="job-panel__head">
          <h2 class="job-panel__title">{{ t('jobs.filter.panelTitle') }}</h2>
          <app-button size="sm" variant="ghost" (clicked)="closed.emit()">
            {{ t('common.close') }}
          </app-button>
        </header>

        <section class="job-panel__section">
          <h3 class="job-panel__label">{{ t('jobs.filter.originLabel') }}</h3>
          <p class="job-panel__hint">{{ t('jobs.filter.originHint') }}</p>
          @for (origin of origins; track origin) {
            <app-checkbox
              size="sm"
              [checked]="filters().origin.includes(origin)"
              (changed)="toggleOrigin(origin)"
            >
              {{ t('jobs.origin.' + origin) }}
            </app-checkbox>
          }
        </section>

        <section class="job-panel__section">
          <h3 class="job-panel__label">{{ t('jobs.filter.projectLabel') }}</h3>
          <app-multi-select
            [options]="projects()"
            [selected]="filters().projectIds"
            [label]="t('jobs.filter.projectAll')"
            [filterPlaceholder]="t('jobs.filter.projectSearch')"
            [disabled]="noProjectOnly()"
            (selectionChange)="onProjects($event)"
          />
          <app-checkbox
            size="sm"
            [checked]="noProjectOnly()"
            (changed)="toggleNoProject($event)"
          >
            {{ t('jobs.filter.noProject') }}
          </app-checkbox>
          <p class="job-panel__hint">{{ t('jobs.filter.noProjectHint') }}</p>
        </section>

        <section class="job-panel__section">
          <h3 class="job-panel__label">{{ t('jobs.filter.archivedLabel') }}</h3>
          <app-checkbox
            size="sm"
            [checked]="filters().includeArchivedProjects"
            (changed)="patch.emit({includeArchivedProjects: $event})"
          >
            {{ t('jobs.filter.includeArchived') }}
          </app-checkbox>
          <p class="job-panel__hint">{{ t('jobs.filter.includeArchivedHint') }}</p>
        </section>
      </aside>
    }
  `,
  styleUrl: './job-filter-panel.component.scss',
})
export class JobFilterPanelComponent {
  readonly open = input(false);
  readonly filters = input.required<JobListFilters>();
  readonly projects = input<MultiSelectOption[]>([]);

  readonly closed = output<void>();
  readonly patch = output<Partial<JobListFilters>>();

  protected readonly origins = KNOWN_JOB_ORIGINS;

  @ViewChild('panel', {read: ElementRef}) private panel?: ElementRef<HTMLElement>;

  /**
   * The server refuses `project_id=none` alongside specific ids (422), so the
   * two are mutually exclusive here rather than silently returning one arm.
   */
  protected readonly noProjectOnly = computed(() => this.filters().hasProject === false);

  protected toggleOrigin(origin: string): void {
    const current = this.filters().origin;
    const next = current.includes(origin)
      ? current.filter((value) => value !== origin)
      : [...current, origin];
    this.patch.emit({origin: next});
  }

  protected onProjects(projectIds: string[]): void {
    this.patch.emit({projectIds, hasProject: projectIds.length ? null : this.filters().hasProject});
  }

  protected toggleNoProject(checked: boolean): void {
    this.patch.emit({hasProject: checked ? false : null, projectIds: checked ? [] : this.filters().projectIds});
  }
}
