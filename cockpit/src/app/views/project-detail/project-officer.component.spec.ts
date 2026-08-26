import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  Component,
  EventEmitter,
  Injector,
  Input,
  Output,
  runInInjectionContext,
  ɵresolveComponentResources,
} from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { DatePipe, DecimalPipe } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { Router, RouterLink } from '@angular/router';
import { of, throwError } from 'rxjs';
import { TranslocoPipe, TranslocoService, TranslocoTestingModule } from '@jsverse/transloco';
import en from '../../../assets/i18n/en.json';
import de from '../../../assets/i18n/de-DE.json';

import {
  ProjectOfficerComponent,
  STARTER_SLOT_DRAFT,
  buildConferenceThreadCreateBody,
  buildOfficerConfig,
  buildOfficerPatch,
  buildSlotsSpec,
  draftFromPost,
  drainHint,
  holdBadgeLabel,
  immediacyLabel,
  kitChips,
  nextWakeLabel,
  postStateOf,
  rosterValidationIssue,
  vacantLedgerOf,
  type OfficerEditorDraft,
  type SlotDraft,
} from './project-officer.component';
import { ApiService } from '../../core/services/api.service';
import { NotificationService } from '../../core/services/notification.service';
import { ModelService } from '../../core/services/model.service';
import type { OfficerPost } from '../../core/models/api.model';

function translator(catalog: Record<string, unknown>) {
  return (key: string, params: Record<string, unknown> = {}): string => {
    const value = key
      .split('.')
      .reduce<unknown>(
        (node, part) =>
          node && typeof node === 'object' ? (node as Record<string, unknown>)[part] : undefined,
        catalog,
      );
    if (typeof value !== 'string') return key;
    return Object.entries(params).reduce(
      (text, [name, replacement]) => text.replaceAll(`{{${name}}}`, String(replacement)),
      value,
    );
  };
}

const trEn = translator(en as Record<string, unknown>);
const trDe = translator(de as Record<string, unknown>);

/**
 * Officer POST card (officer_post.md §8) — pure-function + signal-wiring
 * tests, following the project-detail convention (see the project-backlog
 * spec: TestBed.createComponent JIT-compiles child styleUrls and throws, so
 * the component is built bare via Injector.create and driven through the
 * exact signals/computeds the template reads). The kit spec assembled here is
 * hard-validated again server-side (validate_slots_spec); these tests pin
 * what the CARD may emit — endpoint paths/bodies are pinned again in
 * api.service.spec.ts against the wire.
 */

// ---------------------------------------------------------------------------
// Payload fixtures — the O1–O4 GET contract, one per card state.

function vacantPost(over: Partial<OfficerPost> = {}): OfficerPost {
  return {
    can_manage: true,
    commissioned: false,
    held: null,
    officer: null,
    kit: {},
    spend_today: { tokens: 0, ceiling: null },
    communication_policy: { worker_messages: 'user_direct', officer_response_minutes: 15 },
    incarnations: [],
    ...over,
  };
}

function commissionedPost(over: Partial<OfficerPost> = {}): OfficerPost {
  return {
    can_manage: true,
    commissioned: true,
    held: null,
    officer: {
      thread_id: 't-1',
      status: 'active',
      title: 'Centurion — Apollo',
      model: 'MiniMax-M3',
      reasoning_level: 'high',
      sleep_minutes: { min: 5, max: 60 },
      next_wake_at: null,
      pending_events: 2,
      token_ceiling: { daily: 5000000, deferred_today: false },
      conference: null,
    },
    kit: { line: { count: 2, model: 'MiniMax-M3', backend: 'vm', in_flight: 1 } },
    spend_today: { tokens: 1200000, ceiling: 5000000 },
    communication_policy: { worker_messages: 'officer_first', officer_response_minutes: 15 },
    incarnations: [],
    ...over,
  };
}

function heldPost(): OfficerPost {
  return commissionedPost({
    held: { kind: 'maintenance', since: '2026-08-01T15:10:00Z', note: 'migration' },
  });
}

function emptyDraft(over: Partial<OfficerEditorDraft> = {}): OfficerEditorDraft {
  return {
    slots: [],
    brainModel: '',
    reasoning: '',
    sleepMin: '',
    sleepMax: '',
    tokenCeiling: '',
    maxActions: '',
    maxWorkers: '',
    ...over,
  };
}

function createComponent(lang: 'en' | 'de-DE' = 'en') {
  const api = {
    getOfficerPost: vi.fn().mockReturnValue(of(vacantPost())),
    commissionOfficer: vi.fn().mockReturnValue(of({ thread_id: 't-new', status: 'commissioned' })),
    decommissionOfficer: vi.fn().mockReturnValue(of({ status: 'decommissioned' })),
    holdOfficer: vi.fn().mockReturnValue(of({ status: 'held' })),
    releaseOfficer: vi.fn().mockReturnValue(of({ status: 'released' })),
    recycleOfficer: vi.fn().mockReturnValue(of({ state: 'recycling', phase: 'awaiting_old_pod_exit' })),
    updateOfficerPost: vi.fn().mockReturnValue(of({ status: 'updated' })),
  };
  const router = { navigate: vi.fn().mockResolvedValue(true) };
  const http = { post: vi.fn().mockReturnValue(of({ thread_id: 'conf-1' })) };
  const feed = { listBySource: vi.fn().mockReturnValue(of(null)) };
  const translate = lang === 'de-DE' ? trDe : trEn;
  const transloco = {
    translate,
    getActiveLang: () => lang,
    langChanges$: of(lang),
  };
  const injector = Injector.create({
    providers: [
      { provide: ApiService, useValue: api },
      { provide: Router, useValue: router },
      { provide: ModelService, useValue: { load: vi.fn(), models: () => [] } },
      { provide: HttpClient, useValue: http },
      { provide: NotificationService, useValue: feed },
      { provide: TranslocoService, useValue: transloco },
    ],
  });
  const component = runInInjectionContext(injector, () => new ProjectOfficerComponent());
  // `projectId` is a required input only the rendering pipeline may write;
  // off-DOM (this repo's no-TestBed convention) the field is swapped for a
  // plain accessor — the class only ever CALLS this.projectId().
  (component as unknown as { projectId: () => string }).projectId = () => 'p-1';
  return { component, api, router, http, feed };
}

