import {describe, it, expect, vi, afterEach} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {Router} from '@angular/router';
import {of} from 'rxjs';

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
  vacantLedgerOf,
  type OfficerEditorDraft,
  type SlotDraft,
} from './project-officer.component';
import {ApiService} from '../../core/services/api.service';
import {ModelService} from '../../core/services/model.service';
import type {OfficerPost} from '../../core/models/api.model';

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
    commissioned: false,
    held: null,
    officer: null,
    kit: {},
    spend_today: {tokens: 0, ceiling: null},
    communication_policy: {worker_messages: 'user_direct', officer_response_minutes: 15},
    incarnations: [],
    ...over,
  };
}

function commissionedPost(over: Partial<OfficerPost> = {}): OfficerPost {
  return {
    commissioned: true,
    held: null,
    officer: {
      thread_id: 't-1',
      status: 'active',
      title: 'Centurion — Apollo',
      model: 'MiniMax-M3',
      reasoning_level: 'high',
      sleep_minutes: {min: 5, max: 60},
      next_wake_at: null,
      pending_events: 2,
      pages_today: {used: 1, budget: 3},
      token_ceiling: {daily: 5000000, deferred_today: false},
      digest: [],
      conference: null,
    },
    kit: {line: {count: 2, model: 'MiniMax-M3', backend: 'vm', in_flight: 1}},
    spend_today: {tokens: 1200000, ceiling: 5000000},
    communication_policy: {worker_messages: 'officer_first', officer_response_minutes: 15},
    incarnations: [],
    ...over,
  };
}

function heldPost(): OfficerPost {
  return commissionedPost({
    held: {kind: 'maintenance', since: '2026-08-01T15:10:00Z', note: 'migration'},
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
    maxPages: '',
    maxActions: '',
    maxWorkers: '',
    ...over,
  };
}

function createComponent() {
  const api = {
    getOfficerPost: vi.fn().mockReturnValue(of(vacantPost())),
    commissionOfficer: vi.fn().mockReturnValue(of({thread_id: 't-new', status: 'commissioned'})),
    decommissionOfficer: vi.fn().mockReturnValue(of({status: 'decommissioned'})),
    holdOfficer: vi.fn().mockReturnValue(of({status: 'held'})),
    releaseOfficer: vi.fn().mockReturnValue(of({status: 'released'})),
    updateOfficerPost: vi.fn().mockReturnValue(of({status: 'updated'})),
  };
  const router = {navigate: vi.fn().mockResolvedValue(true)};
  const http = {post: vi.fn().mockReturnValue(of({thread_id: 'conf-1'}))};
  const injector = Injector.create({
    providers: [
      {provide: ApiService, useValue: api},
      {provide: Router, useValue: router},
      {provide: ModelService, useValue: {load: vi.fn(), models: () => []}},
      {provide: HttpClient, useValue: http},
    ],
  });
  const component = runInInjectionContext(
    injector,
    () => new ProjectOfficerComponent(),
  );
  // `projectId` is a required input only the rendering pipeline may write;
  // off-DOM (this repo's no-TestBed convention) the field is swapped for a
  // plain accessor — the class only ever CALLS this.projectId().
  (component as unknown as {projectId: () => string}).projectId = () => 'p-1';
  return {component, api, router, http};
}

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
    expect(holdBadgeLabel({kind: 'maintenance'})).toBe('held — maintenance');
    expect(holdBadgeLabel({kind: 'conference'})).toBe('held — conference');
  });

  it('degrades to a bare badge without a kind', () => {
    expect(holdBadgeLabel({})).toBe('held');
    expect(holdBadgeLabel(null)).toBe('held');
  });
});

