import {Component, OnInit, computed, inject, input, signal} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';

import {ApiService} from '../../core/services/api.service';
import type {
  BacklogItem,
  BacklogPriority,
  ProjectBacklog,
} from '../../core/models/api.model';
import {AppButtonComponent} from '../../ui/button';
import {AppSpinnerComponent} from '../../ui/spinner';

/** Fixed render order — never derived from payload order. */
const PRIORITY_ORDER: BacklogPriority[] = ['high', 'normal', 'low'];

/** One priority bucket's tickets, in display order. */
export interface BacklogGroup {
  priority: BacklogPriority;
  items: BacklogItem[];
}

/**
 * Group tickets into fixed high -> normal -> low order, dropping empty
 * groups (so the template never renders an empty "Low" heading with nothing
 * under it). Pure — the project convention is to test extracted functions
 * rather than the component via TestBed, which JIT-compiles templates and
 * throws on any styleUrl reached through a child component (this component's
 * own `app-button`/`app-spinner` children both use one). See
 * project-loop.component.ts / .spec.ts for the sibling precedent.
 */
export function groupByPriority(items: BacklogItem[]): BacklogGroup[] {
  return PRIORITY_ORDER.map((priority) => ({
    priority,
    items: items.filter((i) => i.priority === priority),
  })).filter((g) => g.items.length > 0);
}

/**
 * Project Backlog panel — the loop's real ticket pool (feature/issue/idea
 * notes the scholars file and the critic schedules from), replacing the
 * fictional "open backlog" the kickoff used to claim existed. Read-only view
 * for now: filing/closing tickets happens through the loop itself (kb_write /
 * the disposition close), not this panel. Mounted next to the Loop tab's live
 * panel — see knowledge-base/knowledge/superpowers/specs/2026-07-26-project-backlog-pipeline-design.md.
 *
 * Priority is shown as a label only (high/normal/low); the server never sends
 * the storage rank, and nothing here gates or reorders on it — it only sorts
 * what's displayed. `counts`/`total` come from an uncapped query while
 * `items` is capped server-side, so they can outnumber the rendered list —
 * both are always shown together so a capped list never hides its own tail.
 */