@Component({
  selector: 'app-button',
  standalone: true,
  template: `<button
    [disabled]="disabled"
    [attr.aria-label]="ariaLabel"
    (click)="clicked.emit($event)"
  >
    <ng-content />
  </button>`,
})
class RenderButtonStub {
  @Input() variant = '';
  @Input() size = '';
  @Input() disabled = false;
  @Input() ariaLabel = '';
  @Output() clicked = new EventEmitter<MouseEvent>();
}

@Component({
  selector: 'app-input',
  standalone: true,
  template: `<input
    [type]="type"
    [attr.aria-label]="ariaLabel"
    [value]="value"
    [placeholder]="placeholder"
  />`,
})
class RenderInputStub {
  @Input() type = 'text';
  @Input() ariaLabel = '';
  @Input() value = '';
  @Input() placeholder = '';
  @Output() changed = new EventEmitter<string>();
}

@Component({
  selector: 'app-select',
  standalone: true,
  template: `<select [attr.aria-label]="ariaLabel" [value]="value">
    <ng-content />
  </select>`,
})
class RenderSelectStub {
  @Input() ariaLabel = '';
  @Input() value: string | null = '';
  @Output() changed = new EventEmitter<string | null>();
}

@Component({
  selector: 'app-form-field',
  standalone: true,
  template: `<label>{{ label }}<ng-content /></label><span>{{ hint }}</span>`,
})
class RenderFormFieldStub {
  @Input() label = '';
  @Input() hint = '';
}

@Component({ selector: 'app-spinner', standalone: true, template: '' })
class RenderSpinnerStub {
  @Input() size = '';
  @Input() tone = '';
}

async function renderComponent(post: OfficerPost, lang: 'en' | 'de-DE') {
  await ɵresolveComponentResources(() => Promise.resolve(''));
  const api = {
    getOfficerPost: vi.fn().mockReturnValue(of(post)),
    commissionOfficer: vi.fn().mockReturnValue(of({ thread_id: 't-new', status: 'commissioned' })),
    decommissionOfficer: vi.fn().mockReturnValue(of({ status: 'decommissioned' })),
    holdOfficer: vi.fn().mockReturnValue(of({ status: 'held' })),
    releaseOfficer: vi.fn().mockReturnValue(of({ status: 'released' })),
    recycleOfficer: vi.fn().mockReturnValue(of({ state: 'recycling', phase: 'awaiting_old_pod_exit' })),
    updateOfficerPost: vi.fn().mockReturnValue(of({ status: 'updated' })),
  };
  TestBed.overrideComponent(ProjectOfficerComponent, {
    set: {
      imports: [
        DatePipe,
        DecimalPipe,
        TranslocoPipe,
        RouterLink,
        RenderButtonStub,
        RenderInputStub,
        RenderSelectStub,
        RenderFormFieldStub,
        RenderSpinnerStub,
      ],
    },
  });
  await TestBed.configureTestingModule({
    imports: [
      ProjectOfficerComponent,
      TranslocoTestingModule.forRoot({
        langs: { en, 'de-DE': de },
        translocoConfig: {
          availableLangs: ['en', 'de-DE'],
          defaultLang: lang,
        },
      }),
    ],
    providers: [
      { provide: ApiService, useValue: api },
      { provide: Router, useValue: { navigate: vi.fn().mockResolvedValue(true) } },
      {
        provide: ModelService,
        useValue: { load: vi.fn(), models: () => [] },
      },
      {
        provide: HttpClient,
        useValue: { post: vi.fn().mockReturnValue(of({ thread_id: 'conf-1' })) },
      },
      {
        provide: NotificationService,
        useValue: { listBySource: vi.fn().mockReturnValue(of(null)) },
      },
    ],
  }).compileComponents();
  TestBed.inject(TranslocoService).setActiveLang(lang);
  const fixture = TestBed.createComponent(ProjectOfficerComponent);
  Object.defineProperty(fixture.componentInstance, 'projectId', {
    value: () => 'p-1',
  });
  Object.defineProperty(fixture.componentInstance, 'projectName', {
    value: () => 'Apollo',
  });
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
  return { fixture, api };
}

// ---------------------------------------------------------------------------
// Recent notifications from this officer (the unified feed replaced the
// digest ring and the pages-per-day budget).

describe('recent notifications from this officer', () => {
  const page = (items: unknown[]) => ({ items, next_before: null, counts: { unseen: 0, unread: 0, pending: 0, by_category: {} } });

  it('a commissioned post lists the last ten feed rows about his session', () => {
    const { component, api, feed } = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    feed.listBySource.mockReturnValue(
      of(page([{ id: 'ntf-1', subject: 'Your centurion needs you', severity: 'high', created_at: '2026-08-26T09:00:00Z', resolved_at: null }])),
    );

    component.refresh();

    expect(feed.listBySource).toHaveBeenCalledWith('thread', 't-1', 10);
    expect(component.recentNotifications().map((n) => n.id)).toEqual(['ntf-1']);
  });

  it('a vacant post asks for nothing and clears the list', () => {
    const { component, api, feed } = createComponent();
    api.getOfficerPost.mockReturnValue(of(vacantPost()));

    component.refresh();

    expect(feed.listBySource).not.toHaveBeenCalled();
    expect(component.recentNotifications()).toEqual([]);
  });

  it('a transport failure keeps the previous list (stale beats wrong)', () => {
    const { component, api, feed } = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    feed.listBySource.mockReturnValueOnce(of(page([{ id: 'ntf-1', subject: 's', severity: 'normal', created_at: null, resolved_at: null }])));
    component.refresh();
    feed.listBySource.mockReturnValueOnce(of(null));

    component.refresh();

    expect(component.recentNotifications().map((n) => n.id)).toEqual(['ntf-1']);
  });
});

// ---------------------------------------------------------------------------
// The state machine and per-state labels.

describe('postStateOf', () => {
  it('is vacant without a payload or without a commission', () => {
    expect(postStateOf(null)).toBe('vacant');
    expect(postStateOf(vacantPost())).toBe('vacant');
  });

  it('is commissioned when a thread holds the post, held when standing down', () => {
    expect(postStateOf(commissionedPost())).toBe('commissioned');
    expect(postStateOf(heldPost())).toBe('held');
  });
});

describe('holdBadgeLabel', () => {
  it('names the hold kind — fixing the old hardcoded conference label', () => {
    expect(holdBadgeLabel({ kind: 'maintenance' }, trEn)).toBe('held — maintenance');
    expect(holdBadgeLabel({ kind: 'conference' }, trEn)).toBe('held — conference');
  });

  it('degrades to a bare badge without a kind', () => {
    expect(holdBadgeLabel({}, trEn)).toBe('held');
    expect(holdBadgeLabel(null, trEn)).toBe('held');
  });
});