describe('immediacyLabel (§7 per-field honesty, verbatim)', () => {
  it('slots and the flat cap land at next dispatch', () => {
    expect(immediacyLabel('slots')).toBe('applies at next dispatch');
    expect(immediacyLabel('max_concurrent_workers')).toBe('applies at next dispatch');
  });

  it('budget fields land at next delivery', () => {
    expect(immediacyLabel('daily_token_ceiling')).toBe('applies at next delivery');
    expect(immediacyLabel('max_pages_per_day')).toBe('applies at next delivery');
  });

  it('sleep bounds land at the next sleep filing', () => {
    expect(immediacyLabel('sleep')).toBe('applies at next sleep filing');
  });

  it('brain and actions-per-wake are honestly deferred to the next respawn', () => {
    expect(immediacyLabel('brain')).toBe('applies on next respawn');
    expect(immediacyLabel('max_actions_per_wake')).toBe('applies on next respawn');
  });
});

describe('drainHint (shrink-below-in-flight is drain semantics, §7)', () => {
  it('names the drain when the new count is below in-flight', () => {
    expect(drainHint(2, 1)).toBe('2 in flight — drains to 1');
    expect(drainHint(3, 1)).toBe('3 in flight — drains to 1');
  });

  it('is silent at or above in-flight, and without live data', () => {
    expect(drainHint(2, 2)).toBeNull();
    expect(drainHint(1, 2)).toBeNull();
    expect(drainHint(0, 1)).toBeNull();
    expect(drainHint(undefined, 1)).toBeNull();
  });
});

describe('kitChips (utilization, not just allocation)', () => {
  it('renders in-flight over count when the GET carries utilization', () => {
    expect(kitChips({line: {count: 2, model: 'MiniMax-M3', backend: 'vm', in_flight: 1}})).toEqual([
      {name: 'line', label: 'line 1/2 · MiniMax-M3 · vm', alert: false},
    ]);
  });

  it('falls back to the ×N allocation chip without live data', () => {
    expect(kitChips({line: {count: 2}})).toEqual([
      {name: 'line', label: 'line ×2', alert: false},
    ]);
    expect(kitChips(null)).toEqual([]);
  });
});

describe('backlogState (policy the officer can read, §6)', () => {
  afterEach(() => vi.restoreAllMocks());

  const withPools = (over: Partial<OfficerPost> = {}): OfficerPost =>
    commissionedPost({
      kit: {researchers: {count: 1, category: 'researcher', in_flight: 0}},
      backlog: {auto_pull: true, breakers: {}, stale_claims: []},
      ...over,
    });

  it('is null for a century with no pools — nothing to explain', () => {
    const {component, api} = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    component.refresh();
    expect(component.backlogState()).toBeNull();
  });

  it('surfaces auto-pull so an idle pool is never a mystery', () => {
    const {component, api} = createComponent();
    api.getOfficerPost.mockReturnValue(
      of(withPools({backlog: {auto_pull: false, breakers: {}, stale_claims: []}})),
    );
    component.refresh();
    expect(component.backlogState()?.auto_pull).toBe(false);
  });

  it('surfaces stalled claims, which are never released automatically', () => {
    const {component, api} = createComponent();
    api.getOfficerPost.mockReturnValue(
      of(
        withPools({
          backlog: {
            auto_pull: true,
            breakers: {},
            stale_claims: [
              {job_id: 'j-1', ticket_note_id: 'feature-a', status: 'pending_review', age_hours: 27},
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
    const {component, api} = createComponent();
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
                context: {provisioning_preflight: {state: 'retryable-failed', phase: 'cloud'}},
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
              {id: 'wake-1', pool: 'researchers', state: 'retryable', attempt_count: 1},
            ],
          },
        }),
      ),
    );
    component.refresh();
    expect(component.provisioningProblems()).toHaveLength(1);
    expect(component.provisioningStateSummary()).toBe('retryable-failed');
    expect(component.knowledgeProblems()).toHaveLength(1);
    expect(component.knowledgeStateSummary()).toBe('canonical/failed');
    expect(component.latestFloorWake()?.state).toBe('retryable');
  });

  it('keeps correctness outcomes visible even when no slot is a backlog pool', () => {
    const {component, api} = createComponent();
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
    expect(component.knowledgeStateSummary()).toBe('failed/projection_only');
    expect(component.latestFloorWake()?.failure_class).toBe('outbox');
  });
});

