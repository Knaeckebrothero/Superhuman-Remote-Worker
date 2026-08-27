import {ChangeDetectionStrategy, Component, computed, input, output} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppDialogComponent} from '../../ui/dialog';
import {AppButtonComponent} from '../../ui/button';
import type {ConfigDriftItem} from '../../core/services/resume-error';

export interface DriftRow {
    kind: string;
    reason: string;
    label: string;
    count: number;
}

/** Collapse items sharing a label into one row with a count — revoked items
 *  all render the same generic string (resume-error.ts), so two of them
 *  would otherwise produce two identical lines. Exported as a pure function
 *  so it is testable without mounting the component: PersistentChatComponent
 *  cannot be mounted in specs (NG0951), and this view tree follows the same
 *  constraint. */
export function groupDriftForDisplay(items: ConfigDriftItem[]): DriftRow[] {
    const rows: DriftRow[] = [];
    const seen = new Map<string, DriftRow>();
    for (const item of items) {
        const existing = seen.get(item.label);
        if (existing) {
            existing.count += 1;
            continue;
        }
        const row: DriftRow = {
            kind: item.kind, reason: item.reason, label: item.label, count: 1,
        };
        seen.set(item.label, row);
        rows.push(row);
    }
    return rows;
}

/** Every item's id, NOT one per collapsed display row — the acknowledgment is
 *  per item, and an incomplete list makes the server 428 again forever.
 *  Extracted the same way `groupDriftForDisplay` is: exported and pure so a
 *  future edit that quietly rewires `ids` to derive from `rows()` instead of
 *  `items()` has a test to fail, not just a mounted component nobody can
 *  mount (NG0951). */
export function acknowledgeableIds(items: ConfigDriftItem[]): string[] {
    return items.map((item) => item.id);
}

/**
 * Shown when POST /resume 428s because a connector, project, or grant this
 * session depended on has since disappeared — resume-error.ts classifies the
 * response and PersistentChatService surfaces it on `pendingDrift`. The user
 * picks between resuming without the missing pieces or leaving this session
 * for a fresh one; there is no way to restore what drifted from here.
 *
 * The acknowledgment POST needs every drifted item's id, not the collapsed
 * display rows' — `ids()` reads straight off `items()`, so a duplicate label
 * collapsed for display still sends both ids and the server does not 428
 * again for the one this dialog silently dropped.
 */
@Component({
    selector: 'app-config-drift-dialog',
    standalone: true,
    changeDetection: ChangeDetectionStrategy.OnPush,
    imports: [AppDialogComponent, AppButtonComponent, TranslocoPipe],
    template: `
        <app-dialog
            [open]="true"
            [closable]="true"
            size="md"
            [title]="'sessions.configDrift.title' | transloco"
            (closed)="dismissed.emit()"
        >
            <p>{{ 'sessions.configDrift.intro' | transloco }}</p>
            <ul class="drift-list">
                @for (row of rows(); track row.label) {
                    <li>
                        {{ 'sessions.configDrift.' + row.kind + '.' + row.reason
                           | transloco: {label: row.label} }}
                        @if (row.count > 1) {
                            <span class="drift-count">{{
                                'sessions.configDrift.countSuffix' | transloco: {count: row.count}
                            }}</span>
                        }
                    </li>
                }
            </ul>
            <ng-container appDialogActions>
                <app-button variant="secondary" (clicked)="startNew.emit()">
                    {{ 'sessions.configDrift.startNew' | transloco }}
                </app-button>
                <app-button variant="primary" (clicked)="resumeAnyway.emit(ids())">
                    {{ 'sessions.configDrift.resumeAnyway' | transloco }}
                </app-button>
            </ng-container>
        </app-dialog>
    `,
    styles: `
        .drift-list {
            display: flex;
            flex-direction: column;
            gap: 4px;
            margin: 0;
            padding: 0;
            list-style: none;
        }

        .drift-list li {
            padding: 6px 0;
            border-bottom: 1px solid var(--border-color);
        }

        .drift-list li:last-child {
            border-bottom: none;
        }

        .drift-count {
            color: var(--text-muted);
        }
    `,
})
export class ConfigDriftDialogComponent {
    readonly items = input.required<ConfigDriftItem[]>();
    readonly resumeAnyway = output<string[]>();
    readonly startNew = output<void>();
    /** Backdrop click / Escape / the header × — dismissing without deciding.
     *  The host must hear this: the inner `app-dialog`'s `open` is a
     *  `model()` bound to a literal `true`, so once any of those three paths
     *  sets it `false` internally, nothing the host does short of destroying
     *  and recreating this component will ever set it back to `true` (a
     *  static-literal binding is never re-pushed by change detection once
     *  bound). Since the host keeps this component mounted for as long as
     *  `pendingDrift` stays non-null, a self-close that the host never learns
     *  about would leave a *later* drift silently updating `items` on this
     *  same, permanently-closed instance. */
    readonly dismissed = output<void>();

    readonly rows = computed(() => groupDriftForDisplay(this.items()));
    readonly ids = computed(() => acknowledgeableIds(this.items()));
}
