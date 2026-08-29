import {describe, it, expect} from 'vitest';

import {
  buildRoleSequence,
  campaignProgressLabel,
  campaignStageState,
  formatRoleSequence,
  formatStage,
  hasInFlightJob,
  isAnalysisRole,
  isLoopWindingDown,
  plannerIneligibility,
  workerExpertsOnly,
} from './project-loop.component';
import type {Job, LoopCampaign, ProjectLoop} from '../../core/models/api.model';

/**
 * Pure-function tests for the loop wind-down logic (the project convention is to
 * test extracted functions, not the component via TestBed). Covers the graceful
 * stop window: the loop is terminal but its last job is still finishing.
 */

const job = (status: string): Pick<Job, 'status'> => ({status}) as Job;
const loop = (status: string): Pick<ProjectLoop, 'status'> =>
  ({status}) as ProjectLoop;

describe('hasInFlightJob', () => {
  it('is false with no jobs', () => {
    expect(hasInFlightJob([])).toBe(false);
  });

  it('is false when every job is terminal', () => {
    expect(
      hasInFlightJob([job('completed'), job('failed'), job('cancelled')]),
    ).toBe(false);
  });

  it('is true when any job is still non-terminal', () => {
    expect(hasInFlightJob([job('completed'), job('processing')])).toBe(true);
    expect(hasInFlightJob([job('paused')])).toBe(true);
    expect(hasInFlightJob([job('created')])).toBe(true);
  });
});

describe('isLoopWindingDown', () => {
  it('is false when there is no loop', () => {
    expect(isLoopWindingDown(null, [job('processing')])).toBe(false);
  });

  it('is false while the loop is still active (running/paused)', () => {
    expect(isLoopWindingDown(loop('running'), [job('processing')])).toBe(false);
    expect(isLoopWindingDown(loop('paused'), [job('processing')])).toBe(false);
  });

  it('is true when the loop is stopped but a job is still finishing', () => {
    expect(
      isLoopWindingDown(loop('stopped'), [job('completed'), job('processing')]),
    ).toBe(true);
  });

  it('is false once a stopped loop has no in-flight jobs left', () => {
    expect(isLoopWindingDown(loop('stopped'), [job('completed')])).toBe(false);
  });

  it('also covers completed/failed loops with a trailing in-flight job', () => {
    expect(isLoopWindingDown(loop('completed'), [job('processing')])).toBe(true);
    expect(isLoopWindingDown(loop('failed'), [job('completed')])).toBe(false);
  });
});

describe('workerExpertsOnly', () => {
  it('keeps bundled experts (no expert_type)', () => {
    expect(workerExpertsOnly([{id: 'scholar'}, {id: 'critic'}])).toHaveLength(2);
  });

  it('keeps worker experts and drops session experts', () => {
    expect(
      workerExpertsOnly([
        {id: 'a', expert_type: 'worker'},
        {id: 'b', expert_type: 'session'},
        {id: 'c', expert_type: 'worker'},
      ]),
    ).toEqual([
      {id: 'a', expert_type: 'worker'},
      {id: 'c', expert_type: 'worker'},
    ]);
  });

  it('keeps a session expert tagged worker (U1: tags are additive role metadata)', () => {
    expect(
      workerExpertsOnly([
        {id: 'a', expert_type: 'session', tags: ['session', 'worker']},
        {id: 'b', expert_type: 'session', tags: ['session']},
        {id: 'c', expert_type: 'worker', tags: ['worker']},
      ]).map((e) => e.id),
    ).toEqual(['a', 'c']);
  });

  it('is empty for empty input', () => {
    expect(workerExpertsOnly([])).toEqual([]);
  });
});

describe('buildRoleSequence', () => {
  it('returns the preset roles in preset mode (ignores custom stages)', () => {
    expect(
      buildRoleSequence('preset', [['ignored']], ['scholar', 'critic']),
    ).toEqual(['scholar', 'critic']);
  });

  it('collapses single-role stages to bare strings in custom mode', () => {
    expect(
      buildRoleSequence(
        'custom',
        [['scholar-fast'], ['critic'], ['developer']],
        ['scholar'],
      ),
    ).toEqual(['scholar-fast', 'critic', 'developer']);
  });

  it('keeps a multi-role step as a parallel (nested) stage', () => {
    expect(
      buildRoleSequence(
        'custom',
        [['scholar', 'product-qa'], ['critic'], ['developer']],
        [],
      ),
    ).toEqual([['scholar', 'product-qa'], 'critic', 'developer']);
  });

  it('trims whitespace, de-dupes within a stage, and drops blank steps', () => {
    expect(
      buildRoleSequence(
        'custom',
        [['  scholar  ', 'scholar'], [''], ['   '], ['critic']],
        [],
      ),
    ).toEqual(['scholar', 'critic']);
  });

  it('is empty when every custom stage is blank (blocks an empty start)', () => {
    expect(buildRoleSequence('custom', [['', '  ']], ['scholar'])).toEqual([]);
  });
});

describe('isAnalysisRole', () => {
  it('accepts the three analysis roles (trimmed)', () => {
    expect(isAnalysisRole('scholar')).toBe(true);
    expect(isAnalysisRole('critic')).toBe(true);
    expect(isAnalysisRole('  product-qa ')).toBe(true);
  });

  it('rejects execution and unknown roles', () => {
    expect(isAnalysisRole('developer')).toBe(false);
    expect(isAnalysisRole('default')).toBe(false);
    expect(isAnalysisRole('my-custom-expert')).toBe(false);
    expect(isAnalysisRole('')).toBe(false);
  });
});