describe('kitChips — pools (B6 of officer_backlog_pools.md §6)', () => {
  const future = new Date(Date.now() + 20 * 60_000).toISOString();
  const past = new Date(Date.now() - 60_000).toISOString();

  it('names the category and the ready depth for a pool', () => {
    expect(
      kitChips({
        researchers: {count: 2, category: 'researcher', in_flight: 1, ready_depth: 4},
      }),
    ).toEqual([
      {name: 'researchers', label: 'researchers 1/2 · researcher · ready 4', alert: false},
    ]);
  });

  it('flags a pool sitting below its floor', () => {
    // The floor IS the slot count: if every agent lands at once, each must
    // find a ticket. An idle slot with a healthy queue is slack and fine.
    const chips = kitChips({
      researchers: {count: 2, category: 'researcher', ready_depth: 1, below_floor: true},
    });
    expect(chips[0].label).toContain('ready 1 — BELOW FLOOR');
    expect(chips[0].alert).toBe(true);
  });

  it('omits depth entirely when the knowledge base could not be read', () => {
    // Absent means unknown. Rendering "ready 0" would be an unmeasured claim
    // that the queue is starved.
    const chips = kitChips({researchers: {count: 2, category: 'researcher'}});
    expect(chips[0].label).toBe('researchers ×2 · researcher');
    expect(chips[0].alert).toBe(false);
  });

  it('an open breaker wins the flag — idle-because-broken is not idle-because-quiet', () => {
    const chips = kitChips(
      {testers: {count: 1, category: 'tester', ready_depth: 5}},
      {testers: {until: future}},
    );
    expect(chips[0].label).toContain('BREAKER OPEN');
    expect(chips[0].alert).toBe(true);
  });

  it('an expired breaker is not rendered', () => {
    const chips = kitChips(
      {testers: {count: 1, category: 'tester', ready_depth: 5}},
      {testers: {until: past}},
    );
    expect(chips[0].label).not.toContain('BREAKER');
    expect(chips[0].alert).toBe(false);
  });

  it('leaves uncategorized slots exactly as they were', () => {
    expect(kitChips({line: {count: 2, model: 'M', backend: 'vm', in_flight: 1}})).toEqual([
      {name: 'line', label: 'line 1/2 · M · vm', alert: false},
    ]);
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
    const post = vacantPost({kit: {heavy: {count: 1, model: 'gpt-x', backend: 'vm'}}});
    expect(draftFromPost(post).slots).toEqual([
      {name: 'heavy', count: 1, model: 'gpt-x', backend: 'vm', category: ''},
    ]);
  });

  it('never invents the starter for a commissioned flat-cap officer', () => {
    expect(draftFromPost(commissionedPost({kit: {}})).slots).toEqual([]);
  });

  it('populates the whole editor live when commissioned (in_flight stays out of the draft)', () => {
    expect(draftFromPost(commissionedPost())).toEqual({
      slots: [{name: 'line', count: 2, model: 'MiniMax-M3', backend: 'vm', category: ''}],
      brainModel: 'MiniMax-M3',
      reasoning: 'high',
      sleepMin: '5',
      sleepMax: '60',
      tokenCeiling: '5000000',
      maxPages: '3',
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
          slots: [{name: 'line', count: 2, model: '', backend: 'sandbox'}],
          brainModel: 'MiniMax-M3',
          reasoning: 'high',
          sleepMin: '5',
          sleepMax: '60',
          tokenCeiling: '5000000',
          maxPages: '3',
        }),
      ),
    ).toEqual({
      slots: {line: {count: 2, backend: 'sandbox'}},
      brain: {model: 'MiniMax-M3', reasoning_level: 'high'},
      sleep_min_minutes: 5,
      sleep_max_minutes: 60,
      daily_token_ceiling: 5000000,
      max_pages_per_day: 3,
    });
  });

  it('an untouched empty editor sends an empty partial', () => {
    expect(buildOfficerConfig(emptyDraft())).toEqual({});
  });
});