describe('immediacyLabel (§7 per-field honesty, verbatim)', () => {
  it('slots and the flat cap land at next dispatch', () => {
    expect(immediacyLabel('slots', trEn)).toBe('applies at next dispatch');
    expect(immediacyLabel('max_concurrent_workers', trEn)).toBe('applies at next dispatch');
  });

  it('budget fields land at next delivery', () => {
    expect(immediacyLabel('daily_token_ceiling', trEn)).toBe('applies at next delivery');
  });

  it('sleep bounds land at the next sleep filing', () => {
    expect(immediacyLabel('sleep', trEn)).toBe('applies at next sleep filing');
  });

  it('brain and actions-per-wake are honestly deferred to the next respawn', () => {
    expect(immediacyLabel('brain', trEn)).toBe('applies on next respawn');
    expect(immediacyLabel('max_actions_per_wake', trEn)).toBe('applies on next respawn');
  });
});

describe('drainHint (shrink-below-in-flight is drain semantics, §7)', () => {
  it('names the drain when the new count is below in-flight', () => {
    expect(drainHint(2, 1, trEn)).toBe('2 in flight — drains to 1');
    expect(drainHint(3, 1, trEn)).toBe('3 in flight — drains to 1');
  });

  it('is silent at or above in-flight, and without live data', () => {
    expect(drainHint(2, 2, trEn)).toBeNull();
    expect(drainHint(1, 2, trEn)).toBeNull();
    expect(drainHint(0, 1, trEn)).toBeNull();
    expect(drainHint(undefined, 1, trEn)).toBeNull();
  });
});

describe('kitChips (utilization, not just allocation)', () => {
  it('renders in-flight over count when the GET carries utilization', () => {
    expect(
      kitChips({ line: { count: 2, model: 'MiniMax-M3', backend: 'vm', in_flight: 1 } }, trEn),
    ).toEqual([{ name: 'line', label: 'line 1/2 · MiniMax-M3 · vm', alert: false }]);
  });

  it('keeps in-flight utilization visible while a zero-capacity slot drains', () => {
    expect(kitChips({ line: { count: 0, in_flight: 2 } }, trEn)).toEqual([
      { name: 'line', label: 'line 2/0', alert: false },
    ]);
    expect(drainHint(2, 0, trEn)).toBe('2 in flight — drains to 0');
  });

  it('falls back to the ×N allocation chip without live data', () => {
    expect(kitChips({ line: { count: 2 } }, trEn)).toEqual([
      { name: 'line', label: 'line ×2', alert: false },
    ]);
    expect(kitChips(null, trEn)).toEqual([]);
  });
});

describe('backlogState (policy the officer can read, §6)', () => {
  afterEach(() => vi.restoreAllMocks());

  const withPools = (over: Partial<OfficerPost> = {}): OfficerPost =>
    commissionedPost({
      kit: { researchers: { count: 1, category: 'researcher', in_flight: 0 } },
      backlog: { auto_pull: true, breakers: {}, stale_claims: [] },
      ...over,
    });

  it('is null for a century with no pools — nothing to explain', () => {
    const { component, api } = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    component.refresh();
    expect(component.backlogState()).toBeNull();
  });

  it('surfaces auto-pull so an idle pool is never a mystery', () => {
    const { component, api } = createComponent();
    api.getOfficerPost.mockReturnValue(
      of(withPools({ backlog: { auto_pull: false, breakers: {}, stale_claims: [] } })),
    );
    component.refresh();
    expect(component.backlogState()?.auto_pull).toBe(false);
  });

  it('surfaces stalled claims, which are never released automatically', () => {
    const { component, api } = createComponent();
    api.getOfficerPost.mockReturnValue(
      of(
        withPools({
          backlog: {
            auto_pull: true,
            breakers: {},
            stale_claims: [
              {
                job_id: 'j-1',
                ticket_note_id: 'feature-a',
                status: 'pending_review',
                age_hours: 27,
              },
            ],
          },
        }),
      ),
    );
    component.refresh();
    expect(component.staleClaims()).toHaveLength(1);
    expect(component.staleClaims()[0].ticket_note_id).toBe('feature-a');
  });

  it('keeps provisioning, knowledge sync, and floor-wake outcomes distinct', () => {
    const { component, api } = createComponent();
    api.getOfficerPost.mockReturnValue(
      of(
        withPools({
          backlog: {
            auto_pull: false,
            breakers: {},
            stale_claims: [],
            provisioning_preflights: [
              {
                id: 'j-parked',
                status: 'paused',
                context: { provisioning_preflight: { state: 'retryable-failed', phase: 'cloud' } },
              },
            ],
            knowledge_materialization: [
              {
                id: 'intent-1',
                note_id: 'feature-a',
                canonical_state: 'canonical',
                projection_state: 'failed',
                retry_state: 'retryable',
              },
              {
                id: 'intent-2',
                note_id: 'feature-b',
                canonical_state: 'canonical',
                projection_state: 'synced',
                retry_state: 'none',
              },
            ],
            floor_wakes: [
              { id: 'wake-1', pool: 'researchers', state: 'retryable', attempt_count: 1 },
            ],
          },
        }),
      ),
    );
    component.refresh();
    expect(component.provisioningProblems()).toHaveLength(1);
    expect(component.provisioningStateSummary()).toBe('retryable failure');
    expect(component.knowledgeProblems()).toHaveLength(1);
    expect(component.knowledgeStateSummary()).toBe('canonical/failed');
    expect(component.latestFloorWake()?.state).toBe('retryable');
  });

  it('keeps correctness outcomes visible even when no slot is a backlog pool', () => {
    const { component, api } = createComponent();
    api.getOfficerPost.mockReturnValue(
      of(
        commissionedPost({
          backlog: {
            auto_pull: false,
            breakers: {},
            stale_claims: [],
            provisioning_preflights: [],
            knowledge_materialization: [
              {
                id: 'projection-only',
                note_id: 'legacy-ticket',
                canonical_state: 'failed',
                projection_state: 'projection_only',
                retry_state: 'permanent',
              },
            ],
            floor_wakes: [
              {
                id: 'attempted-not-queued',
                pool: 'manual',
                state: 'retryable',
                attempt_count: 1,
                failure_class: 'outbox',
              },
            ],
          },
        }),
      ),
    );
    component.refresh();
    expect(component.backlogState()).toBeNull();
    expect(component.knowledgeProblems()[0].projection_state).toBe('projection_only');
    expect(component.knowledgeStateSummary()).toBe('failed/projection only');
    expect(component.latestFloorWake()?.failure_class).toBe('outbox');
  });
});

