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
import {Router, RouterLink} from '@angular/router';
import {firstValueFrom} from 'rxjs';
import {TranslocoPipe} from '@jsverse/transloco';

import {environment} from '../../core/environment';
import {ApiService} from '../../core/services/api.service';
import {ModelService} from '../../core/services/model.service';
import type {
  OfficerBrainSpec,
  OfficerDecommissionResult,
  OfficerHold,
  OfficerKitSlot,
  OfficerPost,
  OfficerPostPatch,
  OfficerSlotSpec,
  OfficerVacantLedger,
  WorkerMessagesPolicy,
} from '../../core/models/api.model';
import {AppButtonComponent} from '../../ui/button';
import {AppInputComponent} from '../../ui/input';
import {AppSelectComponent} from '../../ui/select';
import {AppFormFieldComponent} from '../../ui/form-field';
import {AppSpinnerComponent} from '../../ui/spinner';

/** Editable roster row in the kit editor. */
export interface SlotDraft {
  name: string;
  count: number;
  model: string;
  backend: string;
  /** '' = not a pool (officer-directed only); otherwise a work category. */
  category: string;
}

/**
 * The categories a slot may be a pool for. Mirrors WORK_CATEGORIES in
 * orchestrator/services/work_categories.py — the server validates against its
 * own copy at provision time, so a drift here costs a 400, never a bad kit.
 */
export const WORK_CATEGORY_OPTIONS = [
  {value: '', label: 'No pool — officer dispatches by hand'},
  {value: 'researcher', label: 'Researcher — deliverable is an answer'},
  {value: 'tester', label: 'Tester — deliverable is issue tickets'},
  {value: 'executor', label: 'Executor — deliverable is shipped files'},
] as const;

/** The whole kit editor as plain strings ('' = unset / server default). */
export interface OfficerEditorDraft {
  slots: SlotDraft[];
  brainModel: string;
  reasoning: string;
  sleepMin: string;
  sleepMax: string;
  tokenCeiling: string;
  maxPages: string;
  maxActions: string;
  maxWorkers: string;
}

/** The card's one state machine (officer_post.md §8). */
export type OfficerPostState = 'vacant' | 'commissioned' | 'held';

/** §11 Q2 (decided: keep): the draft a never-kitted vacant post starts from. */
export const STARTER_SLOT_DRAFT: SlotDraft = {
  name: 'line',
  count: 2,
  model: '',
  backend: 'sandbox',
  // No category by default: a starter kit must not silently arm auto-pull on
  // a century whose officer has never triaged a backlog.
  category: '',
};

/** Vacant / commissioned / held — held is commissioned-and-standing-down. */
export function postStateOf(post: OfficerPost | null): OfficerPostState {
  if (!post?.commissioned) return 'vacant';
  return post.held ? 'held' : 'commissioned';
}

/**
 * "held — maintenance" / "held — conference" from the hold's kind — replaces
 * the old hardcoded "held — conference in progress", which was wrong for
 * maintenance holds. The note renders separately.
 */
export function holdBadgeLabel(held: OfficerHold | null | undefined): string {
  const kind = held?.kind?.trim();
  return kind ? `held — ${kind}` : 'held';
}

/** Editor fields grouped by when a live edit actually lands (§7's table). */
export type OfficerEditField =
  | 'slots'
  | 'max_concurrent_workers'
  | 'daily_token_ceiling'
  | 'max_pages_per_day'
  | 'sleep'
  | 'max_actions_per_wake'
  | 'brain';

/** Per-field honesty: what the §7 table promises, verbatim in the UI. */
export function immediacyLabel(field: OfficerEditField): string {
  switch (field) {
    case 'slots':
    case 'max_concurrent_workers':
      return 'applies at next dispatch';
    case 'daily_token_ceiling':
    case 'max_pages_per_day':
      return 'applies at next delivery';
    case 'sleep':
      return 'applies at next sleep filing';
    case 'max_actions_per_wake':
    case 'brain':
      return 'applies on next respawn';
  }
}

/**
 * Shrinking a slot below its in-flight count is drain semantics (§7, decided):
 * new dispatches 409, running jobs are untouched. The hint says so.
 */
export function drainHint(
  inFlight: number | null | undefined,
  newCount: number,
): string | null {
  if (!inFlight || newCount >= inFlight) return null;
  return `${inFlight} in flight — drains to ${newCount}`;
}

/**
 * Utilization chip per kit slot: `line 1/2 · MiniMax-M3 · vm` (×N without live
 * data). Pools carry two more facts, because utilization alone answers the
 * wrong question — an idle slot with a healthy queue is slack, and slack is
 * fine. What matters is whether the QUEUE is starved, so a pool shows its ready
 * depth and is flagged when it sits below its floor (= its slot count). An open
 * breaker wins the flag: an idle pool that is idle because it is BROKEN must
 * not read as merely quiet.
 *
 * `ready_depth` absent means the knowledge base could not be read. It renders
 * as nothing rather than as `ready 0`, which would be an unmeasured claim about
 * a starved queue.
 */
