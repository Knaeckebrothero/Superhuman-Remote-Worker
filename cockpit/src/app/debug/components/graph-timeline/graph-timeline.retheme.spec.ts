import {describe, expect, it, vi} from 'vitest';
import {GraphTimelineComponent} from './graph-timeline.component';

describe('GraphTimelineComponent.rethemeGraph', () => {
  it('re-applies concrete (var-free) styles to the cytoscape instance', () => {
    const update = vi.fn();
    const style = vi.fn(() => ({update}));
    const comp = Object.create(GraphTimelineComponent.prototype);
    comp.cy = {style};
    comp.ngZone = {runOutsideAngular: (fn: () => void) => fn()};
    comp.rethemeGraph();
    expect(style).toHaveBeenCalledOnce();
    const applied = JSON.stringify(style.mock.calls[0][0]);
    expect(applied).not.toContain('var(');
    expect(update).toHaveBeenCalledOnce();
  });

  it('no-ops safely when cy is not yet created', () => {
    const comp = Object.create(GraphTimelineComponent.prototype);
    comp.cy = undefined;
    expect(() => comp.rethemeGraph()).not.toThrow();
  });
});