describe('kitChips — pools (B6 of officer_backlog_pools.md §6)', () => {
  const future = new Date(Date.now() + 20 * 60_000).toISOString();
  const past = new Date(Date.now() - 60_000).toISOString();

  it('names the category and the ready depth for a pool', () => {
    expect(
      kitChips(
        {
          researchers: { count: 2, category: 'researcher', in_flight: 1, ready_depth: 4 },
        },
        trEn,
      ),
    ).toEqual([
      { name: 'researchers', label: 'researchers 1/2 · researcher · ready 4', alert: false },
    ]);
  });

  it('flags a pool sitting below its floor', () => {
    // The floor IS the slot count: if every agent lands at once, each must
    // find a ticket. An idle slot with a healthy queue is slack and fine.
    const chips = kitChips(
      {
        researchers: { count: 2, category: 'researcher', ready_depth: 1, below_floor: true },
      },
      trEn,
    );
    expect(chips[0].label).toContain('ready 1 — BELOW FLOOR');
    expect(chips[0].alert).toBe(true);
  });

  it('omits depth entirely when the knowledge base could not be read', () => {
    // Absent means unknown. Rendering "ready 0" would be an unmeasured claim
    // that the queue is starved.
    const chips = kitChips({ researchers: { count: 2, category: 'researcher' } }, trEn);
    expect(chips[0].label).toBe('researchers ×2 · researcher');
    expect(chips[0].alert).toBe(false);
  });

  it('an open breaker wins the flag — idle-because-broken is not idle-because-quiet', () => {
    const chips = kitChips({ testers: { count: 1, category: 'tester', ready_depth: 5 } }, trEn, {
      testers: { until: future },
    });
    expect(chips[0].label).toContain('BREAKER OPEN');
    expect(chips[0].alert).toBe(true);
  });

  it('an expired breaker is not rendered', () => {
    const chips = kitChips({ testers: { count: 1, category: 'tester', ready_depth: 5 } }, trEn, {
      testers: { until: past },
    });
    expect(chips[0].label).not.toContain('BREAKER');
    expect(chips[0].alert).toBe(false);
  });

  it('leaves uncategorized slots exactly as they were', () => {
    expect(kitChips({ line: { count: 2, model: 'M', backend: 'vm', in_flight: 1 } }, trEn)).toEqual(
      [{ name: 'line', label: 'line 1/2 · M · vm', alert: false }],
    );
  });
});

// ---------------------------------------------------------------------------
// Editor seeding and request bodies.

describe('draftFromPost', () => {
  it('seeds a never-kitted vacant post from the starter draft (§11 Q2: keep)', () => {
    expect(draftFromPost(null).slots).toEqual([STARTER_SLOT_DRAFT]);
    expect(draftFromPost(vacantPost()).slots).toEqual([STARTER_SLOT_DRAFT]);
  });

  it('seeds a vacant post from the row’s last real kit when one exists', () => {
    const post = vacantPost({ kit: { heavy: { count: 1, model: 'gpt-x', backend: 'vm' } } });
    expect(draftFromPost(post).slots).toEqual([
      {
        name: 'heavy',
        count: 1,
        model: 'gpt-x',
        backend: 'vm',
        category: '',
        spendCeilingDaily: null,
      },
    ]);
  });

  it('never invents the starter for a commissioned flat-cap officer', () => {
    expect(draftFromPost(commissionedPost({ kit: {} })).slots).toEqual([]);
  });

  it('preserves flat-cap for a vacant post with a prior incarnation', () => {
    expect(
      draftFromPost(
        vacantPost({
          kit: {},
          incarnations: [{ thread_id: 't-old', decommissioned_at: '2026-08-17' }],
        }),
      ).slots,
    ).toEqual([]);
  });

  it('populates the whole editor live when commissioned (in_flight stays out of the draft)', () => {
    expect(draftFromPost(commissionedPost())).toEqual({
      slots: [
        {
          name: 'line',
          count: 2,
          model: 'MiniMax-M3',
          backend: 'vm',
          category: '',
          spendCeilingDaily: null,
        },
      ],
      brainModel: 'MiniMax-M3',
      reasoning: 'high',
      sleepMin: '5',
      sleepMax: '60',
      tokenCeiling: '5000000',
      maxActions: '',
      maxWorkers: '',
    });
  });
});

describe('buildOfficerConfig (commission body)', () => {
  it('assembles the full body and omits blank fields (never nulls unseen row state)', () => {
    expect(
      buildOfficerConfig(
        emptyDraft({
          slots: [{ name: 'line', count: 2, model: '', backend: 'sandbox' }],
          brainModel: 'MiniMax-M3',
          reasoning: 'high',
          sleepMin: '5',
          sleepMax: '60',
          tokenCeiling: '5000000',
        }),
      ),
    ).toEqual({
      slots: { line: { count: 2, backend: 'sandbox' } },
      brain: { model: 'MiniMax-M3', reasoning_level: 'high' },
      sleep_min_minutes: 5,
      sleep_max_minutes: 60,
      daily_token_ceiling: 5000000,
    });
  });

  it('an empty roster is explicit in a commission body', () => {
    expect(buildOfficerConfig(emptyDraft())).toEqual({ slots: null });
  });
});