export function kitChips(
  kit: Record<string, OfficerKitSlot> | null | undefined,
  breakers: Record<string, {until?: string}> | null | undefined = null,
  now: Date = new Date(),
): {name: string; label: string; alert: boolean}[] {
  if (!kit) return [];
  return Object.entries(kit).map(([name, s]) => {
    const alloc = s.in_flight != null ? `${s.in_flight}/${s.count}` : `×${s.count}`;
    const parts = [`${name} ${alloc}`];
    if (s.category) parts.push(s.category);
    if (s.model) parts.push(s.model);
    if (s.backend) parts.push(s.backend);
    if (s.ready_depth != null) {
      parts.push(`ready ${s.ready_depth}${s.below_floor ? ' — BELOW FLOOR' : ''}`);
    }
    const until = breakers?.[name]?.until;
    const broken = !!until && new Date(until).getTime() > now.getTime();
    if (broken) parts.push('BREAKER OPEN');
    return {name, label: parts.join(' · '), alert: broken || !!s.below_floor};
  });
}

/**
 * Seed the editor from the post. A never-kitted VACANT post gets the starter
 * draft (§11 Q2: keep it); a commissioned officer without slots is genuinely
 * flat-cap — no starter is invented for him. Vacant posts expose only `kit`
 * in the O1–O4 contract, so the non-kit fields seed to '' until commissioned.
 */
export function draftFromPost(post: OfficerPost | null): OfficerEditorDraft {
  const kit = post?.kit ?? null;
  const kitRows: SlotDraft[] = kit
    ? Object.entries(kit).map(([name, s]) => ({
        name,
        count: s.count,
        model: s.model ?? '',
        backend: s.backend ?? '',
        category: s.category ?? '',
      }))
    : [];
  const slots = kitRows.length
    ? kitRows
    : post?.commissioned
      ? []
      : [{...STARTER_SLOT_DRAFT}];
  const o = post?.officer ?? null;
  const num = (v: number | null | undefined): string => (v == null ? '' : String(v));
  return {
    slots,
    brainModel: o?.model ?? '',
    reasoning: o?.reasoning_level ?? '',
    sleepMin: num(o?.sleep_minutes?.min),
    sleepMax: num(o?.sleep_minutes?.max),
    tokenCeiling: num(o?.token_ceiling?.daily),
    maxPages: num(o?.pages_today?.budget),
    maxActions: num(o?.max_actions_per_wake),
    maxWorkers: num(o?.max_concurrent_workers),
  };
}

/** Assemble the `officer.slots` spec from form rows; null = no roster (flat cap). */
export function buildSlotsSpec(rows: SlotDraft[]): Record<string, OfficerSlotSpec> | null {
  const spec: Record<string, OfficerSlotSpec> = {};
  for (const row of rows) {
    const name = row.name.trim().toLowerCase();
    if (!name) continue;
    const entry: OfficerSlotSpec = {
      count: Math.max(1, Math.floor(row.count)),
    };
    if (row.model.trim()) entry.model = row.model.trim();
    if (row.backend.trim()) entry.backend = row.backend.trim();
    // `?? ''` because this field is newer than the type: a draft assembled
    // before it existed (or by a caller that skips the type) must degrade to
    // "not a pool" rather than throwing mid-save.
    const category = (row.category ?? '').trim();
    if (category) entry.category = category.toLowerCase();
    spec[name] = entry;
  }
  return Object.keys(spec).length ? spec : null;
}

/**
 * Full request body from the editor — the commission body, and the base both
 * sides of the PATCH diff are computed from. Blank fields are omitted, not
 * nulled: commission must not clear row state the editor never saw.
 */
export function buildOfficerConfig(draft: OfficerEditorDraft): OfficerPostPatch {
  const body: OfficerPostPatch = {};
  const slots = buildSlotsSpec(draft.slots);
  if (slots) body.slots = slots;
  const brain: OfficerBrainSpec = {};
  if (draft.brainModel.trim()) brain.model = draft.brainModel.trim();
  if (draft.reasoning) brain.reasoning_level = draft.reasoning;
  if (Object.keys(brain).length) body.brain = brain;
  const num = (v: string): number | undefined => {
    const n = parseInt(v, 10);
    return Number.isNaN(n) ? undefined : n;
  };
  const sleepMin = num(draft.sleepMin);
  if (sleepMin !== undefined) body.sleep_min_minutes = sleepMin;
  const sleepMax = num(draft.sleepMax);
  if (sleepMax !== undefined) body.sleep_max_minutes = sleepMax;
  const ceiling = num(draft.tokenCeiling);
  if (ceiling !== undefined) body.daily_token_ceiling = ceiling;
  const pages = num(draft.maxPages);
  if (pages !== undefined) body.max_pages_per_day = pages;
  const actions = num(draft.maxActions);
  if (actions !== undefined) body.max_actions_per_wake = actions;
  const workers = num(draft.maxWorkers);
  if (workers !== undefined) body.max_concurrent_workers = workers;
  return body;
}

const PATCH_FIELDS: (keyof OfficerPostPatch)[] = [
  'slots',
  'brain',
  'sleep_min_minutes',
  'sleep_max_minutes',
  'daily_token_ceiling',
  'max_pages_per_day',
  'max_actions_per_wake',
  'max_concurrent_workers',
];

/**
 * Field-wise diff against the post's last known state: only what actually
 * changed goes on the wire (the server injects a notice per edit — don't cry
 * wolf). A field the user cleared PATCHes as explicit null. Deliberately
 * never emits `communication_policy` — the routing control PATCHes that
 * alone (§7: a row-only, user-owned field).
 */
export function buildOfficerPatch(
  baseline: OfficerEditorDraft,
  draft: OfficerEditorDraft,
): OfficerPostPatch {
  const before = buildOfficerConfig(baseline);
  const after = buildOfficerConfig(draft);
  const patch: OfficerPostPatch = {};
  for (const field of PATCH_FIELDS) {
    const a = before[field];
    const b = after[field];
    if (JSON.stringify(a ?? null) !== JSON.stringify(b ?? null)) {
      (patch as Record<string, unknown>)[field] = b ?? null;
    }
  }
  return patch;
}