describe('buildOfficerPatch (per-field diff against the post)', () => {
  const baseline = draftFromPost(commissionedPost());

  it('an unedited editor patches nothing', () => {
    expect(buildOfficerPatch(baseline, {...baseline})).toEqual({});
  });

  it('a slot count edit patches only the slots', () => {
    const draft = {
      ...baseline,
      slots: [{name: 'line', count: 3, model: 'MiniMax-M3', backend: 'vm'}],
    };
    expect(buildOfficerPatch(baseline, draft)).toEqual({
      slots: {line: {count: 3, model: 'MiniMax-M3', backend: 'vm'}},
    });
  });

  it('a cleared field patches as explicit null', () => {
    expect(buildOfficerPatch(baseline, {...baseline, tokenCeiling: ''})).toEqual({
      daily_token_ceiling: null,
    });
    expect(
      buildOfficerPatch(baseline, {...baseline, brainModel: '', reasoning: ''}),
    ).toEqual({brain: null});
  });

  it('sleep bounds patch individually', () => {
    expect(buildOfficerPatch(baseline, {...baseline, sleepMin: '10'})).toEqual({
      sleep_min_minutes: 10,
    });
  });

  it('never emits communication_policy — the routing control PATCHes that alone', () => {
    const draft = {...baseline, sleepMin: '10', tokenCeiling: ''};
    expect(Object.keys(buildOfficerPatch(baseline, draft))).not.toContain(
      'communication_policy',
    );
  });
});

