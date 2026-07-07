import {describe, it, expect} from 'vitest';

import {
  buildRoleSequence,
  formatRoleSequence,
  formatStage,
  hasInFlightJob,
  isAnalysisRole,
  isLoopWindingDown,
  workerExpertsOnly,
} from './project-loop.component';
import type {Job, ProjectLoop} from '../../core/models/api.model';

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