describe('buildOfficerPatch (per-field diff against the post)', () => {
  const baseline = draftFromPost(commissionedPost());

  it('an unedited editor patches nothing', () => {
    expect(buildOfficerPatch(baseline, { ...baseline })).toEqual({});
  });

  it('a slot count edit patches only the slots', () => {
    const draft = {
      ...baseline,
      slots: [{ name: 'line', count: 3, model: 'MiniMax-M3', backend: 'vm' }],
    };
    expect(buildOfficerPatch(baseline, draft)).toEqual({
      slots: { line: { count: 3, model: 'MiniMax-M3', backend: 'vm' } },
    });
  });

  it('removal and rename emit the complete desired roster without ghost keys', () => {
    const twoSlots = draftFromPost(
      commissionedPost({
        kit: {
          line: { count: 2, model: 'M1', backend: 'sandbox' },
          scout: { count: 1, model: 'M2', backend: 'virtual' },
        },
      }),
    );
    expect(buildOfficerPatch(twoSlots, { ...twoSlots, slots: [twoSlots.slots[1]] })).toEqual({
      slots: { scout: { count: 1, model: 'M2', backend: 'virtual' } },
    });
    expect(
      buildOfficerPatch(twoSlots, {
        ...twoSlots,
        slots: [{ ...twoSlots.slots[0], name: 'builders' }, twoSlots.slots[1]],
      }),
    ).toEqual({
      slots: {
        builders: { count: 2, model: 'M1', backend: 'sandbox' },
        scout: { count: 1, model: 'M2', backend: 'virtual' },
      },
    });
  });

  it('zero is preserved and removing every row restores flat-cap slots:null', () => {
    expect(
      buildOfficerPatch(baseline, {
        ...baseline,
        slots: [{ ...baseline.slots[0], count: 0 }],
      }),
    ).toEqual({
      slots: { line: { count: 0, model: 'MiniMax-M3', backend: 'vm' } },
    });
    expect(buildOfficerPatch(baseline, { ...baseline, slots: [] })).toEqual({
      slots: null,
    });
  });

  it('a cleared field patches as explicit null', () => {
    expect(buildOfficerPatch(baseline, { ...baseline, tokenCeiling: '' })).toEqual({
      daily_token_ceiling: null,
    });
    expect(buildOfficerPatch(baseline, { ...baseline, brainModel: '', reasoning: '' })).toEqual({
      brain: null,
    });
  });

  it('sleep bounds patch individually', () => {
    expect(buildOfficerPatch(baseline, { ...baseline, sleepMin: '10' })).toEqual({
      sleep_min_minutes: 10,
    });
  });

  it('never emits communication_policy — the routing control PATCHes that alone', () => {
    const draft = { ...baseline, sleepMin: '10', tokenCeiling: '' };
    expect(Object.keys(buildOfficerPatch(baseline, draft))).not.toContain('communication_policy');
  });
});