describe('vacantLedgerOf', () => {
  it('normalizes the {entries, dropped} ring', () => {
    const post = vacantPost({
      while_vacant: {entries: [{job_id: 'j1', status: 'completed'}], dropped: 2},
    });
    expect(vacantLedgerOf(post)).toEqual({
      entries: [{job_id: 'j1', status: 'completed'}],
      dropped: 2,
    });
  });

  it('tolerates a bare-array payload while the O3 shape settles', () => {
    const post = vacantPost({
      while_vacant: [{job_id: 'j1', status: 'failed'}] as unknown as OfficerPost['while_vacant'],
    });
    expect(vacantLedgerOf(post)).toEqual({
      entries: [{job_id: 'j1', status: 'failed'}],
      dropped: 0,
    });
  });

  it('is null when absent or empty — the section stays hidden', () => {
    expect(vacantLedgerOf(vacantPost())).toBeNull();
    expect(vacantLedgerOf(vacantPost({while_vacant: {entries: []}}))).toBeNull();
    expect(vacantLedgerOf(null)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Component wiring — the card walked through its three states, and the
// lifecycle calls with their exact payloads.

describe('ProjectOfficerComponent state machine', () => {
  it('walks vacant → commissioned → held from the GET payload', () => {
    const {component, api} = createComponent();
    component.refresh();
    expect(component.postState()).toBe('vacant');
    expect(component.showImmediacy()).toBe(false);

    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    component.refresh(true);
    expect(component.postState()).toBe('commissioned');
    expect(component.showImmediacy()).toBe(true);
    expect(component.kitRows()).toEqual([
      {name: 'line', label: 'line 1/2 · MiniMax-M3 · vm', alert: false},
    ]);
    expect(component.spendCeiling()).toBe(5000000);

    api.getOfficerPost.mockReturnValue(of(heldPost()));
    component.refresh(true);
    expect(component.postState()).toBe('held');
    expect(component.holdLabel()).toBe('held — maintenance');
  });

  it('keeps the commissioned card when a poll fails — stale beats wrong-state', () => {
    const {component, api} = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    component.refresh();
    api.getOfficerPost.mockReturnValue(of(null));
    component.refresh(true);
    expect(component.postState()).toBe('commissioned');
  });

  it('surfaces the while-vacant ledger and newest-first incarnations on the vacant card', () => {
    const {component, api} = createComponent();
    api.getOfficerPost.mockReturnValue(
      of(
        vacantPost({
          while_vacant: {entries: [{job_id: 'j1', status: 'completed'}], dropped: 1},
          incarnations: [
            {thread_id: 't-0', commissioned_at: '2026-07-01', decommissioned_at: '2026-07-10', reason: 'upgrade'},
            {thread_id: 't-1', commissioned_at: '2026-08-01', decommissioned_at: '2026-08-05', reason: 'drain'},
          ],
        }),
      ),
    );
    component.refresh();
    expect(component.vacantLedger()?.entries).toHaveLength(1);
    expect(component.incarnations().map((i) => i.thread_id)).toEqual(['t-1', 't-0']);
  });
});

describe('ProjectOfficerComponent editor seeding', () => {
  it('seeds once per incarnation and never clobbers edits on the 15s poll', () => {
    const {component, api} = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    component.refresh();
    expect(component.fBrainModel()).toBe('MiniMax-M3');
    expect(component.slotDrafts()).toEqual([
      {name: 'line', count: 2, model: 'MiniMax-M3', backend: 'vm', category: ''},
    ]);

    component.fBrainModel.set('other-model');
    component.patchSlot(0, {count: 1});
    component.refresh(true); // same incarnation — the poll must not reseed
    expect(component.fBrainModel()).toBe('other-model');
    expect(component.slotDrafts()[0].count).toBe(1);

    api.getOfficerPost.mockReturnValue(of(vacantPost()));
    component.refresh(true); // decommissioned elsewhere — reseed for the vacant editor
    expect(component.fBrainModel()).toBe('');
    expect(component.slotDrafts()).toEqual([STARTER_SLOT_DRAFT]);
  });

  it('hints the drain when a slot draft shrinks below its in-flight count', () => {
    const {component, api} = createComponent();
    api.getOfficerPost.mockReturnValue(
      of(commissionedPost({kit: {line: {count: 2, in_flight: 2}}})),
    );
    component.refresh();
    expect(component.drainHints()).toEqual([null]);
    component.patchSlot(0, {count: 1});
    expect(component.drainHints()).toEqual(['2 in flight — drains to 1']);
  });
});

describe('ProjectOfficerComponent lifecycle actions', () => {
  it('commissions with the full editor config and stays on the card', async () => {
    const {component, api, router} = createComponent();
    component.refresh(); // vacant — starter draft seeded
    await component.commission();
    expect(api.commissionOfficer).toHaveBeenCalledWith('p-1', {
      slots: {line: {count: 2, backend: 'sandbox'}},
    });
    expect(router.navigate).not.toHaveBeenCalled();
    expect(component.message()).toContain('continuity brief');
    expect(component.busy()).toBe(false);
  });

  it('PATCHes only the fields that changed, and nothing when clean', async () => {
    const {component, api} = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    component.refresh();

    await component.saveEdits(); // clean editor — no wire traffic
    expect(api.updateOfficerPost).not.toHaveBeenCalled();

    component.patchSlot(0, {count: 3});
    expect(component.dirty()).toBe(true);
    await component.saveEdits();
    expect(api.updateOfficerPost).toHaveBeenCalledWith('p-1', {
      slots: {line: {count: 3, model: 'MiniMax-M3', backend: 'vm'}},
    });
  });

  it('a cleared ceiling pends as an explicit null patch', () => {
    const {component, api} = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    component.refresh();
    component.fTokenCeiling.set('');
    expect(component.pendingPatch()).toEqual({daily_token_ceiling: null});
  });

  it('decommission warns on in-flight jobs, then forces through leaving them running', async () => {
    const {component, api} = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    component.refresh();
    api.decommissionOfficer
      .mockReturnValueOnce(
        of({
          warning: 'jobs in flight',
          in_flight_jobs: [{job_id: 'j-1', slot: 'line', status: 'processing'}],
        }),
      )
      .mockReturnValueOnce(of({status: 'decommissioned'}));

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
    const {component, api} = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost()));
    component.refresh();
    component.decommissionArmed.set(true);
    await component.decommission(false);
    expect(component.decommissionWarning()).toBeNull();
    expect(component.decommissionArmed()).toBe(false);
  });

  it('holds with the note, releases from the held card', async () => {
    const {component, api} = createComponent();
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
});

describe('ProjectOfficerComponent worker-question routing', () => {
  it('PATCHes communication_policy alone, and no-ops on the current value', async () => {
    const {component, api} = createComponent();
    api.getOfficerPost.mockReturnValue(of(commissionedPost())); // officer_first
    component.refresh();

    await component.setPolicy('user_direct');
    expect(api.updateOfficerPost).toHaveBeenCalledWith('p-1', {
      communication_policy: {worker_messages: 'user_direct'},
    });

    api.updateOfficerPost.mockClear();
    await component.setPolicy('officer_first'); // refresh restored officer_first
    expect(api.updateOfficerPost).not.toHaveBeenCalled();
  });

  it('recommends Officer first only while an officer actually holds the post', () => {
    const {component, api} = createComponent();
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
      line: {count: 2, model: 'MiniMax-M3', backend: 'vm'},
    });
  });

  it('returns null with no usable rows — flat cap, not an empty roster', () => {
    expect(buildSlotsSpec([])).toBeNull();
    expect(buildSlotsSpec([row({name: '   '})])).toBeNull();
  });

  it('drops blank names and omits blank model/backend', () => {
    const spec = buildSlotsSpec([
      row({name: 'heavy', count: 1, model: '', backend: ''}),
      row({name: ''}),
    ]);
    expect(spec).toEqual({heavy: {count: 1}});
  });

  it('normalizes names to lowercase and floors/clamps counts', () => {
    const spec = buildSlotsSpec([
      row({name: '  Line ', count: 2.9}),
      row({name: 'scout', count: 0}),
    ]);
    expect(spec).toEqual({
      line: {count: 2, model: 'MiniMax-M3', backend: 'vm'},
      scout: {count: 1, model: 'MiniMax-M3', backend: 'vm'},
    });
  });

  it('carries the category through — the field that makes a slot a pool', () => {
    // This builder is allow-list shaped, so a field it does not name is
    // silently dropped. Until category was added here, choosing one in the
    // form never reached the server and the slot was never a pool.
    expect(buildSlotsSpec([row({name: 'researchers', category: 'Researcher'})])).toEqual({
      researchers: {count: 2, model: 'MiniMax-M3', backend: 'vm', category: 'researcher'},
    });
  });

  it('omits the category when the row is not a pool', () => {
    expect(buildSlotsSpec([row({category: '  '})])).toEqual({
      line: {count: 2, model: 'MiniMax-M3', backend: 'vm'},
    });
  });

  it('survives a draft assembled before the field existed', () => {
    const legacy = {name: 'line', count: 1, model: '', backend: ''} as SlotDraft;
    expect(buildSlotsSpec([legacy])).toEqual({line: {count: 1}});
  });

  it('last row wins a duplicate name (the server 400s ambiguity anyway)', () => {
    const spec = buildSlotsSpec([
      row({name: 'line', count: 1}),
      row({name: 'line', count: 3}),
    ]);
    expect(spec).toEqual({line: {count: 3, model: 'MiniMax-M3', backend: 'vm'}});
  });
});

describe('nextWakeLabel', () => {
  afterEach(() => vi.useRealTimers());

  it('reads event-driven when no timer is pending', () => {
    expect(nextWakeLabel(null)).toContain('not scheduled');
    expect(nextWakeLabel(undefined)).toContain('not scheduled');
  });

  it('renders a future fire_at as minutes from now', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-30T04:00:00Z'));
    expect(nextWakeLabel('2026-07-30T04:42:00Z')).toBe('in 42 min');
  });

  it('renders a just-due timer as due now', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-30T04:00:30Z'));
    expect(nextWakeLabel('2026-07-30T04:00:00Z')).toBe('due now');
  });

  it('renders a stale timer as overdue (the watchdog signal)', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-30T04:10:00Z'));
    expect(nextWakeLabel('2026-07-30T04:00:00Z')).toBe('overdue 10 min');
  });

  it('falls back to the raw value on garbage', () => {
    expect(nextWakeLabel('not-a-date')).toBe('not-a-date');
  });
});

describe('conference thread create request', () => {
  it('explicitly requests connector defaults for a conference', () => {
    expect(buildConferenceThreadCreateBody('project-1', 'Apollo')).toEqual({
      title: 'Conference — Apollo',
      config_name: 'centurion',
      project_ids: ['project-1'],
      use_datasource_defaults: true,
      config_override: {officer: {conference: true}},
    });
  });
});
