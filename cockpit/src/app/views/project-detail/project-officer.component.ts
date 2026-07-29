import {
  Component,
  OnDestroy,
  OnInit,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import {DatePipe, DecimalPipe} from '@angular/common';
import {HttpClient} from '@angular/common/http';
import {Router} from '@angular/router';
import {firstValueFrom} from 'rxjs';

import {environment} from '../../core/environment';
import {ModelService} from '../../core/services/model.service';
import {AppButtonComponent} from '../../ui/button';
import {AppInputComponent} from '../../ui/input';
import {AppSelectComponent} from '../../ui/select';
import {AppFormFieldComponent} from '../../ui/form-field';
import {AppSpinnerComponent} from '../../ui/spinner';

/** One typed worker allocation of the officer's kit (S5b slot roster). */
export interface OfficerSlotSpec {
  count: number;
  model?: string;
  backend?: string;
}

/** `GET /api/projects/{id}/officer` — the officer card's data shape. */
export interface OfficerSummary {
  officer: {
    thread_id: string;
    status: string;
    title?: string | null;
    created_at?: string | null;
    hold?: {kind?: string; thread_id?: string; since?: string} | null;
    slots?: Record<string, OfficerSlotSpec> | null;
    sleep_minutes?: {min: number; max: number};
  } | null;
  next_wake_at?: string | null;
  pending_events?: number;
  pages_today?: {used: number; budget: number};
  token_ceiling?: {daily: number; deferred_today: boolean};
  digest?: {at: string; subject: string; message: string}[];
  conference?: {thread_id: string; status: string} | null;
}

/** Editable roster row in the provision form. */
export interface SlotDraft {
  name: string;
  count: number;
  model: string;
  backend: string;
}

/** Assemble the `officer.slots` spec from form rows; null = no roster (flat cap). */
export function buildSlotsSpec(
  rows: SlotDraft[],
): Record<string, OfficerSlotSpec> | null {
  const spec: Record<string, OfficerSlotSpec> = {};
  for (const row of rows) {
    const name = row.name.trim().toLowerCase();
    if (!name) continue;
    const entry: OfficerSlotSpec = {count: Math.max(1, Math.floor(row.count))};
    if (row.model.trim()) entry.model = row.model.trim();
    if (row.backend.trim()) entry.backend = row.backend.trim();
    spec[name] = entry;
  }
  return Object.keys(spec).length ? spec : null;
}

/** "in 42 min" / "overdue 3 min" — the next-wake label. */
export function nextWakeLabel(fireAt: string | null | undefined): string {
  if (!fireAt) return 'not scheduled (event-driven)';
  const delta = (new Date(fireAt).getTime() - Date.now()) / 60000;
  if (Number.isNaN(delta)) return String(fireAt);
  if (delta >= 1) return `in ${Math.round(delta)} min`;
  if (delta > -2) return 'due now';
  return `overdue ${Math.round(-delta)} min`;
}

/**
 * Project Centurion tab — the officer card (centurion.md S9).
 *
 * Shows whether the project has a standing officer, his live state (next
 * wake, queued events, page budget, digest, kit), and owns the lifecycle:
 * provision (with a slot-roster kit), open his log, open/reattach the
 * conference, retire. The log IS his session transcript — the card links
 * there rather than duplicating it.
 */
@Component({
  selector: 'app-project-officer',
  standalone: true,
  imports: [
    DatePipe,
    DecimalPipe,
    AppButtonComponent,
    AppInputComponent,
    AppSelectComponent,
    AppFormFieldComponent,
    AppSpinnerComponent,
  ],
  template: `
    <div class="officer-tab">
      <div class="officer-intro">
        <h3>Centurion</h3>
        <p>
          A standing officer for this project: he holds the charge, watches
          jobs and fleet, dispatches workers from his kit, and pages you only
          when something genuinely needs you. You talk to him in a conference;
          his background log is the session transcript.
        </p>
      </div>

      @if (loading()) {
        <div class="officer-loading"><app-spinner size="md" tone="accent" /></div>
      } @else if (summary()?.officer; as o) {
        <div class="officer-card" data-testid="officer-card">
          <div class="officer-status-row">
            <span class="officer-badge" [attr.data-status]="o.status">{{ o.status }}</span>
            @if (o.hold) {
              <span class="officer-hold" data-testid="officer-hold">held — conference in progress</span>
            }
            <span class="officer-title">{{ o.title || 'Centurion' }}</span>
          </div>

          <div class="officer-meta">
            <div>
              <span class="k">Next wake</span>
              <span class="v" data-testid="next-wake">{{ wakeLabel() }}</span>
            </div>
            <div>
              <span class="k">Queued events</span>
              <span class="v">{{ summary()?.pending_events ?? 0 }}</span>
            </div>
            <div>
              <span class="k">Pages today</span>
              <span class="v">{{ summary()?.pages_today?.used ?? 0 }}/{{ summary()?.pages_today?.budget ?? 3 }}</span>
            </div>
            @if (summary()?.token_ceiling?.daily) {
              <div>
                <span class="k">Token ceiling</span>
                <span class="v">
                  {{ summary()?.token_ceiling?.daily | number }}
                  @if (summary()?.token_ceiling?.deferred_today) {
                    <span class="officer-warn">— reached; sleeping until reset</span>
                  }
                </span>
              </div>
            }
            <div>
              <span class="k">Sleep bounds</span>
              <span class="v">{{ o.sleep_minutes?.min ?? 5 }}–{{ o.sleep_minutes?.max ?? 60 }} min</span>
            </div>
          </div>

          @if (slotRows().length) {
            <div class="officer-slots" data-testid="officer-slots">
              <span class="k">Kit</span>
              @for (s of slotRows(); track s.name) {
                <span class="officer-slot-chip">
                  {{ s.name }} ×{{ s.count }}
                  @if (s.model) { · {{ s.model }} }
                  @if (s.backend) { · {{ s.backend }} }
                </span>
              }
            </div>
          }

          <div class="officer-actions">
            <app-button variant="primary" size="sm" (clicked)="openLog()">Open log</app-button>
            <app-button variant="secondary" size="sm" [disabled]="busy()" (clicked)="openConference()">
              {{ summary()?.conference ? 'Rejoin conference' : 'Conference' }}
            </app-button>
            @if (!retireArmed()) {
              <app-button variant="danger" size="sm" [disabled]="busy()" (clicked)="retireArmed.set(true)">Retire</app-button>
            } @else {
              <app-button variant="danger" size="sm" [disabled]="busy()" (clicked)="retire()">Confirm retire</app-button>
              <app-button variant="secondary" size="sm" (clicked)="retireArmed.set(false)">Keep him</app-button>
            }
          </div>

          @if (digest().length) {
            <div class="officer-digest" data-testid="officer-digest">
              <div class="officer-digest-title">Digest — what he queued for you</div>
              @for (d of digest(); track $index) {
                <div class="officer-digest-item">
                  <span class="dim">{{ d.at | date: 'short' }}</span>
                  <strong>{{ d.subject }}</strong>
                  <span>{{ d.message }}</span>
                </div>
              }
            </div>
          }
        </div>
      } @else {
        <!-- PROVISION FORM -->
        <div class="officer-card" data-testid="provision-form">
          <p class="officer-hint">
            No centurion holds this century yet. Assign the kit — he chooses
            which troops to send; you decide what they are made of.
          </p>

          @for (row of slotDrafts(); track $index; let i = $index) {
            <div class="officer-slot-row">
              <app-form-field label="Slot">
                <app-input
                  [value]="row.name"
                  (changed)="patchSlot(i, {name: $event ?? ''})"
                  placeholder="line"
                />
              </app-form-field>
              <app-form-field label="Count">
                <app-input
                  type="number"
                  [value]="'' + row.count"
                  (changed)="patchSlot(i, {count: toCount($event)})"
                />
              </app-form-field>
              <app-form-field label="Model">
                <app-select [value]="row.model" (changed)="patchSlot(i, {model: $event ?? ''})">
                  <option value="">worker default</option>
                  @for (m of modelOptions(); track m) {
                    <option [value]="m">{{ m }}</option>
                  }
                </app-select>
              </app-form-field>
              <app-form-field label="Workspace">
                <app-select [value]="row.backend" (changed)="patchSlot(i, {backend: $event ?? ''})">
                  <option value="">default</option>
                  <option value="sandbox">sandbox</option>
                  <option value="virtual">virtual</option>
                  <option value="none">none</option>
                  <option value="vm">VM (root)</option>
                </app-select>
              </app-form-field>
              <app-button variant="secondary" size="sm" (clicked)="removeSlot(i)">✕</app-button>
            </div>
          }
          <div class="officer-actions">
            <app-button variant="secondary" size="sm" (clicked)="addSlot()">Add slot</app-button>
            <app-button variant="primary" size="sm" [disabled]="busy()" (clicked)="provision()">
              Provision centurion
            </app-button>
          </div>
          <p class="officer-hint dim">
            No slots = a flat cap of 3 concurrent workers. The officer runs
            headless; opening his session after provisioning boots him.
          </p>
        </div>
      }

      @if (message()) {
        <div class="officer-message" data-testid="officer-message">{{ message() }}</div>
      }
    </div>
  `,
  styles: [
    `
      .officer-tab { display: flex; flex-direction: column; gap: 16px; }
      .officer-intro h3 { margin: 0 0 6px; font-size: 15px; }
      .officer-intro p { margin: 0; color: var(--text-secondary); font-size: 13px; max-width: 70ch; }
      .officer-loading { display: flex; justify-content: center; padding: 32px; }
      .officer-card {
        border: 1px solid var(--border-color);
        border-radius: 10px;
        padding: 16px;
        display: flex;
        flex-direction: column;
        gap: 12px;
        background: var(--bg-secondary);
      }
      .officer-status-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
      .officer-badge {
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.4px;
        padding: 2px 8px;
        border-radius: 999px;
        background: var(--bg-tertiary);
        color: var(--text-secondary);
      }
      .officer-badge[data-status='active'] { background: color-mix(in srgb, var(--success, #22c55e) 18%, transparent); color: var(--success, #22c55e); }
      .officer-badge[data-status='suspended'] { background: color-mix(in srgb, var(--warning, #eab308) 18%, transparent); color: var(--warning, #eab308); }
      .officer-hold { font-size: 12px; color: var(--warning, #eab308); }
      .officer-title { font-weight: 600; font-size: 13px; }
      .officer-meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 8px 16px; }
      .officer-meta .k, .officer-slots .k { display: block; font-size: 11px; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.4px; }
      .officer-meta .v { font-size: 13px; }
      .officer-warn { color: var(--warning, #eab308); font-size: 12px; }
      .officer-slots { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
      .officer-slot-chip {
        font-size: 12px;
        padding: 2px 10px;
        border-radius: 999px;
        border: 1px solid var(--border-color);
        background: var(--bg-tertiary);
      }
      .officer-actions { display: flex; gap: 8px; flex-wrap: wrap; }
      .officer-digest { display: flex; flex-direction: column; gap: 6px; }
      .officer-digest-title { font-size: 12px; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.4px; }
      .officer-digest-item { display: flex; gap: 8px; font-size: 13px; flex-wrap: wrap; }
      .officer-slot-row { display: flex; gap: 8px; align-items: end; flex-wrap: wrap; }
      .officer-hint { margin: 0; font-size: 13px; color: var(--text-secondary); }
      .officer-hint.dim { color: var(--text-tertiary); font-size: 12px; }
      .officer-message { font-size: 13px; color: var(--text-secondary); }
      .dim { color: var(--text-tertiary); }
    `,
  ],
})
export class ProjectOfficerComponent implements OnInit, OnDestroy {
  readonly projectId = input.required<string>();
  readonly projectName = input<string>('');

  private readonly http = inject(HttpClient);
  private readonly router = inject(Router);
  private readonly modelService = inject(ModelService);

  readonly summary = signal<OfficerSummary | null>(null);
  readonly loading = signal(true);
  readonly busy = signal(false);
  readonly message = signal('');
  readonly retireArmed = signal(false);

  // Provision form: one suggested line slot to start from.
  readonly slotDrafts = signal<SlotDraft[]>([
    {name: 'line', count: 2, model: '', backend: 'sandbox'},
  ]);

  private pollHandle: ReturnType<typeof setInterval> | null = null;

  readonly modelOptions = computed(() =>
    this.modelService.models().flatMap((g) => g.models),
  );
  readonly wakeLabel = computed(() =>
    nextWakeLabel(this.summary()?.next_wake_at ?? null),
  );
  readonly digest = computed(() =>
    [...(this.summary()?.digest ?? [])].reverse(),
  );
  readonly slotRows = computed(() => {
    const slots = this.summary()?.officer?.slots ?? null;
    if (!slots) return [];
    return Object.entries(slots).map(([name, s]) => ({
      name,
      count: s.count,
      model: s.model ?? '',
      backend: s.backend ?? '',
    }));
  });

  ngOnInit(): void {
    const pid = this.projectId();
    if (pid) this.modelService.load(pid);
    this.refresh();
    this.pollHandle = setInterval(() => this.refresh(true), 15000);
  }

  ngOnDestroy(): void {
    if (this.pollHandle) clearInterval(this.pollHandle);
  }

  refresh(silent = false): void {
    const pid = this.projectId();
    if (!pid) {
      this.loading.set(false);
      return;
    }
    if (!silent) this.loading.set(true);
    this.http
      .get<OfficerSummary>(`${environment.apiUrl}/projects/${pid}/officer`)
      .subscribe({
        next: (s) => {
          this.summary.set(s);
          this.loading.set(false);
        },
        error: () => {
          this.summary.set(null);
          this.loading.set(false);
        },
      });
  }

  patchSlot(index: number, patch: Partial<SlotDraft>): void {
    this.slotDrafts.update((rows) =>
      rows.map((r, i) => (i === index ? {...r, ...patch} : r)),
    );
  }

  toCount(value: string | null | undefined): number {
    const n = parseInt(value ?? '1', 10);
    return Number.isNaN(n) ? 1 : Math.max(1, n);
  }

  addSlot(): void {
    this.slotDrafts.update((rows) => [
      ...rows,
      {name: '', count: 1, model: '', backend: ''},
    ]);
  }

  removeSlot(index: number): void {
    this.slotDrafts.update((rows) => rows.filter((_, i) => i !== index));
  }

  async provision(): Promise<void> {
    const pid = this.projectId();
    if (!pid || this.busy()) return;
    this.busy.set(true);
    this.message.set('');
    const officer: Record<string, unknown> = {enabled: true};
    const slots = buildSlotsSpec(this.slotDrafts());
    if (slots) officer['slots'] = slots;
    try {
      const resp = await firstValueFrom(
        this.http.post<{thread_id: string}>(
          `${environment.apiUrl}/persistent/threads`,
          {
            title: `Centurion — ${this.projectName() || 'project'}`,
            config_name: 'centurion',
            project_ids: [pid],
            config_override: {officer},
          },
        ),
      );
      // Opening the session boots the headless officer (attach = the loop
      // bootstrap); the Legatus lands on the log and can leave anytime.
      await this.router.navigate(['/sessions', resp.thread_id]);
    } catch (err) {
      this.message.set(this.errText(err, 'Provisioning failed'));
      this.busy.set(false);
    }
  }

  openLog(): void {
    const tid = this.summary()?.officer?.thread_id;
    if (tid) void this.router.navigate(['/sessions', tid]);
  }

  async openConference(): Promise<void> {
    const pid = this.projectId();
    if (!pid || this.busy()) return;
    const existing = this.summary()?.conference;
    if (existing) {
      await this.router.navigate(['/sessions', existing.thread_id]);
      return;
    }
    this.busy.set(true);
    this.message.set('');
    try {
      const resp = await firstValueFrom(
        this.http.post<{thread_id: string}>(
          `${environment.apiUrl}/persistent/threads`,
          {
            title: `Conference — ${this.projectName() || 'project'}`,
            config_name: 'centurion',
            project_ids: [pid],
            config_override: {officer: {conference: true}},
          },
        ),
      );
      await this.router.navigate(['/sessions', resp.thread_id]);
    } catch (err) {
      // conference_open 409 → someone beat us; refresh finds it.
      this.message.set(this.errText(err, 'Could not open the conference'));
      this.busy.set(false);
      this.refresh(true);
    }
  }

  async retire(): Promise<void> {
    const tid = this.summary()?.officer?.thread_id;
    if (!tid || this.busy()) return;
    this.busy.set(true);
    this.message.set('');
    try {
      await firstValueFrom(
        this.http.delete(`${environment.apiUrl}/persistent/threads/${tid}`),
      );
      this.message.set(
        'Centurion retired — the watchdog stands down; his log is preserved.',
      );
    } catch (err) {
      this.message.set(this.errText(err, 'Retirement failed'));
    } finally {
      this.retireArmed.set(false);
      this.busy.set(false);
      this.refresh(true);
    }
  }

  private errText(err: unknown, fallback: string): string {
    const detail = (err as {error?: {detail?: unknown}})?.error?.detail;
    return typeof detail === 'string' && detail ? detail : fallback;
  }
}