/**
 * Normalize the while-vacant ledger. The O3 shape is `{entries, dropped}`
 * (ring, cap 20, drop-oldest); a bare array is tolerated so the card renders
 * either way while the backend contract settles.
 */
export function vacantLedgerOf(
  post: OfficerPost | null,
): {entries: NonNullable<OfficerVacantLedger['entries']>; dropped: number} | null {
  const raw = post?.while_vacant as
    | OfficerVacantLedger
    | OfficerVacantLedger['entries']
    | null
    | undefined;
  if (!raw) return null;
  const entries = Array.isArray(raw) ? raw : (raw.entries ?? []);
  const dropped = Array.isArray(raw) ? 0 : (raw.dropped ?? 0);
  if (!entries.length && !dropped) return null;
  return {entries, dropped};
}

/** Build the officer conference's trusted thread-create request. */
export function buildConferenceThreadCreateBody(
  projectId: string,
  projectName: string,
): Record<string, unknown> {
  return {
    title: `Conference — ${projectName || 'project'}`,
    config_name: 'centurion',
    project_ids: [projectId],
    use_datasource_defaults: true,
    config_override: {officer: {conference: true}},
  };
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
 * Project Centurion tab — the officer's POST (officer_post.md §8).
 *
 * One card, one state machine: vacant / commissioned / held. The kit editor
 * is the card body in every state — seeded from the row while vacant (last
 * real kit, else the starter draft) and populated live once commissioned,
 * with per-slot utilization, per-field immediacy labels (§7), spend against
 * the ceiling, and the lifecycle: commission, hold/release, decommission
 * (warning on in-flight jobs — they are left running, never cancelled). The
 * log IS his session transcript — the card links there rather than
 * duplicating it. Worker-question routing (officer_message_routing.md §6) is
 * the one row-only policy and PATCHes alone.
 */
@Component({
  selector: 'app-project-officer',
  standalone: true,
  imports: [
    DatePipe,
    DecimalPipe,
    TranslocoPipe,
    RouterLink,
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
          his background log is the session transcript. The post outlives him
          — kit, budgets, and memory stay with it between incarnations.
        </p>
      </div>

      @if (loading()) {
        <div class="officer-loading"><app-spinner size="md" tone="accent" /></div>
      } @else {
        <div class="officer-card" data-testid="officer-card" [attr.data-state]="postState()">
          @if (postState() === 'vacant') {
            <p class="officer-hint">
              The post is vacant — no centurion holds this century. Assign the
              kit and commission him: he chooses which troops to send; you
              decide what they are made of.
            </p>

            @if (vacantLedger(); as ledger) {
              <div class="officer-ledger" data-testid="officer-vacant-ledger">
                <div class="officer-section-title">While the post was vacant</div>
                @for (e of ledger.entries; track $index) {
                  <div class="officer-ledger-item">
                    @if (e.at) { <span class="dim">{{ e.at | date: 'short' }}</span> }
                    <span>{{ e.title || e.job_id }}</span>
                    @if (e.status) { <span class="officer-ledger-status">{{ e.status }}</span> }
                  </div>
                }
                @if (ledger.dropped) {
                  <div class="dim">… {{ ledger.dropped }} older entr{{ ledger.dropped === 1 ? 'y' : 'ies' }} dropped</div>
                }
              </div>
            }
          } @else {
            @if (post()?.officer; as o) {
              <div class="officer-status-row">
                <span class="officer-badge" [attr.data-status]="o.status">{{ o.status }}</span>
                @if (post()?.held; as h) {
                  <span class="officer-hold" data-testid="officer-hold">{{ holdLabel() }}</span>
                  @if (h.note) {
                    <span class="officer-hold-note" data-testid="officer-hold-note">{{ h.note }}</span>
                  }
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
                  <span class="v">{{ o.pending_events ?? 0 }}</span>
                </div>
                <div>
                  <span class="k">Pages today</span>
                  <span class="v">{{ o.pages_today?.used ?? 0 }}/{{ o.pages_today?.budget ?? 3 }}</span>
                </div>
                @if (post()?.spend_today; as spend) {
                  <div>
                    <span class="k">Spend today</span>
                    <span class="v" data-testid="officer-spend">
                      {{ (spend.tokens ?? 0) | number }}
                      @if (spendCeiling() != null) {
                        / {{ spendCeiling() | number }}
                      }
                      tokens
                      @if (o.token_ceiling?.deferred_today) {
                        <span class="officer-warn">— ceiling reached; sleeping until reset</span>
                      }
                    </span>
                  </div>
                }
                <div>
                  <span class="k">His model</span>
                  <span class="v" data-testid="officer-model">
                    {{ o.model || 'session default' }}
                    @if (o.reasoning_level) { · {{ o.reasoning_level }} }
                  </span>
                </div>
              </div>

              @if (kitRows().length) {
                <div class="officer-slots" data-testid="officer-slots">
                  <span class="k">Kit</span>
                  @for (s of kitRows(); track s.name) {
                    <span
                      class="officer-slot-chip"
                      [class.officer-slot-chip-alert]="s.alert"
                      >{{ s.label }}</span
                    >
                  }
                </div>
              }

              @if (backlogState(); as bl) {
                <div class="officer-slots" data-testid="officer-backlog">
                  <span class="k">Auto-pull</span>
                  <span class="v">
                    @if (bl.auto_pull) {
                      on — ready tickets are dispatched into free pool slots
                    } @else {
                      <span class="officer-warn"
                        >off — pools stay idle until he dispatches by hand</span
                      >
                    }
                  </span>
                </div>
                @if (staleClaims().length) {
                  <div class="officer-slots" data-testid="officer-stale-claims">
                    <span class="k">Stalled</span>
                    <span class="v officer-warn">
                      {{ staleClaims().length }} claimed
                      {{ staleClaims().length === 1 ? 'ticket' : 'tickets' }} not
                      moving — these are never released automatically; resume or
                      cancel the job, then close or re-ready the ticket.
                    </span>
                  </div>
                }
              }
              @if (provisioningProblems().length) {
                <div class="officer-slots" data-testid="officer-provisioning-state">
                  <span class="k">{{ 'officerCorrectness.provisioningLabel' | transloco }}</span>
                  <span class="v officer-warn">
                    {{ 'officerCorrectness.provisioningProblem' | transloco: {count: provisioningProblems().length} }}
                    · {{ provisioningStateSummary() }}
                  </span>
                </div>
              }
              @if (knowledgeProblems().length) {
                <div class="officer-slots" data-testid="officer-knowledge-sync">
                  <span class="k">{{ 'officerCorrectness.knowledgeLabel' | transloco }}</span>
                  <span class="v officer-warn">
                    {{ 'officerCorrectness.knowledgeProblem' | transloco: {count: knowledgeProblems().length} }}
                    · {{ knowledgeStateSummary() }}
                  </span>
                </div>
              }
              @if (latestFloorWake(); as wake) {
                <div class="officer-slots" data-testid="officer-floor-wake">
                  <span class="k">{{ 'officerCorrectness.floorWakeLabel' | transloco }}</span>
                  <span class="v">
                    {{ wake.pool || ('officerCorrectness.poolFallback' | transloco) }} ·
                    @if (wake.delivered_at) {
                      {{ 'officerCorrectness.delivered' | transloco }}
                    } @else if (wake.last_queued_at) {
                      {{ 'officerCorrectness.durablyQueued' | transloco }}
                      @if (wake.failure_class) {
                        · <span class="officer-warn">{{ 'officerCorrectness.deliveryFailed' | transloco }} · {{ wake.failure_class }}</span>
                      }
                    } @else {
                      <span class="officer-warn">{{ 'officerCorrectness.notQueued' | transloco }} · {{ wake.failure_class || wake.state }}</span>
                    }
                  </span>
                </div>
              }
            }
          }

          <!-- THE EDITOR — the same editor in every state (§8) -->
          <div class="officer-editor" data-testid="officer-editor">
            <div class="officer-editor-head">
              <span class="officer-section-title">His brain</span>
              @if (showImmediacy()) {
                <span class="officer-immediacy" data-testid="immediacy-brain">{{ immediacy('brain') }}</span>
              }
            </div>
            <div class="officer-slot-row">
              <app-form-field
                label="Officer's own model"
                hint="The brain his judgment runs on — pick a strong one; his wakes are cheap but his decisions steer everything. Blank = your session default."
              >
                <app-select [value]="fBrainModel()" (changed)="fBrainModel.set($event ?? '')">
                  <option value="">session default</option>
                  @for (m of modelOptions(); track m) {
                    <option [value]="m">{{ m }}</option>
                  }
                </app-select>
              </app-form-field>
              <app-form-field
                label="Reasoning"
                hint="Effort per wake. Clamped to what the model supports."
              >
                <app-select [value]="fReasoning()" (changed)="fReasoning.set($event ?? '')">
                  <option value="">default (high)</option>
                  <option value="low">low</option>
                  <option value="medium">medium</option>
                  <option value="high">high</option>
                  <option value="xhigh">xhigh</option>
                  <option value="max">max</option>
                </app-select>
              </app-form-field>
              <app-form-field label="Actions per wake" hint="Hard cap on tool calls each wake.">
                <app-input
                  type="number"
                  [value]="fMaxActions()"
                  (changed)="fMaxActions.set($event)"
                  placeholder="default"
                />
              </app-form-field>
            </div>

            <div class="officer-editor-head">
              <span class="officer-section-title">Kit</span>
              @if (showImmediacy()) {
                <span class="officer-immediacy" data-testid="immediacy-slots">{{ immediacy('slots') }}</span>
              }
            </div>
            <p class="officer-hint dim">
              Give a slot a <strong>pool</strong> and the officer fills it
              automatically from ready tickets of that kind; leave it unset for
              capacity he dispatches by hand.
            </p>
            @for (row of slotDrafts(); track $index; let i = $index) {
              <div class="officer-slot-row">
                <app-form-field label="Slot">
                  <app-input
                    [value]="row.name"
                    (changed)="patchSlot(i, {name: $event})"
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
                <app-form-field label="Pool">
                  <app-select
                    [value]="row.category"
                    (changed)="patchSlot(i, {category: $event ?? ''})"
                  >
                    @for (c of categoryOptions; track c.value) {
                      <option [value]="c.value">{{ c.label }}</option>
                    }
                  </app-select>
                </app-form-field>
                <app-button variant="secondary" size="sm" (clicked)="removeSlot(i)">✕</app-button>
              </div>
              @if (drainHints()[i]; as hint) {
                <div class="officer-drain" data-testid="drain-hint">{{ hint }}</div>
              }
            }
            <div class="officer-slot-row">
              <app-form-field
                label="Max workers"
                hint="Flat concurrency cap. Without a kit it is the only gate (default 3)."
              >
                <app-input
                  type="number"
                  [value]="fMaxWorkers()"
                  (changed)="fMaxWorkers.set($event)"
                  placeholder="3"
                />
              </app-form-field>
              <app-button variant="secondary" size="sm" [disabled]="busy()" (clicked)="addSlot()">
                Add slot
              </app-button>
            </div>

            <div class="officer-editor-head">
              <span class="officer-section-title">Budgets</span>
              @if (showImmediacy()) {
                <span class="officer-immediacy" data-testid="immediacy-budgets">{{ immediacy('daily_token_ceiling') }}</span>
              }
            </div>
            <div class="officer-slot-row">
              <app-form-field
                label="Daily token ceiling"
                hint="Across his whole command. Blank = no ceiling."
              >
                <app-input
                  type="number"
                  [value]="fTokenCeiling()"
                  (changed)="fTokenCeiling.set($event)"
                  placeholder="unlimited"
                />
              </app-form-field>
              <app-form-field label="Pages per day" hint="How often he may page you (default 3).">
                <app-input
                  type="number"
                  [value]="fMaxPages()"
                  (changed)="fMaxPages.set($event)"
                  placeholder="3"
                />
              </app-form-field>
            </div>

            <div class="officer-editor-head">
              <span class="officer-section-title">Sleep</span>
              @if (showImmediacy()) {
                <span class="officer-immediacy" data-testid="immediacy-sleep">{{ immediacy('sleep') }}</span>
              }
            </div>
            <div class="officer-slot-row">
              <app-form-field
                label="Min (minutes)"
                hint="He files his own sleep between wakes; the server clamps to these bounds."
              >
                <app-input
                  type="number"
                  [value]="fSleepMin()"
                  (changed)="fSleepMin.set($event)"
                  placeholder="5"
                />
              </app-form-field>
              <app-form-field label="Max (minutes)">
                <app-input
                  type="number"
                  [value]="fSleepMax()"
                  (changed)="fSleepMax.set($event)"
                  placeholder="60"
                />
              </app-form-field>
            </div>
          </div>

          @if (postState() === 'vacant') {
            <div class="officer-actions">
              <app-button
                variant="primary"
                size="sm"
                [disabled]="busy()"
                (clicked)="commission()"
                data-testid="officer-commission"
              >
                Commission centurion
              </app-button>
            </div>
            <p class="officer-hint dim">
              No slots = a flat cap of 3 concurrent workers. The officer runs
              headless; his first wake carries the continuity brief — and the
              while-vacant ledger, if jobs finished without him.
            </p>
          } @else {
            <div class="officer-actions">
              <app-button
                variant="primary"
                size="sm"
                [disabled]="busy() || !dirty()"
                (clicked)="saveEdits()"
                data-testid="officer-save"
              >
                Save changes
              </app-button>
              <app-button variant="secondary" size="sm" (clicked)="openLog()">Open log</app-button>
              <app-button variant="secondary" size="sm" [disabled]="busy()" (clicked)="openConference()">
                {{ post()?.officer?.conference ? 'Rejoin conference' : 'Conference' }}
              </app-button>
              @if (postState() === 'held') {
                <app-button
                  variant="secondary"
                  size="sm"
                  [disabled]="busy()"
                  (clicked)="release()"
                  data-testid="officer-release"
                >
                  Release
                </app-button>
              } @else if (!holdArmed()) {
                <app-button variant="secondary" size="sm" [disabled]="busy()" (clicked)="holdArmed.set(true)">
                  Hold
                </app-button>
              }
              @if (!decommissionArmed()) {
                <app-button
                  variant="danger"
                  size="sm"
                  [disabled]="busy()"
                  (clicked)="decommissionArmed.set(true)"
                >
                  Decommission
                </app-button>
              }
            </div>

            @if (holdArmed()) {
              <div class="officer-confirm" data-testid="officer-hold-confirm">
                <app-form-field
                  label="Hold note"
                  hint="Shown on the badge — why he is standing down. Legate input still reaches him while held."
                >
                  <app-input
                    [value]="holdNote()"
                    (changed)="holdNote.set($event)"
                    placeholder="maintenance"
                  />
                </app-form-field>
                <div class="officer-actions">
                  <app-button variant="primary" size="sm" [disabled]="busy()" (clicked)="hold()">
                    Confirm hold
                  </app-button>
                  <app-button variant="secondary" size="sm" (clicked)="holdArmed.set(false)">Cancel</app-button>
                </div>
              </div>
            }

            @if (decommissionArmed()) {
              <div class="officer-confirm" data-testid="officer-decommission-confirm">
                @if (decommissionWarning(); as warn) {
                  <p class="officer-hint" data-testid="officer-decommission-warning">
                    He has {{ warn.in_flight_jobs?.length ?? 0 }} job(s) in flight.
                    Decommissioning leaves them running — their completions land on
                    the post's ledger for his successor.
                  </p>
                  @for (j of warn.in_flight_jobs ?? []; track j.job_id) {
                    <div class="officer-ledger-item">
                      <span>{{ j.title || shortId(j.job_id) }}</span>
                      @if (j.slot) { <span class="officer-slot-chip">{{ j.slot }}</span> }
                      @if (j.status) { <span class="dim">{{ j.status }}</span> }
                    </div>
                  }
                  <div class="officer-actions">
                    <app-button
                      variant="danger"
                      size="sm"
                      [disabled]="busy()"
                      (clicked)="decommission(true)"
                      data-testid="officer-decommission-force"
                    >
                      Decommission — leave them running
                    </app-button>
                    <app-button variant="secondary" size="sm" (clicked)="cancelDecommission()">Keep him</app-button>
                  </div>
                } @else {
                  <p class="officer-hint">
                    His kit, budgets, digest, and sitrep fingerprints stay on the
                    post; recommissioning restores them with a continuity brief.
                  </p>
                  <div class="officer-actions">
                    <app-button
                      variant="danger"
                      size="sm"
                      [disabled]="busy()"
                      (clicked)="decommission(false)"
                      data-testid="officer-decommission"
                    >
                      Confirm decommission
                    </app-button>
                    <app-button variant="secondary" size="sm" (clicked)="cancelDecommission()">Keep him</app-button>
                  </div>
                }
              </div>
            }
          }

          <!-- Worker questions — the row-only routing policy; exists in every state -->
          <div class="officer-policy" data-testid="officer-policy">
            <div class="officer-editor-head">
              <span class="officer-section-title">Worker questions</span>
            </div>
            <app-form-field label="When a worker asks a question" [hint]="policyHint()">
              <app-select [value]="policyValue()" (changed)="setPolicy($event)">
                <option value="user_direct">Direct to me</option>
                <option value="officer_and_user">Officer and me</option>
                <option value="officer_first">{{ officerFirstLabel() }}</option>
              </app-select>
            </app-form-field>
          </div>

          @if (postState() === 'vacant' && incarnations().length) {
            <div class="officer-incarnations" data-testid="officer-incarnations">
              <div class="officer-section-title">Past incarnations</div>
              @for (inc of incarnations(); track inc.thread_id) {
                <div class="officer-ledger-item">
                  <a class="officer-incarnation-link" [routerLink]="['/sessions', inc.thread_id]">
                    {{ shortId(inc.thread_id) }}
                  </a>
                  <span class="dim">
                    {{ inc.commissioned_at | date: 'mediumDate' }} →
                    {{ inc.decommissioned_at | date: 'mediumDate' }}
                  </span>
                  @if (inc.reason) { <span>{{ inc.reason }}</span> }
                </div>
              }
            </div>
          }

          @if (postState() !== 'vacant' && digest().length) {
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
      .officer-hold-note { font-size: 12px; color: var(--text-secondary); font-style: italic; }
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
      /* A starved or broken pool. Border + text rather than a fill: it must
         read as attention-needed at a glance without competing with a real
         error state elsewhere on the card. */
      .officer-slot-chip-alert {
        border-color: var(--color-warning, #b45309);
        color: var(--color-warning, #b45309);
      }
      .officer-editor { display: flex; flex-direction: column; gap: 10px; }
      .officer-editor-head { display: flex; align-items: baseline; gap: 10px; margin-top: 4px; }
      .officer-section-title { font-size: 12px; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.4px; }
      .officer-immediacy {
        font-size: 11px;
        color: var(--text-tertiary);
        font-style: italic;
        padding: 1px 8px;
        border-radius: 999px;
        border: 1px dashed var(--border-color);
      }
      .officer-drain { font-size: 12px; color: var(--warning, #eab308); padding-left: 2px; }
      .officer-actions { display: flex; gap: 8px; flex-wrap: wrap; }
      .officer-confirm {
        border: 1px solid var(--border-color);
        border-radius: 8px;
        padding: 10px 12px;
        display: flex;
        flex-direction: column;
        gap: 8px;
        background: var(--bg-tertiary);
      }
      .officer-policy { display: flex; flex-direction: column; gap: 6px; }
      .officer-ledger, .officer-incarnations { display: flex; flex-direction: column; gap: 6px; }
      .officer-ledger-item { display: flex; gap: 8px; font-size: 13px; flex-wrap: wrap; align-items: baseline; }
      .officer-ledger-status { font-size: 12px; color: var(--text-secondary); }
      .officer-incarnation-link { font-family: var(--font-mono, monospace); font-size: 12px; }
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
  private readonly api = inject(ApiService);
  private readonly router = inject(Router);
  private readonly modelService = inject(ModelService);

  readonly post = signal<OfficerPost | null>(null);
  readonly loading = signal(true);
  readonly busy = signal(false);
  readonly message = signal('');
  readonly decommissionArmed = signal(false);
  readonly decommissionWarning = signal<OfficerDecommissionResult | null>(null);
  readonly holdArmed = signal(false);
  readonly holdNote = signal('');

  // The kit editor — one set of drafts, every state. Seeded from the post on
  // state transitions only (editorSeedKey), so the 15s poll never clobbers
  // in-progress edits.
  readonly slotDrafts = signal<SlotDraft[]>([{...STARTER_SLOT_DRAFT}]);
  readonly categoryOptions = WORK_CATEGORY_OPTIONS;
  // The officer's OWN brain (distinct from the slot models, which are what
  // his workers run on — the classic mistake is arming the troops and leaving
  // the commander on the account default).
  readonly fBrainModel = signal('');
  readonly fReasoning = signal('');
  readonly fSleepMin = signal('');
  readonly fSleepMax = signal('');
  readonly fTokenCeiling = signal('');
  readonly fMaxPages = signal('');
  readonly fMaxActions = signal('');
  readonly fMaxWorkers = signal('');

  private editorSeedKey: string | null = null;
  private pollHandle: ReturnType<typeof setInterval> | null = null;

  /** §7's per-field honesty, exposed to the template. */
  readonly immediacy = immediacyLabel;

  readonly modelOptions = computed(() =>
    this.modelService.models().flatMap((g) => g.models),
  );
  readonly postState = computed<OfficerPostState>(() => postStateOf(this.post()));
  readonly showImmediacy = computed(() => this.postState() !== 'vacant');
  readonly holdLabel = computed(() => holdBadgeLabel(this.post()?.held));
  readonly wakeLabel = computed(() =>
    nextWakeLabel(this.post()?.officer?.next_wake_at ?? null),
  );
  readonly digest = computed(() =>
    [...(this.post()?.officer?.digest ?? [])].reverse(),
  );
  readonly kitRows = computed(() =>
    kitChips(this.post()?.kit, this.post()?.backlog?.breakers),
  );
  /** Pool policy the officer is operating under, or null when he has no pools. */
  readonly backlogState = computed(() => {
    const post = this.post();
    const hasPools = Object.values(post?.kit ?? {}).some((s) => !!s.category);
    return hasPools ? (post?.backlog ?? null) : null;
  });
  readonly staleClaims = computed(() => this.backlogState()?.stale_claims ?? []);
  readonly provisioningProblems = computed(
    () => this.post()?.backlog?.provisioning_preflights ?? [],
  );
  readonly provisioningStateSummary = computed(() =>
    [
      ...new Set(
        this.provisioningProblems().map(
          (row) => row.context?.provisioning_preflight?.state ?? 'unknown',
        ),
      ),
    ].join(', '),
  );
  readonly knowledgeProblems = computed(() =>
    (this.post()?.backlog?.knowledge_materialization ?? []).filter(
      (row) => row.canonical_state !== 'canonical' || row.projection_state !== 'synced',
    ),
  );
  readonly knowledgeStateSummary = computed(() =>
    [
      ...new Set(
        this.knowledgeProblems().map(
          (row) => `${row.canonical_state}/${row.projection_state}`,
        ),
      ),
    ].join(', '),
  );
  readonly latestFloorWake = computed(
    () => this.post()?.backlog?.floor_wakes?.[0] ?? null,
  );
  readonly spendCeiling = computed(
    () =>
      this.post()?.spend_today?.ceiling ??
      this.post()?.officer?.token_ceiling?.daily ??
      null,
  );
  readonly incarnations = computed(() =>
    [...(this.post()?.incarnations ?? [])].reverse(),
  );
  readonly vacantLedger = computed(() => vacantLedgerOf(this.post()));

  private readonly currentDraft = computed<OfficerEditorDraft>(() => ({
    slots: this.slotDrafts(),
    brainModel: this.fBrainModel(),
    reasoning: this.fReasoning(),
    sleepMin: this.fSleepMin(),
    sleepMax: this.fSleepMax(),
    tokenCeiling: this.fTokenCeiling(),
    maxPages: this.fMaxPages(),
    maxActions: this.fMaxActions(),
    maxWorkers: this.fMaxWorkers(),
  }));
  /** What Save would send — diffed against the post's last known state. */
  readonly pendingPatch = computed(() =>
    buildOfficerPatch(draftFromPost(this.post()), this.currentDraft()),
  );
  readonly dirty = computed(() => Object.keys(this.pendingPatch()).length > 0);
  /** Per-draft-row drain hint when shrinking a slot below its in-flight count. */
  readonly drainHints = computed<(string | null)[]>(() => {
    const kit = this.post()?.kit ?? null;
    const vacant = this.postState() === 'vacant';
    return this.slotDrafts().map((row) => {
      if (vacant || !kit) return null;
      const live = kit[row.name.trim().toLowerCase()];
      return drainHint(live?.in_flight, Math.max(1, Math.floor(row.count)));
    });
  });

  readonly policyValue = computed<WorkerMessagesPolicy>(
    () => this.post()?.communication_policy?.worker_messages ?? 'user_direct',
  );
  readonly officerFirstLabel = computed(() =>
    this.postState() === 'vacant' ? 'Officer first' : 'Officer first (recommended)',
  );
  readonly policyHint = computed(() => {
    const mins = this.post()?.communication_policy?.officer_response_minutes ?? 15;
    const base =
      `Officer first falls back to you if the officer is unavailable, ` +
      `and escalates to you if he stays silent past ${mins} min.`;
    return this.postState() === 'vacant'
      ? `${base} While the post is vacant, questions come direct to you regardless.`
      : base;
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
    this.api.getOfficerPost(pid).subscribe((p) => this.applyPost(p));
  }

  /**
   * A transport failure (null) never flips a commissioned card back to the
   * vacant editor — stale beats wrong-state; the next poll heals it.
   */
  private applyPost(p: OfficerPost | null): void {
    if (p) {
      this.post.set(p);
      const key = p.commissioned ? `c:${p.officer?.thread_id ?? ''}` : 'vacant';
      if (this.editorSeedKey !== key) {
        this.seedEditor(draftFromPost(p));
        this.editorSeedKey = key;
      }
    } else if (!this.post() && this.editorSeedKey === null) {
      this.seedEditor(draftFromPost(null));
      this.editorSeedKey = 'none';
    }
    this.loading.set(false);
  }

  private seedEditor(draft: OfficerEditorDraft): void {
    this.slotDrafts.set(draft.slots);
    this.fBrainModel.set(draft.brainModel);
    this.fReasoning.set(draft.reasoning);
    this.fSleepMin.set(draft.sleepMin);
    this.fSleepMax.set(draft.sleepMax);
    this.fTokenCeiling.set(draft.tokenCeiling);
    this.fMaxPages.set(draft.maxPages);
    this.fMaxActions.set(draft.maxActions);
    this.fMaxWorkers.set(draft.maxWorkers);
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
      // Uncategorized: a new row is plain capacity until someone chooses a
      // pool for it, so adding a slot can never arm auto-pull by accident.
      {name: '', count: 1, model: '', backend: '', category: ''},
    ]);
  }

  removeSlot(index: number): void {
    this.slotDrafts.update((rows) => rows.filter((_, i) => i !== index));
  }

  shortId(id: string): string {
    return id.length > 8 ? id.slice(0, 8) : id;
  }

  /** Raise an officer onto the post with the editor's config (§5). */
  async commission(): Promise<void> {
    const pid = this.projectId();
    if (!pid || this.busy()) return;
    this.busy.set(true);
    this.message.set('');
    try {
      const resp = await firstValueFrom(
        this.api.commissionOfficer(pid, buildOfficerConfig(this.currentDraft())),
      );
      this.message.set(
        resp?.thread_id
          ? 'Commissioned — his first wake carries the continuity brief. Open log to watch him take the post.'
          : 'Commissioned.',
      );
      this.editorSeedKey = null;
      this.refresh(true);
    } catch (err) {
      this.message.set(this.errText(err, 'Commission failed'));
    } finally {
      this.busy.set(false);
    }
  }

  /** PATCH only what changed; the sitrep's capacity line carries the truth. */
  async saveEdits(): Promise<void> {
    const pid = this.projectId();
    const patch = this.pendingPatch();
    if (!pid || this.busy() || !Object.keys(patch).length) return;
    this.busy.set(true);
    this.message.set('');
    try {
      await firstValueFrom(this.api.updateOfficerPost(pid, patch));
      this.message.set(
        "Post updated — the next sitrep's capacity line carries the truth.",
      );
      this.editorSeedKey = null;
      this.refresh(true);
    } catch (err) {
      this.message.set(this.errText(err, 'Update failed'));
    } finally {
      this.busy.set(false);
    }
  }

  /**
   * Non-forced first: with jobs in flight the server answers with the warning
   * + list instead of decommissioning; the forced retry proceeds, leaving
   * them running (their completions land on the post's ledger).
   */
  async decommission(force: boolean): Promise<void> {
    const pid = this.projectId();
    if (!pid || this.busy()) return;
    this.busy.set(true);
    this.message.set('');
    try {
      const resp = await firstValueFrom(this.api.decommissionOfficer(pid, force));
      if (
        !force &&
        ((resp?.in_flight_jobs?.length ?? 0) > 0 || resp?.warning)
      ) {
        this.decommissionWarning.set(resp);
        return;
      }
      this.decommissionArmed.set(false);
      this.decommissionWarning.set(null);
      this.message.set(
        'Post decommissioned — kit, budgets, and fingerprints stay on the post for his successor.',
      );
      this.editorSeedKey = null;
      this.refresh(true);
    } catch (err) {
      this.message.set(this.errText(err, 'Decommission failed'));
    } finally {
      this.busy.set(false);
    }
  }

  cancelDecommission(): void {
    this.decommissionArmed.set(false);
    this.decommissionWarning.set(null);
  }

  /** Maintenance hold — commissioned, standing down; never self-healed away. */
  async hold(): Promise<void> {
    const pid = this.projectId();
    if (!pid || this.busy()) return;
    this.busy.set(true);
    this.message.set('');
    try {
      await firstValueFrom(this.api.holdOfficer(pid, this.holdNote()));
      this.holdArmed.set(false);
      this.holdNote.set('');
      this.message.set(
        'Held — dispatches pause and the watchdog stands down; your input still reaches him.',
      );
      this.refresh(true);
    } catch (err) {
      this.message.set(this.errText(err, 'Hold failed'));
    } finally {
      this.busy.set(false);
    }
  }

  async release(): Promise<void> {
    const pid = this.projectId();
    if (!pid || this.busy()) return;
    this.busy.set(true);
    this.message.set('');
    try {
      await firstValueFrom(this.api.releaseOfficer(pid));
      this.message.set('Released — queued events drain within a tick.');
      this.refresh(true);
    } catch (err) {
      this.message.set(this.errText(err, 'Release failed'));
    } finally {
      this.busy.set(false);
    }
  }

  /**
   * The one row-only field: PATCHes `communication_policy` alone, optimistic
   * so the select doesn't snap back while the write is in flight.
   */
  async setPolicy(value: string | null): Promise<void> {
    const pid = this.projectId();
    const v = value as WorkerMessagesPolicy | null;
    if (!pid || !v || v === this.policyValue() || this.busy()) return;
    const prev = this.post();
    this.post.update((p) =>
      p
        ? {
            ...p,
            communication_policy: {
              ...(p.communication_policy ?? {}),
              worker_messages: v,
            },
          }
        : p,
    );
    try {
      await firstValueFrom(
        this.api.updateOfficerPost(pid, {
          communication_policy: {worker_messages: v},
        }),
      );
      this.refresh(true);
    } catch (err) {
      this.post.set(prev);
      this.message.set(
        this.errText(err, 'Could not change worker-question routing'),
      );
    }
  }

  openLog(): void {
    const tid = this.post()?.officer?.thread_id;
    if (tid) void this.router.navigate(['/sessions', tid]);
  }

  async openConference(): Promise<void> {
    const pid = this.projectId();
    if (!pid || this.busy()) return;
    const existing = this.post()?.officer?.conference;
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
          buildConferenceThreadCreateBody(pid, this.projectName()),
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

  private errText(err: unknown, fallback: string): string {
    const detail = (err as {error?: {detail?: unknown}})?.error?.detail;
    return typeof detail === 'string' && detail ? detail : fallback;
  }
}