describe('formatStage / formatRoleSequence', () => {
  it('renders a bare role unchanged', () => {
    expect(formatStage('scholar')).toBe('scholar');
  });

  it('joins a fan-out stage with the parallel separator', () => {
    expect(formatStage(['scholar', 'product-qa'])).toBe('scholar ∥ product-qa');
  });

  it('renders a full mixed rotation with arrows and parallel bars', () => {
    expect(
      formatRoleSequence([['scholar', 'product-qa'], 'critic', 'developer']),
    ).toBe('scholar ∥ product-qa → critic → developer');
  });
});

describe('plannerIneligibility (client mirror of planner_slots)', () => {
  it('accepts the build preset (scholar → critic → developer)', () => {
    expect(plannerIneligibility(['scholar', 'critic', 'developer'])).toBeNull();
  });

  it('accepts a fan-out analysis stage before the critic', () => {
    expect(
      plannerIneligibility([['scholar', 'product-qa'], 'critic', 'developer']),
    ).toBeNull();
  });

  it('accepts the critic as the LAST step (execution slot wraps cyclically)', () => {
    expect(plannerIneligibility(['developer', 'critic'])).toBeNull();
  });

  it('rejects a critic inside a fan-out step', () => {
    expect(
      plannerIneligibility([['scholar', 'critic'], 'developer']),
    ).toMatch(/own step/);
  });

  it('rejects zero critic steps', () => {
    expect(plannerIneligibility(['scholar', 'developer'])).toMatch(/found 0/);
  });

  it('rejects two critic steps', () => {
    expect(
      plannerIneligibility(['critic', 'developer', 'critic']),
    ).toMatch(/found 2/);
  });

  it('rejects a critic-only rotation (no execution slot)', () => {
    expect(plannerIneligibility(['critic'])).toMatch(/at least one other step/);
  });

  it('rejects a fan-out execution slot after the critic', () => {
    expect(
      plannerIneligibility(['critic', ['scholar', 'product-qa']]),
    ).toMatch(/single role/);
  });
});

const campaign = (over: Partial<LoopCampaign>): LoopCampaign =>
  ({
    id: 'c-1',
    plan_job_id: 'c-1',
    initiative_note_id: 'note-1',
    title: 'Ship the dice roller',
    stages: [{role: 'developer'}, {role: 'developer'}, {role: 'product-qa'}],
    acceptance: [],
    cursor: 0,
    stages_done: 0,
    member_failures: 0,
    extensions_used: 0,
    status: 'active',
    ...over,
  }) as LoopCampaign;

describe('campaignStageState', () => {
  it('marks the freshly-spawned first stage current, rest pending', () => {
    const c = campaign({cursor: 1, stages_done: 0});
    expect(campaignStageState(c, 0)).toBe('current');
    expect(campaignStageState(c, 1)).toBe('pending');
    expect(campaignStageState(c, 2)).toBe('pending');
  });

  it('marks finished stages done and the running one current', () => {
    const c = campaign({cursor: 2, stages_done: 1});
    expect(campaignStageState(c, 0)).toBe('done');
    expect(campaignStageState(c, 1)).toBe('current');
    expect(campaignStageState(c, 2)).toBe('pending');
  });

  it('a stale cursor (torn advance) never marks a done stage current', () => {
    // cursor lagged behind stages_done — the healed write-back fixes the row,
    // but the UI must not point "current" at an already-finished stage.
    const c = campaign({cursor: 1, stages_done: 2});
    expect(campaignStageState(c, 0)).toBe('done');
    expect(campaignStageState(c, 1)).toBe('done');
    expect(campaignStageState(c, 2)).toBe('current');
  });

  it('review status has no current stage — everything is done', () => {
    const c = campaign({status: 'review', cursor: 3, stages_done: 3});
    expect(campaignStageState(c, 0)).toBe('done');
    expect(campaignStageState(c, 2)).toBe('done');
  });

  it('aborted status leaves unrun stages pending, none current', () => {
    const c = campaign({status: 'aborted', cursor: 2, stages_done: 2});
    expect(campaignStageState(c, 1)).toBe('done');
    expect(campaignStageState(c, 2)).toBe('pending');
  });
});

describe('campaignProgressLabel', () => {
  it('active: names the running stage', () => {
    expect(campaignProgressLabel(campaign({cursor: 2, stages_done: 1}))).toBe(
      'stage 2 of 3',
    );
  });

  it('active with a stale cursor still reports a sane stage number', () => {
    expect(campaignProgressLabel(campaign({cursor: 0, stages_done: 1}))).toBe(
      'stage 2 of 3',
    );
  });

  it('review: reports completion and the pending verdict', () => {
    expect(
      campaignProgressLabel(campaign({status: 'review', cursor: 3, stages_done: 3})),
    ).toBe("all 3 stage(s) done — awaiting the critic's verdict");
  });

  it('aborted: reports how far it got', () => {
    expect(
      campaignProgressLabel(campaign({status: 'aborted', cursor: 2, stages_done: 2})),
    ).toBe('aborted after 2 of 3 stage(s)');
  });
});
