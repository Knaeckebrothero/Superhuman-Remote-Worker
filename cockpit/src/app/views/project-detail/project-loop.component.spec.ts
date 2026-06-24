import {describe, it, expect} from 'vitest';

import {hasInFlightJob, isLoopWindingDown} from './project-loop.component';
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