@Component({
  selector: 'app-project-backlog',
  standalone: true,
  imports: [TranslocoPipe, AppButtonComponent, AppSpinnerComponent],
  template: `
    <section class="backlog-panel">
      <header class="backlog-head">
        <h3>{{ 'projectBacklog.title' | transloco }}</h3>
        <app-button
          variant="ghost"
          size="sm"
          [disabled]="loading()"
          (clicked)="refresh()"
        >
          {{ 'projectBacklog.refresh' | transloco }}
        </app-button>
      </header>

      @if (loading()) {
        <div class="backlog-loading"><app-spinner size="md" tone="accent" /></div>
      } @else {
        <!-- Counts-by-priority: always shown, even when the list below is
             capped, so a large pool never silently hides its own tail. -->
        <div class="backlog-stats">
          <div class="backlog-stat">
            <span class="backlog-stat-value">{{ total() }}</span>
            <span class="backlog-stat-label">{{ 'projectBacklog.totalLabel' | transloco }}</span>
          </div>
          @for (p of priorityOrder; track p) {
            <div class="backlog-stat">
              <span class="backlog-stat-value">{{ counts()[p] }}</span>
              <span class="backlog-stat-label">{{ 'projectBacklog.priority.' + p | transloco }}</span>
            </div>
          }
        </div>

        @if (inProgress(); as wip) {
          <!-- The active loop's campaign initiative — deliberately separate
               from the pool below (the server already excludes it there). -->
          <div class="backlog-wip" data-testid="backlog-wip">
            <span class="backlog-wip-badge">{{ 'projectBacklog.inProgress' | transloco }}</span>
            <span class="backlog-wip-title">{{ wip.title || wip.note_id }}</span>
          </div>
        }

        @if (groups().length === 0) {
          <p class="backlog-empty">{{ 'projectBacklog.empty' | transloco }}</p>
        } @else {
          <div class="backlog-groups">
            @for (group of groups(); track group.priority) {
              <div class="backlog-group">
                <h4 class="backlog-group-title" [attr.data-priority]="group.priority">
                  {{ 'projectBacklog.priority.' + group.priority | transloco }}
                  <span class="backlog-group-count">{{ group.items.length }}</span>
                </h4>
                <ul class="backlog-list">
                  @for (item of group.items; track item.note_id) {
                    <li class="backlog-item">
                      <span class="backlog-type" [attr.data-type]="item.note_type">
                        {{ 'projectBacklog.type.' + item.note_type | transloco }}
                      </span>
                      <span class="backlog-item-title">{{ item.title || item.note_id }}</span>
                    </li>
                  }
                </ul>
              </div>
            }
          </div>
        }
      }
    </section>
  `,
  styles: [
    `
      .backlog-panel { display: flex; flex-direction: column; gap: 14px; max-width: 760px; }
      .backlog-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
      .backlog-head h3 { margin: 0; font-size: 15px; color: var(--text-primary); }
      .backlog-loading { display: flex; justify-content: center; padding: 24px; }

      .backlog-stats { display: flex; gap: 12px; flex-wrap: wrap; }
      .backlog-stat {
        flex: 1; min-width: 70px;
        background: var(--panel-bg); border: 1px solid var(--border-color);
        border-radius: var(--radius-surface); padding: 12px; text-align: center;
      }
      .backlog-stat-value { display: block; font-size: 22px; font-weight: 700; color: var(--accent-color); }
      .backlog-stat-label {
        display: block; font-size: 10px; color: var(--text-muted);
        text-transform: capitalize; margin-top: 2px;
      }

      .backlog-wip {
        display: flex; align-items: center; gap: 8px;
        border: 1px solid var(--border-color); border-left: 3px solid var(--info);
        border-radius: var(--radius-control); padding: 8px 12px;
      }
      .backlog-wip-badge {
        font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: var(--radius-tag);
        background: color-mix(in srgb, var(--info) 18%, transparent); color: var(--info);
        text-transform: uppercase; letter-spacing: 0.3px; white-space: nowrap;
      }
      .backlog-wip-title { font-size: 13px; color: var(--text-primary); }

      .backlog-empty { font-size: 13px; color: var(--text-muted); margin: 0; padding: 8px 0; }

      .backlog-groups { display: flex; flex-direction: column; gap: 12px; }
      .backlog-group-title {
        display: flex; align-items: center; gap: 6px;
        margin: 0 0 6px; font-size: 12px; font-weight: 600; text-transform: capitalize;
        color: var(--text-secondary);
      }
      .backlog-group-title[data-priority='high'] { color: var(--danger); }
      .backlog-group-title[data-priority='normal'] { color: var(--info); }
      .backlog-group-title[data-priority='low'] { color: var(--text-muted); }
      .backlog-group-count {
        font-size: 10px; font-weight: 600; padding: 1px 6px; border-radius: var(--radius-tag);
        background: var(--surface-1); color: var(--text-muted);
      }
      .backlog-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
      .backlog-item {
        display: flex; align-items: center; gap: 8px; padding: 6px 8px;
        border-radius: var(--radius-control); background: var(--panel-bg);
        border: 1px solid var(--border-color); font-size: 13px;
      }
      .backlog-type {
        font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.3px;
        padding: 1px 6px; border-radius: var(--radius-tag);
        background: var(--surface-1); color: var(--text-secondary); white-space: nowrap;
      }
      .backlog-item-title {
        color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      }
    `,
  ],
})
export class ProjectBacklogComponent implements OnInit {
  private readonly api = inject(ApiService);

  readonly projectId = input<string>('');

  readonly backlog = signal<ProjectBacklog | null>(null);
  readonly loading = signal(true);

  readonly priorityOrder = PRIORITY_ORDER;

  readonly total = computed(() => this.backlog()?.total ?? 0);
  readonly counts = computed<Record<BacklogPriority, number>>(
    () => this.backlog()?.counts ?? {high: 0, normal: 0, low: 0},
  );
  readonly inProgress = computed(() => this.backlog()?.in_progress ?? null);
  readonly groups = computed(() => groupByPriority(this.backlog()?.items ?? []));

  ngOnInit(): void {
    this.refresh();
  }

  refresh(): void {
    const pid = this.projectId();
    if (!pid) {
      this.loading.set(false);
      return;
    }
    this.loading.set(true);
    this.api.getProjectBacklog(pid).subscribe((b) => {
      this.backlog.set(b);
      this.loading.set(false);
    });
  }
}