describe('vacantLedgerOf', () => {
  it('normalizes the {entries, dropped} ring', () => {
    const post = vacantPost({
      while_vacant: { entries: [{ job_id: 'j1', status: 'completed' }], dropped: 2 },
    });
    expect(vacantLedgerOf(post)).toEqual({
      entries: [{ job_id: 'j1', status: 'completed' }],
      dropped: 2,
    });
  });

  it('tolerates a bare-array payload while the O3 shape settles', () => {
    const post = vacantPost({
      while_vacant: [{ job_id: 'j1', status: 'failed' }] as unknown as OfficerPost['while_vacant'],
    });
    expect(vacantLedgerOf(post)).toEqual({
      entries: [{ job_id: 'j1', status: 'failed' }],
      dropped: 0,
    });
  });

  it('is null when absent or empty — the section stays hidden', () => {
    expect(vacantLedgerOf(vacantPost())).toBeNull();
    expect(vacantLedgerOf(vacantPost({ while_vacant: { entries: [] } }))).toBeNull();
    expect(vacantLedgerOf(null)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Component wiring — the card walked through its three states, and the
// lifecycle calls with their exact payloads.

describe('ProjectOfficerComponent state machine', () => {
  it('walks vacant → commissioned → held from the GET payload', () => {
    const { component, api } = createComponent();
    component.refresh();
    expect(component.postState()).toBe('vacant');
    expect(component.showImmediacy()).toBe(false);

    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    component.refresh(true);
    expect(component.postState()).toBe('commissioned');
    expect(component.showImmediacy()).toBe(true);
    expect(component.kitRows()).toEqual([
      { name: 'line', label: 'line 1/2 · MiniMax-M3 · vm', alert: false },
    ]);
    expect(component.spendCeiling()).toBe(5000000);

    api.getOfficerPost.mockReturnValue(of(heldPost()));
    component.refresh(true);
    expect(component.postState()).toBe('held');
    expect(component.holdLabel()).toBe('held — maintenance');
  });

  it('keeps the commissioned card when a poll fails — stale beats wrong-state', () => {
    const { component, api } = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    component.refresh();
    api.getOfficerPost.mockReturnValue(of(null));
    component.refresh(true);
    expect(component.postState()).toBe('commissioned');
  });

  it('surfaces the while-vacant ledger and newest-first incarnations on the vacant card', () => {
    const { component, api } = createComponent();
    api.getOfficerPost.mockReturnValue(
      of(
        vacantPost({
          while_vacant: { entries: [{ job_id: 'j1', status: 'completed' }], dropped: 1 },
          incarnations: [
            {
              thread_id: 't-0',
              commissioned_at: '2026-07-01',
              decommissioned_at: '2026-07-10',
              reason: 'upgrade',
            },
            {
              thread_id: 't-1',
              commissioned_at: '2026-08-01',
              decommissioned_at: '2026-08-05',
              reason: 'drain',
            },
          ],
        }),
      ),
    );
    component.refresh();
    expect(component.vacantLedger()?.entries).toHaveLength(1);
    expect(component.incarnations().map((i) => i.thread_id)).toEqual(['t-1', 't-0']);
  });

  it('refreshes management authority after an open-page role change', async () => {
    const { component, api } = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost({ can_manage: true })));
    component.refresh();
    expect(component.canManage()).toBe(true);

    api.getOfficerPost.mockReturnValue(of(commissionedPost({ can_manage: false })));
    component.refresh(true);
    expect(component.canManage()).toBe(false);
    component.patchSlot(0, { count: 9 });
    await component.saveEdits();
    await component.setPolicy('user_direct');
    await component.hold();
    expect(component.slotDrafts()[0].count).toBe(2);
    expect(api.updateOfficerPost).not.toHaveBeenCalled();
    expect(api.holdOfficer).not.toHaveBeenCalled();

    api.getOfficerPost.mockReturnValue(of(commissionedPost({ can_manage: true })));
    component.refresh(true);
    expect(component.canManage()).toBe(true);
  });
});

describe('ProjectOfficerComponent rendered authority and localization', () => {
  afterEach(() => TestBed.resetTestingModule());

  for (const lang of ['en', 'de-DE'] as const) {
    const tr = lang === 'de-DE' ? trDe : trEn;

    it(`keeps the ${lang} vacant card useful while hiding management from read-only users`, async () => {
      const { fixture } = await renderComponent(vacantPost({ can_manage: false }), lang);
      const root = fixture.nativeElement as HTMLElement;
      expect(root.querySelector('[data-testid="officer-card"]')?.getAttribute('aria-label')).toBe(
        tr('officerCard.a11y.region'),
      );
      expect(root.textContent).toContain(tr('officerCard.vacant.hint'));
      expect(root.textContent).toContain(tr('officerCard.readOnly'));
      expect(root.querySelector('[data-testid="officer-editor"]')).toBeNull();
      expect(root.querySelector('[data-testid="officer-commission"]')).toBeNull();
      expect(root.querySelector('[data-testid="officer-policy-read-only"]')?.textContent).toContain(
        tr('officerCard.policy.userDirect'),
      );
      fixture.destroy();
      TestBed.resetTestingModule();

      const manager = await renderComponent(vacantPost({ can_manage: true }), lang);
      const managerRoot = manager.fixture.nativeElement as HTMLElement;
      expect(managerRoot.querySelector('[data-testid="officer-editor"]')).not.toBeNull();
      expect(managerRoot.querySelector('[data-testid="officer-commission"]')).not.toBeNull();
      expect(managerRoot.textContent).toContain(tr('officerCard.actions.commission'));
      manager.fixture.destroy();
    });

    it(`renders ${lang} held, conference, backlog, and management states by capability`, async () => {
      const operational = commissionedPost({
        kit: {
          line: {
            count: 2,
            model: 'MiniMax-M3',
            backend: 'vm',
            category: 'executor',
            in_flight: 1,
          },
        },
        held: {
          kind: 'conference',
          since: '2026-08-01T15:10:00Z',
          note: 'planning',
        },
        conference: { thread_id: 'conf-1', status: 'active' },
        backlog: {
          auto_pull: false,
          breakers: {},
          stale_claim_policy: {
            threshold_minutes: 240,
            threshold_source: 'deployment_default',
          },
          stale_claims: [
            {
              job_id: 'j-1',
              ticket_note_id: 'feature-a',
              status: 'processing',
              age_hours: 27,
            },
          ],
        },
      });
      const viewer = await renderComponent({ ...operational, can_manage: false }, lang);
      let root = viewer.fixture.nativeElement as HTMLElement;
      expect(root.textContent).toContain(
        tr('officerCard.status.heldKind', {
          kind: tr('officerCard.holdKind.conference'),
        }),
      );
      expect(root.textContent).toContain(tr('officerCard.backlog.off'));
      expect(root.textContent).toContain(
        tr('officerCard.backlog.stalledDetail', {
          count: 1,
          threshold: 4,
          source: tr('officerCard.backlog.thresholdSource.deployment_default'),
        }),
      );
      expect(root.textContent).toContain(tr('officerCard.actions.openLog'));
      expect(root.textContent).not.toContain(tr('officerCard.actions.rejoinConference'));
      expect(root.querySelector('[data-testid="officer-release"]')).toBeNull();
      expect(root.querySelector('[data-testid="officer-save"]')).toBeNull();
      expect(root.querySelector('[data-testid="officer-recycle"]')).toBeNull();
      viewer.fixture.destroy();
      TestBed.resetTestingModule();

      const manager = await renderComponent({ ...operational, can_manage: true }, lang);
      root = manager.fixture.nativeElement as HTMLElement;
      expect(root.querySelector('[data-testid="officer-editor"]')).not.toBeNull();
      expect(root.querySelector('[data-testid="officer-save"]')).not.toBeNull();
      expect(root.querySelector('[data-testid="officer-release"]')).not.toBeNull();
      expect(root.querySelector('[data-testid="officer-recycle"]')).not.toBeNull();
      expect(root.textContent).toContain(tr('officerCard.actions.rejoinConference'));
      expect(root.querySelector('[data-testid="officer-policy"] app-select')).not.toBeNull();
      expect(root.querySelector('input')?.getAttribute('aria-label')).toBe(
        tr('officerCard.a11y.actionsPerWake'),
      );
      expect(root.querySelector('button[aria-label]')?.getAttribute('aria-label')).toContain(
        tr('officerCard.a11y.removeSlot', { name: 'line' }),
      );
      manager.fixture.destroy();
    });

    it(`renders the ${lang} runtime-authorization incident without credential detail`, async () => {
      const { fixture } = await renderComponent(
        commissionedPost({
          can_manage: false,
          runtime_authorization: {
            status: 'unavailable',
            failure_class: 'refresh_expired',
            operator_notification: 'delivered',
            planning_suppressed: true,
          },
        }),
        lang,
      );
      const alert = (fixture.nativeElement as HTMLElement).querySelector(
        '[data-testid="officer-runtime-authorization"]',
      );

      expect(alert?.getAttribute('role')).toBe('alert');
      expect(alert?.getAttribute('aria-label')).toBe(
        tr('officerCard.runtimeAuthorization.a11y'),
      );
      expect(alert?.textContent).toContain(tr('officerCard.runtimeAuthorization.unavailable'));
      expect(alert?.textContent).not.toContain('refresh_expired');
      fixture.destroy();
    });

    it(`renders the ${lang} automatic-reconciliation rollout fence truthfully`, async () => {
      const { fixture } = await renderComponent(
        commissionedPost({
          runtime_lifecycle: {
            observed_build_sha: 'old-build',
            expected_build_sha: 'new-build',
            drift_state: 'drifted',
            recycle_phase: 'idle',
            automatic_reconciliation_enabled: false,
          },
        }),
        lang,
      );
      const lifecycle = (fixture.nativeElement as HTMLElement).querySelector(
        '[data-testid="officer-runtime-lifecycle"]',
      );
      expect(lifecycle?.textContent).toContain(
        tr('officerCard.runtimeLifecycle.automatic.disabled'),
      );
      expect(lifecycle?.textContent).toContain('old-build');
      expect(lifecycle?.textContent).toContain('new-build');
      fixture.destroy();
    });

    it(`frames ${lang} mutation failures locally without exposing controls to stale authority`, async () => {
      const { fixture, api } = await renderComponent(commissionedPost(), lang);
      api.updateOfficerPost.mockReturnValue(
        throwError(() => ({ error: { detail: 'upstream detail' } })),
      );
      fixture.componentInstance.patchSlot(0, { count: 0 });
      await fixture.componentInstance.saveEdits();
      fixture.detectChanges();
      expect(
        (fixture.nativeElement as HTMLElement).querySelector('[data-testid="officer-message"]')
          ?.textContent,
      ).toContain(`${tr('officerCard.errors.update')}: upstream detail`);

      api.getOfficerPost.mockReturnValue(of(commissionedPost({ can_manage: false })));
      fixture.componentInstance.refresh(true);
      fixture.detectChanges();
      expect(
        (fixture.nativeElement as HTMLElement).querySelector('[data-testid="officer-editor"]'),
      ).toBeNull();
      api.updateOfficerPost.mockClear();
      await fixture.componentInstance.saveEdits();
      expect(api.updateOfficerPost).not.toHaveBeenCalled();
      fixture.destroy();
    });
  }
});

describe('ProjectOfficerComponent editor seeding', () => {
  it('accepts the inclusive 0–20 count range and clamps only outside it', () => {
    const { component } = createComponent();
    expect(component.toCount('0')).toBe(0);
    expect(component.toCount('20')).toBe(20);
    expect(component.toCount('-1')).toBe(0);
    expect(component.toCount('21')).toBe(20);
  });

  it('seeds once per incarnation and never clobbers edits on the 15s poll', () => {
    const { component, api } = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    component.refresh();
    expect(component.fBrainModel()).toBe('MiniMax-M3');
    expect(component.slotDrafts()).toEqual([
      {
        name: 'line',
        count: 2,
        model: 'MiniMax-M3',
        backend: 'vm',
        category: '',
        spendCeilingDaily: null,
      },
    ]);

    component.fBrainModel.set('other-model');
    component.patchSlot(0, { count: 1 });
    component.refresh(true); // same incarnation — the poll must not reseed
    expect(component.fBrainModel()).toBe('other-model');
    expect(component.slotDrafts()[0].count).toBe(1);

    api.getOfficerPost.mockReturnValue(of(vacantPost()));
    component.refresh(true); // decommissioned elsewhere — reseed for the vacant editor
    expect(component.fBrainModel()).toBe('');
    expect(component.slotDrafts()).toEqual([STARTER_SLOT_DRAFT]);
  });

  it('hints the drain when a slot draft shrinks below its in-flight count', () => {
    const { component, api } = createComponent();
    api.getOfficerPost.mockReturnValue(
      of(commissionedPost({ kit: { line: { count: 2, in_flight: 2 } } })),
    );
    component.refresh();
    expect(component.drainHints()).toEqual([null]);
    component.patchSlot(0, { count: 1 });
    expect(component.drainHints()).toEqual(['2 in flight — drains to 1']);
  });
});

describe('ProjectOfficerComponent lifecycle actions', () => {
  it('commissions with the full editor config and stays on the card', async () => {
    const { component, api, router } = createComponent();
    component.refresh(); // vacant — starter draft seeded
    await component.commission();
    expect(api.commissionOfficer).toHaveBeenCalledWith('p-1', {
      slots: { line: { count: 2, backend: 'sandbox' } },
    });
    expect(router.navigate).not.toHaveBeenCalled();
    expect(component.message()).toContain('continuity brief');
    expect(component.busy()).toBe(false);
  });

  it('clears the final durable slot when recommissioning a vacant post', async () => {
    const { component, api } = createComponent();
    api.getOfficerPost.mockReturnValue(
      of(
        vacantPost({
          kit: { line: { count: 2, model: 'MiniMax-M3', backend: 'sandbox' } },
          incarnations: [{ thread_id: 't-old', decommissioned_at: '2026-08-17' }],
        }),
      ),
    );
    component.refresh();
    component.removeSlot(0);

    await component.commission();

    expect(api.commissionOfficer).toHaveBeenCalledWith('p-1', { slots: null });
  });

  it('recommissions an established flat-cap post without inventing a starter', async () => {
    const { component, api } = createComponent();
    api.getOfficerPost.mockReturnValue(
      of(
        vacantPost({
          kit: {},
          incarnations: [{ thread_id: 't-old', decommissioned_at: '2026-08-17' }],
        }),
      ),
    );
    component.refresh();
    expect(component.slotDrafts()).toEqual([]);

    await component.commission();

    expect(api.commissionOfficer).toHaveBeenCalledWith('p-1', { slots: null });
  });

  it('PATCHes only the fields that changed, and nothing when clean', async () => {
    const { component, api } = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    component.refresh();

    await component.saveEdits(); // clean editor — no wire traffic
    expect(api.updateOfficerPost).not.toHaveBeenCalled();

    component.patchSlot(0, { count: 3 });
    expect(component.dirty()).toBe(true);
    await component.saveEdits();
    expect(api.updateOfficerPost).toHaveBeenCalledWith('p-1', {
      slots: { line: { count: 3, model: 'MiniMax-M3', backend: 'vm' } },
    });
  });

  it('a cleared ceiling pends as an explicit null patch', () => {
    const { component, api } = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    component.refresh();
    component.fTokenCeiling.set('');
    expect(component.pendingPatch()).toEqual({ daily_token_ceiling: null });
  });

  it('decommission warns on in-flight jobs, then forces through leaving them running', async () => {
    const { component, api } = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    component.refresh();
    api.decommissionOfficer
      .mockReturnValueOnce(
        of({
          warning: 'jobs in flight',
          in_flight_jobs: [{ job_id: 'j-1', slot: 'line', status: 'processing' }],
        }),
      )
      .mockReturnValueOnce(of({ status: 'decommissioned' }));

    component.decommissionArmed.set(true);
    await component.decommission(false);
    expect(api.decommissionOfficer).toHaveBeenCalledWith('p-1', false);
    expect(component.decommissionWarning()?.in_flight_jobs).toHaveLength(1);
    expect(component.decommissionArmed()).toBe(true); // waiting on the choice

    await component.decommission(true);
    expect(api.decommissionOfficer).toHaveBeenLastCalledWith('p-1', true);
    expect(component.decommissionWarning()).toBeNull();
    expect(component.decommissionArmed()).toBe(false);
    expect(component.message()).toContain('stay on the post');
  });

  it('a clean decommission needs no force and closes the confirm', async () => {
    const { component, api } = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    component.refresh();
    component.decommissionArmed.set(true);
    await component.decommission(false);
    expect(component.decommissionWarning()).toBeNull();
    expect(component.decommissionArmed()).toBe(false);
  });

  it('holds with the note, releases from the held card', async () => {
    const { component, api } = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    component.refresh();
    component.holdArmed.set(true);
    component.holdNote.set('migration window');
    await component.hold();
    expect(api.holdOfficer).toHaveBeenCalledWith('p-1', 'migration window');
    expect(component.holdArmed()).toBe(false);
    expect(component.holdNote()).toBe('');

    api.getOfficerPost.mockReturnValue(of(heldPost()));
    component.refresh(true);
    await component.release();
    expect(api.releaseOfficer).toHaveBeenCalledWith('p-1');
    expect(component.message()).toContain('drain within a tick');
  });

  it('starts one supported recycle and disables repeats while active', async () => {
    const { component, api } = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    component.refresh();
    await component.recycle();
    expect(api.recycleOfficer).toHaveBeenCalledWith('p-1');
    expect(component.message()).toContain('maintenance hold');

    api.getOfficerPost.mockReturnValue(
      of(
        commissionedPost({
          held: { kind: 'maintenance' },
          runtime_lifecycle: {
            drift_state: 'drifted',
            recycle_phase: 'awaiting_old_pod_exit',
          },
        }),
      ),
    );
    component.refresh(true);
    expect(component.recycleActive()).toBe(true);
    await component.recycle();
    expect(api.recycleOfficer).toHaveBeenCalledTimes(1);
  });
});

describe('ProjectOfficerComponent worker-question routing', () => {
  it('PATCHes communication_policy alone, and no-ops on the current value', async () => {
    const { component, api } = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost())); // officer_first
    component.refresh();

    await component.setPolicy('user_direct');
    expect(api.updateOfficerPost).toHaveBeenCalledWith('p-1', {
      communication_policy: { worker_messages: 'user_direct' },
    });

    api.updateOfficerPost.mockClear();
    await component.setPolicy('officer_first'); // refresh restored officer_first
    expect(api.updateOfficerPost).not.toHaveBeenCalled();
  });

  it('recommends Officer first only while an officer actually holds the post', () => {
    const { component, api } = createComponent();
    component.refresh(); // vacant
    expect(component.officerFirstLabel()).toBe('Officer first');
    expect(component.policyHint()).toContain('While the post is vacant');

    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    component.refresh(true);
    expect(component.officerFirstLabel()).toBe('Officer first (recommended)');
    expect(component.policyHint()).toContain('falls back to you');
  });
});

// ---------------------------------------------------------------------------
// Retained pure-function suites from the provision-era card.

describe('buildSlotsSpec', () => {
  const row = (over: Partial<SlotDraft> = {}): SlotDraft => ({
    name: 'line',
    count: 2,
    model: 'MiniMax-M3',
    backend: 'vm',
    // '' = not a pool. The default stays uncategorized on purpose: turning a
    // slot into a pool is an explicit act, never a side effect of editing it.
    category: '',
    ...over,
  });

  it('assembles named rows into the officer.slots shape', () => {
    expect(buildSlotsSpec([row()])).toEqual({
      line: { count: 2, model: 'MiniMax-M3', backend: 'vm' },
    });
  });

  it('returns null with no rows — flat cap, not an empty roster', () => {
    expect(buildSlotsSpec([])).toBeNull();
  });

  it('rejects and surfaces blank names instead of silently dropping rows', () => {
    expect(() => buildSlotsSpec([row({ name: '   ' })])).toThrowError('blank_slot_name');
    expect(rosterValidationIssue([row({ name: '' })])?.key).toBe(
      'officerCard.validation.blankSlot',
    );
  });

  it('normalizes names to lowercase and floors/clamps counts', () => {
    const spec = buildSlotsSpec([
      row({ name: '  Line ', count: 2.9 }),
      row({ name: 'scout', count: 0 }),
    ]);
    expect(spec).toEqual({
      line: { count: 2, model: 'MiniMax-M3', backend: 'vm' },
      scout: { count: 0, model: 'MiniMax-M3', backend: 'vm' },
    });
  });

  it('carries the category through — the field that makes a slot a pool', () => {
    // This builder is allow-list shaped, so a field it does not name is
    // silently dropped. Until category was added here, choosing one in the
    // form never reached the server and the slot was never a pool.
    expect(buildSlotsSpec([row({ name: 'researchers', category: 'Researcher' })])).toEqual({
      researchers: { count: 2, model: 'MiniMax-M3', backend: 'vm', category: 'researcher' },
    });
  });

  it('omits the category when the row is not a pool', () => {
    expect(buildSlotsSpec([row({ category: '  ' })])).toEqual({
      line: { count: 2, model: 'MiniMax-M3', backend: 'vm' },
    });
  });

  it('survives a draft assembled before the field existed', () => {
    const legacy = { name: 'line', count: 1, model: '', backend: '' } as SlotDraft;
    expect(buildSlotsSpec([legacy])).toEqual({ line: { count: 1 } });
  });

  it('rejects duplicate normalized names instead of allowing last-row-wins', () => {
    const duplicate = [row({ name: 'Line', count: 1 }), row({ name: ' line ', count: 3 })];
    expect(() => buildSlotsSpec(duplicate)).toThrowError('duplicate_slot_name');
    expect(rosterValidationIssue(duplicate)).toEqual({
      key: 'officerCard.validation.duplicateSlot',
      params: { name: 'line' },
    });
  });

  it('preserves an existing per-slot spend ceiling without exposing a new control', () => {
    expect(buildSlotsSpec([row({ spendCeilingDaily: 7.5 })])).toEqual({
      line: {
        count: 2,
        model: 'MiniMax-M3',
        backend: 'vm',
        spend_ceiling_daily: 7.5,
      },
    });
  });
});

describe('nextWakeLabel', () => {
  afterEach(() => vi.useRealTimers());

  it('reads event-driven when no timer is pending', () => {
    expect(nextWakeLabel(null, trEn)).toContain('not scheduled');
    expect(nextWakeLabel(undefined, trEn)).toContain('not scheduled');
  });

  it('renders a future fire_at as minutes from now', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-30T04:00:00Z'));
    expect(nextWakeLabel('2026-07-30T04:42:00Z', trEn)).toBe('in 42 min');
  });

  it('renders a just-due timer as due now', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-30T04:00:30Z'));
    expect(nextWakeLabel('2026-07-30T04:00:00Z', trEn)).toBe('due now');
  });

  it('renders a stale timer as overdue (the watchdog signal)', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-30T04:10:00Z'));
    expect(nextWakeLabel('2026-07-30T04:00:00Z', trEn)).toBe('overdue 10 min');
  });

  it('falls back to the raw value on garbage', () => {
    expect(nextWakeLabel('not-a-date', trEn)).toBe('not-a-date');
  });
});

describe('conference thread create request', () => {
  it('explicitly requests connector defaults for a conference', () => {
    expect(buildConferenceThreadCreateBody('project-1', 'Apollo', 'Conference')).toEqual({
      title: 'Conference — Apollo',
      config_name: 'centurion',
      project_ids: ['project-1'],
      use_datasource_defaults: true,
      config_override: { officer: { conference: true } },
    });
  });
});
